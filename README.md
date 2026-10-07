# AI Job Application Workbench

[English](README.md) | [Français](README.fr.md) | [简体中文](README.zh-CN.md)

An **AI-assisted job application workbench** running locally. Collect opportunities, assess relevance, select grounded candidate facts, reuse/adapt/create resumes, generate application documents, review them manually and track applications. This is a human-in-the-loop workflow; submission remains manual.

## Workflow

`SEARCH → REVIEW → SUBMIT → TRACK`

Discover or add a job manually. Inspect relevance, eligibility and posting freshness. Shortlist to queue preparation. Review the resume, cover letter, analysis, company profile and interview notes in the document viewers. Approve, submit manually, then record progress. Ignore/deduplication, Trash/Restore, withdrawal and cleanup are supported. Trash/closed-application cleanup can delete generated documents; review the confirmation before proceeding.

## Architecture

React + TypeScript + Vite frontend, FastAPI backend, local SQLite, supported LLM providers and an explicitly configured external Knowledge Base. The application is self-contained; the Knowledge Base remains external and configurable.

```text
backend/app/        API, workflows, providers, documents and usage ledger
backend/scripts/    staged resume refresh and artifact maintenance
backend/tests/      isolated synthetic tests
frontend/src/       English UI and interactive tests
docs/               Knowledge Base contract in three languages
data/               local database and applications (ignored; created at runtime)
```

## Requirements and installation

Validated locally on Windows with Python 3.13.1, Node.js 24.13.0 and npm 11.10.0. Linux/macOS setup commands are documented but have not yet been validated on those platforms.

Python 3.11+, Node.js 22.12+ or 24+, npm bundled with Node. Codex CLI is needed only for ChatGPT/Codex document preparation.

```sh
git clone https://github.com/Driw0x/ai-job-application-workbench ai-job-application-workbench
cd ai-job-application-workbench
python -m venv .venv
```

PowerShell, from the repository root:

```powershell
. .venv/Scripts/Activate.ps1
python -m pip install -r backend/requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --app-dir backend --env-file .env --host 127.0.0.1 --port 8000
```

On Linux/macOS, activate with `source .venv/bin/activate` and copy with `cp .env.example .env`; the pip and uvicorn commands are identical. In another terminal:

```sh
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Open `http://127.0.0.1:5173`. API documentation: `http://127.0.0.1:8000/docs`. Keep both services on loopback; this application has no multi-user access control.

## Configuration and Knowledge Base

Copy `.env.example` locally. Set `KNOWLEDGE_BASE_PATH` to your external KB directory, or save the directory in Search profile. The saved directory takes precedence over the environment on subsequent starts and applies to search and document generation. Select Markdown sources in the dashboard. The KB can describe your profile, skills, education, experience, projects, constraints and existing resume variants. Only documented candidate facts may be used.

`DATABASE_PATH` optionally overrides the default `data/job_tracker.db`. Application artifacts remain under `data/applications`. No personal database is distributed: startup initializes an empty schema. Keep your KB outside this repository. Document generation requires a compatible KB and your own DOCX templates; see the contract below. Search with a custom prompt works without a KB.

## Custom prompts

Choose **Custom prompt**, enter criteria, then save. For discovery and relevance screening, the saved prompt is the exclusive source of candidate/search criteria. KB files, the KB profile and previous KB-derived prompts are not silently merged. Fixed safety rules, output schemas and retrieved job evidence still apply. Empty prompts are rejected. Switch back to **Knowledge Base** to restore the saved KB selection. This mode does not replace the candidate KB required for resume and cover-letter preparation.

## ChatGPT / Codex and other providers

For ChatGPT document preparation, install Codex CLI through your usual supported installation method and ensure `codex` is on `PATH`. In provider settings, choose **Continue with ChatGPT**, complete the browser OAuth flow, then explicitly select provider, available model and reasoning effort. The ChatGPT/Codex integration uses `codex app-server` for preparation and the Responses streaming path for supported direct Web search. Availability depends on account/model capabilities; a ChatGPT subscription does not guarantee every operation. There is no automatic switch to API billing.

For **OpenAI API**, enter an API key in provider settings, test/save it, acknowledge potential API charges, then explicitly select OpenAI and an available model. Responses, Structured Outputs, reasoning and Web search are used when supported. API use can incur charges independently of a ChatGPT subscription. Anthropic, Gemini and DeepSeek are also supported; capability checks apply, including no DeepSeek direct Web search.

Select the active provider/model explicitly. Stage-specific overrides and a separately enabled fallback are supported; fallback is off by default. For retrieval, configure your SearXNG instance or choose **AI_DIRECT** with a Web-capable provider. Structured relevance screening then uses the common pipeline.

API keys use the operating-system keyring, service `AIJobApplicationWorkbench`; there is no plaintext fallback. ChatGPT OAuth files use `%LOCALAPPDATA%/AIJobApplicationWorkbench/chatgpt/` on Windows or `~/.config/AIJobApplicationWorkbench/chatgpt/` otherwise. Credentials are stored locally for this application and are never committed to the repository. Logout removes local OAuth state and attempts revocation.

## Resumes, cover letters and refresh

**REUSE** copies a suitable existing resume. **ADAPT** retains its project set while tailoring grounded content. **CREATE** selects four relevant grounded projects when available, otherwise only the available relevant projects (one to four); it can register a reusable variant. Never invent a fourth project. Cover-letter context uses the same selected projects, with five grounded paragraphs.

Preparation and refresh use recruiter-facing natural prose, factual claims, meaningful readable metrics, no raw logs or benchmark dumps, understandable project names and a final editorial review. DOCX layout, skill balancing and validation are supported. Resume language follows the supplied template; cover-letter language follows the posting. The application UI and generated analysis/company/interview headings are English.

Refresh eligible unsent REVIEW/SUBMIT documents without recreating the application. First inspect the dry-run, stage, review the DOCX files manually, then apply:

```sh
python backend/scripts/refresh_cvs.py
python backend/scripts/refresh_cvs.py --stage data/cv-review
python backend/scripts/refresh_cvs.py --apply data/cv-review
```

CLI scripts use exported environment variables or the saved dashboard configuration; they do not load `.env` themselves. Pass `--knowledge-base` and `--database` explicitly when needed. Application refresh preserves existing projects/order and can append grounded relevant projects to reach four. Status, offer, history, decision metadata, original variant and other documents remain intact. Global-variant refresh (`--include-variants`) retains the exact manifest project set/order. Changed files or applications invalidate a staged plan. The cleanup script defaults to dry-run; inspect its `--help` and report before executing changes. The legacy letter migration script writes when invoked and requires exported KB/database configuration.

The application includes Markdown and DOCX viewers with integrated navigation. PDF conversion tries Microsoft Word on Windows, then LibreOffice. Install an available converter separately if you need PDF; absence produces an explicit error. Review pagination and content before using any document.

## Token usage

Numbers/Charts views show search versus document-generation totals and date charts. The ledger records provider/model/stage metadata; discovery also exposes observed Web-search metadata. Unknown usage remains unavailable; observed zero remains zero. Date filters use UTC. No invented prices, external analytics service or telemetry is added. Provider requests transmit the context explicitly required by the selected workflow.

## Tests

```sh
cd backend
python -m pytest -q --tb=short
cd ../frontend
npm test
npm run typecheck
npm run build
```

Tests use synthetic KBs/templates and temporary databases, without real credentials or billable calls. Frontend interactions use Vitest/jsdom; no additional browser framework is required. For a restricted Windows environment, set `TEMP` and `TMP` to a writable temporary directory outside the repository. A live provider/OAuth smoke test requires your own account; PDF output requires a converter.

## Security and responsible use

Never commit `.env`, API keys, OAuth credentials, tokens, private KBs/prompts, personal resumes/letters, generated outputs or personal SQLite databases. `.gitignore` excludes these common paths and caches; inspect staged files before publishing. No personal template or source database is included. Reusable-variant creation intentionally writes generated resumes and the variant manifest into your configured external KB; back up that KB.

Validate every generated claim and document before use. Grounding cannot replace human review. The application does not provide automatic submission or mass application mechanisms. You remain responsible for submitted material. Project code is available under the [MIT License](LICENSE). Third-party dependencies retain their own licenses; dependency sources and build outputs are not included in this repository.

## Documentation

- [External Knowledge Base and template contract](docs/knowledge-base.md)
- [AI Knowledge Workflows](https://github.com/Driw0x/ai-knowledge-workflows) — Optional companion repository for reusable AI knowledge workflows.