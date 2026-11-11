"""Calibration metrics and reliability-diagram data (FR-17).

Every policy thresholds one probability of one event, so the metrics take that event's
predicted probability `p` and its outcome `y` (1 or 0):
- noul: p = P(yes), y = the answer was yes;
- choice: p = the top option's probability, y = the top option was right;
- score: p = P(level ≥ k) at a cut k, y = the level was at least k.

`events()` makes those pairs. `multiclass_log_loss` scores a whole distribution, which
choosing a calibration method for choice and score questions needs (FR-16).

Binning matches scikit-learn's `calibration_curve`: bins are closed on the right, and
equal-mass edges are the quantiles of `p`. Sources: Naeini 2015, Guo 2017, Kumar 2019;
the bin intervals are Clopper-Pearson (Clopper 1934). See docs/research.md.
"""

from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
from scipy import stats

Strategy = Literal["width", "mass"]
Array = npt.NDArray[np.float64]
DEFAULT_BINS = 15
_EPS = float(np.finfo(np.float64).eps)  # the clip scikit-learn's log_loss uses


def _pairs(p: npt.ArrayLike, y: npt.ArrayLike) -> tuple[Array, Array]:
    p_arr = np.asarray(p, dtype=np.float64)
    y_arr = np.asarray(y, dtype=np.float64)
    if p_arr.ndim != 1 or p_arr.shape != y_arr.shape or p_arr.size == 0:
        raise ValueError("p and y must be non-empty 1-D arrays of the same length")
    if not np.all(np.isfinite(p_arr)) or np.any((p_arr < 0) | (p_arr > 1)):
        raise ValueError("p must lie in [0, 1]")
    if np.any((y_arr != 0) & (y_arr != 1)):
        raise ValueError("y must be 0 or 1")
    return p_arr, y_arr


def log_loss(p: npt.ArrayLike, y: npt.ArrayLike) -> float:
    """Mean negative log likelihood of the outcomes, in nats."""
    p_arr, y_arr = _pairs(p, y)
    clipped = np.clip(p_arr, _EPS, 1 - _EPS)
    return float(-np.mean(y_arr * np.log(clipped) + (1 - y_arr) * np.log1p(-clipped)))


def brier(p: npt.ArrayLike, y: npt.ArrayLike) -> float:
    """Mean squared difference between the probability and the outcome."""
    p_arr, y_arr = _pairs(p, y)
    return float(np.mean((p_arr - y_arr) ** 2))


def multiclass_log_loss(probs: npt.ArrayLike, labels: npt.ArrayLike) -> float:
    """Mean negative log probability given to the true class. `probs` is (n, k) with
    rows summing to 1; `labels` holds class indices."""
    probs_arr = np.asarray(probs, dtype=np.float64)
    labels_arr = np.asarray(labels)
    if probs_arr.ndim != 2 or labels_arr.shape != (probs_arr.shape[0],) or labels_arr.size == 0:
        raise ValueError("probs must be (n, k) and labels (n,)")
    if not np.issubdtype(labels_arr.dtype, np.integer) or np.any(
        (labels_arr < 0) | (labels_arr >= probs_arr.shape[1])
    ):
        raise ValueError("labels must be class indices")
    if not np.allclose(probs_arr.sum(axis=1), 1, atol=1e-6) or np.any(probs_arr < 0):
        raise ValueError("each row of probs must be a distribution")
    true = probs_arr[np.arange(labels_arr.size), labels_arr]
    return float(-np.mean(np.log(np.clip(true, _EPS, 1))))


def events(
    probs: npt.ArrayLike,
    labels: npt.ArrayLike,
    kind: Literal["noul", "top", "at_least"],
    k: int = 1,
) -> tuple[Array, Array]:
    """The (p, y) pairs a policy thresholds. `probs` is (n, k) over the classes, in
    question order; for noul, class 1 is yes. `at_least` needs the cut level `k`."""
    probs_arr = np.asarray(probs, dtype=np.float64)
    labels_arr = np.asarray(labels)
    if probs_arr.ndim != 2 or labels_arr.shape != (probs_arr.shape[0],):
        raise ValueError("probs must be (n, k) and labels (n,)")
    if kind == "noul":
        return probs_arr[:, 1], (labels_arr == 1).astype(np.float64)
    if kind == "top":
        top = probs_arr.argmax(axis=1)
        return probs_arr.max(axis=1), (top == labels_arr).astype(np.float64)
    if not 1 <= k < probs_arr.shape[1]:
        raise ValueError("the cut level must be between 1 and the number of levels - 1")
    return np.clip(probs_arr[:, k:].sum(axis=1), 0, 1), (labels_arr >= k).astype(np.float64)


@dataclass(frozen=True)
class Bin:
    """One bin of a reliability diagram. `observed` is the share of outcomes that were
    1; `low` and `high` bound it with the requested confidence (Clopper-Pearson)."""

    lower: float  # the bin's edges on the predicted axis
    upper: float
    count: int
    mean_predicted: float
    observed: float
    low: float
    high: float


def _bin_ids(p: Array, bins: int, strategy: Strategy) -> tuple[npt.NDArray[np.intp], Array]:
    if bins < 1:
        raise ValueError("bins must be at least 1")
    if strategy == "width":
        edges = np.linspace(0.0, 1.0, bins + 1)
    elif strategy == "mass":
        edges = np.percentile(p, np.linspace(0, 100, bins + 1))
    else:
        raise ValueError("strategy must be 'width' or 'mass'")
    return np.searchsorted(edges[1:-1], p), edges


def clopper_pearson(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """The exact two-sided interval for a binomial proportion (Clopper 1934)."""
    if n <= 0 or not 0 <= successes <= n:
        raise ValueError("need 0 <= successes <= n and n > 0")
    alpha = 1 - confidence
    low = 0.0 if successes == 0 else float(stats.beta.ppf(alpha / 2, successes, n - successes + 1))
    high = (
        1.0
        if successes == n
        else float(stats.beta.ppf(1 - alpha / 2, successes + 1, n - successes))
    )
    return low, high


def reliability(
    p: npt.ArrayLike,
    y: npt.ArrayLike,
    bins: int = DEFAULT_BINS,
    strategy: Strategy = "width",
    confidence: float = 0.95,
) -> list[Bin]:
    """Reliability-diagram data: the non-empty bins, in order."""
    p_arr, y_arr = _pairs(p, y)
    ids, edges = _bin_ids(p_arr, bins, strategy)
    totals = np.bincount(ids, minlength=bins)
    sums = np.bincount(ids, weights=p_arr, minlength=bins)
    ones = np.bincount(ids, weights=y_arr, minlength=bins)
    out = []
    for b in np.flatnonzero(totals):
        n, k = int(totals[b]), round(float(ones[b]))
        low, high = clopper_pearson(k, n, confidence)
        out.append(
            Bin(float(edges[b]), float(edges[b + 1]), n, float(sums[b] / n), k / n, low, high)
        )
    return out


def ece(
    p: npt.ArrayLike, y: npt.ArrayLike, bins: int = DEFAULT_BINS, strategy: Strategy = "width"
) -> float:
    """Expected calibration error: the count-weighted mean gap between the mean
    prediction and the observed frequency over the bins (Naeini 2015; equal-mass bins as
    in Kumar 2019)."""
    p_arr, y_arr = _pairs(p, y)
    ids, _ = _bin_ids(p_arr, bins, strategy)
    totals = np.bincount(ids, minlength=bins)
    gaps = np.abs(np.bincount(ids, weights=p_arr - y_arr, minlength=bins))
    return float(gaps.sum() / totals.sum())
