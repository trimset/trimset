"""The docs build (ADR 0016): the contract bundles into one document, and Redoc is only
used when it matches its pinned checksum."""

import base64
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

SCRIPT = Path(__file__).parents[1] / "build_docs.py"
spec = importlib.util.spec_from_file_location("build_docs", SCRIPT)
assert spec and spec.loader
bd = importlib.util.module_from_spec(spec)
sys.modules["build_docs"] = bd
spec.loader.exec_module(bd)

CONTRACT = Path(__file__).parents[2] / "docs" / "03-api" / "openapi.yaml"


def _operations(spec_: dict[str, Any]) -> dict[tuple[str, str], str | None]:
    """Every (path, method) with its operationId (the health checks have none)."""
    return {
        (path, method): op.get("operationId")
        for path, item in spec_["paths"].items()
        for method, op in item.items()
        if method in ("get", "post", "put", "patch", "delete")
    }


def test_the_real_contract_bundles_into_one_self_contained_document() -> None:
    original = yaml.safe_load(CONTRACT.read_text())
    bundle = bd.bundle_openapi(CONTRACT)
    assert bd.broken_refs(bundle) == []  # no reference to a file, none that leads nowhere
    assert '"$ref": "./' not in json.dumps(bundle) and ".schema.json" not in json.dumps(bundle)
    assert _operations(bundle) == _operations(original) and len(_operations(bundle)) > 40
    assert set(bundle["paths"]) == set(original["paths"])
    assert set(bundle["components"]["schemas"]) == set(original["components"]["schemas"])
    assert not any(k in json.dumps(bundle) for k in ('"$schema"', '"$id"'))  # root keys dropped
    json.dumps(bundle)  # serializable: no YAML-only values left


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if path.suffix == ".json" else yaml.safe_dump(value))


def test_references_resolve_into_files_pointers_and_each_other(tmp_path: Path) -> None:
    _write(tmp_path / "schemas" / "common.json", {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"uuid": {"type": "string", "format": "uuid"},
                  "pair": {"type": "array", "items": {"$ref": "#/$defs/uuid"}}},
    })  # fmt: skip
    _write(tmp_path / "schemas" / "thing.json", {
        "$schema": "x", "$id": "y", "title": "Thing", "type": "object",
        "properties": {"id": {"$ref": "common.json#/$defs/uuid"},
                       "ids": {"$ref": "common.json#/$defs/pair"}},
    })  # fmt: skip
    _write(tmp_path / "openapi.yaml", {
        "openapi": "3.1.0",
        "paths": {},
        "components": {"schemas": {
            "Thing": {"$ref": "./schemas/thing.json"},
            "Note": {"$ref": "./schemas/common.json#/$defs/uuid", "description": "mine"},
            "Local": {"$ref": "#/components/schemas/Thing"},
        }},
    })  # fmt: skip
    bundle = bd.bundle_openapi(tmp_path / "openapi.yaml")
    schemas = bundle["components"]["schemas"]
    assert schemas["Thing"] == {
        "title": "Thing", "type": "object",
        "properties": {"id": {"type": "string", "format": "uuid"},
                       "ids": {"type": "array", "items": {"type": "string", "format": "uuid"}}},
    }  # fmt: skip
    assert schemas["Note"] == {"type": "string", "format": "uuid", "description": "mine"}
    assert schemas["Local"] == {"$ref": "#/components/schemas/Thing"}  # kept: inside the bundle


@pytest.mark.parametrize(
    "ref,message",
    [
        ("./schemas/loop.json", "refers to itself"),
        ("./schemas/missing.json", "can't read"),
        ("./schemas/loop.json#/nope", "nothing at"),
        ("#/components/schemas/Nope", "lead nowhere"),
    ],
)
def test_a_reference_that_cannot_be_resolved_is_an_error(
    tmp_path: Path, ref: str, message: str
) -> None:
    _write(tmp_path / "schemas" / "loop.json", {"a": {"$ref": "#"}})
    _write(
        tmp_path / "openapi.yaml",
        {"openapi": "3.1.0", "components": {"schemas": {"X": {"$ref": ref}}}},
    )
    with pytest.raises(bd.DocsError, match=message):
        bd.bundle_openapi(tmp_path / "openapi.yaml")


def _integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def test_only_the_pinned_download_is_accepted() -> None:
    bd.verify_integrity(b"redoc", _integrity(b"redoc"))
    with pytest.raises(bd.DocsError, match="pinned checksum"):
        bd.verify_integrity(b"tampered", _integrity(b"redoc"))
    with pytest.raises(bd.DocsError, match="unsupported"):
        bd.verify_integrity(b"x", "md5-abc")


def test_redoc_is_read_from_a_verified_tarball_and_a_bad_cache_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, body in (
            ("package/bundles/redoc.standalone.js", b"js"),
            ("package/LICENSE", b"MIT"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    good = buffer.getvalue()
    monkeypatch.setattr(bd, "REDOC_INTEGRITY", _integrity(good))

    class Response(io.BytesIO):
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(bd.urllib.request, "urlopen", lambda *a, **k: Response(good))
    assert bd.fetch_redoc(tmp_path) == (b"js", b"MIT")
    assert bd.fetch_redoc(tmp_path) == (b"js", b"MIT")  # now from the cache

    (tmp_path / f"redoc-{bd.REDOC}.tgz").write_bytes(b"corrupt")  # a damaged cache entry
    with pytest.raises(bd.DocsError):
        bd.fetch_redoc(tmp_path)
    assert not (tmp_path / f"redoc-{bd.REDOC}.tgz").exists()  # and it does not stay


def test_the_viewer_page_loads_redoc_and_the_bundle_from_beside_itself() -> None:
    assert 'src="redoc.standalone.js"' in bd.VIEWER and 'Redoc.init("openapi.json"' in bd.VIEWER
    assert "http" not in bd.VIEWER.replace("<!doctype html>", "")  # nothing loaded from elsewhere
