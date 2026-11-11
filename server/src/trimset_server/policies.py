"""Policies: reading them (T2.7), and their lifecycle (T3.1, FR-27 to FR-29).

A policy moves draft -> proposed -> approved -> active -> retired, or proposed ->
rejected. Activating one retires the group's active policy; a rollback retires the active
one and re-activates the version that was active before it. Every change is an event on
the policy and an entry in the audit log, in one transaction, under a lock per group
(question and served model). Only admin keys get here (FR-28). A guarantee is certified
or none until drift checks can suspend it (M4).
"""

import json
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from trimset_server import audit
from trimset_server.auth import require
from trimset_server.errors import Problem
from trimset_server.keys import Principal
from trimset_server.questions import _row as question_row
from trimset_server.questions import question_ids

router = APIRouter(prefix="/v1/admin")
Admin = Annotated[Principal, Depends(require("admin"))]
STATES = ("draft", "proposed", "approved", "active", "retired", "rejected")
Action = Literal["submit", "approve", "reject", "activate", "retire"]
TRANSITIONS: dict[str, tuple[str, str]] = {  # FR-27: action -> (from, to)
    "submit": ("draft", "proposed"),
    "approve": ("proposed", "approved"),
    "reject": ("proposed", "rejected"),
    "activate": ("approved", "active"),
    "retire": ("active", "retired"),
}
NEEDS_REASON = {"reject", "retire"}  # FR-28; a rollback always needs one too


class TransitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    reason: str | None = Field(default=None, max_length=1000)


class RollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_served: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=1000)


def next_state(action: str, state: str) -> str | None:
    """Where `action` takes a policy in `state`, or None if it can't."""
    start, end = TRANSITIONS[action]
    return end if state == start else None


def _event(row: Any) -> dict[str, Any]:
    return {
        "from_state": row["from_state"],
        "to_state": row["to_state"],
        "key_id": str(row["key_id"]) if row["key_id"] else None,
        "reason": row["reason"],
        "at": row["at"].isoformat(),
    }


def view(row: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    policy = json.loads(row["body"])
    return {
        "policy": policy,
        "state": row["state"],
        "guarantee_status": "certified" if policy["guarantee"]["status"] == "certified" else "none",
        "suspended_by": None,
        "events": events,
    }


async def views(conn: Any, rows: list[Any]) -> list[dict[str, Any]]:
    """The views of `rows`, with their events, oldest first."""
    found = await conn.fetch(
        "SELECT policy_id, from_state, to_state, key_id, reason, at FROM policy_events "
        "WHERE policy_id = ANY($1::uuid[]) ORDER BY id",
        [r["policy_id"] for r in rows],
    )
    events: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for e in found:
        events.setdefault(e["policy_id"], []).append(_event(e))
    return [view(r, events.get(r["policy_id"], [])) for r in rows]


async def _policy(conn: Any, project_id: uuid.UUID, policy_id: uuid.UUID) -> Any:
    row = await conn.fetchrow(
        "SELECT * FROM policies WHERE project_id = $1 AND policy_id = $2",
        project_id,
        policy_id,
    )
    if row is None:
        raise Problem(404, "not-found", "No such policy in this project")
    return row


async def _lock_group(conn: Any, project_id: uuid.UUID, question_id: str, model: str) -> None:
    """Serializes lifecycle changes within one group, until the transaction ends."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext('policies'), hashtext($1))",
        f"{project_id}/{question_id}/{model}",
    )


async def _active(conn: Any, project_id: uuid.UUID, question_id: str, model: str) -> Any:
    return await conn.fetchrow(
        "SELECT * FROM policies WHERE project_id = $1 AND question_id = $2 "
        "AND model_served = $3 AND state = 'active'",
        project_id,
        question_id,
        model,
    )


async def move(
    conn: Any,
    row: Any,
    to: str,
    key_id: uuid.UUID | None,
    reason: str | None,
    action: str,
) -> None:
    """Moves one policy to `to`, with its event and audit entry. Inside a transaction
    holding the group's lock."""
    policy_id, project_id = row["policy_id"], row["project_id"]
    await conn.execute("UPDATE policies SET state = $2 WHERE policy_id = $1", policy_id, to)
    await conn.execute(
        "INSERT INTO policy_events (project_id, policy_id, from_state, to_state, key_id, reason) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        project_id,
        policy_id,
        row["state"],
        to,
        key_id,
        reason,
    )
    await audit.record(
        conn,
        project_id,
        key_id,
        f"policy.{action}",
        f"policy:{policy_id}",
        {"state": row["state"]},
        {"state": to, "reason": reason},
    )


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
            "SELECT * FROM policies WHERE project_id = $1 "
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
        return {"items": await views(conn, list(rows)), "next_cursor": None}


@router.get("/policies/{policy_id}")
async def get_policy(policy_id: uuid.UUID, request: Request, admin: Admin) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        (out,) = await views(conn, [await _policy(conn, admin.project_id, policy_id)])
        return out


@router.post("/policies/{policy_id}/transitions")
async def transition(
    policy_id: uuid.UUID, body: TransitionIn, request: Request, admin: Admin
) -> dict[str, Any]:
    reason = (body.reason or "").strip() or None
    if body.action in NEEDS_REASON and reason is None:
        raise Problem(422, "invalid-request", f"Give a reason to {body.action} a policy")
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        row = await _policy(conn, admin.project_id, policy_id)
        await _lock_group(conn, admin.project_id, row["question_id"], row["model_served"])
        row = await _policy(conn, admin.project_id, policy_id)  # as of the lock
        to = next_state(body.action, row["state"])
        if to is None:
            start = TRANSITIONS[body.action][0]
            raise Problem(
                409,
                "invalid-transition",
                f"Can't {body.action} a policy that is {row['state']}",
                detail=f"Only a {start} policy can be moved with {body.action}.",
            )
        if body.action == "activate":
            current = await _active(conn, admin.project_id, row["question_id"], row["model_served"])
            if current is not None:
                why = f"replaced by v{row['version']}"
                await move(conn, current, "retired", admin.key_id, why, "retire")
        await move(conn, row, to, admin.key_id, reason, body.action)
        (out,) = await views(conn, [await _policy(conn, admin.project_id, policy_id)])
        return out


@router.post("/questions/{question}/rollback")
async def rollback(
    question: str, body: RollbackIn, request: Request, admin: Admin
) -> dict[str, Any]:
    """FR-29: retire the active policy and re-activate the version active before it."""
    reason = body.reason.strip()
    if not reason:
        raise Problem(422, "invalid-request", "Give a reason to roll back")
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        question_id = (await question_row(conn, admin.project_id, question))["question_id"]
        model = body.model_served
        await _lock_group(conn, admin.project_id, question_id, model)
        current = await _active(conn, admin.project_id, question_id, model)
        if current is None:
            raise Problem(409, "nothing-active", f"No policy is active on {model}")
        previous = await conn.fetchrow(
            "SELECT * FROM policies p WHERE project_id = $1 AND question_id = $2 "
            "AND model_served = $3 AND state = 'retired' AND version < $4 "
            "AND EXISTS (SELECT 1 FROM policy_events e "
            "            WHERE e.policy_id = p.policy_id AND e.to_state = 'active') "
            "ORDER BY version DESC LIMIT 1",
            admin.project_id,
            question_id,
            model,
            current["version"],
        )
        if previous is None:
            raise Problem(
                409,
                "nothing-to-roll-back-to",
                f"No version before v{current['version']} was ever active on {model}",
            )
        await move(conn, current, "retired", admin.key_id, reason, "rollback")
        await move(conn, previous, "active", admin.key_id, reason, "rollback")
        (out,) = await views(conn, [await _policy(conn, admin.project_id, previous["policy_id"])])
        return out


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
            other = await _active(conn, admin.project_id, row["question_id"], row["model_served"])
            if other is not None and other["policy_id"] == policy_id:
                other = None
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
