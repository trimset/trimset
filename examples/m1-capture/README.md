# M1 demo: capture

Refund fraud ([UC-1](../../docs/01-requirements.md#uc-1--refund-fraud)) end to end, with
the mock upstream standing in for the model. It shows what trimset does after M1: it
captures every decision, notices a silent model upgrade, and joins outcomes that arrive
late, early, or never.

```bash
examples/m1-capture/run.sh
```

It needs Docker, curl and openssl, and takes a few minutes, most of it the first build.
It writes a `.env` with a random database password and secret if you don't have one.

## What it does

1. Starts Postgres, the server, the worker and the mock upstream
   (`compose.yaml` plus `compose.mock.yaml`).
2. Creates a project with an admin, an ingest and a proxy key.
3. Sends 150 refund checks through the proxy, the way an app would: it sets
   `X-Trimset-Key`, and uses the refund id as the `X-Trimset-External-Ref`.
4. Moves the mock's `jev-latest` from 1.14.0 to 1.15.0 without telling anyone, and sends
   100 more.
5. Adds five months of history from the data generator: 6,000 refunds, their chargebacks
   (30 to 90 days late) and reviews, and the default rule "no chargeback in 120 days
   means not fraud".
6. Sends outcomes for today's refunds, including one for a refund trimset hasn't seen
   yet. That one is held, and joined once the refund arrives.
7. Restarts the worker, which runs its jobs at start: the held outcome joins, and the
   default rule labels the old refunds that never had a chargeback.

## What you should see

- The question lists two served models behind `jev-latest`, `jev-1.14.0` and
  `jev-1.15.0`: the silent upgrade, caught from the responses alone.
- Decisions counted by served model: 6,150 on `jev-1.14.0` (generated history plus the
  first 150) and 101 on `jev-1.15.0`.
- Labels by source: `downstream` chargebacks, `human_review` verdicts, and `rule` labels
  for refunds older than 120 days.
- No held outcomes left.

Stop everything with `docker compose -f compose.yaml -f compose.mock.yaml down -v`.

CI runs this script on every push, in the image job.
