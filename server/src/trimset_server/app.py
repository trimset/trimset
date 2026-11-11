"""The FastAPI application. Endpoints arrive task by task (docs/04-plan.md)."""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from trimset_server import (
    actions,
    admin,
    errors,
    ingest,
    internal,
    keys,
    outcomes,
    projects,
    questions,
)
from trimset_server.auth import KeyCache
from trimset_server.config import Settings
from trimset_server.recorder import Recorder
from trimset_server.telemetry import setup_telemetry

log = logging.getLogger("trimset.server")


def create_app(
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # min_size=0 opens no connection here, so the server starts while Postgres is
        # still down and /ready reports it.
        pool = await asyncpg.create_pool(
            settings.database_url, min_size=0, max_size=settings.db_pool_max, timeout=5
        )

        async def load_key(digest: bytes) -> keys.Principal | None:
            async with pool.acquire() as conn:
                return await keys.principal_for(conn, digest)

        async def load_project(project_id: uuid.UUID) -> projects.ProjectSettings | None:
            async with pool.acquire() as conn:
                return await projects.load_settings(conn, project_id)

        recorder = Recorder(pool.acquire, capacity=settings.proxy_buffer)
        stop = asyncio.Event()

        app.state.settings = settings
        app.state.pool = pool
        app.state.keys = KeyCache(load_key, settings.key_cache_ttl_s)
        app.state.projects = projects.ProjectCache(load_project, settings.key_cache_ttl_s)
        app.state.recorder = recorder
        app.state.state_keys = {}
        writer = asyncio.create_task(recorder.run(stop))
        try:
            yield
        finally:
            stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
            await recorder.drain(timeout_s=5)
            await pool.close()

    # The contract is docs/03-api/openapi.yaml, so FastAPI's generated docs stay off.
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    errors.install(app)
    app.include_router(admin.router)
    app.include_router(projects.router)
    app.include_router(ingest.router)
    app.include_router(outcomes.router)
    app.include_router(actions.router)
    app.include_router(questions.router)
    app.include_router(internal.router)

    # Public, no key: liveness and readiness never return project data.
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        try:
            async with asyncio.timeout(3), request.app.state.pool.acquire() as con:
                await con.fetchval("SELECT 1")
        except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            log.warning("not ready: database unreachable (%s)", type(e).__name__)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    setup_telemetry(app)
    return app
