import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy import stats
from sklearn.calibration import calibration_curve
from sklearn.metrics import brier_score_loss
from sklearn.metrics import log_loss as sk_log_loss

from trimset_stats import metrics
from trimset_stats.metrics import Strategy

STRATEGIES: list[tuple[Strategy, str]] = [("width", "uniform"), ("mass", "quantile")]


def _sample(seed: int, n: int = 5000, skew: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Calibrated draws pushed off by `skew` (1.0 keeps them calibrated)."""
    rng = np.random.default_rng(seed)
    q = rng.beta(0.6, 1.4, size=n)
    y = (rng.random(n) < q).astype(float)
    z = np.log(q / (1 - q)) * skew
    return 1 / (1 + np.exp(-z)), y


@pytest.mark.golden
@pytest.mark.req("FR-17")
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_log_loss_and_brier_match_scikit_learn(seed: int) -> None:
    p, y = _sample(seed, skew=1.7)
    assert metrics.log_loss(p, y) == pytest.approx(sk_log_loss(y, p), rel=1e-12)
    assert metrics.brier(p, y) == pytest.approx(brier_score_loss(y, p), rel=1e-12)


@pytest.mark.golden
@pytest.mark.req("FR-17")
def test_log_loss_clips_certain_mistakes_as_scikit_learn_does() -> None:
    p, y = np.array([0.0, 1.0, 0.5]), np.array([1.0, 0.0, 1.0])
    assert metrics.log_loss(p, y) == pytest.approx(sk_log_loss(y, p, labels=[0, 1]))


@pytest.mark.golden
@pytest.mark.req("FR-17")
def test_multiclass_log_loss_matches_scikit_learn() -> None:
    rng = np.random.default_rng(3)
    probs = rng.dirichlet([0.5, 1.0, 2.0, 0.7], size=2000)
    labels = np.array([rng.choice(4, p=row) for row in probs])
    expected = sk_log_loss(labels, probs, labels=[0, 1, 2, 3])
    assert metrics.multiclass_log_loss(probs, labels) == pytest.approx(expected, rel=1e-12)


@pytest.mark.golden
@pytest.mark.req("FR-17")
@pytest.mark.parametrize(("ours", "theirs"), STRATEGIES)
@pytest.mark.parametrize("bins", [5, 15, 40])
def test_reliability_bins_match_calibration_curve(ours: Strategy, theirs: str, bins: int) -> None:
    p, y = _sample(4, skew=2.0)
    prob_true, prob_pred = calibration_curve(y, p, n_bins=bins, strategy=theirs)
    got = metrics.reliability(p, y, bins=bins, strategy=ours)
    assert [b.observed for b in got] == pytest.approx(prob_true, rel=1e-12)
    assert [b.mean_predicted for b in got] == pytest.approx(prob_pred, rel=1e-12)
    assert sum(b.count for b in got) == len(p)


@pytest.mark.golden
@pytest.mark.req("FR-17")
@pytest.mark.parametrize(("ours", "theirs"), STRATEGIES)
def test_ece_is_the_count_weighted_gap_of_calibration_curve(ours: Strategy, theirs: str) -> None:
    p, y = _sample(5, skew=2.0)
    prob_true, prob_pred = calibration_curve(y, p, n_bins=15, strategy=theirs)
    counts = np.array([b.count for b in metrics.reliability(p, y, strategy=ours)])
    expected = float(np.sum(counts / len(p) * np.abs(prob_true - prob_pred)))
    assert metrics.ece(p, y, strategy=ours) == pytest.approx(expected, rel=1e-12)


def test_equal_width_ece_by_hand() -> None:
    p = np.array([0.05, 0.1, 0.55, 0.6, 0.95, 1.0])
    y = np.array([0, 1, 1, 1, 1, 1])
    # Bins (0, .5], (.5, 1]: |mean .075 - .5| * 2/6 + |mean .775 - 1| * 4/6
    assert metrics.ece(p, y, bins=2) == pytest.approx(0.425 / 3 + 0.225 * 2 / 3)


@pytest.mark.golden
@pytest.mark.req("FR-17")
@pytest.mark.parametrize(("k", "n"), [(0, 10), (3, 10), (10, 10), (17, 250), (0, 1), (1, 1)])
@pytest.mark.parametrize("confidence", [0.9, 0.95, 0.99])
def test_bin_intervals_are_exact_clopper_pearson(k: int, n: int, confidence: float) -> None:
    expected = stats.binomtest(k, n).proportion_ci(confidence_level=confidence, method="exact")
    low, high = metrics.clopper_pearson(k, n, confidence)
    assert (low, high) == pytest.approx((expected.low, expected.high), abs=1e-12)


@pytest.mark.req("FR-17")
def test_a_calibrated_model_has_a_small_ece_and_a_skewed_one_a_large_one() -> None:
    calibrated, skewed = _sample(6, n=50_000), _sample(6, n=50_000, skew=2.5)
    for strategy in ("width", "mass"):
        assert metrics.ece(*calibrated, strategy=strategy) < 0.01
        assert metrics.ece(*skewed, strategy=strategy) > 0.05


def test_events_for_each_question_type() -> None:
    probs = np.array([[0.2, 0.5, 0.3], [0.6, 0.3, 0.1], [0.1, 0.1, 0.8]])
    labels = np.array([1, 2, 2])
    p, y = metrics.events(probs[:, :2] / probs[:, :2].sum(axis=1, keepdims=True), [1, 0, 1], "noul")
    assert p == pytest.approx([0.5 / 0.7, 0.3 / 0.9, 0.5]) and list(y) == [1, 0, 1]
    p, y = metrics.events(probs, labels, "top")
    assert list(p) == [0.5, 0.6, 0.8] and list(y) == [1, 0, 1]
    p, y = metrics.events(probs, labels, "at_least", k=2)
    assert p == pytest.approx([0.3, 0.1, 0.8]) and list(y) == [0, 1, 1]
    with pytest.raises(ValueError):
        metrics.events(probs, labels, "at_least", k=3)


@pytest.mark.parametrize(
    ("p", "y"),
    [
        ([], []),
        ([0.5], [1, 0]),
        ([1.2], [1]),
        ([float("nan")], [1]),
        ([0.5], [2]),
        ([[0.5]], [[1]]),
    ],
)
def test_bad_inputs_are_refused(p: list[object], y: list[object]) -> None:
    for f in (metrics.log_loss, metrics.brier, metrics.ece, metrics.reliability):
        with pytest.raises(ValueError):
            f(p, y)  # type: ignore[operator]


def test_bad_options_are_refused() -> None:
    with pytest.raises(ValueError):
        metrics.ece([0.5], [1], bins=0)
    with pytest.raises(ValueError):
        metrics.ece([0.5], [1], strategy="both")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        metrics.clopper_pearson(3, 2)
    with pytest.raises(ValueError):
        metrics.multiclass_log_loss([[0.5, 0.6]], [0])
    with pytest.raises(ValueError):
        metrics.multiclass_log_loss([[0.5, 0.5]], [2])
    with pytest.raises(ValueError):
        metrics.multiclass_log_loss([[0.5, 0.5]], [0.0])
    with pytest.raises(ValueError):
        metrics.multiclass_log_loss([0.5, 0.5], [0])
    with pytest.raises(ValueError):
        metrics.events([0.5, 0.5], [0], "top")


@settings(max_examples=200, deadline=None)
@given(
    data=st.lists(
        st.tuples(st.floats(0, 1, allow_nan=False), st.integers(0, 1)), min_size=1, max_size=300
    ),
    bins=st.integers(1, 30),
    strategy=st.sampled_from(["width", "mass"]),
)
def test_reliability_and_ece_invariants(
    data: list[tuple[float, int]], bins: int, strategy: Strategy
) -> None:
    p = np.array([d[0] for d in data])
    y = np.array([d[1] for d in data], dtype=float)
    got = metrics.reliability(p, y, bins=bins, strategy=strategy)
    assert sum(b.count for b in got) == len(p)
    for b in got:
        assert b.low <= b.observed <= b.high
        assert b.lower - 1e-12 <= b.mean_predicted <= b.upper + 1e-12
    value = metrics.ece(p, y, bins=bins, strategy=strategy)
    assert 0 <= value <= 1
    expected = sum(b.count / len(p) * abs(b.mean_predicted - b.observed) for b in got)
    assert value == pytest.approx(expected, abs=1e-12)
