from __future__ import annotations

import json
import os
import logging
import re
import shutil
import sqlite3
import tempfile
import threading
import unicodedata
from collections import deque
from contextlib import nullcontext, asynccontextmanager, closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit
from urllib.request import Request as URLRequest, urlopen
from html.parser import HTMLParser

import httpx
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .chatgpt_auth import ChatGPTAuth
from .ai_providers import (
    ANTHROPIC_PROVIDER, API_PROVIDERS, DEEPSEEK_PROVIDER, EMPTY_CAPABILITIES,
    GEMINI_PROVIDER, OPENAI_CAPABILITIES, OPENAI_PROVIDER, PROVIDER_LABELS, REASONING_EFFORTS,
    AnthropicProvider, ApiKeyStore, ChatGPTOAuthResponsesProvider, DeepSeekProvider,
    GeminiProvider, OpenAIProvider,
)
from .application_artifacts import (
    build_offer_markdown, update_interview_event, validate_application_artifacts,
    validate_interview_prep,
)
from .codex_client import (
    CodexClient, CodexError, CodexStructuredOutputError, WebSearchNotExecutedError,
)
from .cv_documents import (
    cv_template_path,
    CvDocumentError, create_cv, cv_variants, document_text, next_variant_name,
    project_catalog, register_variant, validate_cv_layout, validate_cv_projects, validate_docx,
)
from .cv_pdf import (
    PdfExportError,
    PdfExportUnavailableError,
    convert_docx_to_pdf,
    warm_up_libreoffice,
)
from .company_profiles import company_markdown, profile_columns, valid_postal_address
from .cover_letters import (
    CoverLetterError, cover_letter_date, create_cover_letter_docx,
    missing_optional_cover_letter_values,
    read_cover_letter_docx, validate_cover_letter_docx,
)
from .offer_verification import (
    DnsResolutionError, OfferVerifier, RedirectLimitError, UnsafeDestination,
    deadline_expired, extract_offer_metadata, parse_offer_date, safe_fetch,
)
from .search_providers import (
    AIWebSearchProvider, SearchProviderError, SearchProviderUnavailableError,
    SearchResult, SearXNGProvider, canonical_url,
)
from .codex_workflows import (
    CvRefreshOutput, DiscoveryOutput, PreparationOutput, ScreeningDecision,
    discovery_context, evaluate_offer_eligibility, screening_prompt,
    knowledge_base_available, knowledge_base_sources, knowledge_base_files, knowledge_base_directory_allowed,
    cv_refresh_prompt, normalize_url, preparation_context, preparation_prompt,
    validate_local_sources,
)

APP_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(
    os.environ.get("KNOWLEDGE_BASE_PATH")
    or APP_ROOT / "data" / "knowledge-base-unconfigured"
).expanduser().resolve()
DEFAULT_DB = Path(
    os.environ.get("DATABASE_PATH") or APP_ROOT / "data" / "job_tracker.db"
).expanduser().resolve()
DEFAULT_APPLICATIONS_ROOT = APP_ROOT / "data" / "applications"
CV_ROOT = REPO_ROOT / "01_Career" / "CV" / "Generated"
MAX_CONCURRENT_PREPARATIONS = 2
DEFAULT_MAX_OFFER_AGE_DAYS = 90
INTERVIEW_STATUSES = {"HR_INTERVIEW", "TECH_INTERVIEW", "FINAL_INTERVIEW"}
INACTIVE_STATUSES = {"WITHDRAWN", "IGNORED"}
LOGGER = logging.getLogger(__name__)

STATUSES = {
    "DETECTED", "SHORTLISTED", "PREPARED", "AWAITING_VALIDATION", "APPROVED",
    "SUBMITTING", "SENT", "ACKNOWLEDGED", "HR_INTERVIEW", "TECH_INTERVIEW",
    "FINAL_INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN", "NO_RESPONSE", "IGNORED",
}
TRANSITIONS = {
    "DETECTED": {"SHORTLISTED", "IGNORED"},
    "SHORTLISTED": {"PREPARED", "IGNORED", "WITHDRAWN"},
    "PREPARED": {"AWAITING_VALIDATION", "SHORTLISTED", "IGNORED"},
    "AWAITING_VALIDATION": {"SENT", "PREPARED", "IGNORED"},
    "APPROVED": {"SUBMITTING", "SENT", "PREPARED", "WITHDRAWN"},
    "SUBMITTING": {"SENT", "APPROVED"},
    "SENT": {"ACKNOWLEDGED", "HR_INTERVIEW", "TECH_INTERVIEW", "REJECTED", "WITHDRAWN", "NO_RESPONSE"},
    "ACKNOWLEDGED": {"HR_INTERVIEW", "TECH_INTERVIEW", "REJECTED", "WITHDRAWN", "NO_RESPONSE"},
    "HR_INTERVIEW": {"TECH_INTERVIEW", "FINAL_INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN"},
    "TECH_INTERVIEW": {"FINAL_INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN"},
    "FINAL_INTERVIEW": {"OFFER", "REJECTED", "WITHDRAWN"},
    "NO_RESPONSE": {"ACKNOWLEDGED", "HR_INTERVIEW", "REJECTED", "WITHDRAWN"},
    "OFFER": {"WITHDRAWN"},
    "REJECTED": set(), "WITHDRAWN": set(), "IGNORED": set(),
}
EVENT_FOR_STATUS = {
    "SHORTLISTED": "SHORTLISTED", "PREPARED": "PREPARED",
    "AWAITING_VALIDATION": "AWAITING_VALIDATION", "APPROVED": "APPROVED",
    "SUBMITTING": "SUBMISSION_STARTED", "SENT": "SENT", "ACKNOWLEDGED": "ACKNOWLEDGED",
    "HR_INTERVIEW": "HR_INTERVIEW", "TECH_INTERVIEW": "TECH_INTERVIEW",
    "FINAL_INTERVIEW": "FINAL_INTERVIEW", "OFFER": "OFFER", "REJECTED": "REJECTED",
    "WITHDRAWN": "WITHDRAWN", "NO_RESPONSE": "NO_RESPONSE", "IGNORED": "IGNORED",
}
FILE_FIELDS = {
    "offer": "offer_path", "analysis": "analysis_path", "company": "company_path",
    "cover-letter": "cover_letter_path",
    "interview-prep": "interview_prep_path", "cv": "cv_path",
}
FILE_NAMES = {
    "offer": "offer.md", "analysis": "analysis.md", "company": "company.md",
    "cover-letter": "lettre-motivation.docx", "interview-prep": "interview-prep.md",
    "cv": "CV",
}
SOURCE_ANNOTATION = re.compile(r"[ \t]*\[(?:Source locale|Source)\s*:\s*[^\]\r\n]+\]", re.IGNORECASE)
SOURCE_MARKER = re.compile(r"\[(?:Source locale|Source)\s*:", re.IGNORECASE)
INTERNAL_SOURCE_PATH = re.compile(r"(?:01_Career|02_Projects|03_Knowledge)[\\/]", re.IGNORECASE)
PIPELINE_STAGES = ("screening", "deep_analysis", "company_analysis", "application_preparation")
DEFAULT_SEARXNG_URL = "http://localhost:8080"


class _TextExtractor(HTMLParser):
    IGNORED_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "form"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in self.IGNORED_TAGS:
            self.ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.ignored_depth and tag.casefold() in self.IGNORED_TAGS:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value and not self.ignored_depth and (not self.parts or self.parts[-1] != value):
            self.parts.append(value)


def clean_artifact_text(value: str) -> str:
    cleaned = SOURCE_ANNOTATION.sub("", value)
    if SOURCE_MARKER.search(cleaned) or INTERNAL_SOURCE_PATH.search(cleaned):
        raise ValueError("Knowledge Base reference forbidden in a user artifact")
    return cleaned


def clean_artifact_value(value: Any) -> Any:
    if isinstance(value, str):
        return clean_artifact_text(value)
    if isinstance(value, list):
        return [clean_artifact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: clean_artifact_value(item) for key, item in value.items()}
    return value


def validate_user_artifacts(documents: dict[str, tuple[str, str]], cv_path: Path) -> None:
    for filename, content in documents.values():
        if SOURCE_MARKER.search(content) or INTERNAL_SOURCE_PATH.search(content):
            raise ValueError(f"Knowledge Base reference detected in {filename}")
    cv_text = document_text(cv_path)
    if SOURCE_MARKER.search(cv_text) or INTERNAL_SOURCE_PATH.search(cv_text):
        raise ValueError(f"Knowledge Base reference detected in {cv_path.name}")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-") or "candidature"


def classify_search_result(item: SearchResult) -> Literal["likely_job_detail", "unknown", "obvious_non_job"]:
    parts = urlsplit(item.url)
    path = parts.path.casefold()
    title = unicodedata.normalize("NFKD", item.title).encode("ascii", "ignore").decode().casefold()
    path_and_title = f"{path} {title}"
    if re.search(r"\b(?:formation|mastere?|cours|mooc|evenement|programme academique)\b", path_and_title):
        return "obvious_non_job"
    generic_page = bool(re.search(
        r"/(?:search|recherche)[^/]*(?:/|$)|/pages?(?:/|$)|/(?:jobs?|emplois?|offres?)/?$|"
        r"/(?:q-[^/]+-emplois|stage-[^/]+-(?:emplois|jobs))(?:\.html)?$|"
        r"/(?:emploi|emplois)/[^/]*(?:emplois|srch)|/stage/(?:metier|mot-cle)|"
        r"/(?:jobs?|emplois?)/[^/]*(?:-emplois(?:-|$)|offres-de-stage(?:/|$))|"
        r"/effectuer-un-stage(?:-|/|$)",
        path,
    ))
    detail_path = not generic_page and bool(re.search(
        r"/(?:jobs?/view|job-offers?|apply|offres?/(?:recherche/)?detail)(?:/[^/]+)+$|"
        r"/(?:job|emplois?)/[^/]*(?:\d{4,}|[a-z]+-[a-z]+-[a-z]+)(?:\.html)?$",
        path,
    ))
    title_is_offer = bool(re.search(
        r"\b(?:stage(?: de fin d[' ]etudes)?|stagiaire|intern|internship)\b", title,
    ))
    if detail_path or (title_is_offer and not generic_page and not path.endswith(".pdf")):
        return "likely_job_detail"
    return "unknown"


def rank_search_results(results: list[SearchResult]) -> tuple[list[SearchResult], dict[str, int]]:
    classified = [(classify_search_result(item), item) for item in results]
    counts = {
        "likely_job_detail_count": sum(category == "likely_job_detail" for category, _ in classified),
        "unknown_candidate_count": sum(category == "unknown" for category, _ in classified),
        "obvious_non_job_count": sum(category == "obvious_non_job" for category, _ in classified),
    }
    ranked = [
        item for category, item in sorted(
            (value for value in classified if value[0] != "obvious_non_job"),
            key=lambda value: value[0] != "likely_job_detail",
        )
    ]
    return ranked, counts


def select_search_candidates(
    results: list[SearchResult], known_urls: set[str],
) -> tuple[list[SearchResult], dict[str, int]]:
    unknown_results = [item for item in results if item.url not in known_urls]
    ranked, stats = rank_search_results(unknown_results)
    candidates = ranked
    stats.update({
        "known_url_count": len(results) - len(unknown_results),
        "unknown_url_count": len(unknown_results),
        "candidate_count": len(candidates),
        "candidate_limit_dropped": 0,
    })
    return candidates, stats


def detect_application_channel(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    path = urlsplit(url).path.lower()
    if host == "jobs.smartrecruiters.com" or host.endswith(".smartrecruiters.com"):
        return "SmartRecruiters"
    if host == "welcomekit.co" or host.endswith(".welcomekit.co"):
        return "WelcomeKit"
    if host == "welcometothejungle.com" or host.endswith(".welcometothejungle.com"):
        return "Welcome to the Jungle"
    if host.split(".")[0] in {"career", "careers", "emploi", "jobs", "recrutement", "stages"}:
        return "Career website"
    if any(part in path.split("/") for part in ("career", "careers", "emploi", "jobs", "recrutement", "stages")):
        return "Career website"
    return None


def tracking_for_status(status: str, timestamp: str | None = None) -> tuple[str | None, str | None]:
    actions = {
        "DETECTED": "Decide whether this job is relevant",
        "SHORTLISTED": "Wait for application preparation",
        "PREPARED": "Review analysis, resume and cover letter, then approve",
        "AWAITING_VALIDATION": "Submit on the company website, then confirm submission",
        "APPROVED": "Complete submission on the website, then confirm",
        "SUBMITTING": "Complete submission on the website, then confirm",
        "SENT": "Follow up if no response",
        "ACKNOWLEDGED": "Wait for the next step",
        "OFFER": "Review the offer",
    }
    action = "Prepare for interview" if status in INTERVIEW_STATUSES else actions.get(status)
    next_action_at = (
        (datetime.fromisoformat(timestamp) + timedelta(days=10)).isoformat(timespec="seconds")
        if status == "SENT" and timestamp else None
    )
    return action, next_action_at


def preparation_error(
    application_id: int, model: str, effort: str, error_type: str, field: str,
    message: str, metadata: dict[str, str] | None = None,
) -> str:
    metadata = metadata or {}
    context = {
        "application_id": str(application_id), "model": model, "effort": effort,
        "status": "failed", "type": error_type, "field": field,
        "thread_id": metadata.get("thread_id", ""), "turn_id": metadata.get("turn_id", ""),
    }
    details = "; ".join(f"{key}={value}" for key, value in context.items() if value)
    return f"AI preparation failed [{details}] : {message}"


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                position TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                contract_type TEXT,
                published_at TEXT,
                start_date TEXT,
                availability TEXT,
                why_relevant TEXT NOT NULL DEFAULT '',
                eligibility_status TEXT NOT NULL DEFAULT 'ELIGIBLE',
                eligibility_reason TEXT NOT NULL DEFAULT 'No eligibility restriction detected.',
                detected_at TEXT NOT NULL,
                deadline TEXT,
                status TEXT NOT NULL DEFAULT 'DETECTED',
                cv_variant TEXT,
                cv_path TEXT,
                offer_path TEXT,
                analysis_path TEXT,
                company_path TEXT,
                interview_prep_path TEXT,
                application_path TEXT,
                cover_letter_path TEXT,
                cover_letter_word_count INTEGER,
                company_description TEXT NOT NULL DEFAULT '',
                company_postal_address TEXT,
                company_domain TEXT NOT NULL DEFAULT '',
                company_completed_achievements TEXT NOT NULL DEFAULT '[]',
                company_planned_developments TEXT NOT NULL DEFAULT '[]',
                company_comparable_actors TEXT NOT NULL DEFAULT '[]',
                company_sources TEXT NOT NULL DEFAULT '[]',
                company_address_overridden INTEGER NOT NULL DEFAULT 0,
                application_channel TEXT,
                sent_at TEXT,
                next_action TEXT,
                next_action_at TEXT,
                interview_at TEXT,
                interview_notes TEXT,
                notes TEXT NOT NULL DEFAULT '',
                deleted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
            CREATE INDEX IF NOT EXISTS idx_events_application ON events(application_id, created_at);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                category TEXT NOT NULL CHECK(category IN ('SEARCH', 'DOCUMENT')),
                operation TEXT NOT NULL,
                input_tokens INTEGER CHECK(input_tokens >= 0),
                output_tokens INTEGER CHECK(output_tokens >= 0),
                total_tokens INTEGER CHECK(total_tokens >= 0)
            );
            CREATE INDEX IF NOT EXISTS idx_ai_usage_created_at ON ai_usage(created_at);
            CREATE TABLE IF NOT EXISTS discovery_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                model TEXT NOT NULL, found_count INTEGER NOT NULL DEFAULT 0,
                new_count INTEGER NOT NULL DEFAULT 0, duplicate_count INTEGER NOT NULL DEFAULT 0,
                ignored_count INTEGER NOT NULL DEFAULT 0, error TEXT, profile_mode TEXT
            );
            CREATE TABLE IF NOT EXISTS discovery_candidates (
                discovery_run_id INTEGER NOT NULL REFERENCES discovery_runs(id) ON DELETE CASCADE,
                candidate_id TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                source_query TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                retrieval_mode TEXT NOT NULL DEFAULT 'SEARXNG', search_provider TEXT NOT NULL DEFAULT 'searxng',
                rank INTEGER NOT NULL, pre_classification TEXT NOT NULL,
                selected_for_ai INTEGER NOT NULL DEFAULT 1, ai_batch INTEGER,
                content_mode TEXT NOT NULL, fetch_status TEXT NOT NULL, http_status INTEGER,
                ai_decision TEXT, ai_reason_code TEXT, eligibility_status TEXT,
                ai_reason TEXT, verification_status TEXT NOT NULL DEFAULT 'NOT_RUN',
                final_action TEXT NOT NULL DEFAULT 'PENDING',
                PRIMARY KEY (discovery_run_id, candidate_id)
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_candidates_run
                ON discovery_candidates(discovery_run_id, rank);
        """)
        db.execute("BEGIN")
        columns = {row[1] for row in db.execute("PRAGMA table_info(applications)")}
        for name, declaration in {
            "contract_type": "TEXT", "published_at": "TEXT", "start_date": "TEXT",
            "availability": "TEXT", "why_relevant": "TEXT NOT NULL DEFAULT ''",
            "eligibility_status": "TEXT NOT NULL DEFAULT 'ELIGIBLE'",
            "eligibility_reason": "TEXT NOT NULL DEFAULT 'No eligibility restriction detected.'",
            "deleted_at": "TEXT", "cover_letter_path": "TEXT",
            "cover_letter_word_count": "INTEGER",
            "company_description": "TEXT NOT NULL DEFAULT ''",
            "company_postal_address": "TEXT",
            "company_domain": "TEXT NOT NULL DEFAULT ''",
            "company_completed_achievements": "TEXT NOT NULL DEFAULT '[]'",
            "company_planned_developments": "TEXT NOT NULL DEFAULT '[]'",
            "company_comparable_actors": "TEXT NOT NULL DEFAULT '[]'",
            "company_sources": "TEXT NOT NULL DEFAULT '[]'",
            "company_address_overridden": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                db.execute(f"ALTER TABLE applications ADD COLUMN {name} {declaration}")
        discovery_columns = {row[1] for row in db.execute("PRAGMA table_info(discovery_runs)")}
        for name, declaration in {
            "profile_mode": "TEXT", "provider_id": "TEXT", "auth_mode": "TEXT", "effort": "TEXT",
            "search_mode": "TEXT NOT NULL DEFAULT 'SEARXNG'", "search_model": "TEXT",
            "search_provider": "TEXT", "search_fallback_used": "INTEGER NOT NULL DEFAULT 0",
            "ai_fallback_used": "INTEGER NOT NULL DEFAULT 0",
            "provider_results_raw": "INTEGER NOT NULL DEFAULT 0",
            "per_query_stats": "TEXT NOT NULL DEFAULT '[]'",
            "raw_result_count": "INTEGER NOT NULL DEFAULT 0",
            "admitted_result_count": "INTEGER NOT NULL DEFAULT 0",
            "merged_count": "INTEGER NOT NULL DEFAULT 0",
            "intra_query_duplicate_count": "INTEGER NOT NULL DEFAULT 0",
            "global_duplicate_count": "INTEGER NOT NULL DEFAULT 0",
            "known_url_count": "INTEGER NOT NULL DEFAULT 0",
            "unknown_url_count": "INTEGER NOT NULL DEFAULT 0",
            "likely_job_detail_count": "INTEGER NOT NULL DEFAULT 0",
            "unknown_candidate_count": "INTEGER NOT NULL DEFAULT 0",
            "obvious_non_job_count": "INTEGER NOT NULL DEFAULT 0",
            "candidate_count": "INTEGER NOT NULL DEFAULT 0",
            "candidate_limit_dropped": "INTEGER NOT NULL DEFAULT 0",
            "fetch_attempted": "INTEGER NOT NULL DEFAULT 0",
            "fetch_succeeded": "INTEGER NOT NULL DEFAULT 0",
            "fetch_failed": "INTEGER NOT NULL DEFAULT 0",
            "snippet_fallback_count": "INTEGER NOT NULL DEFAULT 0",
            "ai_input_count": "INTEGER NOT NULL DEFAULT 0",
            "ai_batch_count": "INTEGER NOT NULL DEFAULT 0",
            "ai_decision_count": "INTEGER NOT NULL DEFAULT 0",
            "ai_keep_count": "INTEGER NOT NULL DEFAULT 0",
            "ai_reject_count": "INTEGER NOT NULL DEFAULT 0",
            "ai_review_count": "INTEGER NOT NULL DEFAULT 0",
            "ai_missing_decision_count": "INTEGER NOT NULL DEFAULT 0",
            "ai_retry_count": "INTEGER NOT NULL DEFAULT 0",
            "ai_call_count": "INTEGER NOT NULL DEFAULT 0",
            "ai_input_tokens": "INTEGER",
            "ai_output_tokens": "INTEGER",
            "ai_total_tokens": "INTEGER",
            "parsed_results": "INTEGER NOT NULL DEFAULT 0",
            "schema_valid_results": "INTEGER NOT NULL DEFAULT 0",
            "valid_http_urls": "INTEGER NOT NULL DEFAULT 0",
            "open_or_unknown_count": "INTEGER NOT NULL DEFAULT 0",
            "verified_open": "INTEGER NOT NULL DEFAULT 0",
            "verified_closed": "INTEGER NOT NULL DEFAULT 0",
            "verified_invalid": "INTEGER NOT NULL DEFAULT 0",
            "verification_unknown": "INTEGER NOT NULL DEFAULT 0",
            "duplicates_active": "INTEGER NOT NULL DEFAULT 0",
            "duplicates_ignored": "INTEGER NOT NULL DEFAULT 0",
            "duplicates_excluded": "INTEGER NOT NULL DEFAULT 0",
            "rejected_count": "INTEGER NOT NULL DEFAULT 0",
            "inserted_count": "INTEGER NOT NULL DEFAULT 0",
            "web_search_calls": "INTEGER NOT NULL DEFAULT 0",
            "search_call_count": "INTEGER NOT NULL DEFAULT 0",
            "search_input_tokens": "INTEGER", "search_output_tokens": "INTEGER", "search_total_tokens": "INTEGER",
            "search_provider_errors": "TEXT NOT NULL DEFAULT '[]'",
            "web_domains": "TEXT NOT NULL DEFAULT '[]'",
            "web_search_actions": "TEXT NOT NULL DEFAULT '[]'",
            "web_search_queries": "TEXT NOT NULL DEFAULT '[]'",
            "web_sources": "TEXT NOT NULL DEFAULT '[]'",
            "rejection_reasons": "TEXT NOT NULL DEFAULT '[]'",
            "profile_summary": "TEXT", "prompt_summary": "TEXT",
        }.items():
            if name not in discovery_columns:
                db.execute(f"ALTER TABLE discovery_runs ADD COLUMN {name} {declaration}")
        candidate_columns = {row[1] for row in db.execute("PRAGMA table_info(discovery_candidates)")}
        for name, declaration in {
            "retrieval_mode": "TEXT NOT NULL DEFAULT 'SEARXNG'",
            "search_provider": "TEXT NOT NULL DEFAULT 'searxng'",
        }.items():
            if name not in candidate_columns:
                db.execute(f"ALTER TABLE discovery_candidates ADD COLUMN {name} {declaration}")
        db.commit()


class ApplicationCreate(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    position: str = Field(min_length=1, max_length=300)
    url: str
    source: str = ""
    location: str = ""
    description: str = ""
    deadline: str | None = None


class ApplicationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str | None = None
    position: str | None = None
    source: str | None = None
    location: str | None = None
    company_postal_address: str | None = None
    deadline: str | None = None
    application_channel: str | None = None
    next_action: str | None = None
    next_action_at: str | None = None
    interview_at: str | None = None
    interview_notes: str | None = None
    notes: str | None = None


class StatusChange(BaseModel):
    status: str
    details: str = ""


class Preparation(BaseModel):
    cv_variant: str | None = None
    cv_path: str | None = None
    application_channel: str | None = None


class Discovery(BaseModel):
    provider: Literal["smartrecruiters"] = "smartrecruiters"
    company: str = Field(min_length=1)
    query: str = ""


class CodexSettings(BaseModel):
    model: str = Field(min_length=1)
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"


class ApiKeyPayload(BaseModel):
    api_key: str = Field(min_length=20, max_length=500)


class ActiveProviderSettings(BaseModel):
    provider_id: Literal["openai", "anthropic_api", "gemini_api", "deepseek_api"]
    auth_mode: Literal["chatgpt_oauth", "api_key"]
    model: str = Field(min_length=1)
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    confirm_api_billing: bool = False


class SearchSettings(BaseModel):
    mode: Literal["SEARXNG", "AI_DIRECT"] = "SEARXNG"
    searxng_url: str = Field(default=DEFAULT_SEARXNG_URL, min_length=8, max_length=500)
    provider: Literal["chatgpt_oauth", "openai", "anthropic_api", "gemini_api", "deepseek_api"] | None = None
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    confirm_api_billing: bool = False

    @field_validator("searxng_url")
    @classmethod
    def valid_searxng_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if urlsplit(value).scheme not in {"http", "https"}:
            raise ValueError("Invalid SearXNG URL")
        return value


class OfferAgeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_offer_age_days: int = Field(strict=True, ge=1, le=365)


class PipelineProviderSettings(BaseModel):
    provider: Literal["default", "chatgpt_oauth", "openai", "anthropic_api", "gemini_api", "deepseek_api"] = "default"
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"


class PipelineSettings(BaseModel):
    overrides: dict[Literal["screening", "deep_analysis", "company_analysis", "application_preparation"], PipelineProviderSettings]
    ai_fallback: Literal["chatgpt_oauth", "openai", "anthropic_api", "gemini_api", "deepseek_api"] | None = None
    confirm_api_billing: bool = False


class SearchProfileSettings(BaseModel):
    mode: Literal["knowledge_base", "custom_prompt"]
    custom_search_prompt: str | None = Field(default=None, max_length=12000)
    knowledge_base_selected_files: list[str] | None = None
    knowledge_base_root: str | None = None

    @field_validator("custom_search_prompt")
    @classmethod
    def trim_prompt(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


MAX_REASONING_MODELS = {
    "gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
}


def supported_reasoning_efforts(model: dict[str, Any]) -> list[str]:
    direct = model.get("reasoningEfforts")
    if isinstance(direct, list):
        return [value for value in direct if value in REASONING_EFFORTS] or ["medium"]
    exposed = model.get("supportedReasoningEfforts")
    if isinstance(exposed, list):
        return [
            item["reasoningEffort"] for item in exposed
            if isinstance(item, dict) and item.get("reasoningEffort") in REASONING_EFFORTS
        ]
    slug = model.get("model") or model.get("id")
    if slug in MAX_REASONING_MODELS:
        return list(REASONING_EFFORTS)
    if slug == "gpt-5.5":
        return list(REASONING_EFFORTS[:-1])
    return ["medium"]


def model_capabilities(model: dict[str, Any], fallback: dict[str, bool] | None = None) -> dict[str, bool]:
    value = model.get("capabilities")
    return {**EMPTY_CAPABILITIES, **(value if isinstance(value, dict) else fallback or {})}


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def observed_web_metadata(per_query_stats: list[dict[str, Any]]) -> dict[str, Any]:
    calls = [item["web_search_calls"] for item in per_query_stats
             if type(item.get("web_search_calls")) is int]
    sources = set()
    sources_available = False
    for item in per_query_stats:
        if not isinstance(item.get("web_sources"), list):
            continue
        sources_available = True
        for value in item["web_sources"]:
            if not isinstance(value, str):
                continue
            try:
                sources.add(canonical_url(value))
            except ValueError:
                continue
    metadata = {
        "web_search_calls": sum(calls) if calls else None,
        "web_sources": sorted(sources) if sources_available else None,
    }
    for key in ("web_domains", "web_search_actions", "web_search_queries"):
        lists = [item[key] for item in per_query_stats if isinstance(item.get(key), list)]
        metadata[key] = sorted({value for values in lists for value in values if isinstance(value, str)}) if lists else None
    return metadata


def discovery_run_view(row: sqlite3.Row) -> dict[str, Any]:
    result = row_dict(row)
    try:
        queries = json.loads(result.get("per_query_stats") or "[]")
    except (ValueError, TypeError):
        queries = []
    queries = [item for item in queries if isinstance(item, dict)] if isinstance(queries, list) else []
    instrumented = any(item.get("metrics_version") == 1 for item in queries)
    recorded_funnel = instrumented or bool(queries) and result["status"] in {"SUCCESS", "PARTIAL"}
    if not recorded_funnel:
        for key in (
            "raw_result_count", "admitted_result_count", "merged_count",
            "intra_query_duplicate_count", "global_duplicate_count", "known_url_count", "unknown_url_count",
            "likely_job_detail_count", "unknown_candidate_count", "obvious_non_job_count", "candidate_count",
            "fetch_attempted", "fetch_succeeded", "fetch_failed", "snippet_fallback_count",
            "ai_input_count", "ai_batch_count", "ai_decision_count", "ai_keep_count", "ai_reject_count",
            "ai_review_count", "ai_missing_decision_count", "ai_retry_count", "ai_call_count", "search_call_count",
        ):
            result[key] = None
    attempted = [item for item in queries if item.get("status") != "PENDING"]
    for key in ("extracted_url_count", "invalid_url_count"):
        counts = [item[key] for item in attempted or queries if type(item.get(key)) is int]
        result[key] = sum(counts) if counts else None
    if instrumented:
        for key in ("raw_result_count", "intra_query_duplicate_count"):
            counts = [item[key] for item in attempted or queries if type(item.get(key)) is int]
            result[key] = sum(counts) if counts else None
    metadata = observed_web_metadata(queries)
    result["source_url_count"] = len(metadata["web_sources"]) if metadata["web_sources"] is not None else None
    if result.get("search_mode") == "AI_DIRECT":
        result["web_search_calls"] = metadata["web_search_calls"]
        for key in ("web_sources", "web_domains", "web_search_actions", "web_search_queries"):
            result[key] = json.dumps(metadata[key], ensure_ascii=False)
    return result


def profile_summary(context: str, mode: str) -> str:
    source = ", ".join(re.findall(r"--- SOURCE LOCALE: (.+?) ---", context)) if mode == "knowledge_base" else "Custom prompt"
    return f"Source: {source}; {len(context)} characters"


def parse_discovery_results(
    raw: dict[str, Any], expected_candidate_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items = raw.get("decisions") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ValueError("Invalid structured response : decisions must be an array")
    valid: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    reasons: list[dict[str, Any]] = []
    schema_valid = valid_urls = 0
    for index, item in enumerate(items):
        try:
            decision = ScreeningDecision.model_validate(item)
        except ValidationError as error:
            reason = "MISSING_REQUIRED_FIELD" if any(detail["type"] == "missing" for detail in error.errors()) else "INVALID_SCHEMA"
            reasons.append({"index": index, "reason": reason})
            continue
        candidate_id = decision.candidate_id
        schema_valid += 1
        if expected_candidate_ids is not None and candidate_id not in expected_candidate_ids:
            reasons.append({"index": index, "candidate_id": candidate_id, "reason": "UNKNOWN_CANDIDATE_ID"})
            continue
        if candidate_id in valid or candidate_id in duplicate_ids:
            valid.pop(candidate_id, None)
            duplicate_ids.add(candidate_id)
            reasons.append({"index": index, "candidate_id": candidate_id, "reason": "DUPLICATE_CANDIDATE_ID"})
            continue
        if decision.offer is not None:
            try:
                decision.offer.url = normalize_url(decision.offer.url)
                decision.offer.evidence_urls = [normalize_url(value) for value in decision.offer.evidence_urls]
            except ValueError as error:
                reasons.append({"index": index, "candidate_id": candidate_id, "reason": "INVALID_URL", "detail": str(error)})
                continue
            valid_urls += 1
        valid[candidate_id] = decision.model_dump(mode="json")
    expected = expected_candidate_ids or set(valid)
    missing_ids = sorted(expected - valid.keys())
    counters = {
        "provider_results_raw": len(items), "parsed_results": len(items),
        "schema_valid_results": schema_valid, "valid_http_urls": valid_urls,
        "missing_candidate_ids": missing_ids, "duplicate_candidate_ids": sorted(duplicate_ids),
        "rejection_reasons": reasons,
    }
    return [valid[candidate_id] for candidate_id in sorted(valid)], counters


def screen_candidate_batches(candidates: list[dict[str, Any]], request, batch_size: int = 30, progress=None):
    final: dict[str, dict[str, Any]] = {}
    metrics = {
        "ai_batch_count": 0, "ai_input_count": 0, "ai_decision_count": 0,
        "ai_keep_count": 0, "ai_reject_count": 0, "ai_review_count": 0,
        "ai_missing_decision_count": 0, "ai_retry_count": 0, "ai_call_count": 0,
    }
    diagnostics: list[dict[str, Any]] = []

    def report_progress():
        metrics["ai_decision_count"] = len(final)
        for decision in ("keep", "reject", "review"):
            metrics[f"ai_{decision}_count"] = sum(item["decision"].casefold() == decision for item in final.values())
        if progress:
            progress(dict(metrics))

    report_progress()
    for offset in range(0, len(candidates), batch_size):
        batch = candidates[offset:offset + batch_size]
        batch_number = offset // batch_size + 1
        metrics["ai_batch_count"] += 1
        metrics["ai_input_count"] += len(batch)
        for candidate in batch:
            candidate["ai_batch"] = batch_number
        metrics["ai_call_count"] += 1
        report_progress()
        decisions, current = request(batch, batch_number, False)
        diagnostics.extend(current.get("rejection_reasons", []))
        final.update({item["candidate_id"]: item for item in decisions})
        missing = [item for item in batch if item["candidate_id"] in current["missing_candidate_ids"]]
        metrics["ai_missing_decision_count"] += len(missing)
        report_progress()
        if missing:
            metrics["ai_retry_count"] += 1
            metrics["ai_call_count"] += 1
            report_progress()
            retry_decisions, retry = request(missing, batch_number, True)
            diagnostics.extend(retry.get("rejection_reasons", []))
            final.update({item["candidate_id"]: item for item in retry_decisions})
        for candidate in missing:
            candidate_id = candidate["candidate_id"]
            if candidate_id not in final:
                final[candidate_id] = ScreeningDecision(
                    candidate_id=candidate_id, decision="REVIEW", reason_code="AI_NO_DECISION",
                    eligibility_status="ELIGIBILITY_UNCERTAIN",
                    reason="No valid decision after a targeted retry.", offer=None,
                ).model_dump(mode="json")
        report_progress()
    ordered = [final[item["candidate_id"]] for item in candidates]
    return ordered, metrics, diagnostics


def offer_freshness_reason(
    published_at: str | None, max_age_days: int, *, today: date | None = None,
) -> str | None:
    published = parse_offer_date(published_at)
    if published is None:
        return "PUBLICATION_DATE_UNKNOWN" if not published_at else "PUBLICATION_DATE_INVALID"
    age = ((today or date.today()) - published).days
    if age < 0:
        return "PUBLICATION_DATE_IN_FUTURE"
    return "OFFER_TOO_OLD" if age > max_age_days else None


def offer_expiration_reason(offer: dict[str, Any], *, today: date | None = None) -> str | None:
    if offer.get("availability") in {"closed", "expired"}:
        return "OFFER_CLOSED"
    return "DEADLINE_EXPIRED" if deadline_expired(offer.get("deadline"), today=today) else None


def verify_discovery_results(
    offers: list[dict[str, Any]], diagnostics: dict[str, Any], verifier: OfferVerifier,
    max_age_days: int,
) -> list[dict[str, Any]]:
    verified = []
    counts = {"OPEN": 0, "CLOSED": 0, "INVALID": 0, "UNKNOWN": 0}
    for item in offers:
        result = verifier.verify(item["url"], item)
        status = (
            "UNKNOWN"
            if result.status == "OPEN" and result.provider == "generic" and item["availability"] == "unknown"
            else result.status
        )
        expiration = offer_expiration_reason(item)
        # A later feed or shorter fetch must not renew an older source publication.
        source_publication = parse_offer_date(item.get("published_at"))
        verified_publication = parse_offer_date(result.published_at)
        if verified_publication and (not source_publication or verified_publication < source_publication):
            item["published_at"] = result.published_at
        source_deadline = parse_offer_date(item.get("deadline"))
        verified_deadline = parse_offer_date(result.deadline)
        if verified_deadline and (not source_deadline or verified_deadline < source_deadline):
            item["deadline"] = result.deadline
        expiration = expiration or offer_expiration_reason(item)
        if expiration:
            status = "CLOSED"
        item["verification_status"] = status
        counts[status] += 1
        if status in {"CLOSED", "INVALID"}:
            diagnostics["rejection_reasons"].append({"url": item["url"], "reason": expiration or result.reason})
            continue
        if reason := offer_freshness_reason(item.get("published_at"), max_age_days):
            item["freshness_rejection_reason"] = reason
            diagnostics["rejection_reasons"].append({"url": item["url"], "reason": reason})
            continue
        item["availability"] = status.lower()
        verified.append(item)
    diagnostics.update({
        "verified_open": counts["OPEN"], "verified_closed": counts["CLOSED"],
        "verified_invalid": counts["INVALID"], "verification_unknown": counts["UNKNOWN"],
        "open_or_unknown_count": len(verified),
        "rejected_count": len(diagnostics["rejection_reasons"]),
    })
    return verified


def get_application(db: sqlite3.Connection, application_id: int, include_deleted: bool = False) -> sqlite3.Row:
    sql = "SELECT * FROM applications WHERE id = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    row = db.execute(sql, (application_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Application not found")
    return row


def add_event(
    db: sqlite3.Connection,
    application_id: int,
    event_type: str,
    details: str = "",
    created_at: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO events (application_id, event_type, details, created_at) VALUES (?, ?, ?, ?)",
        (application_id, event_type, details, created_at or now()),
    )


def insert_application(
    db: sqlite3.Connection,
    data: dict[str, Any],
    status: str = "DETECTED",
    timestamp: str | None = None,
    event_details: str = "",
) -> int:
    if status not in STATUSES:
        raise ValueError(f"Unknown status : {status}")
    created_at = timestamp or now()
    normalized_url = normalize_url(data["url"])
    eligibility_status, eligibility_reason = evaluate_offer_eligibility(data)
    next_action, next_action_at = tracking_for_status(status, created_at if status == "SENT" else None)
    fields = {
        "company": data["company"].strip(),
        "position": data["position"].strip(),
        "url": normalized_url,
        "source": data.get("source", "").strip(),
        "location": data.get("location", "").strip(),
        "description": data.get("description", ""),
        "contract_type": data.get("contract_type"),
        "published_at": data.get("published_at"),
        "start_date": data.get("start_date"),
        "availability": data.get("availability"),
        "why_relevant": data.get("why_relevant", ""),
        "eligibility_status": eligibility_status,
        "eligibility_reason": eligibility_reason,
        "detected_at": data.get("detected_at") or created_at,
        "deadline": data.get("deadline"),
        "status": status,
        "cv_variant": data.get("cv_variant"),
        "cv_path": data.get("cv_path"),
        "interview_prep_path": data.get("interview_prep_path"),
        "application_channel": data.get("application_channel") or detect_application_channel(normalized_url),
        "sent_at": created_at if status == "SENT" else data.get("sent_at"),
        "next_action": next_action,
        "next_action_at": next_action_at,
        "notes": data.get("notes", ""),
        "created_at": created_at,
        "updated_at": created_at,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    cursor = db.execute(
        f"INSERT INTO applications ({columns}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    application_id = cursor.lastrowid
    add_event(db, application_id, "OFFER_DETECTED", event_details, created_at)
    if status != "DETECTED":
        add_event(db, application_id, EVENT_FOR_STATUS[status], event_details, created_at)
    return application_id


def cleanup_inactive_application_artifacts(
    db: sqlite3.Connection,
    application_id: int,
    repo_root: Path = REPO_ROOT,
    applications_root: Path = DEFAULT_APPLICATIONS_ROOT,
    dry_run: bool = False,
) -> dict[str, Any]:
    row = get_application(db, application_id)
    if row["status"] not in INACTIVE_STATUSES:
        raise ValueError("Cleanup is restricted to WITHDRAWN or IGNORED applications")

    root = applications_root.resolve()
    folder_slug = slugify(f"{row['company']}-{row['position']}")
    folder_names = {folder_slug, f"{folder_slug}-{application_id}"}
    report: dict[str, Any] = {
        "application_id": application_id,
        "status": row["status"],
        "deleted": [],
        "kept": [],
        "errors": [],
        "bytes": 0,
        "references_cleared": [],
    }

    def resolve(value: str) -> Path:
        path = Path(value)
        return (repo_root / path).resolve() if not path.is_absolute() else path.resolve()

    def safe(path: Path) -> bool:
        try:
            relative = path.relative_to(root)
        except ValueError:
            return False
        return len(relative.parts) > 1 and relative.parts[0] in folder_names

    linked_paths: dict[Path, set[int]] = {}
    for linked in db.execute(
        "SELECT id, cv_path, cover_letter_path FROM applications "
        "WHERE cv_path IS NOT NULL OR cover_letter_path IS NOT NULL"
    ):
        for field in ("cv_path", "cover_letter_path"):
            if linked[field]:
                linked_paths.setdefault(resolve(linked[field]), set()).add(linked["id"])

    candidates: set[Path] = set()
    kept_paths: set[tuple[Path, str]] = set()

    def keep(path: Path, reason: str) -> None:
        key = (path, reason)
        if key not in kept_paths:
            kept_paths.add(key)
            report["kept"].append({"path": str(path), "reason": reason})

    def queue(path: Path) -> None:
        path = path.resolve()
        if not safe(path):
            keep(path, "path not allowed")
        elif linked_paths.get(path, set()) - {application_id}:
            keep(path, "shared file")
        else:
            candidates.add(path)

    direct_paths: dict[str, Path] = {}
    for field in ("cv_path", "cover_letter_path"):
        value = row[field]
        if not value:
            continue
        path = resolve(value)
        direct_paths[field] = path
        if not safe(path):
            keep(path, "path not allowed")
            continue
        if linked_paths.get(path, set()) - {application_id}:
            keep(path, "shared file")
            continue
        if path.suffix.lower() not in {".docx", ".pdf"}:
            keep(path, "ambiguous file type")
            continue
        queue(path)
        queue(path.with_suffix(".pdf"))

    application_folders = [root / name for name in folder_names if (root / name).is_dir()]
    for folder in application_folders:
        queue(folder / "lettre-motivation.docx")
        queue(folder / "lettre-motivation.pdf")
        cv_folder = folder / "cv"
        if cv_folder.is_dir():
            for path in cv_folder.iterdir():
                if path.is_file() and (
                    path.suffix.lower() in {".docx", ".pdf", ".tmp"}
                    or path.name.startswith("~$")
                ):
                    queue(path)
        for path in folder.iterdir():
            if path.is_file() and (path.suffix.lower() == ".tmp" or path.name.startswith("~$")):
                queue(path)

    for folder in application_folders:
        for path in folder.rglob("*"):
            resolved = path.resolve()
            already_kept = any(kept_path == resolved for kept_path, _reason in kept_paths)
            if path.is_file() and path.suffix.lower() != ".md" and resolved not in candidates and not already_kept:
                keep(path.resolve(), "ambiguous file type")

    failed_paths: set[Path] = set()
    for path in sorted(candidates, key=str):
        if not path.is_file():
            continue
        size = path.stat().st_size
        if dry_run:
            report["deleted"].append(str(path))
            report["bytes"] += size
            continue
        try:
            path.unlink()
        except OSError as error:
            failed_paths.add(path)
            report["errors"].append({"path": str(path), "reason": str(error)})
            LOGGER.warning("Artifact not deleted for application %s: %s", application_id, path)
        else:
            report["deleted"].append(str(path))
            report["bytes"] += size

    for field, path in direct_paths.items():
        if path not in failed_paths:
            report["references_cleared"].append(field)

    if not dry_run:
        if report["references_cleared"]:
            assignments = [f"{field} = NULL" for field in report["references_cleared"]]
            if "cover_letter_path" in report["references_cleared"]:
                assignments.append("cover_letter_word_count = NULL")
            db.execute(
                f"UPDATE applications SET {', '.join(assignments)}, updated_at = ? WHERE id = ?",
                (now(), application_id),
            )
        if any(report[key] for key in ("deleted", "kept", "errors", "references_cleared")):
            add_event(db, application_id, "ARTIFACTS_CLEANED", json.dumps(report, ensure_ascii=False))
            db.commit()
    return report


def change_status(
    db: sqlite3.Connection,
    application_id: int,
    target: str,
    details: str = "",
    repo_root: Path = REPO_ROOT,
    applications_root: Path = DEFAULT_APPLICATIONS_ROOT,
) -> dict[str, Any]:
    row = get_application(db, application_id)
    current = row["status"]
    if target not in STATUSES:
        raise HTTPException(422, "Unknown status")
    if target not in TRANSITIONS[current]:
        raise HTTPException(409, f"Transition not allowed : {current} to {target}")
    if target == "SHORTLISTED" and row["eligibility_status"] == "INELIGIBLE":
        raise HTTPException(409, f"Shortlisting not allowed : {row['eligibility_reason']}")
    timestamp = now()
    sent_at = timestamp if target == "SENT" else row["sent_at"]
    next_action, next_action_at = tracking_for_status(target, sent_at if target == "SENT" else None)
    db.execute(
        "UPDATE applications SET status = ?, sent_at = ?, next_action = ?, next_action_at = ?, updated_at = ? WHERE id = ?",
        (target, sent_at, next_action, next_action_at, timestamp, application_id),
    )
    add_event(db, application_id, EVENT_FOR_STATUS[target], details or f"{current} to {target}")
    db.commit()
    if target in INACTIVE_STATUSES:
        try:
            cleanup_inactive_application_artifacts(
                db, application_id, repo_root=repo_root, applications_root=applications_root
            )
        except Exception as error:
            LOGGER.exception("Artifact cleanup failed for application %s", application_id)
            add_event(db, application_id, "ARTIFACT_CLEANUP_FAILED", str(error))
            db.commit()
    return row_dict(get_application(db, application_id))


def reset_to_prepared(db: sqlite3.Connection, application_id: int) -> dict[str, Any]:
    row = get_application(db, application_id)
    if row["status"] == "PREPARED":
        return row_dict(row)
    sent_event = db.execute(
        "SELECT 1 FROM events WHERE application_id = ? AND event_type = 'SENT' LIMIT 1",
        (application_id,),
    ).fetchone()
    if row["sent_at"] or sent_event:
        raise HTTPException(409, "Reset blocked: an actual submission was recorded")
    if row["status"] not in {"AWAITING_VALIDATION", "APPROVED", "SUBMITTING"}:
        raise HTTPException(409, f"Reset not allowed from {row['status']}")
    timestamp = now()
    next_action, next_action_at = tracking_for_status("PREPARED")
    channel = row["application_channel"] or detect_application_channel(row["url"])
    db.execute(
        """UPDATE applications
           SET status = 'PREPARED', application_channel = ?, sent_at = NULL,
               next_action = ?, next_action_at = ?, updated_at = ?
           WHERE id = ?""",
        (channel, next_action, next_action_at, timestamp, application_id),
    )
    add_event(
        db, application_id, "RESET_TO_PREPARED",
        "Reset to PREPARED to review workflow; no actual submission made.",
        timestamp,
    )
    db.commit()
    return row_dict(get_application(db, application_id))


def relative_to_repo(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        # Test instances may inject a temporary application root outside the repository.
        return str(resolved)


def resolve_repo_path(value: str, repo_root: Path = REPO_ROOT, applications_root: Path | None = None) -> Path:
    path = (repo_root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    allowed_roots = [repo_root.resolve()]
    if applications_root is not None:
        allowed_roots.append(applications_root.resolve())
    if not any(path.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(400, "Path outside configured roots")
    return path


def application_folder(root: Path, application_id: int, company: str, position: str) -> Path:
    return root / f"{slugify(f'{company}-{position}')}-{application_id}"


def discover_smartrecruiters(company: str, query: str) -> list[dict[str, Any]]:
    url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings?{urlencode({'q': query, 'limit': 100})}"
    request = URLRequest(url, headers={"User-Agent": "job-tracker-local/1.0"})
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except Exception as error:
        raise HTTPException(502, f"SmartRecruiters unavailable : {error}") from error
    jobs = []
    for item in payload.get("content", []):
        location = item.get("location") or {}
        jobs.append({
            "company": item.get("company", {}).get("name") or company,
            "position": item.get("name", ""),
            "url": item.get("ref") or f"https://jobs.smartrecruiters.com/{company}/{item.get('id', '')}",
            "source": "SmartRecruiters",
            "location": ", ".join(filter(None, [location.get("city"), location.get("region"), location.get("country")])),
            "description": "",
            "published_at": item.get("releasedDate"),
            "deadline": item.get("validThrough"),
            "availability": str(item.get("status") or "open").lower(),
            "detected_at": now(),
        })
    return jobs


def create_app(
    db_path: Path = DEFAULT_DB,
    applications_root: Path = DEFAULT_APPLICATIONS_ROOT,
    auth: ChatGPTAuth | None = None,
    codex_factory=None,
    api_key_store=None,
    openai_factory=None,
    provider_factories=None,
    repo_root: Path = REPO_ROOT,
    pdf_converter=convert_docx_to_pdf,
    offer_http_client_factory=None,
    offer_resolver=None,
    search_provider_factory=None,
) -> FastAPI:
    init_db(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with closing(database()) as db:
            interrupted = db.execute("""
                SELECT a.id FROM applications a
                JOIN events e ON e.id = (
                    SELECT id FROM events WHERE application_id = a.id
                    AND event_type IN ('PREPARATION_QUEUED', 'PREPARATION_STARTED', 'PREPARATION_FAILED')
                    ORDER BY id DESC LIMIT 1
                )
                WHERE a.status = 'SHORTLISTED'
                AND e.event_type IN ('PREPARATION_QUEUED', 'PREPARATION_STARTED')
            """).fetchall()
            for row in interrupted:
                add_event(db, row["id"], "PREPARATION_FAILED", "Preparation interrupted by backend restart")
            db.commit()
        warm_up_libreoffice()
        try:
            yield
        finally:
            try:
                client = app.state.codex_client
                if client is not None and hasattr(client, "close"):
                    client.close()
            finally:
                if auth is None:
                    app.state.auth.close()

    api = FastAPI(title="AI Job Application Workbench", version="1.0.0", lifespan=lifespan)
    api.state.db_path = db_path
    api.state.applications_root = applications_root
    with closing(connect(db_path)) as configured_db:
        configured_root = configured_db.execute("SELECT value FROM settings WHERE key='knowledge_base_root'").fetchone()
    api.state.repo_root = Path(configured_root[0]) if configured_root else repo_root
    api.state.auth = auth or ChatGPTAuth()
    api.state.codex_factory = codex_factory or (lambda token: CodexClient(token, APP_ROOT))
    api.state.api_key_store = api_key_store or ApiKeyStore()
    api.state.provider_factories = {
        OPENAI_PROVIDER: openai_factory or (lambda api_key: OpenAIProvider(api_key)),
        ANTHROPIC_PROVIDER: lambda api_key: AnthropicProvider(api_key),
        GEMINI_PROVIDER: lambda api_key: GeminiProvider(api_key),
        DEEPSEEK_PROVIDER: lambda api_key: DeepSeekProvider(api_key),
        **(provider_factories or {}),
    }
    api.state.codex_client = None
    api.state.offer_http_client_factory = offer_http_client_factory or (
        lambda: httpx.Client(timeout=10, headers={"User-Agent": "job-tracker-local/1.0"})
    )
    api.state.search_provider_factory = search_provider_factory or (
        lambda url: SearXNGProvider(url)
    )
    api.state.codex_client_lock = threading.Lock()
    api.state.discovery_lock = threading.Lock()
    api.state.preparation_lock = threading.Lock()
    api.state.preparation_queue = deque()
    api.state.preparation_jobs = set()
    api.state.active_preparations = 0
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["*"], allow_headers=["*"],
        expose_headers=["Content-Disposition", "X-PDF-Warning"],
    )

    def database() -> sqlite3.Connection:
        return connect(api.state.db_path)

    def application_view(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = row_dict(row)
        for field in (
            "company_completed_achievements", "company_planned_developments",
            "company_comparable_actors", "company_sources",
        ):
            try:
                result[field] = json.loads(result[field] or "[]")
            except json.JSONDecodeError:
                result[field] = []
        result["company_address_warning"] = None if valid_postal_address(row["company_postal_address"]) else "Verify company address"
        result["missing_information"] = []
        event = db.execute(
            "SELECT event_type, details FROM events WHERE application_id=? AND event_type IN ('PREPARATION_QUEUED', 'PREPARATION_STARTED', 'PREPARATION_FAILED') ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        result["preparation_state"] = (
            "running" if row["status"] == "SHORTLISTED" and event and event["event_type"] == "PREPARATION_STARTED"
            else "queued" if row["status"] == "SHORTLISTED" and event and event["event_type"] == "PREPARATION_QUEUED"
            else "failed" if row["status"] == "SHORTLISTED" and event and event["event_type"] == "PREPARATION_FAILED"
            else "failed" if row["status"] == "SHORTLISTED"
            else "completed" if row["status"] == "PREPARED"
            else None
        )
        result["artifacts"] = (
            validate_application_artifacts(row, api.state.repo_root, api.state.applications_root)
            if row["status"] == "PREPARED" or any(row[field] for field in FILE_FIELDS.values())
            else None
        )
        result["preparation_error"] = (
            event["details"] if result["preparation_state"] == "failed" and event
            else "Preparation needs retry" if result["preparation_state"] == "failed"
            else None
        )
        action, sent_follow_up = tracking_for_status(row["status"], row["sent_at"])
        result["next_action"] = (
            "Retry preparation"
            if result["preparation_state"] == "failed"
            else action
        )
        if row["status"] == "SENT":
            result["next_action_at"] = sent_follow_up
        elif row["status"] in {"REJECTED", "WITHDRAWN", "NO_RESPONSE", "IGNORED"}:
            result["next_action_at"] = None
        result["application_channel"] = row["application_channel"] or detect_application_channel(row["url"])
        cv_event = db.execute(
            "SELECT details FROM events WHERE application_id=? AND event_type='CODEX_PREPARATION' ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if cv_event:
            try:
                details = json.loads(cv_event["details"])
                result["cv_action"] = details.get("action")
                result["cv_justification"] = details.get("justification")
                result["missing_information"] = details.get("missing_information", [])
            except json.JSONDecodeError:
                result["cv_justification"] = cv_event["details"]
        ignored_event = db.execute(
            "SELECT created_at FROM events WHERE application_id=? AND event_type='IGNORED' ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        result["ignored_at"] = ignored_event["created_at"] if ignored_event else None
        return result

    def rewrite_company_files(row: sqlite3.Row) -> None:
        profile = {
            "description": row["company_description"],
            "postal_address": row["company_postal_address"],
            "relevant_domain": row["company_domain"],
            "completed_achievements": json.loads(row["company_completed_achievements"] or "[]"),
            "planned_developments": json.loads(row["company_planned_developments"] or "[]"),
            "competitors_or_comparable_actors": json.loads(row["company_comparable_actors"] or "[]"),
        }
        if row["company_path"] and profile["description"] and profile["relevant_domain"]:
            path = (api.state.repo_root / row["company_path"]).resolve()
            if path.is_file():
                path.write_text(company_markdown(row["company"], profile), encoding="utf-8")
        if row["cover_letter_path"]:
            path = (api.state.repo_root / row["cover_letter_path"]).resolve()
            if path.is_file() and path.suffix.lower() == ".docx":
                content = read_cover_letter_docx(path)
                language = "en" if content.salutation.casefold().startswith("dear") else "fr"
                create_cover_letter_docx(
                    api.state.repo_root, path, row["company"], row["position"],
                    row["company_postal_address"], list(content.paragraphs), language,
                    created_on=cover_letter_date(path), salutation=content.salutation,
                    closing=content.closing,
                )

    def settings(db: sqlite3.Connection, *keys: str) -> dict[str, str]:
        placeholders = ",".join("?" for _ in keys)
        return dict(db.execute(f"SELECT key, value FROM settings WHERE key IN ({placeholders})", keys))

    def write_settings(db: sqlite3.Connection, values: dict[str, str]) -> None:
        db.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            values.items(),
        )

    def search_kb_root(db: sqlite3.Connection) -> Path:
        configured = settings(db, "knowledge_base_root").get("knowledge_base_root")
        return Path(configured) if configured else api.state.repo_root.resolve()

    def search_profile(db: sqlite3.Connection, mode: str | None = None) -> dict[str, Any]:
        root = search_kb_root(db)
        rows = settings(
            db, "search_profile_mode", "custom_search_prompt", "custom_search_prompt_updated_at",
            "knowledge_base_selected_files",
        )
        mode = mode or rows.get("search_profile_mode", "custom_prompt")
        files = knowledge_base_files(root) if mode == "knowledge_base" else []
        available = bool(files)
        selected = json.loads(rows["knowledge_base_selected_files"]) if "knowledge_base_selected_files" in rows else ([
            path.relative_to(root).as_posix() for path in knowledge_base_sources(root) if path in files
        ] if mode == "knowledge_base" else [])
        selected = [source for source in selected if all(knowledge_base_directory_allowed(part) for part in Path(source).parts[:-1])]
        def label(source: str) -> str:
            return {"Profil": "Profile", "Domaines": "Domains", "Stage M2": "Career objective"}.get(Path(source).stem, Path(source).stem)
        return {
            "knowledge_base_root": str(root) if mode == "knowledge_base" else "",
            "knowledge_base_files": [
                {"path": path.relative_to(root).as_posix(), "label": label(path.relative_to(root).as_posix())}
                for path in files
            ],
            "knowledge_base_selected_files": selected if mode == "knowledge_base" else [],
            "mode": mode,
            "custom_search_prompt": rows.get("custom_search_prompt", ""),
            "custom_search_prompt_updated_at": rows.get("custom_search_prompt_updated_at"),
            "knowledge_base_available": available,
            "knowledge_base_sources": [label(source) for source in selected] if mode == "knowledge_base" else [],
            "candidate_profile_available": knowledge_base_available(api.state.repo_root) if mode == "knowledge_base" else None,
        }

    def active_settings(db: sqlite3.Connection) -> tuple[str, str, str, str]:
        rows = settings(db, "ai_provider", "ai_auth_mode", "ai_model", "ai_reasoning_effort")
        if not rows.get("ai_auth_mode"):
            raise HTTPException(409, "No active AI provider")
        if not rows.get("ai_model"):
            raise HTTPException(409, "Select a model")
        return rows.get("ai_provider", OPENAI_PROVIDER), rows["ai_auth_mode"], rows["ai_model"], rows.get("ai_reasoning_effort", "medium")

    def codex():
        try:
            token = api.state.auth.access_token()
            with api.state.codex_client_lock:
                if api.state.codex_client is None:
                    api.state.codex_client = api.state.codex_factory(token)
                return api.state.codex_client
        except PermissionError as error:
            raise HTTPException(401, "ChatGPT session expired. Sign in again.") from error

    def api_provider(provider_id: str):
        if provider_id not in API_PROVIDERS:
            raise HTTPException(422, "Unknown API provider")
        key = api.state.api_key_store.get(provider_id)
        if not key:
            raise HTTPException(401, "API key missing")
        return api.state.provider_factories[provider_id](key)

    def provider_error(error: Exception) -> HTTPException:
        status = getattr(error, "status_code", None)
        if isinstance(error, WebSearchNotExecutedError):
            return HTTPException(502, str(error))
        if isinstance(error, CodexError) and ("token_expired" in str(error) or "401 Unauthorized" in str(error)):
            return HTTPException(401, "ChatGPT session expired. Sign in again.")
        if status == 401:
            return HTTPException(401, "Invalid API key")
        if status == 403:
            return HTTPException(403, "Insufficient permission")
        if status == 429:
            return HTTPException(429, "Quota unavailable")
        if isinstance(error, (CodexError, OSError)):
            return HTTPException(502, f"Codex app-server unavailable : {error}")
        return HTTPException(502, "Provider unavailable")

    def models_for(provider_id: str, auth_mode: str) -> list[dict[str, Any]]:
        provider = None
        try:
            provider = codex() if auth_mode == "chatgpt_oauth" else api_provider(provider_id)
            models = provider.models()
        except HTTPException:
            raise
        except Exception as error:
            raise provider_error(error) from error
        finally:
            if auth_mode != "chatgpt_oauth" and hasattr(provider, "close"):
                provider.close()
        fallback = OPENAI_CAPABILITIES if provider_id == OPENAI_PROVIDER else None
        return [{**model, "reasoningEfforts": supported_reasoning_efforts(model),
                 "capabilities": model_capabilities(model, fallback)} for model in models]

    def provider_settings(choice: str, model: str | None = None, effort: str = "medium") -> dict[str, Any]:
        auth_mode = "chatgpt_oauth" if choice == "chatgpt_oauth" else "api_key"
        provider_id = OPENAI_PROVIDER if choice == "chatgpt_oauth" else choice
        if provider_id not in API_PROVIDERS:
            raise HTTPException(422, "Unknown AI provider")
        available = models_for(provider_id, auth_mode)
        selected = next((item for item in available if (item.get("model") or item.get("id")) == model), None)
        selected = selected or next((item for item in available if item.get("isDefault")), None) or (available[0] if available else None)
        if not selected:
            raise HTTPException(422, "Model unavailable")
        selected_model = selected.get("model") or selected.get("id")
        capabilities = model_capabilities(selected, OPENAI_CAPABILITIES if provider_id == OPENAI_PROVIDER else None)
        supported = supported_reasoning_efforts(selected)
        selected_effort = effort if capabilities["reasoning_effort"] and effort in supported else (
            "medium" if "medium" in supported else supported[0]
        )
        return {
            "choice": choice, "provider_id": provider_id, "auth_mode": auth_mode,
            "model": selected_model, "effort": selected_effort, "capabilities": capabilities,
        }

    def stage_settings(db: sqlite3.Connection, stage: str) -> dict[str, Any]:
        if stage not in PIPELINE_STAGES:
            raise HTTPException(422, "Unknown AI stage")
        rows = settings(
            db, "ai_provider", "ai_auth_mode", "ai_model", "ai_reasoning_effort",
            f"pipeline_{stage}_provider", f"pipeline_{stage}_model", f"pipeline_{stage}_effort",
        )
        choice = rows.get(f"pipeline_{stage}_provider", "default")
        if choice == "default":
            provider_id, auth_mode, model, effort = active_settings(db)
            capabilities = active_capabilities(db, provider_id)
            return {
                "choice": "default", "provider_id": provider_id, "auth_mode": auth_mode,
                "model": model, "effort": effort, "capabilities": capabilities,
            }
        return provider_settings(
            choice, rows.get(f"pipeline_{stage}_model"), rows.get(f"pipeline_{stage}_effort", "medium"),
        )

    def record_usage(
        provider: str, model: str, category: str, operation: str, metadata: dict[str, Any],
    ) -> None:
        metadata = metadata if isinstance(metadata, dict) else {}
        tokens = {
            key: value if type(value := metadata.get(key)) is int and value >= 0 else None
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }
        with closing(database()) as db:
            db.execute(
                """INSERT INTO ai_usage(created_at, provider, model, category, operation,
                    input_tokens, output_tokens, total_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (now(), provider, metadata.get("model") or model, category, operation,
                 tokens["input_tokens"], tokens["output_tokens"], tokens["total_tokens"]),
            )
            db.commit()

    def run_ai(
        prompt: str, model: str, schema: dict[str, Any], effort: str, *, web_search: bool,
        provider_id: str, auth_mode: str, category: str, operation: str, isolated: bool = False,
    ) -> dict[str, Any]:
        try:
            provider = codex() if auth_mode == "chatgpt_oauth" else api_provider(provider_id)
            metadata = {}
            try:
                with tempfile.TemporaryDirectory() if isolated and isinstance(provider, CodexClient) else nullcontext(None) as isolated_cwd:
                    options = {"isolated_cwd": isolated_cwd} if isolated_cwd is not None else {}
                    raw = provider.run(prompt, model, schema, effort=effort, web_search=web_search, **options)
                metadata = getattr(raw, "run_metadata", {})
                return raw
            except Exception as error:
                metadata = getattr(error, "metadata", {})
                raise
            finally:
                try:
                    record_usage(provider_id, model, category, operation, metadata)
                finally:
                    if auth_mode != "chatgpt_oauth" and hasattr(provider, "close"):
                        provider.close()
        except HTTPException:
            raise
        except (CodexStructuredOutputError, ValidationError, ValueError):
            raise
        except Exception as error:
            raise provider_error(error) from error

    def run_stage_ai(
        stage: str, prompt: str, schema: dict[str, Any], validator, *, isolated: bool = False,
    ) -> tuple[Any, dict[str, Any], bool, dict[str, Any]]:
        with closing(database()) as db:
            selected = stage_settings(db, stage)
            fallback_choice = settings(db, "ai_fallback").get("ai_fallback")

        def execute(config: dict[str, Any]):
            LOGGER.info(
                "AI call stage=%s provider=%s model=%s fallback=%s",
                stage, config["provider_id"], config["model"], config is not selected,
            )
            raw = run_ai(
                prompt, config["model"], schema, config["effort"], web_search=False,
                provider_id=config["provider_id"], auth_mode=config["auth_mode"],
                isolated=isolated, category="SEARCH" if stage == "screening" else "DOCUMENT",
                operation={"screening": "offer_screening", "deep_analysis": "analysis",
                           "company_analysis": "company", "application_preparation": "application"}[stage],
            )
            return validator(raw), getattr(raw, "run_metadata", {})

        try:
            result, metadata = execute(selected)
            return result, selected, False, metadata
        except (HTTPException, CodexStructuredOutputError, ValidationError, ValueError) as error:
            status = error.status_code if isinstance(error, HTTPException) else 502
            if not fallback_choice or status not in {408, 429, 500, 502, 503, 504}:
                LOGGER.warning(
                    "AI call failed stage=%s provider=%s model=%s fallback=false error=%s",
                    stage, selected["provider_id"], selected["model"], error,
                )
                raise
            fallback = provider_settings(fallback_choice)
            if (fallback["provider_id"], fallback["auth_mode"]) == (selected["provider_id"], selected["auth_mode"]):
                raise
            LOGGER.warning(
                "AI fallback stage=%s primary=%s fallback=%s error=%s",
                stage, selected["provider_id"], fallback["provider_id"], error,
            )
            result, metadata = execute(fallback)
            return result, fallback, True, metadata

    def search_config(db: sqlite3.Connection) -> dict[str, Any]:
        rows = settings(
            db, "search_mode", "search_provider", "search_ai_provider", "search_ai_model",
            "search_ai_effort", "search_ai_capabilities", "searxng_url",
            "max_offer_age_days",
        )
        legacy_provider = rows.get("search_provider")
        mode = rows.get("search_mode") or ("AI_DIRECT" if legacy_provider == "openai_web_search" else "SEARXNG")
        provider = rows.get("search_ai_provider") or ("openai" if legacy_provider == "openai_web_search" else None)
        return {
            "mode": mode,
            "max_offer_age_days": int(rows.get("max_offer_age_days", DEFAULT_MAX_OFFER_AGE_DAYS)),
            "searxng_url": rows.get("searxng_url", DEFAULT_SEARXNG_URL),
            "provider": provider,
            "model": rows.get("search_ai_model"),
            "effort": rows.get("search_ai_effort", "medium"),
            "capabilities": {**EMPTY_CAPABILITIES, **json.loads(rows.get("search_ai_capabilities", "{}"))},
        }

    def search_ai_settings(config: dict[str, Any]) -> dict[str, Any]:
        if not config.get("provider") or not config.get("model"):
            raise HTTPException(409, "Select a provider and model for direct web search.")
        selected = provider_settings(config["provider"], config["model"], config.get("effort", "medium"))
        if not selected["capabilities"].get("web_search"):
            raise HTTPException(409, "Direct web search is unavailable with this provider/model.")
        return selected

    def configured_search_provider(db: sqlite3.Connection, config: dict[str, Any] | None = None):
        config = config or search_config(db)
        if config["mode"] == "SEARXNG":
            return api.state.search_provider_factory(config["searxng_url"])
        selected = search_ai_settings(config)
        if selected["auth_mode"] == "chatgpt_oauth":
            try:
                provider = ChatGPTOAuthResponsesProvider(api.state.auth.access_token())
            except PermissionError as error:
                raise HTTPException(401, "ChatGPT session expired. Sign in again.") from error
        else:
            provider = api_provider(selected["provider_id"])
        return AIWebSearchProvider(
            provider, selected["choice"], selected["model"], selected["effort"],
            usage_recorder=lambda metadata: record_usage(
                selected["provider_id"], selected["model"], "SEARCH", "offer_search", metadata,
            ),
        )

    def search_queries(profile: dict[str, Any], context: str) -> list[str]:
        if profile["mode"] == "custom_prompt":
            return [" ".join(context.split())[:500]]
        sources = re.split(r"--- SOURCE LOCALE: [^\n]+ ---", context)
        queries = []
        for source in sources:
            lines = [line for line in source.splitlines() if line.strip() and not re.search(
                r"(?i)\*\*(?:nom|name|email|phone|téléphone|adresse|address|code postal|postal code)\*\*", line,
            )]
            criteria = " ".join(re.sub(r"[#*_]", "", " ".join(lines)).split())[:480]
            if criteria:
                queries.append(f"jobs {criteria}")
        return list(dict.fromkeys(queries))


    def persist_discovery_metrics(db: sqlite3.Connection, run_id: int, metrics: dict[str, Any]) -> None:
        columns = {row[1] for row in db.execute("PRAGMA table_info(discovery_runs)")}
        values = {
            key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
            for key, value in metrics.items() if key in columns and value is not None
        }
        if values:
            db.execute(
                f"UPDATE discovery_runs SET {', '.join(f'{key}=?' for key in values)} WHERE id=?",
                (*values.values(), run_id),
            )
            db.commit()

    def collect_search_results(
        db: sqlite3.Connection, queries: list[str], run_id: int,
    ) -> tuple[list[SearchResult], str, str | None, dict[str, Any]]:
        config = search_config(db)
        provider_id = "searxng" if config["mode"] == "SEARXNG" else str(config["provider"])
        provider = configured_search_provider(db, config)
        values = []
        per_query_stats = [{
            "query": query, "status": "PENDING", "metrics_version": 1,
            "raw_result_count": 0, "admitted_result_count": 0, "extracted_url_count": 0,
            "intra_query_duplicate_count": 0, "invalid_url_count": 0, "search_call_count": 0,
        } for query in queries]
        provider_errors = []

        def snapshot() -> dict[str, Any]:
            def total(key: str) -> int | None:
                counts = [item[key] for item in per_query_stats if type(item.get(key)) is int]
                return sum(counts) if counts else None

            unique_count = len({item.url for item in values})
            stats = {
                "per_query_stats": per_query_stats,
                "raw_result_count": total("raw_result_count") or 0,
                "admitted_result_count": len(values),
                "intra_query_duplicate_count": total("intra_query_duplicate_count") or 0,
                "merged_count": unique_count,
                "global_duplicate_count": len(values) - unique_count,
                "search_call_count": total("search_call_count") or 0,
                "search_input_tokens": total("search_input_tokens"),
                "search_output_tokens": total("search_output_tokens"),
                "search_total_tokens": total("search_total_tokens"),
                "search_provider_errors": provider_errors,
                "partial": any(item["status"] != "SUCCESS" for item in per_query_stats),
            }
            if config["mode"] == "AI_DIRECT":
                stats.update(observed_web_metadata(per_query_stats))
            persist_discovery_metrics(db, run_id, stats)
            return stats

        try:
            snapshot()
            for index, query in enumerate(queries):
                per_query_stats[index].update({"status": "RUNNING", "search_call_count": 1})
                snapshot()
                try:
                    query_results = provider.search(query, limit=12)
                    provider_stats = getattr(provider, "last_stats", {})
                    per_query_stats[index].update({
                        "query": query,
                        "status": "PARTIAL" if provider_stats.get("partial") else "SUCCESS",
                        "raw_result_count": provider_stats.get("raw_result_count", len(query_results)),
                        "admitted_result_count": len(query_results),
                        "intra_query_duplicate_count": provider_stats.get("intra_query_duplicate_count", 0),
                        "invalid_url_count": provider_stats.get("invalid_url_count", 0),
                        "extracted_url_count": provider_stats.get("extracted_url_count"),
                        "search_call_count": provider_stats.get("search_call_count", 1),
                        "search_input_tokens": provider_stats.get("search_input_tokens"),
                        "search_output_tokens": provider_stats.get("search_output_tokens"),
                        "search_total_tokens": provider_stats.get("search_total_tokens"),
                        **{key: provider_stats.get(key) for key in (
                            "web_search_calls", "web_domains", "web_search_actions", "web_search_queries", "web_sources",
                        )},
                    })
                    web_metadata = observed_web_metadata([per_query_stats[index]])
                    per_query_stats[index].update(web_metadata)
                    sources = web_metadata["web_sources"]
                    per_query_stats[index]["source_url_count"] = len(sources) if sources is not None else None
                    provider_errors.extend(provider_stats.get("provider_errors", []))
                    values.extend(query_results)
                except SearchProviderError as error:
                    LOGGER.warning("Search failed provider=%s query=%s error=%s", provider_id, query, error)
                    provider_errors.append({"query": query, "error": str(error)})
                    # Failed responses are unavailable, not successful empty results.
                    # AIWebSearchProvider resets last_stats and retains observed error metadata.
                    provider_stats = getattr(provider, "last_stats", {}) if config["mode"] == "AI_DIRECT" else {}
                    per_query_stats[index].update({
                        "query": query, "status": "SEARCH_PROVIDER_UNAVAILABLE", "error": str(error),
                        "raw_result_count": None, "admitted_result_count": 0, "extracted_url_count": None,
                        "intra_query_duplicate_count": None, "invalid_url_count": None,
                        "search_call_count": 1,
                        **{key: provider_stats.get(key) for key in (
                            "search_input_tokens", "search_output_tokens", "search_total_tokens",
                            "web_search_calls", "web_domains", "web_search_actions", "web_search_queries", "web_sources",
                        )},
                    })
                    snapshot()
                    if isinstance(error, SearchProviderUnavailableError):
                        break
                snapshot()
            attempted = [item for item in per_query_stats if item["status"] != "PENDING"]
            if attempted and all(item["status"] == "SEARCH_PROVIDER_UNAVAILABLE" for item in attempted):
                raise SearchProviderUnavailableError(str(provider_errors[-1].get("error", "Provider unavailable")))
            unique = {item.url: item for item in values}
            stats = snapshot()
            LOGGER.info("Search provider=%s queries=%s results=%s", provider_id, len(queries), len(unique))
            return list(unique.values()), provider_id, config.get("model") if config["mode"] == "AI_DIRECT" else None, stats
        finally:
            if hasattr(provider, "close"):
                provider.close()


    def search_documents(
        db: sqlite3.Connection, results: list[SearchResult], run_id: int,
    ) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, int]]:
        known = {
            row["url"]: "excluded" if row["deleted_at"] else "ignored" if row["status"] == "IGNORED" else "active"
            for row in db.execute("SELECT url, status, deleted_at FROM applications")
        }
        known_search_urls = {item.url: known[item.url] for item in results if item.url in known}
        candidates, stats = select_search_candidates(results, set(known))
        stats.update({
            "fetch_attempted": 0,
            "fetch_succeeded": 0,
            "fetch_failed": 0,
            "snippet_fallback_count": 0,
            "ai_input_count": 0,
        })
        persist_discovery_metrics(db, run_id, stats)
        documents = []
        with api.state.offer_http_client_factory() as client:
            for rank, item in enumerate(candidates, 1):
                stats["fetch_attempted"] += 1
                persist_discovery_metrics(db, run_id, stats)
                text = item.snippet
                content_mode = "snippet"
                fetch_status = "FAILED"
                http_status = None
                offer_metadata = {"published_at": None, "deadline": None, "availability": None}
                response = None
                try:
                    response = safe_fetch(client, item.url, offer_resolver)
                    http_status = response.status_code
                    response.raise_for_status()
                    body = b""
                    truncated = False
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) >= 500_000:
                            body = body[:500_000]
                            truncated = True
                            break
                    page = body.decode(response.encoding or "utf-8", errors="ignore")
                    offer_metadata = extract_offer_metadata(page)
                    if truncated:
                        offer_metadata.update({"published_at": None, "deadline": None})
                    parser = _TextExtractor()
                    parser.feed(page)
                    extracted = " ".join(parser.parts)[:20_000]
                    stats["fetch_succeeded"] += 1
                    fetch_status = "SUCCESS"
                    if extracted:
                        text = extracted
                        content_mode = "full_content"
                    else:
                        stats["snippet_fallback_count"] += 1
                except (httpx.HTTPError, UnsafeDestination, DnsResolutionError, RedirectLimitError, ValueError):
                    stats["fetch_failed"] += 1
                    stats["snippet_fallback_count"] += 1
                finally:
                    if response is not None:
                        response.close()
                documents.append({
                    "candidate_id": f"candidate_{rank:03d}", "rank": rank,
                    "title": item.title, "url": item.url, "snippet": item.snippet,
                    "source": item.source, "query": item.query,
                    "retrieval_mode": item.retrieval_mode, "search_provider": item.search_provider,
                    "pre_classification": classify_search_result(item),
                    "content": text, "content_mode": content_mode,
                    "fetch_status": fetch_status, "http_status": http_status,
                    **offer_metadata,
                })
                persist_discovery_metrics(db, run_id, stats)
        return documents, known_search_urls, stats

    def active_capabilities(db: sqlite3.Connection, provider_id: str) -> dict[str, bool]:
        stored = settings(db, "ai_capabilities").get("ai_capabilities")
        if stored:
            return {**EMPTY_CAPABILITIES, **json.loads(stored)}
        if provider_id == OPENAI_PROVIDER:
            return dict(OPENAI_CAPABILITIES)
        return dict(EMPTY_CAPABILITIES)

    with closing(database()) as db:
        initial = settings(
            db, "search_profile_mode", "search_mode", "search_provider", "searxng_url",
            "ai_provider", "ai_auth_mode", "ai_model", "ai_reasoning_effort",
            "codex_model", "codex_reasoning_effort", "knowledge_base_selected_files",
        )
        defaults = {}
        if not initial.get("search_profile_mode"):
            files = knowledge_base_files(api.state.repo_root)
            defaults.update({
                "knowledge_base_selected_files": json.dumps([
                    path.relative_to(api.state.repo_root).as_posix()
                    for path in knowledge_base_sources(api.state.repo_root) if path in files
                ]),
                "search_profile_mode": (
                    "knowledge_base" if files else "custom_prompt"
                ),
            })
        if initial.get("search_profile_mode") == "knowledge_base" and "knowledge_base_selected_files" not in initial:
            files = knowledge_base_files(search_kb_root(db))
            defaults["knowledge_base_selected_files"] = json.dumps([
                path.relative_to(search_kb_root(db)).as_posix()
                for path in knowledge_base_sources(search_kb_root(db)) if path in files
            ])
        defaults.setdefault(
            "search_mode",
            initial.get("search_mode", "AI_DIRECT" if initial.get("search_provider") == "openai_web_search" else "SEARXNG"),
        )
        defaults.setdefault("searxng_url", initial.get("searxng_url", DEFAULT_SEARXNG_URL))
        if not initial.get("ai_auth_mode") and initial.get("codex_model"):
            defaults.update({
                "ai_provider": OPENAI_PROVIDER, "ai_auth_mode": "chatgpt_oauth",
                "ai_model": initial["codex_model"],
                "ai_reasoning_effort": initial.get("codex_reasoning_effort", "medium"),
                "ai_capabilities": json.dumps(OPENAI_CAPABILITIES),
            })
        if defaults:
            write_settings(db, defaults)
            db.commit()


    @api.get("/auth/chatgpt/status")
    def chatgpt_status() -> dict[str, Any]:
        return api.state.auth.status()

    @api.get("/auth/chatgpt/start")
    def chatgpt_start(request: Request) -> dict[str, str]:
        redirect_uri = str(request.url_for("chatgpt_callback"))
        return {"authorization_url": api.state.auth.start(redirect_uri)}

    @api.get("/auth/chatgpt/callback", response_class=HTMLResponse)
    def chatgpt_callback(code: str, state: str, client_id: str | None = None) -> str:
        try:
            api.state.auth.callback(code, state, client_id)
        except (ValueError, PermissionError, httpx.HTTPError) as error:
            raise HTTPException(400, str(error)) from error
        return "<p>ChatGPT connected. You can close this window.</p><script>window.close()</script>"

    @api.post("/auth/chatgpt/logout")
    def chatgpt_logout() -> dict[str, bool]:
        with api.state.codex_client_lock:
            client = api.state.codex_client
            if client is not None and hasattr(client, "close"):
                client.close()
        api.state.codex_client = None
        api.state.auth.logout()
        with closing(database()) as db:
            rows = settings(db, "ai_auth_mode")
            if rows.get("ai_auth_mode") == "chatgpt_oauth":
                db.execute("DELETE FROM settings WHERE key IN ('ai_provider', 'ai_auth_mode')")
                db.commit()
        return {"connected": False}

    @api.get("/settings/search-profile")
    def get_search_profile(mode: Literal["knowledge_base", "custom_prompt"] | None = None) -> dict[str, Any]:
        with closing(database()) as db:
            return search_profile(db, mode)

    @api.put("/settings/search-profile")
    def save_search_profile(payload: SearchProfileSettings) -> dict[str, Any]:
        with closing(database()) as db:
            root = search_kb_root(db)
            values = {}
            if payload.mode == "knowledge_base" and payload.knowledge_base_root is not None:
                try:
                    if not payload.knowledge_base_root.strip():
                        raise ValueError("Enter a Knowledge Base folder.")
                    root = Path(payload.knowledge_base_root.strip()).expanduser()
                    if not root.is_absolute():
                        raise ValueError("Knowledge Base folder must be an absolute path.")
                    if not root.exists():
                        raise ValueError("Knowledge Base folder does not exist.")
                    root = root.resolve()
                    if not root.is_dir():
                        raise ValueError("Knowledge Base path must be a folder.")
                    with os.scandir(root):
                        pass
                    files = knowledge_base_files(root)
                    if not files:
                        raise ValueError("Folder contains no usable Markdown files.")
                    for path in files:
                        with path.open("rb") as source_file:
                            source_file.read(1)
                except (ValueError, OSError) as error:
                    raise HTTPException(422, f"Invalid Knowledge Base : {error}") from error
                previous_sources = search_profile(db)["knowledge_base_selected_files"]
                available = {path.relative_to(root).as_posix() for path in files}
                values["knowledge_base_root"] = str(root)
                values["knowledge_base_selected_files"] = json.dumps(
                    [source for source in previous_sources if source in available], ensure_ascii=False,
                )
            if payload.mode == "knowledge_base" and not knowledge_base_files(root):
                raise HTTPException(409, "No usable Knowledge Base found.")
            previous = settings(db, "custom_search_prompt").get("custom_search_prompt", "")
            prompt = previous if payload.custom_search_prompt is None else payload.custom_search_prompt
            if payload.mode == "custom_prompt" and not prompt:
                raise HTTPException(422, "Define your search profile first.")
            values.update({
                "search_profile_mode": payload.mode,
                "custom_search_prompt": prompt,
            })
            if payload.mode == "knowledge_base" and payload.knowledge_base_selected_files is not None:
                sources = []
                available_files = knowledge_base_files(root)
                for source in payload.knowledge_base_selected_files:
                    if Path(source).is_absolute() or Path(source).drive:
                        raise HTTPException(400, "A relative Knowledge Base path is required.")
                    path = resolve_repo_path(source, root)
                    if path not in available_files:
                        raise HTTPException(422, f"Markdown source unavailable : {source}")
                    sources.append(path.relative_to(root).as_posix())
                values["knowledge_base_selected_files"] = json.dumps(list(dict.fromkeys(sources)), ensure_ascii=False)
            if prompt != previous:
                values["custom_search_prompt_updated_at"] = now()
            write_settings(db, values)
            db.commit()
            if "knowledge_base_root" in values:
                api.state.repo_root = root
            return search_profile(db)

    @api.delete("/settings/search-profile/prompt")
    def clear_search_profile_prompt() -> dict[str, Any]:
        with closing(database()) as db:
            db.execute(
                "DELETE FROM settings WHERE key IN ('custom_search_prompt', 'custom_search_prompt_updated_at')",
            )
            db.commit()
            return search_profile(db)

    @api.get("/search/config")
    def get_search_config() -> dict[str, Any]:
        with closing(database()) as db:
            return search_config(db)

    @api.put("/search/config")
    def save_search_config(payload: SearchSettings) -> dict[str, Any]:
        with closing(database()) as db:
            capabilities = dict(EMPTY_CAPABILITIES)
            if payload.mode == "AI_DIRECT":
                selected = search_ai_settings(payload.model_dump())
                capabilities = selected["capabilities"]
                billing_key = f"{selected['provider_id']}_api_billing_confirmed"
                confirmed = settings(db, billing_key).get(billing_key) == "true"
                if selected["auth_mode"] == "api_key" and not confirmed and not payload.confirm_api_billing:
                    raise HTTPException(409, "Confirm separate API billing")
            values = {
                "search_mode": payload.mode,
                "searxng_url": payload.searxng_url,
                "search_ai_provider": payload.provider or "",
                "search_ai_model": payload.model or "",
                "search_ai_effort": selected["effort"] if payload.mode == "AI_DIRECT" else payload.effort,
                "search_ai_capabilities": json.dumps(capabilities),
            }
            if payload.mode == "AI_DIRECT" and payload.confirm_api_billing:
                values[f"{selected['provider_id']}_api_billing_confirmed"] = "true"
            write_settings(db, values)
            db.commit()
            return search_config(db)

    @api.patch("/search/config")
    def save_offer_age(payload: OfferAgeSettings) -> dict[str, Any]:
        with closing(database()) as db:
            write_settings(db, {"max_offer_age_days": str(payload.max_offer_age_days)})
            db.commit()
            return search_config(db)

    @api.post("/search/test")
    def test_search(payload: SearchSettings) -> dict[str, Any]:
        try:
            with closing(database()) as db:
                provider = configured_search_provider(db, payload.model_dump())
                try:
                    results = provider.search("job opportunities", limit=1)
                    stats = getattr(provider, "last_stats", {})
                finally:
                    if hasattr(provider, "close"):
                        provider.close()
        except HTTPException:
            raise
        except SearchProviderError as error:
            raise HTTPException(503, str(error)) from error
        return {
            "connected": True, "status": "PARTIAL" if stats.get("partial") else "SUCCESS",
            "result_count": len(results), "provider_errors": stats.get("provider_errors", []),
        }

    @api.get("/ai/pipeline")
    def get_pipeline_settings() -> dict[str, Any]:
        with closing(database()) as db:
            rows = dict(db.execute("SELECT key, value FROM settings"))
        return {
            "overrides": {
                stage: {
                    "provider": rows.get(f"pipeline_{stage}_provider", "default"),
                    "model": rows.get(f"pipeline_{stage}_model"),
                    "effort": rows.get(f"pipeline_{stage}_effort", "medium"),
                    "capabilities": json.loads(rows.get(f"pipeline_{stage}_capabilities", "{}")),
                }
                for stage in PIPELINE_STAGES
            },
            "ai_fallback": rows.get("ai_fallback") or None,
        }

    @api.put("/ai/pipeline")
    def save_pipeline_settings(payload: PipelineSettings) -> dict[str, Any]:
        values = {"ai_fallback": payload.ai_fallback or ""}
        api_choices = {
            choice for choice in [payload.ai_fallback, *(override.provider for override in payload.overrides.values())]
            if choice in API_PROVIDERS
        }
        with closing(database()) as db:
            billing = settings(db, *(f"{choice}_api_billing_confirmed" for choice in api_choices)) if api_choices else {}
        unconfirmed = [choice for choice in api_choices if billing.get(f"{choice}_api_billing_confirmed") != "true"]
        if unconfirmed and not payload.confirm_api_billing:
            raise HTTPException(409, "Confirm separate API billing")
        if payload.confirm_api_billing:
            values.update({f"{choice}_api_billing_confirmed": "true" for choice in api_choices})
        if payload.ai_fallback:
            provider_settings(payload.ai_fallback)
        for stage in PIPELINE_STAGES:
            override = payload.overrides.get(stage, PipelineProviderSettings())
            if override.provider == "default":
                values.update({
                    f"pipeline_{stage}_provider": "default",
                    f"pipeline_{stage}_model": "",
                    f"pipeline_{stage}_effort": "medium",
                    f"pipeline_{stage}_capabilities": "{}",
                })
                continue
            validated = provider_settings(override.provider, override.model, override.effort)
            values.update({
                f"pipeline_{stage}_provider": override.provider,
                f"pipeline_{stage}_model": validated["model"],
                f"pipeline_{stage}_effort": validated["effort"],
                f"pipeline_{stage}_capabilities": json.dumps(validated["capabilities"]),
            })
        with closing(database()) as db:
            write_settings(db, values)
            db.commit()
        return get_pipeline_settings()

    @api.get("/ai/config")
    def ai_config() -> dict[str, Any]:
        auth_status = api.state.auth.status()
        try:
            api_keys = {provider: api.state.api_key_store.get(provider) for provider in API_PROVIDERS}
        except Exception as error:
            raise HTTPException(502, "Secure credential storage unavailable") from error
        with closing(database()) as db:
            rows = dict(db.execute("SELECT key, value FROM settings"))
        auth_mode = rows.get("ai_auth_mode")
        provider_id = rows.get("ai_provider", OPENAI_PROVIDER)
        capabilities = (
            {**EMPTY_CAPABILITIES, **json.loads(rows["ai_capabilities"])}
            if auth_mode and rows.get("ai_capabilities")
            else dict(OPENAI_CAPABILITIES) if auth_mode and provider_id == OPENAI_PROVIDER
            else None
        )
        return {
            "chatgpt": {**auth_status, "mode": "Abonnement ChatGPT / Codex"},
            "api_keys": {provider: {
                "configured": bool(key), "last_four": key[-4:] if key else None,
                "billing_confirmed": rows.get(f"{provider}_api_billing_confirmed") == "true",
            } for provider, key in api_keys.items()},
            "active": None if not auth_mode else {
                "provider_id": provider_id, "auth_mode": auth_mode,
                "model": rows.get("ai_model"), "effort": rows.get("ai_reasoning_effort", "medium"),
                "label": "ChatGPT — abonnement" if auth_mode == "chatgpt_oauth" else f"{PROVIDER_LABELS[provider_id]} — API",
            },
            "capabilities": {**EMPTY_CAPABILITIES, **(capabilities or {})},
        }

    @api.post("/ai/api-keys/{provider_id}/test")
    def test_api_key(provider_id: str, payload: ApiKeyPayload) -> dict[str, bool]:
        if provider_id not in API_PROVIDERS:
            raise HTTPException(404, "Unknown API provider")
        provider = None
        try:
            provider = api.state.provider_factories[provider_id](payload.api_key)
            provider.test()
        except Exception as error:
            raise provider_error(error) from error
        finally:
            if hasattr(provider, "close"):
                provider.close()
        return {"valid": True}

    @api.put("/ai/api-keys/{provider_id}")
    def save_api_key(provider_id: str, payload: ApiKeyPayload) -> dict[str, bool]:
        if provider_id not in API_PROVIDERS:
            raise HTTPException(404, "Unknown API provider")
        try:
            api.state.api_key_store.set(provider_id, payload.api_key)
        except Exception as error:
            raise HTTPException(502, "Secure credential storage unavailable") from error
        return {"configured": True}

    @api.delete("/ai/api-keys/{provider_id}")
    def delete_api_key(provider_id: str) -> dict[str, bool]:
        if provider_id not in API_PROVIDERS:
            raise HTTPException(404, "Unknown API provider")
        try:
            api.state.api_key_store.delete(provider_id)
        except Exception as error:
            raise HTTPException(502, "Secure credential storage unavailable") from error
        with closing(database()) as db:
            rows = settings(db, "ai_provider", "ai_auth_mode")
            if rows.get("ai_auth_mode") == "api_key" and rows.get("ai_provider") == provider_id:
                db.execute("DELETE FROM settings WHERE key IN ('ai_provider', 'ai_auth_mode')")
            db.commit()
        return {"configured": False}

    @api.get("/ai/models")
    def ai_models(provider_id: str | None = None, auth_mode: Literal["chatgpt_oauth", "api_key"] | None = None) -> dict[str, Any]:
        with closing(database()) as db:
            rows = settings(db, "ai_provider", "ai_auth_mode", "ai_model", "ai_reasoning_effort")
        mode = auth_mode or rows.get("ai_auth_mode")
        if not mode:
            raise HTTPException(409, "No active AI provider")
        provider = OPENAI_PROVIDER if mode == "chatgpt_oauth" else provider_id or rows.get("ai_provider")
        if provider not in API_PROVIDERS:
            raise HTTPException(422, "Unknown API provider")
        active = mode == rows.get("ai_auth_mode") and provider == rows.get("ai_provider", OPENAI_PROVIDER)
        return {
            "models": models_for(provider, mode), "selected": rows.get("ai_model") if active else None,
            "selectedEffort": rows.get("ai_reasoning_effort", "medium") if active else "medium",
        }

    @api.put("/ai/active")
    def choose_active_provider(payload: ActiveProviderSettings) -> dict[str, Any]:
        if payload.auth_mode == "chatgpt_oauth" and not api.state.auth.status().get("connected"):
            raise HTTPException(401, "ChatGPT is not connected")
        if payload.auth_mode == "chatgpt_oauth" and payload.provider_id != OPENAI_PROVIDER:
            raise HTTPException(422, "ChatGPT uses the OpenAI account provider")
        if payload.auth_mode == "api_key" and not api.state.api_key_store.get(payload.provider_id):
            raise HTTPException(401, "API key missing")
        billing_key = f"{payload.provider_id}_api_billing_confirmed"
        with closing(database()) as db:
            rows = settings(db, billing_key)
            if payload.auth_mode == "api_key" and rows.get(billing_key) != "true" and not payload.confirm_api_billing:
                raise HTTPException(409, "Confirm separate API billing")
        models = models_for(payload.provider_id, payload.auth_mode)
        model = next((item for item in models if (item.get("model") or item.get("id")) == payload.model), None)
        if not model:
            raise HTTPException(422, f"Model unavailable : {payload.model}")
        capabilities = model_capabilities(model, OPENAI_CAPABILITIES if payload.provider_id == OPENAI_PROVIDER else None)
        if capabilities["reasoning_effort"] and payload.effort not in supported_reasoning_efforts(model):
            raise HTTPException(422, f"Effort '{payload.effort}' is not supported by model '{payload.model}'")
        if not capabilities["reasoning_effort"] and payload.effort != "medium":
            raise HTTPException(422, f"Configurable reasoning is not supported by model '{payload.model}'")
        with closing(database()) as db:
            values = {
                "ai_provider": payload.provider_id, "ai_auth_mode": payload.auth_mode,
                "ai_model": payload.model, "ai_reasoning_effort": payload.effort,
                "ai_capabilities": json.dumps(capabilities),
            }
            if payload.auth_mode == "api_key" and payload.confirm_api_billing:
                values[billing_key] = "true"
            write_settings(db, values)
            db.commit()
        return {**payload.model_dump(exclude={"confirm_api_billing"}), "capabilities": capabilities}

    @api.delete("/ai/active")
    def clear_active_provider() -> dict[str, None]:
        with closing(database()) as db:
            db.execute("DELETE FROM settings WHERE key IN ('ai_provider', 'ai_auth_mode')")
            db.commit()
        return {"active": None}

    @api.get("/codex/models")
    def codex_models() -> dict[str, Any]:
        models = models_for(OPENAI_PROVIDER, "chatgpt_oauth")
        with closing(database()) as db:
            rows = settings(db, "ai_model", "ai_reasoning_effort", "codex_model", "codex_reasoning_effort")
        return {"models": models, "selected": rows.get("ai_model", rows.get("codex_model")),
                "selectedEffort": rows.get("ai_reasoning_effort", rows.get("codex_reasoning_effort", "medium"))}

    @api.put("/codex/model")
    def choose_model(payload: CodexSettings) -> dict[str, str]:
        models = models_for(OPENAI_PROVIDER, "chatgpt_oauth")
        model = next((item for item in models if (item.get("model") or item.get("id")) == payload.model), None)
        if not model:
            raise HTTPException(422, f"Unknown Codex model : {payload.model}")
        if payload.effort not in supported_reasoning_efforts(model):
            raise HTTPException(422, f"Effort '{payload.effort}' is not supported by model '{payload.model}'")
        with closing(database()) as db:
            write_settings(db, {
                "codex_model": payload.model, "codex_reasoning_effort": payload.effort,
                "ai_provider": "openai", "ai_auth_mode": "chatgpt_oauth",
                "ai_model": payload.model, "ai_reasoning_effort": payload.effort,
                "ai_capabilities": json.dumps(model_capabilities(model, OPENAI_CAPABILITIES)),
            })
            db.commit()
        return payload.model_dump()

    @api.get("/codex/discovery/latest")
    def latest_discovery() -> dict[str, Any] | None:
        with closing(database()) as db:
            row = db.execute("SELECT * FROM discovery_runs ORDER BY id DESC LIMIT 1").fetchone()
        return discovery_run_view(row) if row else None

    @api.get("/codex/discovery/{run_id}/candidates")
    def discovery_candidates(run_id: int) -> list[dict[str, Any]]:
        with closing(database()) as db:
            return [row_dict(row) for row in db.execute(
                "SELECT * FROM discovery_candidates WHERE discovery_run_id=? ORDER BY rank", (run_id,),
            ).fetchall()]

    @api.get("/applications")
    def list_applications(status: str | None = None, company: str = "", source: str = "", search: str = "") -> list[dict[str, Any]]:
        clauses, values = ["deleted_at IS NULL"], []
        for expression, value in [("status = ?", status), ("company LIKE ?", f"%{company}%" if company else ""), ("source LIKE ?", f"%{source}%" if source else "")]:
            if value:
                clauses.append(expression); values.append(value)
        if search:
            clauses.append("(company LIKE ? OR position LIKE ? OR notes LIKE ?)")
            values.extend([f"%{search}%"] * 3)
        sql = "SELECT * FROM applications" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY updated_at DESC"
        with closing(database()) as db:
            return [application_view(db, row) for row in db.execute(sql, values).fetchall()]

    @api.get("/applications/deleted")
    def deleted_applications() -> list[dict[str, Any]]:
        with closing(database()) as db:
            rows = db.execute("SELECT * FROM applications WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC").fetchall()
            return [application_view(db, row) for row in rows]

    @api.get("/applications/{application_id}")
    def application_detail(application_id: int) -> dict[str, Any]:
        with closing(database()) as db:
            result = application_view(db, get_application(db, application_id))
            result["events"] = [row_dict(row) for row in db.execute("SELECT * FROM events WHERE application_id = ? ORDER BY created_at DESC, id DESC", (application_id,))]
            return result

    @api.post("/applications", status_code=201)
    def create_application(payload: ApplicationCreate) -> dict[str, Any]:
        with closing(database()) as db:
            try:
                application_id = insert_application(
                    db,
                    payload.model_dump(),
                    event_details=f"Source : {payload.source or 'manuelle'}",
                )
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            except sqlite3.IntegrityError as error:
                raise HTTPException(409, "Job already imported (normalized URL)") from error
            db.commit()
            return row_dict(get_application(db, application_id))

    @api.patch("/applications/{application_id}")
    def update_application(application_id: int, payload: ApplicationPatch) -> dict[str, Any]:
        values = payload.model_dump(exclude_unset=True)
        if not values:
            raise HTTPException(422, "No changes")
        with closing(database()) as db:
            application = get_application(db, application_id)
            if "company_postal_address" in values:
                address = values["company_postal_address"]
                if isinstance(address, str) and not address.strip():
                    address = None
                    values["company_postal_address"] = None
                if address is not None and not valid_postal_address(address):
                    raise HTTPException(422, "Incomplete postal address")
                values["company_address_overridden"] = (
                    application["company_address_overridden"]
                    if address == application["company_postal_address"]
                    else int(address is not None)
                )
            expected_action, expected_date = tracking_for_status(application["status"], application["sent_at"])
            if "next_action" in values:
                values["next_action"] = expected_action
            if application["status"] == "SENT":
                values["next_action_at"] = expected_date
            elif application["status"] in {"REJECTED", "WITHDRAWN", "NO_RESPONSE", "IGNORED"}:
                values["next_action_at"] = None
            assignments = ", ".join(f"{field} = ?" for field in values)
            db.execute(f"UPDATE applications SET {assignments}, updated_at = ? WHERE id = ?", [*values.values(), now(), application_id])
            if "company_postal_address" in values:
                updated = get_application(db, application_id)
                rewrite_company_files(updated)
            if {"interview_at", "interview_notes"}.intersection(values):
                updated = get_application(db, application_id)
                if updated["interview_prep_path"]:
                    value = Path(updated["interview_prep_path"])
                    path = value.resolve() if value.is_absolute() else (api.state.repo_root / value).resolve()
                    allowed = (api.state.repo_root.resolve(), api.state.applications_root.resolve())
                    if not any(path.is_relative_to(root) for root in allowed):
                        raise HTTPException(400, "Invalid interview preparation path")
                    if path.is_file():
                        update_interview_event(path, updated["interview_at"], updated["interview_notes"])
            add_event(db, application_id, "UPDATED", ", ".join(values))
            db.commit()
            return application_view(db, get_application(db, application_id))

    @api.post("/applications/{application_id}/status")
    def set_status(application_id: int, payload: StatusChange) -> dict[str, Any]:
        if payload.status in {"PREPARED", "AWAITING_VALIDATION", "SENT"}:
            raise HTTPException(409, "Use the corresponding workflow action")
        with closing(database()) as db:
            return change_status(
                db, application_id, payload.status, payload.details,
                api.state.repo_root, api.state.applications_root,
            )

    @api.post("/applications/{application_id}/validate")
    def validate_application(application_id: int) -> dict[str, Any]:
        with closing(database()) as db:
            application = get_application(db, application_id)
            artifacts = validate_application_artifacts(
                application, api.state.repo_root, api.state.applications_root
            )
            if not artifacts["complete"]:
                raise HTTPException(409, {"message": "Incomplete preparation", **artifacts})
            return change_status(db, application_id, "AWAITING_VALIDATION", "Prepared application approved by user")

    @api.post("/applications/{application_id}/ignore")
    def ignore(application_id: int, details: str = Body("Ignored by user", embed=True)) -> dict[str, Any]:
        with closing(database()) as db:
            return change_status(
                db, application_id, "IGNORED", details,
                api.state.repo_root, api.state.applications_root,
            )

    @api.delete("/applications/{application_id}")
    def delete_application(application_id: int) -> dict[str, Any]:
        with closing(database()) as db:
            application = get_application(db, application_id, include_deleted=True)
            if application["deleted_at"]:
                raise HTTPException(409, "This job is already in Trash")
            if application["status"] != "DETECTED":
                raise HTTPException(409, "Only a DETECTED job can be moved to Trash")
            timestamp = now()
            db.execute(
                "UPDATE applications SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (timestamp, timestamp, application_id),
            )
            add_event(db, application_id, "DELETED", "Job moved to Trash")
            db.commit()
            return application_view(db, get_application(db, application_id, include_deleted=True))

    @api.post("/applications/{application_id}/restore")
    def restore_application(application_id: int) -> dict[str, Any]:
        with closing(database()) as db:
            application = get_application(db, application_id, include_deleted=True)
            if application["status"] != "IGNORED" and not application["deleted_at"]:
                raise HTTPException(409, "Only an ignored job can be restored")
            timestamp = now()
            db.execute(
                "UPDATE applications SET deleted_at = NULL, status = 'DETECTED', next_action = ?, next_action_at = NULL, updated_at = ? WHERE id = ?",
                (tracking_for_status("DETECTED")[0], timestamp, application_id),
            )
            add_event(db, application_id, "RESTORED", "Job restored to SEARCH")
            db.commit()
            return application_view(db, get_application(db, application_id))

    @api.delete("/applications/{application_id}/permanent")
    def permanently_delete_application(application_id: int) -> dict[str, Any]:
        with closing(database()) as db:
            application = get_application(db, application_id, include_deleted=True)
            if not application["deleted_at"]:
                raise HTTPException(409, "Permanent deletion is restricted to jobs in Trash")
            preserved_files = [application[field] for field in FILE_FIELDS.values() if application[field]]
            db.execute("DELETE FROM applications WHERE id = ?", (application_id,))
            db.commit()
            return {"id": application_id, "deleted": True, "preserved_files": preserved_files}

    @api.post("/applications/{application_id}/prepare")
    def prepare(application_id: int, payload: Preparation) -> dict[str, Any]:
        raise HTTPException(410, "Template preparation was removed. Use AI preparation.")

    @api.post("/applications/{application_id}/submission/confirm")
    def confirm_submission(application_id: int) -> dict[str, Any]:
        with closing(database()) as db:
            return change_status(db, application_id, "SENT", "Submission confirmed by user")

    @api.get("/applications/{application_id}/events")
    def events(application_id: int) -> list[dict[str, Any]]:
        with closing(database()) as db:
            get_application(db, application_id)
            return [row_dict(row) for row in db.execute("SELECT * FROM events WHERE application_id = ? ORDER BY created_at DESC, id DESC", (application_id,))]

    @api.get("/applications/{application_id}/files/{kind}")
    def application_file(application_id: int, kind: str):
        field = FILE_FIELDS.get(kind)
        if not field:
            raise HTTPException(404, "Unknown file type")
        with closing(database()) as db:
            value = get_application(db, application_id)[field]
        if not value:
            raise HTTPException(404, f"Missing document : {FILE_NAMES[kind]}")
        path = resolve_repo_path(value, api.state.repo_root, api.state.applications_root)
        if not path.is_file():
            raise HTTPException(404, f"Missing document : {path.name}")
        if path.suffix.lower() == ".md":
            return PlainTextResponse(path.read_text(encoding="utf-8"))
        return FileResponse(path, filename=path.name)

    @api.get("/applications/{application_id}/cover-letter.docx")
    def application_cover_letter(application_id: int):
        with closing(database()) as db:
            application = get_application(db, application_id)
        value = application["cover_letter_path"]
        if not value:
            raise HTTPException(404, "No cover letter attached")
        path = resolve_repo_path(value, api.state.repo_root, api.state.applications_root)
        try:
            validate_cover_letter_docx(path, application["company"], position=application["position"])
        except CoverLetterError as error:
            raise HTTPException(404, f"Cover letter missing or invalid : {error}") from error
        return FileResponse(
            path,
            filename="lettre-motivation.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    def docx_pdf_response(path: Path, filename: str, warning_label: str) -> Response:
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / filename
                pdf_converter(path, output)
                if not output.is_file() or output.stat().st_size == 0:
                    raise RuntimeError("Generated PDF is empty.")
                content = output.read_bytes()
        except PdfExportUnavailableError as error:
            raise HTTPException(503, str(error)) from error
        except PdfExportError as error:
            raise HTTPException(500, str(error)) from error
        except Exception as error:
            raise HTTPException(500, f"PDF conversion failed : {error}") from error
        if not content.startswith(b"%PDF-"):
            raise HTTPException(500, "PDF conversion failed : invalid PDF signature.")
        page_count = len(re.findall(rb"/Type\s*/Page\b", content))
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            **({"X-PDF-Page-Count": str(page_count)} if page_count else {}),
            **({"X-PDF-Warning": f"{warning_label} contient {page_count} pages."} if page_count > 1 else {}),
        }
        return Response(content, media_type="application/pdf", headers=headers)

    @api.get("/applications/{application_id}/cv")
    def application_cv(application_id: int):
        with closing(database()) as db:
            value = get_application(db, application_id)["cv_path"]
        if not value:
            raise HTTPException(404, "No resume attached")
        path = resolve_repo_path(value, api.state.repo_root, api.state.applications_root)
        if not path.is_file() or path.suffix.lower() != ".docx":
            raise HTTPException(404, "Resume not found")
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            content_disposition_type="inline",
        )

    @api.get("/applications/{application_id}/cv.docx")
    def application_cv_docx(application_id: int):
        with closing(database()) as db:
            value = get_application(db, application_id)["cv_path"]
        if not value:
            raise HTTPException(404, "No resume attached")
        path = resolve_repo_path(value, api.state.repo_root, api.state.applications_root)
        if not path.is_file() or path.suffix.lower() != ".docx":
            raise HTTPException(404, "Resume not found")
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    @api.get("/applications/{application_id}/cv.pdf")
    def application_cv_pdf(application_id: int):
        with closing(database()) as db:
            value = get_application(db, application_id)["cv_path"]
        if not value:
            raise HTTPException(404, "No resume attached")
        path = resolve_repo_path(value, api.state.repo_root, api.state.applications_root)
        if not path.is_file() or path.suffix.lower() != ".docx":
            raise HTTPException(404, "Resume not found")
        return docx_pdf_response(path, path.with_suffix(".pdf").name, "The exported resume")

    @api.get("/applications/{application_id}/cover-letter.pdf")
    def application_cover_letter_pdf(application_id: int):
        with closing(database()) as db:
            application = get_application(db, application_id)
        value = application["cover_letter_path"]
        if not value:
            raise HTTPException(404, "No cover letter attached")
        path = resolve_repo_path(value, api.state.repo_root, api.state.applications_root)
        try:
            validate_cover_letter_docx(path, application["company"], position=application["position"])
        except CoverLetterError as error:
            raise HTTPException(404, f"Cover letter missing or invalid : {error}") from error
        return docx_pdf_response(path, "lettre-motivation.pdf", "The exported cover letter")

    @api.get("/cv-options")
    def cv_options() -> list[dict[str, str]]:
        root = api.state.repo_root / "01_Career" / "CV" / "Generated"
        return [{"name": path.name, "path": relative_to_repo(path)} for path in sorted(root.glob("*")) if path.is_file()]

    @api.post("/discover")
    def discover(payload: Discovery, import_results: bool = Query(False)) -> dict[str, Any]:
        jobs = discover_smartrecruiters(payload.company, payload.query)
        with closing(database()) as db:
            max_age_days = search_config(db)["max_offer_age_days"]
        diagnostics = {"rejection_reasons": []}
        with api.state.offer_http_client_factory() as offer_http_client:
            jobs = verify_discovery_results(
                jobs, diagnostics, OfferVerifier(offer_http_client, offer_resolver), max_age_days,
            )
        created, duplicates = 0, 0
        if import_results:
            with closing(database()) as db:
                for job in jobs:
                    try:
                        insert_application(db, job, event_details="Import SmartRecruiters")
                        created += 1
                    except (sqlite3.IntegrityError, ValueError):
                        duplicates += 1
                db.commit()
        return {"jobs": jobs, "created": created, "duplicates": duplicates}

    @api.post("/codex/discover")
    def discover_with_codex() -> dict[str, Any]:
        if not api.state.discovery_lock.acquire(blocking=False):
            raise HTTPException(409, "A search is already running")
        started_at = now()
        run_id = None
        try:
            with closing(database()) as db:
                ai_selected = stage_settings(db, "screening")
                if not ai_selected["capabilities"].get("structured_output"):
                    raise HTTPException(409, "The screening provider does not support Structured Output.")
                provider_id, auth_mode = ai_selected["provider_id"], ai_selected["auth_mode"]
                model, effort = ai_selected["model"], ai_selected["effort"]
                profile = search_profile(db)
                if profile["mode"] == "knowledge_base":
                    if not profile["knowledge_base_available"]:
                        raise HTTPException(409, "No usable Knowledge Base found.")
                    try:
                        profile_context = discovery_context(search_kb_root(db), profile["knowledge_base_selected_files"])
                    except (ValueError, OSError) as error:
                        raise HTTPException(409, str(error)) from error
                else:
                    profile_context = profile["custom_search_prompt"].strip()
                    if not profile_context:
                        raise HTTPException(409, "Define your search profile first.")
                queries = search_queries(profile, profile_context)
                current_search = search_config(db)
                if current_search["mode"] == "AI_DIRECT":
                    search_ai_settings(current_search)
                retrieval_provider = "searxng" if current_search["mode"] == "SEARXNG" else current_search["provider"]
                cursor = db.execute(
                    """INSERT INTO discovery_runs(
                        started_at, status, model, profile_mode, provider_id, auth_mode, effort,
                        search_mode, search_provider, search_model, profile_summary, prompt_summary
                    ) VALUES (?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        started_at, model, profile["mode"], provider_id, auth_mode, effort,
                        current_search["mode"], retrieval_provider, current_search.get("model"),
                        profile_summary(profile_context, profile["mode"]),
                        f"Retrieval {current_search['mode']}, fetch pages, then structured AI screening.",
                    ),
                )
                run_id = cursor.lastrowid
                db.commit()
                try:
                    search_results, search_provider_id, search_model, search_stats = collect_search_results(db, queries, run_id)
                except SearchProviderUnavailableError as error:
                    raise HTTPException(503, str(error)) from error
                web_documents, known_search_urls, document_stats = search_documents(db, search_results, run_id)
                funnel = {**search_stats, **document_stats}
                db.executemany(
                    """INSERT INTO discovery_candidates(
                        discovery_run_id, candidate_id, canonical_url, source_query, source, rank,
                        retrieval_mode, search_provider, pre_classification, selected_for_ai,
                        content_mode, fetch_status, http_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                    [(
                        run_id, item["candidate_id"], item["url"], item["query"], item["source"], item["rank"],
                        item["retrieval_mode"], item["search_provider"], item["pre_classification"],
                        item["content_mode"], item["fetch_status"], item["http_status"],
                    ) for item in web_documents],
                )
                db.commit()

            parse_totals = {"parsed_results": 0, "schema_valid_results": 0, "valid_http_urls": 0}
            token_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            token_keys: set[str] = set()
            ai_fallback_used = False

            def request_screening(batch: list[dict[str, Any]], batch_number: int, retry: bool):
                nonlocal ai_selected, ai_fallback_used, provider_id, auth_mode, model, effort
                payload = [{key: item[key] for key in (
                    "candidate_id", "title", "url", "snippet", "content", "content_mode",
                    "fetch_status", "http_status",
                )} for item in batch]
                prompt = screening_prompt(
                    profile_context, profile["mode"],
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                )
                expected = {item["candidate_id"] for item in batch}
                try:
                    parsed, ai_selected, fallback_used, call_metadata = run_stage_ai(
                        "screening", prompt, DiscoveryOutput.model_json_schema(),
                        lambda raw: parse_discovery_results(raw, expected),
                        isolated=profile["mode"] == "custom_prompt",
                    )
                except (CodexStructuredOutputError, ValidationError, ValueError) as error:
                    raise HTTPException(502, f"Invalid structured response : {error}") from error
                ai_fallback_used = ai_fallback_used or fallback_used
                provider_id, auth_mode = ai_selected["provider_id"], ai_selected["auth_mode"]
                model, effort = ai_selected["model"], ai_selected["effort"]
                decisions, call_diagnostics = parsed
                for key in parse_totals:
                    parse_totals[key] += call_diagnostics[key]
                for key in token_totals:
                    if isinstance(call_metadata.get(key), int):
                        token_totals[key] += call_metadata[key]
                        token_keys.add(key)
                with closing(database()) as progress_db:
                    persist_discovery_metrics(progress_db, run_id, {
                        **parse_totals,
                        **{f"ai_{key}": token_totals[key] for key in token_keys},
                    })
                return decisions, call_diagnostics

            def screening_progress(metrics):
                with closing(database()) as progress_db:
                    persist_discovery_metrics(progress_db, run_id, metrics)

            decisions, ai_metrics, parse_reasons = screen_candidate_batches(
                web_documents, request_screening, progress=screening_progress,
            )
            funnel.update(ai_metrics)
            candidates_by_id = {item["candidate_id"]: item for item in web_documents}
            offers = []
            for decision in decisions:
                candidate = candidates_by_id[decision["candidate_id"]]
                if decision["decision"] != "KEEP":
                    continue
                item = decision["offer"]
                item.update({
                    "_candidate_id": decision["candidate_id"],
                    "eligibility_status": decision["eligibility_status"],
                    "eligibility_reason": decision["reason"] or "Eligibility determined by job screening.",
                    # Publication comes from the fetched source, never an AI estimate.
                    "published_at": candidate["published_at"],
                    "deadline": candidate["deadline"],
                })
                if candidate["availability"] == "closed":
                    item["availability"] = "closed"
                item["eligibility_status"], item["eligibility_reason"] = evaluate_offer_eligibility(
                    item, candidate["content"],
                )
                offers.append(item)

            diagnostics = {
                "provider_results_raw": len(search_results), **parse_totals,
                "open_or_unknown_count": 0, "verified_open": 0, "verified_closed": 0,
                "verified_invalid": 0, "verification_unknown": 0,
                "rejection_reasons": parse_reasons,
            }
            all_keep_offers = list(offers)
            if offers:
                with api.state.offer_http_client_factory() as offer_http_client:
                    offers = verify_discovery_results(
                        offers, diagnostics, OfferVerifier(offer_http_client, offer_resolver),
                        current_search["max_offer_age_days"],
                    )
            else:
                diagnostics.update({"open_or_unknown_count": 0, "rejected_count": len(parse_reasons)})

            with closing(database()) as db:
                for decision in decisions:
                    verification = next((
                        item.get("verification_status", "NOT_RUN") for item in all_keep_offers
                        if item["_candidate_id"] == decision["candidate_id"]
                    ), "NOT_RUN")
                    final_action = "PENDING_INSERT" if decision["decision"] == "KEEP" else decision["decision"]
                    if decision["decision"] == "KEEP" and verification in {"CLOSED", "INVALID"}:
                        final_action = "VERIFICATION_REJECTED"
                    if decision["decision"] == "KEEP" and any(
                        item.get("freshness_rejection_reason") for item in all_keep_offers
                        if item["_candidate_id"] == decision["candidate_id"]
                    ):
                        final_action = "FRESHNESS_REJECTED"
                    db.execute(
                        """UPDATE discovery_candidates SET ai_batch=?, ai_decision=?, ai_reason_code=?,
                           eligibility_status=?, ai_reason=?, verification_status=?, final_action=?
                           WHERE discovery_run_id=? AND candidate_id=?""",
                        (
                            candidates_by_id[decision["candidate_id"]]["ai_batch"], decision["decision"],
                            decision["reason_code"], decision["eligibility_status"], decision["reason"],
                            verification, final_action, run_id, decision["candidate_id"],
                        ),
                    )
                db.commit()
            metadata = search_stats if current_search["mode"] == "AI_DIRECT" else {
                "web_search_calls": search_stats["search_call_count"],
                "web_domains": sorted({urlsplit(item.url).hostname or "" for item in search_results}),
                "web_search_actions": ["search"], "web_search_queries": queries,
                "web_sources": [item.url for item in search_results],
            }
            diagnostics["provider_results_raw"] = len(search_results)

            created = 0
            active = sum(value == "active" for value in known_search_urls.values())
            ignored = sum(value == "ignored" for value in known_search_urls.values())
            excluded = sum(value == "excluded" for value in known_search_urls.values())
            imported = []
            with closing(database()) as db:
                persist_discovery_metrics(db, run_id, diagnostics)
                for item in offers:
                    candidate_id = item["_candidate_id"]
                    known = db.execute(
                        "SELECT status, deleted_at FROM applications WHERE url = ?", (item["url"],),
                    ).fetchone()
                    if known:
                        final_action = "DUPLICATE_EXCLUDED" if known["deleted_at"] else "DUPLICATE_IGNORED" if known["status"] == "IGNORED" else "DUPLICATE_ACTIVE"
                        db.execute(
                            "UPDATE discovery_candidates SET final_action=? WHERE discovery_run_id=? AND candidate_id=?",
                            (final_action, run_id, candidate_id),
                        )
                        if item["url"] in known_search_urls:
                            continue
                        if known["deleted_at"]:
                            excluded += 1
                        elif known["status"] == "IGNORED":
                            ignored += 1
                        else:
                            active += 1
                        continue
                    item.update({
                        "position": item.pop("title"),
                        "description": "",
                        "why_relevant": "\n".join(item.pop("why_relevant")),
                        "notes": "Sources Web :\n" + "\n".join(item.pop("evidence_urls")),
                    })
                    try:
                        application_id = insert_application(db, item, event_details="Recherche Web manuelle")
                    except sqlite3.IntegrityError:
                        diagnostics["rejection_reasons"].append({"url": item["url"], "reason": "DUPLICATE"})
                        db.execute(
                            "UPDATE discovery_candidates SET final_action='DUPLICATE_ACTIVE' WHERE discovery_run_id=? AND candidate_id=?",
                            (run_id, candidate_id),
                        )
                        active += 1
                        continue
                    created += 1
                    db.execute(
                        "UPDATE discovery_candidates SET final_action='INSERTED' WHERE discovery_run_id=? AND candidate_id=?",
                        (run_id, candidate_id),
                    )
                    imported.append(row_dict(get_application(db, application_id)))
                duplicates = active + ignored + excluded
                diagnostics["rejected_count"] = (
                    funnel["ai_reject_count"] + diagnostics["verified_closed"] + diagnostics["verified_invalid"]
                    + sum(bool(item.get("freshness_rejection_reason")) for item in all_keep_offers)
                )
                run_status = "PARTIAL" if search_stats["partial"] else "SUCCESS"
                db.execute("""UPDATE discovery_runs SET
                    finished_at=?, status=?, found_count=?, new_count=?, duplicate_count=?, ignored_count=?,
                    per_query_stats=?, raw_result_count=?, admitted_result_count=?, merged_count=?,
                    intra_query_duplicate_count=?, global_duplicate_count=?, known_url_count=?, unknown_url_count=?,
                    likely_job_detail_count=?, unknown_candidate_count=?, obvious_non_job_count=?,
                    candidate_count=?, candidate_limit_dropped=?, fetch_attempted=?, fetch_succeeded=?, fetch_failed=?,
                    snippet_fallback_count=?, ai_input_count=?,
                    ai_batch_count=?, ai_decision_count=?, ai_keep_count=?, ai_reject_count=?, ai_review_count=?,
                    ai_missing_decision_count=?, ai_retry_count=?, ai_call_count=?,
                    ai_input_tokens=?, ai_output_tokens=?, ai_total_tokens=?,
                    provider_results_raw=?, parsed_results=?, schema_valid_results=?, valid_http_urls=?,
                    open_or_unknown_count=?, verified_open=?, verified_closed=?, verified_invalid=?, verification_unknown=?,
                    duplicates_active=?, duplicates_ignored=?, duplicates_excluded=?,
                    rejected_count=?, inserted_count=?, web_search_calls=?, search_call_count=?,
                    search_input_tokens=?, search_output_tokens=?, search_total_tokens=?, search_provider_errors=?,
                    web_domains=?,
                    web_search_actions=?, web_search_queries=?, web_sources=?, rejection_reasons=?,
                    search_mode=?, search_provider=?, search_model=?, search_fallback_used=0, ai_fallback_used=?,
                    provider_id=?, auth_mode=?, model=?, effort=?
                    WHERE id=?""", (
                    now(), run_status, diagnostics["provider_results_raw"], created, duplicates, ignored + excluded,
                    json.dumps(funnel["per_query_stats"], ensure_ascii=False),
                    funnel["raw_result_count"], funnel["admitted_result_count"], funnel["merged_count"],
                    funnel["intra_query_duplicate_count"], funnel["global_duplicate_count"],
                    funnel["known_url_count"], funnel["unknown_url_count"],
                    funnel["likely_job_detail_count"], funnel["unknown_candidate_count"],
                    funnel["obvious_non_job_count"], funnel["candidate_count"],
                    funnel["candidate_limit_dropped"], funnel["fetch_attempted"],
                    funnel["fetch_succeeded"], funnel["fetch_failed"], funnel["snippet_fallback_count"],
                    funnel["ai_input_count"],
                    funnel["ai_batch_count"], funnel["ai_decision_count"], funnel["ai_keep_count"],
                    funnel["ai_reject_count"], funnel["ai_review_count"], funnel["ai_missing_decision_count"],
                    funnel["ai_retry_count"], funnel["ai_call_count"],
                    token_totals["input_tokens"] if "input_tokens" in token_keys else None,
                    token_totals["output_tokens"] if "output_tokens" in token_keys else None,
                    token_totals["total_tokens"] if "total_tokens" in token_keys else None,
                    diagnostics["provider_results_raw"], diagnostics["parsed_results"],
                    diagnostics["schema_valid_results"], diagnostics["valid_http_urls"],
                    diagnostics["open_or_unknown_count"], diagnostics["verified_open"],
                    diagnostics["verified_closed"], diagnostics["verified_invalid"],
                    diagnostics["verification_unknown"], active, ignored, excluded,
                    diagnostics["rejected_count"], created, metadata["web_search_calls"] or 0,
                    search_stats["search_call_count"], search_stats["search_input_tokens"],
                    search_stats["search_output_tokens"], search_stats["search_total_tokens"],
                    json.dumps(search_stats["search_provider_errors"], ensure_ascii=False),
                    json.dumps(metadata.get("web_domains"), ensure_ascii=False),
                    json.dumps(metadata.get("web_search_actions"), ensure_ascii=False),
                    json.dumps(metadata.get("web_search_queries"), ensure_ascii=False),
                    json.dumps(metadata.get("web_sources"), ensure_ascii=False),
                    json.dumps(diagnostics["rejection_reasons"], ensure_ascii=False),
                    current_search["mode"], search_provider_id, search_model, int(ai_fallback_used),
                    provider_id, auth_mode, model, effort, run_id,
                ))
                db.commit()
            return {
                "run_id": run_id,
                "found": diagnostics["provider_results_raw"], "new": created, "duplicates": duplicates,
                "ignored": ignored + excluded, "rejected": diagnostics["rejected_count"],
                "verified_open": diagnostics["verified_open"], "verified_closed": diagnostics["verified_closed"],
                "verified_invalid": diagnostics["verified_invalid"], "verification_unknown": diagnostics["verification_unknown"],
                "web_search_calls": metadata["web_search_calls"], "errors": diagnostics["rejection_reasons"],
                "jobs": imported, **ai_metrics,
                "search_mode": current_search["mode"], "search_provider": search_provider_id,
                "search_model": search_model, "search_call_count": search_stats["search_call_count"],
                "search_input_tokens": search_stats["search_input_tokens"],
                "search_output_tokens": search_stats["search_output_tokens"],
                "search_total_tokens": search_stats["search_total_tokens"],
                "ai_input_tokens": token_totals["input_tokens"] if "input_tokens" in token_keys else None,
                "ai_output_tokens": token_totals["output_tokens"] if "output_tokens" in token_keys else None,
                "ai_total_tokens": token_totals["total_tokens"] if "total_tokens" in token_keys else None,
            }
        except HTTPException as error:
            if run_id:
                with closing(database()) as db:
                    status = "SEARCH_PROVIDER_UNAVAILABLE" if error.status_code == 503 else "FAILED"
                    db.execute("UPDATE discovery_runs SET finished_at=?, status=?, error=? WHERE id=?",
                               (now(), status, str(error.detail), run_id))
                    db.commit()
            raise
        finally:
            api.state.discovery_lock.release()

    def generate_cv_refresh(offer: dict | None, source: Path, project_order: list[str]):
        with closing(database()) as db:
            config = stage_settings(db, "application_preparation")
            if not config["capabilities"].get("structured_output"):
                raise HTTPException(409, "The preparation provider does not support Structured Output.")
        context = preparation_context(
            api.state.repo_root, offer or {"position": "", "description": ""},
            selected_project_ids=list(project_catalog(api.state.repo_root)) if offer is not None else project_order,
        )

        def validate_refresh(raw):
            output = CvRefreshOutput.model_validate(raw)
            validate_local_sources(api.state.repo_root, output.local_sources)
            output.content = type(output.content).model_validate(clean_artifact_value(output.content.model_dump()))
            selected = [project.project_id for project in output.content.projects]
            catalog = project_catalog(api.state.repo_root)
            if selected[:len(project_order)] != project_order or (offer is None and selected != project_order):
                raise ValueError("Refresh must preserve existing projects and their order")
            for project_id in selected[len(project_order):]:
                title = catalog.get(project_id)
                if title is None or not any(
                    value.replace("\\", "/").startswith(f"02_Projects/{title}/")
                    for value in output.local_sources
                ):
                    raise ValueError("Additional refresh projects require explicit Knowledge Base evidence")
            return output

        output, used_config, _, _ = run_stage_ai(
            "application_preparation", cv_refresh_prompt(offer, context, document_text(source), project_order),
            CvRefreshOutput.model_json_schema(), validate_refresh,
        )
        return output, {
            "provider": used_config["provider_id"], "auth_mode": used_config["auth_mode"],
            "model": used_config["model"], "effort": used_config["effort"],
        }

    api.state.generate_cv_refresh = generate_cv_refresh

    def run_preparation(application_id: int) -> None:
        try:
            with closing(database()) as db:
                application = get_application(db, application_id)
                if application["status"] != "SHORTLISTED":
                    raise HTTPException(409, "AI preparation is only available from SHORTLISTED")
                preparation_config = stage_settings(db, "application_preparation")
                deep_config = stage_settings(db, "deep_analysis")
                company_config = stage_settings(db, "company_analysis")
                if not preparation_config["capabilities"].get("structured_output"):
                    raise HTTPException(409, "The preparation provider does not support Structured Output.")
                provider_id, auth_mode = preparation_config["provider_id"], preparation_config["auth_mode"]
                model, effort = preparation_config["model"], preparation_config["effort"]
                offer = row_dict(application)
            context = preparation_context(api.state.repo_root, offer)
            run_metadata: dict[str, Any] = {}
            fallback_used = False

            def validate_preparation(raw):
                value = PreparationOutput.model_validate(raw)
                validate_local_sources(api.state.repo_root, value.local_sources)
                return value

            try:
                prompt = preparation_prompt(offer, context)
                output, used_config, fallback_used, run_metadata = run_stage_ai(
                    "application_preparation", prompt, PreparationOutput.model_json_schema(), validate_preparation,
                )
                provider_id, auth_mode = used_config["provider_id"], used_config["auth_mode"]
                model, effort = used_config["model"], used_config["effort"]
                if (deep_config["provider_id"], deep_config["auth_mode"], deep_config["model"], deep_config["effort"]) != (
                    preparation_config["provider_id"], preparation_config["auth_mode"], preparation_config["model"], preparation_config["effort"],
                ):
                    deep_output, _, deep_fallback, _ = run_stage_ai(
                        "deep_analysis", prompt, PreparationOutput.model_json_schema(), validate_preparation,
                    )
                    output.analysis_markdown = deep_output.analysis_markdown
                    fallback_used = fallback_used or deep_fallback
                if (company_config["provider_id"], company_config["auth_mode"], company_config["model"], company_config["effort"]) != (
                    preparation_config["provider_id"], preparation_config["auth_mode"], preparation_config["model"], preparation_config["effort"],
                ):
                    company_output, _, company_fallback, _ = run_stage_ai(
                        "company_analysis", prompt, PreparationOutput.model_json_schema(), validate_preparation,
                    )
                    output.company_profile = company_output.company_profile
                    fallback_used = fallback_used or company_fallback
            except CodexStructuredOutputError as error:
                raise HTTPException(502, preparation_error(
                    application_id, model, effort, error.error_type, error.field, str(error), error.metadata,
                )) from error
            except ValidationError as error:
                issue = error.errors(include_url=False, include_input=False)[0]
                field = ".".join(str(part) for part in issue["loc"]) or "$"
                raise HTTPException(502, preparation_error(
                    application_id, model, effort, issue["type"], field,
                    f"Validation response failed: {field}: {issue['msg']}", run_metadata,
                )) from error
            except ValueError as error:
                raise HTTPException(502, preparation_error(
                    application_id, model, effort, "source_validation", "local_sources", str(error), run_metadata,
                )) from error
            except (CodexError, OSError) as error:
                raise HTTPException(502, preparation_error(
                    application_id, model, effort, "codex", "$", str(error), run_metadata,
                )) from error
            if any("À compléter" in text for text in (
                output.analysis_markdown, output.interview_prep_markdown,
                json.dumps(output.company_profile.model_dump(), ensure_ascii=False),
                json.dumps(output.cover_letter.model_dump(), ensure_ascii=False),
                json.dumps(output.cv.model_dump(), ensure_ascii=False),
            )):
                raise HTTPException(422, "AI returned a forbidden placeholder")

            decision = output.cv
            if decision.content is not None:
                decision.content = type(decision.content).model_validate(
                    clean_artifact_value(decision.content.model_dump())
                )
            if decision.reusable_content is not None:
                decision.reusable_content = type(decision.reusable_content).model_validate(
                    clean_artifact_value(decision.reusable_content.model_dump())
                )
            catalog = project_catalog(api.state.repo_root)
            variants = cv_variants(api.state.repo_root)
            matching_variants = [
                name for name, variant in variants.items()
                if variant["project_set"] == frozenset(decision.project_set)
            ]
            folder = application_folder(api.state.applications_root, application_id, application["company"], application["position"])
            folder.mkdir(parents=True, exist_ok=True)
            cv_folder = folder / "cv"
            cv_folder.mkdir(exist_ok=True)
            cv_root = api.state.repo_root / "01_Career" / "CV" / "Generated"
            cv_template = cv_template_path(api.state.repo_root)
            if decision.action in {"REUSE", "ADAPT"}:
                source = variants.get(decision.source_variant or "")
                if not source:
                    raise HTTPException(422, "Source resume missing from variant catalog")
                cv_source = source["path"].resolve()
                decision.source_path = relative_to_repo(cv_source)
                if decision.action == "REUSE":
                    decision.project_set = list(source["project_order"])
                    decision.project_order = list(source["project_order"])
                if source["project_set"] != frozenset(decision.project_set):
                    raise HTTPException(422, f"{decision.action} must preserve the source project_set",)
            else:
                cv_source = cv_template
                if decision.source_path or decision.source_variant:
                    raise HTTPException(422, "CREATE cannot specify a source variant")
                if matching_variants:
                    raise HTTPException(422, "CREATE blocked: this project_set already exists in Generated")
                generic = json.dumps(decision.reusable_content.model_dump(), ensure_ascii=False).casefold()
                forbidden = [application["company"], application["position"]]
                if any(value and value.casefold() in generic for value in forbidden):
                    raise HTTPException(422, "The global CREATE variant contains job-specific information")

            unknown = set(decision.project_set) - set(catalog)
            if unknown:
                raise HTTPException(422, f"Unknown projects in the KB : {', '.join(sorted(unknown))}")

            filename = (
                Path(application["cv_path"]).name if application["cv_path"] else decision.target_filename or (
                    cv_source.name if decision.action == "REUSE"
                    else f"resume_{slugify(application['company'])}_{slugify(application['position'])}.docx"
                )
            )
            if Path(filename).name != filename or Path(filename).suffix.lower() != ".docx":
                raise HTTPException(422, "Invalid resume filename")
            destination = cv_folder / filename
            try:
                if decision.action == "REUSE":
                    if decision.content is not None:
                        raise CvDocumentError("REUSE must not supply modified content")
                    validate_cv_layout(cv_source)
                    shutil.copy2(cv_source, destination)
                    validate_docx(destination)
                else:
                    if decision.content is None:
                        raise CvDocumentError(f"{decision.action} requires final resume content")
                    create_cv(cv_source, destination, decision.content, replace=True)
                rendered_titles = (
                    {project.project_id: project.title for project in decision.content.projects}
                    if decision.content else None
                )
                validate_cv_projects(destination, decision.project_order, catalog, rendered_titles)
            except CvDocumentError as error:
                raise HTTPException(422, f"Unable to create resume : {error}") from error

            cv_path = relative_to_repo(destination)
            base = decision.source_variant or "base template"
            retained = "; ".join(decision.changes) or "Source resume retained unchanged"
            gaps = "; ".join(decision.missing_fit) or "No additional gaps reported"
            analysis_cv = (
                f"\n\n## Resume decision\n\n"
                f"- Action : `{decision.action}`\n"
                f" - Source variant : {base}\n"
                f" - Justification : {decision.justification}\n"
                f" - Project set : {', '.join(decision.project_set)}\n"
                f" - Project order : {', '.join(decision.project_order)}\n"
                f" - Main retained content : {retained}\n"
                f" - Gaps compared with existing variants : {gaps}\n"
            )
            profile = output.company_profile
            if application["company_address_overridden"]:
                profile = profile.model_copy(update={"postal_address": application["company_postal_address"]})
            company_document = company_markdown(application["company"], profile)
            company_values = profile_columns(profile)
            cover_letter = type(output.cover_letter).model_validate(
                clean_artifact_value(output.cover_letter.model_dump())
            )
            documents = {
                "offer_path": ("offer.md", build_offer_markdown(application)),
                "analysis_path": ("analysis.md", output.analysis_markdown + analysis_cv),
                "company_path": ("company.md", company_document),
                "interview_prep_path": ("interview-prep.md", output.interview_prep_markdown),
            }
            try:
                documents = {
                    field: (filename, clean_artifact_text(content))
                    for field, (filename, content) in documents.items()
                }
                validate_interview_prep(
                    documents["interview_prep_path"][1], application["company"]
                )
                validate_user_artifacts(documents, destination)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            paths = {}
            for field, (filename, content) in documents.items():
                path = folder / filename
                path.write_text(content.rstrip() + "\n", encoding="utf-8")
                paths[field] = relative_to_repo(path)
            letter_path = folder / "lettre-motivation.docx"
            try:
                cover_letter_word_count = create_cover_letter_docx(
                    api.state.repo_root, letter_path, application["company"], application["position"],
                    company_values["company_postal_address"], cover_letter.paragraphs,
                    cover_letter.language, salutation=cover_letter.salutation,
                    closing=cover_letter.closing,
                )
            except CoverLetterError as error:
                raise HTTPException(422, str(error)) from error
            paths["cover_letter_path"] = relative_to_repo(letter_path)
            paths["cv_path"] = relative_to_repo(destination)
            paths["cv_variant"] = decision.source_variant or "Tailored resume"
            paths["cover_letter_word_count"] = cover_letter_word_count
            paths.update(company_values)
            missing_information = missing_optional_cover_letter_values(
                company_values["company_postal_address"]
            )

            generated_variant = None
            if decision.action == "CREATE":
                with api.state.preparation_lock:
                    current_variants = cv_variants(api.state.repo_root)
                    if any(
                        variant["project_set"] == frozenset(decision.project_set)
                        for variant in current_variants.values()
                    ):
                        raise HTTPException(422, "CREATE interrupted: this project_set was just registered")
                    generated_variant = next_variant_name(current_variants)
                    variant_path = cv_root / f"resume_{generated_variant}.docx"
                    reusable_order = [project.project_id for project in decision.reusable_content.projects]
                    try:
                        create_cv(cv_template, variant_path, decision.reusable_content)
                        reusable_titles = {
                            project.project_id: project.title for project in decision.reusable_content.projects
                        }
                        validate_cv_projects(variant_path, reusable_order, catalog, reusable_titles)
                        register_variant(
                            api.state.repo_root, generated_variant, reusable_order,
                            catalog, decision.reusable_content.headline,
                        )
                    except (CvDocumentError, OSError, KeyError) as error:
                        variant_path.unlink(missing_ok=True)
                        raise HTTPException(422, f"Unable to create global variant : {error}") from error
                paths["cv_variant"] = generated_variant

            with closing(database()) as db:
                assignments = ", ".join(f"{field} = ?" for field in paths)
                db.execute(f"UPDATE applications SET {assignments}, updated_at=? WHERE id=?",
                           [*paths.values(), now(), application_id])
                add_event(db, application_id, "CODEX_PREPARATION", json.dumps({
                    "action": decision.action,
                    "source_variant": decision.source_variant,
                    "cv_decision": decision.action,
                    "cv_source": decision.source_path,
                    "cv_project_set": decision.project_set,
                    "cv_project_order": decision.project_order,
                    "cv_generated_variant": generated_variant,
                    "reason": decision.justification,
                    "justification": decision.justification,
                    "cv_path": paths["cv_path"],
                    "application_id": application_id,
                    "provider": provider_id,
                    "model": model,
                    "effort": effort,
                    "fallback_used": fallback_used,
                    "status": run_metadata.get("status", "completed"),
                    "thread_id": run_metadata.get("thread_id"),
                    "turn_id": run_metadata.get("turn_id"),
                    "local_sources": output.local_sources,
                    "cover_letter": {"path": paths["cover_letter_path"], "validated": True},
                    "missing_information": missing_information,
                }, ensure_ascii=False))
                db.commit()
                artifacts = validate_application_artifacts(
                    get_application(db, application_id), api.state.repo_root,
                    api.state.applications_root,
                )
                if not artifacts["complete"]:
                    raise HTTPException(422, {"message": "Incomplete preparation", **artifacts})
                change_status(db, application_id, "PREPARED", "AI preparation completed")
        except HTTPException as error:
            with closing(database()) as db:
                add_event(db, application_id, "PREPARATION_FAILED", str(error.detail))
                db.commit()
        except Exception as error:
            with closing(database()) as db:
                add_event(db, application_id, "PREPARATION_FAILED", str(error))
                db.commit()
        finally:
            finish_preparation(application_id)

    def start_preparation(application_id: int) -> None:
        threading.Thread(target=run_preparation, args=(application_id,), daemon=True).start()

    def finish_preparation(application_id: int) -> None:
        next_application_id = None
        with api.state.preparation_lock:
            api.state.active_preparations -= 1
            api.state.preparation_jobs.discard(application_id)
            if api.state.preparation_queue:
                next_application_id = api.state.preparation_queue.popleft()
                api.state.active_preparations += 1
                with closing(database()) as db:
                    add_event(db, next_application_id, "PREPARATION_STARTED", "AI preparation started")
                    db.commit()
        if next_application_id is not None:
            start_preparation(next_application_id)

    def enqueue_preparation(application_id: int) -> dict[str, Any]:
        if not knowledge_base_available(api.state.repo_root):
            raise HTTPException(
                409, "A Knowledge Base or candidate profile is required to prepare an application.",
            )
        start_now = False
        with api.state.preparation_lock:
            with closing(database()) as db:
                application = get_application(db, application_id)
                if application["status"] != "SHORTLISTED":
                    raise HTTPException(409, "AI preparation is only available from SHORTLISTED")
                if application["eligibility_status"] == "INELIGIBLE":
                    raise HTTPException(409, f"Preparation not allowed : {application['eligibility_reason']}")
                active_settings(db)
                if application_id in api.state.preparation_jobs:
                    raise HTTPException(409, "This preparation is already queued or running")
                api.state.preparation_jobs.add(application_id)
                if api.state.active_preparations < MAX_CONCURRENT_PREPARATIONS:
                    api.state.active_preparations += 1
                    add_event(db, application_id, "PREPARATION_STARTED", "AI preparation started")
                    start_now = True
                else:
                    api.state.preparation_queue.append(application_id)
                    add_event(db, application_id, "PREPARATION_QUEUED", "AI preparation queued")
                db.commit()
                result = application_view(db, get_application(db, application_id))
        if start_now:
            start_preparation(application_id)
        return result

    @api.post("/applications/{application_id}/prepare-with-codex")
    def prepare_with_codex(application_id: int) -> dict[str, Any]:
        return enqueue_preparation(application_id)

    @api.post("/applications/{application_id}/select")
    def select_application(application_id: int) -> dict[str, Any]:
        if not knowledge_base_available(api.state.repo_root):
            raise HTTPException(
                409, "A Knowledge Base or candidate profile is required to prepare an application.",
            )
        with api.state.preparation_lock:
            with closing(database()) as db:
                active_settings(db)
                application = get_application(db, application_id)
                if application["status"] != "DETECTED":
                    raise HTTPException(409, "This job was already selected or processed")
                change_status(db, application_id, "SHORTLISTED", "Job selected by user")
        return enqueue_preparation(application_id)

    @api.get("/stats/ai-usage")
    def ai_usage_stats(period: Literal["today", "7d", "30d", "all"] = "7d") -> dict[str, Any]:
        current = datetime.now(timezone.utc)
        today = current.replace(hour=0, minute=0, second=0, microsecond=0)
        start = None if period == "all" else today - timedelta(
            days={"today": 0, "7d": 6, "30d": 29}[period],
        )
        where = "created_at <= ?"
        parameters = [current.isoformat()]
        if start is not None:
            where += " AND created_at >= ?"
            parameters.append(start.isoformat())

        aggregates = """COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,
            SUM(output_tokens) AS output_tokens, SUM(total_tokens) AS total_tokens,
            SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL OR total_tokens IS NULL
                THEN 1 ELSE 0 END) AS unknown_calls"""

        def summary(row: sqlite3.Row | None) -> dict[str, Any]:
            if row is None or row["calls"] == 0:
                return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                        "calls": 0, "unknown_calls": 0}
            return {key: row[key] for key in (
                "input_tokens", "output_tokens", "total_tokens", "calls", "unknown_calls",
            )}

        with closing(database()) as db:
            db.execute("BEGIN")
            total = summary(db.execute(
                f"SELECT {aggregates} FROM ai_usage WHERE {where}", parameters,
            ).fetchone())
            categories = {row["category"]: summary(row) for row in db.execute(
                f"SELECT category, {aggregates} FROM ai_usage WHERE {where} GROUP BY category", parameters,
            )}
            first = db.execute(f"SELECT MIN(created_at) FROM ai_usage WHERE {where}", parameters).fetchone()[0]
            if start is None and first is not None:
                start = datetime.fromisoformat(first).astimezone(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0,
                )
            granularity = "hour" if period == "today" else (
                "month" if period == "all" and start is not None and (today - start).days > 120 else "day"
            )
            date_format = {"hour": "%Y-%m-%dT%H:00:00Z", "day": "%Y-%m-%d", "month": "%Y-%m-01"}[granularity]
            buckets = {(row["date"], row["category"]): row["tokens"] for row in db.execute(
                f"""SELECT strftime(?, created_at) AS date, category, SUM(total_tokens) AS tokens
                    FROM ai_usage WHERE {where} GROUP BY date, category ORDER BY date""",
                [date_format, *parameters],
            )}

        daily = []
        if total["calls"] and start is not None:
            cursor = start.replace(day=1) if granularity == "month" else start
            while cursor <= current:
                date = cursor.strftime(date_format)
                daily.append({"date": date, "search_tokens": buckets.get((date, "SEARCH"), 0),
                              "document_tokens": buckets.get((date, "DOCUMENT"), 0)})
                if granularity == "month":
                    cursor = cursor.replace(year=cursor.year + cursor.month // 12, month=cursor.month % 12 + 1)
                else:
                    cursor += timedelta(hours=1) if granularity == "hour" else timedelta(days=1)
        return {
            "period": period, "total": total, "search": categories.get("SEARCH", summary(None)),
            "documents": categories.get("DOCUMENT", summary(None)), "daily": daily,
            "granularity": granularity, "timezone": "UTC",
        }

    @api.get("/stats")
    def stats() -> dict[str, Any]:
        with closing(database()) as db:
            counts = {row["status"]: row["count"] for row in db.execute("SELECT status, COUNT(*) AS count FROM applications WHERE deleted_at IS NULL GROUP BY status")}
            rows = [row_dict(row) for row in db.execute("SELECT * FROM applications WHERE deleted_at IS NULL ORDER BY updated_at DESC")]
        current = datetime.now(timezone.utc)
        followups, today, interviews = [], [], []
        for item in rows:
            if item["status"] == "SENT" and item["sent_at"]:
                sent = datetime.fromisoformat(item["sent_at"])
                if sent <= current - timedelta(days=10):
                    followups.append(item)
            if item["next_action_at"] and item["next_action_at"][:10] <= current.date().isoformat():
                today.append(item)
            if item["status"] in {"HR_INTERVIEW", "TECH_INTERVIEW", "FINAL_INTERVIEW"} and item["interview_at"]:
                interviews.append(item)
        return {
            "counts": counts,
            "phases": {
                "search": counts.get("DETECTED", 0),
                "ignored": counts.get("IGNORED", 0),
                "validation": counts.get("SHORTLISTED", 0) + counts.get("PREPARED", 0),
                "send": sum(counts.get(status, 0) for status in ("AWAITING_VALIDATION", "APPROVED", "SUBMITTING")),
                "tracking": sum(counts.get(status, 0) for status in (
                    "SENT", "ACKNOWLEDGED", "HR_INTERVIEW", "TECH_INTERVIEW", "FINAL_INTERVIEW",
                    "OFFER", "NO_RESPONSE",
                )),
                "rejected": counts.get("REJECTED", 0),
            },
            "new": counts.get("DETECTED", 0),
            "shortlisted": counts.get("SHORTLISTED", 0),
            "to_prepare": counts.get("SHORTLISTED", 0),
            "to_validate": counts.get("AWAITING_VALIDATION", 0),
            "sent": counts.get("SENT", 0),
            "interviews": sum(counts.get(status, 0) for status in ("HR_INTERVIEW", "TECH_INTERVIEW", "FINAL_INTERVIEW")),
            "followups": followups,
            "today": today,
            "upcoming_interviews": interviews,
        }

    return api


app = create_app()
