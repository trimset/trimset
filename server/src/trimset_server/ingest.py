"""POST /v1/decisions:log (FR-01, FR-02, FR-03, FR-09).

Each record is checked on its own: valid records are stored, rejected ones come back by
index, and one bad record never sinks a batch.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from trimset.hashing import question_id as hash_question
from trimset_server.auth import require
from trimset_server.errors import Problem
from trimset_server.keys import Principal
from trimset_server.partitions import DEFAULT_RETENTION_DAYS, ensure_partitions
from trimset_server.records import SUM_TOLERANCE, QuestionIn, RecordIn

log = logging.getLogger("trimset.ingest")
router = APIRouter()
Ingest = Annotated[Principal, Depends(require("ingest"))]

MAX_BODY = 1024 * 1024  # 1 MB (docs/03-api/README.md)
MAX_RECORDS = 1000
FUTURE_SLACK = timedelta(minutes=5)  # ADR 0003


async def read_json(request: Request, limit: int) -> Any:
    """The body as JSON, refusing more than `limit` bytes before reading them all, and the
    NaN and Infinity that Python's parser would otherwise accept."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise Problem(413, "too-large", f"The body is over {limit} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise Problem(413, "too-large", f"The body is over {limit} bytes")

    def no_constants(name: str) -> Any:
        raise ValueError(f"{name} is not JSON")

    try:
        return json.loads(body, parse_constant=no_constants)
    except ValueError:
        raise Problem(400, "invalid-request", "The body is not valid JSON") from None


@dataclass
class _Batch:
    errors: list[dict[str, Any]]
    valid: list[tuple[int, RecordIn]]

    def reject(self, index: int, message: str, field: str = "") -> None:
        pointer = f"/records/{index}" + (f"/{field}" if field else "")
        self.errors.append({"index": index, "pointer": pointer, "message": message})


def _has_nul(value: Any) -> bool:
    """Postgres text and jsonb can't hold U+0000, so it's refused per record up front."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, list):
        return any(_has_nul(v) for v in value)
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    return False


def _shape(records: list[Any]) -> _Batch:
    batch = _Batch(errors=[], valid=[])
    for i, raw in enumerate(records):
        if _has_nul(raw):
            batch.reject(i, "text must not contain the NUL character")
            continue
        try:
            batch.valid.append((i, RecordIn.model_validate(raw)))
        except ValidationError as e:
            first = e.errors()[0]  # one entry per rejected record (openapi BatchResult)
            field = "/".join(str(p) for p in first.get("loc", ()))
            batch.reject(i, first["msg"], field)  # never the submitted value
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
        if _has_nul(sent[qid]):
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


async def _register(
    conn: Any, project_id: uuid.UUID, new: dict[str, Any], names: dict[str, str]
) -> None:
    """New questions, with their default names. A name moves to its newest wording; the
    wording it leaves keeps its history (ADR 0010)."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", str(project_id))
    for qid, definition in new.items():
        inserted = await conn.fetchval(
            "INSERT INTO questions (project_id, question_id, type, definition) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING RETURNING question_id",
            project_id,
            qid,
            definition["type"],
            json.dumps(definition),
        )
        name = names.get(qid)
        if inserted is None or name is None:
            continue
        previous = await conn.fetchval(
            "UPDATE questions SET name = NULL WHERE project_id = $1 AND name = $2 "
            "RETURNING question_id",
            project_id,
            name,
        )
        await conn.execute(
            "UPDATE questions SET name = $3, previous_question_id = $4 "
            "WHERE project_id = $1 AND question_id = $2",
            project_id,
            qid,
            name,
            previous,
        )


_INSERT = """
INSERT INTO decisions (
    project_id, ts, decision_id, request_id, question_id, type, format, model_requested,
    model_served, probs, refusal, latency_ms, cost_usd, policy_version, policy_fingerprint,
    action_taken, action_source, audit, tags, external_ref, state_hash, role, shadow_of)
SELECT $1, t.ts, t.decision_id, t.request_id, t.question_id, t.type, t.format,
    t.model_requested, t.model_served, t.probs::real[], t.refusal, t.latency_ms, t.cost_usd,
    t.policy_version, t.policy_fingerprint, t.action_taken, t.action_source, t.audit, t.tags,
    t.external_ref, t.state_hash, t.role, t.shadow_of
FROM unnest($2::timestamptz[], $3::uuid[], $4::uuid[], $5::text[], $6::text[], $7::text[],
    $8::text[], $9::text[], $10::text[], $11::bool[], $12::real[], $13::float8[], $14::int[],
    $15::text[], $16::text[], $17::text[], $18::bool[], $19::jsonb[], $20::text[], $21::text[],
    $22::text[], $23::uuid[])
    AS t(ts, decision_id, request_id, question_id, type, format, model_requested,
         model_served, probs, refusal, latency_ms, cost_usd, policy_version,
         policy_fingerprint, action_taken, action_source, audit, tags, external_ref,
         state_hash, role, shadow_of)
ON CONFLICT DO NOTHING
RETURNING decision_id
"""


def _columns(records: list[RecordIn]) -> list[list[Any]]:
    # Ragged probability arrays can't go through unnest() as real[][], so each travels as
    # a Postgres array literal and is cast back to real[] inside the insert.
    # Below 1e-37 a real (float4) can't hold the value; it is 0 for every purpose here.
    def probs(r: RecordIn) -> str | None:
        if r.probabilities is None:
            return None
        return "{" + ",".join(repr(0.0 if p < 1e-37 else float(p)) for p in r.probabilities) + "}"

    return [
        [r.ts for r in records],
        [r.decision_id for r in records],
        [r.request_id for r in records],
        [r.question_id for r in records],
        [r.type for r in records],
        [r.format for r in records],
        [r.model_requested for r in records],
        [r.model_served for r in records],
        [probs(r) for r in records],
        [r.refusal for r in records],
        [r.latency_ms for r in records],
        [r.cost_usd for r in records],
        [r.policy_version for r in records],
        [r.policy_fingerprint for r in records],
        [r.action_taken for r in records],
        [r.action_source for r in records],
        [r.audit for r in records],
        [json.dumps(r.tags) for r in records],
        [r.external_ref for r in records],
        [r.state_hash for r in records],
        [r.role for r in records],
        [r.shadow_of for r in records],
    ]


async def _store(conn: Any, project_id: uuid.UUID, records: list[RecordIn]) -> int:
    return len(await conn.fetch(_INSERT, project_id, *_columns(records)))


async def _insert(
    conn: Any, project_id: uuid.UUID, records: list[tuple[int, RecordIn]], batch: _Batch
) -> tuple[int, int]:
    """Store records, ignoring ones already stored. Returns (new, refused).

    One statement for the whole batch. If Postgres refuses it, a missing partition is
    created first (a retention just raised, before the worker's next run); after that,
    records go one at a time, so a record the database refuses is rejected alone and
    never sinks the batch."""
    rows = [r for _, r in records]
    try:
        return await _store(conn, project_id, rows), 0
    except asyncpg.CheckViolationError:
        await ensure_partitions(conn)
        try:
            return await _store(conn, project_id, rows), 0
        except (asyncpg.CheckViolationError, asyncpg.DataError):
            pass
    except asyncpg.DataError:
        pass
    stored = refused = 0
    for i, r in records:
        try:
            stored += await _store(conn, project_id, [r])
        except (asyncpg.CheckViolationError, asyncpg.DataError) as e:
            log.warning("the database refused record %d (%s)", i, type(e).__name__)
            batch.reject(i, "the database refused this record")
            refused += 1
    return stored, refused


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
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        settings = json.loads(
            await conn.fetchval(
                "SELECT settings FROM projects WHERE project_id = $1", principal.project_id
            )
            or "{}"
        )
        wanted = {r.question_id for _, r in batch.valid}
        known, new, problems = await _definitions(conn, principal.project_id, wanted, sent)

        now = datetime.now(UTC)
        retention = settings.get("retention_days")
        oldest = now - timedelta(
            days=retention if isinstance(retention, int) else DEFAULT_RETENTION_DAYS
        )
        allowed = (settings.get("privacy") or {}).get("tag_allowlist")
        good: list[tuple[int, RecordIn]] = []
        names: dict[str, str] = {}
        for i, r in batch.valid:
            question = known.get(r.question_id)
            if question is None:
                batch.reject(i, problems[r.question_id], "question_id")
            elif question.type != r.type:
                batch.reject(i, f"the question is a {question.type} question", "type")
            elif r.probabilities is not None and len(r.probabilities) != question.size():
                batch.reject(
                    i, f"the question needs {question.size()} probabilities", "probabilities"
                )
            elif (
                r.probabilities is not None
                and r.type != "noul"
                and abs(sum(r.probabilities) - 1) > SUM_TOLERANCE
            ):
                batch.reject(i, "probabilities must sum to 1", "probabilities")
            elif not oldest <= r.ts <= now + FUTURE_SLACK:
                batch.reject(i, "ts is outside the project's retention or in the future", "ts")
            elif isinstance(allowed, list) and not r.tags.keys() <= set(allowed):
                bad = sorted(r.tags.keys() - set(allowed))[0]
                batch.reject(i, f"tag key {bad!r} is not on the project's allow-list", "tags")
            else:
                good.append((i, r))
                if r.question_name and r.question_id in new:
                    names.setdefault(r.question_id, r.question_name)

        stored = refused = 0
        if good:
            used = {r.question_id for _, r in good}
            async with conn.transaction():
                await _register(
                    conn, principal.project_id, {q: d for q, d in new.items() if q in used}, names
                )
                pairs = sorted({(r.model_requested, r.model_served) for _, r in good})
                await conn.execute(
                    "INSERT INTO model_versions (project_id, model_requested, model_served) "
                    "SELECT $1, r, s FROM unnest($2::text[], $3::text[]) AS t(r, s) "
                    "ON CONFLICT DO NOTHING",
                    principal.project_id,
                    [p[0] for p in pairs],
                    [p[1] for p in pairs],
                )
            stored, refused = await _insert(conn, principal.project_id, good, batch)

    result: dict[str, Any] = {
        "stored": stored,
        "duplicates": len(good) - stored - refused,
        "rejected": len(records) - len(good) + refused,
    }
    if batch.errors:
        result["errors"] = sorted(batch.errors, key=lambda e: e["index"])
    return JSONResponse(result, status_code=207 if batch.errors else 200)
