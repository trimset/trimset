"""What the Rust proxy asks of this server (ADR 0015). Never public: the proxy refuses
/internal/* from outside, and every call must carry a token derived from TRIMSET_SECRET.

- POST /internal/proxy/principal: who a key is, and where its project's traffic goes. The
  proxy caches the answer, so the database is read here directly, not through a cache.
- POST /internal/proxy/exchanges: forwarded exchanges, to parse and record. Wire formats
  and hashing stay in one place, the SDK; the proxy only carries bytes.
"""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from trimset_server import keys, projects
from trimset_server.auth import DB_ERRORS, Unavailable
from trimset_server.errors import Problem
from trimset_server.proxy import capture, metadata

TOKEN_CONTEXT = b"trimset-internal/1"


def internal_token(secret: bytes) -> str:
    """The token the proxy sends: HMAC-SHA-256 of a fixed context under TRIMSET_SECRET."""
    return hmac.new(secret, TOKEN_CONTEXT, hashlib.sha256).hexdigest()


def _internal(request: Request, x_trimset_internal: Annotated[str | None, Header()] = None) -> None:
    secret = request.app.state.settings.secret
    if (
        secret is None
        or x_trimset_internal is None
        or not hmac.compare_digest(x_trimset_internal, internal_token(secret))
    ):
        raise Problem(404, "not-found", "Not found")  # nothing to see without the token


router = APIRouter(prefix="/internal/proxy", dependencies=[Depends(_internal)])


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Annotated[str, Field(max_length=64)]


def _denied(status: int, code: str, message: str) -> dict[str, Any]:
    return {"denied": {"status": status, "code": code, "message": message}}


@router.post("/principal")
async def principal(body: KeyIn, request: Request) -> dict[str, Any]:
    state = request.app.state
    if not keys.looks_like_key(body.key):
        return _denied(401, "invalid_key", "Unknown or revoked trimset key")
    try:
        async with state.pool.acquire() as conn:
            who = await keys.principal_for(conn, keys.hash_key(body.key))
            settings = await projects.load_settings(conn, who.project_id) if who else None
    except DB_ERRORS:
        raise Problem(503, "unavailable", "The database can't be reached") from None
    if who is None or settings is None:
        return _denied(401, "invalid_key", "Unknown or revoked trimset key")
    if not who.can("proxy"):
        return _denied(403, "forbidden", "This trimset key can't use the proxy")
    upstream = projects.choose_upstream(settings, state.settings.upstreams, who.project_id)
    if upstream is None:
        return _denied(503, "no_upstream", "No upstream is set for this project")
    choice = settings.upstream
    return {
        "principal": {
            "key_id": str(who.key_id),
            "project_id": str(who.project_id),
            "upstream": {
                "name": upstream.name,
                "url": upstream.url,
                "format": upstream.format,
                "server_key": choice is not None and choice.credentials == "server",
                "timeout_ms": choice.timeout_ms if choice else 10_000,
            },
            "max_body_bytes": settings.proxy_max_body_mb * 1024 * 1024,
        }
    }


class ExchangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: uuid.UUID
    format: Literal["systemone", "openai-decisions"]
    request: str  # the request's JSON; for a multipart image request, its `request` part
    response: str
    decision_ids: list[str]  # one per question, in question order
    latency_ms: float
    ts_ms: int
    external_ref: str | None = None  # X-Trimset-External-Ref, as sent
    tags: str | None = None  # X-Trimset-Tags, as sent


class ExchangesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exchanges: Annotated[list[ExchangeIn], Field(max_length=1000)]


@router.post("/exchanges")
async def exchanges(body: ExchangesIn, request: Request) -> JSONResponse:
    state = request.app.state
    queued = 0
    for ex in body.exchanges:
        try:
            settings = await state.projects.settings(ex.project_id)
        except Unavailable:
            settings = None
        if settings is None:
            continue  # a project deleted meanwhile, or no settings at all during an outage
        try:
            sent = json.loads(ex.request)
        except ValueError:
            continue
        ref, tags = metadata(ex.external_ref, ex.tags)
        ids = capture(
            state,
            ex.project_id,
            settings,
            ex.format,
            sent,
            ex.response.encode(),
            ex.latency_ms,
            ref=ref,
            tags=tags,
            given_ids=ex.decision_ids,
            ts=datetime.fromtimestamp(ex.ts_ms / 1000, UTC),
        )
        queued += ids is not None
    return JSONResponse({"queued": queued}, status_code=202)
