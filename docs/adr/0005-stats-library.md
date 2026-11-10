# 0005. Stats implementation

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** FR-15 to FR-19, FR-23, FR-38, FR-46, NFR-08, NFR-19 · **Sources:** [research.md](../research.md)

## Context

Statistical correctness is a requirement: NFR-08 needs golden tests against reference
implementations. The methods are standard; the bookkeeping around them is ours:
- splits;
- provenance;
- keeping fits away from the items they were fitted on.

## Options

1. **scikit-learn at runtime.** It covers the calibrators and metrics, but adds a heavy
   dependency, and the splits and provenance would still be ours.
2. **A calibration library** such as netcal. Another dependency, with less control over
   edge cases.
3. **Our own thin library** on numpy and scipy, with scikit-learn used only as a test
   reference.

## Decision

`trimset-stats`: pure functions over numpy arrays, with no I/O:

| Need | Built on |
|---|---|
| Temperature scaling (Guo 2017) | `scipy.optimize.minimize` on negative log likelihood |
| Platt scaling (Platt 1999) | The same, with the stable formulation of Lin 2007 |
| Isotonic regression (Zadrozny 2002) | `scipy.optimize.isotonic_regression` (SciPy ≥ 1.12) |
| Choosing the calibration method | 5-fold cross-validated log loss inside the fit split (Niculescu-Mizil 2005) |
| Log loss, Brier, ECE with equal-width and equal-mass bins (Naeini 2015, Kumar 2019) | numpy |
| Exact binomial p-values and Clopper-Pearson intervals (Clopper 1934) | `scipy.stats.binom`, `scipy.stats.binomtest` |
| PSI (Yurdakul 2018), Jensen-Shannon (Lin 1991) | numpy. scipy's `jensenshannon` returns the distance, so we report its square. |
| Permutation p-values (Phipson 2010) | Our own vectorised resampling, with p = (b+1)/(m+1) |
| McNemar's exact test (McNemar 1947) | Binomial test on the discordant pairs |
| Confidence sequences (Howard 2018) | Closed-form mixture boundaries, implemented and golden-tested |

All randomness comes from an explicit numpy `Generator`, seeded from the fit or job id
(NFR-19).

## Consequences

- scikit-learn is a test-only dependency. Golden tests compare our fits and metrics with
  it on fixed seeds.
- Every function is a pure function, so it is easy to property-test and to reuse in
  `research/`.
- The compiler and drift checks have their own ADRs:
  [0006](0006-policy-compiler.md) and [0007](0007-drift-and-health.md).
