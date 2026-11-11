"""Questions: resolving names, and GET/PATCH /v1/admin/questions/{question} (ADR 0010).

Only the settings that already do something are accepted; the rest of QuestionSettings in
docs/03-api/openapi.yaml arrives with the tasks that use it.
"""

import json
import re
import uuid
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trimset_server.auth import require
from trimset_server.bodies import TooLarge, parse_json, read_body
from trimset_server.errors import Problem
from trimset_server.keys import Principal
from trimset_server.projects import merge_patch
from trimset_server.records import Label, QuestionIn, fits

router = APIRouter(prefix="/v1/admin")
Admin = Annotated[Principal, Depends(require("admin"))]
QUESTION_ID = re.compile(r"q_[0-9a-f]{32}")

_CHAIN = """
WITH RECURSIVE chain(question_id, previous, depth) AS (
    SELECT question_id, previous_question_id, 1 FROM questions
    WHERE project_id = $1 AND name = $2
  UNION ALL
    SELECT q.question_id, q.previous_question_id, c.depth + 1
    FROM questions q JOIN chain c ON q.question_id = c.previous
    WHERE q.project_id = $1 AND c.depth < 50
)
SELECT question_id FROM chain ORDER BY depth
"""


async def question_ids(conn: Any, project_id: uuid.UUID, ref: str) -> list[str]:
    """The ids a question reference covers: an id is itself; a name is its current
    wording and every wording before it, newest first."""
    if QUESTION_ID.fullmatch(ref):
        return [ref]
    return [r["question_id"] for r in await conn.fetch(_CHAIN, project_id, ref)]


async def definitions(conn: Any, project_id: uuid.UUID, ids: set[str]) -> dict[str, QuestionIn]:
    rows = await conn.fetch(
        "SELECT question_id, definition FROM questions "
        "WHERE project_id = $1 AND question_id = ANY($2::text[])",
        project_id,
        list(ids),
    )
    return {r["question_id"]: QuestionIn.model_validate(json.loads(r["definition"])) for r in rows}


class DefaultRule(BaseModel):
    """FR-13: with no outcome after `after_days`, record `label` with source rule."""

    model_config = ConfigDict(strict=True, extra="forbid")
    after_days: Annotated[int, Field(ge=1, le=400)]
    label: Label


class QuestionSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    default_rule: DefaultRule | None = None
    label_horizon_days: Annotated[int, Field(ge=0, le=400)] | None = None


async def _row(conn: Any, project_id: uuid.UUID, ref: str) -> Any:
    ids = await question_ids(conn, project_id, ref)
    row = None
    if ids:
        row = await conn.fetchrow(
            "SELECT question_id, name, type, definition, settings, previous_question_id, "
            "first_seen FROM questions WHERE project_id = $1 AND question_id = $2",
            project_id,
            ids[0],
        )
    if row is None:
        raise Problem(404, "not-found", "No such question in this project")
    return row


async def _view(conn: Any, project_id: uuid.UUID, row: Any) -> dict[str, Any]:
    # ponytail: a GROUP BY over the question's decisions; fine at v0.1 volumes, move to a
    # per-question model table if this gets slow.
    models = await conn.fetch(
        "SELECT model_requested, model_served, min(ts) AS first_seen FROM decisions "
        "WHERE project_id = $1 AND question_id = $2 GROUP BY 1, 2 ORDER BY 3 LIMIT 50",
        project_id,
        row["question_id"],
    )
    return {
        "question_id": row["question_id"],
        "name": row["name"],
        "type": row["type"],
        "definition": json.loads(row["definition"]),
        "first_seen": row["first_seen"].isoformat(),
        "models": [
            {
                "model_requested": m["model_requested"],
                "model_served": m["model_served"],
                "first_seen": m["first_seen"].isoformat(),
            }
            for m in models
        ],
        "previous_question_id": row["previous_question_id"],
        "settings": json.loads(row["settings"]),
    }


@router.get("/questions/{question}")
async def get_question(question: str, request: Request, admin: Admin) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        return await _view(conn, admin.project_id, await _row(conn, admin.project_id, question))


@router.patch("/questions/{question}")
async def update_question(question: str, request: Request, admin: Admin) -> dict[str, Any]:
    try:
        patch = parse_json(await read_body(request, 64 * 1024))
    except TooLarge as e:
        raise Problem(413, "too-large", str(e)) from None
    except ValueError:
        raise Problem(400, "invalid-request", "The body is not valid JSON") from None
    if not isinstance(patch, dict) or not patch.keys() <= {"name", "settings"}:
        raise Problem(400, "invalid-request", "Expected a merge patch with `name` and `settings`")

    async with request.app.state.pool.acquire() as conn, conn.transaction():
        row = await _row(conn, admin.project_id, question)
        settings = merge_patch(json.loads(row["settings"]), patch.get("settings") or {})
        try:
            checked = QuestionSettings.model_validate(settings)
        except ValidationError as e:
            errors = [
                {
                    "pointer": "/settings/" + "/".join(str(p) for p in err["loc"]),
                    "message": err["msg"],
                }
                for err in e.errors()
            ]
            raise Problem(
                422, "invalid-request", "Unknown or invalid settings", errors=errors
            ) from None
        rule = checked.default_rule
        if rule is not None:
            definition = QuestionIn.model_validate(json.loads(row["definition"]))
            if not fits(definition, rule.label):
                raise Problem(422, "invalid-request", "The default label doesn't fit the question")

        name = patch.get("name", row["name"])
        if name is not None and not (isinstance(name, str) and 1 <= len(name) <= 128):
            raise Problem(422, "invalid-request", "`name` must be 1 to 128 characters")
        try:
            await conn.execute(
                "UPDATE questions SET name = $3, settings = $4::jsonb "
                "WHERE project_id = $1 AND question_id = $2",
                admin.project_id,
                row["question_id"],
                name,
                json.dumps(checked.model_dump(mode="json", exclude_none=True)),
            )
        except asyncpg.UniqueViolationError:
            raise Problem(409, "name-taken", "Another question already has this name") from None
        updated = await _row(conn, admin.project_id, row["question_id"])
        return await _view(conn, admin.project_id, updated)
