# 0008. Policy distribution: pull, with fingerprints

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** FR-22, FR-32, FR-34, NFR-05, NFR-16

## Context

Running apps need their project's active policies. An activation or rollback must reach
every SDK within 60 seconds (NFR-05). The brief asks for a fingerprint check against
tampering.

## Options

1. **Push** over server-sent events or WebSockets. Instant, but it means long-lived
   connections through proxies and firewalls, connection state on the server, and
   reconnect logic in every SDK.
2. **Webhooks to apps.** Every app would have to expose an endpoint.
3. **Pull with ETag.** The SDK polls, and the server replies 304 when nothing changed.

## Decision

**Pull with ETag:**
- `GET /v1/policies` returns the project's bundle: every active policy, plus the
  project's state-hash key ([0009](0009-privacy-model.md)).
- The bundle's ETag is the SHA-256 of the canonical bundle without `generated_at` and
  `etag` (hashing.md §5), so a new policy, a renamed question or a changed guarantee
  status all change it.
- The SDK polls every 30 s with ±20% jitter, sending `If-None-Match`.

**Fingerprints:**
- A fingerprint is the SHA-256 of the policy's canonical JSON (RFC 8785, the JSON
  Canonicalization Scheme), with the fingerprint field itself left out.
- A policy body is immutable once created. Its lifecycle state lives outside the body,
  in `policies.state` and `policy_events`.
- The SDK recomputes each fingerprint before use and rejects any policy that doesn't
  match (FR-34).

**No signatures in v0.1.** A fingerprint proves that a policy is intact, and gives each
decision an exact identity to log. It does not stop someone who can write to the
database, or who sits inside the TLS session, from forging a policy and its fingerprint.
Signing (Ed25519, with the key kept outside the database) is planned for v0.2:
- the threat model lists it as a known remaining risk;
- the SDK would need a vetted crypto library, which conflicts with the pure-Python goal
  (NFR-16), and we will not write our own crypto.

## Consequences

- Load is light: 1,000 SDK instances polling every 30 s is about 33 requests a second,
  nearly all of them 304s.
- Propagation takes at most about 36 s plus network time, within the 60-second target.
- Authenticity rests on TLS plus API keys until signing arrives in v0.2.
