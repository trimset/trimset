"""The release check (TA.1): a tag, the packages' versions and the changelog agree."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "release_check.py"
spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
assert spec and spec.loader
rc = importlib.util.module_from_spec(spec)
sys.modules["release_check"] = rc
spec.loader.exec_module(rc)

CHANGELOG = """# Changelog

## [Unreleased]

Nothing yet.

## [0.1.0a1] - 2026-12-01

First alpha.

### Added
- Everything.

## [0.0.9] - 2026-11-01

Old.
"""


def _repo(tmp_path: Path, version: str = "0.1.0a1", **overrides: str) -> Path:
    for package in rc.PACKAGES:
        (tmp_path / package).mkdir(parents=True)
        v = overrides.get(package, version)
        (tmp_path / package / "pyproject.toml").write_text(
            f'[project]\nname = "x"\nversion = "{v}"\n'
        )
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    return tmp_path


def test_a_tag_that_names_the_version_with_a_dated_section_passes(tmp_path: Path) -> None:
    release = rc.check(_repo(tmp_path), "v0.1.0a1")
    assert (release.version, release.prerelease) == ("0.1.0a1", True)
    assert release.notes.startswith("First alpha.") and "Old." not in release.notes


@pytest.mark.parametrize(
    "version,pre", [("0.1.0", False), ("1.2.3", False), ("0.2.0rc1", True), ("0.1.0b2", True)]
)
def test_prereleases_are_told_from_final_versions(tmp_path: Path, version: str, pre: bool) -> None:
    root = _repo(tmp_path, version)
    (root / "CHANGELOG.md").write_text(f"## [{version}] - 2026-12-01\n\nText.\n")
    assert rc.check(root, f"v{version}").prerelease is pre


def test_a_rehearsal_needs_no_tag_or_changelog(tmp_path: Path) -> None:
    root = _repo(tmp_path, "0.0.0")
    (root / "CHANGELOG.md").unlink()
    release = rc.check(root, None)
    assert (release.version, release.notes) == ("0.0.0", "")


@pytest.mark.parametrize(
    "case,message",
    [
        ("tag", "expected v0.1.0a1"),
        ("disagree", "disagree"),
        ("zero", "still at 0.0.0"),
        ("form", "not a release version"),
        ("nosection", "no section [0.2.0]"),
        ("nodate", "needs a date"),
        ("empty", "is empty"),
    ],
)
def test_every_inconsistency_is_refused_and_says_what_to_fix(
    tmp_path: Path, case: str, message: str
) -> None:
    tag = "v0.1.0a1"
    if case == "tag":
        root, tag = _repo(tmp_path), "v0.1.0a2"
    elif case == "disagree":
        root = _repo(tmp_path, server="0.1.0a2")
    elif case == "zero":
        root, tag = _repo(tmp_path, "0.0.0"), "v0.0.0"
    elif case == "form":
        root, tag = _repo(tmp_path, "0.1.0.dev1"), "v0.1.0.dev1"
    elif case == "nosection":
        root, tag = _repo(tmp_path, "0.2.0"), "v0.2.0"
    elif case == "nodate":
        root = _repo(tmp_path)
        (root / "CHANGELOG.md").write_text("## [0.1.0a1]\n\nText.\n")
    else:
        root = _repo(tmp_path)
        (root / "CHANGELOG.md").write_text(
            "## [0.1.0a1] - 2026-12-01\n\n## [0.0.9] - 2026-11-01\n\nx\n"
        )
    with pytest.raises(rc.ReleaseError, match=message.replace("[", r"\[").replace("]", r"\]")):
        rc.check(root, tag)


def test_main_writes_outputs_and_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    notes = tmp_path / "notes.md"
    assert rc.main(["--tag", "v0.1.0a1", "--notes", str(notes)], root) == 0
    assert out.read_text() == "version=0.1.0a1\nprerelease=true\npublish=true\n"
    assert notes.read_text().startswith("First alpha.")
    assert rc.main(["--dry"], root) == 0
    assert out.read_text().endswith("version=0.1.0a1\nprerelease=true\npublish=false\n")
    assert rc.main(["--tag", "v9.9.9"], root) == 1
