# Releasing

A release is a tag on `main`. `.github/workflows/release.yml` does the rest, in an order
that leaves nothing half-done:

```
check -> image (amd64, arm64) + Python package -> multi-arch tags -> PyPI -> GitHub release
```

PyPI is last because it cannot be undone: a file, once uploaded, is there for good. An
image tag can be moved.

## Making a release

1. **Pick the version.** `X.Y.Z`, or `X.Y.ZaN`, `X.Y.ZbN`, `X.Y.ZrcN` for a prerelease.
2. **Prepare it in a pull request:**
   ```bash
   for p in trimset trimset-stats trimset-server; do uv version --package $p X.Y.Z; done
   ```
   Rename `## [Unreleased]` in [CHANGELOG.md](../CHANGELOG.md) to `## [X.Y.Z] - YYYY-MM-DD`,
   add a fresh empty `[Unreleased]` above it, and update the README's status and
   "not yet" lines. The three packages carry one version. Merge it once CI is green.
3. **Tag the merge commit on `main` and push the tag:**
   ```bash
   git switch main && git pull
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```
4. **Watch the run.** If the `pypi` job has a required reviewer, approve it when the
   images and the package have built.

What the workflow checks before it publishes anything (`tools/release_check.py`): the tag
is `v` plus the packages' version, the packages agree, the version is a plain
`X.Y.Z[aN|bN|rcN]`, CHANGELOG.md has a dated section for it, the tagged commit is on
`main`, and CI was green on that commit. The GitHub release's notes are the changelog
section.

An alpha, beta or release candidate is marked as a prerelease on GitHub and never takes
the `latest` image tag.

## What gets published

| What | Where | How |
|---|---|---|
| Container image, `linux/amd64` and `linux/arm64` | `ghcr.io/trimset/trimset:X.Y.Z` (and `:latest` for a final release) | Each architecture builds on its own runner, is started once to check it runs, then the two are joined under one tag. |
| The Python SDK and CLI | `pypi.org/project/trimset` | `uv publish` with PyPI trusted publishing: no token exists anywhere. Files already on PyPI are skipped, so a rerun is safe. |
| Release notes | GitHub Releases | `gh release create` from the changelog. |

`trimset-stats` and `trimset-server` are inside the image and are not published to PyPI.

## Rehearsal

The same workflow runs without publishing, by hand (`gh workflow run release.yml`) and on
every pull request that touches `Dockerfile`, a lockfile, a `pyproject.toml`, the release
script or the workflow. It builds both images, starts each, builds the wheel and sdist,
runs `twine check --strict`, installs the wheel on Python 3.11 and runs the CLI. A
dependency bump that breaks the arm64 image shows up in that pull request, not on the day
of a release.

## One-time setup (the maintainer, in the browser)

1. **PyPI.** Sign in, go to *Your projects → Publishing* and add a **pending publisher**
   for the project `trimset`: owner `trimset`, repository `trimset`, workflow
   `release.yml`, environment `pypi`. The name is free at the time of writing; the
   pending publisher reserves it.
2. **GitHub environment.** Repository *Settings → Environments → New environment*:
   `pypi`. Optionally add yourself as a required reviewer, so that every upload waits for
   your click.
3. **GitHub Container Registry.** The first push creates the package `trimset` under the
   organisation, private. Open *Packages → trimset → Package settings* and set the
   visibility to **public** (once). The image's `org.opencontainers.image.source` label
   links the package to this repository.
4. **Rulesets.** If a ruleset restricts tag creation, allow the maintainer to create
   `v*` tags.

Nothing in the repository holds a secret for any of this.

## If a release goes wrong

- **The run fails before PyPI.** Nothing irreversible happened. Fix, delete the tag
  (`git push origin :refs/tags/vX.Y.Z`), and tag again. Image tags are overwritten.
- **PyPI succeeded, a later step failed.** Rerun the failed job. Uploading the same files
  again is skipped, not an error.
- **A bad release is out.** Yank it on PyPI (it stays installable by exact version but is
  no longer chosen), and release `X.Y.Z+1`. Never reuse a version.
