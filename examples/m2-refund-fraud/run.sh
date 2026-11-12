#!/usr/bin/env bash
# The M2 demo (T2.8): refund fraud (UC-1) from logged decisions to a policy with a written
# guarantee, through the trimset CLI. From a clean checkout, with Docker, curl and
# openssl: examples/m2-refund-fraud/run.sh
# Clean up afterwards with: docker compose -f compose.yaml -f compose.mock.yaml down -v
set -euo pipefail
cd "$(dirname "$0")/../.."
export COMPOSE_FILE=compose.yaml:compose.mock.yaml
API=http://127.0.0.1:8800
MODEL=jev-1.14.0

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
json_field() { sed -E "s/.*\"$1\": ?\"?([^\",}]*)\"?.*/\1/"; }

if [ ! -f .env ]; then
  printf 'TRIMSET_DB_PASSWORD=%s\nTRIMSET_SECRET=%s\n' "$(openssl rand -hex 16)" \
    "$(openssl rand -hex 32)" > .env
fi

step "Start trimset (Postgres, proxy, server, worker)"
docker compose up -d --build --wait --wait-timeout 180

step "A project with an admin key and an ingest key"
project="refunds-$(date +%s)"
docker compose exec -T server trimset-server create-project "$project" > /dev/null
key() {
  docker compose exec -T server trimset-server create-key "$project" --scope "$1" \
    --label "m2 demo" | json_field key
}
ADMIN=$(key admin)
INGEST=$(key ingest)
# Keys go to containers by name (-e NAME), so they never show in the process list.
export TRIMSET_KEY=$ADMIN TRIMSET_ADMIN_KEY=$ADMIN TRIMSET_INGEST_KEY=$INGEST
# The CLI ships with the SDK, which the server image already holds.
trimset() {
  docker compose exec -T -e TRIMSET_KEY -e TRIMSET_URL=http://127.0.0.1:8810 server trimset "$@"
}

step "Seven months of refunds from a model that is badly calibrated (slope 0.5, bias +1),
with chargebacks 30-90 days late, risk-team reviews, and the 120-day rule"
docker compose run --rm --no-deps -e TRIMSET_INGEST_KEY -e TRIMSET_ADMIN_KEY \
  --entrypoint trimset-datagen mock-upstream refund-fraud --seed 11 --days 210 --per-day 60 \
  --slope 0.5 --bias 1 --url http://proxy:8800

step "Chargebacks can take 120 days: only older refunds count for fitting"
curl -fsS -o /dev/null -X PATCH "$API/v1/admin/questions/refund_fraud" \
  -H @- -H 'content-type: application/json' -d '{"settings": {"label_horizon_days": 120}}' \
  <<< "Authorization: Bearer $ADMIN"
docker compose restart worker > /dev/null # it runs the default rule at start
sql() { docker compose exec -T postgres psql -U trimset -d trimset -tAc "$1"; }
for _ in $(seq 30); do
  rules=$(sql "SELECT count(*) FROM outcomes o JOIN projects p USING (project_id)
               WHERE p.name = '$project' AND o.source = 'rule'")
  [ "$rules" -gt 0 ] && break
  sleep 1
done
echo "$rules refunds labelled 'not fraud' by the 120-day rule"

step "Which paths get labels? Held refunds never see a chargeback"
trimset coverage refund_fraud --model "$MODEL"

step "trimset calibrate: raw against calibrated, on refunds the fit never saw"
trimset calibrate refund_fraud --model "$MODEL" --window-days 210

step "trimset simulate: what 'accept below t' would do at three thresholds, on the 20% of
refunds the fit never saw (--split eval: numbers for a threshold picked by looking are optimistic
on the tune split)"
for t in 0.10 0.18 0.30; do
  trimset simulate refund_fraud --model "$MODEL" --lo "$t" --split eval
done

step "trimset policy compile: at most 2% of paid refunds fraudulent, with 95% confidence,
and at most 15% sent to the risk team"
trimset policy compile refund_fraud --model "$MODEL" --false-pass 0.02 --confidence 0.95 \
  --max-escalation 0.15 | tee /tmp/trimset-m2-policy.txt

# What CI checks: a policy came out, and its guarantee is certified.
grep -q "guarantee (certified)" /tmp/trimset-m2-policy.txt || {
  echo "no certified policy" >&2
  exit 1
}

step "Done. The draft waits for trimset policy submit, approve and activate. Stop with:
docker compose -f compose.yaml -f compose.mock.yaml down -v"
