"""Background jobs (ADR 0004): rows in `jobs`, claimed by the worker with
FOR UPDATE SKIP LOCKED, so any number of workers is safe.

A job that raises JobError fails at once with that message. Any other error is retried
three times with backoff (1, 2, 4 minutes). A job left running by a worker that died is
picked up again after 15 minutes.
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from trimset_server.auth import require
from trimset_server.errors import Problem
from trimset_server.fitting import JobError, run_compile, run_fit
from trimset_server.keys import Principal
from trimset_server.refits import run_refit

log = logging.getLogger("trimset.jobs")
router = APIRouter(prefix="/v1/admin")
Admin = Annotated[Principal, Depends(require("admin"))]
MAX_ATTEMPTS = 3
Handler = Callable[[Any, uuid.UUID, Mapping[str, Any], uuid.UUID | None], Awaitable[dict[str, Any]]]
HANDLERS: dict[str, Handler] = {"fit": run_fit, "compile": run_compile, "refit": run_refit}

_CLAIM = """
UPDATE jobs SET status = 'running', started_at = now(), attempts = attempts + 1
WHERE job_id = (
    SELECT job_id FROM jobs
    WHERE (status = 'queued' AND run_at <= now())
       OR (status = 'running' AND started_at < now() - interval '15 minutes')
    ORDER BY job_id LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING job_id, project_id, kind, payload, attempts, key_id
"""


def view(row: Any) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "kind": row["kind"],
        "status": row["status"],
        "result": json.loads(row["result"]) if row["result"] is not None else None,
        "error": row["error"],
        "created_at": row["created_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


async def enqueue(
    conn: Any, principal: Principal, kind: str, payload: Mapping[str, Any]
) -> JSONResponse:
    """Queue a job and answer 202 with it (the JobAccepted response)."""
    row = await conn.fetchrow(
        "INSERT INTO jobs (project_id, kind, payload, key_id) VALUES ($1, $2, $3::jsonb, $4) "
        "RETURNING job_id, kind, status, result, error, created_at, finished_at",
        principal.project_id,
        kind,
        json.dumps(payload),
        principal.key_id,
    )
    return JSONResponse(
        view(row), status_code=202, headers={"Location": f"/v1/admin/jobs/{row['job_id']}"}
    )


async def run_one(conn: Any) -> bool:
    """Claim and run one due job. Returns False when there was none."""
    job = await conn.fetchrow(_CLAIM)
    if job is None:
        return False
    try:
        handler = HANDLERS[job["kind"]]
        result = await handler(conn, job["project_id"], json.loads(job["payload"]), job["key_id"])
    except JobError as e:
        await _finish(conn, job["job_id"], "failed", None, str(e))
    except Exception as e:
        if job["attempts"] >= MAX_ATTEMPTS:
            log.exception("job %s failed for good", job["job_id"])
            await _finish(
                conn,
                job["job_id"],
                "failed",
                None,
                f"failed {MAX_ATTEMPTS} times ({type(e).__name__})",
            )
        else:
            log.warning("job %s failed (%s), will retry", job["job_id"], type(e).__name__)
            await conn.execute(
                "UPDATE jobs SET status = 'queued', error = $2, "
                "run_at = now() + make_interval(mins => $3) WHERE job_id = $1",
                job["job_id"],
                type(e).__name__,
                2 ** (job["attempts"] - 1),
            )
    else:
        await _finish(conn, job["job_id"], "succeeded", result, None)
    return True


async def _finish(conn: Any, job_id: int, status: str, result: Any, error: str | None) -> None:
    await conn.execute(
        "UPDATE jobs SET status = $2, result = $3::jsonb, error = $4, finished_at = now() "
        "WHERE job_id = $1",
        job_id,
        status,
        json.dumps(result) if result is not None else None,
        error,
    )


async def work(conn: Any, limit: int = 10) -> int:
    """Runs due jobs until none is left or `limit` ran. The worker calls it every second."""
    ran = 0
    while ran < limit and await run_one(conn):
        ran += 1
    return ran


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, request: Request, admin: Admin) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT job_id, kind, status, result, error, created_at, finished_at FROM jobs "
            "WHERE job_id = $1 AND project_id = $2",
            job_id,
            admin.project_id,
        )
    if row is None:
        raise Problem(404, "not-found", "No such job in this project")
    return view(row)
