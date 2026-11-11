// ============================================================================
// MOCK LAYER: sample data from the dashboard design, for screens whose endpoints
// don't exist yet. Every number, id and name here is illustrative; people are
// made up and emails use example.com. Each export names the endpoint that
// replaces it: when that endpoint ships, the screen calls api() instead and the
// matching export here is deleted. Nothing in this file is ever sent anywhere.
// ============================================================================
import type { Severity } from "./ui.tsx";

const sample = <T>(data: T): Promise<T> => Promise.resolve(structuredClone(data));

export const ENDPOINT = {
  overview: "GET /v1/admin/decisions (aggregates)",
  questions: "GET /v1/admin/questions",
  outcomes: "an outcomes feed",
  drift: "GET /v1/admin/drift-reports",
  shadow: "GET /v1/admin/questions/{question}/shadow",
  review: "GET /v1/review/items",
  alerts: "GET /v1/admin/alerts",
} as const;

type Health = { tone: "ok" | "wa" | "cr" | "mu"; text: string };

export interface QuestionRow {
  name: string;
  wording: string;
  note?: string;
  type: string;
  question_id: string;
  models: { name: string; isNew?: string }[];
  decisions24h: number;
  decisions30d: number;
  automated: number;
  outcomesKnown: number;
  ece: number | null;
  policy: string;
  health: Health;
  spark: number[];
}

const QUESTIONS: QuestionRow[] = [
  { name: "refund_fraud", wording: "Is this refund request fraudulent?", note: "2 earlier wordings", type: "noul", question_id: "q_534a3508f2d8…fcc3", models: [{ name: "jev-1.13.0" }, { name: "jev-1.12.2" }], decisions24h: 12480, decisions30d: 371902, automated: 0.882, outcomesKnown: 0.41, ece: 0.018, policy: "v7", health: { tone: "ok", text: "✓ Healthy" }, spark: [9, 11, 10, 14, 13, 15, 16] },
  { name: "ticket_triage", wording: "Which team should handle this ticket?", note: "billing · shipping · returns · tech", type: "choice · 4", question_id: "q_9b17c0e4aa51…20d7", models: [{ name: "jev-1.13.0" }, { name: "jev-1.14.0", isNew: "new 3 h ago" }], decisions24h: 9902, decisions30d: 296415, automated: 0.846, outcomesKnown: 0.931, ece: 0.031, policy: "v4", health: { tone: "wa", text: "▲ Drift" }, spark: [12, 12, 11, 12, 12, 6, 5] },
  { name: "request_difficulty", wording: "How hard is this request?", note: "levels 0–3 · small or large model", type: "score · 4", question_id: "q_e02b6f71c9d3…8a44", models: [{ name: "laya:latest" }], decisions24h: 18775, decisions30d: 561230, automated: 0.96, outcomesKnown: 0.021, ece: 0.026, policy: "v2", health: { tone: "ok", text: "✓ Healthy" }, spark: [14, 15, 13, 15, 16, 15, 17] },
  { name: "personal_data", wording: "Does this text contain personal data?", type: "noul", question_id: "q_41d0a9be7f26…13c5", models: [{ name: "gpt-6-luna" }], decisions24h: 5214, decisions30d: 158004, automated: 0.928, outcomesKnown: 0.05, ece: 0.022, policy: "v3", health: { tone: "ok", text: "✓ Healthy" }, spark: [10, 9, 11, 10, 10, 11, 11] },
  { name: "safe_to_run", wording: "Is this tool call safe to run without a human?", type: "noul", question_id: "q_7f3e215cd08b…e9a1", models: [{ name: "jev-1.13.0" }], decisions24h: 611, decisions30d: 18330, automated: 0.715, outcomesKnown: 0.284, ece: null, policy: "draft", health: { tone: "mu", text: "◷ n = 412 labelled: 188 more for a 0.5% guarantee" }, spark: [4, 6, 5, 8, 9, 10, 12] },
  { name: "listing_severity", wording: "How serious is the problem with this listing?", note: "none · mild · serious · remove", type: "score · 4", question_id: "q_c8a40d2e95f1…5b72", models: [{ name: "laya:latest" }], decisions24h: 1231, decisions30d: 36912, automated: 0.79, outcomesKnown: 0.22, ece: 0.044, policy: "v1", health: { tone: "cr", text: "✕ Queue over cap" }, spark: [8, 9, 12, 14, 16, 18, 20] },
];

export const questions = () => sample(QUESTIONS);

export const overview = () =>
  sample({
    decisionsToday: 48213,
    vsYesterday: "+6.1%",
    automated: 0.914,
    escalated: 0.086,
    coverage30d: 0.623,
    activePolicies: [5, 6],
    openAlerts: { total: 3, critical: 1, warnings: 2 },
    questions: QUESTIONS,
    alerts: [
      { severity: "critical" as Severity, text: "Review queue over 2,000 a day", question: "listing_severity", ago: "14 min ago" },
      { severity: "warning" as Severity, text: "jev-latest now answers as jev-1.14.0", question: "ticket_triage", ago: "3 h ago" },
      { severity: "warning" as Severity, text: "PSI 0.27 over 0.20 for 2 windows", question: "ticket_triage", ago: "2 h ago" },
    ],
    outcomes24h: { joined: 3904, corrections: 212, held: 58, byRule: 1640, sources: { human: 0.31, downstream: 0.27, rule: 0.42 } },
  });

export const outcomes = () =>
  sample({
    tiles: { joined: 3904, corrections: 212, held: 58, byRule: 1640, medianDelay: "54 d" },
    feed: [
      { at: "14:33:10", question: "refund_fraud", by: "external_ref rf-18722", label: "fraud", source: "Downstream signal", delay: "61 d 4 h", correction: "was not fraud (rule)", note: "", sender: "tsi_9fK2… chargebacks" },
      { at: "14:32:58", question: "ticket_triage", by: "external_ref T-88013", label: "returns", source: "Downstream signal", delay: "5 h 12 m", correction: "", note: "New", sender: "tsi_4bQx… helpdesk" },
      { at: "14:32:41", question: "refund_fraud", by: "decision_id 019a6f4e…5e6f", label: "not fraud", source: "Human review", delay: "1 h 46 m", correction: "was fraud", note: "", sender: "r.ito@example.com" },
      { at: "14:32:30", question: "request_difficulty", by: "decision_id 019a6f52…77c0", label: "level 2", source: "Downstream signal", delay: "6 m", correction: "", note: "New · thumbs-down", sender: "tsi_1aZr… chat-app" },
      { at: "14:31:55", question: "personal_data", by: "decision_id 019a6e10…a1d4", label: "no", source: "Human review", delay: "3 d 2 h", correction: "", note: "New · weekly audit", sender: "p.okafor@example.com" },
      { at: "14:30:00", question: "refund_fraud", by: "412 decisions", label: "not fraud", source: "Rule", delay: "120 d", correction: "", note: "New · no chargeback in 120 days", sender: "worker" },
      { at: "14:29:47", question: "listing_severity", by: "decision_id 019a6f3a…0b19", label: "serious", source: "Human review", delay: "2 h 03 m", correction: "", note: "New", sender: "moderator.lee@example.com" },
      { at: "14:29:12", question: "safe_to_run", by: "decision_id 019a6f4d…9e02", label: "unsafe", source: "Human review", delay: "4 m", correction: "", note: "New · escalated call denied", sender: "ops.kim@example.com" },
    ],
    held: [
      { at: "14:20:31", question: "ticket_triage", ref: "T-90114", label: "billing", source: "Downstream signal", expires: "29 d 23 h", soon: false, reason: "Ticket closed before its routing call was logged" },
      { at: "13:58:02", question: "refund_fraud", ref: "rf-20455", label: "fraud", source: "Downstream signal", expires: "29 d 23 h", soon: false, reason: "Refund not sent through trimset" },
      { at: "Nov 3", question: "refund_fraud", ref: "RF-17730", label: "fraud", source: "Downstream signal", expires: "22 d", soon: true, reason: "Upper case: the app logs rf-17730" },
    ],
  });

export const drift = () =>
  sample({
    question: "ticket_triage",
    change: { alias: "jev-latest", from: "jev-1.13.0", to: "jev-1.14.0", since: "Nov 10, 21:04 UTC", index: 12 },
    days: ["Oct 29", "Oct 30", "Oct 31", "Nov 1", "Nov 2", "Nov 3", "Nov 4", "Nov 5", "Nov 6", "Nov 7", "Nov 8", "Nov 9", "Nov 10", "Nov 11"],
    psi: { alert: 0.2, values: [0.04, 0.05, 0.06, 0.05, 0.04, 0.06, 0.05, 0.07, 0.05, 0.04, 0.06, 0.05, 0.24, 0.27], significant: [12, 13] },
    js: { alert: 0.05, values: [0.01, 0.012, 0.009, 0.011, 0.01, 0.013, 0.01, 0.012, 0.011, 0.009, 0.01, 0.012, 0.061, 0.068], significant: [12, 13] },
    hist: { reference: [0.01, 0.01, 0.02, 0.03, 0.04, 0.05, 0.07, 0.12, 0.25, 0.4], now: [0.01, 0.02, 0.04, 0.07, 0.11, 0.14, 0.16, 0.16, 0.15, 0.14] },
    panels: [
      { label: "Escalation rate", value: "15.4%", note: "▲ from 8.1%", tone: "wa" },
      { label: "Errors, auto-routed", value: "4.1%", note: "from 3.2% · n = 1,904 labelled", tone: "t2" },
      { label: "Latency p50 / p99", value: "49 / 140 ms", note: "unchanged", tone: "t2" },
      { label: "Calibration error (ECE)", value: "0.058", note: "▲ from 0.031 · 2,210 labels so far", tone: "wa" },
    ],
  });

export const shadow = () =>
  sample({
    question_id: "q_9b17c0e4aa51…20d7",
    question: "ticket_triage",
    primary_model: "jev-1.13.0",
    candidate_model: "jev-1.14.0",
    since: "Nov 10, 21:04 UTC",
    pairs: 21480,
    labeled_pairs: 3112,
    agreement: 0.912,
    flip_rate: 0.088,
    accuracy: { primary: 0.968, candidate: 0.959, delta: "−0.9 pp [−1.6, −0.2]" },
    mcnemar: { both_right: 2941, b: 72, c: 44, both_wrong: 55, p_value: 0.012 },
    cost_per_1000: { primary: 0.34, candidate: 0.38 },
    latency: { primary: "49 / 140", candidate: "46 / 152", delta: "−3 / +12 ms" },
    flips: [
      { primary: "shipping", candidate: "returns", n: 612, candidateRight: "31%", tone: "bad" },
      { primary: "billing", candidate: "returns", n: 488, candidateRight: "38%", tone: "bad" },
      { primary: "tech", candidate: "billing", n: 301, candidateRight: "64%", tone: "ok" },
      { primary: "returns", candidate: "shipping", n: 255, candidateRight: "52%", tone: "" },
    ],
    other: { pairs: 8, n: 234 },
  });

export interface ReviewItem {
  item_id: string;
  priority: "high" | "normal" | "audit";
  question: string;
  wording: string;
  waiting: string;
  summary: string;
  ref: string;
  decided: string;
  labels: string[];
  probs: [string, number][];
  link: string | null;
}

export const review = () =>
  sample({
    labelledToday: 46,
    total: 37,
    items: [
      { item_id: "r1", priority: "high", question: "ticket_triage", wording: "Which team should handle this ticket?", waiting: "2 h 10 m", summary: "returns 0.41 · shipping 0.38 · escalated", ref: "T-90187", decided: "decided 12:21 UTC by policy v4", labels: ["billing", "shipping", "returns", "tech"], probs: [["returns", 0.41], ["shipping", 0.38], ["billing", 0.15], ["tech", 0.06]], link: "https://helpdesk.example.com/tickets/T-90187" },
      { item_id: "r2", priority: "high", question: "refund_fraud", wording: "Is this refund request fraudulent?", waiting: "1 h 52 m", summary: "P(fraud) 0.79 · $480 refund", ref: "rf-20402", decided: "decided 12:39 UTC by policy v7", labels: ["fraud", "not fraud"], probs: [["fraud", 0.79], ["not fraud", 0.21]], link: "https://shop.example.com/admin/refunds/rf-20402" },
      { item_id: "r3", priority: "normal", question: "refund_fraud", wording: "Is this refund request fraudulent?", waiting: "58 m", summary: "P(fraud) 0.52 · escalated", ref: "rf-20417", decided: "decided 13:33 UTC by policy v7", labels: ["fraud", "not fraud"], probs: [["fraud", 0.52], ["not fraud", 0.48]], link: "https://shop.example.com/admin/refunds/rf-20417" },
      { item_id: "r4", priority: "normal", question: "listing_severity", wording: "How serious is the problem with this listing?", waiting: "41 m", summary: "mild 0.47 · serious 0.31", ref: "L-55021", decided: "decided 13:50 UTC by policy v1", labels: ["none", "mild", "serious", "remove"], probs: [["none", 0.12], ["mild", 0.47], ["serious", 0.31], ["remove", 0.1]], link: null },
      { item_id: "r5", priority: "audit", question: "refund_fraud", wording: "Is this refund request fraudulent?", waiting: "12 m", summary: "P(fraud) 0.04 · 1% audit sample of ACCEPT", ref: "rf-20433", decided: "decided 14:19 UTC by policy v7", labels: ["fraud", "not fraud"], probs: [["fraud", 0.04], ["not fraud", 0.96]], link: "https://shop.example.com/admin/refunds/rf-20433" },
    ] as ReviewItem[],
  });

export interface AlertRow {
  alert_id: string;
  severity: Severity;
  summary: string;
  detail?: string;
  question: string | null;
  status: "open" | "acknowledged" | "resolved";
  statusNote?: string;
  opened: string;
  link?: string;
}

export const alerts = () =>
  sample({
    items: [
      { alert_id: "a1", severity: "critical", summary: "Review queue over 2,000 a day", detail: "2,310", question: "listing_severity", status: "open", opened: "14:18" },
      { alert_id: "a2", severity: "warning", summary: "jev-latest answers as a new version, jev-1.14.0", question: "ticket_triage", status: "open", opened: "11:20" },
      { alert_id: "a3", severity: "warning", summary: "PSI over 0.20 for 2 windows", detail: "0.27", question: "ticket_triage", status: "acknowledged", statusNote: "ops.kim@example.com", opened: "12:05", link: "#/drift" },
      { alert_id: "a4", severity: "info", summary: "Guarantee needs 188 more labels", question: "safe_to_run", status: "open", opened: "Nov 9" },
      { alert_id: "a5", severity: "critical", summary: "Upstream errors over 5% for 5 min", detail: "503s", question: null, status: "resolved", statusNote: "after 7 min", opened: "Nov 8" },
    ] as AlertRow[],
    rules: [
      ["Review queue per day", "listing_severity", "> 2,000", "Slack"],
      ["Model version changed", null, "any", "Slack"],
      ["PSI, top probability", null, "> 0.20 × 2", "Slack"],
      ["Upstream error rate", null, "> 5% · 5 min", "Webhook"],
      ["ECE", "refund_fraud", "> 0.05", "Email"],
    ] as [string, string | null, string, string][],
    channels: [
      { kind: "Slack", name: "#ml-oncall" },
      { kind: "Webhook", name: "pager" },
      { kind: "Email", name: "ml-team@example.com" },
    ],
  });
