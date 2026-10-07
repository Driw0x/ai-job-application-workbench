from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.main import (
    REPO_ROOT,  # noqa: E402
    DEFAULT_APPLICATIONS_ROOT,
    DEFAULT_DB,
    cleanup_inactive_application_artifacts,
    connect,
)


def cleanup_all(
    database_path: Path = DEFAULT_DB,
    repo_root: Path = REPO_ROOT,
    applications_root: Path = DEFAULT_APPLICATIONS_ROOT,
    dry_run: bool = True,
) -> dict[str, object]:
    report: dict[str, object] = {
        "dry_run": dry_run,
        "WITHDRAWN": 0,
        "IGNORED": 0,
        "files": 0,
        "bytes": 0,
        "applications": [],
    }
    with connect(database_path) as db:
        rows = db.execute(
            "SELECT id, status FROM applications "
            "WHERE status IN ('WITHDRAWN', 'IGNORED') ORDER BY id"
        ).fetchall()
        for row in rows:
            application = cleanup_inactive_application_artifacts(
                db, row["id"], repo_root, applications_root, dry_run=dry_run
            )
            report[row["status"]] += 1
            report["files"] += len(application["deleted"])
            report["bytes"] += application["bytes"]
            report["applications"].append(application)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Clean generated artifacts for WITHDRAWN and IGNORED applications."
    )
    parser.add_argument(
        "--execute", action="store_true", help="Delete files (dry-run by default)."
    )
    args = parser.parse_args()
    print(json.dumps(cleanup_all(dry_run=not args.execute), ensure_ascii=False, indent=2))
