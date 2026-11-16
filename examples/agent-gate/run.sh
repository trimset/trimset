#!/usr/bin/env bash
# The agent-gate demo (TV.1, UC-5): before an AI agent's tool call runs, a decision model is
# asked whether it is safe to run without a human, and the approved policy decides: run it,
# ask a person, or block it. Day one has no policy, so every call goes to a person; after two
# months of labelled calls a policy is compiled, approved and activated; then Claude Code's
# PreToolUse hook (`trimset hook`) and the MCP server (`trimset mcp`) apply it.
# From a clean checkout, with Docker, curl, openssl and python3: examples/agent-gate/run.sh
# Clean up afterwards with: docker compose -f compose.yaml -f compose.mock.yaml down -v
set -euo pipefail
cd "$(dirname "$0")/../.."
export COMPOSE_FILE=compose.yaml:compose.mock.yaml
HERE=examples/agent-gate
API=http://127.0.0.1:8800
MODEL=jev-1.14.0
OUT=$(mktemp -d)

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail() { echo "agent-gate demo: $*" >&2; exit 1; }
json_field() { sed -E "s/.*\"$1\": ?\"?([^\",}]*)\"?.*/\1/"; }
policy_id() { python3 -c "import json,sys; print(json.load(sys.stdin)['policy']['policy_id'])"; }

if [ ! -f .env ]; then
  printf 'TRIMSET_DB_PASSWORD=%s\nTRIMSET_SECRET=%s\n' "$(openssl rand -hex 16)" \
    "$(openssl rand -hex 32)" > .env
fi

step "Start trimset (Postgres, proxy, server, worker) and the stand-in decision model"
docker compose up -d --build --wait --wait-timeout 180
# The stand-in answers as jev-<version>. The recorded history below was served by jev-1.14.0,
# so that is the version "jev-latest" must resolve to, or the policy would not apply to it.
curl -fsS -o /dev/null -X PATCH http://127.0.0.1:9000/_mock/config -d '{"version": "1.14.0"}'

step "A project with an admin key (the approver) and an ingest key (what the agent holds)"
project="agent-$(date +%s)"
docker compose exec -T server trimset-server create-project "$project" > /dev/null
key() {
  docker compose exec -T server trimset-server create-key "$project" --scope "$1" \
    --label "agent-gate demo" | json_field key
}
ADMIN=$(key admin)
INGEST=$(key ingest)
# Keys go to containers by name (-e NAME), so they never show in the process list.
export TRIMSET_KEY=$ADMIN TRIMSET_ADMIN_KEY=$ADMIN TRIMSET_INGEST_KEY=$INGEST
trimset() {
  docker compose exec -T -e TRIMSET_KEY -e TRIMSET_URL=http://127.0.0.1:8810 server trimset "$@"
}
# What Claude Code runs, and what an MCP client starts: the agent's own key, which can send
# decisions and read policies but never approve one, and trimset's front door (the proxy).
agent() {
  TRIMSET_KEY=$INGEST docker compose exec -T -e TRIMSET_KEY \
    -e TRIMSET_URL="${AGENT_URL:-http://proxy:8800}" \
    -e XDG_CACHE_HOME=/tmp -e XDG_CONFIG_HOME=/tmp server trimset "$@"
}
# A PreToolUse event as Claude Code sends it: gate <label> <tool> <input field> <value>.
gate() {
  local label=$1 tool=$2 field=$3 value=$4
  python3 -c 'import json, sys
print(json.dumps({"hook_event_name": "PreToolUse", "tool_name": sys.argv[1],
                  "tool_input": {sys.argv[2]: sys.argv[3]}}))' "$tool" "$field" "$value" \
    | agent hook --model jev-latest ${GRANT:+--grant} 2>> "$OUT/hook.err" \
    | python3 "$HERE/show.py" hook "$label"
}

step "Day one: no history and no policy. trimset has nothing to go on, so a person decides"
gate "Bash: pytest -q" Bash command "pytest -q" | tee "$OUT/day-one.txt"

step "Two months of agent tool calls: the model rated each one, and what happened later
came back (a person's review of the ones it escalated, an incident for an unsafe one that ran)"
docker compose run --rm --no-deps -e TRIMSET_INGEST_KEY -e TRIMSET_ADMIN_KEY \
  --entrypoint trimset-datagen mock-upstream tool-approval --seed 3 --days 60 --per-day 200 \
  --url http://proxy:8800
# A high probability means "safe", so the policy accepts at the top. A call that ran and drew
# no incident in three weeks counts as safe; one still inside those weeks is not used yet.
curl -fsS -o /dev/null -X PATCH "$API/v1/admin/questions/safe_to_run" \
  -H @- -H 'content-type: application/json' \
  -d '{"settings": {"actions": {"high_means": "accept"},
       "default_rule": {"after_days": 21, "label": true}, "label_horizon_days": 21}}' \
  <<< "Authorization: Bearer $ADMIN"
docker compose restart worker > /dev/null # it applies that rule when it starts
sql() { docker compose exec -T postgres psql -U trimset -d trimset -tAc "$1"; }
last=-1 same=0
for _ in $(seq 90); do # until the worker has finished labelling
  rules=$(sql "SELECT count(*) FROM outcomes o JOIN projects p USING (project_id)
               WHERE p.name = '$project' AND o.source = 'rule'")
  if [ "$rules" -gt 0 ] && [ "$rules" = "$last" ]; then same=$((same + 1)); else same=0; fi
  [ "$same" -ge 3 ] && break
  last=$rules
  sleep 1
done
[ "$same" -ge 3 ] || fail "the worker did not apply the three-week rule"
echo "$rules calls with no incident in three weeks labelled safe"
SINCE=$(sql "SELECT now()") # decisions the agent makes from here are the ones listed at the end

step "trimset calibrate: is the model's confidence honest?"
trimset calibrate safe_to_run --model "$MODEL"

step "trimset policy compile: let calls through only as far as the history can certify"
policy=""
for budget in 0.005 0.02; do
  echo
  echo "Target: at most $(python3 -c "print(f'{$budget:.1%}')") of the calls it lets through" \
    "may be unsafe, with 95% confidence"
  if out=$(trimset --json policy compile safe_to_run --model "$MODEL" --error-budget "$budget"); then
    policy=$(policy_id <<< "$out")
    break
  fi
  echo "No policy at that target: trimset won't guess. Trying a looser one."
done
[ -n "$policy" ] || fail "no policy could be compiled"
trimset policy show "$policy"

step "The approver submits, approves and activates it, each with a reason on the record"
trimset policy submit "$policy" --reason "from the agent-gate demo"
trimset policy approve "$policy" --reason "guarantee read; unsafe calls let through stay in budget"
trimset policy activate "$policy" --reason "first policy for safe_to_run"

step "The hook (Claude Code runs it before each tool call) on nine calls, as it ships:
it can make Claude Code stricter but never more permissive"
echo "The stand-in model rates a call by a hash of it, not by what it does. These nine are"
echo "calls it happens to rate the way a person would, so the output reads naturally."
{
  gate "Bash: pytest -q" Bash command "pytest -q"
  gate "Bash: pwd" Bash command "pwd"
  gate "Bash: git diff --stat" Bash command "git diff --stat"
  gate "Bash: ruff check ." Bash command "ruff check ."
  gate "Bash: git push --force origin main" Bash command "git push --force origin main"
  gate "Bash: git reset --hard HEAD~10" Bash command "git reset --hard HEAD~10"
  gate "Bash: docker system prune -af" Bash command "docker system prune -af"
  gate "Bash: dd if=/dev/zero of=/dev/sda" Bash command "dd if=/dev/zero of=/dev/sda"
  gate "Bash: rm -rf /" Bash command "rm -rf /"
} | tee "$OUT/calls.txt"

step "--grant (off by default) lets a call the policy accepts run without a prompt"
GRANT=1 gate "Bash: pytest -q" Bash command "pytest -q" | tee "$OUT/grant.txt"

step "If trimset can't be reached the answer is ask, never silence and never allow"
AGENT_URL=http://127.0.0.1:1 GRANT=1 gate "Bash: pytest -q, trimset down" Bash command "pytest -q" \
  | tee "$OUT/down.txt"

step "The same policy over MCP, for agents that call tools: list them, gate a call, explain"
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"agent-gate-demo","version":"0"}}}'
{
  echo "$INIT"
  echo '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  echo '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
  echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"gate","arguments":{"state":{"tool":"Bash","input":{"command":"ruff check ."}}}}}'
  echo '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"explain","arguments":{"probability":0.97}}}'
} | agent mcp --model jev-latest > "$OUT/mcp.jsonl"
python3 "$HERE/show.py" mcp < "$OUT/mcp.jsonl"

step "Later that call turns out to have caused an incident. The agent reports it,
and the next refit learns from it"
id=$(python3 "$HERE/show.py" decision-id < "$OUT/mcp.jsonl")
[[ $id =~ ^[0-9a-f-]{36}$ ]] || fail "not a decision id: $id"
for _ in $(seq 30); do # the proxy logs a decision just after answering
  n=$(sql "SELECT count(*) FROM decisions WHERE decision_id = '$id'")
  [ "$n" = 1 ] && break
  sleep 0.5
done
{
  echo "$INIT"
  printf '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"outcome","arguments":{"decision_id":"%s","safe":false}}}\n' "$id"
} | agent mcp --model jev-latest | python3 "$HERE/show.py" outcome

step "What trimset recorded for the agent: each decision, the action taken, and the incident"
docker compose restart worker > /dev/null # it joins held action reports and outcomes at start
pid="(SELECT project_id FROM projects WHERE name = '$project')"
for _ in $(seq 60); do
  waiting=$(sql "SELECT count(*) FROM decisions WHERE project_id = $pid
                 AND received_at >= '$SINCE' AND action_taken IS NULL")
  [ "$waiting" = 0 ] && break
  sleep 1
done
docker compose exec -T postgres psql -U trimset -d trimset -c "
  SELECT to_char(d.ts, 'HH24:MI:SS') AS at, round(d.probs[1]::numeric, 3) AS model_says_safe,
         coalesce(d.action_taken, '-') AS action, d.action_source AS source,
         coalesce(d.policy_version::text, '-') AS policy,
         coalesce('safe=' || o.label::text || ' (' || o.source || ')', '') AS later
  FROM decisions d LEFT JOIN outcomes o USING (project_id, decision_id)
  WHERE d.project_id = $pid AND d.received_at >= '$SINCE' ORDER BY d.ts"

# What CI checks. The policy is compiled from this run's history, so which of the nine calls
# gets through varies; these hold whatever the thresholds are.
grep -q '^  ask ' "$OUT/day-one.txt" && grep -q "no approved policy" "$OUT/day-one.txt" \
  || fail "with no policy the hook should ask"
grep -Eq '^  (ask|deny) ' "$OUT/calls.txt" || fail "at least one of the nine should need a person"
! grep -q '^  allow ' "$OUT/calls.txt" || fail "without --grant the hook must never allow"
! grep -q '^  silent ' "$OUT/grant.txt" || fail "with --grant an accepted call should be allowed"
grep -q '^  ask ' "$OUT/down.txt" || fail "with trimset down, even with --grant, the hook should ask"

step "Done. Stop it with: docker compose -f compose.yaml -f compose.mock.yaml down -v"
