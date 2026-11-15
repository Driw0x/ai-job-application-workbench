from zipfile import ZipFile

import pytest
from docx import Document
from lxml import etree

from app.codex_workflows import CvCertification, CvContent, preparation_context, preparation_prompt, cv_refresh_prompt
from app.cv_documents import (
    CV_HEADINGS, ATS_SECTIONS, CvDocumentError, convert_cv_to_ats, create_cv,
    cv_skill_lines, document_text, update_cv_skills, validate_cv_layout,
)
from test_codex_integration import client, cv_content, isolated_repo, preparation, shortlist, wait_for_preparation


CERTIFICATIONS = [
    {"label": "Microsoft Applied Skills — MCP", "year": 2026},
    {"label": "Google Cloud Skills Boost — Multi-Agent Architectures (badge)", "year": 2026},
    {"label": "Oracle — Agentic AI Foundations (certification de fondamentaux)", "year": 2026},
]


@pytest.mark.parametrize("certifications", [[], CERTIFICATIONS])
def test_create_adapt_and_reuse_render_conditional_skills(tmp_path, certifications):
    repo = isolated_repo(tmp_path)
    source = repo / "01_Career/CV/Templates/resume-template.docx"
    content = CvContent.model_validate(cv_content() | {"certifications": certifications})
    created, adapted, reused = [tmp_path / name for name in ("created.docx", "adapted.docx", "reused.docx")]
    create_cv(source, created, content)
    create_cv(created, adapted, content)
    convert_cv_to_ats(adapted, reused)
    assert document_text(created) == document_text(adapted) == document_text(reused)
    lines = document_text(reused).splitlines()
    heading = "COMPÉTENCES & CERTIFICATIONS" if certifications else "COMPÉTENCES"
    assert lines.count(heading) == 1
    assert lines.index("PROFIL") < lines.index(heading) < lines.index("PROJETS")
    for certification in certifications:
        assert certification["label"] in document_text(reused)
    assert sum(line.startswith("Certifications et badges") for line in lines) == bool(certifications)
    assert "Certifications" not in lines
    validate_cv_layout(reused)
    # Re-adaptation to an empty selection must also remove inherited certifications.
    content.certifications = []
    create_cv(reused, tmp_path / "no-certifications.docx", content)
    assert "CERTIFICATIONS" not in document_text(tmp_path / "no-certifications.docx")
    assert "Certifications et badges" not in document_text(tmp_path / "no-certifications.docx")


def protected_xml(path):
    with ZipFile(path) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    body = root.find("w:body", ns)
    in_skills = False
    for node in list(body):
        text = "".join(node.xpath(".//w:t/text()", namespaces=ns))
        heading = CV_HEADINGS.get(text, text)
        if heading in ATS_SECTIONS:
            in_skills = heading == "COMPÉTENCES"
        if in_skills:
            body.remove(node)
    return etree.tostring(root)


@pytest.mark.parametrize("initial,selected", [([], CERTIFICATIONS), (CERTIFICATIONS, [])])
def test_skills_update_preserves_manual_content_formatting_links_and_package_parts(tmp_path, initial, selected):
    repo = isolated_repo(tmp_path)
    source, destination = tmp_path / "source.docx", tmp_path / "updated.docx"
    content = CvContent.model_validate(cv_content() | {"certifications": initial})
    create_cv(repo / "01_Career/CV/Templates/resume-template.docx", source, content)
    document = Document(source)
    document.paragraphs[-1].add_run(" Retouche manuelle conservée.").italic = True
    document.save(source)
    original = source.read_bytes()
    update_cv_skills(source, destination, content.skill_groups,
                     [CvCertification.model_validate(value) for value in selected])
    assert source.read_bytes() == original
    assert protected_xml(source) == protected_xml(destination)
    with ZipFile(source) as old, ZipFile(destination) as new:
        assert set(old.namelist()) == set(new.namelist())
        for name in old.namelist():
            if name != "word/document.xml":
                assert old.read(name) == new.read(name), name
    before = destination.read_bytes()
    with pytest.raises(CvDocumentError, match="already exists"):
        update_cv_skills(source, destination, content.skill_groups, [])
    assert destination.read_bytes() == before
    update_cv_skills(destination, destination, content.skill_groups, [], replace=True)
    assert protected_xml(source) == protected_xml(destination)


def test_credentials_years_do_not_invent_a_common_date():
    values = [CvCertification(label="Applied Skills — MCP", year=2025),
              CvCertification(label="Badge — Multi-Agent Architectures", year=2026),
              CvCertification(label="Oracle — Fondamentaux")]
    assert cv_skill_lines([], values) == [
        "Certifications et badges : Applied Skills — MCP (2025) · Badge — Multi-Agent Architectures (2026) · Oracle — Fondamentaux"
    ]
    # Structured-output providers require every property, while old saved drafts may omit credentials.
    for model in (CvContent, CvCertification):
        schema = model.model_json_schema()
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["additionalProperties"] is False
    assert CvContent.model_validate(cv_content()).certifications == []


def test_register_is_available_without_offer_keyword_matches_and_both_prompts_select_credentials(tmp_path):
    repo = isolated_repo(tmp_path)
    register = repo / "01_Career/Certifications.md"
    register.write_text("# Certifications\n\nDocumented credentials and their scope.", encoding="utf-8")
    offer = {"company": "Example", "position": "", "description": ""}
    context = preparation_context(repo, offer, limit=0)
    assert context.count("--- SOURCE LOCALE: 01_Career/Certifications.md ---") == 1
    for prompt in (preparation_prompt(offer, context), cv_refresh_prompt(offer, context, "Current CV", [])):
        assert "COMPÉTENCES & CERTIFICATIONS" in prompt
        assert "ou retourne [] si aucun ne convient" in prompt
        assert "Ne duplique pas les justificatifs dans skill_groups.items" in prompt


def test_failed_update_keeps_existing_document(tmp_path, monkeypatch):
    from docx.document import Document as DocumentType

    repo = isolated_repo(tmp_path)
    source, destination = tmp_path / "source.docx", tmp_path / "existing.docx"
    content = CvContent.model_validate(cv_content())
    create_cv(repo / "01_Career/CV/Templates/resume-template.docx", source, content)
    destination.write_bytes(b"previous working document")
    monkeypatch.setattr(DocumentType, "save", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        update_cv_skills(source, destination, content.skill_groups, [], replace=True)
    assert destination.read_bytes() == b"previous working document"


@pytest.mark.parametrize("action", ["ADAPT", "CREATE"])
@pytest.mark.parametrize("certifications", [[], CERTIFICATIONS[:1]])
def test_preparation_attaches_current_docx_without_browser_cache(tmp_path, action, certifications):
    repo = isolated_repo(tmp_path, with_variants=action == "ADAPT")
    payload = preparation(action)
    payload["cv"]["content"]["certifications"] = certifications
    if action == "CREATE":
        payload["cv"]["reusable_content"]["certifications"] = certifications
    api, app = client(tmp_path, payload, repo_root=repo)
    application_id = shortlist(api)
    assert api.post(f"/applications/{application_id}/prepare-with-codex").status_code == 200
    result = wait_for_preparation(api, application_id, "completed")
    from pathlib import Path
    path = Path(result["cv_path"])
    response = api.get(f"/applications/{application_id}/cv")
    assert response.content == path.read_bytes()
    assert response.headers["cache-control"] == "no-store"
    assert ("COMPÉTENCES & CERTIFICATIONS" in document_text(path)) == bool(certifications)
