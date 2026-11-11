import uuid

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trimset_stats import dataset
from trimset_stats.dataset import DAY, Item

NOW = 1_800_000_000.0


def _item(n: int, days_ago: float, label: int | None = 1, **extra: object) -> Item:
    return Item(
        decision_id=str(uuid.UUID(int=n)),
        ts=NOW - days_ago * DAY,
        probs=(0.3, 0.7),
        label=label,
        **extra,  # type: ignore[arg-type]
    )


@pytest.mark.req("FR-18")
def test_the_split_is_a_pure_function_of_the_decision_and_the_seed() -> None:
    ids = [str(uuid.UUID(int=i)) for i in range(20_000)]
    first = [dataset.split_of(i, seed=3) for i in ids]
    assert first == [dataset.split_of(i, seed=3) for i in ids]
    shares = {s: first.count(s) / len(ids) for s in dataset.SPLITS}
    assert shares == pytest.approx({"fit": 0.4, "tune": 0.4, "eval": 0.2}, abs=0.015)
    other = [dataset.split_of(i, seed=4) for i in ids]
    assert sum(a != b for a, b in zip(first, other, strict=True)) > 0.5 * len(ids)


@pytest.mark.req("FR-19")
def test_window_maturity_labels_and_one_decision_per_reference() -> None:
    items = [
        _item(1, 10),
        _item(2, 100),  # outside the 90-day window
        _item(3, 0.5),  # younger than the 2-day horizon
        _item(4, 10, label=None),
        _item(5, 20, external_ref="rf-1"),
        _item(6, 5, external_ref="rf-1"),  # the latest for rf-1 wins
        _item(7, 30, external_ref="rf-2"),
    ]
    data = dataset.build(items, now=NOW, seed=1, horizon_days=2, minimum=3)
    kept = sorted(data.fit.ids + data.tune.ids + data.eval.ids)
    assert kept == sorted(str(uuid.UUID(int=n)) for n in (1, 4, 6, 7))  # 4 has no label
    assert (data.labeled, data.needed) == (3, 0)
    assert data.dropped == {
        "outside the window": 1,
        "not matured": 1,
        "an older decision for the same external_ref": 1,
    }
    unlabeled = next(p for p in (data.fit, data.tune, data.eval) if str(uuid.UUID(int=4)) in p.ids)
    at = unlabeled.ids.index(str(uuid.UUID(int=4)))
    assert unlabeled.labels[at] == dataset.NO_LABEL and not unlabeled.labeled[at]


@pytest.mark.req("FR-19")
def test_below_the_minimum_it_says_how_many_more_are_needed() -> None:
    data = dataset.build([_item(n, 1) for n in range(150)], now=NOW, seed=0)
    assert (data.labeled, data.needed) == (150, 50)
    empty = dataset.build([], now=NOW, seed=0)
    assert (empty.labeled, empty.needed, empty.fit.probs.shape) == (0, 200, (0, 0))


@pytest.mark.req("FR-18")
def test_items_used_to_fit_are_refused_for_tuning_and_evaluation() -> None:
    data = dataset.build([_item(n, 1) for n in range(500)], now=NOW, seed=2)
    data.check_unseen(data.tune.ids + data.eval.ids)
    with pytest.raises(ValueError, match="used to fit"):
        data.check_unseen([*data.eval.ids, data.fit.ids[0]])


def test_split_arrays_line_up_with_their_ids() -> None:
    items = [
        Item(str(uuid.UUID(int=n)), NOW - DAY, (1 - n / 1000, n / 1000), n % 2) for n in range(300)
    ]
    data = dataset.build(items, now=NOW, seed=5)
    by_id = {i.decision_id: i for i in items}
    for part in (data.fit, data.tune, data.eval):
        assert part.probs.shape == (len(part.ids), 2)
        for row, label, decision in zip(part.probs, part.labels, part.ids, strict=True):
            assert tuple(row) == by_id[decision].probs and label == by_id[decision].label


def test_one_group_has_one_shape() -> None:
    with pytest.raises(ValueError, match="same number of classes"):
        dataset.build([_item(1, 1), Item("x", NOW - DAY, (0.2, 0.3, 0.5), 1)], now=NOW, seed=0)


@pytest.mark.req("FR-14")
def test_coverage_finds_the_path_that_never_gets_labels() -> None:
    items = (
        [_item(n, 10, label=1, action="ACCEPT", labelled_at=NOW - 8 * DAY) for n in range(90)]
        + [_item(100 + n, 10, label=None, action="BLOCK") for n in range(50)]
        + [
            _item(200 + n, 10, label=0, action="ESCALATE", audit=n < 3, labelled_at=NOW - 9 * DAY)
            for n in range(20)
        ]
        + [_item(300, 200, label=1, action="ACCEPT")]  # outside the window
    )
    paths = {p.action: p for p in dataset.coverage(items, now=NOW)}
    assert set(paths) == {"ACCEPT", "BLOCK", "ESCALATE"}
    assert (paths["ACCEPT"].decisions, paths["ACCEPT"].share, paths["ACCEPT"].starved) == (
        90,
        1.0,
        False,
    )
    assert paths["ACCEPT"].median_delay_s == pytest.approx(2 * DAY)
    assert (paths["BLOCK"].labeled, paths["BLOCK"].median_delay_s, paths["BLOCK"].starved) == (
        0,
        None,
        True,
    )
    assert paths["ESCALATE"].audited == 3


@settings(max_examples=50, deadline=None)
@given(
    ages=st.lists(st.floats(0, 120), min_size=1, max_size=200),
    seed=st.integers(0, 1000),
    horizon=st.floats(0, 10),
)
def test_splits_are_disjoint_and_cover_what_was_kept(
    ages: list[float], seed: int, horizon: float
) -> None:
    items = [_item(n, age, label=n % 2) for n, age in enumerate(ages)]
    data = dataset.build(items, now=NOW, seed=seed, horizon_days=horizon)
    parts = [set(data.fit.ids), set(data.tune.ids), set(data.eval.ids)]
    assert not (parts[0] & parts[1]) and not (parts[0] & parts[2]) and not (parts[1] & parts[2])
    assert sum(map(len, parts)) + sum(data.dropped.values()) == len(items)
    assert data.labeled == sum(int(p.labeled.sum()) for p in (data.fit, data.tune, data.eval))
    again = dataset.build(items, now=NOW, seed=seed, horizon_days=horizon)
    for name in dataset.SPLITS:
        ours, theirs = getattr(data, name), getattr(again, name)
        assert ours.ids == theirs.ids
        assert np.array_equal(ours.probs, theirs.probs)
        assert np.array_equal(ours.labels, theirs.labels)
        assert np.array_equal(ours.audit, theirs.audit)
