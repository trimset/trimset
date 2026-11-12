# trimset

**Keep your decision thresholds true.** Open-source, self-hosted decision ops for decision
models like TypeSafe's Jev, Ollaya, Ollama and OpenAI decisions.

A decision model answers typed questions with probabilities (`noul: 0.83`), and your app
turns them into actions with a threshold (`if p > 0.9`). That threshold goes stale: raw
probabilities are rarely calibrated, the right cut-off depends on what a mistake costs,
and the outcome that says whether a decision was right arrives days or months later.
trimset closes that loop:

1. **Capture** every decision, through a proxy (one URL change) or from your backend.
2. **Join** the real outcome when it arrives: a chargeback, a reviewer's label, a reassigned ticket.
3. **Calibrate and compile** thresholds and an escalation band from a target such as
   "false passes ≤ 2% with 95% confidence", and say plainly when there are too few labels
   to promise it.
4. **Gate** in your app: the SDK applies the approved policy locally, without a network
   call on the decision path.

**Status: pre-alpha.** Website: [trimset.dev](https://trimset.dev) · Dashboard demo on
recorded data: [demo.trimset.dev](https://demo.trimset.dev)

## What works today

| | |
|---|---|
| Capture | The proxy forwards `/v1/systemone` and OpenAI-style `/v1/decisions` calls unchanged and logs them; `POST /v1/decisions:log` takes records from anywhere. |
| Outcomes | `POST /v1/outcomes`, matched by decision id or by your own reference; early outcomes are held until their decision arrives; corrections keep their history; default labels by rule ("no chargeback in 120 days"). |
| Calibration | Temperature, Platt and isotonic fits per question and model, chosen by cross-validation, with log loss, Brier and ECE on held-out data. |
| Thresholds | A simulator for any thresholds, and a compiler that certifies a target with Learn-then-Test and exact binomial tests, or says why it can't. |
| Policies | Versions with diffs, propose → approve → activate, rollback, an audit log, and scheduled refits that only propose. |
| SDK | `trimset.Client` in Python 3.11+: `gate()` applies the policy from a verified cache and never blocks or raises; `log_exchange`, `log` and `outcome` send records and outcomes in the background, and the state itself is never sent; `instrument()` captures a model run in your own process, such as a Hugging Face pipeline. |
| CLI | `trimset calibrate`, `simulate`, `policy compile/show/diff/approve/…`, `coverage`, `decisions tail`. |

**Not yet:** drift and silent-upgrade alerts (M4); shadow models and the review queue
(M5); the web dashboard served by your install (v0.2; the demo above is a preview); a
PyPI release. The plan is in [docs/04-plan.md](docs/04-plan.md).

## Quickstart

You need Docker with Compose. trimset runs on one machine (aimed at 2 vCPU, 4 GB) with
Postgres beside it. No model account? [docs/quickstart.md](docs/quickstart.md) walks from a
first call to a policy deciding in code with a stand-in model that ships in the repository.

```bash
git clone https://github.com/trimset/trimset
cd trimset
echo "TRIMSET_DB_PASSWORD=$(openssl rand -hex 16)" > .env
echo "TRIMSET_SECRET=$(openssl rand -hex 32)" >> .env
echo "TRIMSET_UPSTREAM_JEV_URL=https://api.typesafe.ai" >> .env   # your model server
docker compose up -d

# a project and its keys, each shown once
docker compose exec server trimset-server create-project acme
docker compose exec server trimset-server create-key acme --scope proxy    # for your app
docker compose exec server trimset-server create-key acme --scope ingest   # for outcomes
docker compose exec server trimset-server create-key acme --scope admin    # for you
```

Then point your app at trimset instead of the model, and nothing else changes:

```bash
# before: base_url = "https://api.typesafe.ai"
base_url = "http://localhost:8800/p/<proxy key>"
```

Send outcomes when you learn them, with the ingest key:

```bash
curl http://localhost:8800/v1/outcomes -H "Authorization: Bearer <ingest key>" \
  -H 'content-type: application/json' -d '{"outcomes": [{"schema": "outcome/1",
  "external_ref": "rf-1001", "question": "refund_fraud", "label": true,
  "source": "downstream", "observed_at": "2026-11-11T12:00:00Z"}]}'
```

Once labels have matured, calibrate and compile with the admin key. The CLI ships with
the SDK, which isn't on PyPI yet:

```bash
pip install "git+https://github.com/trimset/trimset#subdirectory=sdk/python"
trimset config set --url http://localhost:8800        # asks for the admin key
trimset calibrate refund_fraud --model jev-1.14.0
trimset simulate refund_fraud --model jev-1.14.0 --lo 0.30 --hi 0.85
trimset policy compile refund_fraud --model jev-1.14.0 --false-pass 0.02 --confidence 0.95
```

## See it end to end

- [examples/m2-refund-fraud](examples/m2-refund-fraud): seven months of refunds from a
  badly calibrated model, with chargebacks 30 to 90 days late, end in a policy with its
  guarantee in words. One script, a few minutes.
- [demo.trimset.dev](https://demo.trimset.dev): the dashboard on numbers recorded from that
  example's server, threshold simulator included. Nothing you do there is saved.

## Docs

- **[docs.trimset.dev](https://docs.trimset.dev)**: the documentation site, with the generated API reference
- [Quickstart](docs/quickstart.md) and [self-hosting](docs/self-hosting.md): TLS, upgrades, backups, every setting
- [How it fits together](docs/02-architecture.md) and the decision records in [docs/adr](docs/adr)
- The API contract: [docs/03-api/openapi.yaml](docs/03-api/openapi.yaml) and the [SDK interface](docs/03-api/sdk.md)
- [Requirements](docs/01-requirements.md) and the [plan](docs/04-plan.md)
- Verified sources for every algorithm: [docs/research.md](docs/research.md)

## License

Apache-2.0. Product names belong to their owners; trimset is not affiliated with any of
them.
