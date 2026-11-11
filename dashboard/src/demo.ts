// The public demo at demo.trimset.dev (ADR 0017): built with `npm run build:demo`, it has
// no backend. It answers the dashboard's calls from recorded.json (demo/public/, shipped
// only in the demo build), which CI records from a real server running the M2 demo
// (dashboard/demo/record.py). Nothing is computed here: a request the recording doesn't
// hold is refused, and nothing is ever saved.
import {
  ApiError,
  type DecisionRecord,
  type Fit,
  type Job,
  type Key,
  type PolicyDiff,
  type PolicyMetrics,
  type PolicyView,
  type Project,
  type Question,
  type Rule,
} from "./api.ts";

interface Recorded {
  recorded_at: string;
  project: Project;
  keys: { items: Key[] };
  question: Question;
  model: string;
  fits: Fit[];
  coverage: unknown;
  shapes: Rule[];
  costs: unknown;
  grid: Record<string, PolicyMetrics>;
  compiles: Record<string, Job>;
  policies: PolicyView[];
  diffs: Record<string, PolicyDiff>;
  decisions: DecisionRecord[];
  pending: string[];
}

// Fetched once; a failed fetch is forgotten, so "Try again" fetches it again.
let recording: Promise<Recorded> | null = null;
function load(): Promise<Recorded> {
  recording ??= fetch("./recorded.json")
    .then((r) => {
      if (!r.ok) throw new ApiError(r.status, "The demo recording is missing");
      return r.json() as Promise<Recorded>;
    })
    .catch((e) => {
      recording = null;
      throw e;
    });
  return recording;
}

/** The grid step the recording used; the demo's simulator handles move by it. */
export const STEP = 0.05;

const saved = () =>
  new ApiError(403, "The demo saves nothing", {
    detail: "This is a recording of trimset's refund-fraud example. Run trimset yourself to change things: docker compose up.",
  });

const notRecorded = (detail: string) => new ApiError(422, "Not in the demo recording", { detail });

type Init = { method?: string; body?: unknown };

export async function answer(path: string, init: Init = {}): Promise<unknown> {
  const D = await load();
  await new Promise((r) => setTimeout(r, 120));
  const url = new URL(path, window.location.origin);
  const p = url.pathname;
  const qs = url.searchParams;
  const method = init.method ?? (init.body === undefined ? "GET" : "POST");
  const body = (init.body ?? {}) as Record<string, any>;

  if (p === "/v1/admin/project" && method === "GET") return D.project;
  if (p === "/v1/admin/keys" && method === "GET") return D.keys;

  if (p.startsWith("/v1/admin/questions/")) {
    const [ref, ...rest] = p.slice("/v1/admin/questions/".length).split("/").map(decodeURIComponent);
    if (ref !== D.question.name && ref !== D.question.question_id)
      throw new ApiError(404, "No such question in the demo", {
        detail: `The demo holds recorded data for ${D.question.name} only. The other questions on these screens are design samples.`,
      });
    const sub = rest.join("/");
    const ours = (qs.get("model_served") ?? D.model) === D.model;
    if (method === "GET" && sub === "") return D.question;
    if (method === "GET" && sub === "fits") return { items: ours ? D.fits : [] };
    if (method === "GET" && sub === "coverage") return D.coverage;
    if (method === "POST" && sub === "simulate") {
      const fit_id = D.fits[0].fit_id;
      if (!body.candidates) return { fit_id, rows: [{ rules: D.shapes, metrics: D.grid["0.30,0.85"] }] };
      return {
        fit_id,
        rows: (body.candidates as Rule[][]).map((rules) => {
          const lo = rules.find((r) => r.op === "lt")?.threshold ?? NaN;
          const hi = rules.find((r) => r.op === "ge")?.threshold ?? NaN;
          const metrics = D.grid[`${lo.toFixed(2)},${hi.toFixed(2)}`];
          if (!metrics) throw notRecorded(`The demo recorded thresholds on a ${STEP} grid with the ACCEPT cut-off below the BLOCK one.`);
          return { rules, metrics };
        }),
      };
    }
    if (method === "POST" && sub === "compile") {
      const t = body.target ?? {};
      const job = t.kind === "risk" && t.metric === "false_pass" && t.delta === 0.05 ? D.compiles[String(t.alpha)] : undefined;
      if (!job) throw notRecorded("The demo recorded compiles for false passes ≤ 1%, 2% and 5% at 95% confidence.");
      return job;
    }
  }

  if (p === "/v1/admin/policies" && method === "GET") return { items: D.policies, next_cursor: null };
  if (p.startsWith("/v1/admin/policies/") && p.endsWith("/diff") && method === "GET") {
    const id = p.split("/")[4];
    const diff = D.diffs[`${id}:${qs.get("against") ?? ""}`];
    if (!diff) throw new ApiError(404, "No such policy in the demo");
    return diff;
  }

  if (p === "/v1/admin/decisions" && method === "GET") {
    const q = qs.get("question");
    const action = qs.get("action");
    const to = qs.get("to");
    const pending = new Set(D.pending);
    const items = D.decisions.filter(
      (d) =>
        (!q || q === d.question_name || q === d.question_id) &&
        (!qs.get("model_served") || d.model_served === qs.get("model_served")) &&
        (!action || d.action_taken === action || (action === "SELECT" && d.action_taken?.startsWith("SELECT:"))) &&
        (!qs.get("external_ref") || d.external_ref === qs.get("external_ref")) &&
        (!to || d.ts < to) &&
        (qs.get("outcome") !== "pending" || pending.has(d.decision_id)),
    );
    return { items: items.slice(0, Number(qs.get("limit") ?? 100)), next_cursor: null };
  }

  if (method !== "GET") throw saved();
  throw new ApiError(404, "Not in the demo recording");
}
