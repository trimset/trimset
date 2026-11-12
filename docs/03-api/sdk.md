# SDK interface

The Python SDK ships in v0.1 (`pip install trimset`). The TypeScript SDK follows in
v0.2, with the same behaviour (§8). Both must reproduce [hashing.md](hashing.md)'s test
vectors and the gate examples in [vectors/gates.json](vectors/gates.json): a bundle, a
client configuration, 24 `gate()` calls and the result of each, computed with the numpy
code the compiler certifies with (`vectors/gates.py` writes and checks it).

## 1. Client

```python
import trimset

ts = trimset.Client(
    base_url="http://localhost:8800",
    key="tsi_…",                        # an ingest key
    defaults={"refund_fraud": "ESCALATE", "request_difficulty": "ACCEPT"},
    default_model="jev-1.14.0",         # used when gate() gets no model_served
)
```

| Option | Default | Meaning |
|---|---|---|
| `base_url`, `key` | required | The server and an ingest key (`tsi_`). |
| `defaults` | `{}` | The action for each question when no policy applies ([ADR 0012](../adr/0012-sdk-fail-open.md)). The library default is `ESCALATE`, with a warning logged once per question. |
| `default_model` | `None` | Used by `gate()` when no `model_served` is given. Pin it only when the app pins its model version too. |
| `poll_interval` / `poll_jitter` | `30.0` s / `0.2` | Policy polling ([ADR 0008](../adr/0008-policy-distribution.md)). |
| `max_policy_age` | `None` | When set, a policy older than this is no longer used (reason `stale_policy`). |
| `cache_path` | `None` | Disk cache for the last good bundle, written atomically and checked when loaded. |
| `buffer_size` | `10000` | Records waiting to be sent. When full, the oldest is dropped and counted. |
| `batch_size` / `flush_interval` | `100` / `1.0` s | When a batch is sent. |
| `backoff` | `0.5` → `60` s | Exponential backoff with full jitter. |
| `timeout` | `5.0` s | Per HTTP call, on the background thread only. |

The only dependency is `rfc8785` (pure Python). Supports CPython 3.11 and newer.

## 2. Methods

```python
d = ts.gate("refund_fraud", p=0.83, model_served="jev-1.14.0")
d = ts.gate("team", answer=resp["answers"]["team"], model_served=resp["model"],
            decision_id=decision_ids[0])      # proxy mode: id from X-Trimset-Decision-Id
ts.log_exchange(request, response, format="systemone", gates={"fraud": d},
                external_ref="rf-1001", tags={"channel": "web"}, latency_ms=41)
ts.log(record)                                 # one record/1 dict, for clients doing their own thing
ts.outcome(external_ref="rf-1001", question="refund_fraud", label=True, source="downstream")
ts.wait_for_policies(timeout=2.0)              # the only call that may wait on the network
ts.flush(timeout=2.0); ts.close()                # ts.dropped: items lost so far
```

| Method | Returns | Never… |
|---|---|---|
| `gate(question, *, p=None, probabilities=None, answer=None, model_served=None, decision_id=None, question_def=None, default=None)` | `GateResult` | raises, or touches the network. |
| `log_exchange(request, response, *, format, gates=None, external_ref=None, tags=None, latency_ms=None)` | `list[str]`: decision ids, in question order | sends raw state: the state is hashed on the calling thread, then dropped. |
| `instrument(model_callable, *, model, format="systemone")` | the same callable, logging every call as `log_exchange` would | changes the model's answers; a logging failure never reaches the caller. Bare answers (wire-formats.md §2) are wrapped, and `model` is the served model, `<repo>@<revision>`. |
| `log(record)` | `None` | blocks. No question definition goes with it, so the server must already know the question (from the proxy or a `log_exchange`). |
| `outcome(*, decision_id=None, external_ref=None, question=None, label, source, observed_at=None, outcome_id=None)` | `None` | blocks. |

**Inputs to `gate()`:**
- `question` is a name from the bundle, or a `question_id`.
- Give exactly one of:
  - `p` (noul);
  - `probabilities` (a list in option or level order);
  - `answer` (the vendor's answer object, in either format).
- `question_def` (the question as sent to the model) is optional. When given, the SDK
  hashes it, and a mismatch with the bundle returns the default with reason
  `question_changed`.

**When `gate()` gets a `decision_id`** (from the proxy's header, or from one it made
earlier), it queues an action report (`POST /v1/decisions:actions`). The server then
learns which policy decided. Without one, `gate()` creates a UUIDv7 and returns it, so
that `log_exchange(..., gates=…)` can link the two.

**`GateResult`:**

```python
GateResult(action="ESCALATE", label=None, source="policy", reason=None,
           policy_version=7, policy_fingerprint="sha256:32d8…", decision_id="019a6f4e-…",
           audit=False, scalar=0.6695)
```

| Field | Values |
|---|---|
| `action` | `ACCEPT`, `BLOCK`, `ESCALATE`, or `SELECT:<value>`: a string value as it is, a boolean as `true` or `false`. `label` holds the value, also when an option in `option_actions` turns a SELECT into `BLOCK`. |
| `source` | `policy`, `audit` or `default` |
| `reason`, when the source is `default` | `no_policy`, `model_unknown`, `model_mismatch`, `question_changed`, `invalid_probabilities`, `fingerprint_mismatch`, `stale_policy`, `refusal` |

## 3. Applying a policy

The algorithm every SDK implements exactly, in float64:

1. **Find the question:** a name or id from the bundle. If unknown: default, `no_policy`.
2. **Get the probabilities:** from `p`, `probabilities` or `answer`, as in
   [wire-formats.md](wire-formats.md) §3.
   - A refusal: default, `refusal`.
   - Wrong length, a value outside [0, 1], or a sum off by more than 1e-3: default,
     `invalid_probabilities`.
   - Otherwise divide by the sum, so the probabilities add up to exactly 1.
3. **Find the policy** for (`question_id`, `model_served`):
   - No model, and no `default_model`: default, `model_unknown`.
   - A policy exists for this question but not for this model: default,
     `model_mismatch`.
   - No policy at all: default, `no_policy`.
4. **Audit sampling** (FR-37): if `audit_fraction > 0` and there is a `decision_id`,
   let `u = int(sha256(decision_id.lower().encode()).hexdigest()[:16], 16) / 2**64`. If
   `u < audit_fraction`, return `ESCALATE` with source `audit` and `audit=True`.
5. **Calibrate**, with p clamped to [1e-6, 1 − 1e-6] and `logit(p) = ln(p / (1 − p))`:
   - `identity`: s = p.
   - `temperature`:
     - noul: s = σ(logit(p) / T);
     - choice and score: sᵢ = softmax(ln pᵢ / T).
   - `platt` (noul): s = 1 / (1 + exp(a · logit(p) + b)).
   - `isotonic` (noul): linear interpolation between the breakpoints (x, y); outside them,
     the nearest end value (numpy `interp`).
6. **Work out the scalar each rule reads:**
   - `p` = s (noul);
   - `top` = max sᵢ, with the top option = the first option holding that maximum;
   - `{tail_ge: k}` = Σ sᵢ for i ≥ k.
7. **Check the rules in order:** `lt` fires when scalar < threshold, `ge` when
   scalar ≥ threshold. For a choice `select` rule, use `per_option[top option]` if it is
   there. The first rule that fires decides:
   - `ACCEPT`;
   - `BLOCK`;
   - `SELECT:<top option>`, or `BLOCK` if the top option is in `option_actions`.
8. **No rule fires:** `fallback_action` (`ESCALATE`).

`scalar` in the result is what the deciding rule read, or the first rule's when none
fired. Compiled thresholds never sit on a tuning value ([ADR 0006](../adr/0006-policy-compiler.md)),
so an implementation whose float arithmetic differs from numpy's by an ulp decides the
same; the Python SDK is checked against the compiler on random policies.

**Example:**
- **Policy:** [examples/policy.json](examples/policy.json), Platt with a = −0.71 and
  b = 0.42.
- **Input:** p = 0.83.
- **Calibration:** s = 0.6695.
- **Rules:** `accept` (s < 0.30) doesn't fire, and neither does `block` (s ≥ 0.85).
- **Result:** `ESCALATE`, source `policy`, version 7.

## 4. Policy cache

- **Polling:** a background thread polls `GET /v1/policies` with `If-None-Match`, every
  `poll_interval` ± `poll_jitter`.
- **On a new bundle:**
  1. Recompute every policy's fingerprint ([hashing.md](hashing.md) §4).
  2. Keep the policies that match. Drop any that don't, with reason
     `fingerprint_mismatch`, and keep the last good version of those.
  3. Swap in one immutable snapshot. `gate()` reads that snapshot without locks.
- **Failures:** keep the last good bundle and retry with backoff. The policy's age (since
  the server last confirmed it, a 304 included) is `client.policy_age_seconds`, exported
  as `trimset_policy_age_seconds` once the SDK has metrics.
- **Forks:** after a fork (gunicorn `preload`), the first call in the child notices the
  new process id and starts a fresh thread.

## 5. Sending records, actions and outcomes

- One background thread, with one bounded queue per kind (records, actions, outcomes).
- A batch goes when it holds `batch_size` items or `flush_interval` has passed.
- Retries on network errors, 429 and 5xx use exponential backoff with full jitter. A 4xx
  (other than 429) drops the batch, logs one warning, and counts the drop.
- Each log batch carries `client: {sdk, version, dropped_since_last}`.
- At exit, the SDK flushes for at most 2 s.
- Nothing the SDK logs ever contains a state, a key, or tag values.

## 6. Outcomes

`outcome()` checks only the shape of what it is given: a `decision_id`, or an
`external_ref` plus a question. The server checks the label against the question's type.
`observed_at` defaults to now.

## 7. Examples

**Proxy mode** (the app keeps its own decision-model client):

```python
resp = typesafe_client.post("/v1/systemone", json=req)   # base URL points at trimset
ids = resp.headers["X-Trimset-Decision-Id"].split(",")
d = ts.gate("refund_fraud", answer=resp.json()["answers"]["fraud"],
            model_served=resp.json()["model"], decision_id=ids[0])
```

**SDK-only mode** (the app calls the model directly):

```python
d = ts.gate("refund_fraud", answer=answers["fraud"], model_served=model)
ts.log_exchange(req, resp, format="systemone", gates={"fraud": d}, external_ref=refund.id)
```

**In-process mode** (the model runs in the app, for example a Hugging Face pipeline):

```python
decide = ts.instrument(pipeline("decision", model="autotrust/GEV-26B-Decide", revision=REV),
                       model=f"autotrust/GEV-26B-Decide@{REV}")
answers = decide({"state": message, "questions": QUESTIONS})   # logged on a background thread
d = ts.gate("department", answer=answers["department"], model_served=f"autotrust/GEV-26B-Decide@{REV}")
```

## 8. TypeScript (v0.2)

The same names in camelCase, and the same results:
- `gate()` is synchronous.
- `log*()`, `outcome()` and `flush()` return Promises, and never reject.
- **Runtimes:** server-side only (Node 20+, Bun, Deno), because a key must never ship to
  a browser.
- **Hashing:** `node:crypto` for SHA-256 and HMAC, because `gate()` must stay
  synchronous; `canonicalize` 5.1.0 for RFC 8785.
- **Acceptance:** the same vectors and `vectors/gates.json` must pass.
