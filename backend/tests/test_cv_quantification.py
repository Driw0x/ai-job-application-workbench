from __future__ import annotations

import copy

import pytest

from app.codex_workflows import CvContent, CvProjectMetric, preparation_context, preparation_prompt, cv_refresh_prompt
from app.cv_documents import CvDocumentError, create_cv, document_text
from app.cv_quantification import metric_numbers, validate_cv_quantification
from test_codex_integration import (
    client, cv_content, isolated_repo, metric_gaps, preparation, shortlist, wait_for_preparation,
)
from test_cv_refresh import prepared_application, application_row
from scripts import refresh_cvs


def quantified_payload(repo, action="ADAPT"):
    payload = preparation(action)
    content = payload["cv"]["content"] or cv_content()
    samples = [
        "Reduced planning delay from 11 to 9 ticks on a synthetic instance.",
        "Improved local retrieval Hit@25 from 0.80 to 0.88 on 25 fixed questions.",
        "Measured held-out player-match ROC-AUC 0.7779 on 64 distinct test matches.",
    ]
    reviews = []
    for project, bullet in zip(content["projects"], samples):
        project["bullets"][0] = bullet
        relative = f"02_Projects/{project['title']}/Experiment.md"
        (repo / relative).write_text(f"# Local experiment\n\n{bullet}\n", encoding="utf-8")
        payload["local_sources"].append(relative)
        reviews.append({"project_id": project["project_id"], "bullet": bullet,
                        "source_path": relative, "source_quote": bullet, "missing_reason": None})
    payload["cv"]["project_metrics"] = reviews
    if action == "REUSE":
        create_cv(repo / "01_Career/CV/Templates/resume-template.docx",
                  repo / "01_Career/CV/Generated/resume_A.docx", CvContent.model_validate(content), replace=True)
    elif action == "CREATE":
        for project, review in zip(payload["cv"]["reusable_content"]["projects"], reviews):
            project["bullets"][0] = review["bullet"]
    return payload, content, reviews


@pytest.mark.parametrize("action", ["REUSE", "ADAPT", "CREATE"])
def test_all_preparation_actions_keep_sourced_metric_bullets(tmp_path, action):
    repo = isolated_repo(tmp_path, with_variants=action != "CREATE")
    payload, _, reviews = quantified_payload(repo, action)
    api, _ = client(tmp_path, payload, repo_root=repo)
    application_id = shortlist(api)
    api.post(f"/applications/{application_id}/prepare-with-codex")
    result = wait_for_preparation(api, application_id, "completed")
    from pathlib import Path
    text = document_text(Path(result["cv_path"]))
    assert all("• " + review["bullet"] in text for review in reviews)
    assert "Experiment.md" not in text
    if action == "CREATE":
        generic = document_text(repo / f"01_Career/CV/Generated/resume_{result['cv_variant']}.docx")
        assert all(review["bullet"] in generic for review in reviews)


@pytest.fixture
def metric_document(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    payload, content, reviews = quantified_payload(repo)
    path = tmp_path / "quantified.docx"
    create_cv(repo / "01_Career/CV/Templates/resume-template.docx", path, CvContent.model_validate(content))
    return repo, path, payload, reviews


@pytest.mark.parametrize("fault", ["invented_value", "wrong_bullet", "other_project", "uncited", "coverage"])
def test_rejects_unverified_metrics(metric_document, fault):
    repo, path, payload, reviews = metric_document
    reviews = copy.deepcopy(reviews)
    if fault == "invented_value":
        reviews[0]["source_quote"] = "Measured planning delay: 50 ticks."
        (repo / reviews[0]["source_path"]).write_text(reviews[0]["source_quote"], encoding="utf-8")
    elif fault == "wrong_bullet":
        reviews[0]["bullet"] = "Result 99."
    elif fault == "other_project":
        reviews[0]["source_path"] = reviews[1]["source_path"]
    elif fault == "uncited":
        payload["local_sources"].remove(reviews[0]["source_path"])
    else:
        reviews.pop()
    with pytest.raises(CvDocumentError):
        validate_cv_quantification(repo, path, payload["cv"]["project_order"],
                                   [CvProjectMetric.model_validate(item) for item in reviews], payload["local_sources"])


def test_non_exact_quote_falls_back_to_cited_source(metric_document):
    repo, path, payload, reviews = metric_document
    reviews[0]["source_quote"] = "Planning improved from 11 to 9 ticks."
    warnings = validate_cv_quantification(
        repo, path, payload["cv"]["project_order"],
        [CvProjectMetric.model_validate(item) for item in reviews], payload["local_sources"],
    )
    assert warnings == [
        f"{reviews[0]['project_id']}: exact metric quote not found; numbers validated against the cited source."
    ]


def test_rounding_and_explicit_missing_metric(metric_document):
    repo, path, payload, reviews = metric_document
    # The review must match the rendered bullet; the source keeps its full precision.
    original = reviews[2]["source_quote"]
    reviews[2]["bullet"] = original.replace("0.7779", "0,78")
    content = payload["cv"]["content"]
    content["projects"][2]["bullets"][0] = reviews[2]["bullet"]
    create_cv(repo / "01_Career/CV/Templates/resume-template.docx", path, CvContent.model_validate(content), replace=True)
    reviews[0] = metric_gaps([reviews[0]["project_id"]])[0]
    warnings = validate_cv_quantification(repo, path, payload["cv"]["project_order"],
                                          [CvProjectMetric.model_validate(item) for item in reviews], payload["local_sources"])
    assert len(warnings) == 1 and "human validation required" in warnings[0]
    assert metric_numbers("Version 2; M8; v1.2; Python3; Hit@25; 2026") == []
    assert metric_numbers("Optimum −7") != metric_numbers("Optimum 7")
    assert metric_numbers("17e place") == [17]


@pytest.mark.parametrize("punctuation", [".", ","])
def test_metric_numbers_before_punctuation(punctuation):
    assert metric_numbers(f"Allocated 50{punctuation} requested 60{punctuation}") == [50, 60]
    assert metric_numbers(f"Measured 0.7779{punctuation}") == metric_numbers("0.7779")
    assert metric_numbers("Version 2; M8; v1.2; Python3; Hit@25; 2026; 1.2.3") == []


def test_metric_source_with_sentence_final_number(metric_document):
    repo, path, payload, reviews = metric_document
    reviews[0]["source_quote"] = "Planning delay started at 11. It ended at 9."
    (repo / reviews[0]["source_path"]).write_text(reviews[0]["source_quote"], encoding="utf-8")
    warnings = validate_cv_quantification(
        repo, path, payload["cv"]["project_order"],
        [CvProjectMetric.model_validate(item) for item in reviews], payload["local_sources"],
    )
    assert warnings == []


def test_missing_metric_reaches_human_review_without_dropping_project(tmp_path):
    repo = isolated_repo(tmp_path, with_variants=True)
    api, _ = client(tmp_path, preparation(), repo_root=repo)
    application_id = shortlist(api)
    api.post(f"/applications/{application_id}/prepare-with-codex")
    result = wait_for_preparation(api, application_id, "completed")
    from pathlib import Path
    analysis = Path(result["analysis_path"]).read_text(encoding="utf-8")
    assert analysis.count("human validation required") == 3
    assert "document retrieval quality" in analysis


@pytest.mark.parametrize("position", ["Operations research optimisation CP-SAT", "Agentic AI ML retrieval"])
def test_offer_context_includes_experiments_even_without_lexical_match(tmp_path, position):
    repo = isolated_repo(tmp_path)
    payload, _, reviews = quantified_payload(repo)
    offer = {"company": "Example", "position": position, "description": "internship"}
    context = preparation_context(repo, offer, limit=0)
    assert all(review["source_quote"] in context for review in reviews)
    for prompt in (preparation_prompt(offer, context), cv_refresh_prompt(offer, context, "CV", payload["cv"]["project_order"])):
        assert "project_metrics" in prompt and "missing_reason" in prompt
        assert "sans modifier la sélection des projets" in prompt


@pytest.mark.parametrize("variant", [False, True])
def test_refresh_checks_metrics_for_offer_and_global_variant(prepared_application, tmp_path, variant):
    _, app, application_id, _ = prepared_application
    payload, content, _ = quantified_payload(app.state.repo_root)
    app.state.fake_codex.response = {"content": content, "local_sources": payload["local_sources"],
                                     "project_metrics": payload["cv"]["project_metrics"]}
    target = next(item for item in refresh_cvs.targets(app, include_variants=variant)
                  if (item["application"] is None) == variant)
    before = application_row(app, application_id)
    record = refresh_cvs.stage(app, target, tmp_path / "stage")
    assert record["metric_warnings"] == []
    assert refresh_cvs.apply(app, target, tmp_path / "stage") == "refreshed"
    assert application_row(app, application_id) == before


def test_refresh_rechecks_metric_source_before_apply(prepared_application, tmp_path):
    _, app, _, source = prepared_application
    payload, content, reviews = quantified_payload(app.state.repo_root)
    app.state.fake_codex.response = {"content": content, "local_sources": payload["local_sources"],
                                     "project_metrics": reviews}
    target = refresh_cvs.targets(app)[0]
    refresh_cvs.stage(app, target, tmp_path / "stage")
    original = source.read_bytes()
    (app.state.repo_root / reviews[0]["source_path"]).write_text("Result withdrawn.", encoding="utf-8")
    with pytest.raises(CvDocumentError, match="not supported by cited source"):
        refresh_cvs.apply(app, target, tmp_path / "stage")
    assert source.read_bytes() == original
