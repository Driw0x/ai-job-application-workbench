import httpx
import pytest

from app.ai_providers import ChatGPTOAuthResponsesProvider
from app.codex_client import CodexOutput, CodexStructuredOutputError
from app.search_providers import AIWebSearchProvider, SearchProviderUnavailableError, SearXNGProvider, canonical_url


@pytest.mark.parametrize("injected", [False, True])
def test_searxng_closure_respects_http_client_ownership(monkeypatch, injected):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as http:
        monkeypatch.setattr(httpx, "Client", lambda **_kwargs: http)
        provider = SearXNGProvider("http://localhost:8080", http=http if injected else None)
        provider.close()
        assert http.is_closed is not injected


@pytest.mark.parametrize("injected", [False, True])
def test_ai_search_closure_respects_wrapped_http_client_ownership(monkeypatch, injected):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as http:
        monkeypatch.setattr(httpx, "Client", lambda **_kwargs: http)
        provider = ChatGPTOAuthResponsesProvider("fake-token", http=http if injected else None)
        AIWebSearchProvider(provider, "chatgpt_oauth", "test-model").close()
        assert http.is_closed is not injected


def test_canonical_url_removes_tracking_and_sorts_identity_parameters():
    assert canonical_url(
        "https://jobs.example.test/view?utm_source=test&b=2&gclid=x&a=1#apply"
    ) == "https://jobs.example.test/view?a=1&b=2"


def test_canonical_url_preserves_distinct_indeed_job_ids():
    first = canonical_url("https://fr.indeed.com/viewjob?jk=AAA&utm_medium=email")
    second = canonical_url("https://fr.indeed.com/viewjob?utm_campaign=test&jk=BBB")
    assert first == "https://fr.indeed.com/viewjob?jk=AAA"
    assert second == "https://fr.indeed.com/viewjob?jk=BBB"
    assert first != second


def test_canonical_url_deduplicates_urls_that_only_differ_by_tracking():
    assert canonical_url("https://jobs.example.test/offer?utm_content=a") == canonical_url(
        "https://jobs.example.test/offer?fbclid=b"
    )


def test_searxng_normalizes_and_deduplicates_results():
    def handler(request):
        assert request.url.path == "/search"
        assert request.url.params["q"] == "stage IA Paris"
        assert request.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [
            {"title": "Stage IA", "url": "https://jobs.example.test/offer?tracking=1", "content": "Mission ML", "engine": "google"},
            {"title": "Doublon", "url": "https://jobs.example.test/offer", "content": "", "engine": "bing"},
            {"title": "Invalide", "url": "javascript:alert(1)", "content": ""},
        ]}, request=request)

    provider = SearXNGProvider("http://localhost:8080/", httpx.Client(transport=httpx.MockTransport(handler)))
    results = provider.search("stage IA Paris")
    assert [item.url for item in results] == ["https://jobs.example.test/offer"]
    assert results[0].source == "google" and results[0].query == "stage IA Paris"
    assert provider.last_stats["raw_result_count"] == 3
    assert provider.last_stats["extracted_url_count"] == 2
    assert provider.last_stats["admitted_result_count"] == 1
    assert provider.last_stats["intra_query_duplicate_count"] == 1
    assert provider.last_stats["invalid_url_count"] == 1
    assert provider.last_stats["search_call_count"] == 1


def test_searxng_empty_response_is_valid():
    provider = SearXNGProvider("http://localhost:8080", httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"results": []}, request=request),
    )))
    assert provider.search("aucun résultat") == []
    assert provider.last_stats["raw_result_count"] == provider.last_stats["extracted_url_count"] == 0


def test_searxng_reports_all_unavailable_engines_instead_of_successful_zero():
    provider = SearXNGProvider("http://localhost:8080", httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            "results": [], "unresponsive_engines": [
                ["brave", "too many requests"], ["duckduckgo", "CAPTCHA"],
            ],
        }, request=request),
    )))
    with pytest.raises(SearchProviderUnavailableError, match="brave: too many requests"):
        provider.search("stage IA")


def test_ai_direct_results_use_common_canonicalization_and_expose_usage():
    class Provider:
        def run(self, *_args, **kwargs):
            assert kwargs["web_search"] is True
            return CodexOutput({"results": [
                {"title": "Stage IA", "company": "Example", "url": "https://jobs.example.test/offer?utm_source=ai", "snippet": "ML", "source": "Gemini"},
                {"title": "Doublon", "company": "Example", "url": "https://jobs.example.test/offer", "snippet": "ML", "source": "Gemini"},
                {"url": None},
                {"url": "https://jobs.example.test/another"},
            ]}, {"web_search_calls": 2, "input_tokens": 100, "output_tokens": 20, "total_tokens": 120})

    provider = AIWebSearchProvider(Provider(), "gemini_api", "gemini-test")
    results = provider.search("stage IA", limit=1)
    assert [item.url for item in results] == ["https://jobs.example.test/offer"]
    assert results[0].retrieval_mode == "AI_DIRECT" and results[0].search_provider == "gemini_api"
    assert provider.last_stats["search_call_count"] == 1
    assert provider.last_stats["web_search_calls"] == 2
    assert provider.last_stats["search_total_tokens"] == 120
    assert provider.last_stats["raw_result_count"] == 4
    assert provider.last_stats["extracted_url_count"] == 3
    assert provider.last_stats["admitted_result_count"] == 1
    assert provider.last_stats["intra_query_duplicate_count"] == 1
    assert provider.last_stats["invalid_url_count"] == 1


@pytest.mark.parametrize("metadata", [None, {"web_search_calls": 1, "web_sources": []}])
def test_ai_direct_empty_results_distinguish_zero_from_absent_metadata(metadata):
    class Provider:
        def run(self, *_args, **_kwargs):
            return {"results": []} if metadata is None else CodexOutput({"results": []}, metadata)

    provider = AIWebSearchProvider(Provider(), "chatgpt_oauth", "test-model")
    assert provider.search("stage IA") == []
    assert all(provider.last_stats[key] == 0 for key in (
        "raw_result_count", "extracted_url_count", "admitted_result_count",
        "intra_query_duplicate_count", "invalid_url_count",
    ))
    assert provider.last_stats["web_search_calls"] == (None if metadata is None else 1)
    assert provider.last_stats["web_sources"] == (None if metadata is None else [])


def test_ai_direct_error_preserves_observed_metadata_without_stale_result_counts():
    error_metadata = {"web_search_calls": 3, "web_sources": ["https://jobs.example.test/source"], "total_tokens": 42}

    class Provider:
        response = CodexOutput({"results": [{"url": "https://jobs.example.test/offer"}]}, {"web_search_calls": 1})

        def run(self, *_args, **_kwargs):
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

    ai_provider = Provider()
    recorded = []
    provider = AIWebSearchProvider(ai_provider, "chatgpt_oauth", "test-model", usage_recorder=recorded.append)
    assert len(provider.search("stage IA")) == 1
    ai_provider.response = CodexStructuredOutputError("Invalid JSON", field="$", metadata=error_metadata)
    with pytest.raises(SearchProviderUnavailableError, match="Invalid JSON"):
        provider.search("stage IA")
    assert provider.last_stats["web_search_calls"] == 3
    assert provider.last_stats["web_sources"] == error_metadata["web_sources"]
    assert provider.last_stats["search_total_tokens"] == 42
    assert all(provider.last_stats[key] is None for key in (
        "raw_result_count", "extracted_url_count", "admitted_result_count",
        "intra_query_duplicate_count", "invalid_url_count",
    ))
    assert provider.last_stats["search_call_count"] == 1
    assert recorded[-1] == error_metadata
    ai_provider.response = RuntimeError("Provider unavailable")
    with pytest.raises(SearchProviderUnavailableError, match="Provider unavailable"):
        provider.search("stage IA")
    assert provider.last_stats["web_search_calls"] is None
    assert provider.last_stats["web_sources"] is None
    assert provider.last_stats["search_total_tokens"] is None


def test_searxng_timeout_is_actionable():
    def timeout(request):
        raise httpx.ReadTimeout("timeout", request=request)

    provider = SearXNGProvider("http://localhost:8080", httpx.Client(transport=httpx.MockTransport(timeout)))
    with pytest.raises(SearchProviderUnavailableError, match="SearXNG unreachable"):
        provider.search("stage IA")
