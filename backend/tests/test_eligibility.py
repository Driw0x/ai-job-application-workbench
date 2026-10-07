import pytest
from fastapi.testclient import TestClient

from app.codex_workflows import evaluate_offer_eligibility, screening_prompt
from app.main import connect, create_app


def evaluate(description: str, company: str = "Example") -> tuple[str, str]:
    return evaluate_offer_eligibility({
        "company": company,
        "title": "Stage Machine Learning",
        "description": description,
    })


def test_explicit_french_nationality_and_secret_defense_are_ineligible():
    status, reason = evaluate(
        "Stage IA — Example Defense — Le poste nécessite d'être éligible à une habilitation Secret Défense "
        "et la nationalité française obligatoire.",
        "Example Defense",
    )
    assert status == "INELIGIBLE"
    assert "citizenship" in reason or "clearance" in reason


def test_required_security_clearance_is_ineligible():
    assert evaluate("Security clearance required.")[0] == "INELIGIBLE"


def test_clearance_without_explicit_requirement_is_uncertain():
    assert evaluate("Travaux soumis à une habilitation Défense.")[0] == "ELIGIBILITY_UNCERTAIN"


def test_sensitive_military_context_is_uncertain():
    status, _ = evaluate("Conception d'un algorithme de guidage pour un système d'arme militaire.")
    assert status == "ELIGIBILITY_UNCERTAIN"


def test_civil_offer_at_state_linked_company_is_eligible():
    status, _ = evaluate(
        "Stage Machine Learning — analyse de données industrielles — aucune habilitation requise.",
        "Example Systems",
    )
    assert status == "ELIGIBLE"


def test_public_and_european_words_alone_do_not_reject():
    for description in (
        "secteur public", "administration", "transport public", "projet européen",
        "French citizenship not required.",
    ):
        assert evaluate(description)[0] == "ELIGIBLE"


def test_ineligible_offers_cannot_be_shortlisted_or_prepared(tmp_path):
    database = tmp_path / "tracker.db"
    api = TestClient(create_app(database, tmp_path / "applications"))
    descriptions = (
        "Security clearance required.",
        "Nationalité française obligatoire.",
    )
    for index, description in enumerate(descriptions):
        application = api.post("/applications", json={
            "company": "Example", "position": "Stage IA", "description": description,
            "url": f"https://example.test/restricted-{index}",
        }).json()
        assert application["eligibility_status"] == "INELIGIBLE"
        assert api.post(
            f"/applications/{application['id']}/status", json={"status": "SHORTLISTED"},
        ).status_code == 409
        with connect(database) as db:
            db.execute("UPDATE applications SET status='SHORTLISTED' WHERE id=?", (application["id"],))
            db.commit()
        assert api.post(f"/applications/{application['id']}/prepare-with-codex").status_code == 409


@pytest.mark.parametrize("description,eligibility", [
    ("Analyse de données industrielles, aucune habilitation requise.", "ELIGIBLE"),
    ("Développement d'un système d'arme militaire sensible.", "ELIGIBILITY_UNCERTAIN"),
])
def test_eligible_and_uncertain_offers_can_reach_shortlist(tmp_path, description, eligibility):
    api = TestClient(create_app(tmp_path / "tracker.db", tmp_path / "applications"))
    application = api.post("/applications", json={
        "company": "Example Research Organization", "position": "Stage ML",
        "description": description,
        "url": "https://example.test/civil",
    }).json()
    assert application["eligibility_status"] == eligibility
    selected = api.post(
        f"/applications/{application['id']}/status", json={"status": "SHORTLISTED"},
    )
    assert selected.status_code == 200
    assert selected.json()["status"] == "SHORTLISTED"
    assert selected.json()["eligibility_status"] == eligibility
    assert selected.json()["eligibility_reason"] == application["eligibility_reason"]


def test_screening_prompt_contains_per_offer_eligibility_rule():
    prompt = screening_prompt("profil", "custom_prompt", "annonce")
    assert "Une activité étatique ou Défense ne prouve pas une inéligibilité" in prompt
    assert "Sépare pertinence métier et éligibilité" in prompt
    assert "INELIGIBLE seulement pour une restriction explicite" in prompt
    assert "ELIGIBILITY_UNCERTAIN" in prompt
