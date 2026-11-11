"""The admin API (docs/03-api/openapi.yaml, tag "admin"). Every route needs an admin key and
only sees that key's project (FR-51)."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from trimset_server import audit, keys
from trimset_server.auth import require
from trimset_server.errors import Problem
from trimset_server.keys import Principal, Scope

router = APIRouter(prefix="/v1/admin")
Admin = Annotated[Principal, Depends(require("admin"))]


class Key(BaseModel):
    key_id: uuid.UUID
    scope: Scope
    label: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class KeyCreated(Key):
    secret: str


class KeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Scope
    label: str = Field(min_length=1, max_length=100)


class KeyList(BaseModel):
    items: list[Key]


@router.get("/keys")
async def list_keys(request: Request, admin: Admin) -> KeyList:
    async with request.app.state.pool.acquire() as conn:
        rows: list[dict[str, Any]] = await keys.list_keys(conn, admin.project_id)
    return KeyList(items=[Key(**r) for r in rows])


@router.post("/keys", status_code=201)
async def create_key(body: KeyRequest, request: Request, admin: Admin) -> KeyCreated:
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        row, secret = await keys.create_key(conn, admin.project_id, body.scope, body.label)
        await audit.key_created(conn, admin.project_id, admin.key_id, row)
    return KeyCreated(**row, secret=secret)


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(key_id: uuid.UUID, request: Request, admin: Admin) -> Response:
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        found = await keys.revoke_key(conn, key_id, admin.project_id)
        await audit.key_revoked(conn, key_id, admin.key_id, found)
    if found is None:
        raise Problem(404, "not-found", "No such key in this project")
    request.app.state.keys.forget(key_id)
    return Response(status_code=204)
