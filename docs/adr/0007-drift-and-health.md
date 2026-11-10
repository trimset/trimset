# 0007. Drift and health detection

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** FR-38 to FR-41, FR-43, SM-3, SM-4 · **Sources:** [research.md](../research.md) §1

## Context

Labels arrive late, sometimes months later, so label-based checks are slow. Alerts that
fire on small samples teach people to ignore them. Model versions change behind aliases.

## Options

1. **Rule-of-thumb PSI cut-offs** (0.1 / 0.25), with no test. False alarms on small
   samples.
2. **Tests on the inputs.** trimset never stores raw state by default, so it can't do
   this.
3. **Black-box shift detection on the model's output probabilities** (Rabanser 2019;
   Lipton 2018). It needs only what we store, and it was among the strongest detectors
   in that study.

## Decision

Option 3, plus deterministic and label-based checks.

**Label-free drift (FR-38):** for each group, using the decision scalars from
[0006](0006-policy-compiler.md):
- **Windows:** the current window is the last 24 hours. The reference is the active
  policy's tune window when a policy exists, otherwise the 7 days before the current
  window.
- **Effect sizes:** PSI on 10 equal-mass bins of the reference, with ε smoothing; and
  Jensen-Shannon divergence in base 2, which lies in [0, 1] (Lin 1991).
- **Significance:** a permutation test of PSI, with m = 999 resamples and
  p = (b+1)/(m+1) (Phipson 2010). Each window is subsampled to at most 5,000 decisions.
- **Alert rule:** alert only when PSI ≥ 0.2 **and** p < 0.01 **and** the current window
  has at least 500 decisions. All three are configurable per project and per question.
  SM-3 measures the false-alarm rate on synthetic data with no drift.

**Silent upgrades (FR-39):** deterministic.
- The server records each new (`model_requested`, `model_served`) pair as it ingests.
- A new served version for a known requested model queues an alert job, which runs within
  a minute.

**Health signals (FR-40):**
- **Escalation rate:** compared with the policy's expected rate, using an exact binomial
  test.
- **Upstream error rate:** from the proxy's counters.
- **Latency:** p50 and p99 compared with the previous 7 days. It alerts on a rise of at
  least 50% and at least 20 ms.
- **Calibration:** ECE on matured labels from the last 7 days, compared with the fit's
  ECE on its eval split.

**Live policy check:**
- For each active rule, the realised wrong rate among matured, labeled, fired decisions
  since activation is tracked with an anytime-valid confidence sequence (Howard 2018).
- It alerts "policy violating its target" once the lower bound passes α.
- Checking every hour cannot inflate false alarms: that is what anytime validity buys,
  and Khosravi 2026 applies the same idea to selective risk.
- Only matured labels count (Žliobaitė 2010): label-free drift is the early warning, and
  label-based checks confirm it.

## Consequences

- Drift alerts carry evidence: the effect size, the p-value, both window sizes and the
  histograms.
- A triggered drift report or a failing live check suspends the active policy's
  guarantee ([0006](0006-policy-compiler.md)).
- Many groups checked every hour means many tests. The effect-size floor and the minimum
  sample size are the first defence; if SM-3 shows too many false alarms, a correction
  across groups comes next.
