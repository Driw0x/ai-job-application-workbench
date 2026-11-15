"""Check CV metric provenance; interpretation of experimental scope remains a human review."""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from docx import Document

from .cv_documents import CvDocumentError, canonical_project_id, project_catalog


def metric_numbers(text: str) -> list[Decimal]:
    # Ignore years, versions, milestone IDs, model sizes and metric ranks (Hit@25).
    text = re.sub(r"\b(?:version|milestone|round|v)\s*\d+(?:[.,]\d+)*\b", "", text, flags=re.I)
    text = re.sub(r"(?<=\d)(?:er|e|ème|eme|st|nd|rd|th)\b", "", text, flags=re.I)
    return [Decimal(value.replace(",", ".").replace("−", "-")) for value in re.findall(
        r"(?<![\w@.,+−-])[+−-]?\d+(?:[.,]\d+)?(?!\w|[.,]\d)", text,
    ) if not (len(value) == 4 and value.isdigit() and 1900 <= int(value) <= 2100)]


def validate_cv_quantification(
    repo: Path, path: Path, expected: list[str], reviews: list,
    local_sources: list[str], rendered_titles: dict[str, str] | None = None,
) -> list[str]:
    """Validate one cited, quantified bullet or explicit gap for each rendered project."""
    if len(reviews) != len(expected) or {review.project_id for review in reviews} != set(expected):
        raise CvDocumentError("project_metrics must cover exactly the selected projects")
    catalog = project_catalog(repo)
    titles = {**catalog, **(rendered_titles or {})}
    title_ids = {canonical_project_id(titles[key]): key for key in expected}
    bullets = {key: [] for key in expected}
    current = None
    for paragraph in Document(path).paragraphs:
        if paragraph.style.name == "CV Project":
            current = title_ids.get(canonical_project_id(paragraph.text.split("  |  ")[0]))
        elif paragraph.style.name == "CV Section":
            current = None
        elif current and paragraph.style.name == "CV Bullet":
            bullets[current].append(paragraph.text.removeprefix("• "))

    warnings = []
    root = repo.resolve()
    sources = {value.replace("\\", "/") for value in local_sources}
    for review in reviews:
        key = review.project_id
        if review.missing_reason is not None:
            warnings.append(f"{key}: no usable metric attested; human validation required. {review.missing_reason}")
            continue
        if review.bullet not in bullets[key]:
            raise CvDocumentError(f"{key}: metric must appear in a project bullet")
        numbers = metric_numbers(review.bullet)
        if not numbers:
            raise CvDocumentError(f"{key}: bullet contains no result metric")
        relative = review.source_path.replace("\\", "/")
        source = (root / relative).resolve()
        if (relative not in sources or not source.is_relative_to(root) or source.suffix.lower() != ".md"
                or not relative.startswith((f"02_Projects/{catalog[key]}/", "03_Knowledge/"))):
            raise CvDocumentError(f"{key}: metric source must be a cited project or knowledge note")
        quote = " ".join(review.source_quote.split())
        source_text = " ".join(source.read_text(encoding="utf-8").split())
        if quote in source_text:
            evidence = metric_numbers(quote)
        else:
            evidence = metric_numbers(source_text)
            warnings.append(f"{key}: exact metric quote not found; numbers validated against the cited source.")
        # Accept decimal punctuation and honest rounding, without changing units or scale.
        if any(not any(value == candidate or (
            value.as_tuple().exponent < 0
            and candidate.quantize(Decimal(1).scaleb(value.as_tuple().exponent)) == value
        ) for candidate in evidence) for value in numbers):
            raise CvDocumentError(f"{key}: bullet numbers are not supported by cited source")
    return warnings
