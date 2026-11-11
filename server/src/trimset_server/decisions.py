"""Reading decisions: GET /v1/admin/decisions, newest first, as record/1 (T2.7).

`trimset decisions tail` polls it with `from`. The outcome filter supports `pending` for
now; `right` and `wrong` need the review work of M5.
"""

import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request

from trimset_server.auth import require
from trimset_server.errors import Problem
from trimset_server.keys import Principal
from trimset_server.questions import question_ids

router = APIRouter(prefix="/v1/admin")
Admin = Annotated[Principal, Depends(require("admin"))]


def record(row: Any) -> dict[str, Any]:
    """A decisions row as record/1."""
    return {
        "schema": "record/1",
        "decision_id": str(row["decision_id"]),
        "request_id": str(row["request_id"]) if row["request_id"] else None,
        "ts": row["ts"].isoformat(),
        "question_id": row["question_id"],
        "question_name": row["name"],
        "type": row["type"],
        "format": row["format"],
        "model_requested": row["model_requested"],
        "model_served": row["model_served"],
        "probabilities": [float(p) for p in row["probs"]] if row["probs"] is not None else None,
        "refusal": row["refusal"],
        "latency_ms": row["latency_ms"],
        "cost_usd": row["cost_usd"],
        "policy_version": row["policy_version"],
        "policy_fingerprint": row["policy_fingerprint"],
        "action_taken": row["action_taken"],
        "action_source": row["action_source"],
        "audit": row["audit"],
        "tags": json.loads(row["tags"]),
        "external_ref": row["external_ref"],
        "state_hash": row["state_hash"],
        "state_hash_alg": "hmac-sha256/1" if row["state_hash"] else None,
        "role": row["role"],
        "shadow_of": str(row["shadow_of"]) if row["shadow_of"] else None,
    }


@router.get("/decisions")
async def list_decisions(
    request: Request,
    admin: Admin,
    question: str | None = None,
    model_served: str | None = None,
    action: Literal["ACCEPT", "BLOCK", "SELECT", "ESCALATE"] | None = None,
    outcome: Literal["pending", "right", "wrong"] | None = None,
    external_ref: str | None = None,
    to: datetime | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    if outcome in ("right", "wrong"):
        raise Problem(
            422, "invalid-request", "outcome=right and outcome=wrong arrive with review (M5)"
        )
    since = request.query_params.get("from")  # `from` is a Python keyword
    try:
        start = datetime.fromisoformat(since) if since else None
    except ValueError:
        raise Problem(422, "invalid-request", "from must be an RFC 3339 timestamp") from None
    async with request.app.state.pool.acquire() as conn:
        ids = await question_ids(conn, admin.project_id, question) if question else None
        rows = await conn.fetch(
            "SELECT d.*, q.name FROM decisions d "
            "LEFT JOIN questions q "
            "ON q.project_id = d.project_id AND q.question_id = d.question_id "
            "WHERE d.project_id = $1 "
            "AND ($2::text[] IS NULL OR d.question_id = ANY($2::text[])) "
            "AND ($3::text IS NULL OR d.model_served = $3) "
            "AND ($4::text IS NULL OR d.action_taken = $4 "
            "     OR ($4 = 'SELECT' AND d.action_taken LIKE 'SELECT:%')) "
            "AND ($5::text IS NULL OR d.external_ref = $5) "
            "AND ($6::timestamptz IS NULL OR d.ts > $6) "
            "AND ($7::timestamptz IS NULL OR d.ts <= $7) "
            "AND (NOT $8 OR NOT EXISTS (SELECT 1 FROM outcomes o "
            "     WHERE o.project_id = d.project_id AND o.decision_id = d.decision_id)) "
            "ORDER BY d.ts DESC, d.decision_id DESC LIMIT $9",
            admin.project_id,
            ids,
            model_served,
            action,
            external_ref,
            start,
            to,
            outcome == "pending",
            max(1, min(limit, 1000)),
        )
    return {"items": [record(r) for r in rows], "next_cursor": None}
