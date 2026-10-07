from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient

from app.chatgpt_auth import ChatGPTAuth
from app.codex_client import CodexError, CodexOutput
from app.codex_workflows import (
    CompanyProfile, DiscoveryOutput, PreparationOutput, validate_local_sources,
)
from app.company_profiles import valid_postal_address
from app.cv_documents import create_cv, cv_project_order, cv_variants, document_text, project_catalog
from app.cover_letters import (
    CoverLetterContent, CoverLetterError, candidate_contact, create_cover_letter_docx,
    is_subject, read_cover_letter_docx, validate_cover_letter_content,
    validate_cover_letter_docx,
)
from app.main import (
    CV_ROOT, add_event, classify_search_result, clean_artifact_text, connect, create_app,
    parse_discovery_results, screen_candidate_batches, select_search_candidates,
    offer_freshness_reason, supported_reasoning_efforts, verify_discovery_results,
)
from app.offer_verification import OfferVerificationResult
from app.search_providers import SearchProviderUnavailableError, SearchResult


class FakeAuth:
    def __init__(self, connected=True, callback_error=None):
        self.connected, self.callback_error = connected, callback_error

    def status(self): return {"connected": self.connected, "account": "user@example.test" if self.connected else None}
    def start(self, redirect_uri): return f"https://auth.openai.test/?redirect_uri={redirect_uri}"
    def callback(self, *_):
        if self.callback_error: raise self.callback_error
        self.connected = True
    def access_token(self):
        if not self.connected: raise PermissionError("ChatGPT is not connected")
        return "backend-secret"
    def logout(self): self.connected = False


class FakeKeyStore:
    def __init__(self): self.values = {}
    def get(self, provider): return self.values.get(provider)
    def set(self, provider, value): self.values[provider] = value
    def delete(self, provider): self.values.pop(provider, None)


class FakeOpenAI:
    def __init__(self, response=None, error=None): self.response, self.error, self.tests, self.runs = response, error, 0, []
    def models(self):
        if self.error: raise self.error
        return [{"model": "gpt-5-test", "displayName": "GPT-5 Test", "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "medium", "high")]}]
    def test(self):
        self.tests += 1
        if self.error: raise self.error
    def run(self, *args, **kwargs):
        self.runs.append((args, kwargs))
        if self.error: raise self.error
        return CodexOutput(self.response, {
            "web_search_calls": 1 if kwargs.get("web_search") else 0,
            "web_domains": ["jobs.example.test"] if kwargs.get("web_search") else [],
        })


class FakeApiProvider(FakeOpenAI):
    def __init__(self, provider_id, response, web_search):
        super().__init__(response)
        self.provider_id, self.web_search = provider_id, web_search

    def models(self):
        return [{
            "model": f"{self.provider_id}-test", "displayName": f"{self.provider_id} Test",
            "reasoningEfforts": ["medium"],
            "capabilities": {
                "structured_output": True, "web_search": self.web_search,
                "reasoning_effort": False, "streaming": True, "tool_calling": True,
            },
        }]


class FakeProviderError(Exception):
    def __init__(self, status_code): self.status_code = status_code


class FakeSearchProvider:
    def __init__(self, results=None, error=None):
        self.results = results or [SearchResult("Stage IA", "https://jobs.example.test/offer-1", "Stage IA", "test", "stage IA")]
        self.error = error
        self.queries = []
    def test(self):
        if self.error: raise self.error
    def search(self, query, limit=20):
        self.queries.append(query)
        if self.error: raise self.error
        return self.results[:limit]


def search_result(title: str, url: str, snippet: str = "") -> SearchResult:
    return SearchResult(title, url, snippet, "test", "stage IA")


def test_search_result_classification_is_conservative():
    assert classify_search_result(search_result(
        "Stage Machine Learning", "https://company.test/jobs/stage-machine-learning-12345",
    )) == "likely_job_detail"
    assert classify_search_result(search_result(
        "Master Intelligence Artificielle", "https://university.test/formation/master-ia",
    )) == "obvious_non_job"
    assert classify_search_result(search_result(
        "Opportunités", "https://company.test/carrieres",
    )) == "unknown"
    assert classify_search_result(search_result(
        "Stages Machine Learning", "https://linkedin.test/jobs/machine-learning-internship-emplois-paris",
    )) == "unknown"


def test_candidates_rank_jobs_before_unknown_without_dropping_admissible_results():
    known = search_result("Stage connu", "https://company.test/jobs/stage-known-99999")
    unknown = [search_result("Carrières", f"https://company.test/carrieres/{index}") for index in range(31)]
    likely = search_result("Stage IA", "https://company.test/jobs/stage-ia-12345")
    non_job = search_result("Master IA", "https://university.test/formation/master-ia")
    candidates, stats = select_search_candidates([known, *unknown, likely, non_job], {known.url})
    assert len(candidates) == 32 and candidates[0] == likely and known not in candidates and non_job not in candidates
    assert stats == {
        "likely_job_detail_count": 1, "unknown_candidate_count": 31, "obvious_non_job_count": 1,
        "known_url_count": 1, "unknown_url_count": 33, "candidate_count": 32,
        "candidate_limit_dropped": 0,
    }


def screening_candidates(count):
    return [{"candidate_id": f"candidate_{index:03d}", "url": f"https://jobs.example.test/{index}"}
            for index in range(1, count + 1)]


def parsed_batch(batch, decisions):
    return parse_discovery_results(
        {"decisions": decisions}, {item["candidate_id"] for item in batch},
    )


def decisions_for(batch):
    return [screening_decision(item["candidate_id"], found_offer(item["url"])) for item in batch]


def test_screening_normal_batch_has_no_retry():
    calls = []
    candidates = screening_candidates(30)
    decisions, metrics, _ = screen_candidate_batches(
        candidates, lambda batch, number, retry: (calls.append((len(batch), retry)) or parsed_batch(batch, decisions_for(batch))),
    )
    assert calls == [(30, False)]
    assert len(decisions) == 30 and metrics["ai_retry_count"] == metrics["ai_missing_decision_count"] == 0


def test_screening_processes_all_candidates_in_multiple_batches():
    calls = []
    candidates = screening_candidates(65)
    decisions, metrics, _ = screen_candidate_batches(
        candidates, lambda batch, number, retry: (calls.append(len(batch)) or parsed_batch(batch, decisions_for(batch))),
    )
    assert calls == [30, 30, 5]
    assert len(decisions) == metrics["ai_decision_count"] == 65 and metrics["ai_batch_count"] == 3


@pytest.mark.parametrize("missing_count", [1, 3])
def test_screening_retries_only_missing_candidates(missing_count):
    calls = []
    candidates = screening_candidates(30)

    def request(batch, number, retry):
        calls.append([item["candidate_id"] for item in batch])
        returned = decisions_for(batch if retry else batch[:-missing_count])
        return parsed_batch(batch, returned)

    decisions, metrics, _ = screen_candidate_batches(candidates, request)
    assert len(calls) == 2 and calls[1] == [item["candidate_id"] for item in candidates[-missing_count:]]
    assert len(decisions) == 30 and metrics["ai_retry_count"] == 1
    assert metrics["ai_missing_decision_count"] == missing_count


def test_screening_uses_review_fallback_after_one_incomplete_retry():
    calls = []
    candidates = screening_candidates(2)

    def request(batch, number, retry):
        calls.append(batch)
        return parsed_batch(batch, decisions_for(batch[:-1]))

    decisions, metrics, _ = screen_candidate_batches(candidates, request)
    assert len(calls) == 2
    assert decisions[-1]["decision"] == "REVIEW" and decisions[-1]["reason_code"] == "AI_NO_DECISION"
    assert metrics["ai_decision_count"] == 2


def test_duplicate_and_unknown_candidate_ids_do_not_count_as_coverage():
    batch = screening_candidates(2)
    first = screening_decision("candidate_001", found_offer(batch[0]["url"]))
    unknown = screening_decision("candidate_999", found_offer())
    decisions, diagnostics = parsed_batch(batch, [first, first, unknown])
    assert decisions == []
    assert diagnostics["missing_candidate_ids"] == ["candidate_001", "candidate_002"]
    assert diagnostics["duplicate_candidate_ids"] == ["candidate_001"]
    assert {item["reason"] for item in diagnostics["rejection_reasons"]} == {
        "DUPLICATE_CANDIDATE_ID", "UNKNOWN_CANDIDATE_ID",
    }


def test_compact_reject_needs_no_offer_and_keep_embeds_offer():
    reject = screening_decision(
        offer=None, decision="REJECT", reason_code="NOT_INTERNSHIP",
        eligibility_status="ELIGIBILITY_UNCERTAIN",
    )
    keep = screening_decision(offer=found_offer(), eligibility_status="ELIGIBILITY_UNCERTAIN")
    parsed = DiscoveryOutput.model_validate({"decisions": [reject, keep]}).decisions
    assert parsed[0].offer is None and parsed[0].reason is None
    assert parsed[1].offer.title == "Stage IA" and parsed[1].eligibility_status == "ELIGIBILITY_UNCERTAIN"


class FakeCodex:
    def __init__(self, response=None, error=None): self.response, self.error, self.calls, self.runs = response, error, 0, []
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def models(self):
        if self.error: raise self.error
        return [
            {"model": "test-model", "displayName": "Test Model", "isDefault": True,
             "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "medium", "high", "xhigh")]},
            {"model": "gpt-5.6-sol", "displayName": "GPT-5.6-Sol", "isDefault": False,
             "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "medium", "high", "xhigh", "max")]},
        ]
    def run(self, *args, **kwargs):
        self.calls += 1
        self.runs.append((args, kwargs))
        if self.error: raise self.error
        return CodexOutput(self.response, {
            "web_search_calls": 1 if kwargs.get("web_search") else 0,
            "web_domains": ["jobs.example.test"] if kwargs.get("web_search") else [],
        })


class ControlledCodex(FakeCodex):
    def __init__(self, response, fail=None):
        super().__init__(response)
        self.fail = set(fail or [])
        self.releases = {name: threading.Event() for name in ("A", "B", "C")}
        self.condition = threading.Condition()
        self.started = []
        self.active = 0
        self.max_active = 0

    def run(self, prompt, *args, **kwargs):
        name = next(name for name in self.releases if f'"position": "{name}"' in prompt)
        with self.condition:
            self.calls += 1
            self.runs.append(((prompt, *args), kwargs))
            self.started.append(name)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.condition.notify_all()
        self.releases[name].wait(5)
        with self.condition:
            self.active -= 1
        if name in self.fail:
            raise CodexError(f"failure {name}")
        return self.response

    def wait_started(self, name, timeout=15):
        deadline = time.monotonic() + timeout
        with self.condition:
            while name not in self.started:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"Préparation {name} non démarrée")
                self.condition.wait(remaining)


def found_offer(url="https://jobs.example.test/offer-1"):
    return {
        "company": "Example", "title": "Stage IA", "url": url, "location": "Paris",
        "contract_type": "Stage", "published_at": date.today().isoformat(), "start_date": "2027-02",
        "availability": "open", "source": "Career website", "why_relevant": ["Optimisation"],
        "evidence_urls": [url],
    }


_DEFAULT_OFFER = object()


def screening_decision(candidate_id="candidate_001", offer=_DEFAULT_OFFER, **values):
    offer = found_offer() if offer is _DEFAULT_OFFER else offer
    return {
        "candidate_id": candidate_id, "decision": "KEEP", "reason_code": "KEEP_RELEVANT",
        "eligibility_status": "ELIGIBLE", "reason": None, "offer": offer, **values,
    }


def discovery(url="https://jobs.example.test/offer-1"):
    return {"decisions": [screening_decision(offer=found_offer(url))]}


def cv_content(project_ids=None, generic=False):
    project_ids = project_ids if project_ids is not None else ["search-prototype", "document-workflow", "model-experiment"]
    titles = {
        "search-prototype": "Search Prototype", "document-workflow": "Document Workflow",
        "model-experiment": "Model Experiment", "data-pipeline": "Data Pipeline", "planning-prototype": "Planning Prototype",
        "image-classifier": "Image Classifier",
    }
    return {
        "headline": "Intelligence artificielle et décision" if generic else "Recherche de stage de fin d'études - Intelligence artificielle et décision",
        "profile": "Étudiant en Master 2 Computer Science avec des projets documentés." if generic else "Étudiant en Master 2 Computer Science à Example University, avec des projets documentés en algorithmique, machine learning et systèmes LLM.",
        "skill_groups": [
            {"title": "Systèmes LLM", "items": ["RAG", "Retrieval hybride", "MCP", "Tool use", "LLM locaux"]},
            {"title": "Planification et décision", "items": ["Heuristiques", "Décision sous contraintes", "Allocation", "Ordonnancement", "Pathfinding"]},
            {"title": "Machine learning", "items": ["scikit-learn", "Feature engineering", "Classification", "Évaluation"]},
            {"title": "Développement", "items": ["Python", "Git", "Pandas", "Pytest"]},
        ],
        "projects": [{
            "project_id": project_id, "title": titles[project_id], "technologies": "Python, tests",
            "description": "Projet technique documenté dans la Knowledge Base.",
            "bullets": ["Conception appuyée par la Knowledge Base.", "Validation technique documentée."],
        } for project_id in project_ids],
    }


def preparation(action="REUSE", source_path=None, sources=None, content="default", project_ids=None):
    project_ids = project_ids if project_ids is not None else (
        ["search-prototype", "planning-prototype", "image-classifier"]
        if action == "CREATE" else ["search-prototype", "document-workflow", "model-experiment"]
    )
    generated_content = cv_content(project_ids) if content == "default" and action != "REUSE" else None if content == "default" else content
    return {
        "analysis_markdown": "# Analyse\n\nAdéquation démontrée par les éléments disponibles.",
        "company_profile": {
            "description": "Example développe des solutions techniques pour ses clients. Cette offre concerne le domaine de l’intelligence artificielle appliquée.",
            "postal_address": "10 rue du Test\n75001 Paris",
            "relevant_domain": "Intelligence artificielle appliquée",
            "completed_achievements": [],
            "planned_developments": ["Développer le prototype décrit par l’offre."],
            "competitors_or_comparable_actors": [],
            "sources": ["https://example.test/job"],
        },
        "interview_prep_markdown": """# Interview preparation — Example

## Position
Stage IA

## Company overview
Example développe des solutions techniques.

## Role domain
Intelligence artificielle appliquée.

## Main responsibilities
- Développer un prototype.

## Profile match
- Expérimentation et évaluation.

## Projects to highlight
- Search Prototype.

## Technical topics to review
- Évaluation de modèles.

## Likely technical questions
- Comment évaluer le prototype ?

## Likely HR / motivation questions
- Pourquoi ce stage ?

## Questions for the company
- Quels critères de succès ?

## Company-specific topics
- Contexte à confirmer avec l'équipe.

## Actual interview

No interview scheduled yet.
""",
        "cover_letter": {
            "language": "fr",
            "salutation": "Madame, Monsieur,",
            "closing": "Cordialement,",
            "paragraph_1": "Étudiant en Master 2 Computer Science, je candidate au stage IA proposé par Example.",
            "paragraph_2": "Mon parcours couvre intelligence artificielle, algorithmique et décision.",
            "paragraph_3": "Mes projets démontrent préparation de données, expérimentation et évaluation de modèles.",
            "paragraph_4": "Ces acquis correspondent aux missions du stage et je souhaite approfondir leur application.",
            "paragraph_5": "Je serais disponible pour échanger sur ma candidature lors d’un entretien.",
        },
        "local_sources": sources or ["01_Career/Profil.md"],
        "cv": {"action": action, "source_variant": None if action == "CREATE" else "A", "source_path": source_path,
               "target_filename": "CV_Example_Stage_IA.docx" if action != "REUSE" else None,
               "justification": "Choix fondé sur les preuves locales.", "changes": [], "missing_fit": [],
               "project_set": project_ids, "project_order": project_ids,
               "content": generated_content,
               "reusable_content": cv_content(project_ids, generic=True) if action == "CREATE" else None},
    }


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_valid_docx(path):
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        xml = archive.read("word/document.xml")
    assert xml.strip() and b"Search Prototype" in xml


def assert_no_source_leak(text):
    for marker in ("Source locale", "01_Career/", "02_Projects/", "03_Knowledge/"):
        assert marker not in text


def public_dns(host, port, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def offer_page(published_at=None, **metadata):
    posting = {"@type": "JobPosting", "datePosted": published_at or date.today().isoformat(), **metadata}
    return '<script type="application/ld+json">' + json.dumps(posting) + '</script><h1>Stage IA Paris. Optimisation et décision.</h1>'


@pytest.mark.parametrize("max_age_days,age,accepted", [
    (90, 0, True), (90, 30, True), (90, 89, True), (90, 90, True),
    (90, 91, False), (90, 365, False),
    (30, 29, True), (30, 30, True), (30, 31, False),
    (180, 179, True), (180, 180, True), (180, 181, False),
])
def test_offer_freshness_inclusive_calendar_day_boundary(max_age_days, age, accepted):
    today = date(2026, 10, 6)
    published = (today - timedelta(days=age)).isoformat()
    assert (offer_freshness_reason(published, max_age_days, today=today) is None) is accepted


@pytest.mark.parametrize("published_at,reason", [
    (None, "PUBLICATION_DATE_UNKNOWN"), ("", "PUBLICATION_DATE_UNKNOWN"),
    ("invalid", "PUBLICATION_DATE_INVALID"), ("2026-02-30", "PUBLICATION_DATE_INVALID"),
    ("2026-10-07", "PUBLICATION_DATE_IN_FUTURE"),
])
def test_offer_freshness_rejects_unreliable_dates(published_at, reason):
    assert offer_freshness_reason(published_at, 90, today=date(2026, 10, 6)) == reason


def client(tmp_path, response=None, auth=None, error=None, repo_root=None, offer_handler=None):
    fake = FakeCodex(response)
    offers = [item.get("offer") for item in (response or {}).get("decisions", []) if item.get("offer")]
    search_results = [
        SearchResult(item["title"], item["url"], item["title"], "test", "stage IA") for item in offers
    ] or None
    offer_handler = offer_handler or (lambda request: httpx.Response(200, text=offer_page(), request=request))
    app = create_app(
        tmp_path / "tracker.db", tmp_path / "applications", auth or FakeAuth(), lambda _: fake,
        repo_root=repo_root or CV_ROOT.parents[2],
        offer_http_client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(offer_handler), follow_redirects=True,
        ),
        offer_resolver=public_dns,
        search_provider_factory=lambda _: FakeSearchProvider(search_results),
    )
    app.state.fake_codex = fake
    result = TestClient(app)
    result.put("/codex/model", json={"model": "test-model"})
    fake.error = error
    return result, app


def test_offer_age_setting_defaults_validates_and_survives_restart(tmp_path):
    api, _ = client(tmp_path)
    assert api.get("/search/config").json()["max_offer_age_days"] == 90
    saved = api.patch("/search/config", json={"max_offer_age_days": 60})
    assert saved.status_code == 200 and saved.json()["max_offer_age_days"] == 60
    for invalid in (0, 366, -1, 1.5, "60", True, None):
        result = api.patch("/search/config", json={"max_offer_age_days": invalid})
        assert result.status_code == 422
        assert api.get("/search/config").json()["max_offer_age_days"] == 60
    assert api.patch("/search/config", json={"max_offer_age_days": 45, "mode": "AI_DIRECT"}).status_code == 422
    # Saving an older provider form must not overwrite the newer preference.
    assert api.put("/search/config", json={"mode": "SEARXNG", "max_offer_age_days": 90}).status_code == 200
    reloaded, _ = client(tmp_path)
    assert reloaded.get("/search/config").json()["max_offer_age_days"] == 60
    with connect(reloaded.app.state.db_path) as db:
        assert db.execute("SELECT value FROM settings WHERE key='max_offer_age_days'").fetchone()[0] == "60"


@pytest.mark.parametrize("mode,max_age_days,age,accepted", [
    ("SEARXNG", 90, 91, False), ("AI_DIRECT", 90, 91, False),
    ("SEARXNG", 30, 45, False), ("AI_DIRECT", 30, 45, False),
    ("SEARXNG", 60, 45, True), ("AI_DIRECT", 60, 45, True),
    ("SEARXNG", 90, 90, True), ("AI_DIRECT", 90, 90, True),
])
def test_discovery_uses_persisted_age_for_both_retrieval_modes(tmp_path, monkeypatch, mode, max_age_days, age, accepted):
    published = (date.today() - timedelta(days=age)).isoformat()
    handler = lambda request: httpx.Response(200, text=offer_page(published), request=request)
    if mode == "AI_DIRECT":
        api, app, _search, _codex = ai_direct_client(tmp_path, monkeypatch, "chatgpt_oauth", [
            {"url": "https://jobs.example.test/jobs/stage-ia-12345", "title": "Stage IA", "source": "Career website"},
        ], {"web_search_calls": 1})
        app.state.offer_http_client_factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    else:
        api, app = client(tmp_path, discovery(), offer_handler=handler)
    assert api.patch("/search/config", json={"max_offer_age_days": max_age_days}).status_code == 200
    result = api.post("/codex/discover")
    assert result.status_code == 200
    assert result.json()["new"] == int(accepted)
    audit = api.get(f"/codex/discovery/{result.json()['run_id']}/candidates").json()[0]
    assert audit["final_action"] == ("INSERTED" if accepted else "FRESHNESS_REJECTED")
    if accepted:
        assert api.get("/applications").json()[0]["published_at"] == published
    else:
        assert result.json()["errors"] == [{"url": audit["canonical_url"], "reason": "OFFER_TOO_OLD"}]
        assert result.json()["rejected"] == 1
        assert api.get("/codex/discovery/latest").json()["rejected_count"] == 1


@pytest.mark.parametrize("page,reason", [
    ("<h1>Stage IA</h1>", "PUBLICATION_DATE_UNKNOWN"),
    ('<script type="application/ld+json">{"@type":"JobPosting","datePosted":"invalid"}</script>', "PUBLICATION_DATE_UNKNOWN"),
    ('<meta itemprop="dateModified" content="2026-10-06">', "PUBLICATION_DATE_UNKNOWN"),
])
def test_discovery_does_not_trust_ai_publication_without_source_date(tmp_path, page, reason):
    api, _ = client(tmp_path, discovery(), offer_handler=lambda request: httpx.Response(200, text=page, request=request))
    result = api.post("/codex/discover").json()
    assert result["new"] == 0
    assert result["errors"][0]["reason"] == reason


def test_discovery_rejects_old_publication_despite_recent_modification(tmp_path):
    old = (date.today() - timedelta(days=365)).isoformat()
    api, _ = client(tmp_path, discovery(), offer_handler=lambda request: httpx.Response(
        200, text=offer_page(old, dateModified=date.today().isoformat()), request=request,
    ))
    result = api.post("/codex/discover").json()
    assert result["new"] == 0 and result["errors"][0]["reason"] == "OFFER_TOO_OLD"


@pytest.mark.parametrize("provider", ["generic", "ashby"])
def test_verification_cannot_renew_old_source_publication(provider):
    item = found_offer()
    item["published_at"] = (date.today() - timedelta(days=365)).isoformat()

    class Verifier:
        def verify(self, url, _metadata):
            return OfferVerificationResult("OPEN", "HTTP_AVAILABLE", provider, url, url,
                                           published_at=date.today().isoformat())

    diagnostics = {"rejection_reasons": []}
    assert verify_discovery_results([item], diagnostics, Verifier(), 90) == []
    assert diagnostics["rejection_reasons"][0]["reason"] == "OFFER_TOO_OLD"


def test_verification_cannot_renew_certainly_expired_ambiguous_source_deadline():
    item = found_offer()
    item["deadline"] = f"05/12/{date.today().year - 1}"

    class Verifier:
        def verify(self, url, _metadata):
            return OfferVerificationResult("OPEN", "HTTP_AVAILABLE", "generic", url, url,
                                           deadline=(date.today() + timedelta(days=365)).isoformat())

    diagnostics = {"rejection_reasons": []}
    assert verify_discovery_results([item], diagnostics, Verifier(), 90) == []
    assert diagnostics["rejection_reasons"][0]["reason"] == "DEADLINE_EXPIRED"


def test_discovery_keeps_source_closure_when_second_fetch_is_blocked(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=offer_page() + "<p>Offre expirée</p>", request=request) if calls == 1 else httpx.Response(403, request=request)

    api, _ = client(tmp_path, discovery(), offer_handler=handler)
    result = api.post("/codex/discover").json()
    assert result["new"] == 0 and result["verified_closed"] == 1
    assert result["errors"][0]["reason"] == "OFFER_CLOSED"


@pytest.mark.parametrize("closed_by", ["deadline", "content", "availability"])
def test_recent_offer_expiration_remains_distinct_from_freshness(tmp_path, closed_by):
    metadata = {"validThrough": (date.today() - timedelta(days=1)).isoformat()} if closed_by == "deadline" else {}
    page = offer_page(**metadata) + ("<p>Candidatures closes</p>" if closed_by == "content" else "")
    output = discovery()
    if closed_by == "availability":
        output["decisions"][0]["offer"]["availability"] = "closed"
    api, _ = client(tmp_path, output, offer_handler=lambda request: httpx.Response(200, text=page, request=request))
    result = api.post("/codex/discover").json()
    assert result["new"] == 0 and result["verified_closed"] == 1
    assert result["errors"][0]["reason"] in {"DEADLINE_EXPIRED", "CONTENT_CLOSED", "OFFER_CLOSED"}


def test_offer_age_preference_leaves_existing_applications_untouched(tmp_path):
    api, app = client(tmp_path)
    historical = api.post("/applications", json={
        "company": "Existing", "position": "Stage IA", "url": "https://jobs.example.test/historical",
    }).json()
    with connect(app.state.db_path) as db:
        db.execute("UPDATE applications SET published_at='2025-01-01' WHERE id=?", (historical["id"],))
        db.commit()
    before = api.get("/applications").json()
    assert api.patch("/search/config", json={"max_offer_age_days": 30}).status_code == 200
    assert api.get("/applications").json() == before


def test_smartrecruiters_import_uses_same_age_and_expiration_rules(tmp_path, monkeypatch):
    api, _ = client(tmp_path)
    today = date.today()
    jobs = [{
        "company": "Example", "position": "Stage IA", "url": f"https://jobs.example.test/{age}",
        "published_at": (today - timedelta(days=age)).isoformat(), "availability": "open",
    } for age in (45, 91)]
    jobs.append({**jobs[0], "url": "https://jobs.example.test/closed", "availability": "closed"})
    monkeypatch.setattr("app.main.discover_smartrecruiters", lambda *_args: jobs)
    assert api.patch("/search/config", json={"max_offer_age_days": 60}).status_code == 200
    result = api.post("/discover?import_results=true", json={"company": "Example"})
    assert result.status_code == 200 and result.json()["created"] == 1
    assert [job["url"] for job in result.json()["jobs"]] == [jobs[0]["url"]]


def isolated_repo(tmp_path, with_variants=False):
    repo = tmp_path / "repo"
    for name in ("Profil.md", "Domaines.md", "Stage M2.md"):
        destination = repo / "01_Career" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CV_ROOT.parents[1] / name, destination)
    template = repo / "01_Career/CV/Templates/resume-template.docx"
    template.parent.mkdir(parents=True)
    shutil.copy2(CV_ROOT.parent / "Templates/resume-template.docx", template)
    letter = repo / "01_Career/Cover Letter/Templates/lettre-motivation-template.docx"
    letter.parent.mkdir(parents=True)
    shutil.copy2(CV_ROOT.parents[1] / "Cover Letter/Templates/lettre-motivation-template.docx", letter)
    generated = repo / "01_Career/CV/Generated"
    generated.mkdir(parents=True)
    manifest = repo / "01_Career/CV/CV_Variants.md"
    manifest.write_text("# CV Variants\n", encoding="utf-8")
    for title in ("Search Prototype", "Document Workflow", "Model Experiment", "Planning Prototype", "Image Classifier"):
        project = repo / "02_Projects" / title / f"{title}.md"
        project.parent.mkdir(parents=True)
        project.write_text(f"# {title}\n\nProjet documenté.\n", encoding="utf-8")
    if with_variants:
        from app.codex_workflows import CvContent
        create_cv(template, generated / "resume_A.docx", CvContent.model_validate(cv_content()))
        manifest.write_text("""# CV Variants

## Variante A — Agentic AI

Projets :

1. Search Prototype
2. Document Workflow
3. Model Experiment
""", encoding="utf-8")
    return repo


def shortlist(api):
    created = api.post("/applications", json={"company": "Example", "position": "Stage IA", "url": "https://example.test/job"}).json()
    api.post(f"/applications/{created['id']}/status", json={"status": "SHORTLISTED"})
    return created["id"]


def wait_for_preparation(api, application_id, state, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application = api.get(f"/applications/{application_id}").json()
        if application["preparation_state"] == state:
            return application
        if state == "completed" and application["preparation_state"] == "failed":
            raise AssertionError(application["preparation_error"])
        time.sleep(0.01)
    raise AssertionError(f"État {state} non atteint pour candidature {application_id}")


def test_cover_letter_requires_five_personalized_reasonable_paragraphs(tmp_path):
    payload = preparation()
    path = tmp_path / "lettre-motivation.docx"
    create_cover_letter_docx(
        CV_ROOT.parents[2], path, "Example", "Stage IA", "10 rue du Test\n75001 Paris",
        [payload["cover_letter"][f"paragraph_{index}"] for index in range(1, 6)],
        "fr", date(2026, 10, 4),
    )
    content = read_cover_letter_docx(path)
    text = document_text(path)
    assert len(content.paragraphs) == 5
    assert validate_cover_letter_docx(path, "Example", "Alex Example") < 420
    assert "Alex Example" in text and "alex@example.test" in text and "01 23 45 67 89" in text
    assert "10 rue du Test" in text and "75001 Paris" in text and "Fait à Example City, le 04/10/2026" in text
    assert is_subject(content.subject)
    assert content.subject.endswith("Candidature - Stage IA")
    with pytest.raises(ValueError, match="exactly five"):
        validate_cover_letter_content(CoverLetterContent(content.subject, content.salutation, content.paragraphs[:4], content.closing, content.signature_name), "Example", "Stage IA")
    with pytest.raises(ValueError, match="placeholder"):
        validate_cover_letter_content(CoverLetterContent(content.subject, content.salutation, (*content.paragraphs[:4], "À compléter"), content.closing, content.signature_name), "Example", "Stage IA")
    validate_cover_letter_content(CoverLetterContent(content.subject, content.salutation, tuple(value.replace("Example", "Entreprise") for value in content.paragraphs), content.closing, content.signature_name), "Example", "Stage IA")


def test_candidate_contact_uses_career_sources_without_requiring_company_address(tmp_path):
    contact = candidate_contact(CV_ROOT.parents[2])
    assert contact.name == "Alex Example" and contact.location == "Example City"
    path = tmp_path / "letter.docx"
    create_cover_letter_docx(
        CV_ROOT.parents[2], path, "Example", "Stage IA - F/H", None,
        [preparation()["cover_letter"][f"paragraph_{index}"] for index in range(1, 6)],
        "fr", date(2026, 10, 4),
    )
    assert validate_cover_letter_docx(path, "Example", contact.name, "Stage IA - F/H") > 0


def test_company_profile_rejects_location_and_separates_completed_from_planned():
    payload = preparation()["company_profile"]
    assert CompanyProfile.model_validate(payload).relevant_domain == "Intelligence artificielle appliquée"
    assert valid_postal_address(payload["postal_address"])
    assert not valid_postal_address("Paris-La Défense")
    assert not valid_postal_address("Paris\n75001 Paris")
    with pytest.raises(ValueError, match="complete postal address"):
        CompanyProfile.model_validate({**payload, "postal_address": "Paris"})
    with pytest.raises(ValueError, match="completed achievement"):
        CompanyProfile.model_validate({
            **payload,
            "completed_achievements": ["Prototype lancé."],
            "planned_developments": ["Prototype lancé."],
        })
    assert CompanyProfile.model_validate({
        **payload,
        "competitors_or_comparable_actors": [],
        "completed_achievements": [],
    }).competitors_or_comparable_actors == []


def test_candidate_contact_uses_structured_profile_fields(tmp_path):
    profile = tmp_path / "01_Career" / "Profil.md"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        """**Nom** : Alex Example
**Adresse** : 10 rue du Test
**Code postal** : 12345
**Ville** : Example City
**Email** : alex@example.test
**Téléphone** : 07 00 00 00 00
**Localisation** : Example City
""",
        encoding="utf-8",
    )
    contact = candidate_contact(tmp_path)
    assert contact.postal_address == "10 rue du Test"
    assert contact.postal_code == "12345"
    assert contact.city == "Example City"


def test_manual_company_address_overrides_future_preparation(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    api, _ = client(tmp_path, preparation("REUSE"), repo_root=repo)
    application_id = shortlist(api)
    manual = "99 rue Manuelle\n75002 Paris"
    patched = api.patch(f"/applications/{application_id}", json={"company_postal_address": manual})
    assert patched.status_code == 200
    assert patched.json()["company_address_overridden"] == 1
    assert api.patch(f"/applications/{application_id}", json={"company_postal_address": "Paris"}).status_code == 422
    api.post(f"/applications/{application_id}/prepare-with-codex")
    prepared = wait_for_preparation(api, application_id, "completed")
    assert prepared["company_postal_address"] == manual
    folder = next((tmp_path / "applications").iterdir())
    assert manual in (folder / "company.md").read_text(encoding="utf-8")
    assert "99 rue Manuelle" in document_text(folder / "lettre-motivation.docx")


def test_auth_connection_success(tmp_path):
    auth = FakeAuth(False)
    api, _ = client(tmp_path, auth=auth)
    assert api.get("/auth/chatgpt/start").json()["authorization_url"].startswith("https://auth.openai.test/")
    assert api.get("/auth/chatgpt/callback?code=x&state=y&client_id=z").status_code == 200
    assert api.get("/auth/chatgpt/status").json()["connected"] is True


def test_auth_plan_consent_refused(tmp_path):
    api, _ = client(tmp_path, auth=FakeAuth(False, PermissionError("Consentement refusé")))
    assert api.get("/auth/chatgpt/callback?code=x&state=y").status_code == 400


def test_expired_token_refresh(tmp_path):
    def handler(request):
        assert b"refresh_token=refresh" in request.content
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
    auth = ChatGPTAuth(tmp_path, httpx.Client(transport=httpx.MockTransport(handler)))
    auth._write("credentials.json", {"access_token": "old", "refresh_token": "refresh", "client_id": "client",
                                      "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
    assert auth.access_token() == "new"


def test_discovery_uses_searxng_then_codex_without_ai_web_search(tmp_path):
    auth = ChatGPTAuth(tmp_path / "auth", httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"access_token": "new-oauth", "expires_in": 3600}),
    )))
    auth._write("credentials.json", {
        "access_token": "old-oauth", "refresh_token": "refresh", "client_id": "client",
        "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    })
    codex = FakeCodex(discovery())
    app = create_app(
        tmp_path / "tracker.db", tmp_path / "applications", auth, lambda _: codex,
        search_provider_factory=lambda _: FakeSearchProvider(),
    )
    api = TestClient(app)
    assert api.put("/codex/model", json={"model": "test-model"}).status_code == 200
    assert api.post("/codex/discover").status_code == 200
    assert codex.calls == 1
    assert codex.runs[-1][1]["web_search"] is False


def test_revoked_token(tmp_path):
    auth = ChatGPTAuth(tmp_path, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400))))
    auth._write("credentials.json", {"access_token": "old", "refresh_token": "refresh", "client_id": "client",
                                      "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
    with pytest.raises(PermissionError): auth.access_token()


def test_no_credential_exposed_to_frontend(tmp_path):
    api, _ = client(tmp_path)
    payload = json.dumps(api.get("/auth/chatgpt/status").json())
    assert "backend-secret" not in payload and "refresh_token" not in payload


def test_default_search_and_pipeline_configuration(tmp_path):
    api, _ = client(tmp_path)
    assert api.get("/search/config").json() == {
        "mode": "SEARXNG", "searxng_url": "http://localhost:8080", "provider": None,
        "model": None, "effort": "medium", "max_offer_age_days": 90,
        "capabilities": {
            "structured_output": False, "web_search": False, "reasoning_effort": False,
            "streaming": False, "tool_calling": False,
        },
    }
    pipeline = api.get("/ai/pipeline").json()
    assert {item["provider"] for item in pipeline["overrides"].values()} == {"default"}
    assert pipeline["ai_fallback"] is None


def test_pipeline_override_and_ai_fallback_are_explicit(tmp_path):
    store = FakeKeyStore(); store.set("deepseek_api", "sk-deepseek-secret-123456")
    provider = FakeApiProvider("deepseek_api", discovery(), False)
    api = TestClient(create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: FakeCodex(discovery()),
        api_key_store=store, provider_factories={"deepseek_api": lambda _: provider},
        search_provider_factory=lambda _: FakeSearchProvider(),
    ))
    api.put("/codex/model", json={"model": "test-model"})
    overrides = {stage: {"provider": "default", "effort": "medium"} for stage in (
        "screening", "deep_analysis", "company_analysis", "application_preparation",
    )}
    overrides["screening"] = {"provider": "deepseek_api", "model": "deepseek_api-test", "effort": "medium"}
    saved = api.put("/ai/pipeline", json={"overrides": overrides, "ai_fallback": "chatgpt_oauth", "confirm_api_billing": True})
    assert saved.status_code == 200
    assert saved.json()["overrides"]["screening"]["provider"] == "deepseek_api"
    assert saved.json()["overrides"]["deep_analysis"]["provider"] == "default"
    assert saved.json()["ai_fallback"] == "chatgpt_oauth"


def test_searxng_failure_never_triggers_paid_fallback(tmp_path):
    store = FakeKeyStore(); store.set("openai", "sk-openai-secret-123456")
    openai = FakeOpenAI({"results": [{
        "title": "Stage IA", "url": "https://jobs.example.test/offer-1",
        "snippet": "Mission ML", "source": "OpenAI",
    }]})
    failing_search = FakeSearchProvider(error=SearchProviderUnavailableError("SearXNG unreachable"))
    app = create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: FakeCodex(discovery()),
        api_key_store=store, openai_factory=lambda _: openai,
        search_provider_factory=lambda _: failing_search,
        offer_http_client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=offer_page(), request=request)),
        ),
        offer_resolver=public_dns,
    )
    api = TestClient(app); api.put("/codex/model", json={"model": "test-model"})
    assert api.post("/codex/discover").status_code == 503
    assert openai.runs == []
    latest = api.get("/codex/discovery/latest").json()
    assert latest["status"] == "SEARCH_PROVIDER_UNAVAILABLE"
    assert latest["search_provider"] == "searxng" and latest["search_fallback_used"] == 0


def test_ai_direct_uses_selected_provider_then_common_screening_pipeline(tmp_path):
    store = FakeKeyStore(); store.set("gemini_api", "gemini-secret-1234567890")
    search = FakeApiProvider("gemini_api", {"results": [{
        "title": "Stage IA", "company": "Example", "url": "https://jobs.example.test/offer-1",
        "snippet": "Mission ML", "source": "Gemini",
    }]}, True)
    codex = FakeCodex(discovery())
    app = create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: codex,
        api_key_store=store, provider_factories={"gemini_api": lambda _: search},
        search_provider_factory=lambda _: FakeSearchProvider(error=AssertionError("SearXNG ne doit pas être appelé")),
        offer_http_client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=offer_page(), request=request)),
        ), offer_resolver=public_dns,
    )
    api = TestClient(app); api.put("/codex/model", json={"model": "test-model"})
    saved = api.put("/search/config", json={
        "mode": "AI_DIRECT", "searxng_url": "http://localhost:8080", "provider": "gemini_api",
        "model": "gemini_api-test", "effort": "medium", "confirm_api_billing": True,
    })
    assert saved.status_code == 200
    result = api.post("/codex/discover")
    assert result.status_code == 200 and len(search.runs) == 2
    assert all(call[1]["web_search"] is True for call in search.runs)
    assert codex.calls == 1 and codex.runs[-1][1]["web_search"] is False
    latest = api.get("/codex/discovery/latest").json()
    assert latest["search_mode"] == "AI_DIRECT" and latest["search_provider"] == "gemini_api"
    candidate = api.get(f"/codex/discovery/{result.json()['run_id']}/candidates").json()[0]
    assert candidate["retrieval_mode"] == "AI_DIRECT" and candidate["search_provider"] == "gemini_api"


def ai_direct_client(tmp_path, monkeypatch, provider_id, results, metadata):
    search = FakeOpenAI({"results": results})
    original_run = search.run

    def run(*args, **kwargs):
        original_run(*args, **kwargs)
        return CodexOutput(search.response, metadata)

    search.run = run
    codex = FakeCodex(discovery("https://jobs.example.test/jobs/stage-ia-12345"))
    store = FakeKeyStore()
    store.set("openai", "sk-openai-test-1234567890")
    if provider_id == "chatgpt_oauth":
        monkeypatch.setattr("app.main.ChatGPTOAuthResponsesProvider", lambda _: search)
    app = create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: codex,
        api_key_store=store, openai_factory=lambda _: search,
        search_provider_factory=lambda _: FakeSearchProvider(error=AssertionError("SearXNG appelé")),
        offer_http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=offer_page(), request=request),
        )), offer_resolver=public_dns,
    )
    api = TestClient(app)
    assert api.put("/codex/model", json={"model": "test-model"}).status_code == 200
    assert api.put("/search/config", json={
        "mode": "AI_DIRECT", "searxng_url": "http://localhost:8080", "provider": provider_id,
        "model": "gpt-5.6-sol" if provider_id == "chatgpt_oauth" else "gpt-5-test",
        "effort": "medium", "confirm_api_billing": provider_id != "chatgpt_oauth",
    }).status_code == 200
    return api, app, search, codex


def direct_search_results():
    return [{
        "title": "Stage IA", "company": "Example", "url": url,
        "snippet": "Stage IA Paris, optimisation", "source": "Career website",
    } for url in (
        "https://jobs.example.test/jobs/stage-known-54321",
        "https://jobs.example.test/jobs/stage-ia-12345",
        "https://jobs.example.test/jobs/stage-ia-12345/?utm_source=test#details",
        "not-a-url",
    )]


@pytest.mark.parametrize("provider_id", ["chatgpt_oauth", "openai"])
def test_ai_direct_exposes_observed_search_metadata_and_common_funnel(tmp_path, monkeypatch, provider_id):
    metadata = {
        "web_search_calls": 3, "input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
        "web_sources": [
            "https://sources.example.test/article", "https://sources.example.test/second",
            "https://sources.example.test/article/?utm_source=test#citation", "not-a-url",
        ],
        "web_search_actions": ["search", "open_page"],
        "web_search_queries": ["actual provider query"], "web_domains": ["sources.example.test"],
    }
    api, _, search, codex = ai_direct_client(tmp_path, monkeypatch, provider_id, direct_search_results(), metadata)
    api.post("/applications", json={
        "company": "Example", "position": "Stage connu",
        "url": "https://jobs.example.test/jobs/stage-known-54321",
    })

    result = api.post("/codex/discover")
    assert result.status_code == 200 and result.json()["new"] == 1
    latest = api.get("/codex/discovery/latest").json()
    assert latest["status"] == "SUCCESS" and latest["search_call_count"] == len(search.runs) == 2
    assert latest["web_search_calls"] == 6
    assert latest["raw_result_count"] == 8 and latest["extracted_url_count"] == 6
    assert latest["invalid_url_count"] == latest["intra_query_duplicate_count"] == 2
    assert latest["admitted_result_count"] == 4 and latest["global_duplicate_count"] == 2
    assert latest["merged_count"] == 2 and latest["source_url_count"] == 2
    assert latest["known_url_count"] == latest["unknown_url_count"] == 1
    assert latest["candidate_count"] == latest["likely_job_detail_count"] == 1
    assert latest["fetch_attempted"] == latest["fetch_succeeded"] == 1
    assert latest["fetch_failed"] == latest["snippet_fallback_count"] == 0
    assert latest["ai_batch_count"] == latest["ai_call_count"] == codex.calls == 1
    assert latest["ai_input_count"] == latest["ai_decision_count"] == latest["ai_keep_count"] == 1
    assert latest["ai_reject_count"] == latest["ai_review_count"] == 0
    assert latest["inserted_count"] == latest["new_count"] == 1
    assert (latest["search_input_tokens"], latest["search_output_tokens"], latest["search_total_tokens"]) == (20, 8, 28)
    assert set(json.loads(latest["web_sources"])) == {
        "https://sources.example.test/article", "https://sources.example.test/second",
    }
    assert "actual provider query" in json.loads(latest["web_search_queries"])
    assert set(json.loads(latest["web_search_actions"])) == {"search", "open_page"}
    queries = json.loads(latest["per_query_stats"])
    assert len(queries) == 2 and all(item["metrics_version"] == 1 for item in queries)
    assert all(item["web_search_calls"] == 3 and item["extracted_url_count"] == 3 for item in queries)
    assert all(len(item["web_sources"]) == 2 and item["source_url_count"] == 2 for item in queries)
    assert all(item["web_search_queries"] == ["actual provider query"] for item in queries)
    candidates = api.get(f"/codex/discovery/{result.json()['run_id']}/candidates").json()
    assert len(candidates) == 1 and candidates[0]["ai_decision"] == "KEEP"
    assert candidates[0]["retrieval_mode"] == "AI_DIRECT" and candidates[0]["search_provider"] == provider_id
    assert codex.runs[0][1]["web_search"] is False


@pytest.mark.parametrize("provider_id", ["chatgpt_oauth", "openai"])
def test_ai_direct_empty_results_keep_observed_zero_counters(tmp_path, monkeypatch, provider_id):
    api, _, search, codex = ai_direct_client(tmp_path, monkeypatch, provider_id, [], {
        "web_search_calls": 0, "web_sources": [], "web_search_actions": [], "web_search_queries": [],
    })
    result = api.post("/codex/discover")
    assert result.status_code == 200 and result.json()["new"] == 0
    latest = api.get("/codex/discovery/latest").json()
    assert latest["status"] == "SUCCESS" and latest["search_call_count"] == len(search.runs) == 2
    for key in (
        "web_search_calls", "raw_result_count", "extracted_url_count", "source_url_count",
        "invalid_url_count", "admitted_result_count", "merged_count", "intra_query_duplicate_count",
        "global_duplicate_count", "known_url_count", "unknown_url_count", "candidate_count",
        "fetch_attempted", "fetch_succeeded", "fetch_failed", "snippet_fallback_count",
        "ai_batch_count", "ai_call_count", "ai_input_count", "ai_decision_count", "ai_keep_count",
        "ai_reject_count", "ai_review_count", "ai_missing_decision_count", "ai_retry_count", "inserted_count",
    ):
        assert latest[key] == 0, key
    assert codex.calls == 0 and api.get("/applications").json() == []


@pytest.mark.parametrize("provider_id", ["chatgpt_oauth", "openai"])
def test_ai_direct_missing_metadata_is_unavailable_without_losing_common_metrics(tmp_path, monkeypatch, provider_id):
    api, _, _, _ = ai_direct_client(tmp_path, monkeypatch, provider_id, direct_search_results()[1:2], {})
    assert api.post("/codex/discover").status_code == 200
    latest = api.get("/codex/discovery/latest").json()
    assert latest["search_call_count"] == latest["raw_result_count"] == latest["extracted_url_count"] == 2
    assert latest["merged_count"] == latest["candidate_count"] == latest["inserted_count"] == 1
    assert latest["web_search_calls"] is None and latest["source_url_count"] is None
    assert latest["search_input_tokens"] is latest["search_output_tokens"] is latest["search_total_tokens"] is None
    assert all(item["web_search_calls"] is None for item in json.loads(latest["per_query_stats"]))


@pytest.mark.parametrize("per_query_stats", [[], [{"query": "legacy", "status": "SUCCESS", "raw_result_count": 3}]])
def test_legacy_discovery_does_not_invent_new_or_uninstrumented_metrics(tmp_path, per_query_stats):
    api, app = client(tmp_path)
    with connect(app.state.db_path) as db:
        db.execute("""INSERT INTO discovery_runs(
            started_at, finished_at, status, model, search_mode, search_provider,
            per_query_stats, raw_result_count, new_count, web_search_calls
        ) VALUES (?, ?, 'SUCCESS', 'test-model', 'AI_DIRECT', 'chatgpt_oauth', ?, 3, 2, 6)""", (
            "2026-10-01T12:00:00+00:00", "2026-10-01T12:01:00+00:00", json.dumps(per_query_stats),
        ))
        db.commit()
    latest = api.get("/codex/discovery/latest").json()
    assert latest["status"] == "SUCCESS" and latest["new_count"] == 2
    assert latest["raw_result_count"] == (3 if per_query_stats else None)
    assert latest["extracted_url_count"] is latest["invalid_url_count"] is latest["source_url_count"] is None
    assert latest["web_search_calls"] is None
    if not per_query_stats:
        assert latest["fetch_attempted"] is latest["ai_batch_count"] is latest["search_call_count"] is None


@pytest.mark.parametrize("provider_id", ["chatgpt_oauth", "openai"])
@pytest.mark.parametrize("fail_retry", [False, True])
def test_ai_direct_running_snapshots_survive_retrieval_fetch_screening_and_failure(
    tmp_path, monkeypatch, provider_id, fail_retry,
):
    api, app, search, codex = ai_direct_client(tmp_path, monkeypatch, provider_id, direct_search_results(), {
        "web_search_calls": 3, "web_sources": ["https://sources.example.test/article"],
    })
    api.post("/applications", json={
        "company": "Example", "position": "Stage connu",
        "url": "https://jobs.example.test/jobs/stage-known-54321",
    })
    entered = {stage: threading.Event() for stage in ("retrieval", "fetch", "screening", "retry")}
    releases = {stage: threading.Event() for stage in entered}
    original_search_run = search.run
    search_count = 0

    def search_run(*args, **kwargs):
        nonlocal search_count
        search_count += 1
        if search_count == 2:
            entered["retrieval"].set()
            assert releases["retrieval"].wait(15)
        return original_search_run(*args, **kwargs)

    search.run = search_run

    def fetch(request):
        if not entered["fetch"].is_set():
            entered["fetch"].set()
            assert releases["fetch"].wait(15)
        return httpx.Response(200, text=offer_page(), request=request)

    app.state.offer_http_client_factory = lambda: httpx.Client(transport=httpx.MockTransport(fetch))
    screening_count = 0

    def screening_run(*args, **kwargs):
        nonlocal screening_count
        screening_count += 1
        stage = "screening" if screening_count == 1 else "retry"
        entered[stage].set()
        assert releases[stage].wait(15)
        if stage == "retry" and fail_retry:
            raise CodexError("screening failed after retrieval")
        return CodexOutput({"decisions": []} if stage == "screening" else codex.response, {})

    codex.run = screening_run
    responses = []
    worker = threading.Thread(target=lambda: responses.append(api.post("/codex/discover")), daemon=True)
    snapshots = []
    worker.start()
    try:
        for stage in entered:
            assert entered[stage].wait(15), stage
            latest = api.get("/codex/discovery/latest").json()
            snapshots.append(latest)
            assert latest["status"] == "RUNNING" and latest["finished_at"] is None
            if stage == "retrieval":
                assert latest["search_call_count"] == 2 and latest["raw_result_count"] == 4
                assert latest["extracted_url_count"] == 3 and latest["admitted_result_count"] == 2
                queries = json.loads(latest["per_query_stats"])
                assert [item["status"] for item in queries] == ["SUCCESS", "RUNNING"]
                assert latest["fetch_attempted"] == latest["ai_batch_count"] == 0
            else:
                assert latest["search_call_count"] == 2 and latest["raw_result_count"] == 8
                assert latest["extracted_url_count"] == 6 and latest["merged_count"] == 2
                assert latest["known_url_count"] == latest["unknown_url_count"] == latest["candidate_count"] == 1
                assert latest["fetch_attempted"] == 1
            if stage == "fetch":
                assert latest["fetch_succeeded"] == latest["fetch_failed"] == latest["ai_batch_count"] == 0
            elif stage in {"screening", "retry"}:
                assert latest["fetch_succeeded"] == latest["ai_batch_count"] == latest["ai_input_count"] == 1
                assert latest["ai_call_count"] == (1 if stage == "screening" else 2)
                assert latest["ai_missing_decision_count"] == latest["ai_retry_count"] == (stage == "retry")
            releases[stage].set()
    finally:
        for release in releases.values():
            release.set()
        worker.join(15)
    assert not worker.is_alive() and len(responses) == 1
    assert responses[0].status_code == (502 if fail_retry else 200)
    latest = api.get("/codex/discovery/latest").json()
    snapshots.append(latest)
    assert latest["status"] == ("FAILED" if fail_retry else "SUCCESS")
    assert latest["finished_at"] and latest["raw_result_count"] == 8
    assert latest["search_call_count"] == 2 and latest["fetch_attempted"] == latest["fetch_succeeded"] == 1
    assert latest["ai_call_count"] == 2 and latest["ai_retry_count"] == latest["ai_missing_decision_count"] == 1
    assert latest["inserted_count"] == latest["ai_keep_count"] == (0 if fail_retry else 1)
    if fail_retry:
        assert "screening failed after retrieval" in latest["error"]
    for key in (
        "search_call_count", "raw_result_count", "extracted_url_count", "admitted_result_count",
        "merged_count", "fetch_attempted", "fetch_succeeded", "ai_batch_count", "ai_input_count",
        "ai_call_count", "ai_missing_decision_count", "ai_retry_count",
    ):
        values = [snapshot[key] for snapshot in snapshots]
        assert values == sorted(values), key


def test_chatgpt_web_search_effort_is_restored_and_sent_to_test_and_discovery(tmp_path, monkeypatch):
    search = FakeOpenAI({"results": [{
        "title": "Stage IA", "company": "Example", "url": "https://jobs.example.test/offer-1",
        "snippet": "Mission ML", "source": "ChatGPT",
    }]})
    monkeypatch.setattr("app.main.ChatGPTOAuthResponsesProvider", lambda _token: search)
    codex = FakeCodex(discovery())
    app = create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: codex,
        offer_http_client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=offer_page(), request=request)),
        ), offer_resolver=public_dns,
    )
    api = TestClient(app)
    base = {
        "mode": "AI_DIRECT", "searxng_url": "http://localhost:8080",
        "provider": "chatgpt_oauth", "model": "gpt-5.6-sol",
    }

    for effort in ("low", "max"):
        assert api.post("/search/test", json={**base, "effort": effort}).status_code == 200
        assert search.runs[-1][1]["effort"] == effort

    assert api.put("/search/config", json={**base, "effort": "high"}).status_code == 200
    assert api.get("/search/config").json()["effort"] == "high"
    assert api.put("/codex/model", json={"model": "test-model"}).status_code == 200
    assert api.post("/codex/discover").status_code == 200
    assert search.runs[-1][1]["effort"] == "high"


def test_ai_direct_rejects_provider_without_real_web_search_before_run(tmp_path):
    store = FakeKeyStore(); store.set("deepseek_api", "deepseek-secret-123456")
    deepseek = FakeApiProvider("deepseek_api", {"results": []}, False)
    database = tmp_path / "tracker.db"
    api = TestClient(create_app(
        database, tmp_path / "applications", FakeAuth(), lambda _: FakeCodex(discovery()),
        api_key_store=store, provider_factories={"deepseek_api": lambda _: deepseek},
    ))
    api.put("/codex/model", json={"model": "test-model"})
    result = api.put("/search/config", json={
        "mode": "AI_DIRECT", "searxng_url": "http://localhost:8080", "provider": "deepseek_api",
        "model": "deepseek_api-test", "effort": "medium", "confirm_api_billing": True,
    })
    assert result.status_code == 409 and "unavailable" in result.json()["detail"]
    with connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM discovery_runs").fetchone()[0] == 0


def test_ai_direct_provider_failure_sets_unavailable_status(tmp_path):
    store = FakeKeyStore(); store.set("gemini_api", "gemini-secret-1234567890")
    search = FakeApiProvider("gemini_api", {"results": []}, True)
    api = TestClient(create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: FakeCodex(discovery()),
        api_key_store=store, provider_factories={"gemini_api": lambda _: search},
    ))
    api.put("/codex/model", json={"model": "test-model"})
    assert api.put("/search/config", json={
        "mode": "AI_DIRECT", "searxng_url": "http://localhost:8080", "provider": "gemini_api",
        "model": "gemini_api-test", "effort": "medium", "confirm_api_billing": True,
    }).status_code == 200
    search.error = FakeProviderError(429)
    result = api.post("/codex/discover")
    assert result.status_code == 503
    assert api.get("/codex/discovery/latest").json()["status"] == "SEARCH_PROVIDER_UNAVAILABLE"


def test_ai_fallback_runs_on_technical_failure_not_on_valid_empty_result(tmp_path):
    store = FakeKeyStore(); store.set("deepseek_api", "sk-deepseek-secret-123456")
    primary = FakeApiProvider("deepseek_api", {"decisions": []}, False)
    fallback = FakeCodex(discovery())
    app = create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: fallback,
        api_key_store=store, provider_factories={"deepseek_api": lambda _: primary},
        search_provider_factory=lambda _: FakeSearchProvider(),
        offer_http_client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=offer_page(), request=request)),
        ), offer_resolver=public_dns,
    )
    api = TestClient(app); api.put("/codex/model", json={"model": "test-model"})
    overrides = {stage: {"provider": "default", "effort": "medium"} for stage in (
        "screening", "deep_analysis", "company_analysis", "application_preparation",
    )}
    overrides["screening"] = {"provider": "deepseek_api", "model": "deepseek_api-test", "effort": "medium"}
    assert api.put("/ai/pipeline", json={"overrides": overrides, "ai_fallback": "chatgpt_oauth", "confirm_api_billing": True}).status_code == 200
    assert api.post("/codex/discover").status_code == 200
    assert fallback.calls == 0
    primary.error = FakeProviderError(429)
    assert api.post("/codex/discover").status_code == 200
    assert fallback.calls == 1
    assert api.get("/codex/discovery/latest").json()["ai_fallback_used"] == 1


@pytest.mark.parametrize("provider_id", ["openai", "anthropic_api", "gemini_api", "deepseek_api"])
def test_api_key_lifecycle_never_returns_or_writes_secret_to_sqlite(tmp_path, caplog, provider_id):
    database = tmp_path / "tracker.db"
    store = FakeKeyStore()
    provider = FakeOpenAI()
    api = TestClient(create_app(
        database, tmp_path / "applications", FakeAuth(False), api_key_store=store,
        provider_factories={provider_id: lambda _: provider},
    ))
    assert api.get("/ai/config").json()["api_keys"][provider_id]["configured"] is False
    secret = "sk-test-secret-never-returned"
    assert api.post(f"/ai/api-keys/{provider_id}/test", json={"api_key": secret}).json() == {"valid": True}
    saved = api.put(f"/ai/api-keys/{provider_id}", json={"api_key": secret}).json()
    assert saved == {"configured": True}
    payload = json.dumps(api.get("/ai/config").json())
    assert secret not in payload and payload.count(secret[-4:]) == 1
    with connect(database) as db:
        assert secret not in json.dumps([tuple(row) for row in db.execute("SELECT key, value FROM settings")])
    replacement = "sk-replacement-never-returned"
    api.put(f"/ai/api-keys/{provider_id}", json={"api_key": replacement})
    assert store.get(provider_id) == replacement
    assert api.delete(f"/ai/api-keys/{provider_id}").json() == {"configured": False}
    assert store.get(provider_id) is None
    assert secret not in caplog.text and replacement not in caplog.text


@pytest.mark.parametrize("provider_id", ["openai", "anthropic_api", "gemini_api", "deepseek_api"])
def test_invalid_api_key_has_safe_error(tmp_path, provider_id):
    provider = FakeOpenAI(error=FakeProviderError(401))
    api = TestClient(create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(False),
        api_key_store=FakeKeyStore(), provider_factories={provider_id: lambda _: provider},
    ))
    response = api.post(f"/ai/api-keys/{provider_id}/test", json={"api_key": "sk-invalid-never-returned"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid API key"}
    assert "sk-invalid-never-returned" not in response.text


def test_openai_api_active_is_explicit_and_used_without_fallback(tmp_path):
    store = FakeKeyStore(); store.set("openai", "sk-test-secret-never-returned")
    provider = FakeOpenAI(discovery())
    codex = FakeCodex(error=CodexError("must not run"))
    api = TestClient(create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: codex,
        api_key_store=store, openai_factory=lambda _: provider,
        search_provider_factory=lambda _: FakeSearchProvider(),
    ))
    assert api.put("/ai/active", json={
        "provider_id": "openai", "auth_mode": "api_key", "model": "gpt-5-test",
        "effort": "medium", "confirm_api_billing": False,
    }).status_code == 409
    assert api.put("/ai/active", json={
        "provider_id": "openai", "auth_mode": "api_key", "model": "gpt-5-test",
        "effort": "medium", "confirm_api_billing": True,
    }).status_code == 200
    assert api.post("/codex/discover").status_code == 200
    assert provider.runs[-1][1]["web_search"] is False and codex.calls == 0


def test_unavailable_active_provider_never_falls_back(tmp_path):
    store = FakeKeyStore(); store.set("openai", "sk-test-secret-never-returned")
    provider = FakeOpenAI(discovery())
    codex = FakeCodex(error=CodexError("quota unavailable"))
    api = TestClient(create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: codex,
        api_key_store=store, openai_factory=lambda _: provider,
        search_provider_factory=lambda _: FakeSearchProvider(),
    ))
    assert api.put("/codex/model", json={"model": "test-model"}).status_code == 502
    with connect(tmp_path / "tracker.db") as db:
        db.executemany("INSERT INTO settings(key, value) VALUES(?, ?)", [
            ("ai_provider", "openai"), ("ai_auth_mode", "chatgpt_oauth"),
            ("ai_model", "test-model"), ("ai_reasoning_effort", "medium"),
        ]); db.commit()
    assert api.post("/codex/discover").status_code == 502
    assert provider.runs == []


def test_unavailable_api_never_falls_back_to_connected_chatgpt(tmp_path):
    store = FakeKeyStore(); store.set("openai", "sk-test-secret-never-returned")
    provider = FakeOpenAI(error=FakeProviderError(429))
    codex = FakeCodex(discovery())
    api = TestClient(create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: codex,
        api_key_store=store, openai_factory=lambda _: provider,
        search_provider_factory=lambda _: FakeSearchProvider(),
    ))
    with connect(tmp_path / "tracker.db") as db:
        db.executemany("INSERT INTO settings(key, value) VALUES(?, ?)", [
            ("ai_provider", "openai"), ("ai_auth_mode", "api_key"),
            ("ai_model", "gpt-5-test"), ("ai_reasoning_effort", "medium"),
        ]); db.commit()
    assert api.post("/codex/discover").status_code == 429
    assert codex.calls == 0


@pytest.mark.parametrize("active_provider", ["openai", "anthropic_api", "gemini_api"])
def test_failed_api_provider_never_calls_another_configured_provider(tmp_path, active_provider):
    provider_ids = ["openai", "anthropic_api", "gemini_api", "deepseek_api"]
    store = FakeKeyStore()
    providers = {name: FakeApiProvider(name, discovery(), True) for name in provider_ids}
    providers[active_provider].error = FakeProviderError(429)
    for name in provider_ids:
        store.set(name, f"sk-{name}-secret-never-returned")
    api = TestClient(create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(),
        api_key_store=store,
        provider_factories={name: (lambda _, provider=provider: provider) for name, provider in providers.items()},
        search_provider_factory=lambda _: FakeSearchProvider(),
    ))
    with connect(tmp_path / "tracker.db") as db:
        db.executemany("INSERT INTO settings(key, value) VALUES(?, ?)", [
            ("ai_provider", active_provider), ("ai_auth_mode", "api_key"),
            ("ai_model", f"{active_provider}-test"), ("ai_reasoning_effort", "medium"),
            ("ai_capabilities", json.dumps({"web_search": True, "structured_output": True})),
        ]); db.commit()
    assert api.post("/codex/discover").status_code == 429
    assert all(not provider.runs for name, provider in providers.items() if name != active_provider)


def test_deleting_active_api_key_deactivates_provider(tmp_path):
    store = FakeKeyStore(); store.set("openai", "sk-test-secret-never-returned")
    provider = FakeOpenAI()
    api = TestClient(create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(),
        api_key_store=store, openai_factory=lambda _: provider,
    ))
    api.put("/ai/active", json={
        "provider_id": "openai", "auth_mode": "api_key", "model": "gpt-5-test",
        "effort": "medium", "confirm_api_billing": True,
    })
    api.delete("/ai/api-keys/openai")
    assert api.get("/ai/config").json()["active"] is None


@pytest.mark.parametrize("provider_id,web_search", [
    ("openai", True), ("anthropic_api", True), ("gemini_api", True), ("deepseek_api", False),
])
def test_all_api_providers_support_key_model_activation_and_preparation(tmp_path, provider_id, web_search):
    repo = isolated_repo(tmp_path)
    ids = ["planning-prototype", "search-prototype", "model-experiment", "document-workflow"]
    key = "sk-provider-secret-123456"
    store = FakeKeyStore()
    provider = FakeApiProvider(provider_id, preparation("CREATE", project_ids=ids), web_search)
    codex = FakeCodex(error=AssertionError("ChatGPT fallback interdit"))
    api = TestClient(create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: codex,
        api_key_store=store,
        provider_factories={provider_id: lambda received: provider if received == key else None},
        repo_root=repo,
    ))

    assert api.post(f"/ai/api-keys/{provider_id}/test", json={"api_key": key}).status_code == 200
    assert api.put(f"/ai/api-keys/{provider_id}", json={"api_key": key}).status_code == 200
    models = api.get(f"/ai/models?provider_id={provider_id}&auth_mode=api_key").json()["models"]
    assert models[0]["capabilities"]["structured_output"] is True
    assert models[0]["capabilities"]["web_search"] is web_search
    assert api.put("/ai/active", json={
        "provider_id": provider_id, "auth_mode": "api_key", "model": f"{provider_id}-test",
        "effort": "medium", "confirm_api_billing": True,
    }).status_code == 200
    config = api.get("/ai/config").json()
    assert config["active"]["provider_id"] == provider_id
    assert config["api_keys"][provider_id] == {
        "configured": True, "last_four": "3456", "billing_confirmed": True,
    }
    assert key not in json.dumps(config)

    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    prepared = wait_for_preparation(api, application_id, "completed")
    assert prepared["status"] == "PREPARED"
    assert cv_project_order(Path(prepared["cv_path"]), project_catalog(repo)) == ids
    schema = provider.runs[-1][0][2]
    assert schema["$defs"]["CvContent"]["properties"]["projects"]["maxItems"] == 4
    assert provider.runs[-1][1]["web_search"] is False
    assert codex.calls == 0

    assert api.delete(f"/ai/api-keys/{provider_id}").status_code == 200
    assert api.get("/ai/config").json()["api_keys"][provider_id]["configured"] is False


def test_model_list_and_selection(tmp_path):
    api, _ = client(tmp_path)
    data = api.get("/codex/models").json()
    assert data["models"][0]["model"] == data["selected"] == "test-model"
    assert data["selectedEffort"] == "medium"
    assert data["models"][0]["reasoningEfforts"] == ["low", "medium", "high", "xhigh"]


def test_reasoning_capabilities_prefer_metadata_then_safe_fallback():
    assert supported_reasoning_efforts({"model": "gpt-5.6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}) == ["low"]
    assert supported_reasoning_efforts({"model": "gpt-5.6-sol"})[-1] == "max"
    assert supported_reasoning_efforts({"model": "unknown"}) == ["medium"]


def test_reasoning_effort_default_change_and_persistence(tmp_path):
    fake = FakeCodex()
    database = tmp_path / "tracker.db"
    first = TestClient(create_app(database, tmp_path / "applications", FakeAuth(), lambda _: fake))
    assert first.get("/codex/models").json()["selectedEffort"] == "medium"
    assert first.put("/codex/model", json={"model": "gpt-5.6-sol", "effort": "xhigh"}).status_code == 200
    second = TestClient(create_app(database, tmp_path / "applications", FakeAuth(), lambda _: fake))
    settings = second.get("/codex/models").json()
    assert settings["selected"] == "gpt-5.6-sol" and settings["selectedEffort"] == "xhigh"


def test_unsupported_reasoning_effort_is_rejected(tmp_path):
    api, _ = client(tmp_path)
    result = api.put("/codex/model", json={"model": "test-model", "effort": "max"})
    assert result.status_code == 422
    assert "not supported" in result.json()["detail"]
    assert api.get("/codex/models").json()["selectedEffort"] == "medium"


def test_reasoning_effort_is_used_for_discovery_and_preparation(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    api, app = client(tmp_path, discovery(), repo_root=repo)
    assert api.put("/codex/model", json={"model": "test-model", "effort": "high"}).status_code == 200
    assert api.post("/codex/discover").status_code == 200
    assert app.state.fake_codex.runs[-1][1]["effort"] == "high"
    app.state.fake_codex.response = preparation()
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    wait_for_preparation(api, application_id, "completed")
    assert app.state.fake_codex.runs[-1][1]["effort"] == "high"


def test_discovery_success_and_structured_result(tmp_path):
    api, _ = client(tmp_path, discovery())
    result = api.post("/codex/discover")
    assert result.status_code == 200 and result.json()["found"] == 1
    assert result.json()["jobs"][0]["why_relevant"] == "Optimisation"


def test_discovery_inserts_detected(tmp_path):
    api, _ = client(tmp_path, discovery())
    api.post("/codex/discover")
    assert api.get("/applications").json()[0]["status"] == "DETECTED"


def test_discovery_eligibility_uses_fetched_content_and_blocks_selection(tmp_path):
    restricted = found_offer("https://jobs.example.test/restricted")
    civil = found_offer("https://jobs.example.test/civil")

    def handler(request):
        content = (
            "Stage IA. Security clearance required."
            if request.url.path == "/restricted"
            else "Stage Machine Learning. Analyse de données industrielles. Aucune habilitation requise."
        )
        return httpx.Response(200, text=offer_page() + content, request=request)

    api, _ = client(tmp_path, {"decisions": [
        screening_decision("candidate_001", restricted), screening_decision("candidate_002", civil),
    ]}, offer_handler=handler)
    assert api.post("/codex/discover").status_code == 200
    applications = {item["url"]: item for item in api.get("/applications").json()}
    blocked = applications[restricted["url"]]
    allowed = applications[civil["url"]]

    assert blocked["eligibility_status"] == "INELIGIBLE"
    assert api.post(f"/applications/{blocked['id']}/select").status_code == 409
    assert api.get(f"/applications/{blocked['id']}").json()["status"] == "DETECTED"
    assert allowed["eligibility_status"] == "ELIGIBLE"
    assert api.post(
        f"/applications/{allowed['id']}/status", json={"status": "SHORTLISTED"},
    ).status_code == 200


def test_discovery_reports_all_pipeline_counts(tmp_path):
    offers = []
    for index in range(5):
        offer = json.loads(json.dumps(found_offer()))
        offer["url"] = f"https://jobs.example.test/offer-{index}"
        offer["evidence_urls"] = [offer["url"]]
        offers.append(offer)
    api, _ = client(tmp_path, {"decisions": [
        screening_decision(f"candidate_{index + 1:03d}", offer) for index, offer in enumerate(offers)
    ]}, offer_handler=lambda request: httpx.Response(
        200, text=f'<meta name="datePublished" content="{date.today().isoformat()}">', request=request,
    ))
    result = api.post("/codex/discover")
    latest = api.get("/codex/discovery/latest").json()
    assert result.status_code == 200 and result.json()["new"] == 5
    assert latest["provider_results_raw"] == latest["parsed_results"] == 5
    assert latest["schema_valid_results"] == latest["valid_http_urls"] == 5
    assert latest["open_or_unknown_count"] == latest["inserted_count"] == 5
    assert latest["raw_result_count"] == latest["admitted_result_count"] == 10
    assert latest["merged_count"] == 5 and latest["global_duplicate_count"] == 5
    assert latest["known_url_count"] == 0 and latest["unknown_url_count"] == 5
    assert latest["candidate_count"] == latest["fetch_attempted"] == latest["fetch_succeeded"] == 5
    assert latest["fetch_failed"] == 0 and latest["snippet_fallback_count"] == 5
    assert latest["ai_input_count"] == 5
    assert latest["ai_batch_count"] == latest["ai_call_count"] == 1
    assert latest["ai_decision_count"] == latest["ai_keep_count"] == 5
    assert latest["ai_reject_count"] == latest["ai_review_count"] == 0
    assert latest["ai_missing_decision_count"] == latest["ai_retry_count"] == 0
    assert len(json.loads(latest["per_query_stats"])) == 2


def test_discovery_counts_three_new_and_two_known(tmp_path):
    offers = []
    for index in range(5):
        offer = json.loads(json.dumps(found_offer()))
        offer["url"] = f"https://jobs.example.test/mixed-{index}"
        offer["evidence_urls"] = [offer["url"]]
        offers.append(offer)
    api, app = client(tmp_path, {"decisions": [
        screening_decision(f"candidate_{index + 1:03d}", offer) for index, offer in enumerate(offers)
    ]})
    for offer in offers[:2]:
        api.post("/applications", json={"company": offer["company"], "position": offer["title"], "url": offer["url"]})
    app.state.fake_codex.response = {"decisions": [
        screening_decision(f"candidate_{index + 1:03d}", offer) for index, offer in enumerate(offers[2:])
    ]}
    result = api.post("/codex/discover").json()
    assert result["new"] == 3 and result["duplicates"] == 2
    latest = api.get("/codex/discovery/latest").json()
    assert latest["duplicates_active"] == 2 and latest["inserted_count"] == 3


def test_discovery_rejects_closed_and_keeps_unknown(tmp_path):
    closed = json.loads(json.dumps(found_offer("https://jobs.example.test/closed")))
    closed["availability"] = "closed"
    unknown = json.loads(json.dumps(found_offer("https://jobs.example.test/unknown")))
    unknown["availability"] = "unknown"
    api, _ = client(tmp_path, {"decisions": [
        screening_decision("candidate_001", closed, decision="REJECT", reason_code="OFFER_CLOSED"),
        screening_decision("candidate_002", unknown),
    ]})
    result = api.post("/codex/discover").json()
    assert result["new"] == 1 and result["rejected"] == 1
    assert result["errors"] == []
    candidates = api.get(f"/codex/discovery/{result['run_id']}/candidates").json()
    assert candidates[0]["ai_decision"] == "REJECT" and candidates[0]["ai_reason_code"] == "OFFER_CLOSED"
    assert api.get("/applications").json()[0]["availability"] == "unknown"


def test_fetch_403_uses_snippet_and_still_screens_candidate(tmp_path):
    def handler(request):
        return httpx.Response(403, text="Human Verification", request=request)

    api, app = client(tmp_path, discovery(), offer_handler=handler)
    result = api.post("/codex/discover").json()
    audit = api.get(f"/codex/discovery/{result['run_id']}/candidates").json()[0]
    assert app.state.fake_codex.calls == 1
    assert audit["content_mode"] == "snippet" and audit["fetch_status"] == "FAILED"
    assert audit["http_status"] == 403 and audit["ai_decision"] == "KEEP"
    assert audit["verification_status"] == "UNKNOWN"


def test_discovery_verifies_ashby_and_generic_urls_before_insertion(tmp_path):
    def offer(url, availability="open"):
        item = json.loads(json.dumps(found_offer(url)))
        item["availability"] = availability
        return item

    offers = [
        offer("https://jobs.ashbyhq.com/present/11111111-1111-1111-1111-111111111111"),
        offer("https://jobs.ashbyhq.com/absent/22222222-2222-2222-2222-222222222222"),
        offer("https://jobs.ashbyhq.com/timeout/33333333-3333-3333-3333-333333333333"),
        offer("https://jobs.example.test/open"),
        offer("https://jobs.example.test/missing"),
    ]

    def handler(request):
        if request.url.host == "api.ashbyhq.com":
            board = request.url.path.rsplit("/", 1)[-1]
            if board == "timeout":
                raise httpx.ReadTimeout("timeout", request=request)
            jobs = [{"title": "Stage IA", "jobUrl": offers[0]["url"], "isListed": True,
                     "publishedAt": date.today().isoformat()}] if board == "present" else []
            return httpx.Response(200, json={"apiVersion": "1", "jobs": jobs}, request=request)
        status = 404 if request.url.path == "/missing" else 200
        return httpx.Response(status, text=offer_page(), request=request)

    api, _ = client(tmp_path, {"decisions": [
        screening_decision(f"candidate_{index + 1:03d}", offer) for index, offer in enumerate(offers)
    ]}, offer_handler=handler)
    result = api.post("/codex/discover").json()
    assert result["new"] == 3
    assert result["verified_open"] == 2
    assert result["verified_closed"] == 1
    assert result["verified_invalid"] == 1
    assert result["verification_unknown"] == 1
    assert {error["reason"] for error in result["errors"]} == {
        "ASHBY_POSTING_NOT_PUBLISHED", "HTTP_NOT_FOUND",
    }
    assert sorted(item["availability"] for item in api.get("/applications").json()) == ["open", "open", "unknown"]


def test_discovery_rejects_ssrf_url_without_requesting_it(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, request=request)

    api, _ = client(
        tmp_path, discovery("http://127.0.0.1/admin"), offer_handler=handler,
    )
    result = api.post("/codex/discover").json()
    assert result["new"] == 0
    assert result["verified_invalid"] == 1
    assert result["errors"] == [{"url": "http://127.0.0.1/admin", "reason": "IP_LOOPBACK"}]
    assert calls == []


def test_empty_provider_result_is_real_success(tmp_path):
    api, _ = client(tmp_path, {"decisions": []})
    result = api.post("/codex/discover")
    assert result.status_code == 200 and result.json()["found"] == 1 and result.json()["new"] == 0
    latest = api.get("/codex/discovery/latest").json()
    assert latest["status"] == "SUCCESS" and latest["web_search_calls"] == 2


def test_discovery_does_not_require_ai_web_tool(tmp_path):
    api, app = client(tmp_path, discovery())
    app.state.fake_codex.run = lambda *_args, **_kwargs: CodexOutput(discovery(), {"web_search_calls": 0})
    result = api.post("/codex/discover")
    assert result.status_code == 200
    assert app.state.fake_codex.runs == []


def test_discovery_ignores_ai_web_search_capability(tmp_path):
    api, _ = client(tmp_path, discovery())
    with connect(api.app.state.db_path) as db:
        db.execute("UPDATE settings SET value=? WHERE key='ai_capabilities'", (
            json.dumps({"structured_output": True, "web_search": False}),
        ))
        db.commit()
    result = api.post("/codex/discover")
    assert result.status_code == 200


def test_discovery_reports_expired_chatgpt_session(tmp_path):
    class ExpiredAuth(FakeAuth):
        def access_token(self):
            raise PermissionError("ChatGPT session expired or revoked. Sign in again.")

    database = tmp_path / "tracker.db"
    api = TestClient(create_app(
        database, tmp_path / "applications", ExpiredAuth(), lambda _: FakeCodex(),
        search_provider_factory=lambda _: FakeSearchProvider(),
    ))
    with connect(database) as db:
        db.executemany("INSERT INTO settings(key, value) VALUES(?, ?)", [
            ("ai_provider", "openai"), ("ai_auth_mode", "chatgpt_oauth"),
            ("ai_model", "test-model"), ("ai_reasoning_effort", "medium"),
            ("ai_capabilities", json.dumps({"structured_output": True, "web_search": True, "reasoning_effort": True})),
        ])
        db.commit()
    result = api.post("/codex/discover")
    assert result.status_code == 401
    assert result.json()["detail"] == "ChatGPT session expired. Sign in again."


def test_discovery_fails_when_structured_response_cannot_be_parsed(tmp_path):
    api, app = client(tmp_path, discovery())
    app.state.fake_codex.run = lambda *_args, **_kwargs: CodexOutput({"wrong": []}, {"web_search_calls": 1})
    assert api.post("/codex/discover").status_code == 502
    assert api.get("/applications").json() == []


def test_discovery_deduplicates_known_url(tmp_path):
    api, _ = client(tmp_path, discovery())
    api.post("/codex/discover")
    assert api.post("/codex/discover").json()["duplicates"] == 1


def test_discovery_deduplicates_ignored_offer_without_restoring_it(tmp_path):
    api, _ = client(tmp_path, discovery())
    application = api.post("/applications", json={
        "company": "Example", "position": "Stage IA", "url": "https://jobs.example.test/offer-1",
    }).json()
    assert api.post(f"/applications/{application['id']}/ignore", json={"details": "Test"}).status_code == 200
    result = api.post("/codex/discover").json()
    assert result["new"] == 0 and result["duplicates"] == 1 and result["ignored"] == 1
    latest = api.get("/codex/discovery/latest").json()
    assert latest["duplicates_ignored"] == 1
    ignored = api.get("/applications", params={"status": "IGNORED"}).json()
    assert [item["id"] for item in ignored] == [application["id"]]


def test_discovery_invalid_url_is_reported_without_losing_run(tmp_path):
    api, _ = client(tmp_path, discovery("not-a-url"))
    result = api.post("/codex/discover")
    payload = result.json()
    assert result.status_code == 200 and payload["new"] == 0 and payload["ai_review_count"] == 1
    assert payload["ai_missing_decision_count"] == payload["ai_retry_count"] == 1
    audit = api.get(f"/codex/discovery/{payload['run_id']}/candidates").json()[0]
    assert audit["ai_reason_code"] == "AI_NO_DECISION" and audit["final_action"] == "REVIEW"
    assert api.get("/applications").json() == []


@pytest.mark.parametrize("url", ["https://example.test/offer", "http://example.test/offer"])
def test_discovery_accepts_http_urls(url):
    assert DiscoveryOutput.model_validate(discovery(url)).decisions[0].offer.url == url


@pytest.mark.parametrize("url", ["https:///offer", "ftp://example.test/offer", "texte arbitraire"])
def test_discovery_rejects_invalid_http_urls(url):
    with pytest.raises(ValueError):
        from app.codex_workflows import normalize_url
        normalize_url(url)


def test_discovery_rejects_invalid_evidence_url(tmp_path):
    payload = discovery()
    payload["decisions"][0]["offer"]["evidence_urls"] = ["https://example.test/source", "ftp://example.test/source"]
    api, _ = client(tmp_path, payload)
    result = api.post("/codex/discover")
    assert result.status_code == 200 and result.json()["ai_review_count"] == 1
    assert result.json()["ai_retry_count"] == 1
    assert api.get("/applications").json() == []


def test_structured_output_schemas_have_no_unsupported_url_format():
    schemas = [DiscoveryOutput.model_json_schema(), PreparationOutput.model_json_schema()]
    encoded = json.dumps(schemas)
    assert '"format": "uri"' not in encoded
    assert '"format": "url"' not in encoded

    def assert_all_properties_required(value):
        if isinstance(value, dict):
            if value.get("type") == "object" and "properties" in value:
                assert set(value["properties"]) == set(value.get("required", []))
            for child in value.values():
                assert_all_properties_required(child)
        elif isinstance(value, list):
            for child in value:
                assert_all_properties_required(child)

    for schema in schemas:
        assert_all_properties_required(schema)


def test_web_search_unavailable(tmp_path):
    api, _ = client(tmp_path, error=CodexError("web search unavailable"))
    assert api.post("/codex/discover").status_code == 502


def test_expired_chatgpt_provider_token_is_actionable(tmp_path):
    api, _ = client(tmp_path, error=CodexError("401 Unauthorized: token_expired"))
    result = api.post("/codex/discover")
    assert result.status_code == 401
    assert result.json()["detail"] == "ChatGPT session expired. Sign in again."
    latest = api.get("/codex/discovery/latest").json()
    assert latest["status"] == "FAILED" and latest["error"] == result.json()["detail"]


def test_discovery_concurrency_guard(tmp_path):
    api, app = client(tmp_path, discovery())
    app.state.discovery_lock.acquire()
    try: assert api.post("/codex/discover").status_code == 409
    finally: app.state.discovery_lock.release()


def test_last_discovery_updated(tmp_path):
    api, _ = client(tmp_path, discovery())
    api.post("/codex/discover")
    latest = api.get("/codex/discovery/latest").json()
    assert latest["status"] == "SUCCESS" and latest["finished_at"]


def test_failed_discovery_recorded(tmp_path):
    api, _ = client(tmp_path, error=CodexError("quota exceeded"))
    api.post("/codex/discover")
    assert api.get("/codex/discovery/latest").json()["status"] == "FAILED"


def test_prepare_success_creates_complete_canonical_artifacts(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    cv = repo / "01_Career/CV/Generated/resume_A.docx"
    source_hash = sha256(cv)
    variants_before = {path.name for path in cv.parent.glob("*.docx")}
    api, _ = client(tmp_path, preparation(source_path=str(cv)), repo_root=repo)
    application_id = shortlist(api)
    result = api.post(f"/applications/{application_id}/prepare-with-codex")
    assert result.status_code == 200 and result.json()["preparation_state"] == "running"
    result = wait_for_preparation(api, application_id, "completed")
    assert result["company_postal_address"] == "10 rue du Test\n75001 Paris"
    folder = next((tmp_path / "applications").iterdir())
    assert (folder / "interview-prep.md").exists()
    assert not (folder / "application.md").exists()
    assert (folder / "lettre-motivation.docx").exists()
    assert not (folder / "lettre-motivation.md").exists()
    assert result["cover_letter_word_count"] > 0
    assert "À compléter" not in (folder / "analysis.md").read_text(encoding="utf-8")
    copied = next((folder / "cv").glob("*.docx"))
    assert sha256(cv) == source_hash == sha256(copied)
    assert {path.name for path in cv.parent.glob("*.docx")} == variants_before
    assert result["cv_action"] == "REUSE"
    assert "## Resume decision" in (folder / "analysis.md").read_text(encoding="utf-8")
    assert result["artifacts"] == {"complete": True, "missing": [], "invalid": []}


@pytest.mark.parametrize("separate_stages", [(), ("deep_analysis",), ("company_analysis",), ("deep_analysis", "company_analysis")])
def test_preparation_retains_primary_metadata_and_distinguishes_effort(tmp_path, monkeypatch, separate_stages):
    repo = isolated_repo(tmp_path, with_variants=True)
    api, app = client(tmp_path, preparation(), repo_root=repo)
    original_run = app.state.fake_codex.run
    def run_with_metadata(*args, **kwargs):
        result = original_run(*args, **kwargs)
        call = app.state.fake_codex.calls
        return CodexOutput(result, {"thread_id": f"thread-{call}", "turn_id": f"turn-{call}", "status": "completed"})
    monkeypatch.setattr(app.state.fake_codex, "run", run_with_metadata)
    overrides = {stage: {"provider": "chatgpt_oauth", "model": "test-model", "effort": "high"} for stage in separate_stages}
    assert api.put("/ai/pipeline", json={"overrides": overrides}).status_code == 200
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    wait_for_preparation(api, application_id, "completed")
    assert [kwargs["effort"] for _, kwargs in app.state.fake_codex.runs] == ["medium", *(["high"] * len(separate_stages))]
    event = next(event for event in api.get(f"/applications/{application_id}/events").json() if event["event_type"] == "CODEX_PREPARATION")
    metadata = json.loads(event["details"])
    assert metadata["thread_id"] == "thread-1" and metadata["turn_id"] == "turn-1"


@pytest.mark.parametrize("fails", [False, True])
def test_search_provider_is_closed_after_test_and_discovery(tmp_path, fails):
    api, app = client(tmp_path, discovery())
    providers = []
    class ClosingSearch(FakeSearchProvider):
        def __init__(self):
            super().__init__()
            self.closed = False
        def search(self, *args, **kwargs):
            if fails:
                raise SearchProviderUnavailableError("Recherche indisponible")
            return super().search(*args, **kwargs)
        def close(self):
            self.closed = True
    def factory(_):
        provider = ClosingSearch()
        providers.append(provider)
        return provider
    app.state.search_provider_factory = factory
    assert api.post("/search/test", json={"mode": "SEARXNG"}).status_code == (503 if fails else 200)
    api.post("/codex/discover")
    assert len(providers) == 2 and all(provider.closed for provider in providers)


def test_lifespan_closes_internal_auth_only(tmp_path, monkeypatch):
    auths = []
    class ClosingAuth(FakeAuth):
        closed = False
        def close(self):
            self.closed = True
    def factory():
        value = ClosingAuth()
        auths.append(value)
        return value
    monkeypatch.setattr("app.main.ChatGPTAuth", factory)
    monkeypatch.setattr("app.main.warm_up_libreoffice", lambda: None)
    with TestClient(create_app(tmp_path / "internal.db", tmp_path / "applications")):
        assert not auths[0].closed
    assert auths[0].closed
    external = ClosingAuth()
    with TestClient(create_app(tmp_path / "external.db", tmp_path / "applications", auth=external)):
        pass
    assert not external.closed


@pytest.mark.parametrize("postal_address", [None, "", "Paris", "Paris, France", "10 rue du Test"])
def test_prepare_succeeds_without_complete_company_postal_address(tmp_path, postal_address):
    repo = isolated_repo(tmp_path, with_variants=True)
    payload = preparation()
    payload["company_profile"]["postal_address"] = postal_address
    api, _ = client(tmp_path, payload, repo_root=repo)
    application_id = shortlist(api)

    api.post(f"/applications/{application_id}/prepare-with-codex")
    result = wait_for_preparation(api, application_id, "completed")

    assert result["status"] == "PREPARED"
    assert result["company_postal_address"] is None
    assert result["missing_information"] == [
        {"field": "COMPANY_ADDRESS", "label": "Company address"},
        {"field": "COMPANY_ZIP_CODE", "label": "Company postal code"},
    ]
    assert result["artifacts"] == {"complete": True, "missing": [], "invalid": []}
    letter = next((tmp_path / "applications").rglob("lettre-motivation.docx"))
    assert "{{" not in document_text(letter)
    event = next(item for item in api.get(f"/applications/{application_id}/events").json() if item["event_type"] == "CODEX_PREPARATION")
    assert json.loads(event["details"])["missing_information"] == result["missing_information"]
    assert api.post(f"/applications/{application_id}/validate").json()["status"] == "AWAITING_VALIDATION"


def test_prepare_reuse_resolves_catalog_variant_without_llm_source_path(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    payload = preparation("REUSE")
    payload["cv"]["source_path"] = None
    payload["cv"]["project_set"] = ["unknown-a", "unknown-b", "unknown-c"]
    payload["cv"]["project_order"] = ["unknown-a", "unknown-b", "unknown-c"]
    api, _ = client(tmp_path, payload, repo_root=repo)
    application_id = shortlist(api)

    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    result = wait_for_preparation(api, application_id, "completed")
    event = next(item for item in api.get(f"/applications/{application_id}/events").json() if item["event_type"] == "CODEX_PREPARATION")

    assert result["cv_action"] == "REUSE"
    assert Path(json.loads(event["details"])["cv_source"]) == repo / "01_Career/CV/Generated/resume_A.docx"


def test_prepare_reuse_ignores_incorrect_llm_source_path(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    payload = preparation("REUSE", "01_Career/CV/Generated/not-the-selected-variant.docx")
    api, _ = client(tmp_path, payload, repo_root=repo)
    application_id = shortlist(api)

    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    assert wait_for_preparation(api, application_id, "completed")["cv_action"] == "REUSE"


def test_prepare_rejects_unknown_cv_variant(tmp_path):
    payload = preparation("REUSE")
    payload["cv"]["source_variant"] = "UNKNOWN"
    api, _ = client(tmp_path, payload)
    application_id = shortlist(api)

    api.post(f"/applications/{application_id}/prepare-with-codex")
    failed = wait_for_preparation(api, application_id, "failed")

    assert "Source resume missing from variant catalog" in failed["preparation_error"]


def test_missing_cover_letter_template_keeps_shortlisted(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    cv = repo / "01_Career/CV/Generated/resume_A.docx"
    (repo / "01_Career/Cover Letter/Templates/lettre-motivation-template.docx").unlink()
    api, _ = client(tmp_path, preparation("REUSE", str(cv)), repo_root=repo)
    application_id = shortlist(api)

    api.post(f"/applications/{application_id}/prepare-with-codex")
    failed = wait_for_preparation(api, application_id, "failed")

    assert failed["status"] == "SHORTLISTED"
    assert "COVER_LETTER_TEMPLATE_INVALID" in failed["preparation_error"]


def test_prepare_codex_error_keeps_shortlisted(tmp_path):
    api, _ = client(tmp_path, error=CodexError("failure"))
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    assert wait_for_preparation(api, application_id, "failed")["status"] == "SHORTLISTED"


def test_prepare_adapt_creates_valid_offer_cv_without_changing_source(tmp_path):
    cv = next(CV_ROOT.glob("*.docx"))
    source_hash = sha256(cv)
    variants_before = {path.name for path in CV_ROOT.glob("*.docx")}
    api, _ = client(tmp_path, preparation("ADAPT", str(cv)))
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    result = wait_for_preparation(api, application_id, "completed")
    assert sha256(cv) == source_hash
    assert {path.name for path in CV_ROOT.glob("*.docx")} == variants_before
    generated = next((tmp_path / "applications").rglob("CV_Example_Stage_IA.docx"))
    assert_valid_docx(generated)
    assert_no_source_leak(document_text(generated))
    assert result["cv_action"] == "ADAPT"
    assert result["cv_path"] == str(generated.resolve())


def test_prepare_adapt_replaces_partial_cv_from_failed_attempt(tmp_path):
    cv = next(CV_ROOT.glob("*.docx"))
    api, _ = client(tmp_path, preparation("ADAPT", str(cv)))
    application_id = shortlist(api)
    stale = tmp_path / "applications" / f"example-stage-ia-{application_id}" / "cv" / "CV_Example_Stage_IA.docx"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"partial")

    api.post(f"/applications/{application_id}/prepare-with-codex")
    result = wait_for_preparation(api, application_id, "completed")

    assert result["status"] == "PREPARED"
    assert_valid_docx(stale)


def test_prepare_create_uses_template_and_registers_new_global_variant(tmp_path):
    repo = isolated_repo(tmp_path)
    generated_root = repo / "01_Career/CV/Generated"
    template = repo / "01_Career/CV/Templates/resume-template.docx"
    template_hash = sha256(template)
    payload = preparation("CREATE")
    payload["cv"]["content"]["profile"] += " [Source locale : 01_Career/Profil.md]"
    payload["cv"]["content"]["projects"][0]["bullets"][0] += " [Source: 02_Projects/Search Prototype/Search Prototype.md]"
    api, _ = client(tmp_path, payload, repo_root=repo)
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    result = wait_for_preparation(api, application_id, "completed")
    assert sha256(template) == template_hash
    variant = generated_root / "resume_A.docx"
    assert_valid_docx(variant)
    assert payload["cv"]["reusable_content"]["headline"] in document_text(variant).splitlines()
    assert payload["cv"]["content"]["headline"] not in document_text(variant).splitlines()
    assert "1. Search Prototype" in (repo / "01_Career/CV/CV_Variants.md").read_text(encoding="utf-8")
    generated = next((tmp_path / "applications").rglob("CV_Example_Stage_IA.docx"))
    assert_valid_docx(generated)
    assert_no_source_leak(document_text(generated))
    assert result["cv_action"] == "CREATE"
    event = next(item for item in api.get(f"/applications/{application_id}/events").json() if item["event_type"] == "CODEX_PREPARATION")
    details = json.loads(event["details"])
    assert details["cv_generated_variant"] == "A"
    assert set(details["cv_project_set"]) == {"search-prototype", "planning-prototype", "image-classifier"}
    folder = next((tmp_path / "applications").iterdir())
    assert not (folder / "application.md").exists()


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_prepare_uses_up_to_four_projects_for_cv_and_letter(tmp_path, count):
    repo = isolated_repo(tmp_path)
    ids = ["planning-prototype", "search-prototype", "model-experiment", "document-workflow"][:count]
    payload = preparation("CREATE", project_ids=ids)
    title = payload["cv"]["content"]["projects"][-1]["title"]
    payload["cover_letter"]["paragraph_3"] = f"Mon travail sur {title} illustre ma démarche d’expérimentation et de validation."
    api, app = client(tmp_path, payload, repo_root=repo)
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    result = wait_for_preparation(api, application_id, "completed")

    catalog = project_catalog(repo)
    assert cv_project_order(Path(result["cv_path"]), catalog) == ids
    variant = cv_variants(repo)["A"]
    assert variant["project_order"] == cv_project_order(variant["path"], catalog) == ids
    letter_path = Path(result["cover_letter_path"])
    paragraphs = read_cover_letter_docx(letter_path).paragraphs
    assert paragraphs[2] == payload["cover_letter"]["paragraph_3"]
    event = next(item for item in api.get(f"/applications/{application_id}/events").json() if item["event_type"] == "CODEX_PREPARATION")
    assert json.loads(event["details"])["cv_project_order"] == ids
    prompt = app.state.fake_codex.runs[-1][0][0]
    assert "Sélectionne les 4 projets exploitables les plus pertinents" in prompt
    assert "Si moins de 4 projets pertinents sont documentés" in prompt
    assert "Utilise la sélection commune cv.project_order" in prompt

    assert api.patch(f"/applications/{application_id}", json={
        "company_postal_address": "99 rue Manuelle\n75002 Paris",
    }).status_code == 200
    assert read_cover_letter_docx(letter_path).paragraphs == paragraphs
    assert "99 rue Manuelle" in document_text(letter_path)


@pytest.mark.parametrize("action", ["REUSE", "ADAPT"])
def test_preparation_regenerates_cv_and_letter_with_four_projects(tmp_path, action):
    repo = isolated_repo(tmp_path)
    ids = ["planning-prototype", "search-prototype", "model-experiment", "document-workflow"]
    api, app = client(tmp_path, preparation("CREATE", project_ids=ids), repo_root=repo)
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    first = wait_for_preparation(api, application_id, "completed")
    variant = cv_variants(repo)["A"]["path"]
    original = variant.read_bytes()
    order = ids if action == "REUSE" else list(reversed(ids))
    payload = preparation(action, project_ids=order)
    payload["cover_letter"]["paragraph_3"] = "Document Workflow illustre ma capacité à structurer un travail technique documenté."
    app.state.fake_codex.response = payload
    assert api.post(f"/applications/{application_id}/status", json={"status": "SHORTLISTED"}).status_code == 200
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    regenerated = wait_for_preparation(api, application_id, "completed")

    assert regenerated["cv_action"] == action
    assert cv_project_order(Path(regenerated["cv_path"]), project_catalog(repo)) == order
    assert read_cover_letter_docx(Path(regenerated["cover_letter_path"])).paragraphs[2] == payload["cover_letter"]["paragraph_3"]
    assert variant.read_bytes() == original
    assert len(cv_variants(repo)) == 1
    assert regenerated["cover_letter_path"] == first["cover_letter_path"]


def test_preparation_regenerates_three_project_reuse_as_four_project_create(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    original = cv_variants(repo)["A"]["path"].read_bytes()
    api, app = client(tmp_path, preparation("REUSE"), repo_root=repo)
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    first = wait_for_preparation(api, application_id, "completed")
    ids = ["search-prototype", "document-workflow", "model-experiment", "planning-prototype"]
    payload = preparation("CREATE", project_ids=ids)
    payload["cover_letter"]["paragraph_3"] = "Planning Prototype illustre ma capacité à conduire un travail technique documenté."
    app.state.fake_codex.response = payload
    assert api.post(f"/applications/{application_id}/status", json={"status": "SHORTLISTED"}).status_code == 200
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    regenerated = wait_for_preparation(api, application_id, "completed")

    cv_path = Path(regenerated["cv_path"])
    assert regenerated["cv_action"] == "CREATE"
    assert regenerated["cv_path"] == first["cv_path"]
    assert len(list(cv_path.parent.glob("*.docx"))) == 1
    assert cv_project_order(cv_path, project_catalog(repo)) == ids
    assert read_cover_letter_docx(Path(regenerated["cover_letter_path"])).paragraphs[2] == payload["cover_letter"]["paragraph_3"]
    variants = cv_variants(repo)
    assert set(variants) == {"A", "B"}
    assert variants["B"]["project_order"] == ids
    assert variants["A"]["path"].read_bytes() == original


def test_prepare_rejects_more_than_four_projects(tmp_path):
    repo = isolated_repo(tmp_path)
    ids = ["planning-prototype", "search-prototype", "model-experiment", "document-workflow", "image-classifier"]
    api, _ = client(tmp_path, preparation("CREATE", project_ids=ids), repo_root=repo)
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    failed = wait_for_preparation(api, application_id, "failed")
    assert failed["status"] == "SHORTLISTED"
    assert "cv.project_set" in failed["preparation_error"]
    assert not cv_variants(repo)
    assert failed["cv_path"] is None and failed["cover_letter_path"] is None


def test_prepare_accepts_targeted_project_title_for_selected_project_id(tmp_path):
    repo = isolated_repo(tmp_path)
    project = repo / "02_Projects/Network Planning Experiment/project.md"
    project.parent.mkdir(parents=True)
    project.write_text("# Network Planning Experiment\n", encoding="utf-8")
    payload = preparation("CREATE")
    project_id = "network-planning-experiment"
    payload["cv"]["project_set"][2] = project_id
    payload["cv"]["project_order"][2] = project_id
    for content in (payload["cv"]["content"], payload["cv"]["reusable_content"]):
        content["projects"][2]["project_id"] = project_id
        content["projects"][2]["title"] = "Network Planning Experiment – Allocation sous contraintes"

    api, _ = client(tmp_path, payload, repo_root=repo)
    application_id = shortlist(api)
    api.post(f"/applications/{application_id}/prepare-with-codex")
    result = wait_for_preparation(api, application_id, "completed")

    assert result["status"] == "PREPARED"
    event = next(
        item for item in api.get(f"/applications/{application_id}/events").json()
        if item["event_type"] == "CODEX_PREPARATION"
    )
    details = json.loads(event["details"])
    assert details["cv_project_order"] == ["search-prototype", "planning-prototype", project_id]


def test_create_new_set_is_reused_without_duplicate_variant(tmp_path):
    repo = isolated_repo(tmp_path)
    api, app = client(tmp_path, preparation("CREATE"), repo_root=repo)
    first_id = shortlist(api)
    api.post(f"/applications/{first_id}/prepare-with-codex")
    assert wait_for_preparation(api, first_id, "completed")["cv_action"] == "CREATE"

    variant = repo / "01_Career/CV/Generated/resume_A.docx"
    second = preparation("ADAPT", str(variant))
    ids = ["search-prototype", "planning-prototype", "image-classifier"]
    second["cv"]["source_variant"] = "A"
    second["cv"]["project_set"] = ids
    second["cv"]["project_order"] = list(reversed(ids))
    second["cv"]["content"] = cv_content(list(reversed(ids)))
    app.state.fake_codex.response = second
    created = api.post("/applications", json={
        "company": "Example", "position": "Stage IA", "url": "https://example.test/job-2",
    }).json()
    api.post(f"/applications/{created['id']}/status", json={"status": "SHORTLISTED"})
    api.post(f"/applications/{created['id']}/prepare-with-codex")
    result = wait_for_preparation(api, created["id"], "completed")

    assert result["cv_action"] == "ADAPT"
    assert len(list((repo / "01_Career/CV/Generated").glob("*.docx"))) == 1


def test_prepare_rejects_adapt_that_changes_source_project_set(tmp_path):
    cv = CV_ROOT / "resume_A.docx"
    payload = preparation("ADAPT", str(cv))
    ids = ["search-prototype", "document-workflow", "planning-prototype"]
    payload["cv"]["project_set"] = ids
    payload["cv"]["project_order"] = ids
    payload["cv"]["content"] = cv_content(ids)
    api, _ = client(tmp_path, payload)
    application_id = shortlist(api)
    api.post(f"/applications/{application_id}/prepare-with-codex")
    failed = wait_for_preparation(api, application_id, "failed")
    assert "must preserve the source project_set" in failed["preparation_error"]


def test_create_with_known_set_is_rejected_without_duplicate(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    ids = ["model-experiment", "search-prototype", "document-workflow"]
    payload = preparation("CREATE")
    payload["cv"].update(
        project_set=ids, project_order=ids, content=cv_content(ids),
        reusable_content=cv_content(ids, generic=True),
    )
    api, _ = client(tmp_path, payload, repo_root=repo)
    application_id = shortlist(api)
    api.post(f"/applications/{application_id}/prepare-with-codex")
    failed = wait_for_preparation(api, application_id, "failed")
    assert "project_set already exists" in failed["preparation_error"]
    assert len(list((repo / "01_Career/CV/Generated").glob("*.docx"))) == 1


def test_preparation_keeps_validated_sources_internal_and_cleans_markdown(tmp_path):
    cv = next(CV_ROOT.glob("*.docx"))
    payload = preparation("ADAPT", str(cv), sources=["01_Career/Profil.md", "02_Projects/Search Prototype/Search Prototype.md"])
    for field in ("analysis_markdown", "interview_prep_markdown"):
        payload[field] += " [Source locale : 01_Career/Profil.md]"
    payload["company_profile"]["description"] += " [Source locale : 01_Career/Profil.md]"
    payload["cover_letter"]["paragraph_1"] += " [Source locale : 01_Career/Profil.md]"
    payload["cv"]["content"]["skill_groups"][0]["items"][0] += " [Source: 02_Projects/Search Prototype/Search Prototype.md]"
    api, _ = client(tmp_path, payload)
    application_id = shortlist(api)
    api.post(f"/applications/{application_id}/prepare-with-codex")
    assert wait_for_preparation(api, application_id, "completed")["status"] == "PREPARED"
    folder = next((tmp_path / "applications").iterdir())
    for markdown in folder.glob("*.md"):
        assert_no_source_leak(markdown.read_text(encoding="utf-8"))
    assert_no_source_leak(document_text(next((folder / "cv").glob("*.docx"))))
    event = next(item for item in api.get(f"/applications/{application_id}/events").json() if item["event_type"] == "CODEX_PREPARATION")
    assert json.loads(event["details"])["local_sources"] == payload["local_sources"]


def test_artifact_cleaner_removes_only_annotations_and_rejects_bare_paths():
    assert clean_artifact_text("Python [Source locale : 02_Projects/X.md]") == "Python"
    assert clean_artifact_text("Texte [Source: 01_Career/Profil.md]") == "Texte"
    with pytest.raises(ValueError, match="Knowledge Base reference"):
        clean_artifact_text("Voir 03_Knowledge/Machine Learning/Test.md")


def test_prepare_invalid_adapt_keeps_shortlisted(tmp_path):
    cv = next(CV_ROOT.glob("*.docx"))
    api, _ = client(tmp_path, preparation("ADAPT", str(cv), content=None))
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    assert wait_for_preparation(api, application_id, "failed")["status"] == "SHORTLISTED"


def test_cv_decisions_accept_legitimate_nullable_and_empty_lists():
    cv = str(next(CV_ROOT.glob("*.docx")))
    reuse = PreparationOutput.model_validate(preparation("REUSE", cv))
    adapt = PreparationOutput.model_validate(preparation("ADAPT", cv))
    create = PreparationOutput.model_validate(preparation("CREATE"))
    assert reuse.cv.target_filename is None and reuse.cv.content is None
    assert reuse.cv.changes == adapt.cv.changes == create.cv.missing_fit == []


@pytest.mark.parametrize("action,field,value,message", [
    ("REUSE", "source_variant", None, "REUSE"),
    ("ADAPT", "target_filename", None, "ADAPT"),
    ("CREATE", "source_variant", "A", "CREATE"),
    ("ADAPT", "target_filename", " ", "String must not be blank"),
])
def test_cv_decision_rejects_incoherent_action(action, field, value, message):
    cv = str(next(CV_ROOT.glob("*.docx")))
    payload = preparation(action, None if action == "CREATE" else cv)
    payload["cv"][field] = value
    with pytest.raises(ValueError, match=message):
        PreparationOutput.model_validate(payload)


@pytest.mark.parametrize("mutation,field", [
    (lambda payload: payload["cv"].update(action="UNKNOWN"), "cv.action"),
    (lambda payload: payload["cv"].pop("justification"), "cv.justification"),
    (lambda payload: payload["cv"].update(changes="invalid"), "cv.changes"),
    (lambda payload: payload["company_profile"].update(description=""), "company_profile.description"),
    (lambda payload: payload["company_profile"].update(sources=["invalid"]), "company_profile.sources"),
    (lambda payload: payload["company_profile"].update(postal_address=42), "company_profile.postal_address"),
])
def test_invalid_response_keeps_detailed_field_error(tmp_path, mutation, field):
    cv = next(CV_ROOT.glob("*.docx"))
    payload = preparation("REUSE", str(cv))
    payload["company_profile"]["postal_address"] = "Paris, France"
    mutation(payload)
    api, _ = client(tmp_path, payload)
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    failed = wait_for_preparation(api, application_id, "failed")
    assert failed["status"] == "SHORTLISTED"
    assert field in failed["preparation_error"]
    assert "model=test-model" in failed["preparation_error"]
    assert "effort=medium" in failed["preparation_error"]


def test_local_source_accepts_windows_separator_and_rejects_unknown():
    validate_local_sources(CV_ROOT.parents[2], [r"01_Career\Profil.md"])
    with pytest.raises(ValueError, match="Local source not found"):
        validate_local_sources(CV_ROOT.parents[2], [r"02_Projects\inconnue.md"])


def test_prepare_requires_valid_kb_sources(tmp_path):
    cv = next(CV_ROOT.glob("*.docx"))
    api, _ = client(tmp_path, preparation(source_path=str(cv), sources=["03_Knowledge/missing.md"]))
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    failed = wait_for_preparation(api, application_id, "failed")
    assert "local_sources" in failed["preparation_error"]
    assert "Local source not found" in failed["preparation_error"]


def test_prepare_only_from_shortlisted(tmp_path):
    cv = next(CV_ROOT.glob("*.docx"))
    api, _ = client(tmp_path, preparation(source_path=str(cv)))
    created = api.post("/applications", json={"company": "X", "position": "Y", "url": "https://x.test/y"}).json()
    assert api.post(f"/applications/{created['id']}/prepare-with-codex").status_code == 409


def test_logout_disconnects(tmp_path):
    api, _ = client(tmp_path)
    api.post("/auth/chatgpt/logout")
    assert api.get("/auth/chatgpt/status").json()["connected"] is False


def test_detected_is_counted_in_search_phase(tmp_path):
    api, _ = client(tmp_path)
    api.post("/applications", json={"company": "Example", "position": "Stage", "url": "https://example.test/search"})
    assert api.get("/stats").json()["phases"]["search"] == 1


def test_select_runs_codex_and_reaches_prepared(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    api, app = client(tmp_path, preparation(), repo_root=repo)
    created = api.post("/applications", json={"company": "Example", "position": "Stage", "url": "https://example.test/select"}).json()
    result = api.post(f"/applications/{created['id']}/select")
    assert result.status_code == 200 and result.json()["preparation_state"] == "running"
    wait_for_preparation(api, created["id"], "completed")
    assert app.state.fake_codex.calls == 1
    events = [event["event_type"] for event in api.get(f"/applications/{created['id']}/events").json()]
    assert "SHORTLISTED" in events and "PREPARATION_STARTED" in events and "PREPARED" in events


def test_select_screening_uncertainty_runs_preparation_and_keeps_warning(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    api, app = client(tmp_path, {"decisions": [
        screening_decision(eligibility_status="ELIGIBILITY_UNCERTAIN"),
    ]}, repo_root=repo)
    assert api.post("/codex/discover").status_code == 200
    application = api.get("/applications").json()[0]
    application_id = application["id"]
    assert application["status"] == "DETECTED"
    assert application["eligibility_status"] == "ELIGIBILITY_UNCERTAIN"
    warning = "Eligibility determined by job screening."
    assert application["eligibility_reason"] == warning
    app.state.fake_codex.response = preparation()

    selected = api.post(f"/applications/{application_id}/select")
    assert selected.status_code == 200
    assert selected.json()["status"] == "SHORTLISTED"
    assert selected.json()["preparation_state"] == "running"
    prepared = wait_for_preparation(api, application_id, "completed")
    assert prepared["status"] == "PREPARED"
    assert prepared["eligibility_status"] == "ELIGIBILITY_UNCERTAIN"
    assert prepared["eligibility_reason"] == warning
    assert app.state.fake_codex.calls == 2
    events = [event["event_type"] for event in api.get(f"/applications/{application_id}/events").json()]
    assert "SHORTLISTED" in events and "PREPARATION_STARTED" in events and "PREPARED" in events
    assert api.post(f"/applications/{application_id}/validate").json()["status"] == "AWAITING_VALIDATION"


@pytest.mark.parametrize("description", ["", "Travaux soumis à une habilitation Défense."])
def test_select_failure_stays_shortlisted_and_retry_succeeds(tmp_path, description):
    repo = isolated_repo(tmp_path, with_variants=True)
    api, app = client(tmp_path, error=CodexError("temporary failure"), repo_root=repo)
    created = api.post("/applications", json={
        "company": "Example", "position": "Stage", "url": "https://example.test/retry",
        "description": description,
    }).json()
    assert api.post(f"/applications/{created['id']}/select").status_code == 200
    failed = wait_for_preparation(api, created["id"], "failed")
    assert failed["status"] == "SHORTLISTED" and failed["preparation_state"] == "failed"
    assert "temporary failure" in failed["preparation_error"]
    app.state.fake_codex.error = None
    app.state.fake_codex.response = preparation()
    app.state.fake_codex.response["company_profile"]["postal_address"] = "Paris, France"
    assert api.post(f"/applications/{created['id']}/prepare-with-codex").status_code == 200
    prepared = wait_for_preparation(api, created["id"], "completed")
    assert prepared["status"] == "PREPARED"
    assert prepared["company_postal_address"] is None


def test_double_select_runs_one_preparation(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    api, app = client(tmp_path, preparation(), repo_root=repo)
    created = api.post("/applications", json={"company": "Example", "position": "Stage", "url": "https://example.test/double"}).json()
    assert api.post(f"/applications/{created['id']}/select").status_code == 200
    assert api.post(f"/applications/{created['id']}/select").status_code == 409
    wait_for_preparation(api, created["id"], "completed")
    assert app.state.fake_codex.calls == 1


@pytest.mark.parametrize("fail_a", [False, True])
def test_preparation_queue_runs_two_jobs_fifo_and_releases_slot(tmp_path, fail_a):
    repo = isolated_repo(tmp_path, with_variants=True)
    fake = ControlledCodex(preparation(), fail={"A"} if fail_a else set())
    app = create_app(tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: fake, repo_root=repo)
    api = TestClient(app)
    assert api.put("/codex/model", json={"model": "test-model"}).status_code == 200
    ids = {}
    for name in ("A", "B", "C"):
        ids[name] = api.post("/applications", json={
            "company": "Example", "position": name, "url": f"https://example.test/{name.lower()}",
        }).json()["id"]

    assert api.post(f"/applications/{ids['A']}/select").json()["preparation_state"] == "running"
    assert api.post(f"/applications/{ids['B']}/select").json()["preparation_state"] == "running"
    assert api.post(f"/applications/{ids['C']}/select").json()["preparation_state"] == "queued"
    assert api.post(f"/applications/{ids['A']}/prepare-with-codex").status_code == 409
    assert api.post(f"/applications/{ids['C']}/prepare-with-codex").status_code == 409
    fake.wait_started("A")
    fake.wait_started("B")
    assert fake.max_active == 2
    assert api.get(f"/applications/{ids['C']}").json()["preparation_state"] == "queued"

    fake.releases["A"].set()
    fake.wait_started("C")
    expected_state, expected_status = ("failed", "SHORTLISTED") if fail_a else ("completed", "PREPARED")
    assert wait_for_preparation(api, ids["A"], expected_state)["status"] == expected_status
    assert api.get(f"/applications/{ids['B']}").json()["preparation_state"] == "running"
    assert api.get(f"/applications/{ids['C']}").json()["preparation_state"] == "running"

    fake.releases["B"].set()
    fake.releases["C"].set()
    prepared_b = wait_for_preparation(api, ids["B"], "completed")
    prepared_c = wait_for_preparation(api, ids["C"], "completed")
    assert set(fake.started[:2]) == {"A", "B"} and fake.started[2] == "C"
    assert fake.max_active <= 2
    assert Path(prepared_b["cv_path"]).parent.parent != Path(prepared_c["cv_path"]).parent.parent


def test_backend_restart_marks_queued_preparation_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.warm_up_libreoffice", lambda: None)
    database = tmp_path / "tracker.db"
    first = TestClient(create_app(database, tmp_path / "applications", FakeAuth(), lambda _: FakeCodex()))
    application_id = shortlist(first)
    with connect(database) as db:
        add_event(db, application_id, "PREPARATION_QUEUED", "queued")
        db.commit()

    with TestClient(create_app(database, tmp_path / "applications", FakeAuth(), lambda _: FakeCodex())) as restarted:
        application = restarted.get(f"/applications/{application_id}").json()
    assert application["status"] == "SHORTLISTED"
    assert application["preparation_state"] == "failed"
    assert "restart" in application["preparation_error"]


def test_validate_then_confirm_send_requires_user_actions(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    api, _ = client(tmp_path, preparation(), repo_root=repo)
    created = api.post("/applications", json={"company": "Example", "position": "Stage", "url": "https://example.test/send"}).json()
    application_id = created["id"]
    api.post(f"/applications/{application_id}/select")
    wait_for_preparation(api, application_id, "completed")
    assert api.post(f"/applications/{application_id}/submission/confirm").status_code == 409
    assert api.post(f"/applications/{application_id}/validate").json()["status"] == "AWAITING_VALIDATION"
    assert api.post(f"/applications/{application_id}/submission/confirm").json()["status"] == "SENT"
    assert api.get("/stats").json()["phases"]["tracking"] == 1
