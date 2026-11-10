Design the web dashboard for **trimset**, an open-source, self-hosted tool for teams that use decision models.

## What the product does

Decision models (TypeSafe's Jev, Ollaya, Ollama, OpenAI decisions) don't write text. An app sends them a state (a message, a ticket, any JSON) and typed questions, and gets back probabilities in milliseconds:
- **noul**: a yes/no probability ("Is this refund request fraudulent?" → 0.83)
- **choice**: one of K options ("Which team should handle this ticket?" → billing 0.71, shipping 0.18, …)
- **score**: a level from 0 to L-1 ("How hard is this request?" → level 2)

Apps turn those probabilities into actions with thresholds: ACCEPT, BLOCK, SELECT:<label>, or ESCALATE (send the uncertain middle to a bigger model or a human). trimset logs every decision, joins it to the real outcome when that arrives (hours to months later), refits calibration and thresholds, proposes a new **policy** for a human to approve, and alerts when the model drifts.

**Users:** ML and platform engineers. They are comfortable with numbers and want dense, honest data, not marketing.

## Layout: the Langfuse pattern, not its look

Use the information architecture of an LLM observability tool like Langfuse:
- a left sidebar with a project switcher at the top
- a top bar with a time-range picker
- big filterable tables, with a detail drawer that slides in from the right when you click a row

Don't copy Langfuse's branding, colours or visuals. Sidebar groups:

- **Overview**
- **Capture:** Decisions, Outcomes
- **Analyze:** Questions, Drift, Shadow
- **Act:** Policies, Review queue, Alerts
- **Settings**

## Visual style: match the existing homepage (attached: site/index.html)

- Dark by default. Background `#0b0d10`, alternate band `#0f1216`, surfaces `#12161b`, hover/selected row `#171c22`, borders `#222830` and `#2a3038`, dividers `#1a1f26`.
- Text `#e8eaed`, secondary `#b6bdc6`, muted `#7f8893`. Accent lime `#b8f36b`: keep it for the primary action and the active state. Add semantic colours (right, wrong, pending, warning) that pass WCAG AA on these backgrounds and are safe for colour-blind readers.
- Fonts: Space Grotesk for headings, Inter for UI text, **JetBrains Mono for every number, id, probability, hash and code sample**.
- Radii: 16px cards, 10–12px buttons and inputs, pills fully round. Flat: no gradients, glows or shadows-as-decoration. One border per nesting level, never a card inside a card.
- Also show a light version of the Overview and the Decisions screens.

## Screens

1. **Overview.** Tiles: decisions today, % decided automatically, escalation rate, outcome coverage (% of decisions with a known outcome), active policies, open alerts. Under them, a table of questions, each with a health status and a sparkline.
2. **Decisions** (like Langfuse's trace list).
   - Columns: time, question, model served, top probability, action, policy version, outcome (pending / right / wrong), latency, cost.
   - Filters: question, model, action, outcome status, tags.
   - The row drawer shows: a probability bar chart for that decision, its tags, the state's SHA-256, the policy that decided it, and a timeline of outcome history (arrived, corrected, by whom).
   - Raw state is **hidden by default**. Show a locked placeholder: "State not stored: this project keeps hashes only."
3. **Outcomes.** A feed of arriving outcomes: source (human review, downstream signal, rule), delay since the decision, and corrections. Also unmatched outcomes (unknown `external_ref`).
4. **Questions.** A list of question groups: wording, type, `question_id`, models seen. Opening one shows tabs:
   - **Calibration:**
     - a reliability diagram (predicted against observed, per bin, with a count histogram under it), raw and calibrated overlaid
     - a metrics table: log loss, Brier, ECE equal-width, ECE equal-mass; raw against calibrated
     - a comparison of fit methods: temperature, Platt, isotonic
     - the split sizes (train / calibration / evaluation)
   - **Thresholds and escalation simulator**, the hero screen:
     - A probability axis from 0 to 1 with a histogram of past decisions, coloured by real outcome, and two draggable handles for the escalation band [lo, hi).
     - Live numbers: % automated, error rate on the automated path with its 95% interval, % sent to review, cost per 1,000 decisions.
     - A target input ("false passes ≤ 2% with 95% confidence") and a "Propose policy" button.
     - Show how this adapts for choice (per-option cut-offs) and score (cut-offs between levels).
   - **Drift** and **Policies** for this question (see below).
5. **Policies.**
   - A version history for each question, with state badges: draft, proposed, approved, active, retired.
   - A diff between two versions: thresholds before and after, and metric deltas.
   - Each policy shows its SHA-256 fingerprint and a **guarantee box**: what it promises ("false passes ≤ 2% with 95% confidence") and its assumptions ("labeled n = 12,400; assumes recent traffic looks like the calibration set"). When an assumption is broken, the box says so plainly and shows an "adaptive, no guarantee" badge.
   - Approve, reject and roll back, each with a confirm dialog that names what changes. An audit trail of who did what, when.
6. **Drift.**
   - Time series of PSI and Jensen-Shannon divergence for each question, with the alert line and significance markers (permutation-test p-value).
   - Histograms overlaid: reference window against current window.
   - Event markers when `model_served` changed behind an alias (for example `jev-latest` started answering as a new version).
   - Small panels: escalation rate, error rate, latency p50/p99, and calibration error once labels arrive.
7. **Shadow comparison.**
   - Primary model against candidate model on the same live traffic.
   - Agreement and flip rate, a McNemar 2×2 table with its p-value, accuracy on the labeled subset, and cost and latency deltas.
8. **Review queue.** Escalated and low-confidence items, sorted by priority and age. The review pane shows:
   - the question, the options and the model's probabilities
   - the raw state only if the project opted in, otherwise the locked placeholder
   - one-click label buttons that fit the type (yes/no, the K options, or the L levels), keyboard shortcuts, and skip
   - the reviewer's count for today
9. **Alerts.**
   - A list of alerts: severity, rule, question, status (open, acknowledged, resolved).
   - Rule setup: metric, threshold, channel (webhook, Slack, email).
10. **Settings.**
    - API keys: ingest-only or admin, shown once on creation, revoke.
    - Privacy: a switch for "Store raw state" (off by default, with a clear warning).
    - Data retention.
    - Upstream model endpoints for proxy mode, and the shadow model.

## States to design

- **Empty project:** no decisions yet. Show the one-line adoption snippet: point `TYPESAFE_BASE_URL` at trimset.
- **Loading:** skeletons.
- **Too few samples:** "n = 43: too few to give a guarantee", shown wherever a guarantee would appear.
- **Outcomes still pending:** decisions with no outcome yet.
- **Server error.**

## Sample data (illustrative; label it as such)

- `refund_fraud`
  - type: noul, "Is this refund request fraudulent?"
  - policy v7: ACCEPT below 0.30, ESCALATE from 0.30 to 0.85, BLOCK from 0.85
  - chargebacks arrive 30–90 days later
- `ticket_triage`
  - type: choice, "Which team should handle this ticket?"
  - options: billing, shipping, returns, tech
- `request_difficulty`
  - type: score, "How hard is this request?"
  - levels: 0–3; routes to a small or a large model
- Models: `jev-1.13.0` (behind the alias `jev-latest`), `laya:latest` on Ollaya, `gpt-6-luna`.

## Rules

- Desktop first (1440px). The Review queue and Alerts must also work on a 390px phone, with no sideways scrolling of the page; wide tables scroll inside their own card.
- Never show right/wrong or severity by colour alone: add an icon or a word.
- Every chart has axis labels and units. Probabilities show 2 decimals; rates show a %.
- Deliver every screen above, plus a small component sheet: table, filter bar, drawer, tiles, badges (actions, policy states, outcome states), guarantee box, charts.
