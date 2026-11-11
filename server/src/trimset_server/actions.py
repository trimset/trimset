"""POST /v1/decisions:actions (FR-33): what the app did with a decision logged earlier.

In proxy mode the record is written by the recorder a moment after the response, so a
report can arrive first. Such reports are held for an hour and applied by the worker.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from trimset_server.auth import require
from trimset_server.errors import Problem
from trimset_server.ingest import read_json
from trimset_server.keys import Principal
from trimset_server.records import ActionIn

router = APIRouter()
Ingest = Annotated[Principal, Depends(require("ingest"))]
MAX_ACTIONS = 1000
HOLD_FOR = timedelta(hours=1)

_APPLY = """
UPDATE decisions d SET action_taken = w.a, action_source = w.s, action_reason = w.r,
    policy_version = w.v, policy_fingerprint = w.f, audit = w.au
FROM unnest($2::uuid[], $3::text[], $4::text[], $5::text[], $6::int[], $7::text[], $8::bool[])
    AS w(id, a, s, r, v, f, au)
WHERE d.project_id = $1 AND d.decision_id = w.id
RETURNING d.decision_id
"""


async def apply(conn: Any, project_id: uuid.UUID, reports: list[ActionIn]) -> set[uuid.UUID]:
    """Apply reports to logged decisions. Returns the decision ids it updated."""
    latest = {r.decision_id: r for r in reports}  # within a batch, the last report wins
    rows = list(latest.values())
    updated = await conn.fetch(
        _APPLY,
        project_id,
        [r.decision_id for r in rows],
        [r.action_taken for r in rows],
        [r.action_source for r in rows],
        [r.reason for r in rows],
        [r.policy_version for r in rows],
        [r.policy_fingerprint for r in rows],
        [r.audit for r in rows],
    )
    return {r["decision_id"] for r in updated}


@router.post("/v1/decisions:actions")
async def report_actions(request: Request, principal: Ingest) -> JSONResponse:
    body = await read_json(request, 1024 * 1024)
    items = body.get("actions") if isinstance(body, dict) and body.keys() == {"actions"} else None
    if not isinstance(items, list) or not items:
        raise Problem(400, "invalid-request", "Expected an object with a non-empty `actions`")
    if len(items) > MAX_ACTIONS:
        raise Problem(413, "too-large", f"At most {MAX_ACTIONS} reports per request")

    reports: list[ActionIn] = []
    errors = []
    for i, raw in enumerate(items):
        try:
            reports.append(ActionIn.model_validate(raw))
        except ValidationError as e:
            first = e.errors()[0]
            pointer = f"/actions/{i}/" + "/".join(str(p) for p in first.get("loc", ()))
            errors.append({"index": i, "pointer": pointer.rstrip("/"), "message": first["msg"]})

    now = datetime.now(UTC)
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        updated = await apply(conn, principal.project_id, reports) if reports else set()
        waiting = {r.decision_id: r for r in reports if r.decision_id not in updated}
        if waiting:
            await conn.executemany(
                "INSERT INTO pending_actions (project_id, decision_id, report, received_at, "
                "expires_at) VALUES ($1, $2, $3::jsonb, $4, $5)",
                [
                    (principal.project_id, d, r.model_dump_json(), now, now + HOLD_FOR)
                    for d, r in waiting.items()
                ],
            )

    result: dict[str, Any] = {
        "stored": len(updated),
        "duplicates": len(reports) - len(updated) - len(waiting),
        "rejected": len(errors),
        "held": len(waiting),
    }
    if errors:
        result["errors"] = errors
    return JSONResponse(result, status_code=207 if errors else 200)


async def apply_held(conn: Any) -> tuple[int, int]:
    """Apply held reports whose decisions have arrived; drop expired ones. Returns
    (applied, expired). Runs in the worker every minute."""
    applied = 0
    async with conn.transaction():
        rows = await conn.fetch(
            "SELECT id, project_id, report FROM pending_actions WHERE expires_at > now() "
            "ORDER BY id LIMIT 1000 FOR UPDATE SKIP LOCKED"
        )
        by_project: dict[uuid.UUID, list[tuple[int, ActionIn]]] = {}
        for r in rows:
            report = ActionIn.model_validate(json.loads(r["report"]))
            by_project.setdefault(r["project_id"], []).append((r["id"], report))
        done: list[int] = []
        for project_id, held in by_project.items():
            updated = await apply(conn, project_id, [report for _, report in held])
            done += [i for i, report in held if report.decision_id in updated]
            applied += len(updated)
        if done:
            await conn.execute("DELETE FROM pending_actions WHERE id = ANY($1::bigint[])", done)
        expired = await conn.fetch(
            "DELETE FROM pending_actions WHERE expires_at <= now() RETURNING id"
        )
    return applied, len(expired)
