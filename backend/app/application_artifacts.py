from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .cv_documents import CvDocumentError, validate_docx
from .cover_letters import INTERNAL_SOURCE, CoverLetterError, validate_cover_letter_docx


CANONICAL_ARTIFACTS = {
    "offer.md": "offer_path",
    "analysis.md": "analysis_path",
    "company.md": "company_path",
    "lettre-motivation.docx": "cover_letter_path",
    "interview-prep.md": "interview_prep_path",
    "CV": "cv_path",
}

INTERVIEW_PREP_SECTIONS = (
    "Position",
    "Company overview",
    "Role domain",
    "Main responsibilities",
    "Profile match",
    "Projects to highlight",
    "Technical topics to review",
    "Likely technical questions",
    "Likely HR / motivation questions",
    "Questions for the company",
    "Company-specific topics",
    "Actual interview",
)


def build_offer_markdown(application: Mapping[str, Any]) -> str:
    fields = (
        ("Company", application["company"]),
        ("Position", application["position"]),
        ("URL", application["url"]),
        ("Location", application["location"]),
        ("Contract", application["contract_type"]),
        ("Publication", application["published_at"]),
        ("Expected start", application["start_date"]),
        ("Deadline", application["deadline"]),
    )
    identification = "\n".join(
        f"- **{label} :** {value}" for label, value in fields if value
    )
    description = application["description"].strip() or "Detailed description unavailable."
    return (
        f"# Job — {application['company']}\n\n"
        f"## Identification\n\n{identification}\n\n"
        f"## Job description\n\n{description}\n"
    )


def validate_interview_prep(markdown: str, company: str) -> None:
    expected_title = f"# Interview preparation — {company}"
    if expected_title not in markdown:
        raise ValueError(f"Missing interview-prep.md title : {expected_title}")
    missing = [section for section in INTERVIEW_PREP_SECTIONS if f"## {section}" not in markdown]
    if missing:
        raise ValueError(f"Missing interview-prep.md sections : {', '.join(missing)}")


def update_interview_event(path: Path, interview_at: str | None, notes: str | None) -> None:
    markdown = path.read_text(encoding="utf-8")
    marker = "## Actual interview"
    if marker not in markdown:
        raise ValueError("Actual interview section missing")
    details = "No interview scheduled yet."
    if interview_at:
        lines = ["### Scheduled interview", "", f"- Date and time : {interview_at}"]
        if notes:
            lines.append(f"- Notes : {notes.strip()}")
        details = "\n".join(lines)
    updated = re.sub(
        rf"(?ms)^{re.escape(marker)}\s*\n.*?(?=^## |\Z)",
        lambda _: f"{marker}\n\n{details}\n",
        markdown,
    )
    path.write_text(updated.rstrip() + "\n", encoding="utf-8")


def validate_application_artifacts(
    application: Mapping[str, Any], repo_root: Path, applications_root: Path | None = None
) -> dict[str, Any]:
    missing: list[str] = []
    invalid: list[str] = []
    for name, field in CANONICAL_ARTIFACTS.items():
        value = application[field]
        if not value:
            missing.append(name)
            continue
        path = (repo_root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        allowed_roots = [repo_root.resolve()]
        if applications_root is not None:
            allowed_roots.append(applications_root.resolve())
        if not any(path.is_relative_to(root) for root in allowed_roots):
            invalid.append(name)
            continue
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(name)
            continue
        if name == "CV":
            try:
                validate_docx(path)
                if len(list(path.parent.glob("*.docx"))) != 1:
                    raise CvDocumentError("The folder must contain exactly one resume DOCX")
            except CvDocumentError:
                invalid.append(name)
        elif name == "lettre-motivation.docx":
            try:
                validate_cover_letter_docx(
                    path, application["company"], position=application["position"]
                )
            except CoverLetterError:
                invalid.append(name)
        elif name == "interview-prep.md":
            try:
                validate_interview_prep(path.read_text(encoding="utf-8"), application["company"])
            except (OSError, UnicodeError, ValueError):
                invalid.append(name)
        elif name == "analysis.md":
            try:
                if INTERNAL_SOURCE.search(path.read_text(encoding="utf-8")):
                    invalid.append(name)
            except (OSError, UnicodeError):
                invalid.append(name)
    return {"complete": not missing and not invalid, "missing": missing, "invalid": invalid}
