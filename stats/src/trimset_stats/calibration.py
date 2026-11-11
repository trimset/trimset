"""Calibration maps and choosing between them (FR-16, ADR 0005).

A map is applied exactly as the SDKs apply a policy's (docs/03-api/sdk.md §3), so the
thresholds the compiler picks are the ones the SDK will see: p is clamped to
[1e-6, 1 - 1e-6] and logit(p) = ln(p / (1 - p)).
- **identity:** s = p.
- **temperature** (Guo 2017): s = sigmoid(logit p / T); for a distribution, sᵢ = softmax(ln pᵢ / T).
- **platt** (Platt 1999, the stable loss of Lin 2007): s = 1 / (1 + exp(a · logit p + b)).
  a < 0 keeps the order of p; a fit that doesn't is refused.
- **isotonic** (Zadrozny 2002): linear interpolation between knots (x, y), at most 1,000.

Every map is non-decreasing in p, so a threshold on calibrated p is a threshold on raw p.
`to_json` gives the policy's `calibration` object (fit.schema.json).

`choose` picks the map with the lowest cross-validated log loss inside the fit split
(Niculescu-Mizil 2005), from seeded folds, so the same data and seed give the same choice
(NFR-19). Fitting follows scikit-learn's `_TemperatureScaling`, `_SigmoidCalibration` and
`IsotonicRegression`; the golden tests compare with them.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import numpy as np
import numpy.typing as npt
from scipy import optimize

from trimset_stats.metrics import log_loss, multiclass_log_loss

Method = Literal["identity", "temperature", "platt", "isotonic"]
Array = npt.NDArray[np.float64]
BINARY_METHODS: tuple[Method, ...] = ("identity", "temperature", "platt", "isotonic")
CLAMP = 1e-6  # sdk.md §3
MAX_KNOTS = 1000  # fit.schema.json
_EPS = float(np.finfo(np.float64).eps)


def clamp(p: npt.ArrayLike) -> Array:
    return np.clip(np.asarray(p, dtype=np.float64), CLAMP, 1 - CLAMP)


def logit(p: npt.ArrayLike) -> Array:
    q = clamp(p)
    return np.asarray(np.log(q / (1 - q)))


def _sigmoid(z: Array) -> Array:
    return np.asarray(np.exp(-np.logaddexp(0.0, -z)))


@dataclass(frozen=True)
class Calibrator:
    """A fitted map, with the parameters the policy stores."""

    method: Method
    temperature: float = 1.0
    a: float = -1.0
    b: float = 0.0
    x: tuple[float, ...] = ()
    y: tuple[float, ...] = ()

    def apply(self, probs: npt.ArrayLike) -> Array:
        """Calibrated probabilities: for a 1-D p, of the event; for an (n, k) array, of
        each class (identity and temperature only)."""
        arr = clamp(probs)
        if arr.ndim == 2 and self.method not in ("identity", "temperature"):
            raise ValueError(
                f"{self.method} calibrates one event's probability, not a distribution"
            )
        if self.method == "identity":
            return arr
        if arr.ndim == 2:
            z = np.log(arr) / self.temperature
            z -= z.max(axis=1, keepdims=True)
            weights = np.exp(z)
            return np.asarray(weights / weights.sum(axis=1, keepdims=True))
        if self.method == "temperature":
            return _sigmoid(logit(arr) / self.temperature)
        if self.method == "platt":
            return _sigmoid(-(self.a * logit(arr) + self.b))
        return np.asarray(np.interp(arr, self.x, self.y))

    def to_json(self) -> dict[str, Any]:
        if self.method == "temperature":
            return {"method": "temperature", "temperature": self.temperature}
        if self.method == "platt":
            return {"method": "platt", "a": self.a, "b": self.b}
        if self.method == "isotonic":
            return {"method": "isotonic", "x": list(self.x), "y": list(self.y)}
        return {"method": "identity"}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Calibrator":
        method = data["method"]
        if method == "temperature":
            return cls("temperature", temperature=float(data["temperature"]))
        if method == "platt":
            return cls("platt", a=float(data["a"]), b=float(data["b"]))
        if method == "isotonic":
            return cls("isotonic", x=tuple(data["x"]), y=tuple(data["y"]))
        if method == "identity":
            return cls("identity")
        raise ValueError(f"unknown calibration method {method!r}")


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
    two_class = np.column_stack([np.zeros_like(p_arr), logit(p_arr)])  # softmax = sigmoid(β·logit)
    return Calibrator("temperature", temperature=1 / _fit_beta(two_class, y_arr.astype(np.intp)))


def fit_temperature_multiclass(probs: npt.ArrayLike, labels: npt.ArrayLike) -> Calibrator:
    probs_arr, labels_arr = np.asarray(probs, dtype=np.float64), np.asarray(labels)
    multiclass_log_loss(probs_arr, labels_arr)  # checks the shapes and the values
    beta = _fit_beta(np.log(clamp(probs_arr)), labels_arr.astype(np.intp))
    return Calibrator("temperature", temperature=1 / beta)


def fit_platt(p: npt.ArrayLike, y: npt.ArrayLike) -> Calibrator:
    """Platt's method: P = 1 / (1 + exp(a·F + b)) on F = logit p, fitted to the smoothed
    targets (N+ + 1)/(N+ + 2) and 1/(N- + 2) with L-BFGS, the loss written with
    logaddexp so it never overflows (Lin 2007)."""
    p_arr, y_arr = _check_binary(p, y)
    f = logit(p_arr)
    positives = float(y_arr.sum())
    negatives = y_arr.size - positives
    t = np.where(y_arr > 0, (positives + 1) / (positives + 2), 1 / (negatives + 2))

    def loss(ab: Array) -> tuple[float, Array]:
        z = -(ab[0] * f + ab[1])  # the log-odds of the event
        value = float(np.sum(t * np.logaddexp(0.0, -z) + (1 - t) * np.logaddexp(0.0, z)))
        residual = _sigmoid(z) - t
        return value, np.array([-np.dot(residual, f), -residual.sum()])

    start = np.array([0.0, float(np.log((negatives + 1) / (positives + 1)))])
    found = optimize.minimize(loss, start, jac=True, method="L-BFGS-B")
    a, b = float(found.x[0]), float(found.x[1])
    if a >= 0:
        raise ValueError("Platt's slope keeps no order: the scores don't rank the outcomes")
    return Calibrator("platt", a=a, b=b)


def fit_isotonic(p: npt.ArrayLike, y: npt.ArrayLike) -> Calibrator:
    """Pool-adjacent-violators on the distinct clamped p, weighted by how often each
    occurs. Only knots that bound a flat run are kept, at most MAX_KNOTS."""
    p_arr, y_arr = _check_binary(p, y)
    xs, inverse, counts = np.unique(clamp(p_arr), return_inverse=True, return_counts=True)
    means = np.bincount(inverse, weights=y_arr) / counts
    ys = optimize.isotonic_regression(means, weights=counts.astype(np.float64)).x
    keep = np.ones(xs.size, dtype=bool)
    if xs.size > 2:
        keep[1:-1] = ~((ys[1:-1] == ys[:-2]) & (ys[1:-1] == ys[2:]))
    xs, ys = xs[keep], ys[keep]
    if xs.size > MAX_KNOTS:  # the schema's limit; an evenly spaced subset stays monotone
        pick = np.unique(np.linspace(0, xs.size - 1, MAX_KNOTS).round().astype(np.intp))
        xs, ys = xs[pick], ys[pick]
    if xs.size == 1:
        xs, ys = np.repeat(xs, 2), np.repeat(ys, 2)
    return Calibrator("isotonic", x=tuple(xs.tolist()), y=tuple(ys.tolist()))


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
                fitted = _BINARY_FITS[method](p_arr[train], y_arr[train])
                predicted[test] = fitted.apply(p_arr[test])
        except ValueError as e:
            refused[method] = str(e)
            continue
        scores[method] = log_loss(predicted, y_arr)
    best = cast(Method, min(scores, key=scores.__getitem__))  # the first lowest: simplest
    return Choice(_BINARY_FITS[best](p_arr, y_arr), scores, refused)


def choose_multiclass(
    probs: npt.ArrayLike, labels: npt.ArrayLike, *, seed: int, folds: int = 5
) -> Choice:
    """Identity or temperature for a whole distribution, by cross-validated log loss."""
    probs_arr, labels_arr = np.asarray(probs, dtype=np.float64), np.asarray(labels)
    multiclass_log_loss(probs_arr, labels_arr)
    if folds < 2 or labels_arr.size < 2 * folds:
        raise ValueError(f"choosing a map needs at least {2 * folds} labels")
    predicted = np.empty_like(probs_arr)
    for test in _folds(labels_arr.size, folds, seed):
        train = np.setdiff1d(np.arange(labels_arr.size), test, assume_unique=True)
        fitted = fit_temperature_multiclass(probs_arr[train], labels_arr[train])
        predicted[test] = fitted.apply(probs_arr[test])
    identity = Calibrator("identity").apply(probs_arr)
    scores = {
        "identity": multiclass_log_loss(identity / identity.sum(axis=1, keepdims=True), labels_arr),
        "temperature": multiclass_log_loss(predicted, labels_arr),
    }
    if scores["temperature"] < scores["identity"]:
        return Choice(fit_temperature_multiclass(probs_arr, labels_arr), scores)
    return Choice(Calibrator("identity"), scores)
