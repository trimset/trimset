"""Projects and API keys (FR-51, FR-52, FR-53).

A key is a scope prefix plus 32 base62 characters (docs/03-api/README.md). Only its
SHA-256 is stored.
"""

import hashlib
import re
import secrets
import string
import uuid
from dataclasses import dataclass
from typing import Any, Literal

Scope = Literal["proxy", "ingest", "review", "admin"]
PREFIXES: dict[Scope, str] = {"proxy": "tsp_", "ingest": "tsi_", "review": "tsr_", "admin": "tsa_"}

# What each scope may do. Routes ask for one of these capabilities.
CAPABILITIES: dict[Scope, frozenset[str]] = {
    "proxy": frozenset({"proxy"}),
    "ingest": frozenset({"proxy", "ingest"}),
    "review": frozenset({"review"}),
    "admin": frozenset({"proxy", "ingest", "review", "admin"}),
}

_ALPHABET = string.ascii_letters + string.digits
_KEY = re.compile(r"ts[pira]_[A-Za-z0-9]{32}")


@dataclass(frozen=True)
class Principal:
    """Who is calling: the key's project and scope."""

    key_id: uuid.UUID
    project_id: uuid.UUID
    scope: Scope

    def can(self, capability: str) -> bool:
        return capability in CAPABILITIES[self.scope]


def new_key(scope: Scope) -> str:
    return PREFIXES[scope] + "".join(secrets.choice(_ALPHABET) for _ in range(32))


def looks_like_key(text: str) -> bool:
    return _KEY.fullmatch(text) is not None


def hash_key(key: str) -> bytes:
    return hashlib.sha256(key.encode("ascii")).digest()


async def create_project(conn: Any, name: str) -> uuid.UUID:
    project_id = uuid.uuid4()
    await conn.execute("INSERT INTO projects (project_id, name) VALUES ($1, $2)", project_id, name)
    return project_id


async def project_id_by_name(conn: Any, name: str) -> uuid.UUID | None:
    pid: uuid.UUID | None = await conn.fetchval(
        "SELECT project_id FROM projects WHERE name = $1", name
    )
    return pid


async def create_key(
    conn: Any, project_id: uuid.UUID, scope: Scope, label: str
) -> tuple[dict[str, Any], str]:
    """Returns (the stored row, the key itself). The key is shown once, never stored."""
    key = new_key(scope)
    row = await conn.fetchrow(
        "INSERT INTO api_keys (key_id, project_id, scope, label, prefix, key_hash) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "RETURNING key_id, scope, label, prefix, created_at, last_used_at, revoked_at",
        uuid.uuid4(),
        project_id,
        scope,
        label,
        key[:8],
        hash_key(key),
    )
    return dict(row), key


async def list_keys(conn: Any, project_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT key_id, scope, label, prefix, created_at, last_used_at, revoked_at "
        "FROM api_keys WHERE project_id = $1 ORDER BY created_at",
        project_id,
    )
    return [dict(r) for r in rows]


async def revoke_key(conn: Any, key_id: uuid.UUID, project_id: uuid.UUID | None) -> bool:
    """Revoke a key. With a project_id, only that project's keys. False if not found."""
    found = await conn.fetchval(
        "UPDATE api_keys SET revoked_at = coalesce(revoked_at, now()) "
        "WHERE key_id = $1 AND ($2::uuid IS NULL OR project_id = $2) RETURNING key_id",
        key_id,
        project_id,
    )
    return found is not None


async def principal_for(conn: Any, key_hash: bytes) -> Principal | None:
    """The live key with this hash, or None. Marks it used, at most once a minute."""
    row = await conn.fetchrow(
        "WITH k AS ("
        "  SELECT key_id, project_id, scope FROM api_keys"
        "  WHERE key_hash = $1 AND revoked_at IS NULL),"
        " touched AS ("
        "  UPDATE api_keys SET last_used_at = now() FROM k"
        "  WHERE api_keys.key_id = k.key_id"
        "  AND (api_keys.last_used_at IS NULL"
        "       OR api_keys.last_used_at < now() - interval '1 minute'))"
        " SELECT key_id, project_id, scope FROM k",
        key_hash,
    )
    if row is None:
        return None
    return Principal(key_id=row["key_id"], project_id=row["project_id"], scope=row["scope"])
