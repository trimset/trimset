"""Calibration maps and choosing between them (FR-16, ADR 0005).

For one event's probability p (metrics.events):
- **temperature** (Guo 2017): sigmoid(β · logit p). One parameter.
- **platt** (Platt 1999, with the stable loss of Lin 2007): sigmoid(a · logit p + b),
  fitted to Platt's smoothed targets.
- **isotonic** (Zadrozny 2002): the best non-decreasing step map, interpolated between
  its knots.
- **identity**: p as it is, so "no calibration" competes too.

Every map is non-decreasing in p, so a threshold on calibrated p is a threshold on raw p
and the SDK needs no calibration code (ADR 0006). A Platt fit whose slope isn't positive
would reverse the order; it is refused.

Choice and score questions get temperature scaling on the whole distribution:
softmax(β · log p).

`choose` picks the map with the lowest cross-validated log loss inside the fit split
(Niculescu-Mizil 2005). Folds come from a seeded generator, so the same data and seed
give the same choice (NFR-19).

The fitting follows scikit-learn's `_TemperatureScaling`, `_SigmoidCalibration` and
`IsotonicRegression` closely enough that the golden tests compare with them directly.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt
from scipy import optimize

from trimset_stats.metrics import log_loss, multiclass_log_loss

Method = Literal["identity", "temperature", "platt", "isotonic"]
Array = npt.NDArray[np.float64]
BINARY_METHODS: tuple[Method, ...] = ("identity", "temperature", "platt", "isotonic")
MULTICLASS_METHODS: tuple[Method, ...] = ("identity", "temperature")
_EPS = float(np.finfo(np.float64).eps)


@dataclass(frozen=True)
class Calibrator:
    """A fitted map. `beta` is the inverse temperature; `a`, `b` are Platt's slope and
    intercept on logit p; `xs`, `ys` are the isotonic knots."""

    method: Method
    beta: float = 1.0
    a: float = 1.0
    b: float = 0.0
    xs: tuple[float, ...] = ()
    ys: tuple[float, ...] = ()

    def apply(self, probs: npt.ArrayLike) -> Array:
        """Calibrated probabilities: for a 1-D p, of the event; for an (n, k) array, of
        each class (identity and temperature only)."""
        arr = np.asarray(probs, dtype=np.float64)
        if self.method == "identity":
            return arr.copy()
        if arr.ndim == 2:
            if self.method != "temperature":
                raise ValueError(
                    f"{self.method} calibrates one event's probability, not a distribution"
                )
            logits = self.beta * np.log(arr + _EPS)
            logits -= logits.max(axis=1, keepdims=True)
            weights = np.exp(logits)
            return np.asarray(weights / weights.sum(axis=1, keepdims=True))
        if self.method == "temperature":
            return _sigmoid(self.beta * _logit(arr))
        if self.method == "platt":
            return _sigmoid(self.a * _logit(arr) + self.b)
        return np.asarray(np.interp(arr, self.xs, self.ys))


def _logit(p: Array) -> Array:
    """The two-class logit scikit-learn uses: log(p + eps) - log(1 - p + eps)."""
    return np.asarray(np.log(p + _EPS) - np.log1p(-p + _EPS))


def _sigmoid(z: Array) -> Array:
    return np.asarray(np.exp(-np.logaddexp(0.0, -z)))


def _check_binary(p: npt.ArrayLike, y: npt.ArrayLike) -> tuple[Array, Array]:
    p_arr, y_arr = np.asarray(p, dtype=np.float64), np.asarray(y, dtype=np.float64)
    if p_arr.ndim != 1 or p_arr.shape != y_arr.shape or p_arr.size == 0:
        raise ValueError("p and y must be non-empty 1-D arrays of the same length")
    if not np.all(np.isfinite(p_arr)) or np.any((p_arr < 0) | (p_arr > 1)):
        raise ValueError("p must lie in [0, 1]")
    if np.any((y_arr != 0) & (y_arr != 1)):
        raise ValueError("y must be 0 or 1")
    return p_arr, y_arr


def _fit_beta(logits: Array, labels: npt.NDArray[np.intp]) -> float:
    """The inverse temperature minimising multinomial log loss, searched over log β in
    [-10, 10] as scikit-learn does."""
    rows = np.arange(labels.size)

    def loss(log_beta: float) -> float:
        z = np.exp(log_beta) * logits
        return float(np.mean(np.logaddexp.reduce(z, axis=1) - z[rows, labels]))

    found = optimize.minimize_scalar(loss, bounds=(-10.0, 10.0), options={"xatol": 64 * _EPS})
    return float(np.exp(found.x))


def fit_temperature(p: npt.ArrayLike, y: npt.ArrayLike) -> Calibrator:
    p_arr, y_arr = _check_binary(p, y)
    logits = np.log(np.column_stack([1 - p_arr, p_arr]) + _EPS)
    return Calibrator("temperature", beta=_fit_beta(logits, y_arr.astype(np.intp)))


def fit_platt(p: npt.ArrayLike, y: npt.ArrayLike) -> Calibrator:
    """Platt's method: logistic regression on logit p against the smoothed targets
    (N+ + 1)/(N+ + 2) and 1/(N- + 2), minimised with L-BFGS on a loss written with
    logaddexp, which never overflows (Lin 2007)."""
    p_arr, y_arr = _check_binary(p, y)
    f = _logit(p_arr)
    scale = max(1.0, float(np.max(np.abs(f))) / 30.0)  # rescaled as scikit-learn does
    f = f / scale
    positives = float(y_arr.sum())
    negatives = y_arr.size - positives
    t = np.where(y_arr > 0, (positives + 1) / (positives + 2), 1 / (negatives + 2))

    def loss(ab: Array) -> tuple[float, Array]:
        z = ab[0] * f + ab[1]
        value = float(np.sum(t * np.logaddexp(0.0, -z) + (1 - t) * np.logaddexp(0.0, z)))
        residual = _sigmoid(z) - t
        return value, np.array([np.dot(residual, f), residual.sum()])

    start = np.array([1.0, float(np.log((positives + 1) / (negatives + 1)))])
    found = optimize.minimize(loss, start, jac=True, method="L-BFGS-B")
    a, b = float(found.x[0]) / scale, float(found.x[1])
    if a <= 0:
        raise ValueError("Platt's slope isn't positive: the scores don't rank the outcomes")
    return Calibrator("platt", a=a, b=b)


def fit_isotonic(p: npt.ArrayLike, y: npt.ArrayLike) -> Calibrator:
    """Pool-adjacent-violators on the distinct p, weighted by how often each occurs;
    only the knots that bound a flat run are kept."""
    p_arr, y_arr = _check_binary(p, y)
    xs, inverse, counts = np.unique(p_arr, return_inverse=True, return_counts=True)
    means = np.bincount(inverse, weights=y_arr) / counts
    ys = optimize.isotonic_regression(means, weights=counts.astype(np.float64)).x
    keep = np.ones(xs.size, dtype=bool)
    if xs.size > 2:
        flat = (ys[1:-1] == ys[:-2]) & (ys[1:-1] == ys[2:])
        keep[1:-1] = ~flat
    return Calibrator("isotonic", xs=tuple(xs[keep].tolist()), ys=tuple(ys[keep].tolist()))


def fit_temperature_multiclass(probs: npt.ArrayLike, labels: npt.ArrayLike) -> Calibrator:
    probs_arr, labels_arr = np.asarray(probs, dtype=np.float64), np.asarray(labels)
    multiclass_log_loss(probs_arr, labels_arr)  # checks the shapes and the values
    return Calibrator(
        "temperature", beta=_fit_beta(np.log(probs_arr + _EPS), labels_arr.astype(np.intp))
    )


_BINARY_FITS: dict[Method, Callable[[Array, Array], Calibrator]] = {
    "identity": lambda p, y: Calibrator("identity"),
    "temperature": fit_temperature,
    "platt": fit_platt,
    "isotonic": fit_isotonic,
}


@dataclass(frozen=True)
class Choice:
    """The chosen map, refitted on all the data, and every candidate's out-of-fold log
    loss. A candidate that couldn't be fitted has no score and a reason."""

    calibrator: Calibrator
    scores: dict[str, float]
    refused: dict[str, str] = field(default_factory=dict)


def _folds(n: int, k: int, seed: int) -> list[npt.NDArray[np.intp]]:
    order = np.random.default_rng(seed).permutation(n)
    return [np.sort(part) for part in np.array_split(order, k)]


def choose(
    p: npt.ArrayLike,
    y: npt.ArrayLike,
    *,
    seed: int,
    folds: int = 5,
    methods: Sequence[Method] = BINARY_METHODS,
) -> Choice:
    """The map with the lowest `folds`-fold cross-validated log loss. Ties go to the
    simpler map, in the order of BINARY_METHODS."""
    p_arr, y_arr = _check_binary(p, y)
    positives = int(y_arr.sum())
    if folds < 2 or min(positives, y_arr.size - positives) < folds:
        raise ValueError(f"choosing a map needs at least {folds} of each outcome")
    parts = _folds(p_arr.size, folds, seed)
    scores: dict[str, float] = {}
    refused: dict[str, str] = {}
    for method in sorted(methods, key=BINARY_METHODS.index):
        predicted = np.empty_like(p_arr)
        try:
            for test in parts:
                train = np.setdiff1d(np.arange(p_arr.size), test, assume_unique=True)
                predicted[test] = _BINARY_FITS[method](p_arr[train], y_arr[train]).apply(
                    p_arr[test]
                )
        except ValueError as e:
            refused[method] = str(e)
            continue
        scores[method] = log_loss(predicted, y_arr)
    best = min(scores, key=lambda m: scores[m])  # the first of the lowest: the simplest
    return Choice(_BINARY_FITS[best](p_arr, y_arr), scores, refused)  # type: ignore[index]


def choose_multiclass(
    probs: npt.ArrayLike, labels: npt.ArrayLike, *, seed: int, folds: int = 5
) -> Choice:
    """Identity or temperature for a whole distribution, by cross-validated log loss."""
    probs_arr, labels_arr = np.asarray(probs, dtype=np.float64), np.asarray(labels)
    multiclass_log_loss(probs_arr, labels_arr)
    if folds < 2 or labels_arr.size < 2 * folds:
        raise ValueError(f"choosing a map needs at least {2 * folds} labels")
    parts = _folds(labels_arr.size, folds, seed)
    predicted = np.empty_like(probs_arr)
    for test in parts:
        train = np.setdiff1d(np.arange(labels_arr.size), test, assume_unique=True)
        predicted[test] = fit_temperature_multiclass(probs_arr[train], labels_arr[train]).apply(
            probs_arr[test]
        )
    scores = {
        "identity": multiclass_log_loss(probs_arr, labels_arr),
        "temperature": multiclass_log_loss(predicted, labels_arr),
    }
    if scores["temperature"] < scores["identity"]:
        return Choice(fit_temperature_multiclass(probs_arr, labels_arr), scores)
    return Choice(Calibrator("identity"), scores)
