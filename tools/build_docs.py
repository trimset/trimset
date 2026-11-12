"""Prepares and builds the docs site (ADR 0016, the T7.1 skeleton).

    uv run --frozen python tools/build_docs.py            # prepare, then build to _docs_build/
    uv run --frozen python tools/build_docs.py --prepare  # only the generated files, to preview

The pages are the Markdown in docs/. Two things in docs/api-reference/ are generated and
never committed, so the API reference cannot drift from the contract:

- openapi.json: docs/03-api/openapi.yaml with every reference to a file in schemas/ replaced
  by what it points at, so the viewer reads one self-contained document;
- redoc.standalone.js (and its licence): Redoc, fetched from the npm registry and checked
  against the registry's own checksum before it is used;
- viewer.html: the page that runs Redoc on openapi.json. The docs page frames it, so the
  site's styles and Redoc's never meet.

Zensical is run from an isolated environment, pinned, with every transitive dependency held
at a date at least 7 days old, so it enters neither the lockfile nor any image.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

ZENSICAL = "0.1.0"  # released 2026-11-05 15:32 UTC; 7 days old from 2026-11-12 15:32 UTC
ZENSICAL_CUTOFF = "2026-11-05T15:35:00Z"  # nothing newer resolves, Zensical's dependencies included
REDOC = "2.5.1"  # released 2025-09-26; 3.x is built for Node (import.meta), not a script tag
REDOC_URL = f"https://registry.npmjs.org/redoc/-/redoc-{REDOC}.tgz"
REDOC_INTEGRITY = (  # the registry's own checksum of the tarball
    "sha512-LmqA+4A3CmhTllGG197F0arUpmChukAj9klfSdxNRemT9Hr07xXr7OGKu4PHzBs359sgrJ+4JwmOlM7nxLPGMg=="
)
DROPPED = ("$schema", "$id")  # the root of an inlined schema file; meaningless inside OpenAPI


class DocsError(Exception):
    pass


# ------------------------------------------------------------------ the OpenAPI bundle


def _load(path: Path, cache: dict[Path, Any]) -> Any:
    if path not in cache:
        text = path.read_text()
        cache[path] = yaml.safe_load(text) if path.suffix in (".yaml", ".yml") else json.loads(text)
    return cache[path]


def _pointer(document: Any, pointer: str, where: str) -> Any:
    node = document
    for part in [p for p in pointer.split("/") if p]:
        key = part.replace("~1", "/").replace("~0", "~")
        try:
            node = node[int(key)] if isinstance(node, list) else node[key]
        except (KeyError, IndexError, ValueError, TypeError):
            raise DocsError(f"{where}: nothing at #{pointer}") from None
    return node


def _resolve(
    node: Any, base: Path, root: Path, stack: tuple[tuple[Path, str], ...], cache: dict[Path, Any]
) -> Any:
    """`node` with every reference to another file, and every `#/...` reference inside
    another file, replaced by its target. References within the root spec are kept: they
    point into the bundle's own `components`."""
    if isinstance(node, list):
        return [_resolve(n, base, root, stack, cache) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: _resolve(v, base, root, stack, cache) for k, v in node.items() if k != "$ref"}
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return out
    file_part, _, pointer = ref.partition("#")
    if not file_part and base == root:
        return {"$ref": ref} | out
    target_file = (base.parent / file_part).resolve() if file_part else base
    key = (target_file, pointer)
    if key in stack:
        raise DocsError(f"{base.name}: {ref} refers to itself")
    try:
        document = _load(target_file, cache)
    except OSError as e:
        raise DocsError(f"{base.name}: can't read {file_part}: {e.strerror}") from None
    target = _pointer(document, pointer, f"{base.name} -> {ref}")
    resolved = _resolve(target, target_file, root, (*stack, key), cache)
    if not pointer and isinstance(resolved, dict):
        resolved = {k: v for k, v in resolved.items() if k not in DROPPED}
    return {**resolved, **out} if isinstance(resolved, dict) else resolved


def bundle_openapi(spec: Path) -> dict[str, Any]:
    """The contract as one document."""
    spec = spec.resolve()
    cache: dict[Path, Any] = {}
    bundled: dict[str, Any] = _resolve(_load(spec, cache), spec, spec, (), cache)
    broken = broken_refs(bundled)
    if broken:
        raise DocsError(f"references that lead nowhere: {broken}")
    return bundled


def broken_refs(bundle: dict[str, Any]) -> list[str]:
    """`#/...` references that don't resolve inside the bundle, and any left pointing at a file."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if not ref.startswith("#"):
                    found.append(ref)
                else:
                    try:
                        _pointer(bundle, ref[1:], "bundle")
                    except DocsError:
                        found.append(ref)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(bundle)
    return sorted(set(found))


# ------------------------------------------------------------------ Redoc


def verify_integrity(data: bytes, integrity: str) -> None:
    """Checks `data` against an npm integrity string, `sha512-<base64>`."""
    algorithm, _, expected = integrity.partition("-")
    if algorithm != "sha512":
        raise DocsError(f"unsupported integrity {algorithm!r}")
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if actual != expected:
        raise DocsError("the downloaded Redoc does not match the pinned checksum: not using it")


def fetch_redoc(cache_dir: Path) -> tuple[bytes, bytes]:
    """Redoc's standalone bundle and its licence, from the pinned npm release."""
    tarball = cache_dir / f"redoc-{REDOC}.tgz"
    if not tarball.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(REDOC_URL, timeout=60) as response:
            tarball.write_bytes(response.read())
    data = tarball.read_bytes()
    try:
        verify_integrity(data, REDOC_INTEGRITY)
    except DocsError:
        tarball.unlink()  # a corrupt cache entry must not stay
        raise
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        files = {}
        for name in ("package/bundles/redoc.standalone.js", "package/LICENSE"):
            member = tar.extractfile(name)  # read in memory: nothing is written by name
            if member is None:
                raise DocsError(f"{name} is not in redoc-{REDOC}.tgz")
            files[name] = member.read()
    return files["package/bundles/redoc.standalone.js"], files["package/LICENSE"]


# ------------------------------------------------------------------ the site

VIEWER = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>trimset API reference</title>
<style>body { margin: 0; }</style>
</head>
<body>
<div id="redoc"></div>
<script src="redoc.standalone.js"></script>
<script>
  Redoc.init("openapi.json", {
    expandResponses: "200,201,202",
    pathInMiddlePanel: true,
    theme: {
      colors: { primary: { main: "#4d7a14" } },
      typography: {
        fontFamily: "system-ui, sans-serif",
        code: { fontFamily: "ui-monospace, monospace" },
      },
    },
  }, document.getElementById("redoc"));
</script>
</body>
</html>
"""


def prepare(root: Path = ROOT, cache_dir: Path | None = None) -> Path:
    """Writes the generated files into docs/api-reference/; returns that folder."""
    cache_dir = (
        cache_dir
        or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "trimset-docs"
    )
    out = root / "docs" / "api-reference"
    out.mkdir(parents=True, exist_ok=True)
    bundle = bundle_openapi(root / "docs" / "03-api" / "openapi.yaml")
    (out / "openapi.json").write_text(json.dumps(bundle, separators=(",", ":"), ensure_ascii=False))
    script, licence = fetch_redoc(cache_dir)
    (out / "redoc.standalone.js").write_bytes(script)
    (out / "LICENSE-redoc.txt").write_bytes(licence)
    (out / "viewer.html").write_text(VIEWER)  # its own page: the theme's CSS never touches Redoc
    return out


def build(root: Path = ROOT) -> int:
    command = [
        "uv", "run", "--no-project", "--exclude-newer", ZENSICAL_CUTOFF,
        "--with", f"zensical=={ZENSICAL}", "zensical", "build", "--clean",
    ]  # fmt: skip
    return subprocess.run(command, cwd=root, check=False).returncode  # noqa: S603


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--prepare", action="store_true", help="only write the generated files")
    args = parser.parse_args(argv)
    try:
        out = prepare()
    except (DocsError, OSError) as e:
        print(f"docs: {e}", file=sys.stderr)
        return 1
    print(f"wrote {out.relative_to(ROOT)}")
    return 0 if args.prepare else build()


if __name__ == "__main__":
    sys.exit(main())
