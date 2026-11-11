"""The cron expressions of `refit_schedule` (FR-30)."""

from datetime import UTC, datetime, timedelta

import pytest

from trimset_server.cron import Cron

T = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)  # a Wednesday


@pytest.mark.req("FR-30")
def test_fields_ranges_steps_and_lists() -> None:
    daily = Cron("0 3 * * *")
    assert daily.matches(T) and not daily.matches(T + timedelta(minutes=1))
    assert Cron("*/15 * * * *").matches(T.replace(minute=45))
    assert not Cron("*/15 * * * *").matches(T.replace(minute=50))
    assert Cron("5/20 * * * *").matches(T.replace(minute=45))  # 5, 25, 45
    assert Cron("0 1-5 * * *").matches(T) and not Cron("0 4-5 * * *").matches(T)
    assert Cron("0 3 * * 1,3,5").matches(T) and not Cron("0 3 * * 0,6").matches(T)
    sunday = T + timedelta(days=4)
    assert Cron("0 3 * * 7").matches(sunday) and Cron("0 3 * * 0").matches(sunday)
    assert Cron("0 3 11 11 *").matches(T) and not Cron("0 3 * 12 *").matches(T)


def test_both_day_fields_match_either_as_in_cron() -> None:
    assert Cron("0 3 1 * 3").matches(T)  # not the 1st, but a Wednesday
    assert Cron("0 3 11 * 0").matches(T)  # the 11th, though not a Sunday
    assert not Cron("0 3 1 * 0").matches(T)


def test_latest_finds_the_last_due_minute() -> None:
    daily = Cron("0 3 * * *")
    assert daily.latest(T - timedelta(days=1), T + timedelta(hours=5)) == T
    assert daily.latest(T, T + timedelta(hours=5)) is None  # already done
    assert Cron("*/10 * * * *").latest(T, T + timedelta(minutes=25, seconds=7)) == T.replace(
        minute=20
    )


@pytest.mark.parametrize(
    "bad", ["", "0 3 * *", "60 * * * *", "0 24 * * *", "* * 0 * *", "*/0 * * * *", "a * * * *"]
)
def test_bad_expressions_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        Cron(bad)
