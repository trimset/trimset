"""The audit log (FR-54): policy transitions, keys created and revoked, and settings
changes, each with the key that did it (none for a command run on the server), the time,
and the values before and after. `record` runs inside the change's own transaction.
"""

import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from trimset_server.auth import require
from trimset_server.errors import Problem
from trimset_server.keys import Principal

router = APIRouter(prefix="/v1/admin")
Admin = Annotated[Principal, Depends(require("admin"))]


async def record(
    conn: Any,
    project_id: uuid.UUID,
    key_id: uuid.UUID | None,
    action: str,
    target: str,
    before: Any = None,
    after: Any = None,
) -> None:
    await conn.execute(
        "INSERT INTO audit_log (project_id, key_id, action, target, before, after) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)",
        project_id,
        key_id,
        action,
        target,
        None if before is None else json.dumps(before),
        None if after is None else json.dumps(after),
    )


async def key_created(conn: Any, project_id: uuid.UUID, by: uuid.UUID | None, row: Any) -> None:
    after = {"scope": row["scope"], "label": row["label"], "prefix": row["prefix"]}
    await record(conn, project_id, by, "key.create", f"key:{row['key_id']}", None, after)


async def key_revoked(conn: Any, key_id: uuid.UUID, by: uuid.UUID | None, row: Any) -> None:
    """`row` is what keys.revoke_key returned; a key already revoked logs nothing."""
    if row is None or row["was"] is not None:
        return
    key = {"scope": row["scope"], "label": row["label"]}
    await record(
        conn,
        row["project_id"],
        by,
        "key.revoke",
        f"key:{key_id}",
        key | {"revoked": False},
        key | {"revoked": True},
    )


@router.get("/audit-log")
async def audit_log(
    request: Request, admin: Admin, to: datetime | None = None, limit: int = 100
) -> dict[str, Any]:
    since = request.query_params.get("from")  # `from` is a Python keyword
    try:
        start = datetime.fromisoformat(since) if since else None
    except ValueError:
        raise Problem(422, "invalid-request", "from must be an RFC 3339 timestamp") from None
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, key_id, action, target, before, after, at FROM audit_log "
            "WHERE project_id = $1 AND ($2::timestamptz IS NULL OR at > $2) "
            "AND ($3::timestamptz IS NULL OR at <= $3) ORDER BY id DESC LIMIT $4",
            admin.project_id,
            start,
            to,
            max(1, min(limit, 1000)),
        )
    return {
        "items": [
            {
                "id": r["id"],
                "key_id": str(r["key_id"]) if r["key_id"] else None,
                "action": r["action"],
                "target": r["target"],
                "before": json.loads(r["before"]) if r["before"] is not None else None,
                "after": json.loads(r["after"]) if r["after"] is not None else None,
                "at": r["at"].isoformat(),
            }
            for r in rows
        ],
        "next_cursor": None,
    }
