# Wire formats

How the proxy (and the SDK's `log_exchange`) turns an upstream request and response into
decision records ([record/1](schemas/record.schema.json)).

## 1. Sources

Read on 2026-11-10 from each vendor's own documentation:
- **TypeSafe:** [API reference](https://docs.typesafe.ai/api); the
  [noul](https://docs.typesafe.ai/primitives/noul.md),
  [choice](https://docs.typesafe.ai/primitives/choice.md) and
  [score](https://docs.typesafe.ai/primitives/score.md) pages; and
  [models](https://docs.typesafe.ai/models.md).
- **OpenAI:** the [Decisions guide](https://developers.openai.com/api/docs/guides/decisions)
  and the [create reference](https://developers.openai.com/api/reference/resources/decisions/methods/create).
- **Ollaya:** [docs/api.md](https://github.com/ollaya-dev/ollaya/blob/main/docs/api.md).
- **Ollama:** [api/systemone](https://docs.ollama.com/api/systemone.md) and
  [capabilities/decision](https://docs.ollama.com/capabilities/decision.md).

## 2. Two formats, chosen by the upstream

| Format | Upstreams | Questions | Answers |
|---|---|---|---|
| `systemone` | TypeSafe Jev (`api.typesafe.ai`), Ollaya (`:11435`), Ollama 0.35.0+ (`:11434`) | Object keyed by names the caller picks; `instructions` + `criteria` | Object keyed by the same names |
| `openai-decisions` | OpenAI (`gpt-6-luna`) | Array of 1–400; `name`, `instructions`, `choices` or `levels` | Array in question order |

The upstream's format, set by the operator with the upstream
([ADR 0011](../adr/0011-proxy-design.md)), decides the format, **not the path**. Ollaya
serves the systemone format at `/v1/decisions` too. The proxy forwards the path it
received, unchanged.

## 3. From a response to records

One record per answered question. In systemone, answers are matched to questions by
name; in OpenAI's format, by position.

| Type | systemone answer | OpenAI answer | `probabilities` in the record |
|---|---|---|---|
| noul | `{"type":"noul","noul":p}` | `{"type":"predicate","probability":p}` | `[p]`: the probability of yes |
| choice | `probabilities`: object keyed by option name | `probabilities`: array of `{value, probability}` | One per option, in the question's option order. OpenAI values are matched by JSON equality, so `true` ≠ `"true"`. |
| score | `probabilities`: object keyed `"0"`, `"1"`, … | `probabilities`: array of `{label, probability, value}` | One per level, level 0 first. OpenAI's `value` is the index. |
| refusal | — | `{"type":"refusal","name":…}` | `null`, with `refusal: true` |

**Probabilities:**
- trimset keeps the full distribution. It ignores the vendors' `confidence` (Ollaya and
  Ollama compute it with different formulas) and score's expected `score`.
- Vendors round to 4 decimals: Ollama's documented example sums to 0.9999. A record is
  valid when its probabilities sum to 1 within **1e-3**. They are stored as received and
  renormalised before any statistics or gating.
- A record whose answer is missing an option or level is rejected and counted, never
  guessed.

**Model names:**
- `model_requested` is the request's `model`; `model_served` is the response's `model`.
- What `model_served` means depends on the runtime, and so does silent-upgrade detection
  (FR-39):

| Runtime | Response `model` | Silent upgrades detectable |
|---|---|---|
| TypeSafe | The versioned id, for example `jev-1.14.0` behind `jev-latest` | **Yes.** `jev-latest` moved from 1.13.0 to 1.14.0 on 2026-10-14. |
| Ollaya | The canonical name, for example `laya:en` behind `laya` | Only when the routed tag changes. Re-pulled weights under the same tag are invisible. |
| Ollama | Echoes the requested name | No |
| OpenAI | Echoes `gpt-6-luna` | No |

Where detection is impossible, the docs tell users to pin a version, and drift checks
(FR-38) are the safety net.

**Cost:**
- `cost_usd` per request = (`input_tokens` − cached) × `input` + cached × `cached_input`
  + `output_tokens` × `output`, using the project's per-model `prices` (USD per million
  tokens).
- OpenAI reports cached tokens in `usage.input_tokens_details.cached_tokens`.
- The request's cost is split equally across its questions. A model without a price
  gives `null`.

**Latency:** `latency_ms` is the time from sending to the upstream until its full
response, the same for every decision in the request. Ollaya's `X-Ollaya-*-Duration`
headers are passed through, not recorded.

**Not logged:**
- non-2xx responses, which go to the upstream error counters (FR-40);
- `GET /v1/models`;
- requests whose body can't be parsed (forwarded anyway, and counted).

## 4. Headers

| Direction | Rule |
|---|---|
| App → upstream | Forward every end-to-end header unchanged, including `Authorization` (passthrough mode) and `TypeSafe-Danger-Zone-*`. Remove hop-by-hop headers (`Connection`, `Keep-Alive`, `TE`, `Trailer`, `Transfer-Encoding`, `Upgrade`, `Proxy-*`) and every `X-Trimset-*`. Rewrite `Host`. |
| Upstream → app | Forward every end-to-end header. Add `X-Trimset-Decision-Id`: one id per question, comma-separated, in question order (systemone: the order of the `questions` keys). |

Optional request headers read by trimset:
- `X-Trimset-Key`: header mode ([ADR 0011](../adr/0011-proxy-design.md)).
- `X-Trimset-External-Ref`: applies to every question in the request.
- `X-Trimset-Tags`: comma-separated `key=value` pairs, URL-encoded, at most 20.

## 5. Body and multipart

- **Body size:** up to **8 MiB** by default, the same as Ollaya (the project setting
  `proxy_max_body_mb`).
  This replaces the 1 MB in ADR 0011, which would have refused real image requests.
- **Multipart:** for systemone images (`multipart/form-data`), the `request` part is
  parsed as JSON. Every part is forwarded unchanged. Image bytes are never stored or
  hashed.
- **Runtime-only fields:** some runtimes accept fields others ignore (Ollama's
  `keep_alive` and `images`; OpenAI's `prompt_cache_key` and `safety_identifier`). They
  are forwarded unchanged and not interpreted.

## 6. Errors trimset itself returns on the proxy

Some errors originate in trimset: a bad trimset key, an unreachable upstream, a timeout.
These use **the upstream's error format**, so the app's own client parses them:

| Format | Body | Status codes |
|---|---|---|
| systemone | `{"detail": "<message>"}` (TypeSafe's 400 shape) | 401, 502, 504 |
| openai-decisions | `{"error": {"message": "<message>", "type": "trimset_error", "code": "<code>"}}` | 401, 502, 504 |

OpenAI's Decisions pages don't document an error body. The shape above is OpenAI's
general error envelope, an **assumption to confirm with a real call in M1**.

Upstream errors pass through unchanged. That includes Ollaya's
`{"error", "code"}`, Ollama's `{"error"}`, TypeSafe's 422 `{"detail": [...]}`, and 429
with `retry-after`.

## 7. Limits worth knowing (from the vendors)

| | TypeSafe | Ollaya | Ollama (local) | OpenAI |
|---|---|---|---|---|
| Questions per request | No stated maximum (context-bound) | 1–256 | Not stated (cloud: 64) | 1–400 |
| Choice options | 1–255 | 2–255 | 2–255 in its schema; 2–26 in its guide | 2–255 |
| Score levels | 1–10 in its schema; "at least two" in its prose | 2–10 | 2–10 | 2–10 |
| Auth | Bearer | None unless `OLLAYA_API_KEY` | None (cloud: Bearer) | Bearer |

trimset accepts whatever its upstream accepts. The compiler needs at least two levels or
options to produce a policy.
