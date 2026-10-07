from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx


class SearchProviderError(RuntimeError):
    pass


class SearchProviderUnavailableError(SearchProviderError):
    pass


TRACKING_QUERY_PARAMETERS = {
    "boardid", "dclid", "fbclid", "gclid", "mc_cid", "mc_eid", "msclkid",
    "originid", "ref", "referrer", "tracking", "trk",
}


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str
    query: str
    company: str = ""
    retrieval_mode: str = "SEARXNG"
    search_provider: str = "searxng"

    def model_dump(self) -> dict[str, str]:
        return asdict(self)


def canonical_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("Invalid result URL")
    query = urlencode(sorted(
        (key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in TRACKING_QUERY_PARAMETERS
    ))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


class SearXNGProvider:
    def __init__(self, base_url: str, http: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        if urlsplit(self.base_url).scheme not in {"http", "https"}:
            raise ValueError("Invalid SearXNG URL")
        self._owns_http = http is None
        self.http = http if http is not None else httpx.Client(timeout=20, follow_redirects=True)
        self.last_stats: dict[str, Any] = {}

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def test(self) -> None:
        self._request("test", 1)

    def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        return self._request(query, limit)

    def _request(self, query: str, limit: int) -> list[SearchResult]:
        self.last_stats = {
            "raw_result_count": None, "extracted_url_count": None,
            "admitted_result_count": None, "intra_query_duplicate_count": None,
            "invalid_url_count": None, "search_call_count": 1,
        }
        try:
            response = self.http.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json", "categories": "general"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise SearchProviderUnavailableError(f"SearXNG unreachable : {error}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise SearchProviderError("Invalid SearXNG JSON response")
        unavailable_engines = [
            {"engine": str(item[0]), "reason": str(item[1])}
            for item in payload.get("unresponsive_engines", [])
            if isinstance(item, list) and len(item) >= 2
        ]
        results = []
        seen = set()
        invalid_count = 0
        duplicate_count = 0
        for item in payload["results"]:
            if not isinstance(item, dict):
                invalid_count += 1
                continue
            try:
                url = canonical_url(str(item.get("url", "")))
            except ValueError:
                invalid_count += 1
                continue
            if url in seen:
                duplicate_count += 1
                continue
            seen.add(url)
            if len(results) < limit:
                results.append(SearchResult(
                    title=str(item.get("title") or url),
                    url=url,
                    snippet=str(item.get("content") or ""),
                    source=str(item.get("engine") or item.get("category") or "SearXNG"),
                    query=query,
                ))
        self.last_stats = {
            "raw_result_count": len(payload["results"]),
            "extracted_url_count": len(payload["results"]) - invalid_count,
            "admitted_result_count": len(results),
            "intra_query_duplicate_count": duplicate_count,
            "invalid_url_count": invalid_count,
            "partial": bool(unavailable_engines),
            "provider_errors": unavailable_engines,
            "search_call_count": 1,
        }
        if not results and unavailable_engines:
            details = ", ".join(f"{item['engine']}: {item['reason']}" for item in unavailable_engines)
            raise SearchProviderUnavailableError(f"SearXNG has no usable engine ({details})")
        return results


class AIWebSearchProvider:
    def __init__(self, ai_provider: Any, provider_name: str, model: str, effort: str = "medium",
                 usage_recorder: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.ai_provider = ai_provider
        self.provider_name = provider_name
        self.model = model
        self.effort = effort
        self.usage_recorder = usage_recorder
        self.last_stats: dict[str, Any] = {}

    def close(self) -> None:
        close = getattr(self.ai_provider, "close", None)
        if close is not None:
            close()

    def test(self) -> None:
        self.search("test", 1)

    def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        self.last_stats = {
            "raw_result_count": None, "extracted_url_count": None,
            "admitted_result_count": None, "intra_query_duplicate_count": None,
            "invalid_url_count": None, "search_call_count": 1,
            "partial": False, "provider_errors": [],
        }
        schema = {
            "type": "object",
            "properties": {"results": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"}, "url": {"type": "string"},
                    "company": {"type": "string"}, "snippet": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["title", "url", "company", "snippet", "source"],
                "additionalProperties": False,
            }}},
            "required": ["results"], "additionalProperties": False,
        }
        prompt = (
            f"Effectue une recherche Web actuelle pour cette requête : {query!r}. "
            f"Retourne au maximum {limit} offres réellement trouvées. Chaque résultat doit contenir "
            "un titre, une entreprise si disponible, une URL source vérifiable, un résumé factuel et la source. "
            "N'invente aucune offre ni URL. Omet tout résultat sans URL Web exploitable. Ne décide pas de son éligibilité."
        )
        metadata = {}
        try:
            raw = self.ai_provider.run(
                prompt, self.model, schema, effort=self.effort, web_search=True,
            )
            metadata = getattr(raw, "run_metadata", {})
            data = raw if isinstance(raw, dict) else json.loads(str(raw))
            results = []
            seen = set()
            invalid_count = 0
            duplicate_count = 0
            for item in data.get("results", []):
                try:
                    url = canonical_url(item["url"])
                except (AttributeError, KeyError, TypeError, ValueError):
                    invalid_count += 1
                    continue
                if url in seen:
                    duplicate_count += 1
                    continue
                seen.add(url)
                results.append(SearchResult(
                    title=str(item.get("title") or url), url=url,
                    snippet=str(item.get("snippet") or ""),
                    source=str(item.get("source") or self.provider_name), query=query,
                    company=str(item.get("company") or ""), retrieval_mode="AI_DIRECT",
                    search_provider=self.provider_name,
                ))
            admitted = results[:limit]
            self.last_stats.update({
                "raw_result_count": len(data.get("results", [])),
                "extracted_url_count": len(data.get("results", [])) - invalid_count,
                "admitted_result_count": len(admitted),
                "intra_query_duplicate_count": duplicate_count,
                "invalid_url_count": invalid_count,
            })
            return admitted
        except Exception as error:
            metadata = getattr(error, "metadata", metadata)
            self.last_stats["partial"] = True
            raise SearchProviderUnavailableError(
                f"Direct Web search {self.provider_name} unavailable : {error}"
            ) from error
        finally:
            self.last_stats.update({
                "web_search_calls": metadata.get("web_search_calls"),
                "search_input_tokens": metadata.get("input_tokens"),
                "search_output_tokens": metadata.get("output_tokens"),
                "search_total_tokens": metadata.get("total_tokens"),
                "web_domains": metadata.get("web_domains", []),
                "web_search_actions": metadata.get("web_search_actions", []),
                "web_search_queries": metadata.get("web_search_queries", []),
                "web_sources": metadata.get("web_sources"),
            })
            if self.usage_recorder is not None:
                self.usage_recorder(metadata)
