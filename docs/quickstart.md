# Quickstart: from a call to a guarantee, without a model account

This takes you from nothing to a policy that decides in code, using a stand-in model that
ships with the repository, so you need no account anywhere. At the end, switching to your
own model is two lines.

You need Docker with Compose, `curl`, `openssl` and `python3`. The first build compiles the
proxy and takes a few minutes; nothing after it does.

Every command below is also in a script that CI runs on each change:
[examples/m3-ticket-triage](https://github.com/trimset/trimset/blob/main/examples/m3-ticket-triage/run.sh). If something here
doesn't work, that script is the reference.

## 1. Start trimset

```bash
git clone https://github.com/trimset/trimset && cd trimset
echo "TRIMSET_DB_PASSWORD=$(openssl rand -hex 16)" > .env
echo "TRIMSET_SECRET=$(openssl rand -hex 32)" >> .env
export COMPOSE_FILE=compose.yaml:compose.mock.yaml      # the stack plus a stand-in model
docker compose up -d --build --wait --wait-timeout 300
curl -s http://127.0.0.1:8800/ready                      # {"status":"ready"} when Postgres is reachable
```

You now have Postgres, the server, the worker and the proxy on port 8800. The stand-in
model ([`compose.mock.yaml`](https://github.com/trimset/trimset/blob/main/compose.mock.yaml)) is never part of a real install.

## 2. A project and its keys

A project holds one app's decisions. Each key has one job, and is shown once:

```bash
docker compose exec -T server trimset-server create-project acme
key() { docker compose exec -T server trimset-server create-key acme --scope "$1" \
          | python3 -c "import json,sys; print(json.load(sys.stdin)['key'])"; }
export ADMIN=$(key admin) INGEST=$(key ingest) PROXY=$(key proxy)
```

`proxy` is for your app's calls to the model, `ingest` for your backend (outcomes, the SDK),
`admin` for you. [Scopes](03-api/README.md) lists what each may do.

## 3. Send a decision through the proxy

The proxy forwards the call to the model unchanged and records the answer off the request
path. Ask the stand-in model whether a refund looks fraudulent:

```bash
curl -si http://127.0.0.1:8800/v1/systemone -H "X-Trimset-Key: $PROXY" \
  -H 'content-type: application/json' -d '{"model": "mock-latest",
  "state": {"refund_id": "rf-1", "amount": 120},
  "questions": {"refund_fraud": {"type": "noul",
                "instructions": "Is this refund request fraudulent?"}}}'
```

The answer is the model's own, with one added header, `X-Trimset-Decision-Id`. Note the
`model` in the answer: trimset records the version that actually answered, not the alias
you asked for, so a silent upgrade behind `mock-latest` stays visible.

## 4. Give it a history to learn from

One call tells you nothing about calibration. The data generator plays a month of support
tickets for the question *"Which team should handle this ticket?"*, with the outcomes that
came back within three days:

```bash
export TRIMSET_KEY=$ADMIN TRIMSET_ADMIN_KEY=$ADMIN TRIMSET_INGEST_KEY=$INGEST
docker compose run --rm --no-deps -e TRIMSET_INGEST_KEY -e TRIMSET_ADMIN_KEY \
  --entrypoint trimset-datagen mock-upstream ticket-triage --seed 5 --days 30 --per-day 200 \
  --url http://proxy:8800

# Tickets are resolved within 3 days; younger ones haven't had their chance to be labeled.
curl -fsS -o /dev/null -X PATCH http://127.0.0.1:8800/v1/admin/questions/team \
  -H @- -H 'content-type: application/json' -d '{"settings": {"label_horizon_days": 3}}' \
  <<< "Authorization: Bearer $ADMIN"
```

(`-H @-` reads the key from standard input, which keeps it out of the process list.)

With your own model, this is the step that takes time: outcomes arrive when they arrive,
and a policy needs a few hundred labeled decisions per question and model. `trimset
coverage team` shows how many you have and which paths lack labels.

## 5. Calibrate, compile, approve

The `trimset` command ships in the server image; this runs it there:

```bash
trimset() { docker compose exec -T -e TRIMSET_KEY -e TRIMSET_URL=http://127.0.0.1:8810 \
              server trimset "$@"; }

trimset calibrate team --model jev-1.14.0
trimset policy compile team --model jev-1.14.0 --error-budget 0.25
```

`calibrate` shows whether the model's confidence is honest, raw against calibrated, on
decisions it never saw. `compile` finds the loosest threshold whose error rate it can
certify: *with 95% confidence, at most 25% of the tickets the policy handles by itself are
wrong*. If the data can't support a target, it says so and creates nothing; try a looser
one.

The policy starts as a draft. A person moves it on, with a reason on the record (copy the
policy id from the first line of the output):

```bash
trimset policy submit  POLICY_ID
trimset policy approve POLICY_ID --reason "numbers read, looks right"
trimset policy activate POLICY_ID --reason "first policy"
```

## 6. Decide in your app

The SDK fetches active policies and applies them locally: no network call on the decision
path, and no exception if trimset is down (you get your default and a reason). Inside the
server container, where the SDK is installed:

```bash
docker compose exec -T -e TRIMSET_INGEST_KEY server python - <<'EOF'
import os, trimset
ts = trimset.Client("http://127.0.0.1:8810", os.environ["TRIMSET_INGEST_KEY"],
                    defaults={"team": "ESCALATE"})
ts.wait_for_policies()
for probs in ([0.02, 0.93, 0.03, 0.02], [0.30, 0.30, 0.25, 0.15]):
    d = ts.gate("team", probabilities=probs, model_served="jev-1.14.0")
    print(probs, "->", d.action, f"(policy v{d.policy_version}, {d.source})")
EOF
```

With the numbers above, the clear ticket (93% shipping) is routed, `SELECT:shipping`, and
the unclear one is `ESCALATE`: a person decides. Your thresholds will differ a little; they
are fitted to the data you generated. Change the policy, or roll it back with `trimset policy rollback team
--model jev-1.14.0 --reason "..."`, and a running SDK follows within 36 seconds at the very most (it polls every 30, give or take 20%).

[examples/m3-ticket-triage](https://github.com/trimset/trimset/tree/main/examples/m3-ticket-triage) does the whole of steps 4 to 6,
including the second policy and the rollback, in one script.

## 7. Use your own model

Remove the stand-in, and tell the server where your model lives (only the operator can set
this; no API key can change it):

```bash
unset COMPOSE_FILE
echo "TRIMSET_UPSTREAM_JEV_URL=https://api.typesafe.ai" >> .env      # or your Ollaya, or Ollama
docker compose up -d
```

Create a `proxy` key as in step 2, then in your app change one URL:

```python
# before: base_url = "https://api.typesafe.ai"
base_url = "http://localhost:8800/p/<proxy key>"
```

Everything else about your calls stays the same. For a model that runs inside your own
process, or to send records from your backend instead, see
[the SDK interface](03-api/sdk.md) (`log_exchange()`, `instrument()`).

To run it for real (TLS, backups, upgrades), read [self-hosting](self-hosting.md).
