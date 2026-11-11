"""The proxy (FR-04 to FR-08, ADR 0011, docs/03-api/wire-formats.md).

Apps change one base URL. The upstream's response comes back unchanged, plus one header,
X-Trimset-Decision-Id. Records are queued for the recorder and the response never waits
for the database (NFR-06). Errors trimset itself returns use the upstream's own format.
"""

import email.parser
import email.policy
import json
import logging
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import httpx2
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from trimset.hashing import project_state_key, state_hash
from trimset.wire import WireError, parse_exchange, request_cost, to_records, uuid7
from trimset_server.auth import Unavailable
from trimset_server.bodies import TooLarge, read_body
from trimset_server.ingest import has_nul
from trimset_server.keys import Principal, looks_like_key
from trimset_server.projects import ProjectSettings, choose_upstream
from trimset_server.recorder import Pending
from trimset_server.records import RecordIn
from trimset_server.upstreams import Format, Upstream

log = logging.getLogger("trimset.proxy")
router = APIRouter()

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_NOT_FORWARDED = _HOP_BY_HOP | {"host", "content-length", "accept-encoding"}
_NOT_RETURNED = _HOP_BY_HOP | {"content-length", "content-encoding"}
MAX_RESPONSE = 16 * 1024 * 1024


class _ResponseTooLarge(Exception):
    pass


def _fail(fmt: Format, status: int, message: str, code: str) -> JSONResponse:
    """An error from trimset itself, in the upstream's format (wire-formats.md §6)."""
    if fmt == "openai-decisions":
        body: dict[str, Any] = {
            "error": {"message": message, "type": "trimset_error", "code": code, "param": None}
        }
    else:
        body = {"detail": message}
    return JSONResponse(body, status_code=status)


def _forward_headers(
    request: Request, settings: ProjectSettings, upstream: Upstream
) -> list[tuple[str, str]]:
    server_key = settings.upstream is not None and settings.upstream.credentials == "server"
    out = []
    for name, value in request.headers.items():
        lower = name.lower()
        if lower in _NOT_FORWARDED or lower.startswith("x-trimset-"):
            continue
        if lower == "authorization":
            if server_key:
                continue
            token = value.partition(" ")[2].strip() or value.strip()
            if looks_like_key(token):
                continue  # a trimset key never goes to a vendor
        out.append((name, value))
    if server_key and upstream.key:
        out.append(("authorization", f"Bearer {upstream.key}"))
    return out


async def _read_capped(response: httpx2.Response, limit: int) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > limit:
            raise _ResponseTooLarge
    return bytes(body)


def _request_json(content_type: str, body: bytes) -> Any:
    """The JSON request. For a multipart image request, its `request` part; the image
    parts are forwarded but never parsed, hashed or stored."""
    if content_type.lower().startswith("multipart/form-data"):
        parser = email.parser.BytesParser(policy=email.policy.HTTP)
        message = parser.parsebytes(
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body
        )
        for part in message.iter_parts():
            if part.get_param("name", header="content-disposition") == "request":
                payload = part.get_payload(decode=True)
                if not isinstance(payload, bytes):
                    raise ValueError("unreadable request part")
                return json.loads(payload)
        raise ValueError("no request part")
    return json.loads(body)


def metadata(ref_header: str | None, tags_header: str | None) -> tuple[str | None, dict[str, str]]:
    """X-Trimset-External-Ref and X-Trimset-Tags ("k=v,k2=v2", URL-encoded)."""
    ref = ref_header or None
    tags: dict[str, str] = {}
    for pair in (tags_header or "").split(","):
        key, sep, value = pair.partition("=")
        if sep and key.strip():
            tags[unquote(key.strip())] = unquote(value.strip())
    return ref, tags


def _state_key(state: Any, project_id: uuid.UUID) -> bytes | None:
    secret = state.settings.secret
    if secret is None:
        return None
    cache: dict[uuid.UUID, bytes] = state.state_keys
    if project_id not in cache:
        cache[project_id] = project_state_key(secret, str(project_id))
    return cache[project_id]


def _record(
    request: Request,
    principal: Principal,
    settings: ProjectSettings,
    upstream: Upstream,
    body: bytes,
    content: bytes,
    latency_ms: float,
) -> str | None:
    """Queue the exchange's records. Returns the X-Trimset-Decision-Id value, or None when
    the exchange couldn't be read (it is forwarded anyway, never logged)."""
    try:
        sent = _request_json(request.headers.get("content-type", ""), body)
    except ValueError as e:
        log.info("exchange not logged (%s)", type(e).__name__)
        return None
    ref, tags = metadata(
        request.headers.get("x-trimset-external-ref"), request.headers.get("x-trimset-tags")
    )
    return capture(
        request.app.state,
        principal.project_id,
        settings,
        upstream.format,
        sent,
        content,
        latency_ms,
        ref=ref,
        tags=tags,
    )


def capture(
    state: Any,
    project_id: uuid.UUID,
    settings: ProjectSettings,
    fmt: Format,
    sent: Any,
    content: bytes,
    latency_ms: float,
    *,
    ref: str | None,
    tags: dict[str, str],
    given_ids: Sequence[str] | None = None,
    ts: datetime | None = None,
) -> str | None:
    """Parse one exchange and queue its records, for this proxy and for the Rust one
    (internal.py). The Rust proxy chose the ids already, one per question by position;
    without them, ids are made here. Returns X-Trimset-Decision-Id, or None when the
    exchange couldn't be read."""
    try:
        exchange = parse_exchange(sent, json.loads(content), fmt)
    except (ValueError, WireError) as e:
        log.info("exchange not logged (%s)", type(e).__name__)
        return None
    if exchange.skipped:
        log.info("%d questions not logged", len(exchange.skipped))
    if given_ids is not None and any(d.position >= len(given_ids) for d in exchange.decisions):
        log.warning("exchange not logged: %d ids for its questions", len(given_ids))
        return None

    key = _state_key(state, project_id)
    try:
        hashed = state_hash(key, exchange.state) if key else None
    except ValueError:
        hashed = None  # a state JSON can't carry exactly (huge integers): no hash
    if dropped := settings.disallowed_tags(tags):
        tags = {k: v for k, v in tags.items() if k not in dropped}
        log.info("dropped %d tags not on the project's allow-list", len(dropped))
    ids = (
        [uuid7() for _ in exchange.decisions]
        if given_ids is None
        else [given_ids[d.position] for d in exchange.decisions]
    )
    records = to_records(
        exchange,
        ts=ts or datetime.now(UTC),
        request_id=uuid7(),
        decision_ids=ids,
        state_hash=hashed,
        latency_ms=latency_ms,
        cost_usd=request_cost(exchange.usage, settings.price_for(exchange.model_served)),
        tags=tags,
        external_ref=ref,
    )

    queued: list[Pending] = []
    slots = [""] * len(sent.get("questions") or ())
    for decision, raw in zip(exchange.decisions, records, strict=True):
        if has_nul(raw) or has_nul(decision.definition):
            continue
        try:
            record = RecordIn.model_validate(raw)
        except ValidationError:
            # Bad metadata from headers never costs the decision itself.
            try:
                record = RecordIn.model_validate(raw | {"tags": {}, "external_ref": None})
                log.info("ignored invalid X-Trimset-Tags or X-Trimset-External-Ref")
            except ValidationError:
                continue
        queued.append(Pending(project_id, record, decision.definition, decision.name))
        slots[decision.position] = str(record.decision_id)
    state.recorder.add(queued)
    return ",".join(slots)


async def _proxy(request: Request, key: str | None, path: str, guess: Format) -> Response:
    state = request.app.state
    if not key:
        return _fail(
            guess,
            401,
            "A trimset key is required: /p/<key> in the base URL, or X-Trimset-Key",
            "missing_key",
        )
    try:
        principal = await state.keys.principal(key, allow_stale=True)
        settings = await state.projects.settings(principal.project_id) if principal else None
    except Unavailable:
        return _fail(guess, 503, "trimset can't check this key right now", "unavailable")
    if principal is None or settings is None:
        return _fail(guess, 401, "Unknown or revoked trimset key", "invalid_key")
    if not principal.can("proxy"):
        return _fail(guess, 403, "This trimset key can't use the proxy", "forbidden")
    upstream = choose_upstream(settings, state.settings.upstreams, principal.project_id)
    if upstream is None:
        return _fail(guess, 503, "No upstream is set for this project", "no_upstream")
    fmt = upstream.format

    try:
        body = await read_body(request, settings.proxy_max_body_mb * 1024 * 1024)
    except TooLarge as e:
        return _fail(fmt, 413, str(e), "too_large")
    url = upstream.url + path + (f"?{request.url.query}" if request.url.query else "")
    timeout = (settings.upstream.timeout_ms if settings.upstream else 10_000) / 1000
    outgoing = state.http.build_request(
        request.method,
        url,
        content=body or None,
        headers=_forward_headers(request, settings, upstream),
        timeout=timeout,
    )
    started = time.perf_counter()
    try:
        upstream_response = await state.http.send(outgoing, stream=True)
        try:
            content = await _read_capped(upstream_response, MAX_RESPONSE)
        finally:
            await upstream_response.aclose()
    except httpx2.TimeoutException:
        return _fail(fmt, 504, "The upstream timed out", "upstream_timeout")
    except _ResponseTooLarge:
        return _fail(fmt, 502, "The upstream's response is too large", "upstream_too_large")
    except httpx2.RequestError as e:
        log.warning("upstream %s unreachable (%s)", upstream.name, type(e).__name__)
        return _fail(fmt, 502, "The upstream could not be reached", "upstream_unreachable")
    latency_ms = (time.perf_counter() - started) * 1000

    response = Response(content=content, status_code=upstream_response.status_code)
    for name, value in upstream_response.headers.multi_items():
        lower = name.lower()
        if lower not in _NOT_RETURNED and not lower.startswith("x-trimset-"):
            response.headers.append(name, value)
    if request.method == "POST" and 200 <= upstream_response.status_code < 300:
        ids = _record(request, principal, settings, upstream, body, content, latency_ms)
        if ids is not None:
            response.headers["X-Trimset-Decision-Id"] = ids
    return response


# Header mode: the key travels in X-Trimset-Key, leaving Authorization for the upstream.
@router.post("/v1/systemone")
async def systemone(request: Request) -> Response:
    return await _proxy(request, request.headers.get("x-trimset-key"), "/v1/systemone", "systemone")


@router.post("/v1/decisions")
async def decisions(request: Request) -> Response:
    key = request.headers.get("x-trimset-key")
    return await _proxy(request, key, "/v1/decisions", "openai-decisions")


@router.get("/v1/models")
async def models(request: Request) -> Response:
    return await _proxy(request, request.headers.get("x-trimset-key"), "/v1/models", "systemone")


# Path mode: the key sits in the base URL; trimset's logs redact it (logs.redact).
@router.post("/p/{proxy_key}/v1/systemone")
async def systemone_path(request: Request, proxy_key: str) -> Response:
    return await _proxy(request, proxy_key, "/v1/systemone", "systemone")


@router.post("/p/{proxy_key}/v1/decisions")
async def decisions_path(request: Request, proxy_key: str) -> Response:
    return await _proxy(request, proxy_key, "/v1/decisions", "openai-decisions")


@router.get("/p/{proxy_key}/v1/models")
async def models_path(request: Request, proxy_key: str) -> Response:
    return await _proxy(request, proxy_key, "/v1/models", "systemone")
