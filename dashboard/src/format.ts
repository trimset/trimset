// Display only. Nothing here derives a rate, an interval or a guarantee: those come from
// the server and are formatted as they arrive.
import type { Interval, PolicyMetrics, Rule } from "./api.ts";

const int = new Intl.NumberFormat("en-US");

export const count = (n: number | null | undefined): string => (n == null ? "–" : int.format(n));

/** Probabilities show 2 decimals. */
export const prob = (p: number | null | undefined): string => (p == null ? "–" : p.toFixed(2));

/** Rates show a %. */
export const pct = (x: number | null | undefined, digits = 1): string =>
  x == null ? "–" : `${(100 * x).toFixed(digits)}%`;

export const interval = (ci: Interval | null | undefined, digits = 2): string =>
  ci ? `${pct(ci[0], digits)} – ${pct(ci[1], digits)}` : "–";

export const usd = (x: number | null | undefined): string =>
  x == null ? "–" : `$${x.toLocaleString("en-US", { maximumFractionDigits: x < 10 ? 2 : 0 })}`;

/** "accept: p < 0.30" style label for a rule, in the server's own terms. */
export function scalarName(scalar: Rule["scalar"]): string {
  if (scalar === "p") return "p";
  if (scalar === "top") return "top probability";
  return `P(level ≥ ${scalar.tail_ge})`;
}

export const sameScalar = (a: Rule["scalar"], b: Rule["scalar"]): boolean =>
  JSON.stringify(a) === JSON.stringify(b);

/** The candidate for POST simulate: the server's rule shapes with the user's thresholds. */
export function candidate(shapes: Rule[], thresholds: number[]): Rule[] {
  return shapes.map((r, i) => ({ ...r, threshold: Number(thresholds[i].toFixed(4)) }));
}

/**
 * Starting handles: a rule that fires below its threshold starts low, one that fires from
 * its threshold up starts high, so a pair on one scalar leaves a band between them.
 */
export const startThreshold = (r: Rule): number => (r.op === "lt" ? 0.3 : 0.85);

export interface SimView {
  n: string;
  automated: string;
  escalated: string;
  cost: string | null;
  rules: { id: string; fired: string; wrong: string; unlabeled: string; rate: string; ci95: string }[];
}

/** One row of POST simulate, as the page shows it. Formatting only. */
export function simView(m: PolicyMetrics): SimView {
  return {
    n: count(m.n),
    automated: pct(m.automated),
    escalated: pct(m.escalated),
    cost: m.expected_cost_per_1000 == null ? null : usd(m.expected_cost_per_1000),
    rules: Object.entries(m.rules).map(([id, r]) => ({
      id,
      fired: count(r.fired),
      wrong: count(r.wrong),
      unlabeled: count(r.unlabeled),
      rate: pct(r.rate, 2),
      ci95: interval(r.ci95),
    })),
  };
}

/** A rule in words: "ACCEPT when p < 0.30". */
export const ruleText = (r: Rule): string =>
  `${r.action} when ${scalarName(r.scalar)} ${r.op === "lt" ? "<" : "≥"} ${prob(r.threshold)}`;

/** Names for a choice's options or a score's levels, from the question definition. */
export function optionNames(def?: { options?: { value: string | boolean }[]; levels?: { label: string | null }[] }): string[] | null {
  if (def?.options) return def.options.map((o) => String(o.value));
  if (def?.levels) return def.levels.map((l, i) => l.label ?? `level ${i}`);
  return null;
}

/** The record's leading probability as the table shows it: "yes 0.12", "billing 0.71". */
export function top(
  rec: { type: "noul" | "choice" | "score"; probabilities: number[] | null; refusal?: boolean },
  names: string[] | null,
): { label: string; p: number | null } {
  const ps = rec.probabilities;
  if (!ps || rec.refusal) return { label: "refused", p: null };
  if (rec.type === "noul") return { label: `yes ${prob(ps[0])}`, p: ps[0] };
  let i = 0;
  ps.forEach((p, j) => p > ps[i] && (i = j));
  const name = names?.[i] ?? (rec.type === "score" ? `level ${i}` : `#${i + 1}`);
  return { label: `${name} ${rec.type === "score" ? "· " : ""}${prob(ps[i])}`, p: ps[i] };
}

/** Only absolute http(s) links from data are rendered (threat E3 in the architecture). */
export const safeHref = (url: string | null | undefined): string | null =>
  url && /^https?:\/\//i.test(url) ? url : null;

export function ago(iso: string, now = Date.now()): string {
  const s = Math.max(0, (now - Date.parse(iso)) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export const day = (iso: string): string =>
  new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
