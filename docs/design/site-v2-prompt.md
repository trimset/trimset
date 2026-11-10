Grow the attached one-page homepage (site/index.html) into a full marketing site for **trimset**, an open-source, self-hosted tool for teams that use decision models.

## What the product does

Decision models (TypeSafe's Jev, Ollaya, Ollama, OpenAI decisions) don't write text. An app sends them a state and typed questions and gets probabilities back in milliseconds:
- **noul**: yes/no
- **choice**: one of K options
- **score**: a level

Apps act on those probabilities with thresholds that someone guessed once and never checked again. trimset closes the loop:
1. It logs every decision, through a proxy (change one base URL, no code) or an SDK.
2. It joins each decision to the real outcome when that arrives, hours to months later.
3. It refits calibration and proposes new thresholds that come with a stated guarantee ("false passes ≤ 2% with 95% confidence").
4. A human approves the new policy, the app's SDK picks it up, and trimset alerts when the model drifts.

**Readers:** developers and ML/platform engineers. The project is pre-alpha, Apache-2.0, and has one maintainer.

## Keep the look; don't redesign

Reuse the homepage exactly:
- dark background `#0b0d10`, surfaces `#12161b`, borders `#222830`
- text `#e8eaed` / `#b6bdc6` / `#7f8893`, accent `#b8f36b` through the `--accent` CSS variable
- Space Grotesk headings, Inter body, JetBrains Mono for code and numbers
- 16px card radius, flat, no gradients or glow
- the same header, footer and check-mark logo

## Pages

1. **Home.** Keep the current page.
   - Add a "Use cases" strip linking to the six use cases.
   - Nav: How it works, Use cases, Integrations, Docs, GitHub.
2. **Use cases:** an index plus six pages with one shared template. Each page has:
   - the real-world problem in two or three sentences
   - the request as a short JSON snippet
   - the policy (thresholds plus its guarantee)
   - a small timeline of when the outcome arrives
   - the `gate()` code snippet
   - a placeholder for a dashboard screenshot
   - a status badge: "Demo available" or "Coming soon"

   The six use cases:

   | Use case | Real-world example | Question (type) | Outcome arrives | Target |
   |---|---|---|---|---|
   | PII guardrail | An internal chat assistant checks every prompt before it reaches an LLM | "Does this text contain personal data?" (noul) | Privacy team reviews a sample, days later | False passes ≤ 1% |
   | Support ticket triage | An online shop routes tickets to billing, shipping, returns or tech | "Which team should handle this?" (choice) | The team that finally handled it, hours later | Misroutes ≤ 5% among auto-routed |
   | Refund fraud | The shop screens refund requests before paying out | "Is this refund request fraudulent?" (noul) | Chargeback, 30–90 days later | False passes ≤ 2% with 95% confidence; the middle goes to the risk team |
   | Agent tool approval | An ops agent wants to run a database write or send an email | "Is this tool call safe to run without a human?" (noul) | Human approve/deny; incident reports | Unsafe passes ≤ 0.5% |
   | Model routing | A chat product sends easy requests to a small model and hard ones to a large one | "How hard is this request?" (score 0–3) | Thumbs-down, a retry, or an LLM judge on a sample | Cost of a large-model call vs. a bad answer |
   | Content moderation | A marketplace checks listings before they go live | "How serious is the problem?" (score: none, mild, serious, remove) | Moderator decision or user report, days later | Review queue under N items a day |

   Ticket triage, refund fraud and model routing get "Demo available". The other three get "Coming soon".
3. **How it works.** Expand the four steps (Capture, Join, Refit, Enforce), each with a simple inline-SVG diagram. Add a concepts section:
   - calibration (a reliability-diagram illustration)
   - thresholds and the escalation band (a 0–1 probability axis with lo and hi)
   - drift
   - guarantees: what "≤ 2% with 95% confidence" means, and that trimset says "no guarantee" instead of faking one when traffic shifts
4. **Integrations.** Cards for Jev, Ollaya, Ollama, OpenAI decisions, LangGraph and decision-circuits. Each detail shows a before/after base-URL snippet. Badges: "v0.1" or "Coming soon".
5. **Self-hosting.**
   - `docker compose up`, what it starts (server, worker, Postgres) and what it needs
   - privacy: raw state is never stored unless you switch it on; only hashes and tags are kept
   - data retention
6. **Roadmap.** Milestones with a status each:
   - M1: logging, proxy, outcomes
   - M2: calibration, policy compiler, CLI
   - M3: SDK gate, policy approval
   - M4: drift and alerts
   - M5: shadow models, review queue
   - v0.1 launch
   - then the dashboard and the TypeScript SDK in v0.2
7. **Docs landing.** Cards linking to the quickstart, concepts, API reference and self-hosting guide. These live on a separate docs site, so use `#` links.
8. **404.**

## Honesty rules (strict)

- Pre-alpha: anything not built yet says "Coming soon". The TypeScript SDK is v0.2, so don't show `npm i trimset` as available.
- No invented numbers. Keep the existing `[X]%` placeholders.
- No testimonials, customer logos, star counts, download counts or "trusted by".
- Keep the footer line, extended: "not affiliated with TypeSafe, OpenAI, Ollama or Ollaya".

## Build rules

- Plain static HTML and CSS that GitHub Pages can serve with no build step. Shared header and footer. JavaScript only for small things like copy buttons.
- Diagrams are inline SVG that work on the dark background.
- Must work at 390px phone width with a 16px side gutter. No sideways page scroll: code blocks and tables scroll inside their own box.
- Contrast WCAG AA, visible focus states, semantic headings, alt text.
- Deliver every page above.
