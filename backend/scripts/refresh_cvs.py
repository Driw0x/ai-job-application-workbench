"""Stage and review CV-only rewrites, then apply them without changing applications."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.codex_workflows import CvRefreshOutput, validate_local_sources
from app.cv_documents import create_cv, cv_project_order, cv_variants, project_catalog, validate_docx
from app.main import (
    DEFAULT_APPLICATIONS_ROOT, DEFAULT_DB, REPO_ROOT, add_event, connect,
    create_app, resolve_repo_path, validate_user_artifacts,
)

RULES_VERSION = "cv-layout-v3-four-projects"
REFRESH_STATUSES = {"SHORTLISTED", "PREPARED", "AWAITING_VALIDATION", "APPROVED", "SUBMITTING"}


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def eligible(db, row) -> bool:
    return (
        row["status"] in REFRESH_STATUSES and not row["deleted_at"] and not row["sent_at"]
        and not db.execute(
            "SELECT 1 FROM events WHERE application_id=? AND event_type='SENT' LIMIT 1", (row["id"],)
        ).fetchone()
    )


def preparing(db, row) -> bool:
    event = db.execute(
        "SELECT event_type FROM events WHERE application_id=? AND event_type IN "
        "('PREPARATION_QUEUED','PREPARATION_STARTED','PREPARATION_FAILED','CODEX_PREPARATION') "
        "ORDER BY id DESC LIMIT 1", (row["id"],),
    ).fetchone()
    return row["status"] == "SHORTLISTED" and event is not None and event[0] in {"PREPARATION_QUEUED", "PREPARATION_STARTED"}


def targets(app, include_variants: bool = False) -> list[dict]:
    state = app.state
    catalog = project_catalog(state.repo_root)
    result = []
    with closing(connect(state.db_path)) as db:
        rows = db.execute("SELECT * FROM applications ORDER BY id").fetchall()
        linked = {}
        for row in rows:
            if row["cv_path"]:
                path = resolve_repo_path(row["cv_path"], state.repo_root, state.applications_root)
                linked.setdefault(path, []).append(row["id"])
        for row in rows:
            if not eligible(db, row) or not row["cv_path"]:
                continue
            source = resolve_repo_path(row["cv_path"], state.repo_root, state.applications_root)
            if not source.is_file():
                raise ValueError(f"Resume missing for application {row['id']}")
            if not source.is_relative_to(state.applications_root.resolve()) or source.parent.name != "cv":
                raise ValueError(f"Resume outside application folder : {row['id']}")
            if linked[source] != [row["id"]]:
                raise ValueError(f"Resume shared by application {row['id']}")
            if preparing(db, row):
                raise ValueError(f"Preparation running for application {row['id']}")
            result.append({
                "key": f"application-{row['id']}", "source": source, "application": dict(row),
                "project_order": cv_project_order(source, catalog),
            })
    if include_variants:
        generated = (state.repo_root / "01_Career/CV/Generated").resolve()
        for name, variant in cv_variants(state.repo_root).items():
            source = variant["path"].resolve()
            if not source.is_relative_to(generated):
                raise ValueError("Variant outside Generated folder")
            if source in linked:
                raise ValueError(f"Variant directly referenced by an application : {name}")
            order = cv_project_order(source, catalog)
            if order != variant["project_order"]:
                raise ValueError(f"Project order differs from variant manifest {name}")
            result.append({"key": f"variant-{name}", "source": source, "application": None, "project_order": order})
    return result


def other_artifacts(target: dict) -> dict[str, str]:
    if target["application"] is None:
        return {}
    source = target["source"]
    return {
        str(path.relative_to(source.parent.parent)): fingerprint(path)
        for path in sorted(source.parent.parent.rglob("*"))
        if path.is_file() and path.resolve() != source
    }


def already_refreshed(app, target: dict) -> bool:
    row = target["application"]
    if row is None:
        return False
    with closing(connect(app.state.db_path)) as db:
        event = db.execute(
            "SELECT details FROM events WHERE application_id=? AND event_type='CV_REFRESHED' ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
    if not event:
        return False
    details = json.loads(event[0])
    return details.get("rules_version") == RULES_VERSION and details.get("after_sha256") == fingerprint(target["source"])


def stage(app, target: dict, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    source = target["source"]
    before = fingerprint(source)
    protected = other_artifacts(target)
    offer = target["application"]
    if offer is not None:
        offer = dict(offer)
        if offer["offer_path"]:
            offer_path = resolve_repo_path(offer["offer_path"], app.state.repo_root, app.state.applications_root)
            offer["offer_markdown"] = offer_path.read_text(encoding="utf-8")
    output, metadata = app.state.generate_cv_refresh(offer, source, target["project_order"])
    candidate = directory / f"{target['key']}.docx"
    create_cv(source, candidate, output.content, replace=True)
    validate_user_artifacts({}, candidate)
    selected = [project.project_id for project in output.content.projects]
    if cv_project_order(candidate, project_catalog(app.state.repo_root)) != selected:
        raise ValueError("Refreshed resume project order differs from reviewed content")
    if fingerprint(source) != before or other_artifacts(target) != protected:
        raise ValueError("Artifacts changed during generation")
    record = {
        "rules_version": RULES_VERSION, "key": target["key"], "source": str(source),
        "before_sha256": before, "after_sha256": fingerprint(candidate),
        "application": target["application"], "other_artifacts": protected,
        "project_order": selected, "output": output.model_dump(), **metadata,
    }
    (directory / f"{target['key']}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def apply(app, target: dict, directory: Path) -> str:
    record = json.loads((directory / f"{target['key']}.json").read_text(encoding="utf-8"))
    source = target["source"]
    candidate = directory / f"{target['key']}.docx"
    if record["key"] != target["key"] or Path(record["source"]).resolve() != source or record["rules_version"] != RULES_VERSION:
        raise ValueError("Incompatible refresh plan")
    if fingerprint(source) == record["after_sha256"]:
        return "unchanged"
    output = CvRefreshOutput.model_validate(record["output"])
    validate_local_sources(app.state.repo_root, output.local_sources)
    selected = record["project_order"]
    if (
        [project.project_id for project in output.content.projects] != selected
        or selected[:len(target["project_order"])] != target["project_order"]
        or (target["application"] is None and selected != target["project_order"])
    ):
        raise ValueError("Reviewed project selection has changed")
    validate_docx(candidate)
    validate_user_artifacts({}, candidate)
    if fingerprint(candidate) != record["after_sha256"] or cv_project_order(candidate, project_catalog(app.state.repo_root)) != selected:
        raise ValueError("Reviewed resume has changed")
    with closing(connect(app.state.db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        row = target["application"]
        for linked in db.execute("SELECT id,cv_path FROM applications WHERE cv_path IS NOT NULL"):
            if row is not None and linked["id"] == row["id"]:
                continue
            if resolve_repo_path(linked["cv_path"], app.state.repo_root, app.state.applications_root) == source:
                raise ValueError("Resume is now shared with another application")
        if row is not None:
            current = db.execute("SELECT * FROM applications WHERE id=?", (row["id"],)).fetchone()
            if current is None or not eligible(db, current) or dict(current) != record["application"]:
                raise ValueError("Application changed since review")
            if preparing(db, current):
                raise ValueError("Preparation started since review")
        if fingerprint(source) != record["before_sha256"] or other_artifacts(target) != record["other_artifacts"]:
            raise ValueError("Artifacts changed since review")
        original = source.read_bytes()
        temporary = None
        replaced = False
        try:
            with tempfile.NamedTemporaryFile(dir=source.parent, suffix=".docx", delete=False) as handle:
                temporary = Path(handle.name)
            shutil.copyfile(candidate, temporary)
            temporary.replace(source)
            replaced = True
            if row is not None:
                add_event(db, row["id"], "CV_REFRESHED", json.dumps({
                    key: record[key] for key in (
                        "rules_version", "before_sha256", "after_sha256", "project_order",
                        "provider", "auth_mode", "model", "effort",
                    )
                } | {"cv_path": row["cv_path"], "local_sources": output.local_sources}, ensure_ascii=False))
            db.commit()
        except Exception:
            db.rollback()
            if replaced:
                temporary.write_bytes(original)
                temporary.replace(source)
            raise
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return "refreshed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--knowledge-base", type=Path, default=REPO_ROOT)
    parser.add_argument("--applications-root", type=Path, default=DEFAULT_APPLICATIONS_ROOT)
    parser.add_argument("--include-variants", action="store_true")
    parser.add_argument("--only", nargs="*", help="Target keys from the dry-run report")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stage", type=Path, help="Generate reviewable DOCX files without replacing originals")
    mode.add_argument("--apply", type=Path, help="Apply previously reviewed staged DOCX files")
    args = parser.parse_args()
    app = create_app(db_path=args.database, repo_root=args.knowledge_base, applications_root=args.applications_root)
    try:
        for target in targets(app, args.include_variants):
            if args.only is not None and target["key"] not in args.only:
                continue
            action = "planned"
            if already_refreshed(app, target):
                action = "unchanged"
            elif args.stage:
                stage(app, target, args.stage)
                action = "staged"
            elif args.apply:
                action = apply(app, target, args.apply)
            row = target["application"]
            print(json.dumps({"key": target["key"], "status": row["status"] if row else None, "action": action}, ensure_ascii=False), flush=True)
    finally:
        if app.state.codex_client is not None:
            app.state.codex_client.close()
        app.state.auth.close()


if __name__ == "__main__":
    main()
