#!/usr/bin/env bash
# The M3 demo (T3.7): ticket triage (UC-2). A policy is compiled, approved and activated;
# an app using the SDK picks it up and decides locally; a second policy goes live and is
# rolled back while the app is watching; the audit log has the whole story.
# From a clean checkout, with Docker, curl, openssl and python3: examples/m3-ticket-triage/run.sh
# Clean up afterwards with: docker compose -f compose.yaml -f compose.mock.yaml down -v
set -euo pipefail
cd "$(dirname "$0")/../.."
export COMPOSE_FILE=compose.yaml:compose.mock.yaml
API=http://127.0.0.1:8800
MODEL=jev-1.14.0
LOG=$(mktemp)

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
json_field() { sed -E "s/.*\"$1\": ?\"?([^\",}]*)\"?.*/\1/"; }
policy_id() { python3 -c "import json,sys; print(json.load(sys.stdin)['policy']['policy_id'])"; }

if [ ! -f .env ]; then
  printf 'TRIMSET_DB_PASSWORD=%s\nTRIMSET_SECRET=%s\n' "$(openssl rand -hex 16)" \
    "$(openssl rand -hex 32)" > .env
fi

step "Start trimset (Postgres, proxy, server, worker)"
docker compose up -d --build --wait --wait-timeout 180

step "A project with an admin key (the approver) and an ingest key (the support app)"
project="triage-$(date +%s)"
docker compose exec -T server trimset-server create-project "$project" > /dev/null
key() {
  docker compose exec -T server trimset-server create-key "$project" --scope "$1" \
    --label "m3 demo" | json_field key
}
ADMIN=$(key admin)
INGEST=$(key ingest)
# Keys go to containers by name (-e NAME), so they never show in the process list.
export TRIMSET_KEY=$ADMIN TRIMSET_ADMIN_KEY=$ADMIN TRIMSET_INGEST_KEY=$INGEST
trimset() {
  docker compose exec -T -e TRIMSET_KEY -e TRIMSET_URL=http://127.0.0.1:8810 server trimset "$@"
}

step "A month of support tickets: the model names a team, and the team that took the ticket
reports back within 72 hours (5% never do)"
docker compose run --rm --no-deps -e TRIMSET_INGEST_KEY -e TRIMSET_ADMIN_KEY \
  --entrypoint trimset-datagen mock-upstream ticket-triage --seed 5 --days 30 --per-day 200 \
  --url http://proxy:8800
curl -fsS -o /dev/null -X PATCH "$API/v1/admin/questions/team" \
  -H @- -H 'content-type: application/json' -d '{"settings": {"label_horizon_days": 3}}' \
  <<< "Authorization: Bearer $ADMIN"

step "trimset calibrate: is the model's confidence honest?"
trimset calibrate team --model "$MODEL"

step "trimset policy compile: at most 25% wrong on the tickets it handles by itself"
v1=$(trimset --json policy compile team --model "$MODEL" --error-budget 0.25 | policy_id)
trimset policy show "$v1"

step "The approver submits, approves and activates it, each with a reason on the record"
trimset policy submit "$v1" --reason "from the demo's first compile"
trimset policy approve "$v1" --reason "guarantee read, numbers look right"
trimset policy activate "$v1" --reason "first policy for team"

step "The support app starts. Its SDK fetches the policy and gates each answer locally"
docker compose exec -T -e TRIMSET_INGEST_KEY -e WATCH_SECONDS=90 server python - \
  < examples/m3-ticket-triage/app.py > "$LOG" 2>&1 &
app=$!
for _ in $(seq 60); do
  grep -q "watching the policy" "$LOG" && break
  kill -0 "$app" 2> /dev/null || { cat "$LOG" >&2; echo "the app stopped early" >&2; exit 1; }
  sleep 1
done
cat "$LOG"

step "Meanwhile a second, looser policy is compiled. The approver reads its diff first"
v2=$(trimset --json policy compile team --model "$MODEL" --error-budget 0.4 | policy_id)
trimset policy diff "$v2"

step "...and activates it. The running app should notice within about a poll"
trimset policy submit "$v2"
trimset policy approve "$v2" --reason "wider coverage is worth it"
trimset policy activate "$v2" --reason "second policy"
for _ in $(seq 40); do grep -q "policy v2" "$LOG" && break; sleep 1; done
grep "policy v" "$LOG"

step "It escalates too little. One command puts the previous version back"
trimset policy rollback team --model "$MODEL" --reason "v2 let too much through"
wait "$app" # the app leaves once it has seen v1, v2 and v1 again
grep "policy v" "$LOG"

step "What the app logged: each ticket with the action the policy took"
trimset decisions tail --question team --once

step "The audit log keeps who did what, and why"
curl -fsS "$API/v1/admin/audit-log?limit=30" -H @- <<< "Authorization: Bearer $ADMIN" \
  | python3 -c "
import json, sys
for e in reversed(json.load(sys.stdin)['items']):
    if e['action'].startswith('policy.'):
        reason = (e['after'] or {}).get('reason') or ''
        print(f\"{e['at'][11:19]}  {e['action']:16} {e['target'][:15]}  {reason}\")
" | tee /tmp/trimset-m3-audit.txt

# What CI checks: the app saw the round trip, and the trail has the rollback.
grep -q "the whole round trip was seen" "$LOG" || { echo "no round trip" >&2; exit 1; }
grep -q "policy.rollback" /tmp/trimset-m3-audit.txt || { echo "no rollback in the audit log" >&2; exit 1; }

step "Done. Stop it with: docker compose -f compose.yaml -f compose.mock.yaml down -v"
