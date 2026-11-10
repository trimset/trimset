# Phase 3: Contracts

**Status:** awaiting approval · 2026-11-10 · Inputs: [01-requirements.md](../01-requirements.md), [02-architecture.md](../02-architecture.md)

| File | What it fixes |
|---|---|
| [openapi.yaml](openapi.yaml) | Every endpoint: proxy, ingest, outcomes, policies, review, admin, ops (OpenAPI 3.1) |
| [schemas/](schemas/) | JSON Schemas (draft 2020-12): `question/1`, `record/1`, `outcome/1`, `fit/1`, `policy/1`, `drift_report/1`, `bundle/1`, plus shared definitions |
| [examples/](examples/) | One valid document per schema, all describing the same refund-fraud decision |
| [wire-formats.md](wire-formats.md) | How `/v1/systemone` and OpenAI `/v1/decisions` traffic becomes records; the facts about each runtime |
| [hashing.md](hashing.md) | Canonical JSON, `question_id`, state hash, policy fingerprint and bundle ETag, with test vectors |
| [sdk.md](sdk.md) | The Python SDK's interface and exact gate algorithm; how the TypeScript SDK differs |
| [vectors/](vectors/) | `generate.py`, the reference computation, and `vectors.json` |

The numbers in the examples are illustrative, internally consistent and computed, never
results. Only demo data may fill the homepage.

## Conventions

**Versions.**
- The URL carries the API version (`/v1`). Every payload carries its schema version
  (`"schema": "record/1"`).
- Adding an optional field keeps the version; anything else bumps it.
- The server accepts SDKs one minor version older (NFR-15).

**Keys.** `Authorization: Bearer <key>`. In proxy header mode the key goes in
`X-Trimset-Key`, so `Authorization` stays free for the upstream key.

| Prefix | Scope | Can call |
|---|---|---|
| `tsp_` | proxy | Proxy endpoints only |
| `tsi_` | ingest | Proxy, `/v1/decisions:log`, `/v1/decisions:actions`, `/v1/outcomes`, `GET /v1/policies` |
| `tsr_` | review | `/v1/review/*` |
| `tsa_` | admin | Everything, including `/v1/admin/*` |

A key is its prefix plus 32 base62 characters. The prefixes make leaked keys easy to
find with secret scanners and easy to redact in logs.

**Errors.** RFC 9457 `application/problem+json`, with `type` set to
`https://trimset.dev/problems/<slug>`. The exception is the proxy, which answers in the
upstream's format ([wire-formats.md](wire-formats.md) §6).

| Slug | Status | When |
|---|---|---|
| `invalid-request` | 400 / 422 | The body or a parameter fails its schema |
| `unauthorized` | 401 | Missing, unknown or revoked key |
| `forbidden` | 403 | The key's scope doesn't allow this |
| `not-found` | 404 | No such resource in this project |
| `illegal-transition` | 409 | A policy can't move from its state to the requested one |
| `name-taken` | 409 | The question name is already used |
| `unknown-question` | 422 | A record names a `question_id` the server hasn't seen, and the batch has no definition |
| `question-hash-mismatch` | 422 | A definition doesn't hash to its `question_id` |
| `timestamp-out-of-range` | 422 | `ts` is outside [now − retention, now + 5 minutes] |
| `too-large` | 413 | Over 1,000 items or 1 MB (8 MiB on the proxy) |

**Batches.**
- At most 1,000 items.
- The reply is 200 when everything was stored, matched or held. It is 207 when some
  items were rejected; the valid items are kept either way, and `errors[]` lists the
  rejected ones by index.

**Idempotency.**
- Records are deduplicated by (`decision_id`, `ts`).
- Outcomes are deduplicated by `outcome_id` when it is sent. Without it, a repeat adds a
  history row but leaves the current label as it was.
- For action reports, the last one wins.

**Ids and time.**
- `decision_id` should be a UUIDv7. The server reads its timestamp to find the partition;
  other UUID versions work, but more slowly.
- Times are RFC 3339 in UTC.

**Paging.** `cursor` and `limit` (at most 1,000). The response has `next_cursor`, which is
`null` on the last page.

**Long operations.** Fits, compiles and deletions answer 202 with a `Job` and a `Location`
header. Poll `/v1/admin/jobs/{job_id}`.

## Changes to earlier documents

The official vendor docs, read for this phase, changed these:

| Was | Now | Why |
|---|---|---|
| Ollama "0.40 and later" (brief, FR-04) | Ollama **0.35.0** and later for local models; 0.40.5 for cloud | Ollama's own docs |
| Format chosen by path (FR-05) | Chosen by the upstream's format, which the operator sets | Ollaya serves the systemone format at `/v1/decisions` too |
| Silent upgrades detected for every upstream (FR-39) | Only where the response names the version: fully on TypeSafe, partly on Ollaya, not on Ollama or OpenAI | They echo the requested name |
| Probabilities must sum to 1 within 1e-6 (FR-02) | Within **1e-3**, then renormalised | Vendors round to 4 decimals; Ollama's own example sums to 0.9999 |
| Proxy body limit 1 MB (ADR 0011) | **8 MiB**, configurable | Image requests; matches Ollaya |
| Three key scopes (FR-52) | **Four**: adds `review` | Reviewers shouldn't hold keys that can send outcomes |
| Action taken only in log records (FR-33) | Also `POST /v1/decisions:actions` | In proxy mode, the record is written before the app's `gate()` runs |
| No refusals | `refusal: true` with `probabilities: null`; gate returns the default with reason `refusal` | OpenAI answers can be refusals |
| Policy transitions as one URL each | `POST /v1/admin/policies/{id}/transitions` with `{action}` | Avoids clashing with `/{id}/diff` |

After two security reviews of this phase:
- **Upstreams and alert channels** are defined only by the operator, in the server
  environment, with each key bound to its URL. Projects pick them by name, so the API
  never carries a URL, a header or the name of a secret ([ADR 0011](../adr/0011-proxy-design.md)).
- **Link templates** must be http(s).

These close threats E2, E3 and the new E4 in the architecture doc.

ADR 0006 also gained a rule: with a false-pass target, the block threshold is the highest
one that keeps escalation within the cap.

## How to check this phase

```bash
cd docs/03-api
uvx --from openapi-spec-validator==0.7.2 openapi-spec-validator openapi.yaml
for s in question record outcome fit policy drift_report bundle; do
  uvx --from check-jsonschema==0.33.0 check-jsonschema --schemafile schemas/$s.schema.json examples/$s.json
done
uv run --no-project --with rfc8785==0.1.4 python vectors/generate.py --check
```

All three pass as committed. I also confirmed that each check can fail: a broken schema
reference fails the OpenAPI check, and three deliberately invalid documents (a refusal
that has probabilities, an outcome naming both a decision and an external_ref, a risk
target with no α) fail their schemas.
