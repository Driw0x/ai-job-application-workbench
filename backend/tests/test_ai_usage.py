from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.ai_providers import OpenAIProvider
from app.main import connect, create_app, init_db
from test_ai_providers import Endpoint
from test_codex_integration import (
    FakeApiProvider, FakeAuth, FakeCodex, FakeKeyStore, FakeProviderError,
    FakeSearchProvider, discovery, isolated_repo, preparation, public_dns, shortlist,
    wait_for_preparation,
)


CURRENT = datetime(2026, 10, 6, 12, 34, 56, tzinfo=timezone.utc)
ZERO = {
    "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
    "calls": 0, "unknown_calls": 0,
}


@pytest.fixture
def usage_client(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return CURRENT.astimezone(tz) if tz else CURRENT.replace(tzinfo=None)

    monkeypatch.setattr(main, "datetime", Clock)
    database = tmp_path / "tracker.db"
    api = TestClient(create_app(database, tmp_path / "applications", repo_root=tmp_path))
    return api, database


def insert_usage(database, timestamp, category="SEARCH", counts=(100, 20, 120)):
    with connect(database) as db:
        db.execute(
            """INSERT INTO ai_usage(created_at, provider, model, category, operation,
                input_tokens, output_tokens, total_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, "openai", "gpt-usage-test", category,
             "offer_screening" if category == "SEARCH" else "application", *counts),
        )
        db.commit()


def test_empty_usage_defaults_and_invalid_period(usage_client):
    api, _ = usage_client
    response = api.get("/stats/ai-usage")
    assert response.status_code == 200
    stats = response.json()
    assert stats["period"] == "7d" and stats["timezone"] == "UTC"
    assert stats["granularity"] == "day"
    assert stats["total"] == stats["search"] == stats["documents"] == ZERO
    assert all(item["search_tokens"] == item["document_tokens"] == 0 for item in stats["daily"])
    assert api.get("/stats/ai-usage?period=invalid").status_code == 422


def test_statistics_use_one_snapshot_during_concurrent_usage_insert(usage_client, monkeypatch):
    api, database = usage_client
    insert_usage(database, CURRENT.isoformat())
    base_connect = main.connect
    with closing(base_connect(database)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    inserted = False

    class SnapshotConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal inserted
            if not inserted and sql.lstrip().startswith("SELECT category,"):
                inserted = True
                with closing(base_connect(database)) as writer:
                    writer.execute(
                        """INSERT INTO ai_usage(created_at, provider, model, category, operation,
                            input_tokens, output_tokens, total_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (CURRENT.isoformat(), "openai", "gpt-usage-test", "DOCUMENT", "application", 200, 40, 240),
                    )
                    writer.commit()
            return super().execute(sql, parameters)

    def snapshot_connect(path):
        connection = sqlite3.connect(path, factory=SnapshotConnection)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(main, "connect", snapshot_connect)
    stats = api.get("/stats/ai-usage?period=7d").json()
    assert inserted
    assert stats["total"] == stats["search"] == {
        "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
        "calls": 1, "unknown_calls": 0,
    }
    assert stats["documents"] == ZERO
    today = next(item for item in stats["daily"] if item["date"] == "2026-10-06")
    assert today == {"date": "2026-10-06", "search_tokens": 120, "document_tokens": 0}
    with closing(base_connect(database)) as db:
        assert db.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 2
    next_stats = api.get("/stats/ai-usage?period=7d").json()
    assert next_stats["total"]["calls"] == 2 and next_stats["total"]["total_tokens"] == 360
    assert next_stats["documents"]["total_tokens"] == 240


@pytest.mark.parametrize("period,value,calls,granularity", [
    ("today", 1, 1, "hour"),
    ("7d", 7, 3, "day"),
    ("30d", 31, 5, "day"),
    ("all", 127, 7, "day"),
])
def test_periods_include_calendar_boundary_and_exclude_future(usage_client, period, value, calls, granularity):
    api, database = usage_client
    for timestamp, tokens in [
        ("2026-08-15T00:00:00+00:00", 64),
        ("2026-09-06T23:59:59+00:00", 32),
        ("2026-09-07T00:00:00+00:00", 16),
        ("2026-09-29T23:59:59+00:00", 8),
        ("2026-09-30T00:00:00+00:00", 4),
        ("2026-10-05T23:59:59+00:00", 2),
        ("2026-10-06T00:00:00+00:00", 1),
        ("2026-10-06T12:34:57+00:00", 128),
    ]:
        insert_usage(database, timestamp, counts=(tokens, tokens * 2, tokens * 3))

    response = api.get(f"/stats/ai-usage?period={period}")
    assert response.status_code == 200
    stats = response.json()
    assert stats["period"] == period and stats["granularity"] == granularity
    assert stats["total"] == stats["search"] == {
        "input_tokens": value, "output_tokens": value * 2, "total_tokens": value * 3,
        "calls": calls, "unknown_calls": 0,
    }
    assert stats["documents"] == ZERO
    assert sum(item["search_tokens"] or 0 for item in stats["daily"]) == value * 3


def test_categories_sum_known_metrics_without_inventing_missing_totals(usage_client):
    api, database = usage_client
    timestamp = CURRENT.isoformat()
    for category, counts in [
        ("SEARCH", (100, 20, 120)),
        ("SEARCH", (None, 10, None)),
        ("SEARCH", (None, None, None)),
        ("DOCUMENT", (200, 40, 240)),
        ("DOCUMENT", (None, None, None)),
    ]:
        insert_usage(database, timestamp, category, counts)

    stats = api.get("/stats/ai-usage?period=7d").json()
    assert stats["total"] == {
        "input_tokens": 300, "output_tokens": 70, "total_tokens": 360,
        "calls": 5, "unknown_calls": 3,
    }
    assert stats["search"] == {
        "input_tokens": 100, "output_tokens": 30, "total_tokens": 120,
        "calls": 3, "unknown_calls": 2,
    }
    assert stats["documents"] == {
        "input_tokens": 200, "output_tokens": 40, "total_tokens": 240,
        "calls": 2, "unknown_calls": 1,
    }
    today = next(item for item in stats["daily"] if item["date"] == "2026-10-06")
    assert today == {"date": "2026-10-06", "search_tokens": 120, "document_tokens": 240}


def test_all_period_groups_long_history_by_month_without_losing_tokens(usage_client):
    api, database = usage_client
    insert_usage(database, "2025-01-01T00:00:00+00:00")
    insert_usage(database, "2025-01-31T23:59:59+00:00", "DOCUMENT", (200, 40, 240))
    insert_usage(database, "2025-02-01T00:00:00+00:00", counts=(300, 60, 360))
    insert_usage(database, CURRENT.isoformat(), counts=(400, 80, 480))
    stats = api.get("/stats/ai-usage?period=all").json()
    assert stats["granularity"] == "month" and stats["total"]["total_tokens"] == 1200
    assert stats["daily"][0] == {"date": "2025-01-01", "search_tokens": 120, "document_tokens": 240}
    assert stats["daily"][1] == {"date": "2025-02-01", "search_tokens": 360, "document_tokens": 0}
    assert stats["daily"][-1] == {"date": "2026-10-01", "search_tokens": 480, "document_tokens": 0}
    assert sum(item["search_tokens"] + item["document_tokens"] for item in stats["daily"]) == 1200


@pytest.mark.parametrize("counts,expected,unknown_calls", [
    ((None, None, None), None, 1),
    ((0, 0, 0), 0, 0),
])
def test_unknown_usage_differs_from_known_zero(usage_client, counts, expected, unknown_calls):
    api, database = usage_client
    insert_usage(database, CURRENT.isoformat(), counts=counts)
    stats = api.get("/stats/ai-usage?period=7d").json()
    assert stats["search"] == stats["total"] == {
        "input_tokens": expected, "output_tokens": expected, "total_tokens": expected,
        "calls": 1, "unknown_calls": unknown_calls,
    }
    assert stats["documents"] == ZERO
    today = next(item for item in stats["daily"] if item["date"] == "2026-10-06")
    assert today["search_tokens"] == expected and today["document_tokens"] == 0


def test_usage_schema_and_history_survive_idempotent_initialization_and_restart(usage_client, tmp_path):
    api, database = usage_client
    application = api.post("/applications", json={
        "company": "Example", "position": "Stage IA", "url": "https://example.test/job",
    }).json()
    insert_usage(database, CURRENT.isoformat(), "DOCUMENT", (200, 40, 240))
    before = api.get("/stats/ai-usage?period=all").json()
    init_db(database)
    restarted = TestClient(create_app(database, tmp_path / "applications", repo_root=tmp_path))
    assert restarted.get("/stats/ai-usage?period=all").json() == before
    assert restarted.get(f"/applications/{application['id']}").status_code == 200
    with connect(database) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(ai_usage)")}
        assert columns == {
            "id", "created_at", "provider", "model", "category", "operation",
            "input_tokens", "output_tokens", "total_tokens",
        }
        assert db.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 1


class UsageCodex(FakeCodex):
    def __init__(self, response, usage=None):
        super().__init__(response)
        self.usage = usage or {}

    def run(self, *args, **kwargs):
        output = super().run(*args, **kwargs)
        output.run_metadata.update(self.usage)
        return output


def test_real_openai_provider_usage_reaches_search_ledger_and_statistics(tmp_path):
    search_output = SimpleNamespace(
        id="resp_usage", status="completed",
        output_text=json.dumps({"results": [{
            "title": "Stage IA", "company": "Example", "url": "https://jobs.example.test/offer-1",
            "snippet": "Mission ML", "source": "Career website",
        }]}),
        output=[{"type": "web_search_call", "action": {"type": "search", "query": "stage IA"}}],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120),
    )
    sdk = SimpleNamespace(
        models=Endpoint(SimpleNamespace(data=[SimpleNamespace(id="gpt-6-usage-test")])),
        responses=Endpoint(search_output),
    )
    provider = OpenAIProvider("controlled-key", client_factory=lambda **_: sdk)
    codex = UsageCodex(discovery(), {"input_tokens": 50, "output_tokens": 30, "total_tokens": 80})
    store = FakeKeyStore()
    store.set("openai", "controlled-key")
    database = tmp_path / "tracker.db"
    api = TestClient(create_app(
        database, tmp_path / "applications", FakeAuth(), lambda _: codex,
        api_key_store=store, openai_factory=lambda _: provider,
        offer_http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="Stage IA", request=request),
        )), offer_resolver=public_dns,
    ))
    assert api.put("/codex/model", json={"model": "test-model"}).status_code == 200
    assert api.put("/search/config", json={
        "mode": "AI_DIRECT", "searxng_url": "http://localhost:8080", "provider": "openai",
        "model": "gpt-6-usage-test", "effort": "medium", "confirm_api_billing": True,
    }).status_code == 200
    assert api.post("/codex/discover").status_code == 200
    assert len(sdk.responses.calls) == 2 and codex.calls == 1
    with connect(database) as db:
        rows = db.execute("SELECT * FROM ai_usage ORDER BY id").fetchall()
    assert len(rows) == 3
    assert all((row["provider"], row["model"], row["category"], row["operation"], row["total_tokens"])
               == ("openai", "gpt-6-usage-test", "SEARCH", "offer_search", 120) for row in rows[:2])
    assert (rows[-1]["provider"], rows[-1]["model"], rows[-1]["operation"], rows[-1]["total_tokens"]) == (
        "openai", "test-model", "offer_screening", 80,
    )
    stats = api.get("/stats/ai-usage?period=all").json()
    assert stats["total"] == stats["search"] == {
        "input_tokens": 250, "output_tokens": 70, "total_tokens": 320,
        "calls": 3, "unknown_calls": 0,
    }
    assert stats["documents"] == ZERO


@pytest.mark.parametrize("usage,expected", [
    ({"input_tokens": 200, "output_tokens": 40, "total_tokens": 240}, 240),
    ({"input_tokens": 200, "output_tokens": 40}, None),
    ({}, None),
])
def test_bundled_preparation_records_one_document_call(tmp_path, usage, expected):
    repo = isolated_repo(tmp_path, with_variants=True)
    codex = UsageCodex(preparation(), usage)
    database = tmp_path / "tracker.db"
    api = TestClient(create_app(
        database, tmp_path / "applications", FakeAuth(), lambda _: codex, repo_root=repo,
    ))
    assert api.put("/codex/model", json={"model": "test-model"}).status_code == 200
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    assert wait_for_preparation(api, application_id, "completed")["status"] == "PREPARED"
    with connect(database) as db:
        row = db.execute("SELECT * FROM ai_usage").fetchone()
        assert db.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 1
    assert (row["provider"], row["model"], row["category"], row["operation"], row["total_tokens"]) == (
        "openai", "test-model", "DOCUMENT", "application", expected,
    )
    stats = api.get("/stats/ai-usage?period=all").json()
    assert stats["documents"]["input_tokens"] == usage.get("input_tokens")
    assert stats["documents"]["output_tokens"] == usage.get("output_tokens")
    assert stats["documents"]["total_tokens"] == expected
    assert stats["documents"]["calls"] == 1
    assert stats["documents"]["unknown_calls"] == (1 if expected is None else 0)
    assert stats["search"] == ZERO


def test_incomplete_screening_retries_record_separate_calls_without_searxng_tokens(tmp_path):
    codex = UsageCodex({"decisions": []}, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    database = tmp_path / "tracker.db"
    api = TestClient(create_app(
        database, tmp_path / "applications", FakeAuth(), lambda _: codex,
        search_provider_factory=lambda _: FakeSearchProvider(),
        offer_http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="Stage IA", request=request),
        )), offer_resolver=public_dns,
    ))
    assert api.put("/codex/model", json={"model": "test-model"}).status_code == 200
    assert api.post("/codex/discover").status_code == 200
    assert codex.calls == 2
    with connect(database) as db:
        rows = db.execute("SELECT operation, total_tokens FROM ai_usage").fetchall()
    assert [tuple(row) for row in rows] == [("offer_screening", 15), ("offer_screening", 15)]
    stats = api.get("/stats/ai-usage?period=all").json()
    assert stats["total"]["total_tokens"] == 30 and stats["total"]["calls"] == 2


def test_usage_persists_when_preparation_output_fails_business_validation(tmp_path):
    codex = UsageCodex({}, {"input_tokens": 200, "output_tokens": 40, "total_tokens": 240})
    database = tmp_path / "tracker.db"
    api = TestClient(create_app(database, tmp_path / "applications", FakeAuth(), lambda _: codex))
    assert api.put("/codex/model", json={"model": "test-model"}).status_code == 200
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    assert wait_for_preparation(api, application_id, "failed")["status"] == "SHORTLISTED"
    stats = api.get("/stats/ai-usage?period=all").json()
    assert stats["documents"]["total_tokens"] == 240 and stats["documents"]["calls"] == 1


def test_failed_primary_and_successful_fallback_are_separate_usage_calls(tmp_path):
    primary = FakeApiProvider("deepseek_api", {}, False)
    fallback = UsageCodex(discovery(), {"input_tokens": 50, "output_tokens": 30, "total_tokens": 80})
    store = FakeKeyStore()
    store.set("deepseek_api", "controlled-key")
    database = tmp_path / "tracker.db"
    api = TestClient(create_app(
        database, tmp_path / "applications", FakeAuth(), lambda _: fallback,
        api_key_store=store, provider_factories={"deepseek_api": lambda _: primary},
        search_provider_factory=lambda _: FakeSearchProvider(),
        offer_http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="Stage IA", request=request),
        )), offer_resolver=public_dns,
    ))
    assert api.put("/codex/model", json={"model": "test-model"}).status_code == 200
    overrides = {stage: {"provider": "default", "effort": "medium"} for stage in (
        "screening", "deep_analysis", "company_analysis", "application_preparation",
    )}
    overrides["screening"] = {"provider": "deepseek_api", "model": "deepseek_api-test", "effort": "medium"}
    assert api.put("/ai/pipeline", json={
        "overrides": overrides, "ai_fallback": "chatgpt_oauth", "confirm_api_billing": True,
    }).status_code == 200
    primary.error = FakeProviderError(429)
    assert api.post("/codex/discover").status_code == 200
    with connect(database) as db:
        rows = db.execute("SELECT provider, operation, total_tokens FROM ai_usage ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [
        ("deepseek_api", "offer_screening", None), ("openai", "offer_screening", 80),
    ]
    stats = api.get("/stats/ai-usage?period=all").json()
    assert stats["search"]["total_tokens"] == 80
    assert stats["search"]["calls"] == 2 and stats["search"]["unknown_calls"] == 1
