// NFR-02: POST /v1/decisions:log at 2,000 decisions/s for 10 minutes, in batches of 1 to
// 100, with p99 under 20 ms. Run by .github/workflows/load.yml, never on a laptop.
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const BASE = __ENV.TRIMSET_URL;
const HEADERS = {
  Authorization: `Bearer ${__ENV.TRIMSET_INGEST_KEY}`,
  "Content-Type": "application/json",
};
const QUESTION = JSON.parse(open("./question.json"));
const stored = new Counter("records_stored");
const rejected = new Counter("records_rejected");

export const options = {
  scenarios: {
    ingest: {
      executor: "constant-arrival-rate",
      rate: 40, // requests a second; batches average 50.5 records, so about 2,020 decisions/s
      timeUnit: "1s",
      duration: __ENV.DURATION || "10m",
      preAllocatedVUs: 50,
      maxVUs: 200,
    },
  },
  thresholds: {
    http_req_duration: ["p(99)<20"],
    http_req_failed: ["rate==0"],
    records_rejected: ["count==0"],
  },
  summaryTrendStats: ["avg", "p(50)", "p(95)", "p(99)", "max"],
};

function uuid4() {
  const hex = [];
  for (let i = 0; i < 32; i++) hex.push(Math.floor(Math.random() * 16).toString(16));
  hex[12] = "4";
  hex[16] = ((parseInt(hex[16], 16) & 0x3) | 0x8).toString(16);
  const h = hex.join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function record() {
  return {
    schema: "record/1",
    decision_id: uuid4(),
    ts: new Date().toISOString(),
    question_id: QUESTION.question_id,
    question_name: "refund_fraud",
    type: "noul",
    model_requested: "jev-latest",
    model_served: "jev-1.14.0",
    probabilities: [Math.random()],
    latency_ms: 40 + Math.random() * 200,
    external_ref: `rf-${uuid4().slice(0, 8)}`,
  };
}

function post(records, questions) {
  return http.post(`${BASE}/v1/decisions:log`, JSON.stringify({ questions, records }), {
    headers: HEADERS,
  });
}

// Registers the question once, as the SDK does; later batches name it by id only. Its one
// record is not counted, so the database should hold records_stored + 1.
export function setup() {
  const r = post([record()], { [QUESTION.question_id]: QUESTION.definition });
  if (r.status !== 200) throw new Error(`setup failed: ${r.status} ${r.body}`);
}

export default function () {
  const batch = Array.from({ length: 1 + Math.floor(Math.random() * 100) }, record);
  const r = post(batch, {});
  const ok = check(r, { "200": (res) => res.status === 200 });
  if (ok) {
    const body = r.json();
    stored.add(body.stored);
    rejected.add(body.rejected);
  }
}

export function handleSummary(data) {
  const d = data.metrics.http_req_duration.values;
  const result = {
    requests: data.metrics.http_reqs.values.count,
    records_stored: data.metrics.records_stored ? data.metrics.records_stored.values.count : 0,
    decisions_per_s: data.metrics.records_stored
      ? data.metrics.records_stored.values.rate
      : 0,
    p50_ms: d["p(50)"],
    p95_ms: d["p(95)"],
    p99_ms: d["p(99)"],
    max_ms: d.max,
    failed_rate: data.metrics.http_req_failed.values.rate,
  };
  return {
    "/scripts/out/ingest.json": JSON.stringify(result, null, 2),
    stdout: JSON.stringify(result, null, 2) + "\n",
  };
}
