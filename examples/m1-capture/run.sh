#!/usr/bin/env bash
# The M1 demo (T1.15): refund fraud (UC-1) captured end to end, against the mock upstream.
# From a clean checkout, with Docker, curl and openssl: examples/m1-capture/run.sh
# Clean up afterwards with: docker compose -f compose.yaml -f compose.mock.yaml down -v
set -euo pipefail
cd "$(dirname "$0")/../.."
export COMPOSE_FILE=compose.yaml:compose.mock.yaml
API=http://127.0.0.1:8800
MOCK=http://127.0.0.1:9000

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
json_field() { sed -E "s/.*\"$1\": ?\"?([^\",}]*)\"?.*/\1/"; }

if [ ! -f .env ]; then
  printf 'TRIMSET_DB_PASSWORD=%s\nTRIMSET_SECRET=%s\n' "$(openssl rand -hex 16)" \
    "$(openssl rand -hex 32)" > .env
fi

step "Start trimset (Postgres, server, worker) and the mock upstream"
docker compose up -d --build --wait --wait-timeout 180

step "A project with three keys: admin, ingest (your backend) and proxy (your app)"
project="refunds-$(date +%s)"
docker compose exec -T server trimset-server create-project "$project" > /dev/null
key() {
  docker compose exec -T server trimset-server create-key "$project" --scope "$1" \
    --label "m1 demo" | json_field key
}
ADMIN=$(key admin)
INGEST=$(key ingest)
PROXY=$(key proxy)
echo "project $project; keys ${ADMIN:0:8}…, ${INGEST:0:8}…, ${PROXY:0:8}… (shown once, kept nowhere)"

check_refund() { # $1: the refund id, also the decision's external_ref
  curl -fsS -o /dev/null "$API/v1/systemone" \
    -H "X-Trimset-Key: $PROXY" -H "X-Trimset-External-Ref: $1" \
    -H 'content-type: application/json' \
    -d "{\"model\": \"jev-latest\", \"state\": {\"refund_id\": \"$1\", \"amount\": $((RANDOM % 500))},
         \"questions\": {\"refund_fraud\": {\"type\": \"noul\",
         \"instructions\": \"Is this refund request fraudulent?\"}}}"
}

step "The refund service asks the model through the proxy: 150 refunds on jev-latest (1.14.0)"
curl -fsS -o /dev/null -X PATCH "$MOCK/_mock/config" -d '{"version": "1.14.0"}'
for i in $(seq -w 1 150); do check_refund "rf-$i"; done
echo "150 answered; each response carried its X-Trimset-Decision-Id"

step "The vendor moves jev-latest to 1.15.0 without telling anyone; 100 more refunds"
curl -fsS -o /dev/null -X PATCH "$MOCK/_mock/config" -d '{"version": "1.15.0"}'
for i in $(seq 151 250); do check_refund "rf-$i"; done

step "Five months of history from the data generator: 6,000 refunds, their chargebacks
and reviews, and the 120-day \"no chargeback means not fraud\" rule"
docker compose run --rm --no-deps -e TRIMSET_INGEST_KEY="$INGEST" -e TRIMSET_ADMIN_KEY="$ADMIN" \
  --entrypoint trimset-datagen mock-upstream refund-fraud --seed 7 --days 150 --per-day 40 \
  --url http://proxy:8800

step "Outcomes for today's refunds: two chargebacks, a review, and one for a refund
trimset hasn't seen yet"
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
outcome() { # $1: refund id, $2: label, $3: source
  printf '{"schema": "outcome/1", "external_ref": "%s", "question": "refund_fraud",
    "label": %s, "source": "%s", "observed_at": "%s"}' "$1" "$2" "$3" "$now"
}
curl -fsS "$API/v1/outcomes" -H "Authorization: Bearer $INGEST" \
  -H 'content-type: application/json' \
  -d "{\"outcomes\": [$(outcome rf-007 true downstream), $(outcome rf-042 true downstream),
       $(outcome rf-120 false human_review), $(outcome rf-900 true downstream)]}"
echo
echo "rf-900 is held. Its refund arrives now, and the worker joins the two"
check_refund rf-900

sql() { docker compose exec -T postgres psql -U trimset -d trimset "$@"; }
held() {
  sql -tAc "SELECT count(*) FROM unmatched_outcomes u JOIN projects p USING (project_id)
            WHERE p.name = '$project'"
}

step "The worker runs its jobs (every minute and hourly; a restart runs them now)"
docker compose restart worker > /dev/null
for _ in $(seq 30); do [ "$(held)" = 0 ] && break; sleep 1; done

step "What trimset now knows about the question"
question=$(curl -fsS "$API/v1/admin/questions/refund_fraud" -H "Authorization: Bearer $ADMIN")
echo "$question" | { python3 -m json.tool 2> /dev/null || cat; }
step "Decisions by the model that actually answered (the silent upgrade is visible)"
sql -c "SELECT model_requested, model_served, count(*) AS decisions FROM decisions d
     JOIN projects p USING (project_id) WHERE p.name = '$project'
     GROUP BY 1, 2 ORDER BY 2"

step "Labels by where they came from"
sql -c "SELECT o.source, count(*) AS labels FROM outcomes o
     JOIN projects p USING (project_id) WHERE p.name = '$project' GROUP BY 1 ORDER BY 1"

step "Still waiting for their decision"
echo "$(held) held outcomes"

# What CI checks: the upgrade was seen and the early outcome found its refund.
for model in jev-1.14.0 jev-1.15.0; do
  grep -q "\"model_served\":\"$model\"" <<< "$question" || { echo "missing $model" >&2; exit 1; }
done
[ "$(held)" = 0 ] || { echo "the held outcome was never joined" >&2; exit 1; }

step "Done. Stop it with: docker compose -f compose.yaml -f compose.mock.yaml down -v"
