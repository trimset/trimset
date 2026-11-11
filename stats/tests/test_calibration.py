import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sklearn.calibration import _SigmoidCalibration, _TemperatureScaling
from sklearn.isotonic import IsotonicRegression

from trimset_stats import calibration, metrics
from trimset_stats.calibration import Calibrator


def _overconfident(seed: int, n: int, power: float = 3.0) -> tuple[np.ndarray, np.ndarray]:
    """Calibrated beliefs q, outcomes drawn from q, and reported p = sigmoid(power·logit q)."""
    rng = np.random.default_rng(seed)
    q = rng.beta(1.2, 1.2, size=n)
    y = (rng.random(n) < q).astype(float)
    return 1 / (1 + np.exp(-power * np.log(q / (1 - q)))), y


@pytest.mark.golden
@pytest.mark.req("FR-16")
@pytest.mark.parametrize("seed", [0, 1])
def test_binary_temperature_matches_scikit_learn(seed: int) -> None:
    p, y = _overconfident(seed, 3000)
    ours = calibration.fit_temperature(p, y)
    # scikit-learn turns a 1-D decision value x into the logits (-x, x): x = logit / 2.
    theirs = _TemperatureScaling().fit(calibration.logit(p) / 2, y)
    assert 1 / ours.temperature == pytest.approx(float(theirs.beta_), rel=1e-6)
    expected = 1 / (1 + np.exp(-float(theirs.beta_) * calibration.logit(p)))
    assert ours.apply(p) == pytest.approx(expected, abs=1e-5)


@pytest.mark.golden
@pytest.mark.req("FR-16")
def test_multiclass_temperature_matches_scikit_learn() -> None:
    rng = np.random.default_rng(2)
    truth = rng.dirichlet([1.0, 1.0, 1.0, 1.0], size=3000)
    labels = np.array([rng.choice(4, p=row) for row in truth])
    sharp = truth**2.5 / (truth**2.5).sum(axis=1, keepdims=True)  # overconfident
    ours = calibration.fit_temperature_multiclass(sharp, labels)
    logits = np.log(calibration.clamp(sharp))  # negative, so read as logits, not probabilities
    theirs = _TemperatureScaling().fit(logits, labels)
    assert 1 / ours.temperature == pytest.approx(float(theirs.beta_), rel=1e-6)
    assert ours.apply(sharp) == pytest.approx(theirs.predict(logits), abs=1e-5)
    assert ours.temperature > 1  # an overconfident model gets a temperature above 1


@pytest.mark.golden
@pytest.mark.req("FR-16")
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_platt_matches_scikit_learn(seed: int) -> None:
    p, y = _overconfident(seed, 2000, power=0.5)  # underconfident this time
    ours = calibration.fit_platt(p, y)
    theirs = _SigmoidCalibration().fit(calibration.logit(p), y)
    assert (ours.a, ours.b) == pytest.approx((theirs.a_, theirs.b_), rel=1e-4, abs=1e-6)
    assert ours.apply(p) == pytest.approx(theirs.predict(calibration.logit(p)), abs=1e-6)


@pytest.mark.golden
@pytest.mark.req("FR-16")
@pytest.mark.parametrize("seed", [0, 1])
def test_isotonic_matches_scikit_learn_including_ties(seed: int) -> None:
    rng = np.random.default_rng(seed)
    p = np.round(rng.random(2000), 2)  # many ties
    y = (rng.random(2000) < p**2).astype(float)
    ours = calibration.fit_isotonic(p, y)
    theirs = IsotonicRegression(out_of_bounds="clip").fit(calibration.clamp(p), y)
    grid = np.linspace(0, 1, 1001)
    assert ours.apply(grid) == pytest.approx(theirs.predict(calibration.clamp(grid)), abs=1e-12)
    assert len(ours.x) < len(np.unique(p))  # only the knots that matter are kept


def test_the_sdks_worked_example() -> None:
    """docs/03-api/sdk.md §3: Platt with a = -0.71, b = 0.42 maps p = 0.83 to 0.6695."""
    assert Calibrator("platt", a=-0.71, b=0.42).apply([0.83])[0] == pytest.approx(0.6695, abs=5e-5)


def test_maps_round_trip_through_the_policys_json() -> None:
    p, y = _overconfident(9, 3000)
    for fitted in (
        Calibrator("identity"),
        calibration.fit_temperature(p, y),
        calibration.fit_platt(p, y),
        calibration.fit_isotonic(p, y),
    ):
        again = Calibrator.from_json(fitted.to_json())
        assert again == fitted
        assert np.array_equal(again.apply(p), fitted.apply(p))
    with pytest.raises(ValueError):
        Calibrator.from_json({"method": "magic"})


def test_isotonic_keeps_at_most_a_thousand_knots_and_at_least_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rng = np.random.default_rng(10)
    p = rng.random(50_000)
    y = (rng.random(p.size) < p).astype(float)
    many = calibration.fit_isotonic(p, y)
    assert 2 <= len(many.x) <= calibration.MAX_KNOTS
    assert np.all(np.diff(many.apply(np.linspace(0, 1, 2001))) >= 0)
    monkeypatch.setattr(calibration, "MAX_KNOTS", 5)  # real fits rarely reach 1,000
    capped = calibration.fit_isotonic(p, y)
    assert len(capped.x) == 5 and capped.x[0] == many.x[0] and capped.x[-1] == many.x[-1]
    assert np.all(np.diff(capped.y) >= 0)
    one = calibration.fit_isotonic([0.3, 0.3], [0, 1])
    assert one.x == (0.3, 0.3) and one.apply([0.1, 0.9]).tolist() == [0.5, 0.5]


@pytest.mark.golden
@pytest.mark.req("FR-16")
def test_one_temperature_removes_most_of_an_overconfident_models_error() -> None:
    """Guo 2017's finding, on synthetic overconfident scores."""
    p, y = _overconfident(3, 20_000, power=2.5)
    fit_p, fit_y, test_p, test_y = p[:10_000], y[:10_000], p[10_000:], y[10_000:]
    calibrated = calibration.fit_temperature(fit_p, fit_y).apply(test_p)
    before, after = metrics.ece(test_p, test_y), metrics.ece(calibrated, test_y)
    assert before > 0.08
    assert after < 0.25 * before


@pytest.mark.golden
@pytest.mark.req("FR-16")
def test_platt_wins_with_little_data_and_isotonic_with_more() -> None:
    """Niculescu-Mizil 2005's finding, on a distortion that isn't a sigmoid of logit p,
    averaged over seeds and scored on fresh data."""

    def distort(q: np.ndarray) -> np.ndarray:
        return np.where(q < 0.5, 0.5 * (2 * q) ** 2.2, 1 - 0.5 * (2 * (1 - q)) ** 0.6)

    def mean_loss(n: int, method: str) -> float:
        losses = []
        for seed in range(30):
            rng = np.random.default_rng(seed)
            q = rng.random(n + 20_000)
            y = (rng.random(q.size) < q).astype(float)
            p = distort(q)
            fit = calibration.fit_platt if method == "platt" else calibration.fit_isotonic
            calibrator = fit(p[:n], y[:n])
            losses.append(metrics.log_loss(calibrator.apply(p[n:]), y[n:]))
        return float(np.mean(losses))

    assert mean_loss(60, "platt") < mean_loss(60, "isotonic")
    assert mean_loss(20_000, "isotonic") < mean_loss(20_000, "platt")


@pytest.mark.req("FR-16")
def test_choose_picks_a_map_for_a_distorted_model_and_records_every_score() -> None:
    p, y = _overconfident(4, 4000, power=2.5)
    choice = calibration.choose(p, y, seed=7)
    assert set(choice.scores) == {"identity", "temperature", "platt", "isotonic"}
    assert choice.calibrator.method != "identity"
    assert choice.scores[choice.calibrator.method] == min(choice.scores.values())
    assert metrics.ece(choice.calibrator.apply(p), y) < 0.3 * metrics.ece(p, y)


@pytest.mark.req("NFR-19")
def test_the_same_data_and_seed_give_the_same_choice() -> None:
    p, y = _overconfident(5, 1500)
    assert calibration.choose(p, y, seed=11) == calibration.choose(p, y, seed=11)


def test_a_reversed_score_is_refused_by_platt_and_skipped_by_choose() -> None:
    p, y = _overconfident(6, 2000)
    with pytest.raises(ValueError, match="keeps no order"):
        calibration.fit_platt(1 - p, y)
    choice = calibration.choose(1 - p, y, seed=1)
    assert "platt" in choice.refused and "platt" not in choice.scores


def test_choose_needs_enough_of_each_outcome() -> None:
    with pytest.raises(ValueError, match="at least 5 of each outcome"):
        calibration.choose([0.2, 0.4, 0.6, 0.8] * 3, [0, 0, 0, 1] * 3, seed=0)
    with pytest.raises(ValueError):
        calibration.choose([0.5] * 20, [0, 1] * 10, seed=0, folds=1)


@pytest.mark.req("FR-16")
def test_choose_multiclass_uses_temperature_only_when_it_helps() -> None:
    rng = np.random.default_rng(8)
    truth = rng.dirichlet([1.0, 1.0, 1.0], size=3000)
    labels = np.array([rng.choice(3, p=row) for row in truth])
    sharp = truth**3 / (truth**3).sum(axis=1, keepdims=True)
    assert calibration.choose_multiclass(sharp, labels, seed=1).calibrator.method == "temperature"
    calibrated = calibration.choose_multiclass(truth, labels, seed=1)
    assert calibrated.scores["identity"] <= calibrated.scores["temperature"] + 0.01
    with pytest.raises(ValueError):
        calibration.choose_multiclass(truth[:6], labels[:6], seed=1)


def test_maps_for_one_event_dont_take_distributions() -> None:
    with pytest.raises(ValueError):
        Calibrator("platt").apply([[0.5, 0.5]])
    assert Calibrator("identity").apply([[0.2, 0.8]]).tolist() == [[0.2, 0.8]]
    assert Calibrator("identity").apply([0.0, 1.0]).tolist() == [
        1e-6,
        1 - 1e-6,
    ]  # clamped, as the SDK does


@pytest.mark.parametrize(("p", "y"), [([], []), ([0.5], [1, 0]), ([1.5], [1]), ([0.5], [3])])
def test_bad_inputs_are_refused(p: list[float], y: list[float]) -> None:
    for fit in (calibration.fit_temperature, calibration.fit_platt, calibration.fit_isotonic):
        with pytest.raises(ValueError):
            fit(p, y)


@settings(max_examples=60, deadline=None)
@given(seed=st.integers(0, 10_000), power=st.floats(0.3, 4.0), n=st.integers(40, 400))
def test_every_map_keeps_the_order_of_probabilities(seed: int, power: float, n: int) -> None:
    """Thresholds on calibrated p are thresholds on raw p (ADR 0006)."""
    p, y = _overconfident(seed, n, power)
    if y.min() == y.max():
        return
    grid = np.linspace(0, 1, 501)
    fits = [calibration.fit_temperature, calibration.fit_isotonic]
    try:
        fits.append(calibration.fit_platt)
        maps = [fit(p, y) for fit in fits]
    except ValueError:
        maps = [fit(p, y) for fit in fits[:2]]
    for calibrator in maps:
        out = calibrator.apply(grid)
        assert np.all(np.diff(out) >= -1e-12), calibrator.method
        assert np.all((out >= 0) & (out <= 1))
