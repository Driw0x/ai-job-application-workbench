"""Stage or apply content-preserving ATS conversions against an explicit existing database."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.cv_documents import convert_cv_to_ats, cv_variants, validate_cv_layout, validate_cv_preservation

REFRESH_STATUSES = {"SHORTLISTED", "PREPARED", "AWAITING_VALIDATION", "APPROVED", "SUBMITTING"}


def connect(path: Path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)
    db.row_factory = sqlite3.Row
    return db


def resolve_repo_path(value, repo, applications):
    path = (repo / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    if not any(path.is_relative_to(root.resolve()) for root in (repo, applications)):
        raise ValueError("Path outside configured roots")
    return path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_event(db, application_id, event_type, details):
    db.execute("INSERT INTO events(application_id,event_type,details,created_at) VALUES(?,?,?,?)",
               (application_id, event_type, details, now()))


def preparing(db, row):
    event = db.execute(
        "SELECT event_type FROM events WHERE application_id=? AND event_type IN "
        "('PREPARATION_QUEUED','PREPARATION_STARTED','PREPARATION_FAILED','CODEX_PREPARATION') "
        "ORDER BY id DESC LIMIT 1", (row["id"],),
    ).fetchone()
    return event is not None and event[0] in {"PREPARATION_QUEUED", "PREPARATION_STARTED"}


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ats_migration(database: Path, repo: Path, applications: Path, directory: Path,
                  apply_changes: bool = False, include_variants: bool = False) -> list[dict]:
    """Convert existing content without an AI call. Never initialize a missing database."""
    if not database.is_file():
        raise ValueError("Existing application database required; refusing to create an empty database")
    directory.mkdir(parents=True, exist_ok=True)
    report = []
    with closing(connect(database)) as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM applications ORDER BY id")]
        history = {row["id"]: [dict(event) for event in db.execute(
            "SELECT * FROM events WHERE application_id=? ORDER BY id", (row["id"],)
        )] for row in rows}
    linked = {}
    for row in rows:
        if row["cv_path"]:
            source = resolve_repo_path(row["cv_path"], repo, applications)
            linked.setdefault(source, []).append(row["id"])
    work = []
    for row in rows:
        if not row["cv_path"]:
            continue
        source = resolve_repo_path(row["cv_path"], repo, applications)
        types = {event["event_type"] for event in history[row["id"]]}
        sent_evidence = {"SENT", "ACKNOWLEDGED", "HR_INTERVIEW", "TECH_INTERVIEW", "FINAL_INTERVIEW", "OFFER", "NO_RESPONSE"}
        reason = None
        if row["sent_at"] or row["status"] in sent_evidence or types & sent_evidence:
            reason = "sent-history-protected"
        elif row["status"] not in REFRESH_STATUSES or row["deleted_at"]:
            reason = "inactive-or-ambiguous-status"
        elif row["status"] == "SUBMITTING" or "SUBMISSION_STARTED" in types:
            reason = "submission-started-send-status-ambiguous"
        elif linked[source] != [row["id"]]:
            reason = "shared-resume"
        elif not source.is_relative_to(applications.resolve()) or source.parent.name != "cv" or source.is_symlink():
            reason = "unsafe-resume-path"
        elif not source.is_file():
            reason = "missing-resume"
        with closing(connect(database)) as db:
            if reason is None and preparing(db, row):
                reason = "preparation-running"
        if reason:
            report.append({"key": f"application-{row['id']}", "action": "skipped", "reason": reason})
        else:
            work.append((f"application-{row['id']}", source, row))
    if include_variants:
        sources = sorted((repo / "01_Career/CV/Templates").glob("*.docx"))
        sources += [variant["path"] for variant in cv_variants(repo).values()]
        for index, source in enumerate(dict.fromkeys(sources)):
            source = source.resolve()
            if source in linked:
                report.append({"key": str(source), "action": "skipped", "reason": "application-linked-template"})
            else:
                work.append((f"template-{index}", source, None))
    reviewed = {}
    if apply_changes and work:
        plan = directory / "report.json"
        if not plan.is_file():
            raise ValueError("Stage and validate the ATS documents before applying migration")
        reviewed = {entry["key"]: entry for entry in json.loads(plan.read_text(encoding="utf-8"))
                    if entry["action"] == "staged"}
        if set(reviewed) != {key for key, _, _ in work}:
            raise ValueError("Migration targets changed since staging; stage and validate again")
        # Validate the entire reviewed batch before replacing any original.
        for key, source, _ in work:
            candidate = directory / f"{key}.docx"
            entry = reviewed[key]
            if (Path(entry["source"]).resolve() != source or fingerprint(source) != entry["before_sha256"]
                    or fingerprint(candidate) != entry["after_sha256"]):
                raise ValueError("Original or reviewed ATS document changed since staging")
            validate_cv_layout(candidate)
            validate_cv_preservation(source, candidate)
    for key, source, row in work:
        before = fingerprint(source)
        candidate = directory / f"{key}.docx"
        if not apply_changes:
            convert_cv_to_ats(source, candidate, replace=True)
        entry = {"key": key, "source": str(source), "before_sha256": before,
                 "after_sha256": fingerprint(candidate), "action": "staged"}
        if apply_changes:
            with closing(connect(database)) as db:
                db.execute("BEGIN IMMEDIATE")
                if row is not None:
                    current = db.execute("SELECT * FROM applications WHERE id=?", (row["id"],)).fetchone()
                    current_events = [dict(event) for event in db.execute(
                        "SELECT * FROM events WHERE application_id=? ORDER BY id", (row["id"],)
                    )]
                    if current is None or dict(current) != row or current_events != history[row["id"]]:
                        raise ValueError("Application or send history changed during ATS conversion")
                for other in db.execute("SELECT id,cv_path FROM applications WHERE cv_path IS NOT NULL"):
                    if row is not None and other["id"] == row["id"]:
                        continue
                    if resolve_repo_path(other["cv_path"], repo, applications) == source:
                        raise ValueError("Resume is now shared by another application")
                if fingerprint(source) != before:
                    raise ValueError("Resume changed during ATS conversion")
                backup = directory / f"{key}.original.docx"
                if backup.exists():
                    raise ValueError("Backup already exists; use a new ATS migration directory")
                shutil.copy2(source, backup)
                pdf = source.with_suffix(".pdf")
                pdf_backup = directory / f"{key}.original.pdf"
                if pdf.exists():
                    shutil.copy2(pdf, pdf_backup)
                temporary = None
                try:
                    # Candidate and original may be on different volumes.
                    with tempfile.NamedTemporaryFile(dir=source.parent, suffix=".docx", delete=False) as handle:
                        temporary = Path(handle.name)
                    shutil.copyfile(candidate, temporary)
                    temporary.replace(source)
                    pdf.unlink(missing_ok=True)  # Export endpoints regenerate PDFs from the current DOCX.
                    if row is not None:
                        db.execute("UPDATE applications SET updated_at=? WHERE id=?", (now(), row["id"]))
                        if row["status"] == "APPROVED":
                            db.execute("UPDATE applications SET status='AWAITING_VALIDATION', next_action=?, next_action_at=NULL WHERE id=?",
                                       ("Review converted ATS resume before submission", row["id"]))
                        add_event(db, row["id"], "CV_ATS_CONVERTED", json.dumps(entry, ensure_ascii=False))
                    db.commit()
                    entry["action"] = "converted"
                except Exception:
                    db.rollback()
                    shutil.copy2(backup, source)
                    if pdf_backup.exists():
                        shutil.copy2(pdf_backup, pdf)
                    raise
                finally:
                    if temporary:
                        temporary.unlink(missing_ok=True)
        report.append(entry)
        (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if apply_changes:
        # Only discard reviewed candidates after the entire batch succeeds.
        # Original documents and the audit report remain available for restoration.
        for entry in report:
            if entry["action"] == "converted":
                (directory / f"{entry['key']}.docx").unlink(missing_ok=True)
    return report



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--knowledge-base", type=Path, required=True)
    parser.add_argument("--applications-root", type=Path, required=True)
    parser.add_argument("--directory", type=Path, help="Persistent staging, backups and report; defaults outside Git")
    parser.add_argument("--apply", action="store_true", help="Apply the staged, validated documents from --directory")
    parser.add_argument("--include-variants", action="store_true")
    args = parser.parse_args()
    if args.directory is None:
        if args.apply:
            parser.error("--apply requires the reviewed staging --directory")
        storage = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
        args.directory = storage / "ai-job-application-workbench" / "ats-migrations" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    print(f"ATS migration directory: {args.directory.resolve()}", file=sys.stderr, flush=True)
    for record in ats_migration(args.database, args.knowledge_base, args.applications_root,
                                args.directory, args.apply, args.include_variants):
        print(json.dumps(record, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
