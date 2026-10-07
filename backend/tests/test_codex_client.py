from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import codex_client
from app.codex_client import (
    CodexClient, CodexError, CodexStructuredOutputError, WebSearchNotExecutedError, codex_command,
)


class FakeProcess:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.terminated = False

    def poll(self): return 0 if self.terminated else None
    def terminate(self): self.terminated = True
    def wait(self, timeout=None): return 0
    def kill(self): self.terminated = True


def test_codex_absent(monkeypatch):
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: None)
    with pytest.raises(CodexError, match="Codex CLI not found in PATH"):
        codex_command(["app-server"])


def test_codex_exe_is_launched_directly(monkeypatch):
    executable = r"C:\Tools\codex.exe"
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: executable)
    assert codex_command(["app-server"], windows=True) == [executable, "app-server"]


def test_codex_cmd_uses_cmd_exe(monkeypatch):
    executable = r"C:\npm\codex.cmd"
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: executable)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    command = codex_command(["app-server", "--listen", "stdio://"], windows=True)
    assert isinstance(command, str)
    assert command.startswith(r"C:\Windows\System32\cmd.exe /d /s /c")
    assert "codex.cmd" in command and "app-server" in command


def test_non_windows_cmd_path_is_launched_directly(monkeypatch):
    executable = "/usr/local/bin/codex"
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: executable)
    assert codex_command(["app-server"], windows=False) == [executable, "app-server"]


def test_app_server_start_initialize_and_model_list(monkeypatch, tmp_path):
    process = FakeProcess()
    popen_calls = []
    requests = []
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda command, **kwargs: popen_calls.append((command, kwargs)) or process)

    def request(self, method, params):
        requests.append((method, params))
        return {"data": [{"model": "test-model"}]} if method == "model/list" else {}

    monkeypatch.setattr(CodexClient, "_request", request)
    monkeypatch.setattr(CodexClient, "_send", lambda *_: None)
    client = CodexClient("token", tmp_path)
    assert popen_calls[0][0][1:3] == ["app-server", "--listen"]
    assert "shell" not in popen_calls[0][1]
    assert requests[0][0] == "initialize"
    assert client.models() == [{"model": "test-model"}]
    assert requests[-1] == ("model/list", {})
    client.close()
    assert process.terminated


def test_app_server_start_error_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError(2, "missing")))
    with pytest.raises(CodexError, match="Codex app-server startup failed"):
        CodexClient("token", tmp_path)


def test_initialize_error_is_explicit(monkeypatch, tmp_path):
    process = FakeProcess()
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(CodexClient, "_request", lambda *_: (_ for _ in ()).throw(CodexError("invalid response")))
    with pytest.raises(CodexError, match="initialize failed"):
        CodexClient("token", tmp_path)


@pytest.mark.parametrize("isolated", [False, True])
def test_reasoning_effort_is_sent_to_turn(monkeypatch, tmp_path, isolated):
    process = FakeProcess()
    requests = []
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def request(self, method, params):
        requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            self.notifications["thread-1"].put({
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "delta": "{}"},
            })
            self.notifications["thread-1"].put({
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turn": {"status": "completed"}},
            })
        return {}

    monkeypatch.setattr(CodexClient, "_request", request)
    monkeypatch.setattr(CodexClient, "_send", lambda *_: None)
    client = CodexClient("token", tmp_path)
    assert client.run("prompt", "test-model", {}, effort="high", isolated_cwd=str(tmp_path / "isolated") if isolated else None) == {}
    start = next(params for method, params in requests if method == "thread/start")
    assert start["cwd"] == str(tmp_path / "isolated" if isolated else tmp_path)
    if isolated:
        assert start["config"] == {"project_doc_max_bytes": 0, "features.shell_tool": False}
        assert "aucun fichier local" in start["developerInstructions"]
    else:
        assert "config" not in start
    assert next(params for method, params in requests if method == "turn/start")["effort"] == "high"
    client.close()


def test_web_search_is_enabled_and_proven_by_app_server_event(monkeypatch, tmp_path):
    process = FakeProcess()
    requests = []
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def request(self, method, params):
        requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-web"}}
        if method == "turn/start":
            messages = self.notifications["thread-web"]
            messages.put({"method": "item/completed", "params": {
                "threadId": "thread-web", "item": {
                    "id": "web-1", "type": "webSearch", "query": "stage IA Paris",
                    "action": {"type": "open_page", "url": "https://jobs.example.test/offer"},
                },
            }})
            messages.put({"method": "turn/completed", "params": {
                "threadId": "thread-web", "turn": {"id": "turn-web", "status": "completed", "items": [
                    {"id": "final", "type": "agentMessage", "phase": "final_answer", "text": '{"offers":[]}'},
                ]},
            }})
        return {}

    monkeypatch.setattr(CodexClient, "_request", request)
    monkeypatch.setattr(CodexClient, "_send", lambda *_: None)
    client = CodexClient("token", tmp_path)
    result = client.run("prompt", "test-model", {}, web_search=True)
    start = next(params for method, params in requests if method == "thread/start")
    assert start["config"] == {"web_search": "live"}
    assert result.run_metadata["web_search_calls"] == 1
    assert result.run_metadata["web_domains"] == ["jobs.example.test"]
    client.close()


def test_required_web_search_without_event_fails(monkeypatch, tmp_path):
    process = FakeProcess()
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def request(self, method, params):
        if method == "thread/start":
            return {"thread": {"id": "thread-no-web"}}
        if method == "turn/start":
            self.notifications["thread-no-web"].put({"method": "turn/completed", "params": {
                "threadId": "thread-no-web", "turn": {"id": "turn-no-web", "status": "completed", "items": []},
            }})
        return {}

    monkeypatch.setattr(CodexClient, "_request", request)
    monkeypatch.setattr(CodexClient, "_send", lambda *_: None)
    client = CodexClient("token", tmp_path)
    with pytest.raises(WebSearchNotExecutedError, match="not executed"):
        client.run("prompt", "test-model", {}, web_search=True)
    client.close()


def test_completed_turn_uses_final_answer_not_commentary_deltas(monkeypatch, tmp_path):
    process = FakeProcess()
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def request(self, method, params):
        if method == "thread/start":
            return {"thread": {"id": "thread-failure-shape"}}
        if method == "turn/start":
            messages = self.notifications["thread-failure-shape"]
            messages.put({"method": "item/agentMessage/delta", "params": {
                "threadId": "thread-failure-shape", "turnId": "turn-1",
                "itemId": "commentary-1", "delta": "Je vérifie les sources.",
            }})
            messages.put({"method": "item/agentMessage/delta", "params": {
                "threadId": "thread-failure-shape", "turnId": "turn-1",
                "itemId": "final-1", "delta": '{"result":"accepted"}',
            }})
            messages.put({"method": "turn/completed", "params": {
                "threadId": "thread-failure-shape", "turn": {
                    "id": "turn-1", "status": "completed", "items": [
                        {"id": "commentary-1", "type": "agentMessage", "phase": "commentary", "text": "Je vérifie les sources."},
                        {"id": "final-1", "type": "agentMessage", "phase": "final_answer", "text": '{"result":"accepted"}'},
                    ],
                },
            }})
        return {}

    monkeypatch.setattr(CodexClient, "_request", request)
    monkeypatch.setattr(CodexClient, "_send", lambda *_: None)
    client = CodexClient("token", tmp_path)
    result = client.run("prompt", "test-model", {})
    assert result == {"result": "accepted"}
    assert result.run_metadata == {
        "thread_id": "thread-failure-shape", "turn_id": "turn-1", "status": "completed",
        "web_search_calls": 0, "web_domains": [],
    }
    client.close()


def test_invalid_final_answer_reports_safe_json_location(monkeypatch, tmp_path):
    process = FakeProcess()
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def request(self, method, params):
        if method == "thread/start":
            return {"thread": {"id": "thread-invalid"}}
        if method == "turn/start":
            self.notifications["thread-invalid"].put({"method": "turn/completed", "params": {
                "threadId": "thread-invalid", "turn": {"id": "turn-invalid", "status": "completed", "items": [
                    {"id": "final-invalid", "type": "agentMessage", "phase": "final_answer", "text": '{"cv":'},
                ]},
            }})
        return {}

    monkeypatch.setattr(CodexClient, "_request", request)
    monkeypatch.setattr(CodexClient, "_send", lambda *_: None)
    client = CodexClient("token", tmp_path)
    with pytest.raises(CodexStructuredOutputError, match=r"line 1, column 7") as captured:
        client.run("prompt", "test-model", {})
    assert captured.value.metadata["turn_id"] == "turn-invalid"
    assert '{"cv":' not in str(captured.value)
    client.close()


def test_concurrent_runs_use_distinct_threads(monkeypatch, tmp_path):
    process = FakeProcess()
    lock = threading.Lock()
    thread_ids = iter(("thread-1", "thread-2"))
    turns = []
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def request(self, method, params):
        if method == "thread/start":
            with lock:
                return {"thread": {"id": next(thread_ids)}}
        if method == "turn/start":
            thread_id = params["threadId"]
            turns.append(thread_id)
            self.notifications[thread_id].put({
                "method": "item/agentMessage/delta",
                "params": {"threadId": thread_id, "delta": f'{{"thread":"{thread_id}"}}'},
            })
            self.notifications[thread_id].put({
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": {"status": "completed"}},
            })
        return {}

    monkeypatch.setattr(CodexClient, "_request", request)
    monkeypatch.setattr(CodexClient, "_send", lambda *_: None)
    client = CodexClient("token", tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: client.run("prompt", "test-model", {}), range(2)))
    assert {result["thread"] for result in results} == {"thread-1", "thread-2"}
    assert set(turns) == {"thread-1", "thread-2"}
    client.close()


@pytest.mark.parametrize("status,text,web_search,error_class", [
    ("completed", "{}", False, None),
    ("completed", "{", False, CodexStructuredOutputError),
    ("completed", "{}", True, WebSearchNotExecutedError),
    ("failed", "{}", False, CodexError),
])
def test_token_usage_notifications_keep_latest_thread_total_once(
    monkeypatch, tmp_path, status, text, web_search, error_class,
):
    process = FakeProcess()
    monkeypatch.setattr(codex_client.shutil, "which", lambda _: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def request(self, method, params):
        if method == "thread/start":
            return {"thread": {"id": "usage-thread"}, "model": "actual-model"}
        if method == "turn/start":
            messages = self.notifications["usage-thread"]
            for input_tokens, output_tokens in [(80, 20), (180, 50), (180, 50)]:
                messages.put({"method": "thread/tokenUsage/updated", "params": {
                    "threadId": "usage-thread", "turnId": "usage-turn", "tokenUsage": {
                        "total": {"inputTokens": input_tokens, "outputTokens": output_tokens,
                                  "totalTokens": input_tokens + output_tokens},
                        "last": {"inputTokens": 100, "outputTokens": 30, "totalTokens": 130},
                    },
                }})
            messages.put({"method": "turn/completed", "params": {
                "threadId": "usage-thread", "turn": {
                    "id": "usage-turn", "status": status,
                    "usage": {"input_tokens": 9999, "output_tokens": 9999, "total_tokens": 19998},
                    "items": [{"type": "agentMessage", "phase": "final_answer", "text": text}],
                },
            }})
        return {}

    monkeypatch.setattr(CodexClient, "_request", request)
    monkeypatch.setattr(CodexClient, "_send", lambda *_: None)
    with CodexClient("fake", tmp_path) as client:
        if error_class:
            with pytest.raises(error_class) as captured:
                client.run("prompt", "configured-model", {}, web_search=web_search)
            metadata = captured.value.metadata
        else:
            metadata = client.run("prompt", "configured-model", {}).run_metadata
    assert {key: metadata[key] for key in ("input_tokens", "output_tokens", "total_tokens")} == {
        "input_tokens": 180, "output_tokens": 50, "total_tokens": 230,
    }
    assert metadata["model"] == "actual-model"
