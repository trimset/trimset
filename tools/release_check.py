"""Checks that a release is consistent before anything is published (TA.1, NFR-16).

    python3 tools/release_check.py --tag v0.1.0a1 --notes notes.md   # on a tag push
    python3 tools/release_check.py --dry                             # a rehearsal

A release is `v` + a PEP 440 version (`0.1.0`, `0.1.0a1`, `0.1.0b2`, `0.1.0rc1`). The
three packages that ship (trimset, trimset-stats, trimset-server) carry the same version,
the tag names it, and CHANGELOG.md has a dated section for it, whose text becomes the
GitHub release's notes. Writes version, prerelease and publish to $GITHUB_OUTPUT when set.
"""

import argparse
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("sdk/python", "stats", "server")
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)((?:a|b|rc)\d+)?$")
HEADING = re.compile(r"^## \[(?P<name>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$")


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    prerelease: bool
    notes: str


def versions(root: Path) -> dict[str, str]:
    found = {}
    for package in PACKAGES:
        data = tomllib.loads((root / package / "pyproject.toml").read_text())
        found[package] = data["project"]["version"]
    return found


def changelog_section(text: str, version: str) -> str:
    """The body of `## [version] - date`, or raises."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = HEADING.match(line)
        if m and m["name"] == version:
            if not m["date"]:
                raise ReleaseError(f"CHANGELOG.md: section [{version}] needs a date: - YYYY-MM-DD")
            end = next((j for j in range(i + 1, len(lines)) if HEADING.match(lines[j])), len(lines))
            body = "\n".join(lines[i + 1 : end]).strip()
            if not body:
                raise ReleaseError(f"CHANGELOG.md: section [{version}] is empty")
            return body
    raise ReleaseError(
        f"CHANGELOG.md has no section [{version}]: rename [Unreleased] and add its date"
    )


def check(root: Path, tag: str | None) -> Release:
    """The release `tag` names (or, without one, a rehearsal of the current version)."""
    found = versions(root)
    if len(set(found.values())) != 1:
        raise ReleaseError(f"the packages disagree on the version: {found}")
    version = next(iter(found.values()))
    if not VERSION.match(version):
        raise ReleaseError(f"{version!r} is not a release version like 0.1.0 or 0.1.0a1")
    prerelease = bool(VERSION.match(version)[4])  # type: ignore[index]
    if tag is None:
        return Release(version, prerelease, "")
    if version == "0.0.0":
        raise ReleaseError("the packages are still at 0.0.0: bump them before tagging")
    if tag != f"v{version}":
        raise ReleaseError(
            f"the tag is {tag} but the packages are at {version}: expected v{version}"
        )
    notes = changelog_section((root / "CHANGELOG.md").read_text(), version)
    return Release(version, prerelease, notes)


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tag", help="the pushed tag, e.g. v0.1.0a1")
    group.add_argument("--dry", action="store_true", help="a rehearsal: nothing is published")
    parser.add_argument("--notes", type=Path, help="write the release notes here")
    args = parser.parse_args(argv)
    try:
        release = check(root, args.tag)
    except (ReleaseError, OSError, KeyError, tomllib.TOMLDecodeError) as e:
        print(f"release check failed: {e}", file=sys.stderr)
        return 1
    if args.notes:
        args.notes.write_text(release.notes + "\n")
    outputs = {
        "version": release.version,
        "prerelease": str(release.prerelease).lower(),
        "publish": str(args.tag is not None).lower(),
    }
    print(", ".join(f"{k}={v}" for k, v in outputs.items()))
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as f:
            f.writelines(f"{k}={v}\n" for k, v in outputs.items())
    return 0


if __name__ == "__main__":
    sys.exit(main())
