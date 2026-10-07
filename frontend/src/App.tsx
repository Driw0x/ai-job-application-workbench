import { FormEvent, ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { renderAsync } from "docx-preview";
import { matchPath, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const API = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";
const STATUS_GROUPS: Record<string, readonly string[]> = {
  SEARCH: ["DETECTED"],
  REVIEW: ["SHORTLISTED", "PREPARED"],
  SUBMIT: ["AWAITING_VALIDATION", "APPROVED", "SUBMITTING"],
  TRACK: ["SENT", "ACKNOWLEDGED", "HR_INTERVIEW", "TECH_INTERVIEW", "FINAL_INTERVIEW", "OFFER", "REJECTED", "NO_RESPONSE"],
  WITHDRAW: ["WITHDRAWN"],
  IGNORED: ["IGNORED"],
};
const STATUS_FILTERS = [
  ["Search", STATUS_GROUPS.SEARCH],
  ["Review", STATUS_GROUPS.REVIEW],
  ["Submit", STATUS_GROUPS.SUBMIT],
  ["Track", STATUS_GROUPS.TRACK],
  ["Withdrawn", STATUS_GROUPS.WITHDRAW],
  ["Ignored job", STATUS_GROUPS.IGNORED],
] as const;
const TRACKING = new Set(STATUS_GROUPS.TRACK);
const INTERVIEW_STATUSES = new Set(["HR_INTERVIEW", "TECH_INTERVIEW", "FINAL_INTERVIEW"]);
const PAGE_SIZE = 10;
const FOLLOW_UP: Record<string, [string, string][]> = {
  SENT: [["ACKNOWLEDGED", "Acknowledged"], ["HR_INTERVIEW", "HR interview"], ["TECH_INTERVIEW", "Technical interview"], ["REJECTED", "Rejected"], ["WITHDRAWN", "Withdraw"], ["NO_RESPONSE", "No response"]],
  ACKNOWLEDGED: [["HR_INTERVIEW", "HR interview"], ["TECH_INTERVIEW", "Technical interview"], ["REJECTED", "Rejected"], ["WITHDRAWN", "Withdraw"], ["NO_RESPONSE", "No response"]],
  HR_INTERVIEW: [["TECH_INTERVIEW", "Technical interview"], ["FINAL_INTERVIEW", "Final interview"], ["OFFER", "Offer received"], ["REJECTED", "Rejected"], ["WITHDRAWN", "Withdraw"]],
  TECH_INTERVIEW: [["FINAL_INTERVIEW", "Final interview"], ["OFFER", "Offer received"], ["REJECTED", "Rejected"], ["WITHDRAWN", "Withdraw"]],
  FINAL_INTERVIEW: [["OFFER", "Offer received"], ["REJECTED", "Rejected"], ["WITHDRAWN", "Withdraw"]],
  NO_RESPONSE: [["ACKNOWLEDGED", "Acknowledged"], ["HR_INTERVIEW", "HR interview"], ["REJECTED", "Rejected"], ["WITHDRAWN", "Withdraw"]],
};
const EFFORT_LABELS: Record<string, string> = { low: "Low", medium: "Medium", high: "High", xhigh: "Very high", max: "Maximum" };
const API_PROVIDER_LABELS = { openai: "OpenAI", anthropic_api: "Anthropic / Claude", gemini_api: "Google Gemini", deepseek_api: "DeepSeek" } as const;
const SEARCH_PROVIDER_LABELS = { chatgpt_oauth: "ChatGPT", ...API_PROVIDER_LABELS } as const;
const SEARCH_MODEL_LABELS: Record<string, string> = { "gpt-5.6-sol": "GPT-5.6 Sol" };
const SEARCH_PROFILE_EXAMPLE = "Describe the roles, location, availability and requirements you want to search for.";

type Application = {
  id: number; company: string; position: string; url: string; source: string; location: string;
  detected_at: string; deadline?: string; status: string; cv_variant?: string; cv_path?: string;
  offer_path?: string; analysis_path?: string; company_path?: string;
  cover_letter_path?: string; cover_letter_word_count?: number;
  interview_prep_path?: string; application_channel?: string; sent_at?: string;
  next_action?: string; next_action_at?: string; interview_at?: string; interview_notes?: string;
  notes: string; why_relevant?: string; published_at?: string; contract_type?: string; updated_at: string; events?: Event[];
  eligibility_status?: "ELIGIBLE" | "INELIGIBLE" | "ELIGIBILITY_UNCERTAIN"; eligibility_reason?: string;
  preparation_state?: "queued" | "running" | "failed" | "completed"; preparation_error?: string;
  cv_action?: "REUSE" | "ADAPT" | "CREATE"; cv_justification?: string;
  company_description?: string; company_postal_address?: string; company_domain?: string;
  company_completed_achievements?: string[]; company_planned_developments?: string[];
  company_comparable_actors?: string[]; company_address_warning?: string;
  missing_information?: { field: string; label: string }[];
  deleted_at?: string; ignored_at?: string;
  artifacts?: { complete: boolean; missing: string[]; invalid: string[] };
};
const ELIGIBILITY_MESSAGES: Record<NonNullable<Application["eligibility_status"]>, { label: string; description: string }> = {
  ELIGIBLE: { label: "Eligible", description: "No restriction detected." },
  INELIGIBLE: { label: "Ineligible", description: "A restriction prevents this application." },
  ELIGIBILITY_UNCERTAIN: { label: "Eligibility uncertain", description: "Some requirements need verification." },
};

function EligibilityNotice({ status = "ELIGIBLE", reason }: { status?: Application["eligibility_status"]; reason?: string }) {
  const message = ELIGIBILITY_MESSAGES[status] || ELIGIBILITY_MESSAGES.ELIGIBILITY_UNCERTAIN;
  const description = reason?.trim();
  const explanation = description && description !== "Eligibility determined by job screening." ? description : message.description;
  return <div className="eligibility-notice"><strong className={status === "ELIGIBLE" ? "success" : "failure-text"}>{status === "ELIGIBILITY_UNCERTAIN" && "⚠ "}{message.label}</strong><small className="muted" title={explanation}>{explanation}</small></div>;
}

type Event = { id: number; event_type: string; details: string; created_at: string };
type Stats = { phases: { search: number; ignored: number; validation: number; send: number; tracking: number; rejected: number }; followups: Application[]; today: Application[]; upcoming_interviews: Application[] };
type UsagePeriod = "today" | "7d" | "30d" | "all";
type UsageSummary = { input_tokens: number | null; output_tokens: number | null; total_tokens: number | null; calls: number; unknown_calls: number };
type AiUsage = {
  period: UsagePeriod; total: UsageSummary; search: UsageSummary; documents: UsageSummary;
  daily: { date: string; search_tokens: number | null; document_tokens: number | null }[];
  granularity: "hour" | "day" | "month"; timezone: "UTC";
};
type DocumentKind = "offer" | "analysis" | "company" | "cover-letter" | "interview-prep";
type AuthStatus = { connected: boolean; account?: string };
type CodexModel = { model: string; displayName?: string; hidden?: boolean; reasoningEfforts?: string[]; capabilities?: Capabilities };
type SearchProfile = { mode: "knowledge_base" | "custom_prompt"; custom_search_prompt: string; custom_search_prompt_updated_at?: string; knowledge_base_available: boolean; knowledge_base_root: string; knowledge_base_sources: string[]; knowledge_base_files: { path: string; label: string }[]; knowledge_base_selected_files: string[]; candidate_profile_available: boolean | null };
type DiscoveryRun = {
  finished_at?: string; status: string; model: string; effort?: string; provider_id?: ProviderId;
  profile_mode?: SearchProfile["mode"]; provider_results_raw: number | null; new_count: number | null;
  duplicate_count: number | null; ignored_count: number | null; rejected_count: number | null; web_search_calls: number | null; error?: string;
  verified_open: number | null; verified_closed: number | null; verified_invalid: number | null; verification_unknown: number | null;
  raw_result_count?: number | null; admitted_result_count?: number | null; merged_count?: number | null;
  extracted_url_count?: number | null; invalid_url_count?: number | null; source_url_count?: number | null;
  intra_query_duplicate_count?: number | null; global_duplicate_count?: number | null;
  known_url_count?: number | null; unknown_url_count?: number | null; likely_job_detail_count?: number | null;
  unknown_candidate_count?: number | null; obvious_non_job_count?: number | null; candidate_count?: number | null;
  fetch_attempted?: number | null; fetch_succeeded?: number | null;
  fetch_failed?: number | null; snippet_fallback_count?: number | null; ai_input_count?: number | null;
  ai_batch_count?: number | null; ai_decision_count?: number | null; ai_keep_count?: number | null;
  ai_reject_count?: number | null; ai_review_count?: number | null; ai_missing_decision_count?: number | null;
  ai_retry_count?: number | null; ai_call_count?: number | null; ai_input_tokens?: number | null;
  ai_output_tokens?: number | null; ai_total_tokens?: number | null;
  search_mode?: "SEARXNG" | "AI_DIRECT"; search_provider?: string; search_model?: string;
  search_call_count?: number | null; search_input_tokens?: number | null;
  search_output_tokens?: number | null; search_total_tokens?: number | null;
  open_or_unknown_count?: number | null; inserted_count?: number | null;
  per_query_stats?: string | null;
};
type AuthMode = "chatgpt_oauth" | "api_key";
type ConfigPanel = "account" | "api";
type ProviderId = keyof typeof API_PROVIDER_LABELS;
type ActiveChoice = "chatgpt_oauth" | ProviderId | "";
type ProviderChoice = "chatgpt_oauth" | ProviderId;
type Capabilities = { structured_output: boolean; web_search: boolean; reasoning_effort: boolean; streaming: boolean; tool_calling?: boolean };
type ApiKeyStatus = { configured: boolean; last_four?: string; billing_confirmed: boolean };
type AiConfig = {
  chatgpt: AuthStatus & { mode: string };
  api_keys: Record<ProviderId, ApiKeyStatus>;
  active: null | { provider_id: ProviderId; auth_mode: AuthMode; model?: string; effort: string; label: string };
  capabilities: Capabilities;
};
type SearchConfig = {
  mode: "SEARXNG" | "AI_DIRECT"; searxng_url: string; provider: ProviderChoice | null;
  model?: string; effort: string; capabilities: Capabilities; max_offer_age_days: number;
};
type StageConfig = { provider: "default" | ProviderChoice; model?: string; effort: string; capabilities?: Partial<Capabilities> };
type PipelineConfig = {
  overrides: Record<"screening" | "deep_analysis" | "company_analysis" | "application_preparation", StageConfig>;
  ai_fallback: ProviderChoice | null;
};

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) {
    const data = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = data.detail;
    const message = typeof detail === "string" ? detail : Array.isArray(detail)
      ? detail.map(item => typeof item === "string" ? item : item?.msg).filter(item => typeof item === "string").join("; ")
      : typeof detail?.message === "string" ? detail.message : "";
    throw new Error(message || (detail && typeof detail === "object" ? JSON.stringify(detail) : "") || response.statusText || `HTTP ${response.status}`);
  }
  return response.json();
}

function date(value?: string) {
  return value ? new Intl.DateTimeFormat("en-US", { dateStyle: "short", timeStyle: value.includes("T") ? "short" : undefined }).format(new Date(value)) : "—";
}

function conciseError(value?: string) {
  if (!value) return "";
  if (value.includes("token_expired") || value.includes("401 Unauthorized")) return "ChatGPT session expired. Sign in again.";
  return value.length > 180 ? `${value.slice(0, 177)}…` : value;
}

function metric(value?: number | null) {
  return value ?? "—";
}

function queryFunnel(value?: string | null) {
  try {
    const rows = JSON.parse(value || "[]") as { raw_result_count?: number | null; admitted_result_count?: number | null }[];
    return rows.map((row, index) => `Q${index + 1} ${metric(row.raw_result_count)}/${metric(row.admitted_result_count)}`).join(" · ");
  } catch {
    return "";
  }
}

function cvLabel(application: Application) {
  if (application.cv_action === "ADAPT") return `Tailored resume · Base: Variant ${application.cv_variant || "—"}`;
  if (application.cv_action === "CREATE") return "Resume: new tailored resume";
  return application.cv_variant ? `Resume: Variant ${application.cv_variant}` : "Resume: —";
}

function downloadFilename(response: Response, fallback: string) {
  return response.headers.get("Content-Disposition")?.match(/filename="?([^";]+)"?/)?.[1] || fallback;
}

function ItemList({ items }: { items?: string[] }) {
  return items?.length ? <ul>{items.map(item => <li key={item}>{item}</li>)}</ul> : <>—</>;
}

export default function App() {
  const location = useLocation(); const navigate = useNavigate();
  const [applications, setApplications] = useState<Application[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [selected, setSelected] = useState<Application | null>(null);
  const [error, setError] = useState("");
  const [ai, setAi] = useState<AiConfig | null>(null);
  const [models, setModels] = useState<CodexModel[]>([]); const [model, setModel] = useState(""); const [effort, setEffort] = useState("medium");
  const [lastDiscovery, setLastDiscovery] = useState<DiscoveryRun | null>(null); const [searching, setSearching] = useState(false);
  const [searchProfile, setSearchProfile] = useState<SearchProfile | null>(null);
  const [searchConfig, setSearchConfig] = useState<SearchConfig | null>(null);
  const [pipelineConfig, setPipelineConfig] = useState<PipelineConfig | null>(null);
  const [preparing, setPreparing] = useState<Set<number>>(new Set());
  const [routeError, setRouteError] = useState("");
  const preparingRef = useRef(new Set<number>());
  const applicationMatch = matchPath("/applications/:id/*", location.pathname);
  const routeApplicationId = applicationMatch && applicationMatch.params.id !== "new" ? Number(applicationMatch.params.id) : null;

  const refresh = useCallback(async () => {
    try {
      const [items, summary] = await Promise.all([request<Application[]>("/applications"), request<Stats>("/stats")]);
      setApplications(items); setStats(summary);
      setSelected(current => current ? { ...current, ...items.find(item => item.id === current.id) } : current);
      setError("");
    } catch (caught) { setError((caught as Error).message); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (routeApplicationId === null) return;
    if (!Number.isInteger(routeApplicationId) || routeApplicationId < 1) { setSelected(null); setRouteError("Application not found."); return; }
    const controller = new AbortController();
    setRouteError("");
    request<Application>(`/applications/${routeApplicationId}`, { signal: controller.signal })
      .then(application => { if (!controller.signal.aborted) { setSelected(application); setRouteError(""); } })
      .catch(caught => { if (!controller.signal.aborted) { setSelected(null); setRouteError((caught as Error).message); } });
    return () => controller.abort();
  }, [routeApplicationId]);
  useEffect(() => {
    if (!applications.some(item => item.status === "SHORTLISTED" && ["queued", "running"].includes(item.preparation_state || ""))) return;
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [applications, refresh]);

  const refreshAI = useCallback(async () => {
    try {
      const [config, latest, profile, searchSettings, pipelineSettings] = await Promise.all([
        request<AiConfig>("/ai/config"), request<DiscoveryRun | null>("/codex/discovery/latest"),
        request<SearchProfile>("/settings/search-profile"),
        request<SearchConfig>("/search/config"), request<PipelineConfig>("/ai/pipeline"),
      ]);
      setAi(config); setLastDiscovery(latest); setSearchProfile(profile);
      setSearchConfig(searchSettings); setPipelineConfig(pipelineSettings);
      if (config.active) {
        const data = await request<{ models: CodexModel[]; selected?: string; selectedEffort?: string }>("/ai/models");
        setModels(data.models.filter(item => !item.hidden)); setModel(data.selected || ""); setEffort(data.selectedEffort || "medium");
      } else { setModels([]); setModel(""); setEffort("medium"); }
    } catch (caught) { setError((caught as Error).message); }
  }, []);
  useEffect(() => { void refreshAI(); }, [refreshAI]);
  const discoveryRunning = lastDiscovery?.status === "RUNNING";
  useEffect(() => {
    if (!searching && !discoveryRunning) return;
    let active = true;
    let pending = false;
    async function poll() {
      if (pending) return;
      pending = true;
      try {
        const latest = await request<DiscoveryRun | null>("/codex/discovery/latest");
        if (active && latest) {
          setLastDiscovery(latest);
          if (!searching && latest.status !== "RUNNING") void refresh();
        }
      } catch { /* Keep the last persisted snapshot when polling fails. */ }
      finally { pending = false; }
    }
    void poll();
    const timer = window.setInterval(() => void poll(), 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [searching, discoveryRunning, refresh]);

  async function connectChatGPT() {
    try {
      const { authorization_url } = await request<{ authorization_url: string }>("/auth/chatgpt/start");
      window.open(authorization_url, "chatgpt-oauth", "popup,width=600,height=760");
      const timer = window.setInterval(async () => {
        try {
          const status = await request<AuthStatus>("/auth/chatgpt/status");
          if (status.connected) { window.clearInterval(timer); await refreshAI(); }
        } catch (caught) { setError((caught as Error).message); }
      }, 1500);
      window.setTimeout(() => window.clearInterval(timer), 120000);
    } catch (caught) { setError((caught as Error).message); }
  }
  async function disconnectChatGPT() {
    try { await request("/auth/chatgpt/logout", { method: "POST" }); await refreshAI(); }
    catch (caught) { setError((caught as Error).message); }
  }
  async function chooseAISettings(providerId: ProviderId, authMode: AuthMode, nextModel: string, nextEffort: string, confirmApiBilling = false) {
    try {
      await request("/ai/active", { method: "PUT", body: JSON.stringify({ provider_id: providerId, auth_mode: authMode, model: nextModel, effort: nextEffort, confirm_api_billing: confirmApiBilling }) });
      setModel(nextModel); setEffort(nextEffort); setError(""); await refreshAI();
    } catch (caught) { setError((caught as Error).message); }
  }
  function chooseModel(value: string) {
    if (!value || !ai?.active) return;
    const supported = models.find(item => item.model === value)?.reasoningEfforts || ["medium"];
    const nextEffort = supported.includes(effort) ? effort : supported.includes("medium") ? "medium" : supported[0];
    void chooseAISettings(ai.active.provider_id, ai.active.auth_mode, value, nextEffort);
  }
  async function activate(choice: ActiveChoice) {
    try {
      if (!choice) { await request("/ai/active", { method: "DELETE" }); await refreshAI(); return; }
      const authMode: AuthMode = choice === "chatgpt_oauth" ? "chatgpt_oauth" : "api_key";
      const providerId: ProviderId = choice === "chatgpt_oauth" ? "openai" : choice;
      if (authMode === "api_key" && !ai?.api_keys[providerId].configured) throw new Error("API key absente");
      const confirmApi = authMode !== "api_key" || ai?.api_keys[providerId].billing_confirmed || window.confirm("This mode uses the provider API and may incur charges separate from your subscription.\n\nUse this API?");
      if (!confirmApi) return;
      const data = await request<{ models: CodexModel[] }>(`/ai/models?provider_id=${providerId}&auth_mode=${authMode}`);
      const available = data.models.filter(item => !item.hidden);
      const nextModel = available.find(item => item.model === model)?.model || available[0]?.model;
      if (!nextModel) throw new Error("Model unavailable");
      setModels(available);
      await chooseAISettings(providerId, authMode, nextModel, "medium", authMode === "api_key" && !ai?.api_keys[providerId].billing_confirmed);
    } catch (caught) { setError((caught as Error).message); }
  }
  async function searchOffers() {
    setSearching(true);
    try { await request("/codex/discover", { method: "POST" }); await Promise.all([refresh(), refreshAI()]); }
    catch (caught) {
      setError((caught as Error).message);
      await request<DiscoveryRun | null>("/codex/discovery/latest")
        .then(latest => { if (latest) setLastDiscovery(latest); })
        .catch(() => undefined);
    } finally { setSearching(false); }
  }

  async function saveSearchProfile(mode: SearchProfile["mode"], custom_search_prompt: string, knowledge_base_selected_files?: string[], knowledge_base_root?: string) {
    const profile = await request<SearchProfile>("/settings/search-profile", {
      method: "PUT", body: JSON.stringify({ mode, custom_search_prompt, knowledge_base_selected_files, knowledge_base_root }),
    });
    setSearchProfile(profile); setError("");
  }

  async function clearSearchPrompt() {
    setSearchProfile(await request<SearchProfile>("/settings/search-profile/prompt", { method: "DELETE" }));
  }

  async function prepareApplication(application: Application, retry = false) {
    if (preparingRef.current.has(application.id)) return;
    preparingRef.current.add(application.id); setPreparing(new Set(preparingRef.current));
    const optimistic = { ...application, status: "SHORTLISTED", preparation_state: "running" as const, preparation_error: undefined };
    setApplications(current => current.map(item => item.id === application.id ? optimistic : item));
    setSelected(current => current?.id === application.id ? { ...current, ...optimistic } : current);
    try {
      const path = retry ? `/applications/${application.id}/prepare-with-codex` : `/applications/${application.id}/select`;
      const updated = await request<Application>(path, { method: "POST" });
      setApplications(current => current.map(item => item.id === updated.id ? updated : item));
    } catch (caught) { setError((caught as Error).message); }
    finally {
      preparingRef.current.delete(application.id); setPreparing(new Set(preparingRef.current)); await refresh();
      try {
        const detail = await request<Application>(`/applications/${application.id}`);
        setSelected(current => current?.id === application.id ? detail : current);
      } catch (caught) { setError((caught as Error).message); }
    }
  }

  async function ignoreApplication(id: number) {
    await mutate(`/applications/${id}/ignore`, { details: "Ignored by user" });
  }

  async function restoreApplication(application: Application) {
    try {
      const restored = await request<Application>(`/applications/${application.id}/restore`, { method: "POST" });
      setApplications(current => current.map(item => item.id === restored.id ? restored : item));
      await refresh();
    } catch (caught) { setError((caught as Error).message); }
  }

  function openDetail(id: number) { navigate(`/applications/${id}`); }

  function openDocument(application: Application, kind: DocumentKind | "cv") {
    setSelected(application);
    navigate(`/applications/${application.id}/${kind}`);
  }

  function goBack(fallback: string) {
    const index = (window.history.state as { idx?: number } | null)?.idx;
    if (typeof index === "number" && index > 0) navigate(-1);
    else navigate(fallback);
  }

  async function mutate(path: string, body?: unknown) {
    try {
      const updated = await request<Application>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
      setSelected(await request<Application>(`/applications/${updated.id}`)); await refresh(); return updated;
    } catch (caught) { setError((caught as Error).message); return null; }
  }

  async function saveApplication(id: number, body: unknown) {
    const updated = await request<Application>(`/applications/${id}`, { method: "PATCH", body: JSON.stringify(body) });
    setSelected(current => current?.id === id ? { ...current, ...updated } : current);
    setApplications(current => current.map(item => item.id === id ? updated : item));
    void refresh();
    return updated;
  }

  const routeApplication = selected?.id === routeApplicationId ? selected : null;
  const preparationOverride = pipelineConfig?.overrides.application_preparation;
  const preparationStructured = preparationOverride?.provider === "default" || !preparationOverride
    ? ai?.capabilities.structured_output : preparationOverride.capabilities?.structured_output;
  const prepareDisabledReason = (!searchProfile || searchProfile.candidate_profile_available === false)
    ? "A Knowledge Base or candidate profile is required to prepare an application."
    : !preparationStructured ? "The preparation provider does not support Structured Output." : "";
  const applicationPage = (content: (application: Application) => ReactNode) =>
    routeError ? <NotFound message={routeError} /> : routeApplication ? content(routeApplication) : <p>Loading application…</p>;

  return <div className="layout">
    <aside>
      <h1>AI Job Application Workbench</h1>
      {([['/', 'Dashboard'], ['/applications', 'Applications'], ['/kanban', 'Kanban'], ['/applications/new', 'New job'], ['/excluded', 'Ignored jobs']] as const).map(([path, label]) =>
        <NavLink to={path} key={path} className={() => location.pathname === path || (path === "/applications" && /^\/applications\/\d/.test(location.pathname)) ? "active" : ""}>{label}</NavLink>)}
    </aside>
    <main>
      {error && <div className="error">{error}<button onClick={() => setError("")}>×</button></div>}
      <Routes>
        <Route path="/" element={stats && ai && searchProfile && searchConfig && pipelineConfig ? <Dashboard stats={stats} open={openDetail} auth={ai.chatgpt} ai={ai} models={models} model={model} effort={effort} last={lastDiscovery} profile={searchProfile} searchConfig={searchConfig} pipeline={pipelineConfig} searching={searching} connect={connectChatGPT} disconnect={disconnectChatGPT} refreshAI={refreshAI} saveProfile={saveSearchProfile} clearPrompt={clearSearchPrompt} activate={activate} chooseModel={chooseModel} chooseEffort={value => ai.active && void chooseAISettings(ai.active.provider_id, ai.active.auth_mode, model, value)} search={searchOffers} reportError={setError} /> : <p>Loading…</p>} />
        <Route path="/applications" element={<ApplicationList applications={applications} open={openDetail} />} />
        <Route path="/kanban" element={<Kanban applications={applications} open={openDetail} openDocument={openDocument} preparing={preparing} prepare={prepareApplication} ignore={ignoreApplication} mutate={mutate} canPrepare={!prepareDisabledReason} prepareDisabledReason={prepareDisabledReason} />} />
        <Route path="/applications/new" element={<NewApplication done={async id => { await refresh(); openDetail(id); }} />} />
        <Route path="/excluded" element={<DiscardedOffers applications={applications.filter(item => item.status === "IGNORED")} restore={restoreApplication} />} />
        <Route path="/applications/:id" element={applicationPage(application => <Detail application={application} openDocument={openDocument} mutate={mutate} save={saveApplication} prepare={prepareApplication} preparing={preparing.has(application.id)} canPrepare={!prepareDisabledReason} prepareDisabledReason={prepareDisabledReason} back={() => goBack("/applications")} />)} />
        {(["analysis", "company", "offer", "interview-prep"] as DocumentKind[]).map(kind => <Route key={kind} path={`/applications/:id/${kind}`} element={applicationPage(application => <MarkdownViewer application={application} kind={kind} back={() => goBack(`/applications/${application.id}`)} />)} />)}
        <Route path="/applications/:id/cover-letter" element={applicationPage(application => <CoverLetterViewer application={application} back={() => goBack(`/applications/${application.id}`)} />)} />
        <Route path="/applications/:id/cv" element={applicationPage(application => <CvViewer application={application} back={() => goBack(`/applications/${application.id}`)} />)} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </main>
  </div>;
}

function NotFound({ message = "Page introuvable." }: { message?: string }) {
  return <section><h2>Not found</h2><p className="error">{message}</p><NavLink to="/applications">View applications</NavLink></section>;
}

type KnowledgeBaseFile = SearchProfile["knowledge_base_files"][number];

function KnowledgeBaseTree({ files, selected, filter, onSelect, prefix = "" }: { files: KnowledgeBaseFile[]; selected: string[]; filter: string; onSelect: (paths: string[]) => void; prefix?: string }) {
  const visible = files.filter(file => `${file.label} ${file.path}`.toLocaleLowerCase().includes(filter.toLocaleLowerCase()));
  const folders = [...new Set(visible.map(file => file.path.slice(prefix.length).split("/")).filter(parts => parts.length > 1).map(parts => parts[0]))].sort();
  return <>
    {folders.map(folder => <KnowledgeBaseFolder key={folder} name={folder} files={files.filter(file => file.path.startsWith(`${prefix}${folder}/`))} prefix={`${prefix}${folder}/`} selected={selected} filter={filter} onSelect={onSelect} />)}
    {visible.filter(file => !file.path.slice(prefix.length).includes("/")).map(file => <label key={file.path}><input type="checkbox" checked={selected.includes(file.path)} onChange={event => onSelect(event.target.checked ? [...selected, file.path] : selected.filter(path => path !== file.path))} /><span>{file.label}</span></label>)}
  </>;
}

function KnowledgeBaseFolder({ name, files, prefix, selected, filter, onSelect }: { name: string; files: KnowledgeBaseFile[]; prefix: string; selected: string[]; filter: string; onSelect: (paths: string[]) => void }) {
  const [expanded, setExpanded] = useState(true);
  const allSelected = files.every(file => selected.includes(file.path));
  return <details className="knowledge-base-folder" open={Boolean(filter) || expanded} onToggle={event => { if (!filter) setExpanded(event.currentTarget.open); }}>
    <summary>{name}<button type="button" onClick={event => {
      event.preventDefault(); event.stopPropagation();
      onSelect(allSelected ? selected.filter(path => !files.some(file => file.path === path)) : [...new Set([...selected, ...files.map(file => file.path)])]);
    }}>{allSelected ? "Deselect all" : "Select all"}</button></summary>
    <div><KnowledgeBaseTree files={files} prefix={prefix} selected={selected} filter={filter} onSelect={onSelect} /></div>
  </details>;
}

function Dashboard({ stats, open, auth, ai, models, model, effort, last, profile, searchConfig, pipeline, searching, connect, disconnect, refreshAI, saveProfile, clearPrompt, activate, chooseModel, chooseEffort, search, reportError }: { stats: Stats; open: (id: number) => void; auth: AuthStatus; ai: AiConfig; models: CodexModel[]; model: string; effort: string; last: DiscoveryRun | null; profile: SearchProfile; searchConfig: SearchConfig; pipeline: PipelineConfig; searching: boolean; connect: () => void; disconnect: () => void; refreshAI: () => Promise<void>; saveProfile: (mode: SearchProfile["mode"], prompt: string, sources?: string[], root?: string) => Promise<void>; clearPrompt: () => Promise<void>; activate: (mode: ActiveChoice) => Promise<void>; chooseModel: (value: string) => void; chooseEffort: (value: string) => void; search: () => Promise<void>; reportError: (message: string) => void }) {
  const cards = [["Search", stats.phases.search], ["Ignored", stats.phases.ignored], ["Review", stats.phases.validation], ["Submit", stats.phases.send], ["Track", stats.phases.tracking], ["Rejected", stats.phases.rejected]];
  const efforts = models.find(item => item.model === model)?.reasoningEfforts || ["medium"];
  const [apiProvider, setApiProvider] = useState<ProviderId>("openai");
  const [apiKey, setApiKey] = useState(""); const [keyStatus, setKeyStatus] = useState("");
  const [testingApiKey, setTestingApiKey] = useState(false);
  const [configPanel, setConfigPanel] = useState<ConfigPanel>(() => auth.connected || !Object.values(ai.api_keys).some(key => key.configured) ? "account" : "api");
  const [profileMode, setProfileMode] = useState(profile.mode);
  const [kbProfile, setKbProfile] = useState(profile);
  useEffect(() => { setKbProfile(profile); }, [profile]);
  useEffect(() => {
    if (profileMode !== "knowledge_base" || profile.mode === "knowledge_base") return;
    const controller = new AbortController();
    request<SearchProfile>("/settings/search-profile?mode=knowledge_base", { signal: controller.signal })
      .then(setKbProfile).catch(caught => { if (!controller.signal.aborted) reportError((caught as Error).message); });
    return () => controller.abort();
  }, [profileMode, profile.mode]);
  const [customPrompt, setCustomPrompt] = useState(profile.custom_search_prompt);
  const [profileStatus, setProfileStatus] = useState("");
  const [selectedSources, setSelectedSources] = useState(profile.knowledge_base_selected_files);
  const [sourceFilter, setSourceFilter] = useState("");
  const [kbRoot, setKbRoot] = useState(profile.knowledge_base_root || "");
  const [savingKbRoot, setSavingKbRoot] = useState(false);
  useEffect(() => { setKbRoot(profile.knowledge_base_root || ""); }, [profile.knowledge_base_root]);
  async function changeKbRoot() {
    setSavingKbRoot(true);
    try { await saveProfile("knowledge_base", profile.custom_search_prompt, undefined, kbRoot); setProfileStatus("Knowledge Base folder saved"); }
    catch (caught) { reportError((caught as Error).message); }
    finally { setSavingKbRoot(false); }
  }
  const [searchSettings, setSearchSettings] = useState(searchConfig);
  const [maxOfferAgeDays, setMaxOfferAgeDays] = useState(String(searchConfig.max_offer_age_days));
  const savedMaxOfferAgeDays = useRef(searchConfig.max_offer_age_days);
  const ageSave = useRef<Promise<void> | null>(null);
  const [savingOfferAge, setSavingOfferAge] = useState(false);
  const [startingSearch, setStartingSearch] = useState(false);
  const [searchStatus, setSearchStatus] = useState<{ kind: "success" | "error" | "info"; message: string } | null>(null);
  const searchAction = useRef(0);
  const searchProvider = useRef(searchConfig.provider);
  const [testingSearch, setTestingSearch] = useState(false);
  const [searchModelCatalog, setSearchModelCatalog] = useState<{ provider: ProviderChoice; models: CodexModel[] } | null>(null);
  const searchModels = searchModelCatalog?.provider === searchSettings.provider ? searchModelCatalog.models : [];
  const [pipelineSettings, setPipelineSettings] = useState(pipeline);
  const [stageModels, setStageModels] = useState<Partial<Record<ProviderChoice, CodexModel[]>>>({});
  const stageModelActions = useRef<Partial<Record<ProviderChoice, number>>>({});
  const stageActions = useRef<Partial<Record<keyof PipelineConfig["overrides"], number>>>({});
  useEffect(() => { setProfileMode(profile.mode); setCustomPrompt(profile.custom_search_prompt); setSelectedSources(profile.knowledge_base_selected_files); }, [profile]);
  useEffect(() => { searchProvider.current = searchConfig.provider; setSearchSettings(searchConfig); }, [searchConfig]);
  useEffect(() => {
    if (searchSettings.mode === "AI_DIRECT" && searchSettings.provider) {
      void loadSearchModels(searchSettings.provider);
    }
  }, [searchSettings.mode, searchSettings.provider]);
  useEffect(() => {
    if (searchStatus?.kind !== "success") return;
    const timeout = window.setTimeout(() => setSearchStatus(null), 3000);
    return () => window.clearTimeout(timeout);
  }, [searchStatus]);
  useEffect(() => { setPipelineSettings(pipeline); }, [pipeline]);
  async function keyAction(path: string, method: "POST" | "PUT") {
    if (method === "POST") setTestingApiKey(true);
    try {
      await request(path, { method, body: JSON.stringify({ api_key: apiKey }) });
      setKeyStatus(method === "POST" ? "Connection successful" : "Key saved");
      if (method === "PUT") { setApiKey(""); await refreshAI(); }
    } catch (caught) { reportError((caught as Error).message); }
    finally { if (method === "POST") setTestingApiKey(false); }
  }
  async function deleteKey() {
    try { await request(`/ai/api-keys/${apiProvider}`, { method: "DELETE" }); setApiKey(""); setKeyStatus("Key deleted"); await refreshAI(); }
    catch (caught) { reportError((caught as Error).message); }
  }
  function showConfigPanel(panel: ConfigPanel) {
    setApiKey(""); setKeyStatus(""); setConfigPanel(panel);
  }
  async function persistProfile() {
    try { await saveProfile(profileMode, customPrompt, profileMode === "knowledge_base" ? selectedSources : undefined); setProfileStatus("Profile saved"); }
    catch (caught) { reportError((caught as Error).message); }
  }
  async function erasePrompt() {
    if (!customPrompt || !window.confirm("Clear prompt de recherche ?")) return;
    try { await clearPrompt(); setCustomPrompt(""); setProfileStatus("Prompt cleared"); }
    catch (caught) { reportError((caught as Error).message); }
  }
  function saveOfferAge(): Promise<void> {
    if (ageSave.current) return ageSave.current;
    const value = Number(maxOfferAgeDays);
    if (!maxOfferAgeDays.trim() || !Number.isInteger(value) || value < 1 || value > 365) {
      reportError("Maximum age must be an integer between 1 and 365 days.");
      setMaxOfferAgeDays(String(savedMaxOfferAgeDays.current));
      return Promise.resolve();
    }
    if (value === savedMaxOfferAgeDays.current) return Promise.resolve();
    setSavingOfferAge(true);
    ageSave.current = request<SearchConfig>("/search/config", { method: "PATCH", body: JSON.stringify({ max_offer_age_days: value }) })
      .then(saved => {
        savedMaxOfferAgeDays.current = saved.max_offer_age_days;
        setMaxOfferAgeDays(String(saved.max_offer_age_days));
        setSearchSettings(current => ({ ...current, max_offer_age_days: saved.max_offer_age_days }));
        reportError("");
      })
      .catch(caught => { reportError((caught as Error).message); setMaxOfferAgeDays(String(savedMaxOfferAgeDays.current)); })
      .finally(() => { ageSave.current = null; setSavingOfferAge(false); });
    return ageSave.current;
  }
  async function startSearch() {
    if (startingSearch) return;
    setStartingSearch(true);
    try { await saveOfferAge(); await search(); }
    finally { setStartingSearch(false); }
  }
  function resetSearchStatus() {
    searchAction.current += 1;
    setSearchStatus(null); setTestingSearch(false);
    return searchAction.current;
  }
  function changeSearchSettings(values: Partial<SearchConfig>) {
    resetSearchStatus();
    setSearchSettings(current => ({ ...current, ...values }));
  }
  async function saveSearchSettings(test = false) {
    const action = resetSearchStatus();
    try {
      const provider = searchSettings.provider;
      const paid = searchSettings.mode === "AI_DIRECT" && provider !== null && provider !== "chatgpt_oauth";
      const confirmApi = !test && paid && !ai.api_keys[provider as ProviderId].billing_confirmed;
      if (confirmApi && !window.confirm("This search uses the provider API and may incur separate charges.\n\nActivate this mode?")) return;
      setTestingSearch(test);
      setSearchStatus({ kind: "info", message: test ? "Testing…" : "Saving…" });
      await request(test ? "/search/test" : "/search/config", { method: test ? "POST" : "PUT", body: JSON.stringify({ ...searchSettings, confirm_api_billing: confirmApi }) });
      if (action !== searchAction.current) return;
      setSearchStatus({ kind: "success", message: test ? searchSettings.mode === "AI_DIRECT" ? "Web search available" : "Connection successful" : "Settings saved" });
      if (!test) await refreshAI();
    } catch (caught) {
      if (action === searchAction.current) setSearchStatus({ kind: "error", message: (caught as Error).message });
    } finally { if (action === searchAction.current) setTestingSearch(false); }
  }
  async function loadSearchModels(provider: ProviderChoice, action = searchAction.current) {
    const authMode = provider === "chatgpt_oauth" ? "chatgpt_oauth" : "api_key";
    const providerId = provider === "chatgpt_oauth" ? "openai" : provider;
    try {
      const data = await request<{ models: CodexModel[] }>(`/ai/models?provider_id=${providerId}&auth_mode=${authMode}`);
      const available = data.models.filter(item => !item.hidden);
      if (provider !== searchProvider.current) return;
      setSearchModelCatalog({ provider, models: available });
      const selected = available.find(item => item.capabilities?.web_search) || available[0];
      const supported = selected?.reasoningEfforts || ["medium"];
      setSearchSettings(current => current.provider !== provider || current.model ? current : {
        ...current, model: selected?.model,
        effort: supported.includes("medium") ? "medium" : supported[0],
        capabilities: selected?.capabilities || current.capabilities,
      });
    } catch (caught) {
      if (action === searchAction.current) reportError((caught as Error).message);
    }
  }
  function changeSearchProvider(provider: ProviderChoice | "") {
    resetSearchStatus();
    searchProvider.current = provider || null;
    setSearchModelCatalog(null);
    setSearchSettings(current => ({ ...current, provider: provider || null, model: undefined,
      capabilities: { structured_output: false, web_search: false, reasoning_effort: false, streaming: false },
    }));
  }
  function changeSearchModel(model: string) {
    resetSearchStatus();
    const selected = searchModels.find(item => item.model === model);
    const supported = selected?.reasoningEfforts || ["medium"];
    setSearchSettings(current => ({
      ...current, model,
      effort: supported.includes(current.effort) ? current.effort : supported.includes("medium") ? "medium" : supported[0],
      capabilities: selected?.capabilities || current.capabilities,
    }));
  }
  async function savePipeline(next: PipelineConfig, stage?: keyof PipelineConfig["overrides"], action?: number) {
    try {
      const choices = [next.ai_fallback, ...Object.values(next.overrides).map(item => item.provider)];
      const needsConfirmation = choices.some(choice => choice && choice !== "default" && choice !== "chatgpt_oauth" && !ai.api_keys[choice as ProviderId].billing_confirmed);
      if (needsConfirmation && !window.confirm("This setting uses a paid API separate from your subscription.\n\nActivate this provider?")) return;
      const saved = await request<PipelineConfig>("/ai/pipeline", { method: "PUT", body: JSON.stringify({ ...next, confirm_api_billing: needsConfirmation }) });
      if (stage && stageActions.current[stage] !== action) return;
      setPipelineSettings(saved); await refreshAI();
    } catch (caught) { reportError((caught as Error).message); }
  }
  async function loadStageModels(provider: ProviderChoice) {
    if (stageModels[provider]?.length) return stageModels[provider] || [];
    const action = (stageModelActions.current[provider] || 0) + 1;
    stageModelActions.current[provider] = action;
    const authMode = provider === "chatgpt_oauth" ? "chatgpt_oauth" : "api_key";
    const providerId = provider === "chatgpt_oauth" ? "openai" : provider;
    const data = await request<{ models: CodexModel[] }>(`/ai/models?provider_id=${providerId}&auth_mode=${authMode}`);
    const available = data.models.filter(item => !item.hidden);
    if (stageModelActions.current[provider] === action) setStageModels(current => ({ ...current, [provider]: available }));
    return available;
  }
  async function changeStage(stage: keyof PipelineConfig["overrides"], provider: StageConfig["provider"]) {
    const action = (stageActions.current[stage] || 0) + 1;
    stageActions.current[stage] = action;
    let nextStage: StageConfig = { provider, effort: "medium" };
    if (provider !== "default") {
      try {
        const available = await loadStageModels(provider);
        if (stageActions.current[stage] !== action) return;
        if (!available[0]) throw new Error("Model unavailable");
        nextStage = { provider, model: available[0].model, effort: "medium", capabilities: available[0].capabilities };
      } catch (caught) { if (stageActions.current[stage] === action) reportError((caught as Error).message); return; }
    }
    await savePipeline({ ...pipelineSettings, overrides: { ...pipelineSettings.overrides, [stage]: nextStage } }, stage, action);
  }
  async function changeStageModel(stage: keyof PipelineConfig["overrides"], nextModel: string) {
    const current = pipelineSettings.overrides[stage];
    const item = current.provider === "default" ? undefined : stageModels[current.provider]?.find(candidate => candidate.model === nextModel);
    await savePipeline({ ...pipelineSettings, overrides: { ...pipelineSettings.overrides, [stage]: { ...current, model: nextModel, effort: item?.reasoningEfforts?.includes(current.effort) ? current.effort : "medium", capabilities: item?.capabilities } } });
  }
  const profileDirty = profileMode !== profile.mode || customPrompt !== profile.custom_search_prompt
    || (profileMode === "knowledge_base" && JSON.stringify(selectedSources) !== JSON.stringify(profile.knowledge_base_selected_files));
  const screening = pipelineSettings.overrides.screening;
  const searchModel = searchModels.find(item => item.model === searchSettings.model);
  const searchCapabilities = searchModel?.capabilities || searchSettings.capabilities;
  const searchEfforts = searchModel?.reasoningEfforts || [searchSettings.effort];
  const screeningReady = screening.provider === "default" ? Boolean(ai.active && model) : Boolean(screening.model && screening.capabilities?.structured_output);
  const directSearchUnavailable = searchSettings.mode === "AI_DIRECT" && (!searchSettings.provider || !searchSettings.model || !searchCapabilities.web_search);
  const searchDisabledReason = directSearchUnavailable ? "Direct web search is unavailable with this provider/model."
    : !screeningReady ? "Configure the screening AI provider and model first."
    : profileMode === "knowledge_base" && !selectedSources.length ? "Select at least one Knowledge Base source."
    : profileDirty ? "Save the search profile before starting a search."
    : profile.mode === "custom_prompt" && !profile.custom_search_prompt.trim() ? "Define your search profile first."
    : profile.mode === "knowledge_base" && !profile.knowledge_base_available ? "No usable Knowledge Base found." : "";
  const providerOptions: [StageConfig["provider"], string][] = [
    ["default", "Default settings"],
    ...(auth.connected ? [["chatgpt_oauth", "ChatGPT / Codex — abonnement"]] as [ProviderChoice, string][] : []),
    ...Object.entries(API_PROVIDER_LABELS).filter(([id]) => ai.api_keys[id as ProviderId].configured).map(([id, label]) => [id as ProviderId, `${label} — API`] as [ProviderId, string]),
  ];
  const stageLabels: Record<keyof PipelineConfig["overrides"], string> = {
    screening: "Job screening", deep_analysis: "Deep analysis",
    company_analysis: "Company analysis", application_preparation: "Application preparation",
  };
  return <><header><h2>Dashboard</h2><p>Priority actions and pipeline status.</p></header>
    <section className="panel ai-provider">
      <details className="config-section" open><summary>AI provider</summary>
      <div className="config-switch" aria-label="Configuration method">
        <button type="button" aria-pressed={configPanel === "account"} onClick={() => showConfigPanel("account")}>Account sign-in</button>
        <button type="button" aria-pressed={configPanel === "api"} onClick={() => showConfigPanel("api")}>API key</button>
      </div>
      {configPanel === "account" ?
        <div className="provider-row"><div><strong>ChatGPT</strong><p className="muted">Uses your ChatGPT / Codex connection.</p></div>
          {auth.connected ? <div><strong>Connected</strong><p>{auth.account || "Active account"}</p><button onClick={disconnect}>Disconnect</button></div> : <button className="primary" onClick={connect}>Continue with ChatGPT</button>}</div>
        : <div><p className="muted">API billing is separate from consumer subscriptions.</p>
          <div className="api-key-form"><label>Provider<select value={apiProvider} onChange={event => { setApiProvider(event.target.value as ProviderId); setApiKey(""); setKeyStatus(""); }}>{Object.entries(API_PROVIDER_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
            <label>API key<input type="password" autoComplete="off" value={apiKey} placeholder={ai.api_keys[apiProvider].configured ? "Key configured" : "API key"} onChange={event => { setApiKey(event.target.value); setKeyStatus(""); }} /></label></div>
          <div className="actions"><button disabled={apiKey.length < 20 || testingApiKey} onClick={() => void keyAction(`/ai/api-keys/${apiProvider}/test`, "POST")}>{testingApiKey ? "Testing…" : "Test"}</button><button disabled={apiKey.length < 20} onClick={() => void keyAction(`/ai/api-keys/${apiProvider}`, "PUT")}>{ai.api_keys[apiProvider].configured ? "Replace" : "Save"}</button>{ai.api_keys[apiProvider].configured && <button onClick={() => void deleteKey()}>Delete</button>}</div>
          {ai.api_keys[apiProvider].configured && <p>Key configured : {ai.api_keys[apiProvider].last_four ? `••••${ai.api_keys[apiProvider].last_four}` : "Yes"}</p>}{keyStatus && <p className="success">{keyStatus}</p>}</div>}
      </details>
      <details className="config-section" open><summary>Web search</summary>
      <div className="codex-settings search-settings"><label>Search mode<select value={searchSettings.mode} onChange={event => changeSearchSettings({ mode: event.target.value as SearchConfig["mode"] })}><option value="SEARXNG">SearXNG</option><option value="AI_DIRECT">AI provider</option></select></label>
        {searchSettings.mode === "SEARXNG" ? <label>URL SearXNG<input value={searchSettings.searxng_url} onChange={event => changeSearchSettings({ searxng_url: event.target.value })} /></label> : <><label>Provider<select value={searchSettings.provider || ""} onChange={event => void changeSearchProvider(event.target.value as ProviderChoice | "")}><option value="">Select</option>{providerOptions.filter(([id]) => id !== "default").map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label><label>Model<select value={searchSettings.model || ""} disabled={!searchSettings.provider} onFocus={() => searchSettings.provider && void loadSearchModels(searchSettings.provider)} onChange={event => changeSearchModel(event.target.value)}>{(searchModels.length ? searchModels : searchSettings.model ? [{ model: searchSettings.model, displayName: searchSettings.model }] : []).map(item => <option key={item.model} value={item.model}>{item.displayName || item.model}</option>)}</select></label>{searchSettings.model && searchCapabilities.reasoning_effort && <label>Effort<select value={searchSettings.effort} onChange={event => changeSearchSettings({ effort: event.target.value })}>{searchEfforts.map(value => <option key={value} value={value}>{EFFORT_LABELS[value] || value}</option>)}</select></label>}</>}
      </div>
      <div className="actions"><button disabled={testingSearch || directSearchUnavailable} onClick={() => void saveSearchSettings(true)}>{testingSearch ? "Testing…" : "Test"}</button><button disabled={directSearchUnavailable} onClick={() => void saveSearchSettings()}>Save</button></div>
      <p role="status" className={searchStatus?.kind === "success" ? "success" : searchStatus?.kind === "error" ? "failure-text" : "muted"}>{searchStatus?.message || ""}</p>
      </details>
      <details className="config-section" open><summary>Default settings</summary>
      {!ai.active && <p className="muted">No AI provider configured.</p>}
      <div className="codex-settings"><label>Mode<select value={ai.active ? (ai.active.auth_mode === "chatgpt_oauth" ? "chatgpt_oauth" : ai.active.provider_id) : ""} onChange={event => void activate(event.target.value as ActiveChoice)}><option value="">No active provider</option>{auth.connected && <option value="chatgpt_oauth">ChatGPT — subscription</option>}{Object.entries(API_PROVIDER_LABELS).filter(([id]) => ai.api_keys[id as ProviderId].configured).map(([id, label]) => <option key={id} value={id}>{label} — API</option>)}</select></label>
        <label>Model<select value={model} disabled={!ai.active} onChange={event => chooseModel(event.target.value)}><option value="">Select</option>{models.map(item => <option key={item.model} value={item.model}>{item.displayName || item.model}</option>)}</select></label>
        <label>Effort<select value={effort} disabled={!model || !ai.capabilities.reasoning_effort} onChange={event => chooseEffort(event.target.value)}>{efforts.map(value => <option key={value} value={value}>{EFFORT_LABELS[value]}</option>)}</select></label></div>
      <p><strong>Active provider :</strong> {ai.active?.label || "None"}</p>
      <p><strong>Web search :</strong> {searchSettings.mode === "SEARXNG" ? "SearXNG" : `${searchSettings.provider ? SEARCH_PROVIDER_LABELS[searchSettings.provider] : "AI provider"} — ${searchSettings.model ? SEARCH_MODEL_LABELS[searchSettings.model] || searchModel?.displayName || "Model" : "No model selected"}`}</p>
      <ul className="capabilities"><li>{ai.capabilities.structured_output ? "✓" : "✗"} Structured Output</li><li>{ai.capabilities.reasoning_effort ? "✓" : "✗"} Configurable reasoning</li><li>{ai.capabilities.streaming ? "✓" : "✗"} Streaming</li></ul>
      </details>
      <details className="config-section"><summary>Configure each stage</summary>
        <div className="pipeline-grid">{(Object.keys(stageLabels) as (keyof PipelineConfig["overrides"])[]).map(stage => {
          const value = pipelineSettings.overrides[stage]; const available = value.provider === "default" ? [] : stageModels[value.provider] || [];
          const reasoning = value.provider === "default" ? ai.capabilities.reasoning_effort : Boolean(value.capabilities?.reasoning_effort);
          const effortValues = available.find(item => item.model === value.model)?.reasoningEfforts || [value.effort || "medium"];
          return <div className="pipeline-row" key={stage}><label>{stageLabels[stage]}<select value={value.provider} onChange={event => void changeStage(stage, event.target.value as StageConfig["provider"])}>{providerOptions.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
            {value.provider !== "default" && <><label>Model<select value={value.model || ""} onFocus={() => void loadStageModels(value.provider as ProviderChoice).catch(caught => reportError((caught as Error).message))} onChange={event => void changeStageModel(stage, event.target.value)}>{(available.length ? available : [{ model: value.model || "", displayName: value.model }]).map(item => <option key={item.model} value={item.model}>{item.displayName || item.model}</option>)}</select></label><label>Effort<select value={value.effort} disabled={!reasoning} onChange={event => void savePipeline({ ...pipelineSettings, overrides: { ...pipelineSettings.overrides, [stage]: { ...value, effort: event.target.value } } })}>{effortValues.map(item => <option key={item} value={item}>{EFFORT_LABELS[item] || item}</option>)}</select></label></>}
          </div>;
        })}</div>
        <label>AI fallback<select value={pipelineSettings.ai_fallback || ""} onChange={event => void savePipeline({ ...pipelineSettings, ai_fallback: (event.target.value || null) as ProviderChoice | null })}><option value="">None</option>{providerOptions.filter(([id]) => id !== "default").map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
      </details>
    </section>
    <section className="panel search-profile"><h3>Search profile</h3>
      <div className="search-profile-source">
      <fieldset><legend>Profile source</legend>
        <div className="search-profile-options">
        <label><input type="radio" name="profile-mode" value="knowledge_base" checked={profileMode === "knowledge_base"} onChange={() => { setProfileMode("knowledge_base"); setProfileStatus(""); }} /> Knowledge Base</label>
        <label><input type="radio" name="profile-mode" value="custom_prompt" checked={profileMode === "custom_prompt"} onChange={() => { setProfileMode("custom_prompt"); setProfileStatus(""); }} /> Custom prompt</label>
        </div>
      </fieldset>
      {profileMode === "knowledge_base" && <>
      <div className="knowledge-base-root"><label>Knowledge Base folder<input value={kbRoot} onChange={event => setKbRoot(event.target.value)} disabled={savingKbRoot} /></label><button type="button" onClick={() => void changeKbRoot()} disabled={savingKbRoot || !kbRoot.trim() || kbRoot === profile.knowledge_base_root}>{savingKbRoot ? "Review…" : "Edit"}</button></div>
      {kbProfile.knowledge_base_available ? <><p className="success">Valid Knowledge Base</p><p className="muted">Sources used : {selectedSources.map(path => kbProfile.knowledge_base_files.find(file => file.path === path)?.label || path).join(", ") || "None"}</p></> : <p className="muted">No Knowledge Base found.</p>}
      </>}
      </div>
      {profileMode === "knowledge_base" && kbProfile.knowledge_base_available && <details className="config-section">
        <summary>Edit sources ({selectedSources.length} selected)</summary>
        <label>Filter files<input type="search" value={sourceFilter} onChange={event => setSourceFilter(event.target.value)} /></label>
        <fieldset className="knowledge-base-files"><legend>Knowledge Base files</legend>
          <KnowledgeBaseTree files={[...kbProfile.knowledge_base_files, ...selectedSources.filter(path => !kbProfile.knowledge_base_files.some(file => file.path === path)).map(path => ({ path, label: "Source unavailable" }))]}
            selected={selectedSources} filter={sourceFilter} onSelect={paths => { setSelectedSources(paths); setProfileStatus(""); }} />
        </fieldset>
      </details>}
      {profileMode === "custom_prompt" && <label>Search prompt<textarea rows={9} value={customPrompt} maxLength={12000} onChange={event => { setCustomPrompt(event.target.value); setProfileStatus(""); }} placeholder={SEARCH_PROFILE_EXAMPLE} /></label>}
      <div>
      <span className="search-profile-label">Search period</span>
      <div className="search-launch">
        <div className="search-launch-settings">
          <label className="offer-age">
            <input
              type="number"
              aria-label="Maximum age in days"
              min={1}
              max={365}
              step={1}
              value={maxOfferAgeDays}
              disabled={savingOfferAge}
              onChange={event => setMaxOfferAgeDays(event.target.value)}
              onBlur={() => void saveOfferAge()}
              onKeyDown={event => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  event.currentTarget.blur();
                }
              }}
            />
            {" "}days max
          </label>

          <button onClick={() => void persistProfile()}>
            Save
          </button>

          {customPrompt && (
            <button onClick={() => void erasePrompt()}>
              Clear prompt
            </button>
          )}

          {savingOfferAge && (
            <small className="muted" role="status">
              Saving…
            </small>
          )}
        </div>

        <button
          className="primary"
          disabled={
            Boolean(searchDisabledReason) ||
            searching ||
            startingSearch ||
            last?.status === "RUNNING"
          }
          title={searchDisabledReason}
          onClick={() => void startSearch()}
        >
          {searching || startingSearch || last?.status === "RUNNING"
            ? "Searching…"
            : "Search for new jobs"}
        </button>
      </div>
      </div>
      {profile.custom_search_prompt_updated_at && <p className="muted">Last edited : {date(profile.custom_search_prompt_updated_at)}</p>}
      {profileStatus && <p className="success">{profileStatus}</p>}
      {searchDisabledReason && <p className="muted">{searchDisabledReason}</p>}
      {last && <div className="config-section search-profile-last muted">
        <h4>Last search · {date(last.finished_at)}</h4>
        <div className="search-profile-metadata">
          <p>Profile : {last.profile_mode === "custom_prompt" ? "Custom prompt" : "Knowledge Base"}</p>
          <p>Retrieval : {last.search_mode || "SEARXNG"} / {last.search_provider || "searxng"}{last.search_model ? ` / ${last.search_model}` : ""}</p>
          <p>Screening : {last.provider_id ? API_PROVIDER_LABELS[last.provider_id] : "ChatGPT"} / {last.model}{last.effort ? ` (${last.effort})` : ""}</p>
        </div>
        <p>{metric(last.merged_count)} merged URLs · {metric(last.new_count)} new · {metric(last.duplicate_count)} already known · {metric(last.verified_closed)} expired jobs · {metric(last.verified_invalid)} invalid links · {metric(last.verification_unknown)} unknown checks · {metric(last.rejected_count)} rejected · {metric(last.search_call_count)} Search requests · <span className={last.status === "SUCCESS" ? "success" : undefined}>{last.status}</span>{last.error ? ` · ${conciseError(last.error)}` : ""}</p>
        <details className="muted"><summary>Search funnel</summary>
          <p>Search : {metric(last.search_call_count)} Search requests · {metric(last.raw_result_count)} raw{last.search_mode === "AI_DIRECT" && <> (AI output) · {metric(last.web_search_calls)} observed web tools · {metric(last.source_url_count)} unique observed web sources</>}</p>
          <p>Normalization : <span title="Valid URL occurrences before deduplication and limits.">{metric(last.extracted_url_count)} extracted URLs</span> · {metric(last.invalid_url_count)} invalid URLs · {metric(last.admitted_result_count)} admitted · {metric(last.merged_count)} unique · duplicates {metric(last.intra_query_duplicate_count)} intra/{metric(last.global_duplicate_count)} global · {metric(last.known_url_count)} known · {metric(last.unknown_url_count)} unknown</p>
          <p>Qualification : {metric(last.likely_job_detail_count)} likely jobs · {metric(last.unknown_candidate_count)} ambiguous · {metric(last.obvious_non_job_count)} non-jobs · {metric(last.candidate_count)} candidates · {metric(last.verified_open)} open jobs · {metric(last.verified_closed)} expired jobs · {metric(last.verified_invalid)} invalid links · {metric(last.verification_unknown)} unknown checks · {metric(last.rejected_count)} rejected</p>
          <p>{[last.fetch_attempted, last.fetch_succeeded, last.fetch_failed, last.snippet_fallback_count].every(value => value == null) ? "fetch —" : <>fetch {metric(last.fetch_attempted)} attempted/{metric(last.fetch_succeeded)} succeeded/{metric(last.fetch_failed)} failed/{metric(last.snippet_fallback_count)} snippets</>}</p>
          <p>AI: {metric(last.ai_batch_count)} batches · {metric(last.ai_input_count)} inputs · {metric(last.ai_decision_count)} decisions · {metric(last.ai_keep_count)} KEEP · {metric(last.ai_reject_count)} REJECT · {metric(last.ai_review_count)} REVIEW · {metric(last.ai_missing_decision_count)} initially missing decisions · {metric(last.ai_retry_count)} retry</p>
          <p>Result : {metric(last.open_or_unknown_count)} retained · {metric(last.inserted_count)} imported</p>
          {[last.search_input_tokens, last.search_output_tokens, last.search_total_tokens, last.ai_input_tokens, last.ai_output_tokens, last.ai_total_tokens].some(value => value != null) && <p>Observed tokens : Search {metric(last.search_input_tokens)}/{metric(last.search_output_tokens)}/{metric(last.search_total_tokens)} · Screening {metric(last.ai_input_tokens)}/{metric(last.ai_output_tokens)}/{metric(last.ai_total_tokens)} · Observed total {last.search_total_tokens != null && last.ai_total_tokens != null ? last.search_total_tokens + last.ai_total_tokens : "—"}</p>}
          {queryFunnel(last.per_query_stats) && <p>Per query : {queryFunnel(last.per_query_stats)}</p>}
        </details></div>}
    </section>
    <AiUsageCard refreshToken={stats} activity={searching || testingSearch} />
    <section className="cards">{cards.map(([label, value]) => <article key={String(label)}><strong>{value}</strong><span>{label}</span></article>)}</section>
    <ActionList title="Due today" items={stats.today} open={open} />
    <ActionList title="Suggested follow-ups after 10 days" items={stats.followups} open={open} />
    <ActionList title="Upcoming interviews" items={stats.upcoming_interviews} open={open} />
  </>;
}

function tokenCount(value: number | null | undefined) {
  if (value == null) return "Unknown";
  const divisor = value >= 1_000_000 ? 1_000_000 : value >= 1_000 ? 1_000 : 1;
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: divisor === 1 ? 0 : 1 }).format(value / divisor)}${divisor === 1_000_000 ? "M" : divisor === 1_000 ? "k" : ""}`;
}

export function AiUsageCard({ refreshToken, activity = false }: { refreshToken?: Stats; activity?: boolean }) {
  const [period, setPeriod] = useState<UsagePeriod>("7d");
  const [view, setView] = useState<"numbers" | "charts">("numbers");
  const [usage, setUsage] = useState<AiUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    request<AiUsage>(`/stats/ai-usage?period=${period}`, { signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) setUsage(data); })
      .catch(caught => { if (!controller.signal.aborted) setError(`Usage unavailable : ${conciseError((caught as Error).message)}`); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [period, refreshToken, activity]);
  const periods: [UsagePeriod, string][] = [["today", "Today"], ["7d", "7 days"], ["30d", "30 days"], ["all", "All"]];
  const metrics = usage ? [["Total", usage.total], ["Search", usage.search], ["Document generation", usage.documents]] as const : [];
  return <details className="panel ai-usage" open>
    <summary>AI usage</summary>
    <div className="ai-usage-controls">
      <div className="config-switch" role="group" aria-label="Usage view">
        <button type="button" aria-pressed={view === "numbers"} onClick={() => setView("numbers")}>Numbers</button>
        <button type="button" aria-pressed={view === "charts"} onClick={() => setView("charts")}>Charts</button>
      </div>
      <div className="config-switch" role="group" aria-label="Usage period">
        {periods.map(([value, label]) => <button type="button" key={value} aria-pressed={period === value} onClick={() => setPeriod(value)}>{label}</button>)}
      </div>
    </div>
    {period === "today" && <p className="muted ai-usage-note">Today and times use UTC.</p>}
    <div aria-live="polite" aria-busy={loading}>
      {loading ? <p className="muted">Loading usage…</p>
        : error ? <p className="error">{error}</p>
        : usage && (usage.total.calls === 0 ? <p className="muted">No AI usage recorded for this period.</p>
          : <>{view === "numbers" ? <>
            <div className="ai-usage-metrics">
              {metrics.map(([label, summary]) => <article key={label}><span>{label}</span><strong title={summary.total_tokens == null ? undefined : new Intl.NumberFormat("en-US").format(summary.total_tokens)}>{tokenCount(summary.total_tokens)}</strong><small>{summary.total_tokens == null ? "unknown usage" : summary.unknown_calls > 0 ? "known tokens" : "tokens"}</small></article>)}
              <article><span>AI calls</span><strong>{new Intl.NumberFormat("en-US").format(usage.total.calls)}</strong><small>in this period</small></article>
            </div>
            <div className="ai-usage-breakdown muted">{([["Search", usage.search], ["Documents", usage.documents]] as const).map(([label, summary]) => <div key={label}><b>{label}</b><dl><dt>Input</dt><dd>{tokenCount(summary.input_tokens)}</dd><dt>Output</dt><dd>{tokenCount(summary.output_tokens)}</dd><dt>Total</dt><dd>{tokenCount(summary.total_tokens)}</dd></dl></div>)}</div>
          </> : <AiUsageChart usage={usage} />}
          {usage.total.unknown_calls > 0 && <p className="muted ai-usage-note">{usage.total.unknown_calls} call{usage.total.unknown_calls > 1 ? "s" : ""} without usage data. Totals include known tokens only.</p>}
        </>)}
    </div>
  </details>;
}

function AiUsageChart({ usage }: { usage: AiUsage }) {
  const rows = usage.daily;
  const known = rows.flatMap(row => [row.search_tokens, row.document_tokens]).filter((value): value is number => value != null);
  if (!known.length) return <p className="muted">No known usage data for this chart.</p>;
  const left = 66; const right = 850; const top = 24; const bottom = 224;
  const maximum = Math.max(2, Math.ceil(Math.max(...known) / 2) * 2);
  const x = (index: number) => rows.length === 1 ? (left + right) / 2 : left + index * (right - left) / (rows.length - 1);
  const y = (value: number) => bottom - value / maximum * (bottom - top);
  const label = (value: string) => new Intl.DateTimeFormat("en-US", { timeZone: usage.timezone, ...(usage.granularity === "hour" ? { hour: "2-digit", minute: "2-digit" } : usage.granularity === "month" ? { month: "short", year: "numeric" } : { day: "2-digit", month: "2-digit" }) }).format(new Date(value));
  const series = [["search_tokens", "Search", "search"], ["document_tokens", "Document generation", "documents"]] as const;
  return <figure className="ai-usage-chart">
    <div className="ai-usage-legend">{series.map(([, name, category]) => <span key={category}><i className={category} aria-hidden="true" />{name}</span>)}</div>
    <svg viewBox="0 0 880 274" role="img" aria-labelledby="ai-usage-chart-title ai-usage-chart-description">
      <title id="ai-usage-chart-title">AI usage by {usage.granularity === "hour" ? "hour" : usage.granularity === "month" ? "month" : "day"}</title>
      <desc id="ai-usage-chart-description">Search and document generation tokens. Unknown usage is not plotted.</desc>
      <text x={left} y="13" className="chart-label">Tokens</text>
      {[0, 0.5, 1].map(fraction => <g key={fraction}><line x1={left} x2={right} y1={y(maximum * fraction)} y2={y(maximum * fraction)} className="chart-grid" /><text x={left - 10} y={y(maximum * fraction) + 4} textAnchor="end" className="chart-label">{tokenCount(maximum * fraction)}</text></g>)}
      {rows.map((row, index) => index === 0 || index === rows.length - 1 || index % Math.ceil(rows.length / 6) === 0 ? <text key={row.date} x={x(index)} y="247" textAnchor="middle" className="chart-label">{label(row.date)}</text> : null)}
      {series.map(([key, name, category]) => {
        let connected = false;
        const path = rows.map((row, index) => {
          const value = row[key];
          if (value == null) { connected = false; return ""; }
          const point = `${connected ? "L" : "M"}${x(index)},${y(value)}`;
          connected = true; return point;
        }).join(" ");
        return <g className={`chart-series ${category}`} key={key}><path d={path} />{rows.map((row, index) => row[key] == null ? null : <circle key={row.date} cx={x(index)} cy={y(row[key])} r="3"><title>{label(row.date)} · {name} : {new Intl.NumberFormat("en-US").format(row[key])} tokens</title></circle>)}</g>;
      })}
      <text x={(left + right) / 2} y="270" textAnchor="middle" className="chart-label">{usage.granularity === "hour" ? "Hours" : usage.granularity === "month" ? "Months" : "Dates"}</text>
    </svg>
  </figure>;
}

function ActionList({ title, items, open }: { title: string; items: Application[]; open: (id: number) => void }) {
  return <section><h3>{title}</h3>{items.length === 0 ? <p className="muted">No items.</p> : items.map(item =>
    <button className="row" key={item.id} onClick={() => open(item.id)}><span><b>{item.company}</b> — {item.position}</span><span>{item.next_action || item.status}</span></button>)}</section>;
}

export function ApplicationList({ applications, open }: { applications: Application[]; open: (id: number) => void }) {
  const [selectedStatusGroups, setSelectedStatusGroups] = useState<string[]>([]); const [company, setCompany] = useState(""); const [source, setSource] = useState(""); const [search, setSearch] = useState("");
  const [sort, setSort] = useState<{ column: "company" | "position" | "detected_at" | "status" | "cv_variant"; direction: "ascending" | "descending" }>();
  const [currentPage, setCurrentPage] = useState(1); const [displayMode, setDisplayMode] = useState<"10" | "all">("10");
  const columns = [["company", "Company"], ["position", "Position"], ["detected_at", "Date"], ["status", "Status"], ["cv_variant", "CV"]] as const;
  const selectedStatuses = STATUS_FILTERS.filter(([label]) => selectedStatusGroups.includes(label)).flatMap(([, statuses]) => statuses);
  const filteredApplications = applications.filter(item => (selectedStatuses.length === 0 || selectedStatuses.includes(item.status)) && item.company.toLowerCase().includes(company.toLowerCase()) && item.source.toLowerCase().includes(source.toLowerCase()) && `${item.company} ${item.position}`.toLowerCase().includes(search.toLowerCase())).sort((left, right) => {
    if (!sort) return 0;
    const leftValue = sort.column === "detected_at" ? Date.parse(left.detected_at || "") : left[sort.column] || "";
    const rightValue = sort.column === "detected_at" ? Date.parse(right.detected_at || "") : right[sort.column] || "";
    const leftMissing = typeof leftValue === "number" ? Number.isNaN(leftValue) : !leftValue;
    const rightMissing = typeof rightValue === "number" ? Number.isNaN(rightValue) : !rightValue;
    if (leftMissing) return rightMissing ? 0 : 1;
    if (rightMissing) return -1;
    const result = typeof leftValue === "number" && typeof rightValue === "number" ? leftValue - rightValue : String(leftValue).localeCompare(String(rightValue), "en", { sensitivity: "base" });
    return sort.direction === "ascending" ? result : -result;
  });
  const total = filteredApplications.length; const totalPages = Math.ceil(total / PAGE_SIZE); const page = Math.min(currentPage, Math.max(totalPages, 1));
  const shown = displayMode === "all" ? filteredApplications : filteredApplications.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const numberedPages = totalPages <= 7 ? Array.from({ length: totalPages }, (_, index) => index + 1) : [...new Set([1, page - 1, page, page + 1, totalPages].filter(item => item >= 1 && item <= totalPages))].sort((left, right) => left - right);
  const pageItems = numberedPages.flatMap((item, index) => index > 0 && item - numberedPages[index - 1] > 1 ? ["…", item] : [item]);
  useEffect(() => { if (currentPage !== page) setCurrentPage(page); }, [currentPage, page]);
  function order(column: typeof columns[number][0]) { setSort(current => ({ column, direction: current?.column === column && current.direction === "ascending" ? "descending" : "ascending" })); }
  function toggleStatusGroup(label: string) { setCurrentPage(1); setSelectedStatusGroups(current => current.includes(label) ? current.filter(item => item !== label) : [...current, label]); }
  return <><header><h2>Applications</h2></header><div className="filters">
    <input placeholder="Search" value={search} onChange={event => { setSearch(event.target.value); setCurrentPage(1); }} />
    <input placeholder="Company" value={company} onChange={event => { setCompany(event.target.value); setCurrentPage(1); }} />
    <input placeholder="Source" value={source} onChange={event => { setSource(event.target.value); setCurrentPage(1); }} />
    <div className="status-filters">
      {STATUS_FILTERS.map(([label]) => <button key={label} className="status-chip" aria-pressed={selectedStatusGroups.includes(label)} onClick={() => toggleStatusGroup(label)}>{label}</button>)}
      {selectedStatusGroups.length > 0 && <button onClick={() => { setSelectedStatusGroups([]); setCurrentPage(1); }}>Clear</button>}
    </div>
  </div><div className="table-wrap"><table><thead><tr>{columns.map(([column, label]) => <th key={column} aria-sort={sort?.column === column ? sort.direction : "none"}><button className="sort-button" onClick={() => order(column)}>{label}{sort?.column === column && <span aria-hidden="true"> {sort.direction === "ascending" ? "↑" : "↓"}</span>}</button></th>)}<th>Channel</th><th>Next action</th></tr></thead>
    <tbody>{shown.map(item => <tr key={item.id} onClick={() => open(item.id)}><td>{item.company}</td><td>{item.position}</td><td>{date(item.detected_at)}</td><td><Badge status={item.status} /></td><td>{item.cv_variant || "—"}</td><td>{item.application_channel || "—"}</td><td>{item.next_action || "—"}</td></tr>)}</tbody></table>
    <div className="pagination-bar"><span>{displayMode === "all" || total === 0 ? `${total} job${total === 1 ? "" : "s"}` : `${(page - 1) * PAGE_SIZE + 1}–${Math.min(page * PAGE_SIZE, total)} of ${total} job${total === 1 ? "" : "s"}`}</span>
      <label>Show : <select value={displayMode} onChange={event => { setDisplayMode(event.target.value as "10" | "all"); setCurrentPage(1); }}><option value="10">10 per page</option><option value="all">Show all</option></select></label>
      {displayMode === "10" && totalPages > 1 && <nav className="pagination-controls" aria-label="Pagination"><button disabled={page === 1} onClick={() => setCurrentPage(page - 1)}>‹ Previous</button>{pageItems.map((item, index) => item === "…" ? <span key={`ellipsis-${index}`}>…</span> : <button key={item} aria-current={item === page ? "page" : undefined} onClick={() => setCurrentPage(item as number)}>{item}</button>)}<button disabled={page === totalPages} onClick={() => setCurrentPage(page + 1)}>Next ›</button></nav>}
    </div></div></>;
}

export function Kanban({ applications, open, openDocument, preparing, prepare, ignore, mutate, canPrepare, prepareDisabledReason = "" }: { applications: Application[]; open: (id: number) => void; openDocument: (application: Application, kind: DocumentKind | "cv") => void; preparing: Set<number>; prepare: (application: Application, retry?: boolean) => Promise<void>; ignore: (id: number) => Promise<void>; mutate: (path: string, body?: unknown) => Promise<Application | null>; canPrepare: boolean; prepareDisabledReason?: string }) {
  const columns = ["SEARCH", "REVIEW", "SUBMIT", "TRACK", "WITHDRAW"];
  const inColumn = (item: Application, column: string) => STATUS_GROUPS[column].includes(item.status);
  const run = (event: React.MouseEvent, action: () => void) => { event.stopPropagation(); action(); };
  return <><header><h2>Kanban</h2><p>Search, review, submission, tracking and withdrawal.</p></header><div className="kanban">{columns.map(column => <section key={column}><h3>{column}</h3>{applications.filter(item => inColumn(item, column)).map(item => {
    const busy = preparing.has(item.id) || item.preparation_state === "running";
    const ineligible = item.eligibility_status === "INELIGIBLE";
    return <article className="ticket" key={item.id} onClick={() => open(item.id)}>
      <b>{item.company}</b><span>{item.position}</span>
      <small>{item.preparation_state === "queued" ? "Queued for preparation" : busy ? "Preparing with AI…" : item.preparation_state === "failed" ? "Preparation failed" : item.status === "PREPARED" ? "Preparation complete" : item.status}</small>
      {item.eligibility_status && item.eligibility_status !== "ELIGIBLE" && <EligibilityNotice status={item.eligibility_status} reason={item.eligibility_reason} />}
      {item.status === "PREPARED" && <small>{cvLabel(item)}</small>}
      {item.preparation_error && !busy && <small className="failure-text">{item.preparation_error}</small>}
      {item.next_action && TRACKING.has(item.status) && <small>{item.next_action}{item.next_action_at ? ` · ${date(item.next_action_at)}` : ""}</small>}
      <div className="ticket-actions">
        {item.status === "DETECTED" && <><button className="primary" disabled={busy || !canPrepare || ineligible} title={ineligible ? item.eligibility_reason : !canPrepare ? prepareDisabledReason : ""} onClick={event => run(event, () => void prepare(item))}>Select</button>{item.url && <a className="button" href={item.url} target="_blank" rel="noopener noreferrer" onClick={event => event.stopPropagation()}>View job ↗</a>}<button onClick={event => run(event, () => void ignore(item.id))}>Ignore</button></>}
        {item.status === "SHORTLISTED" && item.preparation_state === "failed" && <button className="primary" disabled={!canPrepare} title={!canPrepare ? prepareDisabledReason : ""} onClick={event => run(event, () => void prepare(item, true))}>Retry</button>}
        {item.status === "PREPARED" && <><FileLink item={item} kind="analysis" label="Analysis" open={openDocument} /><FileLink item={item} kind="cv" label="CV" open={openDocument} /><FileLink item={item} kind="cover-letter" label="Cover letter" open={openDocument} /><button className="primary" disabled={item.artifacts?.complete === false} onClick={event => run(event, () => void mutate(`/applications/${item.id}/validate`))}>Approve</button><button onClick={event => run(event, () => void ignore(item.id))}>Withdraw</button></>}
        {["AWAITING_VALIDATION", "APPROVED", "SUBMITTING"].includes(item.status) && <><a className="button" href={item.url} target="_blank" rel="noopener noreferrer" onClick={event => event.stopPropagation()}>Open job</a><button className="primary" onClick={event => run(event, () => { if (window.confirm("Confirm that you have submitted this application?")) void mutate(`/applications/${item.id}/submission/confirm`); })}>Confirm submission</button></>}
      </div>
    </article>;
  })}</section>)}</div></>;
}

function DiscardedOffers({ applications, restore }: { applications: Application[]; restore: (application: Application) => Promise<void> }) {
  return <><header><h2>Ignored jobs</h2><p>Jobs ignored during SEARCH.</p></header>
    {applications.length === 0 ? <p className="muted">No ignored jobs.</p> : <div className="discarded-list">{applications.map(application => <article className="ticket" key={application.id}>
      <b>{application.company}</b><span>{application.position}</span>
      <a href={application.url} target="_blank" rel="noreferrer">{application.url}</a>
      {application.location && <small>{application.location}</small>}
      <small>Ignored on {date(application.ignored_at)}{application.source ? ` · ${application.source}` : ""}</small>
      <div className="ticket-actions"><button className="primary" onClick={() => void restore(application)}>Restore</button></div>
    </article>)}</div>}
  </>;
}

export function FileLink({ item, kind, label, open }: { item: Application; kind: DocumentKind | "cv"; label: string; open: (application: Application, kind: DocumentKind | "cv") => void }) {
  const field = `${kind.replaceAll("-", "_")}_path` as keyof Application;
  return item[field] ? <button onClick={event => { event.stopPropagation(); open(item, kind); }}>{label}</button> : null;
}

function NewApplication({ done }: { done: (id: number) => void }) {
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try { const created = await request<Application>("/applications", { method: "POST", body: JSON.stringify(Object.fromEntries(form)) }); done(created.id); }
    catch (caught) { setError((caught as Error).message); }
  }
  return <><header><h2>New job</h2><p>Manual entry. Initial status: DETECTED.</p></header>{error && <p className="error">{error}</p>}
    <form className="form" onSubmit={submit}><label>URL<input name="url" type="url" required /></label><label>Company<input name="company" required /></label><label>Position<input name="position" required /></label><label>Source<input name="source" /></label><label>Location<input name="location" /></label><label>Deadline<input name="deadline" type="date" /></label><label className="wide">Description<textarea name="description" rows={10} /></label><button type="submit">Create job</button></form></>;
}

function editable(application: Application) {
  return { company_postal_address: application.company_postal_address || "", application_channel: application.application_channel || "", next_action: application.next_action || "", next_action_at: application.next_action_at?.slice(0, 16) || "", interview_at: application.interview_at?.slice(0, 16) || "", interview_notes: application.interview_notes || "", notes: application.notes || "" };
}

export function Detail({ application, openDocument, mutate, save, prepare, preparing, canPrepare = true, prepareDisabledReason = "", back }: { application: Application; openDocument: (application: Application, kind: DocumentKind | "cv") => void; mutate: (path: string, body?: unknown) => Promise<Application | null>; save: (id: number, body: unknown) => Promise<Application>; prepare: (application: Application, retry?: boolean) => Promise<void>; preparing: boolean; canPrepare?: boolean; prepareDisabledReason?: string; back: () => void }) {
  const [files, setFiles] = useState<Record<string, string>>({});
  const [filesError, setFilesError] = useState("");
  const [edit, setEdit] = useState(() => editable(application)); const [saving, setSaving] = useState(false);
  const [saveResult, setSaveResult] = useState<"success" | "error" | null>(null); const [saveError, setSaveError] = useState("");
  const savingRef = useRef(false);
  const draft = useRef({ applicationId: application.id, revision: 0, dirty: false });
  useEffect(() => {
    const controller = new AbortController();
    setFiles({}); setFilesError("");
    for (const kind of ["offer", "analysis", "company", "interview-prep"]) {
      fetch(`${API}/applications/${application.id}/files/${kind}`, { signal: controller.signal })
        .then(response => response.ok ? response.text() : "")
        .then(text => { if (!controller.signal.aborted) setFiles(current => ({ ...current, [kind]: text })); })
        .catch(caught => { if (!controller.signal.aborted) setFilesError(`Application files indisponibles : ${(caught as Error).message}`); });
    }
    return () => controller.abort();
  }, [application.id, application.updated_at]);
  useEffect(() => {
    if (draft.current.applicationId !== application.id) {
      draft.current = { applicationId: application.id, revision: 0, dirty: false };
      setSaveResult(null); setSaveError("");
    }
    if (!draft.current.dirty) setEdit(editable(application));
  }, [application]);
  useEffect(() => {
    if (saveResult !== "success") return;
    const timeout = window.setTimeout(() => setSaveResult(null), 2500);
    return () => window.clearTimeout(timeout);
  }, [saveResult]);
  function change(values: Partial<typeof edit>) { draft.current.dirty = true; draft.current.revision += 1; setEdit(current => ({ ...current, ...values })); setSaveResult(null); setSaveError(""); }
  async function patch(event: FormEvent) {
    event.preventDefault();
    if (savingRef.current) return;
    const applicationId = application.id; const revision = draft.current.revision;
    savingRef.current = true; setSaving(true); setSaveResult(null); setSaveError("");
    try {
      const updated = await save(applicationId, edit);
      if (draft.current.applicationId === applicationId) {
        if (draft.current.revision === revision) { draft.current.dirty = false; setEdit(editable(updated)); }
        setSaveResult("success");
      }
    } catch (caught) { if (draft.current.applicationId === applicationId) { setSaveError((caught as Error).message); setSaveResult("error"); } }
    finally { savingRef.current = false; setSaving(false); }
  }
  async function status(target: string) { await mutate(`/applications/${application.id}/status`, { status: target }); }
  const busy = preparing || application.preparation_state === "running";
  const ineligible = application.eligibility_status === "INELIGIBLE";
  const interviewRelevant = INTERVIEW_STATUSES.has(application.status) || Boolean(application.interview_at);
  const closed = ["REJECTED", "WITHDRAWN", "NO_RESPONSE", "IGNORED"].includes(application.status);
  const cvFilename = application.cv_path?.split(/[\\/]/).pop() || "—";
  return <><header className="detail-head"><div><button className="link" onClick={back}>← Back</button><h2>{application.company}</h2><p>{application.position}</p></div><Badge status={application.status} /></header>
    <div className="detail-grid"><section><h3>Job</h3><dl><dt>URL</dt><dd><a href={application.url} target="_blank">{application.url}</a></dd><dt>Source</dt><dd>{application.source || "—"}</dd><dt>Location</dt><dd>{application.location || "—"}</dd><dt>Publication</dt><dd>{date(application.published_at)}</dd><dt>Contract</dt><dd>{application.contract_type || "—"}</dd><dt>Eligibility</dt><dd><EligibilityNotice status={application.eligibility_status} reason={application.eligibility_reason} /></dd><dt>Detected</dt><dd>{date(application.detected_at)}</dd><dt>Submitted</dt><dd>{date(application.sent_at)}</dd></dl>{application.why_relevant && <><h4>Relevance</h4><p>{application.why_relevant}</p></>}</section>
      <section><h3>Actions</h3><div className="actions">
        {application.status === "DETECTED" && <><button className="primary" disabled={busy || !canPrepare || ineligible} title={ineligible ? application.eligibility_reason : !canPrepare ? prepareDisabledReason : ""} onClick={() => void prepare(application)}>Select</button><button onClick={() => void mutate(`/applications/${application.id}/ignore`, { details: "Ignored by user" })}>Ignore</button></>}
        {application.status === "SHORTLISTED" && application.preparation_state === "queued" && <span>Queued for preparation</span>}
        {application.status === "SHORTLISTED" && application.preparation_state === "running" && <span>Preparing with AI…</span>}
        {application.status === "SHORTLISTED" && application.preparation_state === "failed" && <button className="primary" disabled={!canPrepare} title={!canPrepare ? prepareDisabledReason : ""} onClick={() => void prepare(application, true)}>Retry preparation</button>}
        {application.status === "PREPARED" && <><button className="primary" disabled={application.artifacts?.complete === false} onClick={() => void mutate(`/applications/${application.id}/validate`)}>Approve application</button><button onClick={() => void mutate(`/applications/${application.id}/ignore`, { details: "Application withdrawn by user" })}>Withdraw</button></>}
        {["AWAITING_VALIDATION", "APPROVED", "SUBMITTING"].includes(application.status) && <><a className="button" href={application.url} target="_blank" rel="noopener noreferrer">Open job</a><button className="primary" onClick={() => { if (window.confirm("Confirm that you have submitted this application?")) void mutate(`/applications/${application.id}/submission/confirm`); }}>Confirm submission</button></>}
        {(FOLLOW_UP[application.status] || []).map(([target, label]) => <button key={target} onClick={() => void status(target)}>{label}</button>)}
      </div>{application.preparation_error && !busy && <p className="error"><strong>Preparation failed.</strong> {application.preparation_error}</p>}{application.missing_information?.length ? <div className="warning"><strong>Missing information :</strong><ul>{application.missing_information.map(item => <li key={item.field}>{item.label}</li>)}</ul><span>These fields were left blank in the generated documents.</span></div> : null}{!["DETECTED", "SHORTLISTED", "WITHDRAWN", "IGNORED"].includes(application.status) && application.artifacts?.complete === false && <p className="error">Incomplete preparation : {[...application.artifacts.missing, ...application.artifacts.invalid].join(", ")}</p>}{application.status === "AWAITING_VALIDATION" && <p className="warning">Your application is ready. Submit it on the company website, then return here to confirm.</p>}</section></div>
    {application.status === "PREPARED" && <section className="panel"><h3>Preparation</h3><dl><dt>CV</dt><dd>{cvFilename}</dd><dt>Decision</dt><dd>{application.cv_action || "—"}</dd><dt>Source variant</dt><dd>{application.cv_variant || "—"}</dd>{application.cover_letter_word_count && <><dt>Cover letter</dt><dd>{application.cover_letter_word_count} words</dd></>}</dl>{application.cv_justification && <p>{application.cv_justification}</p>}<div className="actions"><FileLink item={application} kind="analysis" label="Open analysis" open={openDocument} /><FileLink item={application} kind="cv" label="Open resume" open={openDocument} /><FileLink item={application} kind="cover-letter" label="Open cover letter" open={openDocument} /><FileLink item={application} kind="interview-prep" label="Interview preparation" open={openDocument} /></div></section>}
    <section className="panel"><h3>Company</h3><dl><dt>Description</dt><dd>{application.company_description || "—"}</dd><dt>Address</dt><dd>{application.company_postal_address || <span className="warning">Verify company address</span>}</dd><dt>Relevant domain</dt><dd>{application.company_domain || "—"}</dd><dt>Relevant achievements</dt><dd><ItemList items={application.company_completed_achievements} /></dd><dt>Announced developments</dt><dd><ItemList items={application.company_planned_developments} /></dd><dt>Competitors / comparable organizations</dt><dd><ItemList items={application.company_comparable_actors} /></dd></dl></section>
    <form className="form panel" onSubmit={patch}><h3 className="wide">Track</h3><dl className="wide"><dt>Current status</dt><dd>{application.status}</dd>{application.sent_at && <><dt>Submission date</dt><dd>{date(application.sent_at)}</dd></>}<dt>Last updated</dt><dd>{date(application.updated_at)}</dd></dl><label className="wide">Company address<textarea value={edit.company_postal_address} onChange={event => change({ company_postal_address: event.target.value })} rows={3} /></label><label>Channel<input value={edit.application_channel} onChange={event => change({ application_channel: event.target.value })} /></label>{application.next_action && <label>Next action<input value={edit.next_action} readOnly /></label>}{application.next_action && <label>Next action date<input type="datetime-local" value={edit.next_action_at} onChange={event => change({ next_action_at: event.target.value })} /></label>}{interviewRelevant && <><label>Interview date<input type="datetime-local" value={edit.interview_at} onChange={event => change({ interview_at: event.target.value })} /></label><label className="wide">Interview notes<textarea value={edit.interview_notes} onChange={event => change({ interview_notes: event.target.value })} /></label></>}<label className="wide">General notes<textarea value={edit.notes} onChange={event => change({ notes: event.target.value })} /></label>{closed && <p className="wide muted">Application closed.</p>}<div className="save-actions"><button type="submit" disabled={saving} aria-busy={saving}>{saving ? "Saving…" : "Confirm"}</button><div className="save-feedback" aria-live="polite">{saveResult === "success" && <span className="success">✓ Changes saved</span>}{saveResult === "error" && <span className="failure"><strong>⚠ Save failed</strong><small>Unable to save changes.{saveError && ` ${saveError}`}</small></span>}</div></div></form>
    {application.status !== "PREPARED" && (application.analysis_path || application.cv_path) && <section><h3>Ready documents</h3>{application.cv_path && <p><strong>{cvLabel(application)}</strong>{application.cv_justification && <> — {application.cv_justification}</>}</p>}<div className="actions">{application.analysis_path && <FileLink item={application} kind="analysis" label="Open analysis" open={openDocument} />}{application.cv_path && <FileLink item={application} kind="cv" label="Open resume" open={openDocument} />}<FileLink item={application} kind="cover-letter" label="Open cover letter" open={openDocument} /></div></section>}
    <section><h3>Application files</h3>{filesError && <p className="error" role="alert">{filesError}</p>}<div className="documents">{Object.entries(files).filter(([, content]) => content).map(([kind, content]) => <details key={kind}><summary>{kind}.md</summary><div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" /> }}>{content}</ReactMarkdown></div></details>)}</div></section>
    <section><h3>Timeline</h3><ol className="timeline">{application.events?.map(event => <li key={event.id}><time>{date(event.created_at)}</time><b>{event.event_type}</b><span>{event.details}</span></li>)}</ol></section>
  </>;
}

export function MarkdownViewer({ application, kind, back }: { application: Application; kind: DocumentKind; back: () => void }) {
  const [content, setContent] = useState(""); const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setContent(""); setError("");
    fetch(`${API}/applications/${application.id}/files/${kind}`, { signal: controller.signal })
      .then(response => response.ok ? response.text() : Promise.reject(new Error("Document not found.")))
      .then(text => { if (!controller.signal.aborted) setContent(text); })
      .catch(caught => { if (!controller.signal.aborted) setError((caught as Error).message); });
    return () => controller.abort();
  }, [application.id, kind]);
  const labels: Record<DocumentKind, string> = { offer: "Job", analysis: "Analysis", company: "Company", "cover-letter": "Cover letter", "interview-prep": "Interview preparation" };
  return <><header className="viewer-toolbar"><button className="link" onClick={back}>← Back</button><h2>{labels[kind]} — {application.company}</h2></header>
    <section className="document-viewer">{error ? <p className="error">{error}</p> : content ? <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" /> }}>{content}</ReactMarkdown></div> : <p>Loading document…</p>}</section></>;
}

export function CoverLetterViewer({ application, back }: { application: Application; back: () => void }) {
  const [exporting, setExporting] = useState(false); const [exportError, setExportError] = useState("");
  const pdfUrl = `${API}/applications/${application.id}/cover-letter.pdf?v=${encodeURIComponent(application.updated_at)}`;
  async function exportPdf() {
    setExporting(true); setExportError("");
    try {
      const response = await fetch(pdfUrl, { cache: "no-store" });
      if (!response.ok) {
        const data = await response.json().catch(() => ({ detail: "Export PDF impossible." }));
        throw new Error(data.detail || "Export PDF impossible.");
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a"); link.href = url; link.download = downloadFilename(response, "lettre-motivation.pdf"); link.click(); URL.revokeObjectURL(url);
    } catch (error) { setExportError((error as Error).message); }
    finally { setExporting(false); }
  }
  return <><header className="viewer-toolbar"><button className="link" onClick={back}>← Back</button><h2>Cover letter — {application.company}</h2>
    <a className="button" href={`${API}/applications/${application.id}/cover-letter.docx`} download>Download DOCX</a>
    <button className="primary" disabled={exporting} onClick={() => void exportPdf()}>{exporting ? "Export…" : "Exporter en PDF"}</button></header>
    {exportError && <p className="error">{exportError}</p>}
    <DocxPreview url={`${API}/applications/${application.id}/cover-letter.docx`} loadingLabel="Loading cover letter…" missingLabel="Cover letter not found." errorLabel="Unable to preview this cover letter." />
  </>;
}

export function CvViewer({ application, back }: { application: Application; back: () => void }) {
  const [state, setState] = useState<DocxState>("loading");
  const [exporting, setExporting] = useState(false); const [exportError, setExportError] = useState(""); const [exportWarning, setExportWarning] = useState("");
  async function exportPdf() {
    setExporting(true); setExportError(""); setExportWarning("");
    try {
      const response = await fetch(`${API}/applications/${application.id}/cv.pdf`);
      if (!response.ok) {
        const data = await response.json().catch(() => ({ detail: "Export PDF impossible." }));
        throw new Error(data.detail || "Export PDF impossible.");
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url; link.download = downloadFilename(response, "resume.pdf"); link.click();
      URL.revokeObjectURL(url);
      setExportWarning(response.headers.get("X-PDF-Warning") || "");
    } catch (error) { setExportError((error as Error).message); }
    finally { setExporting(false); }
  }
  return <><header className="viewer-toolbar"><button className="link" onClick={back}>← Back</button><h2>CV — {application.company}</h2>
    {application.cv_path && state !== "missing" && <a className="button" href={`${API}/applications/${application.id}/cv.docx`} download>Download DOCX</a>}
    <button className="primary" disabled={state === "loading" || state === "missing" || exporting} onClick={() => void exportPdf()}>{exporting ? "Export…" : "Exporter en PDF"}</button></header>
    {exportError && <p className="error">{exportError}</p>}
    {exportWarning && <p className="warning">{exportWarning}</p>}
    <DocxPreview url={`${API}/applications/${application.id}/cv`} loadingLabel="Loading resume…" missingLabel="Resume not found." errorLabel="Unable to preview this resume." onStateChange={setState} />
  </>;
}

type DocxState = "loading" | "ready" | "missing" | "error";

export function DocxPreview({ url, loadingLabel, missingLabel, errorLabel, onStateChange }: { url: string; loadingLabel: string; missingLabel: string; errorLabel: string; onStateChange?: (state: DocxState) => void }) {
  const container = useRef<HTMLDivElement>(null); const [state, setState] = useState<DocxState>("loading");
  useEffect(() => {
    let active = true;
    const update = (value: DocxState) => { if (active) { setState(value); onStateChange?.(value); } };
    update("loading");
    fetch(url).then(async response => {
      if (response.status === 404) throw new Error("missing");
      if (!response.ok) throw new Error("fetch");
      const content = await response.arrayBuffer();
      if (!active) return;
      const target = document.createElement("div");
      await renderAsync(content, target, undefined, { breakPages: true, ignoreWidth: false, ignoreHeight: false, ignoreFonts: false });
      if (!active || !container.current) return;
      container.current.replaceChildren(...target.childNodes);
      update("ready");
    }).catch(error => update(error.message === "missing" ? "missing" : "error"));
    return () => { active = false; };
  }, [url, onStateChange]);
  return <>{state === "loading" && <p>{loadingLabel}</p>}{state === "missing" && <p className="error">{missingLabel}</p>}{state === "error" && <p className="error">{errorLabel}</p>}<div className={`docx-viewer ${state === "ready" ? "ready" : ""}`} ref={container} /></>;
}

function Badge({ status }: { status: string }) { return <span className={`badge ${["OFFER", "REJECTED", "WITHDRAWN", "NO_RESPONSE", "IGNORED"].includes(status) ? "done" : ""}`}>{status}</span>; }
