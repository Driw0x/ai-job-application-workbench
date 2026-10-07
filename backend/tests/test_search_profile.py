from pathlib import Path
import json
import os

import httpx
import pytest

from fastapi.testclient import TestClient

from app.codex_client import CodexClient, CodexOutput
from app.codex_workflows import discovery_prompt, discovery_context, knowledge_base_files
from app.main import create_app, connect
from app.search_providers import SearchResult


class FakeAuth:
    def status(self):
        return {"connected": True, "account": "test@example.test"}

    def access_token(self):
        return "token"


class FakeCodex:
    def __init__(self):
        self.prompts = []

    def models(self):
        return [{
            "model": "test-model", "displayName": "Test", "isDefault": True,
            "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
        }]

    def run(self, prompt, *_args, **_kwargs):
        self.prompts.append(prompt)
        return CodexOutput({"decisions": [{
            "candidate_id": "candidate_001", "decision": "KEEP", "reason_code": "KEEP_RELEVANT",
            "eligibility_status": "ELIGIBLE", "reason": None, "offer": {
                "company": "Example", "title": "Stage ML", "url": "https://jobs.example.test/1",
                "location": "Lyon", "contract_type": "Stage", "published_at": None,
                "start_date": "mars", "availability": "open", "source": "Career website",
                "why_relevant": ["Machine learning à Lyon"],
                "evidence_urls": ["https://jobs.example.test/1"],
            },
        }]}, {"web_search_calls": 1, "web_domains": ["jobs.example.test"]})


class FakeSearchProvider:
    def __init__(self):
        self.queries = []

    def search(self, query, limit=20):
        self.queries.append(query)
        return [SearchResult("Stage ML", "https://jobs.example.test/1", "Stage ML", "test", query)]

    def test(self):
        pass


def make_client(tmp_path: Path, repo_root: Path):
    codex = FakeCodex()
    app = create_app(
        tmp_path / "tracker.db", tmp_path / "applications", FakeAuth(), lambda _: codex,
        search_provider_factory=lambda _: FakeSearchProvider(),
        offer_http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="<h1>Stage ML</h1>", request=request),
        )),
        offer_resolver=lambda host, port, **_: [(2, 1, 6, "", ("93.184.216.34", port))],
        repo_root=repo_root,
    )
    client = TestClient(app)
    assert client.put("/codex/model", json={"model": "test-model"}).status_code == 200
    return client, codex


def create_kb(root: Path) -> None:
    career = root / "01_Career"
    career.mkdir(parents=True)
    for name, content in {
        "Profil.md": "PROFIL_KB_TEST", "Domaines.md": "DOMAINES_KB_TEST",
        "Stage M2.md": "STAGE_KB_TEST",
    }.items():
        (career / name).write_text(content, encoding="utf-8")


def test_knowledge_base_mode_is_default_and_used(tmp_path):
    repo = tmp_path / "repo"
    create_kb(repo)
    client, codex = make_client(tmp_path, repo)

    profile = client.get("/settings/search-profile").json()
    assert profile["mode"] == "knowledge_base"
    assert profile["knowledge_base_available"] is True
    assert profile["knowledge_base_sources"] == ["Profile", "Domains", "Career objective"]
    assert client.post("/codex/discover").status_code == 200
    assert all(marker in codex.prompts[-1] for marker in ("PROFIL_KB_TEST", "DOMAINES_KB_TEST", "STAGE_KB_TEST"))
    assert client.get("/codex/discovery/latest").json()["profile_mode"] == "knowledge_base"


def test_custom_prompt_crud_persistence_and_discovery_without_career_data(tmp_path):
    repo = tmp_path / "empty-repo"
    prompt = "Je recherche un stage de 6 mois en machine learning ou NLP à Lyon, à partir de mars."
    client, codex = make_client(tmp_path, repo)

    initial = client.get("/settings/search-profile").json()
    assert initial["mode"] == "custom_prompt"
    assert initial["custom_search_prompt"] == ""
    missing = client.post("/codex/discover")
    assert missing.status_code == 409
    assert missing.json()["detail"] == "Define your search profile first."

    saved = client.put("/settings/search-profile", json={
        "mode": "custom_prompt", "custom_search_prompt": prompt,
    }).json()
    assert saved["custom_search_prompt"] == prompt
    assert saved["custom_search_prompt_updated_at"]
    assert client.post("/codex/discover").status_code == 200
    sent = codex.prompts[-1]
    assert "SEARCH PROFILE PROVIDED BY USER" in sent and prompt in sent
    assert "PROFIL_KB_TEST" not in sent and "CONTEXTE CANDIDAT ISSU" not in sent
    assert "Example University" not in sent and "Computer Science" not in sent

    modified = prompt.replace("Lyon", "Grenoble")
    assert client.put("/settings/search-profile", json={
        "mode": "custom_prompt", "custom_search_prompt": modified,
    }).json()["custom_search_prompt"] == modified
    reloaded, _ = make_client(tmp_path, repo)
    assert reloaded.get("/settings/search-profile").json()["custom_search_prompt"] == modified
    cleared = reloaded.delete("/settings/search-profile/prompt").json()
    assert cleared["mode"] == "custom_prompt" and cleared["custom_search_prompt"] == ""
    assert reloaded.post("/codex/discover").status_code == 409


@pytest.mark.parametrize("kb_state", ["valid", "missing", "inaccessible"])
def test_custom_mode_never_reads_career_and_mode_persists_with_kb(tmp_path, monkeypatch, kb_state):
    repo = tmp_path / "repo"
    create_kb(repo)
    client, codex = make_client(tmp_path, repo)
    prompt = "Recherche uniquement des stages en optimisation à Toulouse."
    class IsolatedCodex(FakeCodex, CodexClient):
        def run(self, prompt, *args, **kwargs):
            if kwargs.get("isolated_cwd") is not None:
                directory = Path(kwargs["isolated_cwd"])
                assert directory.is_dir() and not list(directory.iterdir())
                assert not directory.is_relative_to(repo)
                self.isolated_cwd = directory
            else:
                assert "PROFIL_KB_TEST" in prompt
            return super().run(prompt, *args, **kwargs)

    codex = IsolatedCodex()
    client.app.state.codex_client = codex
    initial = client.get("/settings/search-profile").json()
    if kb_state == "missing":
        with connect(client.app.state.db_path) as db:
            db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('knowledge_base_root', ?)", (str(tmp_path / "missing"),))
            db.commit()

    def forbidden(*args, **kwargs):
        raise AssertionError("Knowledge Base accessed in custom mode")

    with monkeypatch.context() as guarded:
        for name in ("knowledge_base_files", "knowledge_base_available", "knowledge_base_sources", "discovery_context"):
            guarded.setattr(f"app.main.{name}", forbidden)
        original_walk = os.walk
        def guarded_walk(root, *args, **kwargs):
            if Path(root).is_relative_to(repo):
                raise AssertionError("Knowledge Base directory traversed")
            return original_walk(root, *args, **kwargs)
        guarded.setattr("app.codex_workflows.os.walk", guarded_walk)
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(repo):
                raise AssertionError("Knowledge Base file opened")
            return original_open(path, *args, **kwargs)
        guarded.setattr(Path, "open", guarded_open)
        if kb_state == "inaccessible":
            original_stat = Path.stat
            def guarded_stat(path, *args, **kwargs):
                if path.is_relative_to(repo):
                    raise PermissionError("Knowledge Base inaccessible")
                return original_stat(path, *args, **kwargs)
            guarded.setattr(Path, "stat", guarded_stat)
        assert client.put("/settings/search-profile", json={
            "mode": "custom_prompt", "custom_search_prompt": prompt,
            "knowledge_base_root": str(tmp_path / "ignored"),
            "knowledge_base_selected_files": ["ignored.md"],
        }).status_code == 200
        profile = client.get("/settings/search-profile").json()
        assert profile["knowledge_base_files"] == []
        assert profile["knowledge_base_selected_files"] == []
        provider = FakeSearchProvider()
        client.app.state.search_provider_factory = lambda _: provider
        assert client.post("/codex/discover").status_code == 200
        assert not codex.isolated_cwd.exists()
        assert provider.queries == [prompt]
        assert prompt in codex.prompts[-1]
        for marker in ("PROFIL_KB_TEST", "DOMAINES_KB_TEST", "STAGE_KB_TEST", "Privilégie IA agentique", "Écarte BI/reporting"):
            assert marker not in codex.prompts[-1]
        reloaded, _ = make_client(tmp_path, repo)
        assert reloaded.get("/settings/search-profile").json()["mode"] == "custom_prompt"
    if kb_state == "valid":
        assert profile["knowledge_base_root"] == ""
        preview = client.get("/settings/search-profile?mode=knowledge_base").json()
        assert preview["knowledge_base_available"]
        assert client.get("/settings/search-profile").json()["mode"] == "custom_prompt"
        restored = client.put("/settings/search-profile", json={"mode": "knowledge_base"}).json()
        assert restored["knowledge_base_selected_files"] == initial["knowledge_base_selected_files"]
        assert restored["knowledge_base_root"] == initial["knowledge_base_root"]
        assert client.post("/codex/discover").status_code == 200
        assert "PROFIL_KB_TEST" in codex.prompts[-1]


def test_preparation_requires_candidate_profile_not_search_prompt(tmp_path):
    client, _ = make_client(tmp_path, tmp_path / "empty-repo")
    client.put("/settings/search-profile", json={
        "mode": "custom_prompt", "custom_search_prompt": "Stage ML à Lyon",
    })
    application = client.post("/applications", json={
        "company": "Example", "position": "Stage ML", "url": "https://example.test/job",
    }).json()
    response = client.post(f"/applications/{application['id']}/select")
    assert response.status_code == 409
    assert response.json()["detail"] == "A Knowledge Base or candidate profile is required to prepare an application."
    assert client.get(f"/applications/{application['id']}").json()["status"] == "DETECTED"


def test_blank_prompt_is_rejected(tmp_path):
    client, _ = make_client(tmp_path, tmp_path / "empty-repo")
    response = client.put("/settings/search-profile", json={
        "mode": "custom_prompt", "custom_search_prompt": "   ",
    })
    assert response.status_code == 422


def test_knowledge_base_search_uses_only_the_supplied_career_preferences():
    prompt = discovery_prompt("Seeking a technical writing role in healthcare", "knowledge_base")
    assert "Seeking a technical writing role in healthcare" in prompt
    assert "criteria explicitly documented in the supplied profile" in prompt
    for personal_preference in ("Privilégie IA agentique", "Île-de-France"):
        assert personal_preference not in prompt


def test_custom_search_keeps_user_defined_strategy():
    prompt = discovery_prompt("Stage ML à Lyon", "custom_prompt")

    assert "SEARCH PROFILE PROVIDED BY USER" in prompt
    assert "STRATÉGIE DE RECHERCHE ET DE CLASSEMENT Computer Science" not in prompt


def test_file_discovery_selection_persistence_and_exclusive_context(tmp_path):
    repo = tmp_path / "repo"
    create_kb(repo)
    project = repo / "02_Projects" / "Projet.md"
    project.parent.mkdir()
    project.write_text("PROJET_SELECTIONNE_TEST", encoding="utf-8")
    for name in (".git/cache.md", "tools/node_modules/readme.md", ".obsidian/config.md"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("IGNORED", encoding="utf-8")
    (repo / "notes.txt").write_text("not markdown", encoding="utf-8")
    client, codex = make_client(tmp_path, repo)
    initial = client.get("/settings/search-profile").json()
    assert initial["knowledge_base_selected_files"] == [
        "01_Career/Profil.md", "01_Career/Domaines.md", "01_Career/Stage M2.md",
    ]
    assert {item["path"] for item in initial["knowledge_base_files"]} == {
        *initial["knowledge_base_selected_files"], "02_Projects/Projet.md",
    }
    sources = ["01_Career/Domaines.md", "02_Projects/Projet.md"]
    saved = client.put("/settings/search-profile", json={
        "mode": "knowledge_base", "knowledge_base_selected_files": [*sources, "./01_Career/Domaines.md"],
    })
    assert saved.status_code == 200
    assert saved.json()["knowledge_base_selected_files"] == sources
    with connect(client.app.state.db_path) as db:
        value = db.execute("SELECT value FROM settings WHERE key='knowledge_base_selected_files'").fetchone()[0]
        assert json.loads(value) == sources
        assert "PROJET_SELECTIONNE_TEST" not in value
    reloaded, reloaded_codex = make_client(tmp_path, repo)
    assert reloaded.get("/settings/search-profile").json()["knowledge_base_selected_files"] == sources
    search_provider = FakeSearchProvider()
    reloaded.app.state.search_provider_factory = lambda _: search_provider
    assert reloaded.post("/codex/discover").status_code == 200
    assert search_provider.queries
    assert all("stage" not in query.lower() and "Paris" not in query for query in search_provider.queries)
    prompt = reloaded_codex.prompts[-1]
    assert "DOMAINES_KB_TEST" in prompt and "PROJET_SELECTIONNE_TEST" in prompt
    assert "PROFIL_KB_TEST" not in prompt and "STAGE_KB_TEST" not in prompt
    latest = reloaded.get("/codex/discovery/latest").json()
    assert "02_Projects/Projet.md" in latest["profile_summary"]
    assert "Stage M2" not in latest["profile_summary"]
    assert (repo / "01_Career/Stage M2.md").is_file()


@pytest.mark.parametrize("source", ["../outside.md", "/outside.md", "C:/outside.md", "C:outside.md", "01_Career/missing.md", "notes.txt"])
def test_invalid_source_is_rejected_without_changing_selection(tmp_path, source):
    repo = tmp_path / "repo"
    create_kb(repo)
    client, _ = make_client(tmp_path, repo)
    initial = client.get("/settings/search-profile").json()
    response = client.put("/settings/search-profile", json={
        "mode": "knowledge_base", "knowledge_base_selected_files": [source],
    })
    assert response.status_code in (400, 422)
    assert client.get("/settings/search-profile").json() == initial


def test_empty_selection_is_persisted_and_blocks_only_kb_search(tmp_path):
    repo = tmp_path / "repo"
    create_kb(repo)
    client, codex = make_client(tmp_path, repo)
    assert client.put("/settings/search-profile", json={
        "mode": "knowledge_base", "knowledge_base_selected_files": [],
    }).status_code == 200
    reloaded, _ = make_client(tmp_path, repo)
    assert reloaded.get("/settings/search-profile").json()["knowledge_base_selected_files"] == []
    response = client.post("/codex/discover")
    assert response.status_code == 409 and "at least one Knowledge Base source" in response.json()["detail"]
    assert not codex.prompts
    assert client.put("/settings/search-profile", json={
        "mode": "custom_prompt", "custom_search_prompt": "Emploi à Lyon",
    }).status_code == 200
    assert client.post("/codex/discover").status_code == 200
    assert "Emploi à Lyon" in codex.prompts[-1] and "PROFIL_KB_TEST" not in codex.prompts[-1]
    assert client.get("/settings/search-profile").json()["knowledge_base_selected_files"] == []


def test_kb_without_legacy_files_and_missing_selected_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    goal = repo / "Objectif.md"
    goal.write_text("Emploi en optimisation à Lyon", encoding="utf-8")
    client, codex = make_client(tmp_path, repo)
    initial = client.get("/settings/search-profile").json()
    assert initial["knowledge_base_available"] and initial["mode"] == "knowledge_base"
    assert initial["knowledge_base_selected_files"] == []
    assert not initial["candidate_profile_available"]
    assert client.put("/settings/search-profile", json={
        "mode": "knowledge_base", "knowledge_base_selected_files": ["Objectif.md"],
    }).status_code == 200
    assert client.post("/codex/discover").status_code == 200
    assert "Emploi en optimisation à Lyon" in codex.prompts[-1]
    assert "Stages M2" not in codex.prompts[-1]
    goal.unlink()
    (repo / "Autre.md").write_text("Non sélectionné", encoding="utf-8")
    assert client.get("/settings/search-profile").json()["knowledge_base_selected_files"] == ["Objectif.md"]
    assert client.post("/codex/discover").status_code == 409


def test_resolves_paths_again_at_read_time(tmp_path):
    repo = tmp_path / "repo"
    create_kb(repo)
    outside = tmp_path / "outside.md"
    outside.write_text("PRIVATE", encoding="utf-8")
    link = repo / "link.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links unavailable")
    assert link not in knowledge_base_files(repo)
    with pytest.raises(ValueError, match="hors de la Knowledge Base"):
        discovery_context(repo, ["link.md"])


@pytest.mark.parametrize("relative", [
    "01_Career/Applications/offer/analysis.md", "Applications/offer.md",
    "90_Templates/CV.md", "99_Archive/old.md", "9_Prompts/example.md",
    "03_Knowledge/90_Templates/example.md", "02_Projects/applications/offer.md",
])
def test_excluded_sources_are_never_available_or_read(tmp_path, relative):
    repo = tmp_path / "repo"
    create_kb(repo)
    excluded = repo / relative
    excluded.parent.mkdir(parents=True, exist_ok=True)
    excluded.write_text("EXCLUDED", encoding="utf-8")
    allowed = repo / "02_Projects/Search Prototype/9_notes.md"
    allowed.parent.mkdir(parents=True, exist_ok=True)
    allowed.write_text("ALLOWED", encoding="utf-8")
    client, _ = make_client(tmp_path, repo)
    files = {item["path"] for item in client.get("/settings/search-profile").json()["knowledge_base_files"]}
    assert relative not in files
    assert "02_Projects/Search Prototype/9_notes.md" in files
    assert all(not Path(path).is_absolute() for path in files)
    response = client.put("/settings/search-profile", json={
        "mode": "knowledge_base", "knowledge_base_selected_files": [relative],
    })
    assert response.status_code == 422
    with pytest.raises(ValueError, match="unavailable"):
        discovery_context(repo, [relative])
    with connect(client.app.state.db_path) as db:
        db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                   ("knowledge_base_selected_files", json.dumps([relative])))
        db.commit()
    assert client.get("/settings/search-profile").json()["knowledge_base_selected_files"] == []


def test_root_configuration_persists_and_discovery_uses_new_kb(tmp_path):
    repo = tmp_path / "repo"
    create_kb(repo)
    other = tmp_path / "other"
    other.mkdir()
    (other / "01_Career").mkdir()
    (other / "01_Career/Profil.md").write_text("NEW_KB_PROFILE", encoding="utf-8")
    (other / "Other.md").write_text("NEW_KB_OTHER", encoding="utf-8")
    client, _ = make_client(tmp_path, repo)
    saved = client.put("/settings/search-profile", json={
        "mode": "knowledge_base", "knowledge_base_root": str(other),
    })
    assert saved.status_code == 200, saved.text
    assert client.app.state.repo_root == other.resolve()
    profile = saved.json()
    assert profile["knowledge_base_root"] == str(other.resolve())
    assert profile["knowledge_base_selected_files"] == ["01_Career/Profil.md"]
    assert {item["path"] for item in profile["knowledge_base_files"]} == {"Other.md", "01_Career/Profil.md"}
    with connect(client.app.state.db_path) as db:
        assert db.execute("SELECT value FROM settings WHERE key='knowledge_base_root'").fetchone()[0] == str(other.resolve())
    reloaded, codex = make_client(tmp_path, repo)
    assert reloaded.get("/settings/search-profile").json() == profile
    assert reloaded.app.state.repo_root == other.resolve()
    assert reloaded.post("/codex/discover").status_code == 200
    assert "NEW_KB_PROFILE" in codex.prompts[-1]
    assert "PROFIL_KB_TEST" not in codex.prompts[-1]
    assert (repo / "01_Career/Profil.md").is_file()
    rejected = reloaded.put("/settings/search-profile", json={
        "mode": "knowledge_base", "knowledge_base_selected_files": ["../repo/01_Career/Profil.md"],
    })
    assert rejected.status_code == 400
    (other / "01_Career/Profil.md").unlink()
    (other / "Other.md").unlink()
    assert reloaded.get("/settings/search-profile").json()["knowledge_base_available"] is False


@pytest.mark.parametrize("kind", ["missing", "file", "empty", "excluded", "relative", "blank", "unreadable"])
def test_invalid_root_does_not_change_configuration(tmp_path, kind, monkeypatch):
    repo = tmp_path / "repo"
    create_kb(repo)
    client, _ = make_client(tmp_path, repo)
    initial = client.get("/settings/search-profile").json()
    target = tmp_path / "invalid"
    if kind == "file":
        target.write_text("not a directory", encoding="utf-8")
    elif kind in {"empty", "excluded", "unreadable"}:
        target.mkdir()
        if kind == "excluded":
            (target / "90_Templates").mkdir()
            (target / "90_Templates/CV.md").write_text("ignored", encoding="utf-8")
        elif kind == "unreadable":
            (target / "Profile.md").write_text("private", encoding="utf-8")
            original_open = Path.open
            def guarded_open(path, *args, **kwargs):
                if path == target / "Profile.md":
                    raise PermissionError("Access denied")
                return original_open(path, *args, **kwargs)
            monkeypatch.setattr(Path, "open", guarded_open)
    value = "relative/path" if kind == "relative" else " " if kind == "blank" else str(target)
    response = client.put("/settings/search-profile", json={"mode": "knowledge_base", "knowledge_base_root": value})
    assert response.status_code == 422
    assert "Invalid Knowledge Base" in response.json()["detail"]
    assert client.get("/settings/search-profile").json() == initial


def test_unavailable_kb_never_falls_back_to_saved_custom_prompt(tmp_path):
    repo = tmp_path / "repo"
    create_kb(repo)
    client, codex = make_client(tmp_path, repo)
    assert client.put("/settings/search-profile", json={
        "mode": "knowledge_base", "custom_search_prompt": "Ne pas utiliser ce prompt",
    }).status_code == 200
    for path in (repo / "01_Career").iterdir():
        path.unlink()
    assert client.get("/settings/search-profile").json()["mode"] == "knowledge_base"
    assert client.post("/codex/discover").status_code == 409
    assert not codex.prompts


def test_english_kb_sources_and_custom_response_do_not_expose_saved_files(tmp_path):
    repo = tmp_path / "candidate"
    career = repo / "01_Career"
    career.mkdir(parents=True)
    for name in ("Profile.md", "Domains.md", "Job Search.md"):
        (career / name).write_text(f"ENGLISH_EVIDENCE_{name}", encoding="utf-8")
    client, codex = make_client(tmp_path, repo)
    profile = client.get("/settings/search-profile").json()
    assert profile["knowledge_base_sources"] == ["Profile", "Domains", "Job Search"]
    assert client.post("/codex/discover").status_code == 200
    assert "ENGLISH_EVIDENCE_Profile.md" in codex.prompts[-1]
    custom = client.put("/settings/search-profile", json={
        "mode": "custom_prompt", "custom_search_prompt": "Find remote technical writer jobs",
    }).json()
    assert custom["knowledge_base_root"] == ""
    assert custom["knowledge_base_files"] == custom["knowledge_base_selected_files"] == []
    assert custom["knowledge_base_sources"] == []
    assert client.post("/codex/discover").status_code == 200
    assert "ENGLISH_EVIDENCE_" not in codex.prompts[-1]
