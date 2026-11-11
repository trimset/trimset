// NFR-03: the proxy adds under 5 ms at p99 over calling the mock upstream directly, at
// 500 requests/s with logging on. The two runs go one after the other, so they never
// compete. Run by .github/workflows/load.yml, never on a laptop.
import http from "k6/http";
import { check } from "k6";

const DURATION = __ENV.DURATION || "5m";
const RATE = Number(__ENV.RATE || 500); // NFR-03 is 500; lower or higher rates find the knee
const DIRECT = `${__ENV.MOCK_URL}/v1/systemone`;
const PROXIED = `${__ENV.TRIMSET_URL}/p/${__ENV.TRIMSET_PROXY_KEY}/v1/systemone`;
const PARAMS = { headers: { "Content-Type": "application/json" } };

function seconds(text) {
  const m = /^(\d+)([sm])$/.exec(text);
  if (!m) throw new Error(`DURATION must look like 90s or 5m, not ${text}`);
  return Number(m[1]) * (m[2] === "m" ? 60 : 1);
}

const run = {
  executor: "constant-arrival-rate",
  rate: RATE,
  timeUnit: "1s",
  duration: DURATION,
  preAllocatedVUs: 100,
  maxVUs: 500,
};

export const options = {
  scenarios: {
    direct: { ...run, exec: "direct" },
    proxy: { ...run, exec: "proxied", startTime: `${seconds(DURATION) + 15}s` },
  },
  // Listed so the summary carries each run's own numbers; the 5 ms check is made on them
  // below.
  thresholds: {
    "http_req_duration{scenario:direct}": ["p(99)<1000"],
    "http_req_duration{scenario:proxy}": ["p(99)<1000"],
    http_req_failed: ["rate<0.001"],
  },
  summaryTrendStats: ["avg", "p(50)", "p(95)", "p(99)", "max"],
};

function body() {
  return JSON.stringify({
    model: "mock-latest",
    state: { refund_id: `rf-${Math.floor(Math.random() * 1e9)}`, amount: Math.random() * 500 },
    questions: {
      refund_fraud: { type: "noul", instructions: "Is this refund request fraudulent?" },
      team: {
        type: "choice",
        instructions: "Which team should handle this ticket?",
        criteria: { billing: null, shipping: null, returns: null, tech: null },
      },
    },
  });
}

export function direct() {
  check(http.post(DIRECT, body(), PARAMS), { "200": (r) => r.status === 200 });
}

export function proxied() {
  const r = http.post(PROXIED, body(), PARAMS);
  check(r, {
    "200": (res) => res.status === 200,
    "decision ids": (res) => (res.headers["X-Trimset-Decision-Id"] || "").split(",").length === 2,
  });
}

export function handleSummary(data) {
  const p99 = (name) => data.metrics[`http_req_duration{scenario:${name}}`].values["p(99)"];
  const result = {
    rate: RATE,
    direct_p99_ms: p99("direct"),
    proxy_p99_ms: p99("proxy"),
    overhead_p99_ms: p99("proxy") - p99("direct"),
    requests: data.metrics.http_reqs.values.count,
    failed_rate: data.metrics.http_req_failed.values.rate,
    checks_passed_rate: data.metrics.checks.values.rate,
  };
  return {
    "/scripts/out/proxy.json": JSON.stringify(result, null, 2),
    stdout: JSON.stringify(result, null, 2) + "\n",
  };
}
