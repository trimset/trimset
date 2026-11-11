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
