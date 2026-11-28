import { isTauri } from "./tauri";

export interface ScanRun {
  run_id: string;
  state: "running" | "done" | "failed";
  files_seen: number;
  files_new: number;
  files_updated: number;
  files_moved: number;
  files_missing: number;
  errors: number;
  message: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface SourceStats {
  counts: Record<string, number>;
  available: number;
  stale: number;
  missing: number;
  offline: number;
  last_run: ScanRun | null;
}

export interface SourceParseProgress {
  total: number;
  parsed: number;
  failed: number;
  empty: number;
  pending: number;
  locked: number;
  unsupported: number;
}

export interface Source {
  source_id: string;
  root_uri: string;
  label: string;
  state: "active" | "paused";
  health: string;
  last_scan_at: string | null;
  l0_completed_at: string | null;
  excludes: string;
  stats: SourceStats;
  parse: SourceParseProgress;
  scanning: boolean;
}

export interface SearchItem {
  location_id: string;
  asset_id: string;
  name: string;
  path: string;
  directory: string;
  rel_path: string;
  ext: string;
  size: number;
  mtime: string | null;
  availability: string;
  index_level: string;
  semantic: boolean;
  match: "name" | "content" | "both" | "semantic";
  snippet: string | null;
  page: number | null;
  sheet: string | null;
  bbox: number[][] | null;
  range:
    | { kind: "page"; page: number }
    | { kind: "text"; startOffset: number; endOffset: number }
    | null;
  stale: boolean;
  source_id: string;
  source_label: string;
  family_id?: string | null;
  family_size?: number;
  family_title?: string;
  version_label?: string | null;
  matched_folder?: string | null;
  weak?: boolean;
  family_versions?: {
    asset_id: string;
    location_id: string | null;
    path: string | null;
    name: string | null;
    rel_path?: string | null;
    version_label: string;
  }[];
}

export interface SearchResponse {
  query: string;
  total: number;
  strong: number;
  items: SearchItem[];
  index_level: string;
  content_hits: number;
  truncated: boolean;
  stale: boolean;
  rewritten: string | null;
  vector_used: boolean;
  vector_model: string | null;
  vector_error: string | null;
}

export interface IndexFreshness {
  stale: boolean;
  stale_files: number;
  pending_jobs: number;
  failed_jobs: number;
  sources: {
    source_id: string;
    label: string;
    state: string;
    last_scan_at: string | null;
    last_indexed_at: string | null;
    stale_files: number;
    pending_jobs: number;
    failed_jobs: number;
    stale: boolean;
  }[];
}

export interface IndexJob {
  job_id: string;
  source_id: string;
  version_id: string;
  asset_id: string;
  stage: "l1" | "l2";
  status: string;
  attempts: number;
  error: string | null;
  updated_at: string;
  path: string | null;
  ext: string;
  name: string | null;
}

export interface MaintenanceInfo {
  jobs: Record<string, number>;
  chunks: number;
  artifacts: number;
  db_bytes: number;
  running: {
    running: boolean;
    kind: string | null;
    source_id: string | null;
    started_at: string | null;
    message: string | null;
  };
  budget: {
    date: string;
    l1_used: number;
    l1_limit: number;
    l2_used: number;
    l2_limit: number;
    knowledge_used: number;
    knowledge_limit: number;
  };
  vectors: number;
  vector_skipped?: number;
  embedding: {
    provider: "none" | "openai" | "ollama";
    base_url: string;
    model: string;
    allow_cloud: boolean;
    top_k: number;
    has_key: boolean;
    last_error: string | null;
  };
  knowledge: {
    summaries: number;
    entities: number;
    relations: number;
    questions: number;
    pending: number;
    failed: number;
    jobs: Record<string, number>;
    budget: {
      date: string;
      l1_used: number;
      l1_limit: number;
      l2_used: number;
      l2_limit: number;
      knowledge_used: number;
      knowledge_limit: number;
    };
  };
}

export interface IngestSettings {
  profile: "quiet" | "balanced" | "fast";
  paused: boolean;
  auto_parse: boolean;
  profiles: { key: "quiet" | "balanced" | "fast"; label: string }[];
}

export interface UpdateActivity {
  name: string | null;
  path: string;
  source_id: string;
  source_label: string;
  stage: "L1" | "L2";
  state: string;
  quality: string | null;
  chars: number;
  created_at: string;
}

export interface UpdateFailure {
  job_id: string;
  name: string | null;
  path: string;
  source_id: string;
  source_label: string;
  stage: string;
  attempts: number;
  error: string | null;
  updated_at: string;
}

export interface UpdateOverview {
  auto_parse: boolean;
  paused: boolean;
  profile: "quiet" | "balanced" | "fast";
  watcher_running: boolean;
  watch_enabled: boolean;
  last_sync_at: string | null;
  summary: {
    pending_l1: number;
    pending_l2: number;
    parsed_24h: number;
    failed: number;
    sources: number;
    awaiting_policy: number;
  };
  recent: UpdateActivity[];
  failures: UpdateFailure[];
  generated_at: string;
}

export type ContentPolicy = "deep_parse" | "standard_parse" | "metadata_only" | "exclude_content";

export interface InventoryFileType {
  ext: string;
  label: string;
  count: number;
  bytes: number;
  effective_count: number;
  effective_bytes: number;
  system_excluded_count: number;
  system_excluded_bytes: number;
  count_pct: number;
  bytes_pct: number;
  category: string;
  recommended_policy: ContentPolicy;
  selected_policy: ContentPolicy | null;
  confidence: "high" | "medium" | "low";
  reasons: string[];
  subcategories: Record<string, number>;
  samples: { path: string; size: number }[];
}

export interface InventoryFileTypes {
  items: InventoryFileType[];
  policies: Record<string, ContentPolicy>;
  confirmed: boolean;
  total_files: number;
  total_bytes: number;
  effective_files: number;
  effective_bytes: number;
  system_excluded_files: number;
  system_excluded_bytes: number;
  generated_at: string;
  rules_version: string;
}

export type ExternalPreset = "codex" | "claude_code" | "deepseek" | "custom";
export type ExternalTransport = "mcp" | "http";

export interface ExternalClient {
  client_id: string;
  name: string;
  preset: ExternalPreset;
  preset_label: string;
  preset_hint: string;
  transport: ExternalTransport;
  transport_label: string;
  state: string;
  project_id: string;
  project_name: string;
  project_kind: "global" | "standard" | "";
  created_at: string;
  last_test_at: string | null;
  last_status: "ok" | "failed" | null;
  last_error: string | null;
  call_count: number;
  last_call_at: string | null;
}

export interface ExternalClientConfig {
  client_id: string;
  name: string;
  preset: ExternalPreset;
  transport: ExternalTransport;
  project_id: string;
  project_name: string;
  mcp: {
    command: string;
    args: string[];
    cwd: string | null;
    env: Record<string, string>;
    snippet: string;
  } | null;
  http: {
    base_url: string;
    authorization: string;
    endpoints: { method: string; path: string; description: string }[];
  } | null;
  notes: string[];
}

export type ParseResultStatus =
  | "ok"
  | "failed"
  | "empty"
  | "pending"
  | "locked"
  | "unsupported"
  | "metadata_only";

export interface ParseResultItem {
  location_id: string;
  version_id: string;
  name: string;
  path: string;
  rel_path: string;
  ext: string;
  size: number;
  status: ParseResultStatus;
  stage: "L1" | "L2" | "L1+L2" | null;
  chars: number;
  pages: number;
  quality: string | null;
  message: string | null;
  error: string | null;
  attempts: number;
  updated_at: string | null;
}

export interface ParseResults {
  source_id: string;
  summary: {
    total: number;
    parsed: number;
    failed: number;
    empty: number;
    pending: number;
    locked: number;
    unsupported: number;
    metadata_only: number;
  };
  items: ParseResultItem[];
  total: number;
  limit: number;
  offset: number;
  generated_at: string;
}

export interface DocumentText {
  location_id: string;
  name: string;
  path: string;
  rel_path: string;
  ext: string;
  chars: number;
  truncated: boolean;
  text: string;
}

export interface ExternalTestResult {
  client_id: string;
  ok: boolean;
  status: "ok" | "failed";
  message: string;
  latency_ms: number;
}

export interface FullCoverageSource {
  source_id: string;
  label: string;
  root_uri: string;
  created_at: string;
  l0_completed_at: string | null;
  l1_completed_at: string | null;
  l2_completed_at: string | null;
  full_coverage_at: string | null;
  full_coverage_seconds: number | null;
}

export interface FullCoverage {
  complete: boolean;
  started_at: string | null;
  completed_at: string | null;
  time_to_full_coverage_seconds: number | null;
  elapsed_seconds: number | null;
  l0_complete: boolean;
  l1_complete: boolean;
  l2_complete: boolean;
  l0_completed_at: string | null;
  l1_completed_at: string | null;
  l2_completed_at: string | null;
  l0_seconds: number | null;
  l1_seconds: number | null;
  l2_seconds: number | null;
  pending_l1: number;
  pending_l2: number;
  failed: number;
  ocr_failed: number;
  unsupported: number;
  excluded: number;
  sources: FullCoverageSource[];
}

export interface OverviewStats {
  files: { available: number; missing: number; offline: number; stale: number };
  by_ext: { ext: string; count: number }[];
  sources: {
    source_id: string;
    label: string;
    root_uri: string;
    state: string;
    health: string;
    last_scan_at: string | null;
    l0_completed_at: string | null;
    l1_completed_at: string | null;
    l2_completed_at: string | null;
    full_coverage_at: string | null;
    available: number;
    stale: number;
  }[];
  coverage: {
    directory_level: string;
    directory_indexed: number;
    content_level: string;
    content_indexed: number;
    content_pending: number;
    content_empty: number;
    content_failed: number;
    content_locked: number;
    content_unsupported: number;
    low_quality: number;
    ocr_indexed: number;
    ocr_empty: number;
    ocr_failed: number;
    ocr_locked: number;
    ocr_pending: number;
    mixed_pdf: number;
    ocr_policy: "auto" | "windows" | "rapidocr";
    ocr_available: boolean;
    ocr_engine: string;
    ocr_languages: string[];
    ocr_message: string | null;
  };
  maintenance: MaintenanceInfo;
  ingest: IngestSettings;
  full_coverage: FullCoverage;
}

export interface Citation {
  n: number;
  chunk_id: string;
  asset_id: string;
  location_id: string;
  name: string;
  path: string;
  page: number | null;
  sheet: string | null;
  snippet: string;
  ocr: boolean;
  semantic: boolean;
  version_id: string;
  mtime: string | null;
  bbox: number[][] | null;
  family_id?: string | null;
  family_size?: number;
  family_title?: string;
  version_label?: string | null;
  sources?: {
    asset_id: string;
    location_id: string | null;
    path: string | null;
    name: string | null;
    version_label: string;
  }[];
}

export interface RetrievalPlan {
  intent: string;
  label: string;
  intent_hint?: string | null;
  budget: number;
  per_family: number;
  per_asset: number;
  min_families: number;
  output_mode: string;
  deduplicate_by: string[];
  required_coverage: string[];
  reason: string;
  candidates?: number;
  used?: number;
  families?: number;
  facets?: { name: string; covered: boolean }[];
  missing?: string[];
  reranked?: boolean;
  rerank_kept?: number;
  rerank_empty?: boolean;
  rerank_error?: string | null;
  knowledge_used?: number;
  summary_sources?: number;
  relation_sources?: number;
  faq_sources?: number;
  knowledge_pending?: number;
  knowledge_error?: number;
}

export interface AnswerResult {
  question: string;
  answer: string;
  refused: boolean;
  reason: string | null;
  provider: "extractive" | "openai" | "ollama" | "library";
  model: string | null;
  citations: Citation[];
  evidence_count: number;
  warning: string | null;
  session_id?: string | null;
  plan?: RetrievalPlan | null;
  scope?: RetrievalScopeInfo | null;
  coverage: {
    content_indexed: number;
    content_pending: number;
    content_empty: number;
    content_failed: number;
    content_metadata_only: number;
    ocr_indexed: number;
    ocr_pending: number;
  };
}

export type RetrievalScope = "project" | "expanded" | "global";

export interface RetrievalScopeInfo {
  requested: RetrievalScope;
  used: RetrievalScope;
  expanded: boolean;
  project_id: string | null;
}

export interface ProjectStats {
  assets: number;
  primary: number;
  linked: number;
  excluded: number;
  usable: number;
  parsed: number;
  embedded: number;
  failed: number;
  pending: number;
  sources: number;
  last_updated: string | null;
}

export interface ProjectSource {
  source_id: string;
  relation_type: "primary" | "linked";
  path_prefix: string;
  label: string;
  root_uri: string;
  state: string;
  health: string;
  last_scan_at: string | null;
  available: number;
}

export interface Project {
  project_id: string;
  name: string;
  description: string;
  status: "active" | "archived";
  kind: "global" | "standard";
  retrieval_policy: "project_first" | "expanded" | "global";
  privacy_policy: "external_allowed" | "local_only";
  created_at: string;
  updated_at: string;
  stats?: ProjectStats;
  sources?: ProjectSource[];
  source_count?: number;
  primary_count?: number;
  linked_count?: number;
  excluded_count?: number;
}

export interface ProjectAsset {
  asset_id: string;
  location_id: string;
  name: string;
  path: string;
  rel_path?: string;
  ext: string;
  size: number;
  membership_type: "primary" | "linked" | "excluded";
  priority?: number;
  source_label?: string;
  parse_state?: string | null;
  parse_message?: string | null;
  projects?: string[];
}

export interface ChatSession {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface ChatMessage {
  message_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  payload: AnswerResult | null;
}

export interface AnswerSettings {
  provider: "extractive" | "openai" | "ollama";
  base_url: string;
  model: string;
  allow_cloud: boolean;
  top_k: number;
  reasoning_effort?: "low" | "high" | "max";
  max_tokens?: number;
  has_key: boolean;
}

export interface EmbeddingSettings {
  provider: "none" | "openai" | "ollama";
  base_url: string;
  model: string;
  allow_cloud: boolean;
  top_k: number;
  has_key: boolean;
  last_error: string | null;
}

export interface QuerySettings {
  custom: string[][];
  builtin_groups: number;
}

export interface OcrEngineInfo {
  available: boolean;
  engine: string;
  label: string;
  languages: string[];
  message: string | null;
}

export interface OcrSettings {
  policy: "auto" | "windows" | "rapidocr";
  engines: OcrEngineInfo[];
}

export interface MaintenanceEvent {
  event_id: number;
  type: string;
  source_id: string | null;
  detail: string | null;
  outcome: string | null;
  timestamp: string;
}

export interface DocumentFamilySummary {
  family_id: string;
  title: string;
  confidence: string;
  size: number;
  members: {
    asset_id: string;
    version_label: string;
    similarity: number;
    path: string | null;
  }[];
}

export interface DocumentFamilyMember {
  asset_id: string;
  location_id: string | null;
  path: string | null;
  name: string | null;
  rel_path: string | null;
  version_label: string;
}

export interface FamilyContentSource {
  chunk_id: string;
  asset_id: string;
  location_id: string | null;
  path: string | null;
  name: string | null;
}

export interface FamilyContentGroup {
  content_group_id: string;
  normalized_hash: string;
  text: string;
  created_at: string;
  sources: FamilyContentSource[];
}

export interface DocumentFamilyDetail {
  family: {
    family_id: string;
    title: string;
    confidence: string;
    rules_version: string;
    created_at: string;
    updated_at: string;
  };
  members: DocumentFamilyMember[];
  content_groups: FamilyContentGroup[];
}

export interface FamilyDiffItem {
  content_group_id: string;
  text: string;
  sources: {
    asset_id: string;
    name: string | null;
    path: string | null;
    version_label: string;
  }[];
}

export interface FamilyDiffChange {
  similarity: number;
  left: FamilyDiffItem;
  right: FamilyDiffItem;
}

export interface FamilyDiff {
  family_id: string;
  family_title: string;
  left: { asset_id: string; name: string | null; path: string | null; version_label: string };
  right: { asset_id: string; name: string | null; path: string | null; version_label: string };
  summary: {
    left_groups: number;
    right_groups: number;
    shared: number;
    removed: number;
    added: number;
    modified: number;
  };
  shared: FamilyDiffItem[];
  removed: FamilyDiffItem[];
  added: FamilyDiffItem[];
  modified: FamilyDiffChange[];
  truncated: {
    shared: boolean;
    removed: boolean;
    added: boolean;
    modified: boolean;
  };
}

// 安装版由 Tauri 资源服务器加载，相对 /api 会返回 index.html；直接访问本机核心。
// 开发模式下同样可用（FastAPI CORS 允许 localhost:1420）。
const API_BASE = "http://127.0.0.1:8737";

let apiToken: string | null = null;
let tokenPromise: Promise<string> | null = null;

const wait = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

async function readApiToken(): Promise<string> {
  if (apiToken) return apiToken;
  if (!tokenPromise) {
    tokenPromise = (async () => {
      // 安装版 onefile 启动需要解包，给核心最多 30 秒写入令牌。
      for (let attempt = 0; attempt < 120; attempt += 1) {
        try {
          let token = "";
          if (isTauri()) {
            const { invoke } = await import("@tauri-apps/api/core");
            token = (await invoke<string>("api_token")).trim();
          } else {
            const response = await fetch("/__shiyuan_token", { cache: "no-store" });
            if (response.ok) token = (await response.text()).trim();
          }
          if (token) {
            apiToken = token;
            return token;
          }
        } catch {
          /* 核心尚未写入令牌时继续重试 */
        }
        await wait(250);
      }
      throw new Error("无法获取本地核心访问令牌，请确认桌面端或核心已启动");
    })();
  }
  try {
    return await tokenPromise;
  } finally {
    tokenPromise = null;
  }
}

export interface AskStreamHandlers {
  onStatus?: (stage: string, message: string) => void;
  onDelta?: (text: string) => void;
  onDone?: (result: AnswerResult) => void;
}

async function askStreamRequest(
  question: string,
  options: { sourceId?: string; ext?: string; topK?: number; sessionId?: string;
    projectId?: string; scope?: RetrievalScope },
  handlers: AskStreamHandlers,
  retry = true,
): Promise<void> {
  const token = await readApiToken();
  const response = await fetch(`${API_BASE}/api/ask/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      question,
      ...(options.sourceId ? { source_id: options.sourceId } : {}),
      ...(options.ext ? { ext: options.ext } : {}),
      ...(options.topK ? { top_k: options.topK } : {}),
      ...(options.sessionId ? { session_id: options.sessionId } : {}),
      ...(options.projectId ? { project_id: options.projectId } : {}),
      ...(options.scope ? { scope: options.scope } : {}),
    }),
  });
  if (response.status === 401 && retry) {
    apiToken = null;
    return askStreamRequest(question, options, handlers, false);
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch {
      /* 保留状态码信息 */
    }
    throw new Error(detail);
  }
  if (!response.body) throw new Error("当前环境不支持流式响应");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut = buffer.indexOf("\n\n");
    while (cut >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const line = block.split("\n").find((item) => item.startsWith("data:"));
      if (line) {
        let event: { type?: string; stage?: string; message?: string; text?: string; result?: AnswerResult };
        try {
          event = JSON.parse(line.slice(5).trim());
        } catch {
          cut = buffer.indexOf("\n\n");
          continue;
        }
        if (event.type === "status") {
          handlers.onStatus?.(event.stage ?? "", event.message ?? "");
        } else if (event.type === "delta") {
          handlers.onDelta?.(event.text ?? "");
        } else if (event.type === "done" && event.result) {
          finished = true;
          handlers.onDone?.(event.result);
        } else if (event.type === "error") {
          throw new Error(event.message || "生成回答失败");
        }
      }
      cut = buffer.indexOf("\n\n");
    }
  }
  if (!finished) throw new Error("流式连接中断，未收到完整回答");
}

async function request<T>(path: string, init?: RequestInit, retry = true): Promise<T> {
  const token = await readApiToken();
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(init?.headers ?? {}),
    },
  });
  if (response.status === 401 && retry) {
    // 核心重启后令牌会变；清缓存并重取一次。
    apiToken = null;
    return request<T>(path, init, false);
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* 保留状态码信息 */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<{ status: string; version: string; db: string }>("/api/health"),
  sources: () => request<{ items: Source[] }>("/api/sources"),
  addSource: (root: string) =>
    request<{
      source_id: string;
      root_uri: string;
      label: string;
      created: boolean;
      covered_by: string | null;
      merged: { source_id: string; label: string; root_uri: string }[];
    }>("/api/sources", { method: "POST", body: JSON.stringify({ root }) }),
  scan: (id: string) => request<{ state: string }>(`/api/sources/${id}/scan`, { method: "POST" }),
  ocr: (id: string) =>
    request<{ state: string; pending: number }>(`/api/sources/${id}/ocr`, { method: "POST" }),
  setState: (id: string, state: "active" | "paused") =>
    request<{ state: string }>(`/api/sources/${id}/state?state=${state}`, { method: "POST" }),
  removeSource: (id: string) => request<{ assets_cleared: number }>(`/api/sources/${id}`, { method: "DELETE" }),
  search: (query: string, options: { limit?: number; offset?: number; sourceId?: string;
    ext?: string; projectId?: string; scope?: RetrievalScope; mtimeDays?: number } = {}) => {
    const params = new URLSearchParams({ q: query, limit: String(options.limit ?? 50) });
    if (options.offset) params.set("offset", String(options.offset));
    if (options.sourceId) params.set("source_id", options.sourceId);
    if (options.ext) params.set("ext", options.ext);
    if (options.projectId) params.set("project_id", options.projectId);
    if (options.scope) params.set("scope", options.scope);
    if (options.mtimeDays) params.set("mtime_days", String(options.mtimeDays));
    return request<SearchResponse>(`/api/search?${params.toString()}`);
  },
  stats: () => request<OverviewStats>("/api/stats"),
  freshness: (projectId?: string) => {
    const params = new URLSearchParams();
    if (projectId) params.set("project_id", projectId);
    const query = params.toString();
    return request<IndexFreshness>(`/api/freshness${query ? `?${query}` : ""}`);
  },
  projects: (options: { includeArchived?: boolean; externalOnly?: boolean } = {}) => {
    const params = new URLSearchParams();
    if (options.includeArchived) params.set("include_archived", "true");
    if (options.externalOnly) params.set("external_only", "true");
    return request<{ items: Project[]; total: number }>(`/api/projects?${params.toString()}`);
  },
  createProject: (payload: {
    name: string;
    description?: string;
    folders?: string[];
    retrieval_policy?: string;
    privacy_policy?: string;
  }) =>
    request<{ project: Project; sources: { source_id?: string; root?: string; error?: string; assets_linked?: number }[] }>(
      "/api/projects",
      { method: "POST", body: JSON.stringify(payload) },
    ),
  project: (id: string) => request<Project>(`/api/projects/${id}`),
  updateProject: (id: string, payload: Partial<{
    name: string;
    description: string;
    status: string;
    retrieval_policy: string;
    privacy_policy: string;
  }>) =>
    request<{ project: Project }>(`/api/projects/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteProject: (id: string) =>
    request<{ deleted: boolean }>(`/api/projects/${id}`, { method: "DELETE" }),
  addProjectSource: (id: string, payload: {
    root?: string;
    source_id?: string;
    relation_type?: string;
    path_prefix?: string;
  }) =>
    request<{ source_id: string; assets_linked: number }>(`/api/projects/${id}/sources`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  removeProjectSource: (id: string, sourceId: string) =>
    request<{ removed: boolean; assets_removed: number }>(
      `/api/projects/${id}/sources/${sourceId}`,
      { method: "DELETE" },
    ),
  projectAssets: (id: string, options: {
    membership?: string;
    q?: string;
    limit?: number;
    offset?: number;
  } = {}) => {
    const params = new URLSearchParams({
      limit: String(options.limit ?? 100),
      offset: String(options.offset ?? 0),
    });
    if (options.membership) params.set("membership", options.membership);
    if (options.q) params.set("q", options.q);
    return request<{ items: ProjectAsset[]; total: number; limit: number; offset: number }>(
      `/api/projects/${id}/assets?${params.toString()}`,
    );
  },
  addProjectAssets: (id: string, payload: {
    asset_ids?: string[];
    paths?: string[];
    membership_type?: string;
    priority?: number;
  }) =>
    request<{ updated: number; missing?: string[] }>(`/api/projects/${id}/assets`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  removeProjectAsset: (id: string, assetId: string) =>
    request<{ removed: boolean; action?: string }>(
      `/api/projects/${id}/assets/${assetId}`,
      { method: "DELETE" },
    ),
  projectKnowledge: (id: string) =>
    request<{
      summaries: { title: string; summary: string; path?: string | null }[];
      entities: { name: string; type: string; description: string; path?: string | null }[];
      relations: { subject: string; predicate: string; object: string; path?: string | null }[];
      questions: { question: string; answer: string; path?: string | null }[];
    }>(`/api/projects/${id}/knowledge`),
  assets: (options: { unassigned?: boolean; q?: string; limit?: number; offset?: number } = {}) => {
    const params = new URLSearchParams({
      limit: String(options.limit ?? 100),
      offset: String(options.offset ?? 0),
    });
    if (options.unassigned) params.set("unassigned", "true");
    if (options.q) params.set("q", options.q);
    return request<{ items: ProjectAsset[]; total: number; limit: number; offset: number }>(
      `/api/assets?${params.toString()}`,
    );
  },
  askStream: (
    question: string,
    options: { sourceId?: string; ext?: string; topK?: number; sessionId?: string;
      projectId?: string; scope?: RetrievalScope },
    handlers: AskStreamHandlers,
  ) => askStreamRequest(question, options, handlers),
  ask: (question: string, options: { sourceId?: string; ext?: string; topK?: number;
    sessionId?: string; projectId?: string; scope?: RetrievalScope } = {}) =>
    request<AnswerResult>("/api/ask", {
      method: "POST",
      body: JSON.stringify({
        question,
        ...(options.sourceId ? { source_id: options.sourceId } : {}),
        ...(options.ext ? { ext: options.ext } : {}),
        ...(options.topK ? { top_k: options.topK } : {}),
        ...(options.sessionId ? { session_id: options.sessionId } : {}),
        ...(options.projectId ? { project_id: options.projectId } : {}),
        ...(options.scope ? { scope: options.scope } : {}),
      }),
    }),
  chatSessions: () => request<{ items: ChatSession[] }>("/api/chat/sessions"),
  createChatSession: (title?: string) =>
    request<ChatSession>("/api/chat/sessions", {
      method: "POST",
      body: JSON.stringify(title ? { title } : {}),
    }),
  renameChatSession: (id: string, title: string) =>
    request<ChatSession>(`/api/chat/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  deleteChatSession: (id: string) =>
    request<{ session_id: string; deleted: boolean }>(`/api/chat/sessions/${id}`, {
      method: "DELETE",
    }),
  chatSession: (id: string) =>
    request<{ session: ChatSession; messages: ChatMessage[] }>(`/api/chat/sessions/${id}`),
  answerSettings: () => request<AnswerSettings>("/api/settings/answer"),
  saveAnswerSettings: (payload: Record<string, unknown>) =>
    request<AnswerSettings>("/api/settings/answer", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  embeddingSettings: () => request<EmbeddingSettings>("/api/settings/embedding"),
  saveEmbeddingSettings: (payload: Record<string, unknown>) =>
    request<EmbeddingSettings>("/api/settings/embedding", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  querySettings: () => request<QuerySettings>("/api/settings/query"),
  saveQuerySettings: (payload: Record<string, unknown>) =>
    request<QuerySettings>("/api/settings/query", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  ocrSettings: () => request<OcrSettings>("/api/settings/ocr"),
  saveOcrSettings: (payload: Record<string, unknown>) =>
    request<OcrSettings>("/api/settings/ocr", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  ingestSettings: () => request<IngestSettings>("/api/settings/ingest"),
  saveIngestSettings: (payload: {
    profile?: string;
    paused?: boolean;
    auto_parse?: boolean;
  }) =>
    request<IngestSettings>("/api/settings/ingest", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  updatesOverview: () => request<UpdateOverview>("/api/updates/overview"),
  checkUpdates: () =>
    request<{ started: string[] }>("/api/updates/check", { method: "POST" }),
  rebuildFamilies: () =>
    request<{ state: string }>("/api/families/rebuild", { method: "POST" }),
  documentFamilies: (limit = 50) =>
    request<{ items: DocumentFamilySummary[] }>(`/api/families?limit=${limit}`),
  documentFamily: (id: string, limit = 1000) =>
    request<DocumentFamilyDetail>(`/api/families/${id}?limit=${limit}`),
  documentFamilyMembers: (id: string) =>
    request<{
      family_id: string;
      title: string;
      confidence: string;
      members: DocumentFamilyMember[];
    }>(`/api/families/${id}/members`),
  familyMembersByAsset: (assetId: string) =>
    request<{
      family_id: string;
      title: string;
      confidence: string;
      members: DocumentFamilyMember[];
    }>(`/api/families/by-asset/${encodeURIComponent(assetId)}/members`),
  familyDiff: (
    id: string,
    leftAssetId: string,
    rightAssetId: string,
    limit = 200,
  ) => {
    const params = new URLSearchParams({
      left_asset_id: leftAssetId,
      right_asset_id: rightAssetId,
      limit: String(limit),
    });
    return request<FamilyDiff>(`/api/families/${id}/diff?${params.toString()}`);
  },
  inventoryFileTypes: (sourceId?: string) =>
    request<InventoryFileTypes>(
      `/api/inventory/file-types${sourceId ? `?source_id=${encodeURIComponent(sourceId)}` : ""}`,
    ),
  saveInventoryPolicies: (sourceId: string, policies: Record<string, ContentPolicy>) =>
    request<{
      source_id: string;
      policies: Record<string, ContentPolicy>;
      confirmed: boolean;
      started: boolean;
    }>("/api/inventory/policies", {
      method: "PUT",
      body: JSON.stringify({ source_id: sourceId, policies }),
    }),
  sourceParseResults: (
    sourceId: string,
    options: { status?: string; limit?: number; offset?: number } = {},
  ) => {
    const params = new URLSearchParams({
      limit: String(options.limit ?? 100),
      offset: String(options.offset ?? 0),
    });
    if (options.status && options.status !== "all") params.set("status", options.status);
    return request<ParseResults>(
      `/api/sources/${sourceId}/parse-results?${params.toString()}`,
    );
  },
  documentText: (locationId: string, maxChars = 200_000) => {
    const params = new URLSearchParams({
      location_id: locationId,
      max_chars: String(maxChars),
    });
    return request<DocumentText>(`/api/document/text?${params.toString()}`);
  },
  externalClients: (projectId?: string) =>
    request<{ items: ExternalClient[] }>(
      `/api/external/clients${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
    ),
  createExternalClient: (payload: {
    name?: string;
    preset: ExternalPreset;
    transport?: ExternalTransport;
    projectId?: string;
  }) =>
    request<{ client: ExternalClient }>("/api/external/clients", {
      method: "POST",
      body: JSON.stringify({
        name: payload.name,
        preset: payload.preset,
        transport: payload.transport,
        project_id: payload.projectId,
      }),
    }),
  deleteExternalClient: (id: string) =>
    request<{ client_id: string; deleted: boolean }>(`/api/external/clients/${id}`, {
      method: "DELETE",
    }),
  externalClientConfig: (id: string) =>
    request<ExternalClientConfig>(`/api/external/clients/${id}/config`),
  testExternalClient: (id: string) =>
    request<ExternalTestResult>(`/api/external/clients/${id}/test`, { method: "POST" }),
  events: (limit = 30) => request<{ items: MaintenanceEvent[] }>(`/api/events?limit=${limit}`),
  maintenance: (
    action: "reindex" | "retry" | "ocr" | "cleanup" | "embed" | "knowledge",
    sourceId?: string,
  ) =>
    request<{ kind: string }>(
      `/api/maintenance/${action}${sourceId ? `?source_id=${encodeURIComponent(sourceId)}` : ""}`,
      { method: "POST" },
    ),
  jobs: (options: { status?: string; limit?: number } = {}) => {
    const params = new URLSearchParams({ limit: String(options.limit ?? 20) });
    if (options.status) params.set("status", options.status);
    return request<{ items: IndexJob[] }>(`/api/jobs?${params.toString()}`);
  },
  retryJob: (jobId: string) =>
    request<{ job_id: string; state: string }>(`/api/jobs/${jobId}/retry`, { method: "POST" }),
};

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[index]}`;
}

export function formatTime(iso: string | null): string {
  if (!iso) return "未同步";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
