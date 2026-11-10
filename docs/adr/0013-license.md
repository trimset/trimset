# 0013. License

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** NFR-18

## Context

trimset is open source, self-hosted, and meant to be adopted inside companies. Its
neighbours (Ollaya, the decision-model SDKs) are mostly Apache-2.0.

## Options

1. **MIT.** Permissive, but it grants no patents.
2. **Apache-2.0.** Permissive, with a patent grant, and familiar to company legal teams.
3. **AGPL-3.0.** Protects against a hosted fork, but many companies won't adopt it.
4. **Source-available** (BSL, Elastic License). Not open source, which conflicts with the
   project's aim.

## Decision

**Apache-2.0** for everything in the repository: code, docs and the site.
- Contributions come in under the same licence, with a Developer Certificate of Origin
  sign-off (`git commit -s`) instead of a contributor licence agreement.
- CI fails if a shipped dependency carries a licence that conflicts with Apache-2.0, such
  as GPL or AGPL (NFR-18).

## Consequences

- Anyone may offer trimset as a hosted service. That is accepted. Hosted SaaS is a
  non-goal for this project, not something to defend.
- Phase 7 adds `LICENSE`, `NOTICE` and the DCO check.
