# 0016. The docs site is Zensical, on docs.trimset.dev

**Status:** accepted (Phase 5, for T7.1, 2026-11-11) · **Requirements:** NFR-20 ·
**Follows:** [ADR 0002](0002-monorepo-layout.md)

## Context

T7.1 needs a docs site: concepts, an API reference built from
[`openapi.yaml`](../03-api/openapi.yaml), and integration guides. NFR-20 asks that every
user-facing requirement is documented by v0.1.

- What exists: Markdown under `docs/` with Mermaid diagrams, and the website in `site/`,
  14 hand-written static pages on Cloudflare at trimset.dev with no build step. Its Docs
  page links into the repository until the docs site exists.
- The repo is Python, managed with uv. Every dependency must be at least 7 days old.
- The tools, as of 2026-11-11, read from their own release pages:
  - Material for MkDocs went into maintenance mode in November 2025. Its authors promised
    fixes until 2026-11-05 ([issue 8523](https://github.com/squidfunk/mkdocs-material/issues/8523));
    9.8.1 still shipped on 2026-11-07 ([PyPI](https://pypi.org/project/mkdocs-material/)).
  - Zensical is the same authors' successor. It reads `mkdocs.yml`, and it is at 0.1.1
    (2026-11-10), marked beta ([PyPI](https://pypi.org/project/zensical/)).

## Options

1. **Zensical.** Python, Markdown, Mermaid and search built in. Beta today.
2. **Material for MkDocs.** Mature and widely used, but it gets no new features, and its
   promised support window has ended.
3. **A JavaScript framework** (Docusaurus, Starlight). Mature, but it brings a Node
   toolchain into a Python repo, with hundreds of npm packages under the 7-day hold.
4. **Sphinx.** Python, but reStructuredText or MyST, and harder to theme like the site.
5. **Hand-written HTML, like `site/`.** No build step, but no search, and every page
   repeats its header and footer. Docs change too often for that.

## Decision

**Option 1, built at T7.1**, with Option 2 as the fallback through the same `mkdocs.yml`.

- **Pages:** Markdown in the repository, next to the code they describe, so a feature and
  its docs change in one pull request. Mermaid renders the way it does on GitHub.
- **Look:** the site's tokens (`site/style.css`): the dark theme, `--accent`, and Space
  Grotesk / Inter / JetBrains Mono.
- **API reference:** one page that renders `docs/03-api/openapi.yaml` with an OpenAPI
  viewer, pinned like any dependency. Endpoints are never listed by hand, so the
  reference can't drift from the contract.
- **SDK reference:** written from [`sdk.md`](../03-api/sdk.md) while the public surface is
  small (`gate()` and the client settings). Generate it from docstrings only if it grows.
- **Hosting:** docs.trimset.dev, a second assets-only Worker on the same Cloudflare
  account, deployed with wrangler like the site. `site/` keeps no build step.
- **Dependency:** Zensical goes in the dev group, pinned to a release at least 7 days old.
  It ships in no package and no image.

## Consequences

- At T7.1, check Zensical again. If it is still beta or lacks something the docs need,
  build with Material for MkDocs from the same files, and record the switch here.
- When docs.trimset.dev is live, `site/docs.html` links its cards there instead of into
  the repository.
- No Node toolchain enters the repository. The OpenAPI viewer is one pinned script.

## Amendment, 2026-11-12: built early, with two changes

The re-plan after M3 ([04-plan.md](../04-plan.md)) moved the API reference and a skeleton of
the site, with the pages that already exist, ahead of the alpha. Concept pages and
integration guides stay in T7.1, after M4 and M5. Building it settled two details:

- **Zensical is not in the lockfile.** Its first usable release, 0.1.0 (2026-11-05 15:32
  UTC), passed the 7-day hold on 2026-11-12, and the project's resolver cutoff
  (2026-11-03) is still older. Moving the cutoff would re-resolve every development
  dependency for the sake of a documentation tool. `tools/build_docs.py` instead runs it
  from an isolated environment (`uv run --no-project --exclude-newer 2026-11-05T15:35:00Z
  --with zensical==0.1.0`), which holds Zensical's own dependencies at the same date. It
  ships in no package and no image, as decided. When the project cutoff passes that date,
  it can move into the dev group.
- **The OpenAPI viewer is Redoc 2.5.1** (MIT), read-only, so there is no "try it" button
  to put a key into. Redoc 3.1.0, released a week earlier, ships a standalone bundle that
  uses `import.meta` for Node and cannot run from a script tag, so the 2.x line, whose
  browser bundle is what Redoc's own guides describe, is pinned instead. The build fetches it from the npm registry and
  checks it against the registry's own checksum, pinned in the script, so no third-party
  script is committed or loaded from a CDN by the reader. The contract is first bundled into
  one file (`schemas/*.json` inlined), so the viewer never has to fetch files by itself; the
  bundle and the viewer are generated into `docs/api-reference/`, which is not committed.

- **The reference sits in its own page, framed.** Embedded in a theme page, the theme's
  heading colours turned Redoc's titles white on white, and the theme's light-on-dark
  palette fought Redoc's styles at every turn. A generated `viewer.html` that only loads
  Redoc, framed by `api-reference.md`, keeps the two stylesheets apart.
- **Who it is for.** The navigation is Use trimset, Under the hood (the architecture,
  hashing, the decision records) and Contribute (requirements, plan, testing, releasing).
  The kickoff brief, the discovery notes and the design briefs are the project's history:
  they stay in the repository and are left out of the site.

Hosting is as decided: docs.trimset.dev, a second assets-only Worker
(`wrangler.docs.jsonc`), deployed by hand like the site. That needs the DNS record and
the Cloudflare login of the maintainer.
