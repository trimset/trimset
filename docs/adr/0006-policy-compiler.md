# 0006. Policy compiler: threshold forms and guarantees

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** FR-14, FR-18, FR-20 to FR-26, NFR-09 · **Sources:** [research.md](../research.md) §1

## Context

A policy must say what it guarantees ("false-pass rate ≤ 2% with 95% confidence") and
mean it with finite samples. Four things make that harder:
- the risk that matters is conditional on the action (errors among *accepted* items);
- labels arrive late;
- some actions never get a label (a blocked refund);
- choice and score questions need threshold forms too.

This ADR settles Phase 1 open questions 4 and 5.

## Options for the guarantee

1. **A point estimate with an interval at the chosen threshold.** Not valid: choosing the
   threshold after looking at the data inflates error.
2. **Conformal risk control** (Angelopoulos 2022). It controls the *expected* risk. That
   is useful, but it is not the high-confidence statement users ask for.
3. **Learn-then-Test** (Angelopoulos 2021) with exact binomial p-values and fixed-sequence
   testing. It gives a high-confidence, selection-conditioned guarantee from finite
   samples, with exact-binomial certification (Priye 2026).
4. **E-value methods** (SCoRE, Bai 2026; CSA, Khosravi 2026). Newer, and promising under
   shift and streaming, but less proven. They belong in `research/` and v0.2.

## Decision

Option 3 for every guarantee in v0.1.

### Data and splits (FR-18)

- **Which items count:** only labeled decisions that have *matured* (are older than the
  question's label horizon) within the window. Where `external_ref` is set, only one
  item per (`external_ref`, `question_id`, `model_served`): the latest decision.
- **The split:** each item goes to fit, tune or eval (40 / 40 / 20) by a hash of
  (`decision_id`, the fit's seed).
- **Calibration map:** fitted on the fit split, with the method chosen by 5-fold
  cross-validation inside that split.
- **Thresholds:** tuned on the tune split only.
- **Reporting:** metrics on the eval split.

### Threshold forms (FR-20)

Every v0.1 policy is at most two one-sided **rules**: an accept rule, and a block or
select rule. Each rule is a threshold on one calibrated **decision scalar**:

| Type | Decision scalar | Rules |
|---|---|---|
| noul | s = calibrated p | ACCEPT when s < lo; BLOCK when s ≥ hi; ESCALATE in between. The project sets which side blocks, for example UC-5, where high p means safe. |
| choice | s = calibrated probability of the top option | SELECT:top when s ≥ t, or t_k for each option; otherwise ESCALATE. Options mapped to BLOCK return BLOCK. |
| score | s_k = calibrated P(level ≥ k) at configured cut levels | For example BLOCK when P(level ≥ 3) ≥ h, ACCEPT when P(level ≥ 1) < l, otherwise ESCALATE. |

The calibration map travels inside the policy. The SDK applies it before comparing
against the thresholds.

### Guarantee (FR-21 to FR-23)

1. A rule's risk is r(λ) = P(wrong | the rule fires at λ). What counts as wrong:
   - for ACCEPT, the outcome says the item should have been blocked;
   - for BLOCK, the outcome says it should have been accepted;
   - for SELECT:x, the outcome is not x.
2. **The test at each threshold:**
   - Candidate thresholds are up to 200 quantiles of the tune-split scalar, ordered from
     strictest (fires on the fewest items) to loosest.
   - At each candidate, let n be the items the rule fires on and k the wrong ones among
     them. The p-value is P(Binomial(n, α) ≤ k).
3. **Fixed-sequence testing:** walk the order, keep thresholds while p ≤ δ_rule, and stop
   at the first that fails. The chosen threshold is the loosest one kept. The confidence
   budget δ is split across rules by Bonferroni (δ/2 for two rules, δ/K for per-option
   thresholds). This is valid whether or not risk rises steadily with the threshold;
   when it doesn't, the walk just stops earlier.
4. **Statement:** "With probability ≥ 1−δ over the tuning data, P(wrong | ACCEPT) ≤ α on
   future traffic that is exchangeable with the tuning data." It is shown with n and the
   window.
5. **Error budget over all automatic decisions:** each rule must meet α. The overall rate
   is a weighted average of the rule rates, so it is then ≤ α. This is conservative, and
   simple.

### Missing labels (FR-14)

The project configures each action path, which keeps the choice independent of the data:
- **If the path has a default-by-rule label,** so every matured item eventually has one:
  use all fired items.
- **Otherwise:** use only the *audited* fired items, which are a random sample (Lakkaraju
  2017).

In both cases, a fired item still without a label counts as **wrong**. That can only make
the bound more cautious, never invalid.

### Availability and feasibility (FR-23, FR-24)

- **Before compiling:** estimate the base risk μ on the fit split. If μ > α, at least
  (μ−α)/(1−α) of items must be escalated (Kotte 2026). If that is more than the
  escalation cap, the result is "infeasible", naming that constraint.
- **Not enough labels:** when the best threshold still has too few labeled items, say how
  many more are needed. With no errors, a rule needs n ≥ ln δ / ln(1−α) labeled fired
  items (Priye 2026). For α = 0.005 and δ = 0.05, that is 598.

### Escalation cap and cost targets (FR-21)

- **The escalation cap is an operating limit, not a guarantee.** It is checked on the tune
  split's point estimate and reported with an interval on the eval split.
- **Cost targets** minimise the empirical expected cost on the tune split (Elkan 2001):
  the cost of each escalation plus the cost of each wrong action. Where risk rules are
  also set, cost picks among the certified thresholds. A policy with only a cost target
  carries `guarantee: none` ("expected-cost optimum").

### Under shift (FR-25)

- **The active policy:** once a drift report or a failing live check
  ([0007](0007-drift-and-health.md)) arrives after the tune window ended, its guarantee
  reads **suspended**. This is computed when the policy is read, never stored.
- **Proposals made during a shift** are tuned on a recent window (the last 14 days by
  default, Gama 2014). They carry `guarantee: none` with the reason "adaptive:
  recent-window refit under detected shift".
- **Adaptive conformal inference stays in `research/`:** Kotte 2026 found it missing
  its target on 14 of 16 shifted transfers.

## Consequences

- Every guarantee trimset shows has a named method, its assumptions and its sample
  sizes, and is checked by the coverage experiment (NFR-09).
- With rare events (UC-5), policies stay uncertified until enough labels arrive, and
  trimset says so.
- Several budgets at once (MultiRisk) and guarantees per slice (HG-CRC) are v0.2.
  Expectation guarantees (LEC) are a v0.2 candidate.
