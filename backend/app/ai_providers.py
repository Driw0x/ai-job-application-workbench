from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx
import keyring
from keyring.errors import PasswordDeleteError
from openai import OpenAI

from .codex_client import CodexOutput, CodexStructuredOutputError, WebSearchNotExecutedError
from .search_providers import canonical_url

OPENAI_PROVIDER = "openai"
ANTHROPIC_PROVIDER = "anthropic_api"
GEMINI_PROVIDER = "gemini_api"
DEEPSEEK_PROVIDER = "deepseek_api"
API_PROVIDERS = (OPENAI_PROVIDER, ANTHROPIC_PROVIDER, GEMINI_PROVIDER, DEEPSEEK_PROVIDER)
PROVIDER_LABELS = {
    OPENAI_PROVIDER: "OpenAI",
    ANTHROPIC_PROVIDER: "Anthropic / Claude",
    GEMINI_PROVIDER: "Google Gemini",
    DEEPSEEK_PROVIDER: "DeepSeek",
}
EMPTY_CAPABILITIES = {
    "structured_output": False, "web_search": False, "reasoning_effort": False,
    "streaming": False, "tool_calling": False,
}
OPENAI_CAPABILITIES = {
    "structured_output": True, "web_search": True, "reasoning_effort": True,
    "streaming": True, "tool_calling": True,
}
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CHATGPT_RESPONSES_URL = "https://api.openai.com/v1/responses"


class ChatGPTResponsesError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: str = "",
                 metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.metadata = metadata or {}


class ChatGPTAuthExpiredError(ChatGPTResponsesError):
    pass


class WebSearchNotAllowedError(ChatGPTResponsesError):
    pass


class WebSearchUnsupportedModelError(ChatGPTResponsesError):
    pass


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    for method in ("to_dict", "model_dump"):
        converter = getattr(value, method, None)
        if converter:
            return converter()
    return vars(value)


def _supported(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value.get("supported"))
    return bool(getattr(value, "supported", False))


def _usage_metadata(response: Any) -> dict[str, int]:
    data = _dict(response)
    usage = data.get("usage") or data.get("usage_metadata") or {}
    if not isinstance(usage, dict):
        try:
            usage = _dict(usage)
        except TypeError:
            return {}

    def first(*names: str) -> int | None:
        for name in names:
            value = usage.get(name)
            if type(value) is int and value >= 0:
                return value
        return None

    input_tokens = first("input_tokens", "total_input_tokens", "input_token_count", "prompt_token_count", "prompt_tokens")
    output_tokens = first("output_tokens", "total_output_tokens", "output_token_count", "candidates_token_count",
                          "response_token_count", "completion_tokens")
    if input_tokens is not None:
        # Anthropic reports cache reads/writes separately from uncached input.
        input_tokens += (first("cache_creation_input_tokens") or 0) + (first("cache_read_input_tokens") or 0)
    total_tokens = first("total_tokens", "total_token_count")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        if "total_input_tokens" not in usage and "prompt_token_count" not in usage:
            total_tokens = input_tokens + output_tokens
        else:
            # Gemini totals also include thoughts and tool input. Missing details stay unknown.
            thoughts = first("total_thought_tokens", "thoughts_token_count")
            tools = first("total_tool_use_tokens", "tool_use_prompt_token_count")
            if thoughts is not None and tools is not None:
                total_tokens = input_tokens + output_tokens + thoughts + tools
    return {
        key: value for key, value in {
            "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens,
        }.items() if value is not None
    }


def _response_metadata(response: Any, *, web_search: bool = False, provider: str = "",
                       status_field: str = "status") -> dict[str, Any]:
    data = _dict(response)
    metadata = {"response_id": str(data.get("id") or ""), "status": str(data.get(status_field) or "")}
    if isinstance(data.get("model"), str) and data["model"]:
        metadata["model"] = data["model"]
    metadata.update(_usage_metadata(response))
    try:
        metadata.update(_web_metadata(response, web_search=web_search, provider=provider))
    except WebSearchNotExecutedError as error:
        error.metadata = metadata
        raise
    return metadata


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)
    elif hasattr(value, "model_dump"):
        yield from _walk(value.model_dump())


def _web_metadata(response: Any, *, web_search: bool, provider: str) -> dict[str, Any]:
    data = _dict(response)
    objects = list(_walk(data))
    calls = [item for item in objects if (
        item.get("type") in {"web_search_call", "google_search_call"}
        or item.get("type") == "server_tool_use" and item.get("name") == "web_search"
    )]
    queries = [item for item in objects if isinstance(item.get("web_search_queries"), list)]
    count = len({
        str(item.get("id") or json.dumps(item, sort_keys=True, default=str))
        for item in calls
    }) or sum(len(item["web_search_queries"]) for item in queries)
    source_values = []
    sources_available = False
    for item in _walk(calls):
        if isinstance(item.get("sources"), list):
            sources_available = True
            source_values.extend(source.get("url") for source in item["sources"] if isinstance(source, dict))
        if item.get("type") in {"open_page", "find_in_page"} and "url" in item:
            sources_available = True
            source_values.append(item["url"])
    for item in objects:
        if item.get("type") in {"url_citation", "web_search_result", "web_search_result_location"} and "url" in item:
            sources_available = True
            source_values.append(item["url"])
        for key in ("grounding_chunks", "groundingChunks"):
            if isinstance(item.get(key), list):
                sources_available = True
                source_values.extend(
                    chunk["web"].get("uri") for chunk in item[key]
                    if isinstance(chunk, dict) and isinstance(chunk.get("web"), dict)
                )
    source_urls = set()
    for value in source_values:
        if not isinstance(value, str):
            continue
        try:
            source_urls.add(canonical_url(value))
        except ValueError:
            continue
    domains = sorted({
        host
        for value in source_urls
        if (host := (urlsplit(value).hostname or "").lower())
    })
    actions = sorted({
        action
        for item in calls
        if isinstance(item.get("action"), dict)
        if (action := item["action"].get("type"))
    })
    search_queries = sorted({
        query
        for item in calls
        if isinstance(item.get("action"), dict)
        for query in ([item["action"].get("query")] + (item["action"].get("queries") or []))
        if isinstance(query, str) and query
    } | {
        query
        for item in calls
        if isinstance(item.get("arguments"), dict)
        for query in item["arguments"].get("queries", []) or []
        if isinstance(query, str) and query
    } | {
        query for item in queries for query in item["web_search_queries"]
        if isinstance(query, str) and query
    })
    if web_search and count == 0:
        raise WebSearchNotExecutedError(f"Web search not executed by {provider}")
    return {
        "web_search_calls": count, "web_domains": domains,
        "web_search_actions": actions, "web_search_queries": search_queries,
        "web_sources": sorted(source_urls) if sources_available else None,
    }


def _json_output(text: str, metadata: dict[str, Any]) -> CodexOutput:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        field = f"line {error.lineno}, column {error.colno}"
        raise CodexStructuredOutputError(
            f"Validation response failed: $ ({field}): {error.msg}", field=field, metadata=metadata,
        ) from error
    if not isinstance(value, dict):
        raise CodexStructuredOutputError(
            f"Validation response failed: $: expected object, got {type(value).__name__}",
            field="$", metadata=metadata,
        )
    return CodexOutput(value, metadata)


class ApiKeyStore:
    """Store API keys in the operating system credential store."""

    def get(self, provider: str) -> str | None:
        return keyring.get_password("AIJobApplicationWorkbench", f"{provider}:api_key")

    def set(self, provider: str, value: str) -> None:
        keyring.set_password("AIJobApplicationWorkbench", f"{provider}:api_key", value)

    def delete(self, provider: str) -> None:
        try:
            keyring.delete_password("AIJobApplicationWorkbench", f"{provider}:api_key")
        except PasswordDeleteError:
            pass


class OpenAIProvider:
    def __init__(self, api_key: str, client_factory=None) -> None:
        self._owns_client = client_factory is None
        if client_factory is None:
            client_factory = OpenAI
        self.client = client_factory(api_key=api_key)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def models(self) -> list[dict[str, Any]]:
        ids = sorted({item.id for item in self.client.models.list().data
                      if item.id.startswith(("gpt-5", "gpt-6")) and "chatgpt" not in item.id.lower()})
        return [{"model": model, "displayName": model, "reasoningEfforts": list(REASONING_EFFORTS),
                 "capabilities": OPENAI_CAPABILITIES} for model in ids]

    def test(self) -> None:
        self.client.models.list()

    def run(self, prompt: str, model: str, output_schema: dict[str, Any], effort: str = "medium",
            *, web_search: bool = False) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "model": model, "input": prompt, "reasoning": {"effort": effort},
            "text": {"format": {"type": "json_schema", "name": "job_tracker_output",
                                "schema": output_schema, "strict": True}},
        }
        if web_search:
            arguments["tools"] = [{"type": "web_search"}]
            arguments["tool_choice"] = "required"
            arguments["include"] = ["web_search_call.action.sources"]
        response = self.client.responses.create(**arguments)
        metadata = _response_metadata(response, web_search=web_search, provider="OpenAI")
        return _json_output(response.output_text, metadata)


class ChatGPTOAuthResponsesProvider:
    """Direct Responses API client for ChatGPT-plan discovery only."""

    def __init__(self, access_token: str, http: httpx.Client | None = None) -> None:
        self.access_token = access_token
        self._owns_http = http is None
        self.http = http if http is not None else httpx.Client(timeout=300)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    @staticmethod
    def _error(response: httpx.Response, payload: dict[str, Any] | None = None) -> ChatGPTResponsesError:
        payload = payload or {}
        error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        code = str(error.get("code") or "")
        param = str(error.get("param") or "")
        message = str(error.get("message") or payload.get("detail") or response.reason_phrase)
        metadata = _response_metadata(payload)
        if response.status_code == 401 or code == "subscription_sharing_invalid_user":
            return ChatGPTAuthExpiredError(
                "ChatGPT session expired. Sign in again.", status_code=401, code=code, metadata=metadata,
            )
        if code == "subscription_sharing_unsupported_capability" and "model" in param.casefold():
            return WebSearchUnsupportedModelError(
                "Web search unavailable for this model.", status_code=400, code=code, metadata=metadata,
            )
        if response.status_code == 403 or code in {
            "chatpass_v2_scope_not_authorized", "chatpass_v2_invalid_authorization_context",
            "subscription_sharing_user_not_eligible", "subscription_sharing_unsupported_capability",
        }:
            return WebSearchNotAllowedError(
                "Web search is not authorized for this account / workspace / model.",
                status_code=response.status_code, code=code, metadata=metadata,
            )
        return ChatGPTResponsesError(message, status_code=response.status_code, code=code, metadata=metadata)

    @staticmethod
    def _output_text(response: dict[str, Any]) -> str:
        return "".join(
            content.get("text", "")
            for item in response.get("output", [])
            if isinstance(item, dict) and item.get("type") == "message"
            for content in item.get("content", [])
            if isinstance(content, dict) and content.get("type") == "output_text"
        )

    def run(
        self, prompt: str, model: str, output_schema: dict[str, Any], effort: str = "medium",
        *, web_search: bool = False, reasoning_effort: bool = True,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "model": model,
            "input": [{"role": "user", "content": prompt}],
            "store": False,
            "stream": True,
            "text": {"format": {"type": "json_schema", "name": "job_tracker_output",
                                  "schema": output_schema, "strict": True}},
        }
        if reasoning_effort:
            arguments["reasoning"] = {"effort": effort}
        if web_search:
            arguments.update({
                "tools": [{"type": "web_search"}],
                "tool_choice": {"type": "web_search"},
                "include": ["web_search_call.action.sources"],
            })

        completed = None
        observed_response: dict[str, Any] = {}
        streamed_items: list[dict[str, Any]] = []
        streamed_web_calls: set[str] = set()
        text_deltas: list[str] = []
        with self.http.stream(
            "POST", CHATGPT_RESPONSES_URL,
            headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"},
            json=arguments,
        ) as response:
            if response.status_code >= 400:
                response.read()
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"detail": response.text}
                raise self._error(response, payload)
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                event = json.loads(data)
                event_type = event.get("type", "")
                if isinstance(event.get("response"), dict):
                    observed_response = event["response"]
                item = event.get("item")
                if event_type == "response.output_item.done" and isinstance(item, dict):
                    streamed_items.append(item)
                if event_type.startswith("response.web_search_call.") and event.get("item_id"):
                    streamed_web_calls.add(str(event["item_id"]))
                if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
                    text_deltas.append(event["delta"])
                if event_type == "response.failed":
                    error_response = httpx.Response(400, request=response.request)
                    raise self._error(error_response, event.get("response", {}))
                if event_type == "response.completed":
                    completed = event.get("response")

        if not isinstance(completed, dict):
            raise ChatGPTResponsesError(
                "Responses stream ended without response.completed.", metadata=_response_metadata(observed_response),
            )
        metadata = _response_metadata(completed)
        observed = {**completed, "output": [*completed.get("output", []), *streamed_items]}
        metadata.update(_web_metadata(observed, web_search=False, provider="OpenAI Responses"))
        metadata["web_search_calls"] = max(metadata["web_search_calls"], len(streamed_web_calls))
        if web_search and not metadata["web_search_calls"]:
            raise WebSearchNotExecutedError("Web search not executed by OpenAI Responses", metadata=metadata)
        return _json_output(self._output_text(completed) or "".join(text_deltas), metadata)


class AnthropicProvider:
    def __init__(self, api_key: str, client_factory=None) -> None:
        self._owns_client = client_factory is None
        if client_factory is None:
            from anthropic import Anthropic
            client_factory = Anthropic
        self.client = client_factory(api_key=api_key)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def models(self) -> list[dict[str, Any]]:
        result = []
        for item in self.client.models.list().data:
            data = _dict(item)
            capabilities = data.get("capabilities") or {}
            effort = capabilities.get("effort") or {}
            efforts = [name for name in REASONING_EFFORTS if _supported(effort.get(name))]
            result.append({
                "model": data["id"], "displayName": data.get("display_name", data["id"]),
                "reasoningEfforts": efforts or ["medium"],
                "capabilities": {
                    "structured_output": _supported(capabilities.get("structured_outputs")),
                    "web_search": _supported(capabilities.get("web_search")),
                    "reasoning_effort": bool(efforts), "streaming": True,
                    "tool_calling": _supported(capabilities.get("tool_use")),
                },
            })
        return result

    def test(self) -> None:
        self.client.models.list(limit=1)

    def run(self, prompt: str, model: str, output_schema: dict[str, Any], effort: str = "medium",
            *, web_search: bool = False) -> dict[str, Any]:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": output_schema}}
        if effort != "medium":
            output_config["effort"] = effort
        arguments: dict[str, Any] = {
            "model": model, "max_tokens": 16000, "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        if web_search:
            arguments["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]
        response = self.client.messages.create(**arguments)
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        metadata = _response_metadata(response, web_search=web_search, provider="Anthropic", status_field="stop_reason")
        return _json_output(text, metadata)


class GeminiProvider:
    def __init__(self, api_key: str, client_factory=None) -> None:
        self._owns_client = client_factory is None
        if client_factory is None:
            from google import genai
            client_factory = genai.Client
        self.client = client_factory(api_key=api_key)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def models(self) -> list[dict[str, Any]]:
        result = []
        excluded = ("embedding", "image", "live", "tts", "transcribe", "robotics", "computer-use")
        for item in self.client.models.list():
            data = _dict(item)
            model = (data.get("name") or data.get("id") or "").removeprefix("models/")
            if not model.startswith("gemini-") or any(part in model for part in excluded):
                continue
            current_text_model = model.startswith(("gemini-3", "gemini-2.5"))
            web_search = model.startswith(("gemini-3", "gemini-2.5", "gemini-2.0"))
            result.append({
                "model": model, "displayName": data.get("display_name", model),
                "reasoningEfforts": ["medium"],
                "capabilities": {
                    "structured_output": current_text_model, "web_search": web_search,
                    "reasoning_effort": False, "streaming": True, "tool_calling": current_text_model,
                },
            })
        return sorted(result, key=lambda item: item["model"])

    def test(self) -> None:
        next(iter(self.client.models.list()))

    def run(self, prompt: str, model: str, output_schema: dict[str, Any], effort: str = "medium",
            *, web_search: bool = False) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "model": model, "input": prompt, "store": False,
        }
        if not web_search or model.startswith("gemini-3"):
            arguments["response_format"] = {
                "type": "text", "mime_type": "application/json", "schema": output_schema,
            }
        if web_search:
            arguments["tools"] = [{"type": "google_search"}]
        response = self.client.interactions.create(**arguments)
        metadata = _response_metadata(response, web_search=web_search, provider="Gemini")
        return _json_output(response.output_text, metadata)


class DeepSeekProvider:
    def __init__(self, api_key: str, client_factory=None) -> None:
        self._owns_client = client_factory is None
        if client_factory is None:
            client_factory = OpenAI
        self.client = client_factory(api_key=api_key, base_url="https://api.deepseek.com")

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def models(self) -> list[dict[str, Any]]:
        result = []
        for item in self.client.models.list().data:
            data = _dict(item)
            effort = data.get("effort") or {}
            efforts = [value for value in effort.get("supported_levels", []) if value in REASONING_EFFORTS]
            result.append({
                "model": data["id"], "displayName": data.get("name", data["id"]),
                "reasoningEfforts": efforts or ["medium"],
                "capabilities": {
                    "structured_output": True, "web_search": False,
                    "reasoning_effort": bool(efforts), "streaming": True, "tool_calling": True,
                },
            })
        return result

    def test(self) -> None:
        self.client.models.list()

    def run(self, prompt: str, model: str, output_schema: dict[str, Any], effort: str = "medium",
            *, web_search: bool = False) -> dict[str, Any]:
        if web_search:
            raise ValueError("DeepSeek does not support Web search")
        arguments: dict[str, Any] = {
            "model": model, "input": prompt,
            "text": {"format": {"type": "json_schema", "name": "job_tracker_output",
                                "schema": output_schema, "strict": True}},
        }
        if effort != "medium":
            arguments["reasoning"] = {"effort": effort}
        response = self.client.responses.create(**arguments)
        metadata = _response_metadata(response)
        return _json_output(response.output_text, metadata)
