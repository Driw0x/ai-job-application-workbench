from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.main import REPO_ROOT, DEFAULT_DB

from app.cover_letters import (  # noqa: E402
    CoverLetterError,
    migrate_cover_letter_docx,
    validate_cover_letter_docx,
    validate_cover_letter_template,
    cover_letter_template_path,
)


def migrate_all(
    repo: Path = REPO_ROOT,
    database_path: Path | None = None,
) -> dict[str, object]:
    database_path = database_path or DEFAULT_DB
    report: dict[str, object] = {
        "applications_total": 0,
        "letters_found": 0,
        "letters_migrated": 0,
        "letters_validated": 0,
        "letters_failed": 0,
        "letters_unchanged": 0,
        "placeholders_remaining": 0,
        "pdf_checks_successful": 0,
        "failures": [],
    }
    validate_cover_letter_template(cover_letter_template_path(repo))
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, company, position, company_postal_address, cover_letter_path "
            "FROM applications ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    report["applications_total"] = len(rows)
    for row in rows:
        if not row["cover_letter_path"]:
            continue
        path = Path(row["cover_letter_path"])
        path = path if path.is_absolute() else repo / path
        if not path.is_file():
            continue
        report["letters_found"] += 1
        try:
            changed = migrate_cover_letter_docx(
                repo, path, row["company"], row["position"], row["company_postal_address"]
            )
            validate_cover_letter_docx(path, row["company"], position=row["position"])
            report["letters_validated"] += 1
            key = "letters_migrated" if changed else "letters_unchanged"
            report[key] += 1
        except (OSError, CoverLetterError) as error:
            report["letters_failed"] += 1
            report["failures"].append({
                "application_id": row["id"],
                "company": row["company"],
                "reason": str(error),
            })
    return report


if __name__ == "__main__":
    print(json.dumps(migrate_all(), ensure_ascii=False, indent=2))
