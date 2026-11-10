"""The FastAPI application. Endpoints arrive task by task (docs/04-plan.md)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from trimset_server.config import Settings
from trimset_server.telemetry import setup_telemetry

log = logging.getLogger("trimset.server")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # min_size=0 opens no connection here, so the server starts while Postgres is
        # still down and /ready reports it.
        app.state.pool = await asyncpg.create_pool(
            settings.database_url, min_size=0, max_size=settings.db_pool_max, timeout=5
        )
        try:
            yield
        finally:
            await app.state.pool.close()

    # The contract is docs/03-api/openapi.yaml, so FastAPI's generated docs stay off.
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

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
