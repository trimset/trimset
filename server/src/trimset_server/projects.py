"""Project settings: the model, a cache that survives database outages, and the admin
endpoints GET/PATCH /v1/admin/project (docs/03-api/openapi.yaml, ProjectSettings)."""

import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trimset_server import audit
from trimset_server.auth import DB_ERRORS, MAX_STALE_S, Unavailable, require
from trimset_server.bodies import TooLarge, parse_json, read_body
from trimset_server.errors import Problem
from trimset_server.keys import Principal
from trimset_server.upstreams import Upstream

router = APIRouter(prefix="/v1/admin")
Admin = Annotated[Principal, Depends(require("admin"))]
OperatorName = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,32}$")]


class UpstreamChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: OperatorName
    timeout_ms: Annotated[int, Field(ge=100, le=120_000)] = 10_000
    credentials: Literal["passthrough", "server"] = "passthrough"


class Price(BaseModel):
    """USD per million tokens (FR-07). Prices are settings, never code."""

    model_config = ConfigDict(extra="forbid", strict=True)
    input: Annotated[float, Field(ge=0)]
    cached_input: Annotated[float, Field(ge=0)] | None = None
    output: Annotated[float, Field(ge=0)] = 0.0


class Privacy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # Storing raw state (FR-09) isn't built yet, so only `false` is accepted for now.
    store_raw_state: Literal[False] = False
    raw_state_retention_days: Annotated[int, Field(ge=1, le=400)] = 30
    tag_allowlist: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]] | None = None


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    upstream: UpstreamChoice | None = None
    prices: dict[str, Price] = {}
    proxy_max_body_mb: Annotated[int, Field(ge=1, le=32)] = 8
    retention_days: Annotated[int, Field(ge=1, le=3650)] = 400
    privacy: Privacy = Privacy()
    refit_schedule: Annotated[str, Field(min_length=9, max_length=100)] = "0 3 * * *"

    def disallowed_tags(self, tags: Mapping[str, str]) -> list[str]:
        """Tag keys the project's allow-list doesn't permit (FR-09); none without a list.
        The log API rejects such records; the proxy strips the tags. Both ask here."""
        allowed = self.privacy.tag_allowlist
        return [] if allowed is None else sorted(k for k in tags if k not in allowed)

    def price_for(self, model: str) -> dict[str, float] | None:
        price = self.prices.get(model)
        return price.model_dump(exclude_none=True) if price else None


Loader = Callable[[uuid.UUID], Awaitable[ProjectSettings | None]]


class ProjectCache:
    """Settings per project for `ttl_s` seconds, for the proxy. If the database fails, the
    last known settings stay in use for at most `max_stale_s`, so the proxy keeps
    forwarding (NFR-06)."""

    def __init__(
        self,
        loader: Loader,
        ttl_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        max_stale_s: float = MAX_STALE_S,
    ) -> None:
        self._load, self._ttl, self._clock = loader, ttl_s, clock
        self._max_stale = max_stale_s
        self._entries: dict[uuid.UUID, tuple[float, ProjectSettings]] = {}

    async def settings(self, project_id: uuid.UUID) -> ProjectSettings | None:
        now = self._clock()
        hit = self._entries.get(project_id)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        try:
            found = await self._load(project_id)
        except DB_ERRORS as e:
            if hit is not None and now - hit[0] < self._max_stale:
                return hit[1]
            raise Unavailable from e
        if found is None:
            self._entries.pop(project_id, None)
        else:
            self._entries[project_id] = (now, found)
        return found

    def forget(self, project_id: uuid.UUID) -> None:
        self._entries.pop(project_id, None)


async def load_settings(conn: Any, project_id: uuid.UUID) -> ProjectSettings | None:
    raw = await conn.fetchval("SELECT settings FROM projects WHERE project_id = $1", project_id)
    return None if raw is None else ProjectSettings.model_validate(json.loads(raw))


def available(upstreams: Mapping[str, Upstream], project_id: uuid.UUID) -> list[Upstream]:
    return [u for u in upstreams.values() if u.allows(project_id)]


def choose_upstream(
    settings: ProjectSettings, upstreams: Mapping[str, Upstream], project_id: uuid.UUID
) -> Upstream | None:
    """The project's upstream; with none chosen, the only one available, if there is one."""
    if settings.upstream is not None:
        u = upstreams.get(settings.upstream.name)
        return u if u is not None and u.allows(project_id) else None
    choices = available(upstreams, project_id)
    return choices[0] if len(choices) == 1 else None


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7396: objects merge, null deletes, anything else replaces."""
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def _view(
    project_id: uuid.UUID, name: str, settings: ProjectSettings, request: Request
) -> dict[str, Any]:
    upstreams = available(request.app.state.settings.upstreams, project_id)
    return {
        "project_id": str(project_id),
        "name": name,
        "settings": settings.model_dump(mode="json", exclude_none=True),  # unset is absent
        "available": {
            "upstreams": [
                {"name": u.name, "format": u.format, "server_key": u.key is not None}
                for u in upstreams
            ],
            "alert_channels": [],  # operator-defined channels arrive with alerts (M4)
        },
    }


@router.get("/project")
async def get_project(request: Request, admin: Admin) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, settings FROM projects WHERE project_id = $1", admin.project_id
        )
    settings = ProjectSettings.model_validate(json.loads(row["settings"]))
    return _view(admin.project_id, row["name"], settings, request)


@router.patch("/project")
async def update_project(request: Request, admin: Admin) -> dict[str, Any]:
    try:
        patch = parse_json(await read_body(request, 64 * 1024))
    except TooLarge as e:
        raise Problem(413, "too-large", str(e)) from None
    except ValueError:
        raise Problem(400, "invalid-request", "The body is not valid JSON") from None
    if not isinstance(patch, dict):
        raise Problem(400, "invalid-request", "Expected a JSON merge patch object")

    async with request.app.state.pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT name, settings FROM projects WHERE project_id = $1 FOR UPDATE",
            admin.project_id,
        )
        merged = merge_patch(json.loads(row["settings"]), patch)
        try:
            settings = ProjectSettings.model_validate(merged)
        except ValidationError as e:
            errors = [
                {"pointer": "/" + "/".join(str(p) for p in err["loc"]), "message": err["msg"]}
                for err in e.errors()
            ]
            raise Problem(
                422, "invalid-request", "The settings are not valid", errors=errors
            ) from None
        if settings.upstream is not None:
            upstream = request.app.state.settings.upstreams.get(settings.upstream.name)
            if upstream is None or not upstream.allows(admin.project_id):
                raise Problem(422, "invalid-request", "No such upstream for this project")
            if settings.upstream.credentials == "server" and upstream.key is None:
                raise Problem(422, "invalid-request", "The operator set no key for this upstream")
        stored = settings.model_dump(mode="json", exclude_defaults=True)
        before = json.loads(row["settings"])
        await conn.execute(
            "UPDATE projects SET settings = $2::jsonb WHERE project_id = $1",
            admin.project_id,
            json.dumps(stored),
        )
        if stored != before:
            await audit.record(
                conn, admin.project_id, admin.key_id, "project.settings", "project", before, stored
            )
    request.app.state.projects.forget(admin.project_id)
    return _view(admin.project_id, row["name"], settings, request)
