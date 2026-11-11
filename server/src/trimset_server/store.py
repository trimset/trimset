"""Storing decision records: shared by the log API and the proxy (FR-01, FR-03).

Questions are registered on first sight, model versions recorded, and records inserted in
one statement that ignores ones already stored. A record Postgres refuses is rejected
alone and never sinks the rest.
"""

import json
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import asyncpg

from trimset_server.partitions import ensure_partitions
from trimset_server.records import RecordIn

log = logging.getLogger("trimset.store")

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
    # a Postgres array literal and is cast back to real[] inside the insert. Below 1e-37 a
    # real (float4) can't hold the value; it is 0 for every purpose here.
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


async def _insert(conn: Any, project_id: uuid.UUID, records: list[RecordIn]) -> int:
    return len(await conn.fetch(_INSERT, project_id, *_columns(records)))


async def _register(
    conn: Any, project_id: uuid.UUID, definitions: Mapping[str, Any], names: Mapping[str, str]
) -> None:
    """New questions, with their default names. A name moves to its newest wording; the
    wording it leaves keeps its history (ADR 0010). Known questions are left as they are."""
    if not definitions:
        return
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", str(project_id))
    for qid, definition in definitions.items():
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


async def store(
    conn: Any,
    project_id: uuid.UUID,
    records: list[tuple[int, RecordIn]],
    definitions: Mapping[str, Any],
    names: Mapping[str, str],
    refused: Callable[[int], None],
) -> tuple[int, int]:
    """Register `definitions`, record model versions, insert `records`.

    Returns (new, refused). `refused(index)` is called for each record the database won't
    take: if the batch statement fails, a missing partition is created first (a retention
    just raised, before the worker's next run), then records go one at a time."""
    rows = [r for _, r in records]
    async with conn.transaction():
        await _register(conn, project_id, definitions, names)
        pairs = sorted({(r.model_requested, r.model_served) for r in rows})
        await conn.execute(
            "INSERT INTO model_versions (project_id, model_requested, model_served) "
            "SELECT $1, r, s FROM unnest($2::text[], $3::text[]) AS t(r, s) "
            "ON CONFLICT DO NOTHING",
            project_id,
            [p[0] for p in pairs],
            [p[1] for p in pairs],
        )
    try:
        return await _insert(conn, project_id, rows), 0
    except asyncpg.CheckViolationError:
        await ensure_partitions(conn)
        try:
            return await _insert(conn, project_id, rows), 0
        except (asyncpg.CheckViolationError, asyncpg.DataError):
            pass
    except asyncpg.DataError:
        pass
    stored = count = 0
    for index, record in records:
        try:
            stored += await _insert(conn, project_id, [record])
        except (asyncpg.CheckViolationError, asyncpg.DataError) as e:
            log.warning("the database refused record %d (%s)", index, type(e).__name__)
            refused(index)
            count += 1
    return stored, count
