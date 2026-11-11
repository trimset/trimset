"""The admin API around calibration (T2.6): fits, compiles, the simulator and label
coverage. Fits and compiles run as jobs (jobs.py); the simulator answers at once, with
its arithmetic in a thread."""

import asyncio
import json
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trimset_server import fitting, jobs
from trimset_server.auth import require
from trimset_server.bodies import TooLarge, parse_json, read_body
from trimset_server.errors import Problem
from trimset_server.keys import Principal
from trimset_server.questions import _row as question_row
from trimset_server.records import QuestionIn
from trimset_stats.calibration import Calibrator
from trimset_stats.compiler import Costs

router = APIRouter(prefix="/v1/admin")
Admin = Annotated[Principal, Depends(require("admin"))]
Model = Annotated[str, Field(min_length=1, max_length=256)]
COVERAGE_DAYS = 90


class FitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model_served: Model
    window_days: Annotated[int, Field(ge=1, le=400)] = 90


class CompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model_served: Model
    fit_id: uuid.UUID | None = None
    target: dict[str, Any]
    audit_fraction: Annotated[float, Field(ge=0, le=0.5)] | None = None


class SimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model_served: Model
    fit_id: uuid.UUID | None = None
    candidates: (
        Annotated[list[Annotated[list[dict[str, Any]], Field(max_length=2)]], Field(max_length=500)]
        | None
    ) = None
    costs: dict[str, Any] | None = None
    # Thresholds are explored on the tune split by default: numbers for a threshold picked
    # by looking at them are optimistic, so the eval split stays for compiled policies.
    split: Literal["tune", "eval"] = "tune"


async def _json(request: Request) -> Any:
    try:
        return parse_json(await read_body(request, 1024 * 1024))
    except TooLarge as e:
        raise Problem(413, "too-large", str(e)) from None
    except ValueError:
        raise Problem(400, "invalid-request", "The body is not valid JSON") from None


def _parse(model: type[BaseModel], body: Any) -> Any:
    try:
        return model.model_validate(body)
    except ValidationError as e:
        errors = [
            {"pointer": "/" + "/".join(map(str, err["loc"])), "message": err["msg"]}
            for err in e.errors()
        ]
        raise Problem(
            422, "invalid-request", "The request doesn't match the contract", errors=errors
        ) from None


@router.post("/questions/{question}/fits")
async def create_fit(question: str, request: Request, admin: Admin) -> JSONResponse:
    body: FitRequest = _parse(FitRequest, await _json(request))
    async with request.app.state.pool.acquire() as conn:
        row = await question_row(conn, admin.project_id, question)
        payload = {"question_id": row["question_id"]} | body.model_dump()
        return await jobs.enqueue(conn, admin, "fit", payload)


@router.get("/questions/{question}/fits")
async def list_fits(
    question: str, request: Request, admin: Admin, model_served: str | None = None
) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        row = await question_row(conn, admin.project_id, question)
        fits = await conn.fetch(
            "SELECT body FROM fits WHERE project_id = $1 AND question_id = $2 "
            "AND ($3::text IS NULL OR model_served = $3) ORDER BY created_at DESC LIMIT 50",
            admin.project_id,
            row["question_id"],
            model_served,
        )
    return {"items": [json.loads(f["body"]) for f in fits]}


@router.get("/fits/{fit_id}")
async def get_fit(fit_id: uuid.UUID, request: Request, admin: Admin) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        body = await conn.fetchval(
            "SELECT body FROM fits WHERE project_id = $1 AND fit_id = $2", admin.project_id, fit_id
        )
    if body is None:
        raise Problem(404, "not-found", "No such fit in this project")
    return json.loads(body)  # type: ignore[no-any-return]


@router.post("/questions/{question}/compile")
async def compile_policy(question: str, request: Request, admin: Admin) -> JSONResponse:
    body: CompileRequest = _parse(CompileRequest, await _json(request))
    try:
        fitting.target_of(body.target)
    except fitting.JobError as e:
        raise Problem(422, "invalid-request", str(e)) from None
    async with request.app.state.pool.acquire() as conn:
        row = await question_row(conn, admin.project_id, question)
        payload = {"question_id": row["question_id"]} | body.model_dump(
            mode="json", exclude_none=True
        )
        return await jobs.enqueue(conn, admin, "compile", payload)


@router.post("/questions/{question}/simulate")
async def simulate(question: str, request: Request, admin: Admin) -> dict[str, Any]:
    body: SimulateRequest = _parse(SimulateRequest, await _json(request))
    async with request.app.state.pool.acquire() as conn:
        row = await question_row(conn, admin.project_id, question)
        try:
            fit = await fitting.fit_row(
                conn, admin.project_id, row["question_id"], body.model_served,
                str(body.fit_id) if body.fit_id else None,
            )  # fmt: skip
        except fitting.JobError as e:
            raise Problem(404, "not-found", str(e)) from None
    definition = QuestionIn.model_validate(json.loads(row["definition"]))
    fit_body = json.loads(fit["body"])
    try:
        actions = fitting.actions_of(definition, json.loads(row["settings"]))
        candidates = (
            [[fitting.rule_of(r) for r in rules] for rules in body.candidates]
            if body.candidates is not None
            else None
        )
        costs = (
            Costs(float(body.costs.get("escalation", 0.0)), dict(body.costs.get("wrong") or {}))
            if body.costs
            else None
        )
        tune, evaluation = fitting.unpack(fit["splits"])
        split = tune if body.split == "tune" else evaluation
        cal = Calibrator.from_json(fit_body["calibration"])
        rows = await asyncio.to_thread(
            fitting.simulate_rows, definition, cal, split, actions, candidates, costs
        )
        reliability = await asyncio.to_thread(fitting.calibrated_bins, definition, cal, split)
    except (fitting.JobError, KeyError, TypeError, ValueError) as e:
        raise Problem(422, "invalid-request", f"Can't simulate these candidates: {e}") from None
    return {
        "fit_id": fit_body["fit_id"],
        "split": body.split,
        "reliability": reliability,
        "rows": rows,
    }


@router.get("/questions/{question}/coverage")
async def coverage(
    question: str, request: Request, admin: Admin, model_served: str | None = None
) -> dict[str, Any]:
    """FR-14: per served model and action path, over the label horizon plus 90 days, so
    90 days of matured decisions show however long labels take."""
    async with request.app.state.pool.acquire() as conn:
        row = await question_row(conn, admin.project_id, question)
        horizon = int(json.loads(row["settings"]).get("label_horizon_days") or 0)
        paths = await conn.fetch(
            """
            SELECT d.model_served,
                   CASE WHEN d.action_taken LIKE 'SELECT:%' THEN 'SELECT'
                        ELSE coalesce(d.action_taken, 'none') END AS action,
                   count(*) AS decisions,
                   count(*) FILTER (WHERE d.ts <= now() - make_interval(days => $3)) AS matured,
                   count(o.decision_id) AS labeled,
                   count(o.decision_id) FILTER (WHERE d.audit) AS audited_labeled,
                   percentile_cont(0.5) WITHIN GROUP
                       (ORDER BY extract(epoch FROM o.observed_at - d.ts))
                       FILTER (WHERE o.decision_id IS NOT NULL) / 3600 AS median_delay_hours
            FROM decisions d
            LEFT JOIN outcomes o ON o.project_id = d.project_id AND o.decision_id = d.decision_id
            WHERE d.project_id = $1 AND d.question_id = $2 AND d.role = 'primary'
              AND d.ts >= now() - make_interval(days => $4)
              AND ($5::text IS NULL OR d.model_served = $5)
            GROUP BY 1, 2 ORDER BY 1, 2
            """,
            admin.project_id,
            row["question_id"],
            horizon,
            COVERAGE_DAYS + horizon,
            model_served,
        )
    return {
        "question_id": row["question_id"],
        "paths": [
            {
                "model_served": p["model_served"],
                "action": p["action"],
                "decisions": p["decisions"],
                "matured": p["matured"],
                "labeled": p["labeled"],
                "audited_labeled": p["audited_labeled"],
                "median_delay_hours": p["median_delay_hours"],
            }
            for p in paths
        ],
    }
