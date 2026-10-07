from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit


POSTAL_CODE_PATTERN = r"\b\d{3,10}(?:-\d{3,4})?\b|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b|\b[A-Z]\d[A-Z]\s*\d[A-Z]\d\b"


def valid_postal_address(value: str | None) -> bool:
    """Accept a street and a separate postal line, not a city or street alone."""
    lines = [line.strip() for line in (value or "").splitlines() if line.strip()]
    street_number = re.compile(r"\b(?!\d{5}\b)\d+[A-Za-z]?(?:\s+et\s+\d+)?\s+.+", re.IGNORECASE)
    return any(
        index != other and re.search(POSTAL_CODE_PATTERN, line, re.IGNORECASE)
        and street_number.search(street)
        for index, line in enumerate(lines)
        for other, street in enumerate(lines)
    )


def company_markdown(company: str, profile: Any) -> str:
    data = profile.model_dump() if hasattr(profile, "model_dump") else dict(profile)
    address = data.get("postal_address")
    sections = [
        f"# Company — {company}",
        "## Description",
        data["description"].strip(),
        "## Application postal address",
        address.strip() if valid_postal_address(address) else "Verify company address",
        "## Relevant domain for this job",
        data["relevant_domain"].strip(),
        "## Relevant achievements",
        markdown_list(data.get("completed_achievements", [])),
        "## Relevant announced developments",
        markdown_list(data.get("planned_developments", [])),
        "## Relevant competitors / comparable organizations",
        markdown_list(data.get("competitors_or_comparable_actors", [])),
    ]
    return "\n\n".join(sections).rstrip() + "\n"


def markdown_list(values: list[str]) -> str:
    return "\n".join(f"- {value.strip()}" for value in values if value.strip()) or "- No relevant verified information retained."


def profile_columns(profile: Any) -> dict[str, Any]:
    data = profile.model_dump() if hasattr(profile, "model_dump") else dict(profile)
    address = data.get("postal_address")
    return {
        "company_description": data["description"].strip(),
        "company_postal_address": address.strip() if valid_postal_address(address) else None,
        "company_domain": data["relevant_domain"].strip(),
        "company_completed_achievements": json.dumps(data.get("completed_achievements", []), ensure_ascii=False),
        "company_planned_developments": json.dumps(data.get("planned_developments", []), ensure_ascii=False),
        "company_comparable_actors": json.dumps(data.get("competitors_or_comparable_actors", []), ensure_ascii=False),
        "company_sources": json.dumps(data.get("sources", []), ensure_ascii=False),
    }


def valid_source_url(value: str) -> bool:
    parts = urlsplit(value.strip())
    return parts.scheme in {"http", "https"} and bool(parts.netloc)
