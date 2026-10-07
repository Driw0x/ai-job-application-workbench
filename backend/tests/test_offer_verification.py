from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json

import httpx
import pytest

from app import offer_verification
from app.offer_verification import (
    MAX_REDIRECTS, MAX_RESPONSE_BYTES, DnsResolutionError, OfferVerifier, UnsafeDestination,
    ashby_reference, deadline_expired, extract_offer_dates, extract_offer_metadata,
    parse_offer_date, validate_public_http_url,
)


BOARD = "example-company"
POSTING = "11111111-1111-4111-8111-111111111111"
URL = f"https://jobs.ashbyhq.com/{BOARD}/{POSTING}"
API_URL = f"https://api.ashbyhq.com/posting-api/job-board/{BOARD}"


def dns(*addresses):
    def resolve(_host, port, **_kwargs):
        return [
            (10 if ":" in address else 2, 1, 6, "", (address, port, 0, 0) if ":" in address else (address, port))
            for address in addresses
        ]
    return resolve


PUBLIC_DNS = dns("93.184.216.34")


@pytest.mark.parametrize("url,reason", [("https://jobs.example.test/offer", "HTTP_TIMEOUT"), (URL, "ASHBY_API_TIMEOUT")])
def test_timeout_after_response_starts_returns_unknown_and_closes(url, reason):
    class InterruptedStream(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            yield b" " * 65_536
            raise httpx.ReadTimeout("Flux interrompu")
        def close(self):
            self.closed = True
    stream = InterruptedStream()
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream, request=request))) as client:
        result = OfferVerifier(client, PUBLIC_DNS).verify(url)
    assert result.status == "UNKNOWN" and result.reason == reason
    assert stream.closed


def verifier(handler, resolver=PUBLIC_DNS):
    return OfferVerifier(
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True), resolver,
    )


def response(request, status=200, **kwargs):
    return httpx.Response(status, request=request, **kwargs)


def ashby_payload(url=URL, listed=True, key="jobUrl"):
    return {"apiVersion": "1", "jobs": [{
        "title": "AI Engineer Intern", key: url, "isListed": listed,
    }]}


def test_extracts_ashby_board_and_posting():
    assert ashby_reference(URL) == (BOARD, POSTING)


@pytest.mark.parametrize("url", ["http://93.184.216.34/", "https://[2606:4700:4700::1111]/"])
def test_public_ip_literals_are_allowed(url):
    validate_public_http_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/", "http://localhost./", "http://127.0.0.1/", "http://127.1/",
        "http://10.0.0.1/", "http://172.16.0.1/", "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/", "http://224.0.0.1/", "http://240.0.0.1/",
        "file:///etc/passwd", "ftp://example.com/file", "https:///missing-host",
        "https://user:password@example.com/",
    ],
)
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(UnsafeDestination):
        validate_public_http_url(url, PUBLIC_DNS)


def test_dns_all_public_addresses_are_allowed():
    addresses = validate_public_http_url(
        "https://jobs.example.test/offer", dns("93.184.216.34", "2606:4700:4700::1111"),
    )
    assert len(addresses) == 2


def test_dns_single_public_address_is_allowed():
    assert validate_public_http_url("https://jobs.example.test/offer", PUBLIC_DNS)


@pytest.mark.parametrize(
    "addresses", [("93.184.216.34", "10.0.0.1"), ("127.0.0.1",), ("::1",)],
)
def test_dns_rejects_destination_when_any_address_is_unsafe(addresses):
    with pytest.raises(UnsafeDestination):
        validate_public_http_url("https://jobs.example.test/offer", dns(*addresses))


def test_dns_failure_is_safe():
    def failure(*_args, **_kwargs):
        raise OSError("DNS unavailable")

    with pytest.raises(DnsResolutionError):
        validate_public_http_url("https://jobs.example.test/offer", failure)
    assert verifier(lambda request: response(request), failure).verify(
        "https://jobs.example.test/offer"
    ).status == "UNKNOWN"


@pytest.mark.parametrize("listed", [True, False])
def test_ashby_posting_present_is_open_regardless_of_is_listed(listed):
    check = verifier(lambda request: response(request, json=ashby_payload(listed=listed)))
    result = check.verify(URL, {"title": "AI engineer - intern"})
    assert result.status == "OPEN"
    assert result.reason == "ASHBY_POSTING_PUBLISHED"
    assert result.title_match is True


def test_ashby_apply_url_matches_posting_identifier():
    payload = ashby_payload(f"{URL}/application", key="applyUrl")
    result = verifier(lambda request: response(request, json=payload)).verify(URL)
    assert result.status == "OPEN"


def test_ashby_tracking_parameters_do_not_prevent_match():
    result = verifier(lambda request: response(request, json=ashby_payload())).verify(f"{URL}/?utm_source=test#offer")
    assert result.status == "OPEN"


@pytest.mark.parametrize("jobs", [[], [{"title": "Other", "jobUrl": f"https://jobs.ashbyhq.com/{BOARD}/different"}]])
def test_ashby_missing_or_different_posting_is_closed(jobs):
    check = verifier(lambda request: response(request, json={"apiVersion": "1", "jobs": jobs}))
    result = check.verify(URL)
    assert result.status == "CLOSED"
    assert result.reason == "ASHBY_POSTING_NOT_PUBLISHED"


def test_ashby_javascript_shell_is_never_used_as_open_proof():
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if str(request.url) == API_URL:
            return response(request, json={"apiVersion": "1", "jobs": []})
        return response(request, text="You need to enable JavaScript to run this app.")

    result = verifier(handler).verify(URL)
    assert result.status == "CLOSED"
    assert requested == [API_URL]


def test_ashby_timeout_is_unknown():
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)

    result = verifier(handler).verify(URL)
    assert (result.status, result.reason) == ("UNKNOWN", "ASHBY_API_TIMEOUT")


@pytest.mark.parametrize("status, reason", [(429, "ASHBY_API_RATE_LIMIT"), (500, "ASHBY_API_ERROR")])
def test_ashby_temporary_api_errors_are_unknown(status, reason):
    result = verifier(lambda request: response(request, status)).verify(URL)
    assert (result.status, result.reason) == ("UNKNOWN", reason)


def test_ashby_invalid_json_is_unknown():
    result = verifier(lambda request: response(request, text="not-json")).verify(URL)
    assert (result.status, result.reason) == ("UNKNOWN", "ASHBY_API_INVALID_RESPONSE")


def test_ashby_missing_board_is_invalid():
    result = verifier(lambda request: response(request, 404)).verify(URL)
    assert (result.status, result.reason) == ("INVALID", "ASHBY_BOARD_NOT_FOUND")


def test_ashby_invalid_url_is_invalid_without_api_call():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return response(request)

    result = verifier(handler).verify("https://jobs.ashbyhq.com/example-company")
    assert (result.status, result.reason, calls) == ("INVALID", "ASHBY_URL_INVALID", 0)


def test_ashby_board_cache_is_shared_by_concurrent_checks():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return response(request, json=ashby_payload())

    check = verifier(handler)
    urls = [URL, *[f"https://jobs.ashbyhq.com/{BOARD}/other-{index}" for index in range(4)]]
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(check.verify, urls))
    assert calls == 1
    assert [result.status for result in results].count("OPEN") == 1


@pytest.mark.parametrize(
    "status, expected",
    [(200, "OPEN"), (404, "INVALID"), (410, "CLOSED"), (403, "UNKNOWN"), (405, "UNKNOWN"),
     (429, "UNKNOWN"), (500, "UNKNOWN")],
)
def test_generic_http_status_mapping(status, expected):
    result = verifier(lambda request: response(request, status)).verify("https://jobs.example.test/offer")
    assert result.status == expected


def test_generic_timeout_is_unknown():
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)

    assert verifier(handler).verify("https://jobs.example.test/offer").status == "UNKNOWN"


def test_public_redirect_and_relative_location_are_followed_manually():
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.path == "/start":
            return response(request, 302, headers={"location": "/final"})
        return response(request, text="Open position")

    result = verifier(handler).verify("https://jobs.example.test/start")
    assert result.status == "OPEN"
    assert result.final_url == "https://jobs.example.test/final"
    assert requested == ["https://jobs.example.test/start", "https://jobs.example.test/final"]


@pytest.mark.parametrize(
    "target",
    [
        "http://localhost/admin", "http://127.0.0.1/admin", "http://10.0.0.1/admin",
        "http://192.168.1.1/admin", "http://172.16.0.1/admin",
        "http://169.254.169.254/latest/meta-data/", "http://[::1]/admin", "http://[fe80::1]/admin",
    ],
)
def test_redirect_to_unsafe_destination_is_blocked_before_second_request(target):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return response(request, 302, headers={"location": target})

    result = verifier(handler).verify("https://jobs.example.test/start")
    assert (result.status, result.reason) == ("INVALID", "UNSAFE_REDIRECT")
    assert requested == ["https://jobs.example.test/start"]


def test_redirect_to_mixed_public_private_dns_is_blocked_before_second_request():
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return response(request, 302, headers={"location": "https://mixed.example.test/admin"})

    def resolver(host, port, **kwargs):
        return dns("93.184.216.34", "10.0.0.1")(host, port, **kwargs) if host.startswith("mixed.") else PUBLIC_DNS(host, port, **kwargs)

    result = verifier(handler, resolver).verify("https://jobs.example.test/start")
    assert (result.status, result.reason) == ("INVALID", "UNSAFE_REDIRECT")
    assert requested == ["https://jobs.example.test/start"]


def test_redirect_chain_is_limited():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return response(request, 302, headers={"location": f"/redirect-{calls}"})

    result = verifier(handler).verify("https://jobs.example.test/start")
    assert (result.status, result.reason) == ("UNKNOWN", "TOO_MANY_REDIRECTS")
    assert calls == MAX_REDIRECTS + 1


@pytest.mark.parametrize(
    "content",
    [
        "This position is no longer available.",
        "<p>Cette offre n’est <strong>plus disponible</strong>.</p>",
    ],
)
def test_generic_http_200_with_explicit_closed_content_is_closed(content):
    result = verifier(lambda request: response(request, text=content)).verify(
        "https://jobs.example.test/offer"
    )
    assert (result.status, result.reason) == ("CLOSED", "CONTENT_CLOSED")


def job_page(**properties):
    return f'<script type="application/ld+json">{json.dumps({"@type": "JobPosting", **properties})}</script>'


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-10-06", date(2026, 10, 6)),
        ("2026-10-06T09:15:00Z", date(2026, 10, 6)),
        ("2026-10-06T09:15:00+02:00", date(2026, 10, 6)),
        ("06/10/2026", None), ("01/08/2026", None),
        ("29/03/2026", date(2026, 3, 29)), ("03/29/2026", date(2026, 3, 29)),
        ("08/08/2026", date(2026, 8, 8)),
        ("6 octobre 2026", date(2026, 10, 6)),
        ("6 février 2026", date(2026, 2, 6)),
        ("October 6, 2026", date(2026, 10, 6)),
        (None, None), ("invalid", None), ("2026-02-30", None),
        ("2026-10-06 nonsense", None), ("2026-10", None), (1728172800, None),
    ],
)
def test_parse_offer_date_requires_complete_valid_date(value, expected):
    assert parse_offer_date(value) == expected


def test_date_posted_precedes_recent_modification_and_publication_metadata():
    content = job_page(datePosted="2025-01-01", dateModified="2026-10-06")
    content += '<p>Publié le 6 octobre 2026</p><meta property="article:published_time" content="2026-10-06">'
    assert extract_offer_dates(content) == {"published_at": "2025-01-01", "deadline": None}
    result = verifier(lambda request: response(request, text=content)).verify("https://jobs.example.test/offer")
    assert result.published_at == "2025-01-01"


def test_truncated_page_metadata_cannot_replace_unread_job_posting_dates():
    content = '<meta name="datePublished" content="2026-10-06">' + " " * MAX_RESPONSE_BYTES
    content += job_page(datePosted="2025-01-01")
    result = verifier(lambda request: response(request, text=content)).verify("https://jobs.example.test/offer")
    assert result.published_at is None
    assert result.deadline is None


@pytest.mark.parametrize(
    "content",
    [
        "<p>Internship starts 2026-10-06</p><footer>Publié le 2026-10-06</footer>",
        job_page(dateModified="2026-10-06"),
        '<meta name="dateModified" content="2026-10-06"><meta name="crawl_date" content="2026-10-06">',
        job_page(datePosted="invalid") + '<meta name="datePublished" content="2026-10-06">',
        job_page(datePosted="2026-02-30") + "<p>Publié le 2026-10-06</p>",
        job_page(datePosted="01/08/2026") + '<meta name="datePublished" content="2026-10-06">',
        '<p>Publié le 01/08/2026</p>',
        '<script type="application/ld+json">{not json}</script>',
    ],
)
def test_missing_or_invalid_publication_never_becomes_recent(content):
    assert extract_offer_dates(content)["published_at"] is None


@pytest.mark.parametrize(
    "content",
    [
        '<meta name="datePublished" content="2026-10-06">',
        '<meta property="article:published_time" content="2026-10-06T09:00:00Z">',
        '<meta itemprop="datePublished" content="2026-10-06">',
        '<time itemprop="datePublished" datetime="2026-10-06">6 octobre 2026</time>',
        '<span itemprop="datePublished">6 octobre 2026</span>',
        "<p>Date de publication : <strong>6 octobre 2026</strong></p>",
        "<p>Posted on October 6, 2026</p>",
    ],
)
def test_explicit_publication_and_reliable_metadata_are_supported(content):
    assert extract_offer_dates(content)["published_at"] == "2026-10-06"


def test_explicit_publication_precedes_metadata():
    content = '<p>Publié le 01/01/2025</p><meta name="datePublished" content="2026-10-06">'
    assert extract_offer_dates(content)["published_at"] == "2025-01-01"


@pytest.mark.parametrize(
    "payload",
    [
        [{"@type": "Organization", "datePublished": "2026-10-06"}, {"@type": "JobPosting", "datePosted": "2025-01-01"}],
        {"@graph": [{"@type": ["Thing", "JobPosting"], "datePosted": "2025-01-01"}]},
        {"mainEntity": {"@type": "https://schema.org/JobPosting", "datePosted": "2025-01-01"}},
    ],
)
def test_json_ld_lists_and_graph_select_job_posting_dates(payload):
    content = f'<script type="application/ld+json">{json.dumps(payload)}</script>'
    assert extract_offer_dates(content)["published_at"] == "2025-01-01"


def test_multiple_job_postings_cannot_provide_unambiguous_offer_dates():
    content = job_page(datePosted="2026-10-06", validThrough="2026-12-31")
    content += job_page(datePosted="2025-01-01", validThrough="2025-12-31")
    content += '<meta name="datePublished" content="2026-10-06">'
    assert extract_offer_dates(content) == {"published_at": None, "deadline": None}


def test_script_without_type_attribute_does_not_crash_date_extraction():
    assert extract_offer_dates('<script type>console.log("hello")</script>') == {
        "published_at": None, "deadline": None,
    }


@pytest.mark.parametrize("content", ["Offre expirée", job_page(status="closed")])
def test_source_metadata_preserves_explicit_closure_without_a_second_fetch(content):
    assert extract_offer_metadata(content) == {
        "published_at": None, "deadline": None, "availability": "closed",
    }


@pytest.mark.parametrize("relative_days, status", [(-1, "CLOSED"), (0, "OPEN"), (1, "OPEN")])
def test_deadline_is_distinct_from_recent_publication_and_includes_current_day(relative_days, status):
    today = date.today()
    deadline = (today + timedelta(days=relative_days)).isoformat()
    content = job_page(datePosted=today.isoformat(), validThrough=deadline)
    result = verifier(lambda request: response(request, text=content)).verify("https://jobs.example.test/offer")
    assert result.status == status
    assert result.published_at == today.isoformat()
    assert result.deadline == deadline
    if status == "CLOSED":
        assert result.reason == "DEADLINE_EXPIRED"


def test_explicit_deadline_is_extracted_without_publication():
    assert extract_offer_dates("<p>Date limite de candidature : 31/12/2025</p>") == {
        "published_at": None, "deadline": "2025-12-31",
    }


def test_explicit_deadline_preserves_timestamp():
    assert extract_offer_dates("<p>Deadline: 2026-10-06T11:00:00Z</p>")["deadline"] == "2026-10-06T11:00:00+00:00"


@pytest.mark.parametrize("content", [job_page(validThrough="05/12/2025"), "<p>Deadline: 05/12/2025</p>"])
def test_ambiguous_deadline_is_preserved_for_conservative_expiration_check(content):
    assert extract_offer_dates(content)["deadline"] == "05/12/2025"


@pytest.mark.parametrize("status", ["closed", "expired", "filled"])
def test_recent_job_posting_with_closed_json_ld_status_is_closed(status):
    content = job_page(datePosted=datetime.now(timezone.utc).date().isoformat(), status=status)
    result = verifier(lambda request: response(request, text=content)).verify("https://jobs.example.test/offer")
    assert (result.status, result.reason) == ("CLOSED", "CONTENT_CLOSED")


@pytest.mark.parametrize(
    "content",
    [
        "Offre expirée", "Cette offre est pourvue", "Poste pourvu", "Candidatures closes",
        "Les candidatures sont fermées", "Applications are closed", "No longer accepting applications",
        "This job has expired", "Cette offre est clôturée", "Candidatures clôturées",
    ],
)
def test_closed_phrases_reject_recent_offers(content):
    content += job_page(datePosted=datetime.now(timezone.utc).date().isoformat())
    result = verifier(lambda request: response(request, text=content)).verify("https://jobs.example.test/offer")
    assert (result.status, result.reason) == ("CLOSED", "CONTENT_CLOSED")


def test_javascript_closed_messages_are_not_offer_status():
    content = '<script>const messages = ["Job is closed"];</script><p>Open position</p>'
    assert verifier(lambda request: response(request, text=content)).verify("https://jobs.example.test/offer").status == "OPEN"


def test_ashby_uses_official_published_at():
    payload = ashby_payload()
    payload["jobs"][0].update(publishedAt="2025-01-01T09:00:00Z", dateModified="2026-10-06")
    result = verifier(lambda request: response(request, json=payload)).verify(URL)
    assert result.published_at == "2025-01-01"


def test_recent_ashby_job_with_expired_deadline_is_closed():
    today = datetime.now(timezone.utc).date()
    payload = ashby_payload()
    payload["jobs"][0].update(publishedAt=today.isoformat(), validThrough=(today - timedelta(days=1)).isoformat())
    result = verifier(lambda request: response(request, json=payload)).verify(URL)
    assert (result.status, result.reason) == ("CLOSED", "DEADLINE_EXPIRED")
    assert result.published_at == today.isoformat()


@pytest.mark.parametrize(
    "deadline, expected",
    [
        ("2026-10-06T11:00:00Z", True), ("2026-10-06T13:00:00Z", False),
        ("2026-10-06T14:00:00+03:00", True), ("2026-10-06T10:00:00-03:00", False),
        ("2026-10-05", True), ("2026-10-06", False),
        (None, False), ("invalid", False), ("2026-10-06T99:00:00Z", False),
    ],
)
def test_deadline_expired_preserves_time_precision_and_date_only_boundary(monkeypatch, deadline, expected):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 6, 12, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(offer_verification, "datetime", FixedDatetime)
    assert deadline_expired(deadline, today=date(2026, 10, 6)) is expected


def test_date_only_deadline_defaults_to_local_calendar_day():
    assert deadline_expired(date.today().isoformat()) is False


@pytest.mark.parametrize(
    "deadline, expected",
    [("05/12/2025", True), ("05/12/2027", False), ("05/12/2026", False), ("12/05/2026", False)],
)
def test_ambiguous_deadline_expires_only_when_every_interpretation_has_passed(deadline, expected):
    assert deadline_expired(deadline, today=date(2026, 10, 6)) is expected


@pytest.mark.parametrize("provider", ["generic", "ashby"])
def test_recent_offer_with_ambiguous_deadline_in_previous_year_is_closed(provider):
    today = date.today()
    deadline = f"05/12/{today.year - 1}"
    if provider == "ashby":
        payload = ashby_payload()
        payload["jobs"][0].update(publishedAt=today.isoformat(), validThrough=deadline)
        result = verifier(lambda request: response(request, json=payload)).verify(URL)
    else:
        content = job_page(datePosted=today.isoformat(), validThrough=deadline)
        result = verifier(lambda request: response(request, text=content)).verify("https://jobs.example.test/offer")
    assert (result.status, result.reason) == ("CLOSED", "DEADLINE_EXPIRED")
    assert result.deadline == deadline


@pytest.mark.parametrize("hours, expected", [(-1, True), (1, False)])
def test_naive_deadline_uses_local_time(hours, expected):
    deadline = (datetime.now() + timedelta(hours=hours)).isoformat()
    assert deadline_expired(deadline) is expected


@pytest.mark.parametrize("provider", ["generic", "ashby"])
def test_recent_offer_with_deadline_elapsed_today_is_closed(monkeypatch, provider):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 6, 12, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(offer_verification, "datetime", FixedDatetime)
    deadline = "2026-10-06T11:00:00Z"
    if provider == "ashby":
        payload = ashby_payload()
        payload["jobs"][0].update(publishedAt="2026-10-06", validThrough=deadline)
        result = verifier(lambda request: response(request, json=payload)).verify(URL)
    else:
        content = job_page(datePosted="2026-10-06", validThrough=deadline)
        result = verifier(lambda request: response(request, text=content)).verify("https://jobs.example.test/offer")
    assert (result.status, result.reason) == ("CLOSED", "DEADLINE_EXPIRED")
    assert result.deadline == "2026-10-06T11:00:00+00:00"
