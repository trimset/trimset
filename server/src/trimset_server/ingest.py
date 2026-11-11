"""POST /v1/decisions:log (FR-01, FR-02, FR-03, FR-09).

Each record is checked on its own: valid records are stored, rejected ones come back by
index, and one bad record never sinks a batch.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from trimset.hashing import question_id as hash_question
from trimset_server.auth import require
from trimset_server.bodies import TooLarge, parse_json, read_body
from trimset_server.errors import Problem
from trimset_server.keys import Principal
from trimset_server.projects import ProjectSettings, load_settings
from trimset_server.records import SUM_TOLERANCE, QuestionIn, RecordIn
from trimset_server.store import store

log = logging.getLogger("trimset.ingest")
router = APIRouter()
Ingest = Annotated[Principal, Depends(require("ingest"))]

MAX_BODY = 1024 * 1024  # 1 MB (docs/03-api/README.md)
MAX_RECORDS = 1000
FUTURE_SLACK = timedelta(minutes=5)  # ADR 0003


async def read_json(request: Request, limit: int) -> Any:
    try:
        return parse_json(await read_body(request, limit))
    except TooLarge as e:
        raise Problem(413, "too-large", str(e)) from None
    except ValueError:
        raise Problem(400, "invalid-request", "The body is not valid JSON") from None


def has_nul(value: Any) -> bool:
    """Postgres text and jsonb can't hold U+0000, so it's refused per record up front."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, list):
        return any(has_nul(v) for v in value)
    if isinstance(value, dict):
        return any(has_nul(k) or has_nul(v) for k, v in value.items())
    return False


@dataclass
class _Batch:
    errors: list[dict[str, Any]] = field(default_factory=list)
    valid: list[tuple[int, RecordIn]] = field(default_factory=list)

    def reject(self, index: int, message: str, at: str = "") -> None:
        pointer = f"/records/{index}" + (f"/{at}" if at else "")
        self.errors.append({"index": index, "pointer": pointer, "message": message})


def _shape(records: list[Any]) -> _Batch:
    batch = _Batch()
    for i, raw in enumerate(records):
        if has_nul(raw):
            batch.reject(i, "text must not contain the NUL character")
            continue
        try:
            batch.valid.append((i, RecordIn.model_validate(raw)))
        except ValidationError as e:
            first = e.errors()[0]  # one entry per rejected record (openapi BatchResult)
            at = "/".join(str(p) for p in first.get("loc", ()))
            batch.reject(i, first["msg"], at)  # never the submitted value
    return batch


async def _definitions(
    conn: Any, project_id: uuid.UUID, wanted: set[str], sent: dict[str, Any]
) -> tuple[dict[str, QuestionIn], dict[str, Any], dict[str, str]]:
    """(known definitions, new definitions to register, problems by question_id)."""
    rows = await conn.fetch(
        "SELECT question_id, definition FROM questions "
        "WHERE project_id = $1 AND question_id = ANY($2::text[])",
        project_id,
        list(wanted),
    )
    known = {r["question_id"]: QuestionIn.model_validate(json.loads(r["definition"])) for r in rows}
    new: dict[str, Any] = {}
    problems: dict[str, str] = {}
    for qid in wanted - known.keys():
        if qid not in sent:
            problems[qid] = "unknown question: send its definition in `questions`"
            continue
        if has_nul(sent[qid]):
            problems[qid] = "the question's definition contains the NUL character"
            continue
        try:
            definition = QuestionIn.model_validate(sent[qid])
            matches = hash_question(sent[qid]) == qid
        except (ValidationError, ValueError):
            problems[qid] = "the question's definition is not a valid question/1"
            continue
        if not matches:
            problems[qid] = "the question's definition doesn't hash to its question_id"
            continue
        known[qid], new[qid] = definition, sent[qid]
    return known, new, problems


@router.post("/v1/decisions:log")
async def log_decisions(request: Request, principal: Ingest) -> JSONResponse:
    body = await read_json(request, MAX_BODY)
    if not isinstance(body, dict) or not body.keys() <= {"questions", "records", "client"}:
        raise Problem(400, "invalid-request", "Expected an object with `records`")
    records, sent = body.get("records"), body.get("questions", {})
    if not isinstance(records, list) or not records:
        raise Problem(400, "invalid-request", "`records` must be a non-empty array")
    if not isinstance(sent, dict):
        raise Problem(400, "invalid-request", "`questions` must be an object keyed by question_id")
    if len(records) > MAX_RECORDS:
        raise Problem(413, "too-large", f"At most {MAX_RECORDS} records per batch")
    client = body.get("client")
    dropped = client.get("dropped_since_last") if isinstance(client, dict) else None
    if isinstance(dropped, int) and not isinstance(dropped, bool) and dropped > 0:
        log.warning("an SDK reported %d dropped records", dropped)

    batch = _shape(records)
    async with request.app.state.pool.acquire() as conn:
        settings = await load_settings(conn, principal.project_id) or ProjectSettings()
        wanted = {r.question_id for _, r in batch.valid}
        known, new, problems = await _definitions(conn, principal.project_id, wanted, sent)

        now = datetime.now(UTC)
        oldest = now - timedelta(days=settings.retention_days)
        good: list[tuple[int, RecordIn]] = []
        names: dict[str, str] = {}
        for i, r in batch.valid:
            question = known.get(r.question_id)
            if question is None:
                batch.reject(i, problems[r.question_id], "question_id")
            elif question.type != r.type:
                batch.reject(i, f"the question is a {question.type} question", "type")
            elif r.probabilities is not None and len(r.probabilities) != question.size():
                need = question.size()
                batch.reject(i, f"the question needs {need} probabilities", "probabilities")
            elif (
                r.probabilities is not None
                and r.type != "noul"
                and abs(sum(r.probabilities) - 1) > SUM_TOLERANCE
            ):
                batch.reject(i, "probabilities must sum to 1", "probabilities")
            elif not oldest <= r.ts <= now + FUTURE_SLACK:
                batch.reject(i, "ts is outside the project's retention or in the future", "ts")
            elif bad := settings.disallowed_tags(r.tags):
                batch.reject(i, f"tag key {bad[0]!r} is not on the project's allow-list", "tags")
            else:
                good.append((i, r))
                if r.question_name and r.question_id in new:
                    names.setdefault(r.question_id, r.question_name)

        stored = refused = 0
        if good:
            used = {r.question_id for _, r in good}
            stored, refused = await store(
                conn,
                principal.project_id,
                good,
                {q: d for q, d in new.items() if q in used},
                names,
                lambda i: batch.reject(i, "the database refused this record"),
            )

    result: dict[str, Any] = {
        "stored": stored,
        "duplicates": len(good) - stored - refused,
        "rejected": len(records) - len(good) + refused,
    }
    if batch.errors:
        result["errors"] = sorted(batch.errors, key=lambda e: e["index"])
    return JSONResponse(result, status_code=207 if batch.errors else 200)
