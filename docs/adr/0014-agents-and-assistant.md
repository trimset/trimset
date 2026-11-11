# 0014. Decisions for agents, and a chat assistant, after the guarantees

**Status:** accepted (Phase 5, 2026-11-11) · **Requirements:** UC-5, FR-27–35, FR-54, FR-61

## Context

The question came up in Phase 5: should trimset put a chat layer on top of decision
models, the way ChatGPT put one on top of GPT models?

The comparison only half holds. GPT models already spoke text, so a chat box was the
natural way in. Decision models answer apps, not people: probabilities for typed
questions, read by code. So "chat on top" can mean three different things.

## Options

1. **A chat playground for the models.** Type a situation, ask a question, see the
   probabilities. Easy to build, but it has no moat, and the vendors will ship their own.
2. **An assistant over trimset's own data.** "Why are we blocking more refunds this
   week?" "What threshold keeps false passes under 2%?" "Draft a policy for ticket
   triage." A text model calls trimset's API for fits, simulations, drift reports and
   policy diffs. It serves the approver persona, who wants the trade-off in business
   terms, not a calibration plot.
3. **Decisions for agents.** Agents keep needing small yes/no calls: is this tool call
   safe to run, does it need a human, which tool fits. That is UC-5. An MCP server and an
   agent hook would ask a decision model and let an approved policy turn the answer into
   run, ask a human, or block, with a stated guarantee ("unsafe runs ≤ 0.5%, 95%
   confidence") instead of a hunch.
4. **Nothing.**

## Decision

- **No playground** (option 1).
- **Agents** (option 3): a small MCP server exposing gate, outcome and explain tools, plus
  a Claude Code `PreToolUse` hook demo. Before a command runs, the hook asks "Is this tool
  call safe to run without a human?" and the project's approved policy decides. It is a
  thin layer over the M3 gate (FR-31–35), so it can't come earlier. It is planned for
  v0.2, or before the v0.1.0 launch if the re-plan after M3 finds room.
- **Assistant** (option 2): part of the v2 dashboard (FR-61), once the numbers it
  explains exist (M2 to M4). Its tools are read-only, except that it may draft a policy
  proposal. Approving one stays a human transition (FR-27–29), recorded in the audit log
  (FR-54).

## Consequences

- v0.1's scope and dates don't change.
- The MCP server uses the SDK with its own ingest key, like any app. It needs no new
  server endpoints.
- Agent actions are rare events. UC-5's 0.5% target needs about 600 labelled auto-runs
  with no incident, so until then the gate reports "not enough labels" and escalates
  (FR-23). The demo shows that honestly.
- The assistant's text model is chosen by the operator and off by default. Nothing
  leaves the server unless the operator turns it on, in keeping with the
  no-telemetry and privacy decisions (ADR 0009).
