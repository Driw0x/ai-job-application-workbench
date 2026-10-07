import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app.ai_providers import (
    AnthropicProvider, ChatGPTOAuthResponsesProvider, DeepSeekProvider, GeminiProvider,
    OpenAIProvider, WebSearchNotAllowedError, WebSearchUnsupportedModelError, _usage_metadata, _web_metadata,
)
from app.chatgpt_auth import ChatGPTAuth, REVOCATION_URL, TOKEN_URL
from app.codex_client import WebSearchNotExecutedError


@pytest.mark.parametrize("provider_class", [ChatGPTAuth, ChatGPTOAuthResponsesProvider])
@pytest.mark.parametrize("injected", [False, True])
def test_http_client_closure_respects_ownership(tmp_path, monkeypatch, provider_class, injected):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as http:
        monkeypatch.setattr(httpx, "Client", lambda **_kwargs: http)
        argument = tmp_path if provider_class is ChatGPTAuth else "fake-token"
        provider = provider_class(argument, http=http if injected else None)
        provider.close()
        assert http.is_closed is not injected


@pytest.mark.parametrize("provider_class,constructor", [
    (OpenAIProvider, "app.ai_providers.OpenAI"),
    (AnthropicProvider, "anthropic.Anthropic"),
    (GeminiProvider, "google.genai.Client"),
    (DeepSeekProvider, "app.ai_providers.OpenAI"),
])
@pytest.mark.parametrize("injected", [False, True])
def test_sdk_client_closure_respects_factory_ownership(monkeypatch, provider_class, constructor, injected):
    sdk = SimpleNamespace(close=Mock())
    factory = lambda **_kwargs: sdk
    monkeypatch.setattr(constructor, factory)
    provider = provider_class("fake-key", client_factory=factory if injected else None)
    provider.close()
    assert sdk.close.call_count == (0 if injected else 1)


def test_logout_waits_for_refresh_and_removes_refreshed_credentials(tmp_path):
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    logout_waiting = threading.Event()
    revoked = []

    def handler(request):
        if str(request.url) == TOKEN_URL:
            refresh_started.set()
            assert release_refresh.wait(10)
            return httpx.Response(200, json={
                "access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 3600,
            })
        assert str(request.url) == REVOCATION_URL
        revoked.append(request.content)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        auth = ChatGPTAuth(tmp_path, http)
        auth._write("credentials.json", {
            "access_token": "old-token", "refresh_token": "old-refresh", "client_id": "client",
            "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        })
        lock = auth.lock

        class ObservedLock:
            def __enter__(self):
                if refresh_started.is_set():
                    logout_waiting.set()
                lock.acquire()

            def __exit__(self, *_args):
                lock.release()

        auth.lock = ObservedLock()
        with ThreadPoolExecutor(max_workers=2) as executor:
            refresh = executor.submit(auth.access_token)
            try:
                assert refresh_started.wait(10)
                logout = executor.submit(auth.logout)
                assert logout_waiting.wait(10)
            finally:
                release_refresh.set()
            assert refresh.result(timeout=10) == "new-token"
            logout.result(timeout=10)
        assert len(revoked) == 1 and b"token=new-refresh" in revoked[0]
        assert not (tmp_path / "credentials.json").exists()
        assert auth.status()["connected"] is False


class Endpoint:
    def __init__(self, result):
        self.result, self.calls = result, []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def test_usage_metadata_uses_provider_counts_when_available():
    assert _usage_metadata(SimpleNamespace(usage=SimpleNamespace(
        input_tokens=120, output_tokens=30, total_tokens=150,
    ))) == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}


def test_usage_metadata_reads_real_sdk_shapes_and_keeps_authoritative_total():
    from anthropic.types import Usage as AnthropicUsage
    from google.genai._gaos.types.interactions.usage import Usage as GeminiUsage
    from openai.types.responses.response_usage import ResponseUsage

    responses = [
        (ResponseUsage(
            input_tokens=120, output_tokens=30, total_tokens=150,
            input_tokens_details={"cached_tokens": 20}, output_tokens_details={"reasoning_tokens": 10},
        ), {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}),
        (AnthropicUsage(
            input_tokens=20, output_tokens=30, cache_read_input_tokens=80, cache_creation_input_tokens=20,
        ), {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}),
        (GeminiUsage(
            total_input_tokens=120, total_output_tokens=30, total_thought_tokens=40,
            total_tool_use_tokens=10, total_tokens=200,
        ), {"input_tokens": 120, "output_tokens": 30, "total_tokens": 200}),
        ({"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
         {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}),
    ]
    for usage, expected in responses:
        assert _usage_metadata(SimpleNamespace(usage=usage)) == expected


@pytest.mark.parametrize("response,expected", [
    ({}, {}),
    ({"usage": None}, {}),
    ({"usage": "unavailable"}, {}),
    ({"usage": {"input_tokens": True, "output_tokens": -1, "total_tokens": "120"}}, {}),
    ({"usage": {"input_tokens": 0, "output_tokens": 0}},
     {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}),
    ({"usage": {"total_input_tokens": 120, "total_output_tokens": 30}},
     {"input_tokens": 120, "output_tokens": 30}),
])
def test_usage_metadata_keeps_unknown_counts_unknown(response, expected):
    assert _usage_metadata(response) == expected


@pytest.mark.parametrize("text,web_search,error_type", [
    ("{", False, "json_decode"),
    ("{}", True, None),
])
def test_failed_response_keeps_usage_and_actual_model(text, web_search, error_type):
    from app.codex_client import CodexStructuredOutputError

    client = SimpleNamespace(responses=Endpoint(SimpleNamespace(
        output_text=text, id="r1", status="completed", model="actual-model", output=[],
        usage={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
    )))
    expected_error = CodexStructuredOutputError if error_type else WebSearchNotExecutedError
    with pytest.raises(expected_error) as captured:
        OpenAIProvider("fake", client_factory=lambda **_: client).run(
            "prompt", "configured-model", {}, web_search=web_search,
        )
    assert captured.value.metadata["total_tokens"] == 150
    assert captured.value.metadata["model"] == "actual-model"


def test_chatgpt_failed_stream_keeps_usage():
    from app.ai_providers import ChatGPTResponsesError

    event = {"type": "response.failed", "response": {
        "id": "failed", "status": "failed", "model": "actual-model",
        "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        "error": {"message": "Provider failed"},
    }}
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, text="data: " + json.dumps(event) + "\n\n",
    )))
    with pytest.raises(ChatGPTResponsesError) as captured:
        ChatGPTOAuthResponsesProvider("fake", http).run("prompt", "configured-model", {})
    assert captured.value.metadata["total_tokens"] == 150
    assert captured.value.metadata["model"] == "actual-model"


def sse_response(output):
    event = {"type": "response.completed", "response": {
        "id": "resp_oauth", "status": "completed", "output": output,
    }}
    return "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n"


def test_chatgpt_oauth_discovery_streams_direct_responses_with_required_web_search():
    request_body = {}

    def handler(request):
        request_body.update(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer oauth-secret"
        return httpx.Response(200, text=sse_response([
            {"type": "web_search_call", "action": {
                "type": "search", "queries": ["stages IA"],
                "sources": [{"type": "url", "url": "https://jobs.example.test/offer"}],
            }},
            {"type": "message", "content": [{"type": "output_text", "text": "{\"offers\": []}"}]},
        ]), headers={"content-type": "text/event-stream"})

    provider = ChatGPTOAuthResponsesProvider(
        "oauth-secret", httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.run("prompt", "gpt-test", {"type": "object"}, effort="high", web_search=True)
    assert result == {"offers": []}
    assert request_body["store"] is False and request_body["stream"] is True
    assert request_body["tools"] == [{"type": "web_search"}]
    assert request_body["tool_choice"] == {"type": "web_search"}
    assert request_body["reasoning"] == {"effort": "high"}
    assert result.run_metadata["web_search_calls"] == 1
    assert result.run_metadata["web_search_actions"] == ["search"]
    assert result.run_metadata["web_domains"] == ["jobs.example.test"]


def test_chatgpt_oauth_discovery_rejects_missing_web_search_call():
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, text=sse_response([
            {"type": "message", "content": [{"type": "output_text", "text": "{\"offers\": []}"}]},
        ]),
    )))
    with pytest.raises(WebSearchNotExecutedError):
        ChatGPTOAuthResponsesProvider("oauth-secret", http).run(
            "prompt", "gpt-test", {"type": "object"}, web_search=True,
        )


def test_chatgpt_oauth_discovery_collects_streamed_web_search_items():
    web_item = {"id": "ws_1", "type": "web_search_call", "action": {
        "type": "open_page", "url": "https://jobs.example.test/offer",
    }}
    completed = {"type": "response.completed", "response": {
        "id": "resp_oauth", "status": "completed", "output": [web_item],
    }}
    body = "\n\n".join([
        "data: " + json.dumps({"type": "response.web_search_call.completed", "item_id": "ws_1"}),
        "data: " + json.dumps({"type": "response.output_item.done", "item": web_item}),
        "data: " + json.dumps({"type": "response.output_text.delta", "delta": "{\"offers\": []}"}),
        "data: " + json.dumps(completed),
        "data: [DONE]",
    ]) + "\n\n"
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)))
    result = ChatGPTOAuthResponsesProvider("oauth-secret", http).run(
        "prompt", "gpt-test", {"type": "object"}, web_search=True,
    )
    assert result.run_metadata["web_search_calls"] == 1
    assert result.run_metadata["web_search_actions"] == ["open_page"]
    assert result.run_metadata["web_sources"] == ["https://jobs.example.test/offer"]


@pytest.mark.parametrize("status,payload,error", [
    (403, {"error": {"code": "subscription_sharing_user_not_eligible"}}, WebSearchNotAllowedError),
    (400, {"error": {"code": "subscription_sharing_unsupported_capability", "param": "model"}}, WebSearchUnsupportedModelError),
])
def test_chatgpt_oauth_discovery_maps_web_policy_errors(status, payload, error):
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload)))
    with pytest.raises(error):
        ChatGPTOAuthResponsesProvider("oauth-secret", http).run(
            "prompt", "gpt-test", {"type": "object"}, web_search=True,
        )


def test_openai_provider_uses_models_responses_schema_and_web_search():
    client = SimpleNamespace(
        models=Endpoint(SimpleNamespace(data=[SimpleNamespace(id="gpt-6-test"), SimpleNamespace(id="embedding-test")])),
        responses=Endpoint(SimpleNamespace(
            output_text="{}", id="r1", status="completed",
            output=[{"type": "web_search_call", "action": {"type": "search", "query": "stages IA"}}],
        )),
    )
    provider = OpenAIProvider("fake", client_factory=lambda **_: client)
    assert [item["model"] for item in provider.models()] == ["gpt-6-test"]
    provider.run("prompt", "gpt-6-test", {"type": "object"}, effort="high", web_search=True)
    call = client.responses.calls[-1]
    assert call["reasoning"] == {"effort": "high"}
    assert call["tools"] == [{"type": "web_search"}]
    assert call["tool_choice"] == "required"
    assert call["include"] == ["web_search_call.action.sources"]
    assert call["text"]["format"]["type"] == "json_schema"
    assert len(client.responses.calls) == 1


def test_web_metadata_unites_sources_and_url_citations_without_technical_urls():
    metadata = _web_metadata({
        "technical_url": "https://api.example.test/responses/123",
        "output": [
            {"id": "web-1", "type": "web_search_call", "action": {
                "type": "search", "queries": ["stage IA"], "sources": [
                    {"url": "https://jobs.example.test/offer?utm_source=search"},
                    {"url": "https://jobs.example.test/offer"},
                    {"url": "javascript:alert(1)"},
                ],
            }},
            {"type": "message", "content": [{"type": "output_text", "annotations": [
                {"type": "url_citation", "url": "https://jobs.example.test/offer#apply"},
                {"type": "url_citation", "url": "https://careers.example.test/second"},
                {"type": "url_citation", "title": "Citation without URL"},
                {"type": "url_citation", "url": "not a URL"},
            ]}]},
        ],
    }, web_search=True, provider="OpenAI")
    assert metadata["web_search_calls"] == 1
    assert metadata["web_sources"] == ["https://careers.example.test/second", "https://jobs.example.test/offer"]
    assert metadata["web_domains"] == ["careers.example.test", "jobs.example.test"]
    assert metadata["web_search_queries"] == ["stage IA"]


@pytest.mark.parametrize("action,expected_sources", [
    ({"type": "search", "sources": []}, []),
    ({"type": "search"}, None),
])
def test_web_metadata_distinguishes_empty_sources_from_unavailable_sources(action, expected_sources):
    metadata = _web_metadata({
        "technical_url": "https://api.example.test/responses/123",
        "output": [
            {"type": "web_search_call", "action": action},
            {"type": "message", "annotations": [{"type": "url_citation", "title": "No URL"}]},
        ],
    }, web_search=True, provider="OpenAI")
    assert metadata["web_sources"] == expected_sources
    assert metadata["web_domains"] == []


def test_anthropic_provider_uses_model_capabilities_structured_output_and_search():
    model = SimpleNamespace(id="claude-test", display_name="Claude Test", capabilities={
        "structured_outputs": {"supported": True}, "web_search": {"supported": True},
        "tool_use": {"supported": True}, "effort": {"high": {"supported": True}},
    })
    search = SimpleNamespace(
        type="server_tool_use", name="web_search",
        model_dump=lambda: {"type": "server_tool_use", "name": "web_search"},
    )
    client = SimpleNamespace(
        models=Endpoint(SimpleNamespace(data=[model])),
        messages=Endpoint(SimpleNamespace(
            content=[search, SimpleNamespace(type="text", text="{}")], id="m1", stop_reason="end_turn",
        )),
    )
    provider = AnthropicProvider("fake", client_factory=lambda **_: client)
    listed = provider.models()[0]
    assert listed["capabilities"]["structured_output"] is True
    assert listed["capabilities"]["web_search"] is True
    provider.run("prompt", "claude-test", {"type": "object"}, effort="high", web_search=True)
    call = client.messages.calls[-1]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "high"
    assert call["tools"][0]["type"] == "web_search_20250305"


def test_gemini_provider_uses_interactions_schema_and_google_search():
    client = SimpleNamespace(
        models=Endpoint([SimpleNamespace(name="models/gemini-3.8-flash", display_name="Gemini Test")]),
        interactions=Endpoint(SimpleNamespace(
            output_text="{}", id="g1", status="completed",
            grounding_metadata={"web_search_queries": ["stages IA"], "grounding_chunks": [
                {"web": {"uri": "https://jobs.example.test/offer", "title": "Stage IA"}},
            ]},
        )),
    )
    provider = GeminiProvider("fake", client_factory=lambda **_: client)
    listed = provider.models()[0]
    assert listed["capabilities"]["structured_output"] is True
    assert listed["capabilities"]["reasoning_effort"] is False
    result = provider.run("prompt", "gemini-3.8-flash", {"type": "object"}, web_search=True)
    call = client.interactions.calls[-1]
    assert call["response_format"]["schema"] == {"type": "object"}
    assert call["tools"] == [{"type": "google_search"}]
    assert result.run_metadata["web_search_calls"] == 1
    assert result.run_metadata["web_search_queries"] == ["stages IA"]
    assert result.run_metadata["web_sources"] == ["https://jobs.example.test/offer"]


def test_gemini_25_uses_grounding_without_unsupported_schema_combination():
    client = SimpleNamespace(
        models=Endpoint([SimpleNamespace(name="models/gemini-2.5-flash", display_name="Gemini 2.5")]),
        interactions=Endpoint(SimpleNamespace(
            output_text="{}", id="g25", status="completed",
            grounding_metadata={"web_search_queries": ["stages IA"]},
        )),
    )
    provider = GeminiProvider("fake", client_factory=lambda **_: client)
    listed = provider.models()[0]
    assert listed["capabilities"]["structured_output"] is True
    assert listed["capabilities"]["web_search"] is True
    provider.run("prompt JSON", "gemini-2.5-flash", {"type": "object"}, web_search=True)
    call = client.interactions.calls[-1]
    assert call["tools"] == [{"type": "google_search"}]
    assert "response_format" not in call


def test_deepseek_provider_has_no_web_search_and_only_sends_explicit_effort():
    model = SimpleNamespace(id="deepseek-test", name="DeepSeek Test", effort={"supported_levels": ["low", "high"]})
    client = SimpleNamespace(
        models=Endpoint(SimpleNamespace(data=[model])),
        responses=Endpoint(SimpleNamespace(output_text="{}", id="d1", status="completed")),
    )
    provider = DeepSeekProvider("fake", client_factory=lambda **kwargs: client)
    listed = provider.models()[0]
    assert listed["capabilities"]["web_search"] is False
    assert listed["reasoningEfforts"] == ["low", "high"]
    provider.run("prompt", "deepseek-test", {"type": "object"})
    assert "reasoning" not in client.responses.calls[-1]
    with pytest.raises(ValueError, match="Web search"):
        provider.run("prompt", "deepseek-test", {"type": "object"}, web_search=True)
