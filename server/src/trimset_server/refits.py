"""Scheduled refits (T3.3, FR-30): refits only propose.

Every minute the worker queues a project's `refit` job when its `refit_schedule` (cron,
UTC) has fallen due, catching up at most a day after downtime. The job refits each
group (question and served model) that has an active policy, with that policy's target,
audit fraction and window, and stores the result as a proposal, unless it is the same
policy again. A new proposal replaces the refits' earlier proposal still waiting in the
group. Nothing is ever activated: a person approves (FR-28). The "proposal ready" alert
arrives with alerting (T4.1); until then the proposal's event and the job's result say so.
"""

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from trimset_server import fitting, policies
from trimset_server.cron import Cron
from trimset_server.projects import REFIT_SCHEDULE

log = logging.getLogger("trimset.refits")
REASON = "scheduled refit"


async def schedule(conn: Any, now: datetime | None = None) -> int:
    """Queues the refits that are due; returns how many. Two workers queue each once."""
    now = now or datetime.now(UTC)
    rows = await conn.fetch(
        "SELECT p.project_id, coalesce(p.settings->>'refit_schedule', $1) AS cron, "
        "(SELECT max(scheduled_for) FROM jobs j "
        " WHERE j.project_id = p.project_id AND j.kind = 'refit') AS last "
        "FROM projects p",
        REFIT_SCHEDULE,
    )
    queued = 0
    for r in rows:
        try:
            cron = Cron(r["cron"])
        except ValueError:
            log.warning("project %s has an invalid refit_schedule", r["project_id"])
            continue
        # A project's first look starts now; later ones catch up at most a day.
        # ponytail: scans up to 1,440 minutes per project a minute; keep a per-worker
        # last-tick if projects number in the thousands.
        after = max(r["last"] or now - timedelta(minutes=2), now - timedelta(days=1))
        due = cron.latest(after, now)
        if due is None:
            continue
        status = await conn.execute(
            "INSERT INTO jobs (project_id, kind, payload, scheduled_for) "
            "VALUES ($1, 'refit', '{}'::jsonb, $2) "
            "ON CONFLICT (project_id, scheduled_for) WHERE kind = 'refit' DO NOTHING",
            r["project_id"],
            due,
        )
        queued += status == "INSERT 0 1"
    return queued


def _window_days(fit: Mapping[str, Any]) -> int:
    window = fit["provenance"]["window"]
    start, end = (datetime.fromisoformat(window[k].replace("Z", "+00:00")) for k in ("from", "to"))
    return max(1, round((end - start) / timedelta(days=1)))


async def run_refit(
    conn: Any, project_id: uuid.UUID, payload: Mapping[str, Any], key_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """The job: one result per group with an active policy."""
    actives = await conn.fetch(
        "SELECT p.body, f.body AS fit FROM policies p JOIN fits f ON f.fit_id = p.fit_id "
        "WHERE p.project_id = $1 AND p.state = 'active' ORDER BY p.question_id, p.model_served",
        project_id,
    )
    results = []
    for row in actives:
        active = json.loads(row["body"])
        group = {"question_id": active["question_id"], "model_served": active["model_served"]}
        try:
            results.append(group | await _refit(conn, project_id, group, active, row["fit"]))
        except fitting.JobError as e:
            results.append(group | {"error": str(e)})
    return {"groups": results}


async def _refit(
    conn: Any, project_id: uuid.UUID, group: dict[str, str], active: Any, fit: str
) -> dict[str, Any]:
    fitted = await fitting.run_fit(
        conn, project_id, group | {"window_days": _window_days(json.loads(fit))}
    )
    if "fit_id" not in fitted:
        return fitted  # not_enough_labels
    compile_payload = group | {
        "fit_id": fitted["fit_id"],
        "target": active["target"],
        "audit_fraction": active["audit_fraction"],
    }
    out = await fitting.run_compile(
        conn, project_id, compile_payload, None, state="proposed", reason=REASON, unless_like=active
    )
    if "policy_id" in out:
        await _supersede(conn, project_id, group, out)
        out["proposal_ready"] = True
    return out


async def _supersede(
    conn: Any, project_id: uuid.UUID, group: dict[str, str], new: Mapping[str, Any]
) -> None:
    """Rejects the refits' earlier proposals still waiting in the group."""
    async with conn.transaction():
        await policies.lock_group(conn, project_id, group["question_id"], group["model_served"])
        older = await conn.fetch(
            "SELECT * FROM policies p WHERE project_id = $1 AND question_id = $2 "
            "AND model_served = $3 AND state = 'proposed' AND policy_id <> $4 "
            "AND EXISTS (SELECT 1 FROM policy_events e WHERE e.policy_id = p.policy_id "
            "            AND e.from_state IS NULL AND e.key_id IS NULL)",
            project_id,
            group["question_id"],
            group["model_served"],
            uuid.UUID(new["policy_id"]),
        )
        for row in older:
            why = f"superseded by v{new['version']}"
            await policies.move(conn, row, "rejected", None, why, "reject")
