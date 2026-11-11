"""GET /v1/policies: the bundle SDKs poll (T3.2, FR-32, ADR 0008).

The project's named questions, its active policies with their guarantee status, and the
key for state hashes. The ETag is the hash of the canonical bundle (hashing.md §5), so any
activation, rollback, rename or change of guarantee status changes it, and a poll with a
matching If-None-Match gets an empty 304.
"""

import base64
import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from trimset.hashing import bundle_etag
from trimset_server.auth import require
from trimset_server.capture import _state_key as state_key
from trimset_server.errors import Problem
from trimset_server.keys import Principal
from trimset_server.policies import guarantee_status

router = APIRouter()
Ingest = Annotated[Principal, Depends(require("ingest"))]


def matches(header: str | None, etag: str) -> bool:
    """Whether an If-None-Match header names `etag` (weak or strong, or `*`)."""
    if not header:
        return False
    tags = {t.strip().removeprefix("W/").strip('"') for t in header.split(",")}
    return "*" in tags or etag in tags


@router.get("/v1/policies")
async def policy_bundle(request: Request, principal: Ingest) -> Response:
    key = state_key(request.app.state, principal.project_id)
    if key is None:
        raise Problem(
            503,
            "unavailable",
            "The server has no TRIMSET_SECRET",
            "Without it there is no state-hash key to give SDKs (ADR 0009).",
        )
    async with request.app.state.pool.acquire() as conn:
        questions = await conn.fetch(
            "SELECT name, question_id, definition FROM questions "
            "WHERE project_id = $1 AND name IS NOT NULL",
            principal.project_id,
        )
        policies = await conn.fetch(
            "SELECT body FROM policies WHERE project_id = $1 AND state = 'active' "
            "ORDER BY question_id, model_served",
            principal.project_id,
        )
    bundle: dict[str, Any] = {
        "schema": "bundle/1",
        "project_id": str(principal.project_id),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "state_hash_key": {"alg": "hmac-sha256/1", "key_b64": base64.b64encode(key).decode()},
        "questions": {
            q["name"]: {"question_id": q["question_id"], "definition": json.loads(q["definition"])}
            for q in questions
        },
        "policies": [
            {"policy": policy, "guarantee_status": guarantee_status(policy)}
            for policy in (json.loads(p["body"]) for p in policies)
        ],
    }
    bundle["etag"] = bundle_etag(bundle)
    headers = {"ETag": f'"{bundle["etag"]}"', "Cache-Control": "no-cache"}
    if matches(request.headers.get("if-none-match"), bundle["etag"]):
        return Response(status_code=304, headers=headers)
    return JSONResponse(bundle, headers=headers)
