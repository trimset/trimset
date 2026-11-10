# 0011. Proxy design

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** FR-04 to FR-08, FR-45, FR-52, NFR-03, NFR-06

## Context

Adoption must take one base-URL change and no code (FR-08). The proxy is on the app's
request path, so it must add under 5 ms (NFR-03), and it must keep working when Postgres
is down (NFR-06). Phase 1 open question 1: how the proxy knows the project when the app
adds no header.

## Options for knowing the project

1. **A key in the URL path:** `http://trimset:8800/p/<key>`. Works with any client that
   appends paths to its base URL, but URLs end up in logs.
2. **A header:** `X-Trimset-Key`. Clean, but it needs a code or config change in the app.
3. **One port or hostname per project.** Needs operator setup, and DNS or ports for each
   project.
4. **Map the app's upstream API key to a project.** Breaks when the vendor rotates keys,
   and local Ollaya has no key.

## Decision

**Knowing the project:**
- Accept a header (option 2) when present. Otherwise use the URL path (option 1), with a
  new, narrow **`proxy` key scope**.
- A proxy key can only call the proxy endpoints. It can't read data, log through the
  API, or send outcomes, which are the most damaging thing to poison.
- trimset's own logs redact the key segment of the path. The docs show how to do the same
  in nginx and Caddy, and recommend header mode where the app can set headers.

**Upstream credentials:**
- Passed through by default: the app's own `Authorization` header goes to the upstream
  and is never stored or logged.
- Credentials held by the server (an environment-variable reference) are allowed. The
  docs warn that a leaked proxy key would then spend your upstream quota.

**Upstreams and alert channels are defined by the operator** (added after two Phase 3
security reviews). They exist only in the server environment, and projects pick them by
name:

```
TRIMSET_UPSTREAM_<NAME>_URL        https://api.typesafe.ai, or http://host.docker.internal:11435
TRIMSET_UPSTREAM_<NAME>_FORMAT     systemone | openai-decisions
TRIMSET_UPSTREAM_<NAME>_KEY        optional; sent as "Authorization: Bearer …" only to this URL
TRIMSET_UPSTREAM_<NAME>_PROJECTS   optional comma list of project ids; default: every project
TRIMSET_ALERT_<NAME>_KIND          webhook | slack | email
TRIMSET_ALERT_<NAME>_URL / _SECRET / _TO / _PROJECTS
```

- A key is bound to its URL by construction, and the server refuses to start if a key is
  paired with an `http://` URL. Plain http is only for keyless local runtimes.
- Redirects are never followed.
- An admin key can choose among the upstreams and channels its project is allowed. It
  can never supply a URL, a header, or the name of a variable. So it can neither point
  the server at an internal address nor make it send a secret, its own project's or
  another's.

**What comes back:**
- The upstream's status, body and end-to-end headers, unchanged.
- One added header, `X-Trimset-Decision-Id`, listing the decision ids in question order,
  comma-separated.
- trimset's optional request headers (`X-Trimset-External-Ref`, `X-Trimset-Tags`) are
  removed before forwarding.

**Logging:**
- The proxy builds records after it replies and puts them in a bounded in-memory buffer
  (50,000 records per process by default).
- A writer inserts them in batches of up to 500, or every 50 ms. Failures retry with
  backoff.
- When the buffer is full, the oldest records are dropped and counted, and an alert fires
  (NFR-06). The response never waits.

**Limits:**
- Upstream timeout: 10 s per project by default.
- Request body: up to 8 MiB, configurable (raised from 1 MB in Phase 3, so image requests aren't refused).
- Upstream errors are passed back unchanged and counted (FR-40).

**Shadow traffic (FR-45):**
- Runs after the response is sent, for a share chosen by a hash of the first decision id.
- A daily call cap is counted in Postgres.
- Candidate failures are only logged.

## Consequences

- `FR-52` gains a third key scope, `proxy`, next to `ingest` and `admin`.
- The adoption snippet on the homepage becomes `TYPESAFE_BASE_URL=http://localhost:8800/p/<proxy key>`.
- If the proxy itself is down, the app's calls fail. The docs show how to run two server
  replicas, and how to use SDK mode where that risk is unacceptable.
