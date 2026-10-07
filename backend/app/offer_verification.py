from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Any, Callable, Literal
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx


VerificationStatus = Literal["OPEN", "CLOSED", "INVALID", "UNKNOWN"]
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 262_144
REDIRECT_STATUSES = {301, 302, 303, 307, 308}

CLOSED_CONTENT = re.compile(
    r"\b(?:"
    r"(?:this |the )?(?:job|position|posting|vacancy) (?:is |has been )?"
    r"(?:no longer available|closed|filled|expired)"
    r"|(?:job|position|posting) (?:is )?no longer (?:available|open)"
    r"|(?:job|position|posting|vacancy) has (?:expired|closed)"
    r"|(?:offre|poste) (?:n['’]est plus (?:disponible|ouverte|ouvert)|(?:est |a été )?(?:fermée|fermé|clôturée|clôturé|pourvue|pourvu|expirée|expiré))"
    r"|candidatures (?:sont )?(?:closes|fermées|clôturées|terminées)"
    r"|applications (?:are |have been )?(?:closed|expired)"
    r"|(?:no longer accepting|not accepting) applications"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OfferVerificationResult:
    status: VerificationStatus
    reason: str
    provider: str
    original_url: str
    canonical_url: str
    final_url: str | None = None
    http_status: int | None = None
    job_board: str | None = None
    posting_identifier: str | None = None
    title_match: bool | None = None
    published_at: str | None = None
    deadline: str | None = None


def _offer_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ].+", value.strip()):
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            pass
    return None


def _ambiguous_offer_dates(value: Any) -> tuple[date, ...]:
    if not isinstance(value, str):
        return ()
    match = re.fullmatch(r"(\d{1,2})([./-])(\d{1,2})\2(\d{4})", value.strip())
    if not match:
        return ()
    first, _, second, year = match.groups()
    first, second, year = int(first), int(second), int(year)
    if not (1 <= first <= 12 and 1 <= second <= 12 and first != second):
        return ()
    try:
        return date(year, first, second), date(year, second, first)
    except ValueError:
        return ()


def parse_offer_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    value = " ".join(value.strip().split())
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].+)?", value):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        for separator in ("/", "-", "."):
            if re.fullmatch(rf"\d{{1,2}}\{separator}\d{{1,2}}\{separator}\d{{4}}", value):
                day, month, year = map(int, value.split(separator))
                if day <= 12 and month <= 12 and day != month:
                    return None
                if month > 12 and day <= 12:
                    day, month = month, day
                return date(year, month, day)
        normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
        months = (
            ("january", "jan", "janvier", "janv"), ("february", "feb", "fevrier", "fevr"),
            ("march", "mar", "mars"), ("april", "apr", "avril", "avr"),
            ("may", "mai"), ("june", "jun", "juin"), ("july", "jul", "juillet", "juil"),
            ("august", "aug", "aout"), ("september", "sep", "sept", "septembre"),
            ("october", "oct", "octobre"), ("november", "nov", "novembre"),
            ("december", "dec", "decembre"),
        )
        match = re.fullmatch(r"(\d{1,2}) ([a-z]+)\.? (\d{4})", normalized)
        if match:
            day, month, year = match.groups()
        else:
            match = re.fullmatch(r"([a-z]+)\.? (\d{1,2}),? (\d{4})", normalized)
            if not match:
                return None
            month, day, year = match.groups()
        month_number = next((index for index, names in enumerate(months, 1) if month in names), None)
        return date(int(year), month_number, int(day)) if month_number else None
    except ValueError:
        return None


def deadline_expired(value: Any, *, today: date | None = None) -> bool:
    if timestamp := _offer_datetime(value):
        if timestamp.tzinfo is None:
            timestamp = timestamp.astimezone()
        return timestamp < datetime.now(timezone.utc)
    deadline = parse_offer_date(value)
    dates = (deadline,) if deadline else _ambiguous_offer_dates(value)
    return bool(dates) and max(dates) < (today or date.today())


DATE_TEXT = r"(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?)?|\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{1,2}\s+[a-zéû.]+\s+\d{4}|[a-z]+\s+\d{1,2},?\s+\d{4})"
PUBLICATION_TEXT = re.compile(
    rf"\b(?:date (?:de publication|de parution)|publiée?(?: le)?|published(?: on)?|posted(?: on)?|posting date|publication date)\s*[:\-]?\s*({DATE_TEXT})\b",
    re.IGNORECASE,
)
DEADLINE_TEXT = re.compile(
    rf"\b(?:date limite(?: (?:de candidatures?|de dépôt des candidatures))?|candidater avant le|deadline|application deadline|applications close(?: on)?)\s*[:\-]?\s*({DATE_TEXT})\b",
    re.IGNORECASE,
)


def _job_postings(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _job_postings(item)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if isinstance(types, list) and any(
            isinstance(item, str) and item.rsplit("/", 1)[-1] == "JobPosting" for item in types
        ):
            yield value
        yield from _job_postings(value.get("@graph"))
        yield from _job_postings(value.get("mainEntity"))


class _OfferPageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jobs: list[dict[str, Any]] = []
        self.publications: list[str] = []
        self.deadlines: list[str] = []
        self.parts: list[str] = []
        self._ignored: list[str] = []
        self._script: list[str] | None = None
        self._date_tag: tuple[str, list[str]] | None = None
        self._date_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {"script", "style", "footer", "nav"}:
            self._ignored.append(tag)
            if tag == "script" and (values.get("type") or "").lower() == "application/ld+json":
                self._script = []
        if self._ignored:
            return
        properties = (values.get("itemprop") or "").casefold().split()
        name = (values.get("property") or values.get("name") or "").casefold()
        dates = None
        if "datepublished" in properties or tag == "meta" and name in {"datepublished", "article:published_time"}:
            dates = self.publications
        elif "validthrough" in properties:
            dates = self.deadlines
        if dates is not None:
            value = values.get("content") or values.get("datetime")
            if value:
                dates.append(value)
            elif tag not in {"meta", "input", "img", "br"}:
                self._date_tag = (tag, dates)
                self._date_parts = []

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            try:
                self.jobs.extend(_job_postings(json.loads("".join(self._script))))
            except (ValueError, RecursionError):
                pass
            self._script = None
        if self._ignored:
            if tag == self._ignored[-1]:
                self._ignored.pop()
            return
        if self._date_tag is not None and tag == self._date_tag[0]:
            self._date_tag[1].append(" ".join(self._date_parts))
            self._date_tag = None

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        elif not self._ignored:
            self.parts.append(data)
            if self._date_tag is not None:
                self._date_parts.append(data)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())

    def dates(self) -> dict[str, str | None]:
        if len(self.jobs) > 1:
            return {"published_at": None, "deadline": None}

        def first_date(values, *, keep_time=False):
            for value in values:
                if parsed := parse_offer_date(value):
                    timestamp = _offer_datetime(value) if keep_time else None
                    return timestamp.isoformat() if timestamp else parsed.isoformat()
                if keep_time and _ambiguous_offer_dates(value):
                    return value.strip()
            return None

        posted = [job["datePosted"] for job in self.jobs if "datePosted" in job]
        # An invalid explicit datePosted must not be replaced by newer metadata.
        if posted:
            published = parse_offer_date(posted[0])
            published_at = published.isoformat() if published else None
        else:
            published_at = first_date([
                *PUBLICATION_TEXT.findall(self.text),
                *[job.get("datePublished") for job in self.jobs], *self.publications,
            ])
        return {
            "published_at": published_at,
            "deadline": first_date([
                *[job.get("validThrough") for job in self.jobs],
                *DEADLINE_TEXT.findall(self.text), *self.deadlines,
            ], keep_time=True),
        }

    def is_closed(self) -> bool:
        return bool(CLOSED_CONTENT.search(self.text)) or len(self.jobs) == 1 and any(
            str(job.get(key, "")).strip().casefold() in {"expired", "closed", "filled"}
            for job in self.jobs for key in ("status", "jobStatus", "applicationStatus")
        )


def extract_offer_metadata(html_content: str) -> dict[str, str | None]:
    parser = _OfferPageParser()
    parser.feed(html_content)
    return {**parser.dates(), "availability": "closed" if parser.is_closed() else None}


def extract_offer_dates(html_content: str) -> dict[str, str | None]:
    metadata = extract_offer_metadata(html_content)
    return {key: metadata[key] for key in ("published_at", "deadline")}


class UnsafeDestination(ValueError):
    pass


class DnsResolutionError(OSError):
    pass


class RedirectLimitError(RuntimeError):
    pass


def _blocked_ip_reason(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    for blocked, reason in (
        (address.is_loopback, "IP_LOOPBACK"),
        (address.is_link_local, "IP_LINK_LOCAL"),
        (address.is_multicast, "IP_MULTICAST"),
        (address.is_reserved, "IP_RESERVED"),
        (address.is_unspecified, "IP_UNSPECIFIED"),
        (address.is_private, "IP_PRIVATE"),
    ):
        if blocked:
            return reason
    return None if address.is_global else "IP_NOT_PUBLIC"


def validate_public_http_url(
    url: str,
    resolver: Callable[..., list[tuple[Any, ...]]] | None = None,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    resolver = resolver or socket.getaddrinfo
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except (AttributeError, ValueError) as error:
        raise UnsafeDestination("INVALID_URL") from error
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsafeDestination("INVALID_SCHEME")
    if not parts.hostname:
        raise UnsafeDestination("MISSING_HOST")
    if parts.username is not None or parts.password is not None:
        raise UnsafeDestination("URL_CREDENTIALS")

    hostname = parts.hostname.rstrip(".").lower()
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeDestination("LOCALHOST")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", hostname):
            raise UnsafeDestination("AMBIGUOUS_IP")
        try:
            records = resolver(
                hostname, port or (443 if parts.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = tuple(dict.fromkeys(ipaddress.ip_address(record[4][0]) for record in records))
        except (OSError, ValueError, IndexError) as error:
            raise DnsResolutionError("DNS_ERROR") from error
        if not addresses:
            raise DnsResolutionError("DNS_ERROR")
    else:
        addresses = (literal,)

    for address in addresses:
        if reason := _blocked_ip_reason(address):
            raise UnsafeDestination(reason)
    return addresses


def safe_fetch(
    client: httpx.Client,
    url: str,
    resolver: Callable[..., list[tuple[Any, ...]]] | None = None,
) -> httpx.Response:
    current_url = url
    for redirects in range(MAX_REDIRECTS + 1):
        try:
            validate_public_http_url(current_url, resolver)
        except UnsafeDestination as error:
            if redirects:
                raise UnsafeDestination("UNSAFE_REDIRECT") from error
            raise
        response = client.send(
            client.build_request("GET", current_url), stream=True, follow_redirects=False,
        )
        if response.status_code not in REDIRECT_STATUSES:
            return response
        location = response.headers.get("location")
        response.close()
        if not location:
            return response
        if redirects == MAX_REDIRECTS:
            raise RedirectLimitError("TOO_MANY_REDIRECTS")
        current_url = urljoin(current_url, location)
    raise RedirectLimitError("TOO_MANY_REDIRECTS")


def _read_limited(response: httpx.Response) -> bytes:
    body = b""
    for chunk in response.iter_bytes(chunk_size=65_536):
        body += chunk
        if len(body) >= MAX_RESPONSE_BYTES:
            return body[:MAX_RESPONSE_BYTES]
    return body


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def ashby_reference(url: str) -> tuple[str, str] | None:
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != "jobs.ashbyhq.com":
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2:
        return None
    return segments[0], segments[1]


def similar_title(left: str, right: str) -> bool:
    def normalize(value: str) -> str:
        value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

    return SequenceMatcher(None, normalize(left), normalize(right)).ratio() >= 0.65


class OfferVerifier:
    def __init__(
        self,
        client: httpx.Client,
        resolver: Callable[..., list[tuple[Any, ...]]] | None = None,
    ):
        self.client = client
        self.resolver = resolver or socket.getaddrinfo
        self._ashby_boards: dict[str, tuple[int, dict[str, Any] | None, str]] = {}
        self._ashby_lock = threading.Lock()

    def verify(self, url: str, metadata: dict[str, Any] | None = None) -> OfferVerificationResult:
        try:
            hostname = (urlsplit(url).hostname or "").lower()
        except ValueError:
            hostname = ""
        if hostname == "jobs.ashbyhq.com":
            return self._verify_ashby(url, metadata or {})
        return self._verify_generic(url)

    def _ashby_board(self, board: str) -> tuple[int, dict[str, Any] | None, str]:
        with self._ashby_lock:
            if board in self._ashby_boards:
                return self._ashby_boards[board]
            endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{quote(board, safe='')}"
            try:
                response = safe_fetch(self.client, endpoint, self.resolver)
            except httpx.TimeoutException:
                result = (0, None, "ASHBY_API_TIMEOUT")
            except httpx.RequestError:
                result = (0, None, "ASHBY_API_ERROR")
            except (UnsafeDestination, DnsResolutionError, RedirectLimitError):
                result = (0, None, "ASHBY_API_UNSAFE_DESTINATION")
            else:
                try:
                    if response.status_code == 404:
                        result = (404, None, "ASHBY_BOARD_NOT_FOUND")
                    elif response.status_code == 429:
                        result = (429, None, "ASHBY_API_RATE_LIMIT")
                    elif response.status_code != 200:
                        result = (response.status_code, None, "ASHBY_API_ERROR")
                    else:
                        try:
                            payload = json.loads(_read_limited(response))
                        except (ValueError, UnicodeDecodeError):
                            payload = None
                        jobs = payload.get("jobs") if isinstance(payload, dict) else None
                        valid_jobs = isinstance(jobs, list) and all(
                            isinstance(job, dict) and any(isinstance(job.get(key), str) for key in ("jobUrl", "applyUrl"))
                            for job in jobs
                        )
                        if not valid_jobs:
                            result = (200, None, "ASHBY_API_INVALID_RESPONSE")
                        else:
                            result = (200, payload, "ASHBY_API_OK")
                except httpx.TimeoutException:
                    result = (0, None, "ASHBY_API_TIMEOUT")
                except httpx.RequestError:
                    result = (0, None, "ASHBY_API_ERROR")
                finally:
                    response.close()
            self._ashby_boards[board] = result
            return result

    def _verify_ashby(self, url: str, metadata: dict[str, Any]) -> OfferVerificationResult:
        normalized = canonical_url(url)
        reference = ashby_reference(url)
        if reference is None:
            return OfferVerificationResult("INVALID", "ASHBY_URL_INVALID", "ashby", url, normalized)
        try:
            validate_public_http_url(url, self.resolver)
        except UnsafeDestination as error:
            return OfferVerificationResult("INVALID", str(error), "ashby", url, normalized)
        except DnsResolutionError as error:
            return OfferVerificationResult("UNKNOWN", str(error), "ashby", url, normalized)
        board, posting = reference
        status, payload, reason = self._ashby_board(board)
        common = {
            "provider": "ashby", "original_url": url, "canonical_url": normalized,
            "http_status": status or None, "job_board": board, "posting_identifier": posting,
        }
        if reason == "ASHBY_BOARD_NOT_FOUND":
            return OfferVerificationResult("INVALID", reason, **common)
        if payload is None:
            return OfferVerificationResult("UNKNOWN", reason, **common)

        for job in payload["jobs"]:
            if not isinstance(job, dict):
                continue
            urls = [value for key in ("jobUrl", "applyUrl") if isinstance((value := job.get(key)), str)]
            matched = next((value for value in urls if canonical_url(value) == normalized), None)
            if matched is None:
                matched = next((value for value in urls if ashby_reference(value) == reference), None)
            if matched is not None:
                title = metadata.get("title") or metadata.get("position")
                api_title = job.get("title")
                published = parse_offer_date(job.get("publishedAt"))
                raw_deadline = job.get("validThrough") or job.get("deadline")
                deadline = parse_offer_date(raw_deadline)
                timestamp = _offer_datetime(raw_deadline)
                normalized_deadline = timestamp.isoformat() if timestamp else deadline.isoformat() if deadline else None
                if normalized_deadline is None and _ambiguous_offer_dates(raw_deadline):
                    normalized_deadline = raw_deadline.strip()
                dates = {
                    "published_at": published.isoformat() if published else None,
                    "deadline": normalized_deadline,
                }
                if deadline_expired(raw_deadline):
                    return OfferVerificationResult("CLOSED", "DEADLINE_EXPIRED", final_url=matched, **dates, **common)
                if str(job.get("status", "")).strip().casefold() in {"closed", "expired", "filled"}:
                    return OfferVerificationResult("CLOSED", "CONTENT_CLOSED", final_url=matched, **dates, **common)
                return OfferVerificationResult(
                    "OPEN", "ASHBY_POSTING_PUBLISHED", final_url=matched,
                    title_match=similar_title(title, api_title) if isinstance(title, str) and isinstance(api_title, str) else None,
                    **dates, **common,
                )
        return OfferVerificationResult("CLOSED", "ASHBY_POSTING_NOT_PUBLISHED", **common)

    def _verify_generic(self, url: str) -> OfferVerificationResult:
        try:
            normalized = canonical_url(url)
        except (AttributeError, ValueError):
            normalized = url
        common = {"provider": "generic", "original_url": url, "canonical_url": normalized}
        response = None
        try:
            response = safe_fetch(self.client, url, self.resolver)
            final_url = str(response.url)
            status = response.status_code
            body = b""
            if status == 200:
                content_type = response.headers.get("content-type", "").lower()
                if not content_type or content_type.startswith("text/") or "html" in content_type:
                    body = _read_limited(response)
        except UnsafeDestination as error:
            return OfferVerificationResult("INVALID", str(error), **common)
        except DnsResolutionError as error:
            return OfferVerificationResult("UNKNOWN", str(error), **common)
        except RedirectLimitError as error:
            return OfferVerificationResult("UNKNOWN", str(error), **common)
        except httpx.TimeoutException:
            return OfferVerificationResult("UNKNOWN", "HTTP_TIMEOUT", **common)
        except httpx.RequestError:
            return OfferVerificationResult("UNKNOWN", "HTTP_ERROR", **common)
        finally:
            if response is not None:
                response.close()
        if 200 <= status < 400:
            parser = _OfferPageParser()
            parser.feed(body.decode("utf-8", errors="ignore"))
            # Later JobPosting data may contradict metadata in a truncated page.
            dates = parser.dates() if len(body) < MAX_RESPONSE_BYTES else {"published_at": None, "deadline": None}
            if deadline_expired(dates["deadline"]):
                return OfferVerificationResult(
                    "CLOSED", "DEADLINE_EXPIRED", final_url=final_url, http_status=status, **dates, **common,
                )
            if status == 200 and parser.is_closed():
                return OfferVerificationResult(
                    "CLOSED", "CONTENT_CLOSED", final_url=final_url, http_status=status, **dates, **common,
                )
            return OfferVerificationResult("OPEN", "HTTP_AVAILABLE", final_url=final_url, http_status=status, **dates, **common)
        if status == 404:
            return OfferVerificationResult("INVALID", "HTTP_NOT_FOUND", final_url=final_url, http_status=status, **common)
        if status == 410:
            return OfferVerificationResult("CLOSED", "HTTP_GONE", final_url=final_url, http_status=status, **common)
        if status in {403, 405, 429} or status >= 500:
            return OfferVerificationResult("UNKNOWN", f"HTTP_{status}", final_url=final_url, http_status=status, **common)
        return OfferVerificationResult("INVALID", f"HTTP_{status}", final_url=final_url, http_status=status, **common)
