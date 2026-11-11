from datetime import UTC, date, datetime, timedelta, timezone
from itertools import pairwise

import pytest

from trimset_server.partitions import MONTHS_AHEAD, add_months, partition_ddl, partition_months


@pytest.mark.parametrize(
    ("start", "n", "expected"),
    [
        (date(2026, 11, 1), 1, date(2026, 12, 1)),
        (date(2026, 11, 1), 2, date(2027, 1, 1)),
        (date(2026, 1, 1), -1, date(2025, 12, 1)),
        (date(2026, 3, 1), 12, date(2027, 3, 1)),
    ],
)
def test_add_months_crosses_years(start: date, n: int, expected: date) -> None:
    assert add_months(start, n) == expected


def test_months_run_from_retention_to_three_ahead() -> None:
    now = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)
    months = partition_months(now, retention_days=400)
    assert months[0] == date(2025, 10, 1)  # 400 days before 2026-11-11 is 2025-10-07
    assert months[-1] == add_months(date(2026, 11, 1), MONTHS_AHEAD)
    assert all(b == add_months(a, 1) for a, b in pairwise(months))


def test_months_use_utc_not_local_time() -> None:
    # 23:30 on 30 November in UTC-5 is already 1 December in UTC.
    late = datetime(2026, 11, 30, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert partition_months(late, retention_days=0)[0] == date(2026, 12, 1)


def test_partition_ddl_names_and_bounds() -> None:
    name, ddl = partition_ddl("decisions", date(2026, 12, 1))
    assert name == "decisions_p2026_12"
    assert "PARTITION OF decisions" in ddl
    assert "FROM ('2026-12-01 00:00:00+00') TO ('2027-01-01 00:00:00+00')" in ddl


def test_partition_ddl_refuses_other_tables() -> None:
    with pytest.raises(ValueError, match="not partitioned"):
        partition_ddl("projects; DROP TABLE x", date(2026, 12, 1))
