import assert from "node:assert/strict";
import { test } from "node:test";
import type { PolicyMetrics, Rule } from "./api.ts";
import { candidate, safeHref, simView, top } from "./format.ts";

const metrics: PolicyMetrics = {
  n: 12400,
  automated: 0.951,
  escalated: 0.049,
  rules: {
    accept: { fired: 9800, wrong: 121, unlabeled: 4, rate: 0.012347, ci95: [0.0102, 0.0147] },
    block: { fired: 2000, wrong: 0, unlabeled: 0, rate: null, ci95: null },
  },
  expected_cost_per_1000: 1212.4,
};

test("the simulator shows the server's numbers, formatted and nothing else", () => {
  assert.deepEqual(simView(metrics), {
    n: "12,400",
    automated: "95.1%",
    escalated: "4.9%",
    cost: "$1,212",
    rules: [
      { id: "accept", fired: "9,800", wrong: "121", unlabeled: "4", rate: "1.23%", ci95: "1.02% – 1.47%" },
      { id: "block", fired: "2,000", wrong: "0", unlabeled: "0", rate: "–", ci95: "–" },
    ],
  });
  assert.equal(simView({ ...metrics, expected_cost_per_1000: null }).cost, null);
});

test("a candidate keeps the server's rule shapes and changes only thresholds", () => {
  const shapes: Rule[] = [
    { id: "accept", action: "ACCEPT", scalar: "p", op: "lt", threshold: 0.11 },
    { id: "block", action: "BLOCK", scalar: { tail_ge: 2 }, op: "ge", threshold: 0.9 },
  ];
  assert.deepEqual(candidate(shapes, [0.3, 0.854999999]), [
    { id: "accept", action: "ACCEPT", scalar: "p", op: "lt", threshold: 0.3 },
    { id: "block", action: "BLOCK", scalar: { tail_ge: 2 }, op: "ge", threshold: 0.855 },
  ]);
  assert.equal(shapes[0].threshold, 0.11);
});

test("the top probability names the option or level", () => {
  assert.deepEqual(top({ type: "noul", probabilities: [0.834] }, null), { label: "yes 0.83", p: 0.834 });
  assert.deepEqual(top({ type: "choice", probabilities: [0.1, 0.71, 0.19] }, ["a", "billing", "c"]), { label: "billing 0.71", p: 0.71 });
  assert.deepEqual(top({ type: "score", probabilities: [0.2, 0.64, 0.16] }, null), { label: "level 1 · 0.64", p: 0.64 });
  assert.deepEqual(top({ type: "choice", probabilities: null, refusal: true }, null), { label: "refused", p: null });
});

test("links from data render only when they are http(s)", () => {
  assert.equal(safeHref("https://shop.example/refunds/rf-1"), "https://shop.example/refunds/rf-1");
  assert.equal(safeHref("javascript:alert(1)"), null);
  assert.equal(safeHref(" https://x.example"), null);
  assert.equal(safeHref(null), null);
});
