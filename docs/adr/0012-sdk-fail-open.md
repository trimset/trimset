# 0012. SDK fail-open semantics

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** FR-31 to FR-37, NFR-04, NFR-07

## Context

The SDK runs inside customer apps. It must never break or slow their request path. The
brief says it "fails open with configurable defaults". Phase 1 open question 3: what
happens when the app configures no default.

## Options for the library's own default

1. **ACCEPT.** Truly "open", but silently pays out fraud and lets personal data through
   while the server is unreachable.
2. **BLOCK.** Silently refuses customers.
3. **ESCALATE.** Hands the decision to the stronger decider, a bigger model or a person.
   Costly if the outage is long, but never silently wrong.
4. **Raise an error.** Breaks the app, which contradicts the whole point.

## Decision

**Fail open means: never raise, never block on the network, and always return an
action.**

**How `gate()` decides, in order:**
1. A cached policy for (`question_id`, `model_served`) whose fingerprint checks out: use
   its action, or `ESCALATE` with source `audit` for an audited decision (FR-37).
2. Otherwise, the app's default for that question (`defaults={"refund_fraud": "ESCALATE"}`,
   or `gate(…, default=…)`).
3. Otherwise **ESCALATE** (option 3), with a warning logged once per question. The docs
   tell every app to set its own defaults.

**What every result carries:**
- the action and its `source`: `policy`, `audit` or `default`;
- a `reason`: `no_policy`, `model_mismatch`, `question_changed`, `invalid_probabilities`,
  `fingerprint_mismatch` or `stale_policy`;
- the policy version when one decided.

**Stale policies:** the last good policy is used with no time limit by default. An
optional `max_policy_age` switches to the default after that age. The policy's age is
exported as a metric.

**`log()` and `outcome()`:**
- They run on a background thread, with a bounded queue (10,000 records by default).
- Records are sent in batches of 100, or every second.
- Retries use exponential backoff with full jitter, from 0.5 s up to 60 s.
- When the queue is full the oldest record is dropped, and the counts are reported to the
  server.
- At exit the SDK flushes, for at most 2 s.

**Startup and processes:**
- `wait_for_policies(timeout)` is the only call that waits on the network, and the app
  decides whether to make it.
- After a fork (for example gunicorn's preload), the SDK notices the new process id and
  restarts its thread.
- An optional disk cache is written atomically, and its fingerprints are checked when it
  is loaded.

## Consequences

- An outage can never silently turn into "accept everything".
- Apps where escalating is expensive must set explicit defaults. The quickstart shows
  this.
- The chaos tests for NFR-07 cover all six reasons above.
