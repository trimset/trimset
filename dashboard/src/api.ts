// The only way the dashboard reaches a backend: the trimset API in
// docs/03-api/openapi.yaml, with the admin key the user pasted. Screens whose endpoints
// don't exist yet read src/mock.ts instead.

// The key lives in sessionStorage: gone when the tab closes, never sent anywhere but
// this origin's API. Blocked storage falls back to memory until the page reloads.
const KEY = "trimset.adminKey";
let memoryKey: string | null = null;

export function getKey(): string | null {
  try {
    return sessionStorage.getItem(KEY) ?? memoryKey;
  } catch {
    return memoryKey;
  }
}

export function setKey(key: string | null): void {
  memoryKey = key;
  try {
    if (key) sessionStorage.setItem(KEY, key);
    else sessionStorage.removeItem(KEY);
  } catch {
    // Kept in memory only.
  }
  window.dispatchEvent(new Event("trimset:key"));
}

/** An RFC 9457 problem, or a network failure (status 0). */
export class ApiError extends Error {
  status: number;
  type: string | null;
  detail: string | null;
  errors: { pointer?: string; message?: string }[];
  constructor(status: number, title: string, body: Record<string, unknown> = {}) {
    super(title);
    this.status = status;
    this.type = typeof body.type === "string" ? body.type : null;
    this.detail = typeof body.detail === "string" ? body.detail : null;
    this.errors = Array.isArray(body.errors) ? body.errors : [];
  }
}

export async function problemOf(res: Response): Promise<ApiError> {
  let body: Record<string, unknown> = {};
  try {
    body = await res.json();
  } catch {
    // Not JSON: a proxy or load balancer answered.
  }
  const title = typeof body.title === "string" ? body.title : res.statusText || `HTTP ${res.status}`;
  return new ApiError(res.status, title, body);
}

type Init = { method?: string; body?: unknown; merge?: boolean; signal?: AbortSignal; key?: string };

/** The public demo build (`npm run build:demo`): answers come from a recording. */
export const DEMO = import.meta.env.VITE_DEMO === "1";

export async function api<T>(path: string, init: Init = {}): Promise<T> {
  if (DEMO) return (await (await import("./demo.ts")).answer(path, init)) as T;
  const key = init.key ?? getKey();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (key) headers.Authorization = `Bearer ${key}`;
  if (init.body !== undefined) {
    headers["Content-Type"] = init.merge ? "application/merge-patch+json" : "application/json";
  }
  let res: Response;
  try {
    res = await fetch(path, {
      method: init.method ?? (init.body === undefined ? "GET" : "POST"),
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      signal: init.signal,
      credentials: "omit",
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(0, "The trimset server didn't answer");
  }
  if (res.status === 401 && !init.key) setKey(null);
  if (!res.ok) throw await problemOf(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const q = (name: string) => encodeURIComponent(name);

const questions = new Map<string, Promise<Question | null>>();

/** A question's definition, once per page load, for labelling options and levels. */
export function questionOf(ref: string): Promise<Question | null> {
  if (!questions.has(ref)) {
    questions.set(ref, api<Question>(`/v1/admin/questions/${q(ref)}`).catch(() => null));
  }
  return questions.get(ref)!;
}

/** Poll a job until it finishes. Fit and compile run on the worker, usually in seconds. */
export async function waitJob(job: Job, signal?: AbortSignal): Promise<Job> {
  let current = job;
  for (let i = 0; current.status === "queued" || current.status === "running"; i++) {
    if (i > 600) throw new ApiError(0, "The job is still running; check it again later");
    await new Promise((r) => setTimeout(r, i < 10 ? 500 : 2000));
    current = await api<Job>(`/v1/admin/jobs/${current.job_id}`, { signal });
  }
  return current;
}

// ------------------------------------------------------------- contract types

export type Probability = number;
export type Interval = [number, number];

export interface Key {
  key_id: string;
  scope: "proxy" | "ingest" | "review" | "admin";
  label: string;
  prefix: string;
  created_at: string;
  last_used_at?: string | null;
  revoked_at?: string | null;
}

export interface ProjectSettings {
  upstream?: { name: string; timeout_ms?: number; credentials?: "passthrough" | "server" };
  retention_days?: number;
  privacy?: { store_raw_state?: boolean; raw_state_retention_days?: number; tag_allowlist?: string[] | null };
  refit_schedule?: string;
  proxy_max_body_mb?: number;
}

export interface Project {
  project_id: string;
  name: string;
  settings: ProjectSettings;
  available?: {
    upstreams: { name: string; format: "systemone" | "openai-decisions"; server_key: boolean }[];
    alert_channels: { name: string; kind: "webhook" | "slack" | "email" }[];
  };
}

export type QuestionType = "noul" | "choice" | "score";

export interface Costs {
  escalation?: number;
  wrong?: Partial<Record<"ACCEPT" | "BLOCK" | "SELECT", number>>;
}

export interface Target {
  kind: "risk" | "cost";
  metric?: "false_pass" | "error_budget";
  alpha?: number;
  delta?: number;
  max_escalation?: number;
  costs?: Costs;
}

export interface Question {
  question_id: string;
  name: string | null;
  type: QuestionType;
  definition: {
    instructions: unknown;
    options?: { value: string | boolean; description: unknown }[];
    levels?: { label: string | null; description: unknown }[];
  };
  first_seen: string;
  models: { model_requested: string; model_served: string; first_seen: string }[];
  settings: { target?: Target; actions?: Record<string, unknown> };
}

export interface Bin {
  lo: number;
  hi: number;
  count: number;
  mean_predicted: number | null;
  observed: number | null;
  ci95: Interval | null;
}

export interface FitMetrics {
  n: number;
  log_loss: number;
  brier: number;
  ece_equal_width: number;
  ece_equal_mass: number;
  bins: number;
}

export interface Fit {
  fit_id: string;
  model_served: string;
  type: QuestionType;
  created_at: string;
  calibration: { method: "identity" | "temperature" | "platt" | "isotonic" } & Record<string, unknown>;
  selection: { cv_folds: number; cv_log_loss: Partial<Record<string, number>> };
  provenance: {
    window: { from: string; to: string; n?: number };
    n: { fit: number; tune: number; eval: number };
    label_horizon_days: number;
  };
  metrics: { raw: FitMetrics; calibrated: FitMetrics };
  reliability: { raw: Bin[]; calibrated: Bin[] };
}

export interface Rule {
  id: "accept" | "block" | "select";
  action: "ACCEPT" | "BLOCK" | "SELECT";
  scalar: "p" | "top" | { tail_ge: number };
  op: "lt" | "ge";
  threshold: number;
}

export interface RuleMetrics {
  fired: number;
  wrong: number;
  unlabeled: number;
  rate: number | null;
  ci95: Interval | null;
}

export interface PolicyMetrics {
  n: number;
  automated: number;
  escalated: number;
  rules: Record<string, RuleMetrics>;
  expected_cost_per_1000?: number | null;
}

export interface SimulateResult {
  fit_id: string;
  rows: { rules: Rule[]; metrics: PolicyMetrics }[];
}

export interface CoveragePath {
  model_served: string;
  action: "ACCEPT" | "BLOCK" | "SELECT" | "ESCALATE" | "none";
  decisions: number;
  matured: number;
  labeled: number;
  audited_labeled?: number;
  median_delay_hours?: number | null;
}

/** record/1, as GET /v1/admin/decisions returns it. */
export interface DecisionRecord {
  decision_id: string;
  ts: string;
  question_id: string;
  question_name?: string | null;
  type: QuestionType;
  model_requested: string;
  model_served: string;
  probabilities: number[] | null;
  refusal?: boolean;
  latency_ms?: number | null;
  cost_usd?: number | null;
  policy_version?: number | null;
  policy_fingerprint?: string | null;
  action_taken?: string | null;
  action_source?: "policy" | "audit" | "default" | null;
  audit?: boolean;
  tags?: Record<string, string>;
  external_ref?: string | null;
  state_hash?: string | null;
  state_hash_alg?: string | null;
  role?: "primary" | "shadow";
}

export type Guarantee =
  | {
      status: "certified";
      method: string;
      statement: string;
      alpha: number;
      delta: number;
      assumptions: string[];
      n: Record<string, number>;
      window: { from: string; to: string; n?: number };
    }
  | { status: "none"; reason: "cost_target" | "adaptive_under_shift"; detail?: string };

export interface Policy {
  policy_id: string;
  question_id: string;
  question_name?: string | null;
  model_served: string;
  type: QuestionType;
  version: number;
  created_at: string;
  fit_id: string;
  rules: Rule[];
  fallback_action: string;
  audit_fraction: number;
  target: Target;
  guarantee: Guarantee;
  metrics: { tune: PolicyMetrics; eval: PolicyMetrics };
  fingerprint: string;
}

export type PolicyState = "draft" | "proposed" | "approved" | "active" | "retired" | "rejected";

export interface PolicyView {
  policy: Policy;
  state: PolicyState;
  guarantee_status: "certified" | "suspended" | "none";
  suspended_by?: string | null;
  events: { from_state: PolicyState | null; to_state: PolicyState; key_id: string | null; reason?: string | null; at: string }[];
}

export interface PolicyDiff {
  policy_id: string;
  against: string | null;
  rules: { id: string; before: number | null; after: number | null }[];
  metrics: { before: PolicyMetrics | null; after: PolicyMetrics };
}

export interface Job {
  job_id: number;
  kind: "fit" | "compile" | "deletion" | "refit";
  status: "queued" | "running" | "succeeded" | "failed";
  result?: Record<string, unknown> | null;
  error?: string | null;
}
