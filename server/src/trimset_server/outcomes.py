"""POST /v1/outcomes, and the joins and rules that run later (FR-10 to FR-13).

An outcome names its decision by id, or by external_ref and question. One that arrives
before its decision is held and joined later. The last label *received* wins, enforced in
SQL, so a held outcome joined late never overwrites a newer one; every label stays in
outcome_history.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from trimset_server.auth import require
from trimset_server.errors import Problem
from trimset_server.ingest import has_nul, read_json
from trimset_server.keys import Principal
from trimset_server.questions import definitions, question_ids
from trimset_server.records import OutcomeIn, fits

log = logging.getLogger("trimset.outcomes")
router = APIRouter()
Ingest = Annotated[Principal, Depends(require("ingest"))]

MAX_BODY = 1024 * 1024
MAX_OUTCOMES = 1000
HOLD_FOR = timedelta(days=30)  # FR-11
MAX_HELD = 100_000  # per project, so one project can't fill the table for the rest

# Held rows whose decision may have arrived, least recently tried first. By decision_id
# the check is exact; by external_ref the question can still differ, so a row tried and
# still held goes to the back of the line.
# ponytail: probes every held row until `batch` are found, at most MAX_HELD per project;
# drive the join from newly received decisions if that gets slow.
_DUE = """
SELECT * FROM unmatched_outcomes u
WHERE u.expires_at > now()
  AND (EXISTS (SELECT 1 FROM decisions d
               WHERE d.project_id = u.project_id AND d.decision_id = u.decision_id)
       OR EXISTS (SELECT 1 FROM decisions d
                  WHERE d.project_id = u.project_id AND d.external_ref = u.external_ref))
ORDER BY u.tried_at NULLS FIRST, u.id
LIMIT $1 FOR UPDATE OF u SKIP LOCKED
"""


@dataclass(frozen=True)
class Claim:
    """One outcome, from the API or from the held table."""

    index: int
    outcome_id: uuid.UUID | None
    decision_id: uuid.UUID | None
    external_ref: str | None
    question: str | None
    label: Any
    source: str
    observed_at: datetime
    received_at: datetime
    key_id: uuid.UUID | None


@dataclass
class Result:
    status: str  # matched | held | duplicate | rejected
    decision_ids: list[uuid.UUID]
    message: str | None = None


async def _seen(conn: Any, project_id: uuid.UUID, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    rows = await conn.fetch(
        "SELECT outcome_id FROM outcome_history WHERE project_id = $1 AND outcome_id = ANY($2) "
        "UNION SELECT outcome_id FROM unmatched_outcomes WHERE project_id = $1 "
        "AND outcome_id = ANY($2)",
        project_id,
        ids,
    )
    return {r["outcome_id"] for r in rows}


async def process(
    conn: Any, project_id: uuid.UUID, claims: list[Claim], check_duplicates: bool = True
) -> dict[int, Result]:
    """Match, check and record outcomes. Unmatched claims come back as `held`; the caller
    decides what holding means."""
    results: dict[int, Result] = {}

    if check_duplicates:
        given = [c.outcome_id for c in claims if c.outcome_id]
        seen = await _seen(conn, project_id, given) if given else set()
        fresh = []
        for c in claims:
            if c.outcome_id and c.outcome_id in seen:
                results[c.index] = Result("duplicate", [])
                continue
            if c.outcome_id:
                seen.add(c.outcome_id)
            fresh.append(c)
        claims = fresh

    by_id = await conn.fetch(
        "SELECT decision_id, question_id FROM decisions WHERE project_id = $1 "
        "AND decision_id = ANY($2::uuid[])",
        project_id,
        [c.decision_id for c in claims if c.decision_id],
    )
    question_of = {r["decision_id"]: r["question_id"] for r in by_id}

    covered: dict[str, list[str]] = {}
    for ref in {c.question for c in claims if c.external_ref and c.question}:
        covered[ref] = await question_ids(conn, project_id, ref)
    pairs = [
        (c.external_ref, q) for c in claims if c.external_ref for q in covered[c.question or ""]
    ]
    by_ref = await conn.fetch(
        "SELECT d.decision_id, d.question_id, d.external_ref FROM decisions d "
        "JOIN unnest($2::text[], $3::text[]) AS w(ref, qid) "
        "ON d.external_ref = w.ref AND d.question_id = w.qid WHERE d.project_id = $1",
        project_id,
        [p[0] for p in pairs],
        [p[1] for p in pairs],
    )
    found: dict[tuple[str, str], list[uuid.UUID]] = {}
    for r in by_ref:
        found.setdefault((r["external_ref"], r["question_id"]), []).append(r["decision_id"])
        question_of[r["decision_id"]] = r["question_id"]

    questions = await definitions(conn, project_id, set(question_of.values()))
    history: list[tuple[Claim, uuid.UUID]] = []
    for c in claims:
        if c.decision_id:
            matched = [c.decision_id] if c.decision_id in question_of else []
        else:
            matched = [
                d
                for q in covered[c.question or ""]
                for d in found.get((c.external_ref or "", q), [])
            ]
        if not matched:
            results[c.index] = Result("held", [])
            continue
        fitting = [d for d in matched if fits(questions[question_of[d]], c.label)]
        if not fitting:
            results[c.index] = Result("rejected", [], "the label doesn't fit the question")
            continue
        results[c.index] = Result("matched", fitting)
        history += [(c, d) for d in fitting]

    if history:
        await _write(conn, project_id, history, question_of)
    return results


async def _write(
    conn: Any,
    project_id: uuid.UUID,
    history: list[tuple[Claim, uuid.UUID]],
    question_of: dict[uuid.UUID, str],
) -> None:
    def columns(rows: list[tuple[Claim, uuid.UUID]]) -> list[list[Any]]:
        return [
            [d for _, d in rows],
            [c.outcome_id for c, _ in rows],
            [json.dumps(c.label) for c, _ in rows],
            [c.source for c, _ in rows],
            [c.observed_at for c, _ in rows],
            [c.received_at for c, _ in rows],
            [c.key_id for c, _ in rows],
        ]

    await conn.execute(
        "INSERT INTO outcome_history (project_id, decision_id, outcome_id, label, source, "
        "observed_at, received_at, key_id) "
        "SELECT $1, * FROM unnest($2::uuid[], $3::uuid[], $4::jsonb[], $5::text[], "
        "$6::timestamptz[], $7::timestamptz[], $8::uuid[]) "
        "ON CONFLICT DO NOTHING",
        project_id,
        *columns(history),
    )
    # One current label per decision: within a batch the later one wins, and against the
    # table the one received last wins (FR-12).
    latest: dict[uuid.UUID, tuple[Claim, uuid.UUID]] = {}
    for claim, decision in history:
        latest[decision] = (claim, decision)
    rows = list(latest.values())
    await conn.execute(
        "INSERT INTO outcomes (project_id, decision_id, question_id, label, source, "
        "observed_at, received_at, key_id) "
        "SELECT $1, w.d, w.q, w.l, w.s, w.o, w.r, w.k FROM unnest($2::uuid[], $3::text[], "
        "$4::jsonb[], $5::text[], $6::timestamptz[], $7::timestamptz[], $8::uuid[]) "
        "AS w(d, q, l, s, o, r, k) "
        "ON CONFLICT (project_id, decision_id) DO UPDATE SET label = EXCLUDED.label, "
        "source = EXCLUDED.source, observed_at = EXCLUDED.observed_at, "
        "received_at = EXCLUDED.received_at, key_id = EXCLUDED.key_id "
        "WHERE outcomes.received_at <= EXCLUDED.received_at",
        project_id,
        [d for _, d in rows],
        [question_of[d] for _, d in rows],
        [json.dumps(c.label) for c, _ in rows],
        [c.source for c, _ in rows],
        [c.observed_at for c, _ in rows],
        [c.received_at for c, _ in rows],
        [c.key_id for c, _ in rows],
    )


@router.post("/v1/outcomes")
async def post_outcomes(request: Request, principal: Ingest) -> JSONResponse:
    body = await read_json(request, MAX_BODY)
    items = body.get("outcomes") if isinstance(body, dict) and body.keys() == {"outcomes"} else None
    if not isinstance(items, list) or not items:
        raise Problem(400, "invalid-request", "Expected an object with a non-empty `outcomes`")
    if len(items) > MAX_OUTCOMES:
        raise Problem(413, "too-large", f"At most {MAX_OUTCOMES} outcomes per request")

    now = datetime.now(UTC)
    results: dict[int, Result] = {}
    claims: list[Claim] = []
    for i, raw in enumerate(items):
        if has_nul(raw):
            results[i] = Result("rejected", [], "text must not contain the NUL character")
            continue
        try:
            o = OutcomeIn.model_validate(raw)
        except ValidationError as e:
            results[i] = Result("rejected", [], e.errors()[0]["msg"])  # never the value
            continue
        claims.append(
            Claim(
                i,
                o.outcome_id,
                o.decision_id,
                o.external_ref,
                o.question,
                o.label,
                o.source,
                o.observed_at,
                now,
                principal.key_id,
            )
        )

    async with request.app.state.pool.acquire() as conn, conn.transaction():
        results |= await process(conn, principal.project_id, claims)
        held = [c for c in claims if results[c.index].status == "held"]
        if held:
            waiting = await conn.fetchval(
                "SELECT count(*) FROM (SELECT 1 FROM unmatched_outcomes WHERE project_id = $1 "
                "LIMIT $2) s",
                principal.project_id,
                MAX_HELD,
            )
            room = max(0, MAX_HELD - waiting)
            for c in held[room:]:
                results[c.index] = Result(
                    "rejected", [], f"{MAX_HELD} outcomes are already waiting for their decisions"
                )
            held = held[:room]
        if held:
            await conn.executemany(
                "INSERT INTO unmatched_outcomes (project_id, outcome_id, decision_id, "
                "external_ref, question, label, source, observed_at, received_at, key_id, "
                "expires_at) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11) "
                "ON CONFLICT DO NOTHING",
                [
                    (
                        principal.project_id,
                        c.outcome_id,
                        c.decision_id,
                        c.external_ref,
                        c.question if c.external_ref else None,
                        json.dumps(c.label),
                        c.source,
                        c.observed_at,
                        c.received_at,
                        c.key_id,
                        now + HOLD_FOR,
                    )
                    for c in held
                ],
            )

    counts = {
        s: sum(r.status == s for r in results.values()) for s in ("matched", "held", "rejected")
    }
    out: dict[str, Any] = counts | {
        "results": [
            {"index": i, "status": r.status}
            | ({"decision_ids": [str(d) for d in r.decision_ids]} if r.decision_ids else {})
            | ({"message": r.message} if r.message else {})
            for i, r in sorted(results.items())
        ]
    }
    return JSONResponse(out, status_code=207 if counts["rejected"] else 200)


async def join_held(conn: Any, batch: int = 1000) -> tuple[int, int]:
    """Join held outcomes whose decisions have arrived; drop expired ones. Returns
    (joined, expired). Runs in the worker every minute."""
    joined = 0
    async with conn.transaction():
        rows = await conn.fetch(_DUE, batch)
        by_project: dict[uuid.UUID, list[Any]] = {}
        for r in rows:
            by_project.setdefault(r["project_id"], []).append(r)
        done: list[int] = []
        tried: list[int] = []
        for project_id, held in by_project.items():
            claims = [
                Claim(
                    r["id"],
                    r["outcome_id"],
                    r["decision_id"],
                    r["external_ref"],
                    r["question"],
                    json.loads(r["label"]),
                    r["source"],
                    r["observed_at"],
                    r["received_at"],
                    r["key_id"],
                )
                for r in held
            ]
            results = await process(conn, project_id, claims, check_duplicates=False)
            for i, res in results.items():
                (tried if res.status == "held" else done).append(i)
            joined += sum(res.status == "matched" for res in results.values())
        if done:
            await conn.execute("DELETE FROM unmatched_outcomes WHERE id = ANY($1::bigint[])", done)
        if tried:
            await conn.execute(
                "UPDATE unmatched_outcomes SET tried_at = now() WHERE id = ANY($1::bigint[])", tried
            )
        expired = await conn.fetch(
            "DELETE FROM unmatched_outcomes WHERE expires_at <= now() RETURNING id"
        )
    if expired:
        log.warning("%d held outcomes expired without their decision", len(expired))
    return joined, len(expired)


async def apply_default_rules(conn: Any, batch: int = 5000) -> int:
    """FR-13: label matured decisions that still have no outcome. Runs in the worker."""
    rules = await conn.fetch(
        "SELECT project_id, question_id, settings->'default_rule' AS rule FROM questions "
        "WHERE settings ? 'default_rule'"
    )
    labelled = 0
    for r in rules:
        rule = json.loads(r["rule"])
        while True:
            status = await conn.execute(
                """
                WITH picked AS (
                    SELECT d.decision_id FROM decisions d
                    WHERE d.project_id = $1 AND d.question_id = $2
                      AND d.ts < now() - make_interval(days => $3)
                      AND NOT EXISTS (SELECT 1 FROM outcomes o
                                      WHERE o.project_id = $1 AND o.decision_id = d.decision_id)
                    LIMIT $5),
                added AS (
                    INSERT INTO outcomes (project_id, decision_id, question_id, label, source,
                                          observed_at, received_at)
                    SELECT $1, decision_id, $2, $4::jsonb, 'rule', now(), now() FROM picked
                    ON CONFLICT DO NOTHING RETURNING decision_id)
                INSERT INTO outcome_history (project_id, decision_id, label, source,
                                             observed_at, received_at)
                SELECT $1, decision_id, $4::jsonb, 'rule', now(), now() FROM added
                """,
                r["project_id"],
                r["question_id"],
                int(rule["after_days"]),
                json.dumps(rule["label"]),
                batch,
            )
            count = int(status.split()[-1])
            labelled += count
            if count < batch:
                break
    return labelled
