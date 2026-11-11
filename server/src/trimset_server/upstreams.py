"""Upstreams, defined only by the operator in the server environment (ADR 0011).

    TRIMSET_UPSTREAM_<NAME>_URL        e.g. https://api.typesafe.ai or http://ollaya:11435
    TRIMSET_UPSTREAM_<NAME>_FORMAT     systemone (default) | openai-decisions
    TRIMSET_UPSTREAM_<NAME>_KEY        optional; sent as "Authorization: Bearer ..." to URL only
    TRIMSET_UPSTREAM_<NAME>_PROJECTS   optional comma list of project ids; default every project

Projects pick an upstream by name. The API never carries a URL, a header or a secret, so
an admin key can neither point the server somewhere nor make it send a secret.
"""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, cast
from urllib.parse import urlsplit

Format = Literal["systemone", "openai-decisions"]
_VAR = re.compile(r"^TRIMSET_UPSTREAM_([A-Z0-9_]+?)_(URL|FORMAT|KEY|PROJECTS)$")


@dataclass(frozen=True)
class Upstream:
    name: str
    url: str  # no trailing slash
    format: Format
    key: str | None = field(default=None, repr=False)
    projects: frozenset[str] | None = None  # None: every project

    def allows(self, project_id: uuid.UUID) -> bool:
        return self.projects is None or str(project_id) in self.projects


def upstreams_from_env(env: Mapping[str, str]) -> dict[str, Upstream]:
    parts: dict[str, dict[str, str]] = {}
    for var, value in env.items():
        if m := _VAR.match(var):
            parts.setdefault(m[1].lower(), {})[m[2]] = value

    found = {}
    for name, p in sorted(parts.items()):
        where = f"TRIMSET_UPSTREAM_{name.upper()}"
        url = p.get("URL", "").strip()
        if not url:
            raise ValueError(f"{where}_URL is not set")
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"{where}_URL must be an http or https URL")
        if u.query or u.fragment or u.username or u.password:
            raise ValueError(f"{where}_URL must not carry a query, fragment or credentials")
        fmt = p.get("FORMAT", "systemone").strip()
        if fmt not in ("systemone", "openai-decisions"):
            raise ValueError(f"{where}_FORMAT must be systemone or openai-decisions")
        key = p.get("KEY", "").strip() or None
        if key and u.scheme != "https":
            raise ValueError(f"{where}_KEY is set, so {where}_URL must use https")
        listed = [s.strip().lower() for s in p.get("PROJECTS", "").split(",") if s.strip()]
        found[name] = Upstream(
            name, url.rstrip("/"), cast(Format, fmt), key, frozenset(listed) if listed else None
        )
    return found
