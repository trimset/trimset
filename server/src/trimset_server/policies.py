"""Reading policies: the list, one policy, and a diff (T2.7, FR-22).

The lifecycle (submit, approve, activate, roll back) arrives in M3 (FR-27 to FR-29), with
`policy_events`; until then every policy is a draft, its events are empty, and a
guarantee is certified or none (suspension needs drift checks, M4).
"""

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from trimset_server.auth import require
from trimset_server.errors import Problem
from trimset_server.keys import Principal
from trimset_server.questions import question_ids

router = APIRouter(prefix="/v1/admin")
Admin = Annotated[Principal, Depends(require("admin"))]
STATES = ("draft", "proposed", "approved", "active", "retired", "rejected")


def view(row: Any) -> dict[str, Any]:
    policy = json.loads(row["body"])
    return {
        "policy": policy,
        "state": row["state"],
        "guarantee_status": "certified" if policy["guarantee"]["status"] == "certified" else "none",
        "suspended_by": None,
        "events": [],
    }


async def _policy(conn: Any, project_id: uuid.UUID, policy_id: uuid.UUID) -> Any:
    row = await conn.fetchrow(
        "SELECT body, state, question_id, model_served FROM policies "
        "WHERE project_id = $1 AND policy_id = $2",
        project_id,
        policy_id,
    )
    if row is None:
        raise Problem(404, "not-found", "No such policy in this project")
    return row


@router.get("/policies")
async def list_policies(
    request: Request,
    admin: Admin,
    question: str | None = None,
    model_served: str | None = None,
    state: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    if state is not None and state not in STATES:
        raise Problem(422, "invalid-request", f"state must be one of {', '.join(STATES)}")
    async with request.app.state.pool.acquire() as conn:
        ids = await question_ids(conn, admin.project_id, question) if question else None
        rows = await conn.fetch(
            "SELECT body, state FROM policies WHERE project_id = $1 "
            "AND ($2::text[] IS NULL OR question_id = ANY($2::text[])) "
            "AND ($3::text IS NULL OR model_served = $3) "
            "AND ($4::text IS NULL OR state = $4) "
            "ORDER BY created_at DESC LIMIT $5",
            admin.project_id,
            ids,
            model_served,
            state,
            max(1, min(limit, 1000)),
        )
    return {"items": [view(r) for r in rows], "next_cursor": None}


@router.get("/policies/{policy_id}")
async def get_policy(policy_id: uuid.UUID, request: Request, admin: Admin) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        return view(await _policy(conn, admin.project_id, policy_id))


@router.get("/policies/{policy_id}/diff")
async def diff_policy(
    policy_id: uuid.UUID, request: Request, admin: Admin, against: uuid.UUID | None = None
) -> dict[str, Any]:
    """Thresholds and eval metrics against another version: `against`, or the group's
    active policy. Without either, every `before` is null."""
    async with request.app.state.pool.acquire() as conn:
        row = await _policy(conn, admin.project_id, policy_id)
        if against is not None:
            other = await _policy(conn, admin.project_id, against)
        else:
            other = await conn.fetchrow(
                "SELECT body, state, question_id, model_served FROM policies "
                "WHERE project_id = $1 AND question_id = $2 AND model_served = $3 "
                "AND state = 'active' AND policy_id <> $4",
                admin.project_id,
                row["question_id"],
                row["model_served"],
                policy_id,
            )
    after = json.loads(row["body"])
    before = json.loads(other["body"]) if other is not None else None
    old = {r["id"]: r["threshold"] for r in (before or {}).get("rules", [])}
    new = {r["id"]: r["threshold"] for r in after["rules"]}
    return {
        "policy_id": str(policy_id),
        "against": before["policy_id"] if before else None,
        "rules": [
            {"id": rule, "before": old.get(rule), "after": new.get(rule)}
            for rule in sorted(old.keys() | new.keys())
        ],
        "metrics": {
            "before": before["metrics"]["eval"] if before else None,
            "after": after["metrics"]["eval"],
        },
    }
