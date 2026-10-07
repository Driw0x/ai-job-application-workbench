from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class CodexError(RuntimeError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.metadata = metadata or {}


class CodexStructuredOutputError(CodexError):
    def __init__(self, message: str, *, field: str, metadata: dict[str, Any]) -> None:
        super().__init__(message)
        self.error_type = "json_decode"
        self.field = field
        self.metadata = metadata


class CodexOutput(dict[str, Any]):
    def __init__(self, value: dict[str, Any], metadata: dict[str, Any]) -> None:
        super().__init__(value)
        self.run_metadata = metadata


class WebSearchNotExecutedError(CodexError):
    pass


def _token_metadata(usage: dict[str, Any]) -> dict[str, int]:
    metadata = {}
    for target, names in {
        "input_tokens": ("input_tokens", "inputTokens"),
        "output_tokens": ("output_tokens", "outputTokens"),
        "total_tokens": ("total_tokens", "totalTokens"),
    }.items():
        value = next((usage.get(name) for name in names
                      if type(usage.get(name)) is int and usage[name] >= 0), None)
        if value is not None:
            metadata[target] = value
    if "total_tokens" not in metadata and "input_tokens" in metadata and "output_tokens" in metadata:
        metadata["total_tokens"] = metadata["input_tokens"] + metadata["output_tokens"]
    return metadata


def _urls(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _urls(child)]
    if isinstance(value, list):
        return [item for child in value for item in _urls(child)]
    return [value] if isinstance(value, str) and value.startswith(("http://", "https://")) else []


def final_agent_text(turn: dict[str, Any], deltas: dict[str, list[str]], item_order: list[str]) -> str:
    messages = [item for item in turn.get("items", []) if item.get("type") == "agentMessage"]
    final = [item for item in messages if item.get("phase") == "final_answer"]
    compatible = [item for item in messages if item.get("phase") is None]
    candidates = final or compatible
    if candidates:
        return candidates[-1].get("text", "")
    if item_order:
        return "".join(deltas[item_order[-1]])
    return ""


def codex_command(arguments: list[str], windows: bool | None = None) -> list[str] | str:
    executable = shutil.which("codex")
    if not executable:
        raise CodexError("Codex CLI not found in PATH.")
    if (os.name == "nt" if windows is None else windows) and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        command_shell = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
        if not command_shell:
            raise CodexError("Windows cmd.exe shell not found.")
        command_line = subprocess.list2cmdline(arguments)
        prefix = subprocess.list2cmdline([command_shell, "/d", "/s", "/c"])
        return f'{prefix} ""{executable}" {command_line}"'
    return [executable, *arguments]


class CodexClient:
    def __init__(self, access_token: str, cwd: Path, timeout: int = 600) -> None:
        self.cwd = cwd
        env = os.environ.copy()
        for name in ("OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"):
            env.pop(name, None)
        env["ACCESS_TOKEN"] = access_token
        arguments = [
            "app-server", "--listen", "stdio://",
            "-c", "model_provider=openai_chatgpt_plan",
            "-c", "model_providers.openai_chatgpt_plan.name='ChatGPT plan'",
            "-c", "model_providers.openai_chatgpt_plan.base_url=https://api.openai.com/v1",
            "-c", "model_providers.openai_chatgpt_plan.env_key=ACCESS_TOKEN",
            "-c", "model_providers.openai_chatgpt_plan.wire_api=responses",
            "-c", "model_providers.openai_chatgpt_plan.requires_openai_auth=false",
            "-c", "model_providers.openai_chatgpt_plan.supports_websockets=false",
        ]
        try:
            self.process = subprocess.Popen(
                codex_command(arguments), cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            )
        except CodexError:
            raise
        except OSError as error:
            raise CodexError(f"Codex app-server startup failed : {error}") from error
        self.timeout = timeout
        self.responses: dict[int, queue.Queue[dict[str, Any]]] = {}
        self.notifications: dict[str, queue.Queue[dict[str, Any]]] = {}
        self.buffered_notifications: dict[str, list[dict[str, Any]]] = {}
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.next_id = 1
        self.errors: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_errors, daemon=True).start()
        try:
            self._request("initialize", {"clientInfo": {"name": "ai_job_application_workbench", "title": "AI Job Application Workbench", "version": "1.0.0"}})
            self._send({"method": "initialized", "params": {}})
        except Exception as error:
            self.close()
            raise CodexError(f"initialize failed : {error}") from error

    def _read(self) -> None:
        assert self.process.stdout
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self.lock:
                request_id = message.get("id")
                if request_id in self.responses:
                    self.responses[request_id].put(message)
                    continue
                thread_id = message.get("params", {}).get("threadId")
                if thread_id in self.notifications:
                    self.notifications[thread_id].put(message)
                elif thread_id:
                    self.buffered_notifications.setdefault(thread_id, []).append(message)

    def _read_errors(self) -> None:
        assert self.process.stderr
        for line in self.process.stderr:
            self.errors.append(line.strip())
            self.errors = self.errors[-10:]

    def _send(self, message: dict[str, Any]) -> None:
        with self.send_lock:
            if self.process.poll() is not None:
                raise CodexError("Codex app-server stopped")
            assert self.process.stdin
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
            response: queue.Queue[dict[str, Any]] = queue.Queue()
            self.responses[request_id] = response
        try:
            self._send({"id": request_id, "method": method, "params": params})
            try:
                message = response.get(timeout=self.timeout)
            except queue.Empty as error:
                detail = self.errors[-1] if self.errors else f"Timeout during {method}"
                raise CodexError(detail) from error
            if "error" in message:
                raise CodexError(str(message["error"].get("message", message["error"])))
            return message.get("result", {})
        finally:
            with self.lock:
                self.responses.pop(request_id, None)

    def models(self) -> list[dict[str, Any]]:
        try:
            return self._request("model/list", {}).get("data", [])
        except CodexError as error:
            raise CodexError(f"model/list failed : {error}") from error

    def run(
        self, prompt: str, model: str, output_schema: dict[str, Any], effort: str = "medium",
        *, web_search: bool = False, isolated_cwd: str | None = None,
    ) -> dict[str, Any]:
        thread_params: dict[str, Any] = {
            "model": model, "cwd": isolated_cwd or str(self.cwd), "approvalPolicy": "never", "sandbox": "read-only",
            "ephemeral": True,
        }
        if isolated_cwd is not None:
            thread_params["config"] = {"project_doc_max_bytes": 0, "features.shell_tool": False}
            thread_params["developerInstructions"] = "Utilise exclusivement le contexte fourni. Ne consulte aucun fichier local ni aucune Knowledge Base."
        if web_search:
            thread_params.update({
                "config": {"web_search": "live"},
                "developerInstructions": (
                    "Cette tâche exige une recherche Web actuelle. Utilise Web Search au moins une fois "
                    "avant la réponse finale. N'utilise pas seulement tes connaissances internes."
                ),
            })
        started = self._request("thread/start", thread_params)
        thread_id = started["thread"]["id"]
        metadata: dict[str, Any] = {"thread_id": thread_id}
        actual_model = started.get("model") or started["thread"].get("model")
        if isinstance(actual_model, str) and actual_model:
            metadata["model"] = actual_model
        messages: queue.Queue[dict[str, Any]] = queue.Queue()
        with self.lock:
            self.notifications[thread_id] = messages
            for message in self.buffered_notifications.pop(thread_id, []):
                messages.put(message)
        try:
            self._request("turn/start", {
                "threadId": thread_id, "input": [{"type": "text", "text": prompt}],
                "outputSchema": output_schema, "effort": effort,
            })
            deltas: dict[str, list[str]] = {}
            item_order: list[str] = []
            web_items: list[dict[str, Any]] = []
            turn: dict[str, Any] = {}
            while True:
                try:
                    message = messages.get(timeout=self.timeout)
                except queue.Empty as error:
                    raise CodexError("Codex turn timed out") from error
                method, params = message.get("method"), message.get("params", {})
                if method == "thread/tokenUsage/updated":
                    usage = params.get("tokenUsage") or {}
                    if isinstance(usage, dict) and isinstance(usage.get("total"), dict):
                        # Each run owns a fresh thread: retain its latest cumulative snapshot once.
                        metadata.update(_token_metadata(usage["total"]))
                if method == "model/rerouted" and isinstance(params.get("toModel"), str):
                    metadata["model"] = params["toModel"]
                if method == "item/agentMessage/delta":
                    item_id = params.get("itemId", "legacy")
                    if item_id not in deltas:
                        deltas[item_id] = []
                        item_order.append(item_id)
                    deltas[item_id].append(params.get("delta", ""))
                if method == "item/completed" and params.get("item", {}).get("type") == "webSearch":
                    web_items.append(params["item"])
                if method == "turn/completed":
                    turn = params.get("turn", {})
                    break
        except Exception as error:
            error.metadata = {**metadata, **getattr(error, "metadata", {})}
            raise
        finally:
            with self.lock:
                self.notifications.pop(thread_id, None)
        metadata.update({
            "turn_id": str(turn.get("id", "")),
            "status": str(turn.get("status", "completed")),
            "web_search_calls": len(web_items),
            "web_domains": sorted({
                host
                for item in web_items
                for value in _urls(item)
                if (host := (urlsplit(value).hostname or "").lower())
            }),
        })
        usage = turn.get("usage") if isinstance(turn.get("usage"), dict) else {}
        if not any(key in metadata for key in ("input_tokens", "output_tokens", "total_tokens")):
            metadata.update(_token_metadata(usage))
        if turn.get("status") != "completed":
            raise CodexError(str(turn.get("error") or "Codex turn failed"), metadata=metadata)
        if web_search and not web_items:
            raise WebSearchNotExecutedError("Web search not executed by the provider", metadata=metadata)
        text = final_agent_text(turn, deltas, item_order)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            field = f"line {error.lineno}, column {error.colno}"
            raise CodexStructuredOutputError(
                f"Validation response failed: $ ({field}): {error.msg}; length={len(text)}",
                field=field,
                metadata=metadata,
            ) from error
        if not isinstance(value, dict):
            raise CodexStructuredOutputError(
                f"Validation response failed: $: expected object, got {type(value).__name__}",
                field="$",
                metadata=metadata,
            )
        return CodexOutput(value, metadata)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def __enter__(self) -> "CodexClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
