import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx2
import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ADMIN_URL = os.environ.get("TRIMSET_TEST_DATABASE_URL", "")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def dsn() -> AsyncIterator[str]:
    """A fresh, empty database for one test. CI provides Postgres; locally this skips."""
    if not ADMIN_URL:
        pytest.skip("needs TRIMSET_TEST_DATABASE_URL (CI has Postgres)")
    name = "trimset_test_" + uuid.uuid4().hex[:12]
    admin = await asyncpg.connect(ADMIN_URL)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        yield urlunsplit(urlsplit(ADMIN_URL)._replace(path="/" + name))
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


# ------------------------------------------------------------------ the API contract

API = Path(__file__).resolve().parents[2] / "docs" / "03-api"
SPEC = yaml.safe_load((API / "openapi.yaml").read_text())
BASE = "https://contract.trimset.test/"
REGISTRY: Registry = Registry().with_resources(
    [(BASE + "openapi.yaml", Resource.from_contents(SPEC, default_specification=DRAFT202012))]
    + [
        (
            BASE + "schemas/" + f.name,
            Resource.from_contents(json.loads(f.read_text()), default_specification=DRAFT202012),
        )
        for f in (API / "schemas").glob("*.json")
    ]
)
PARAM = re.compile(r"\{[^}]+\}")


def _escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def _check(response: httpx2.Response, method: str, template: str) -> None:
    """The response against what the contract says `method template` returns."""
    op = SPEC["paths"][template][method.lower()]
    status = str(response.status_code)
    listed = op["responses"]
    key = next((k for k in (status, status[0] + "XX", "default") if k in listed), None)
    where = f"{method} {template} answered {status}"
    if key is None:
        assert response.status_code >= 400, f"{where}, which the contract doesn't list"
        assert "proxy" not in op["tags"], f"{where}, which the contract doesn't list"
        pointer = "/components/schemas/Problem"
        media = "application/problem+json"
    else:
        spec = listed[key]
        path = ["paths", template, method.lower(), "responses", key]
        if "$ref" in spec:
            name = spec["$ref"].rsplit("/", 1)[1]
            spec, path = SPEC["components"]["responses"][name], ["components", "responses", name]
        if not spec.get("content"):
            return  # nothing promised about the body
        media = response.headers["content-type"].split(";")[0]
        assert media in spec["content"], f"{where} as {media}"
        pointer = "/" + "/".join(_escape(p) for p in [*path, "content", media, "schema"])
    assert response.headers["content-type"].split(";")[0] == media, where
    validator = Draft202012Validator({"$ref": f"{BASE}openapi.yaml#{pointer}"}, registry=REGISTRY)
    errors = [
        f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
        for e in validator.iter_errors(response.json())
    ]
    assert not errors, f"{where}: {errors}"


@pytest.fixture
def check() -> Any:
    """Checks a response against docs/03-api/openapi.yaml: a status the operation lists,
    a body its schema accepts, and a Problem for any error it doesn't list (T1.12)."""
    return _check


@pytest.fixture
def conforms() -> Any:
    """Checks a body against one of docs/03-api/schemas, e.g. conforms(body, "policy")."""

    def run(instance: Any, schema: str) -> None:
        ref = {"$ref": f"{BASE}schemas/{schema}.schema.json"}
        errors = [
            f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
            for e in Draft202012Validator(ref, registry=REGISTRY).iter_errors(instance)
        ]
        assert not errors, f"{schema}: {errors}"

    return run
