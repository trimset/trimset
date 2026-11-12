# Self-hosting trimset

trimset is one image, run as three processes, with Postgres beside it. This page is for
the person who keeps it running: what it needs, how to expose it safely, how to back it up
and upgrade it. To try it first, use the [quickstart](quickstart.md).

## What runs

| Container | What it does | Port |
|---|---|---|
| `proxy` | The front door. Forwards model calls to your upstream unchanged, records them off the request path, and passes every other path to the server. | **8800**, all interfaces |
| `server` | The API (FastAPI): ingest, outcomes, fits, compiles, policies, the audit log. | 8810, inside the compose network only |
| `worker` | Background jobs: joining late outcomes, default labels by rule, fits and compiles, scheduled refits, partition upkeep. | none |
| `postgres` | The only store. No Redis, no queue. Decisions live in monthly partitions. | none |

The proxy, server and worker are the same image with different commands. Aim for 2 vCPU
and 4 GB on one machine, with Postgres on the same box; disk grows with your decision
volume (a decision record is a few hundred bytes plus its hashes).

## Before you expose it

1. **Put TLS in front.** Port 8800 speaks plain HTTP and serves both the model proxy and
   the admin API. Terminate TLS in a reverse proxy and publish only that. A Caddy
   example, with automatic certificates:
   ```
   trimset.example.com {
       reverse_proxy 127.0.0.1:8800
       log {
           output discard      # see the next point
       }
   }
   ```
   Then publish trimset's own port on loopback only: in `compose.yaml`, change the proxy
   service's `ports:` line to `"127.0.0.1:8800:8800"`.
2. **Prefer the header to the path form of the proxy key.** Calls can authenticate with
   `X-Trimset-Key: <key>` or with the key in the URL, `/p/<key>/v1/...`. trimset redacts
   the key from its own logs, but a reverse proxy's access log would record the path.
   Use the header, or turn access logging off as above.
3. **Keep `.env` private.** It holds `TRIMSET_SECRET` and the database password.
4. **Hand out the narrowest key.** `proxy` for the app's model calls, `ingest` for the
   backend and the SDK, `review` for reviewers, `admin` for people who approve policies.
   Keys are stored as hashes and shown once. `trimset-server revoke-key <key_id>` (or
   `DELETE /v1/admin/keys/<id>`) ends one; a revoked key is refused everywhere within 30
   seconds.

## Configuration

Everything is an environment variable on the compose services (`.env` is read by all of
them). Only the first two are required.

| Variable | Default | Meaning |
|---|---|---|
| `TRIMSET_DB_PASSWORD` | required | Postgres password. Letters and digits only. |
| `TRIMSET_SECRET` | required | At least 32 characters (`openssl rand -hex 32`). Derives each project's key for state hashes ([ADR 0009](adr/0009-privacy-model.md)). Keep it stable: with a new secret, new decisions get hashes that no longer match older ones. |
| `TRIMSET_UPSTREAM_<NAME>_URL` | none | A model server. `<NAME>` is yours, e.g. `JEV`, `OLLAYA`. Only the operator can set these. |
| `TRIMSET_UPSTREAM_<NAME>_FORMAT` | `systemone` | `systemone` or `openai-decisions`. |
| `TRIMSET_UPSTREAM_<NAME>_KEY` | none | A key sent to that URL only, as `Authorization: Bearer`. The URL must be `https`. |
| `TRIMSET_UPSTREAM_<NAME>_PROJECTS` | every project | Comma list of project ids allowed to use it. |
| `TRIMSET_WORKERS` | `2` | Server processes. |
| `TRIMSET_LOG_LEVEL` | `INFO` | |
| `TRIMSET_DB_POOL_MAX` | `10` | Connections per server process. |
| `TRIMSET_AUTO_MIGRATE` | `true` in compose | Migrate the database on start. |
| `TRIMSET_PROXY_HOST`, `TRIMSET_PROXY_PORT` | `0.0.0.0`, `8800` | Where the proxy listens *inside its container*. Leave them; limit who can reach it with the `ports:` line in `compose.yaml`. |
| `TRIMSET_PROXY_BUFFER` | `50000` | Records held in memory while the database is slow or down. When full, the oldest are dropped (and counted); the proxy keeps forwarding. |
| `TRIMSET_KEY_CACHE_TTL_S` | `30` (max 60) | How long a key check is cached; also how long a revoked key may still work. |
| `TRIMSET_JOIN_INTERVAL_S`, `TRIMSET_RULES_INTERVAL_S`, `TRIMSET_JOBS_INTERVAL_S`, `TRIMSET_MAINTENANCE_INTERVAL_S` | 60, 3600, 1, 3600 | How often the worker joins held outcomes, applies default labels, runs queued jobs, and creates partitions. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | none | Send OpenTelemetry traces to a collector. Off unless set. |

Per-project settings (retention, prices, tag allow-list, the refit schedule) live in the
database and are set through the API: `PATCH /v1/admin/project`. Changes are audited.

## What leaves the machine

The proxy connects out only to the upstreams you configured. The server makes no outbound
connections of its own. Nothing is sent to anyone else, and there is no usage reporting.
If you set `OTEL_EXPORTER_OTLP_ENDPOINT`, traces go there.

## Day to day

- **Health.** `GET /health` answers if the process is up. `GET /ready` also checks
  Postgres and answers 503 when it can't reach it. Neither needs a key or returns data.
- **Logs.** Every container logs one JSON object per line to standard output
  (`docker compose logs -f`). Keys are redacted, including in the proxy's URL form.
- **If Postgres is down,** the proxy keeps forwarding your model calls and holds the
  records it can't write (up to `TRIMSET_PROXY_BUFFER`); it writes them when the database
  returns. The SDK keeps deciding from its cached policies.
- **More than one worker** is safe (jobs are claimed one at a time, and a refit is queued
  once per project and minute), but one is enough.

## Backups

All state is in the Postgres volume. A logical dump is enough at this size:

```bash
docker compose exec -T postgres pg_dump -U trimset trimset | gzip > "trimset-$(date +%F).sql.gz"
```

To restore, stop the writers, replace the database with an empty one, load the dump, and
start again (the server and worker find the schema in place):

```bash
docker compose stop proxy server worker
docker compose exec -T postgres psql -U trimset -d postgres \
  -c 'DROP DATABASE trimset WITH (FORCE)' -c 'CREATE DATABASE trimset'
gunzip -c trimset-2026-12-01.sql.gz | docker compose exec -T postgres psql -U trimset trimset
docker compose start server worker proxy
```

Try a restore on a spare machine before you need one. `TRIMSET_SECRET` is not in the dump:
keep it with your other secrets, and restore with the same value.

## Upgrading

```bash
git pull                       # or set the image tag to the version you want
docker compose pull && docker compose up -d
```

The server and the worker migrate the database when they start, under an advisory lock so
that only one does. Migrations only move forward, so **back up first**, and read the
release's notes in [CHANGELOG.md](../CHANGELOG.md). There is no downgrade: to go back,
restore the backup you took.

While trimset is 0.x, keep the SDK and the server on the same version.

## Not yet

Retention (dropping months older than a project's `retention_days`) and deleting all data
for one external reference are planned for v0.1 and are not enforced yet: today, nothing
is deleted automatically. Drift and silent-upgrade alerts are planned too. See the
[plan](04-plan.md).
