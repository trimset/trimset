"""Monthly partitions for time-partitioned tables (ADR 0003).

Partitions cover every month from the oldest one still inside retention to three months
ahead, so a decision logged late (up to the retention) or early (up to 5 minutes ahead)
always has somewhere to go.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

PARTITIONED_TABLES = ("decisions",)
MONTHS_AHEAD = 3
DEFAULT_RETENTION_DAYS = 400  # FR-56
_LOCK = 7_246_521_002  # pg advisory lock: one partition run at a time


def add_months(month: date, n: int) -> date:
    y, m = divmod(month.year * 12 + month.month - 1 + n, 12)
    return date(y, m + 1, 1)


def partition_months(now: datetime, retention_days: int) -> list[date]:
    """First day of every month that needs a partition, oldest first."""
    now = now.astimezone(UTC)
    first = (now - timedelta(days=retention_days)).date().replace(day=1)
    last = add_months(now.date().replace(day=1), MONTHS_AHEAD)
    months = [first]
    while months[-1] < last:
        months.append(add_months(months[-1], 1))
    return months


def partition_ddl(table: str, month: date) -> tuple[str, str]:
    if table not in PARTITIONED_TABLES:
        raise ValueError(f"{table} is not partitioned")
    name = f"{table}_p{month:%Y_%m}"
    start, end = month.isoformat(), add_months(month, 1).isoformat()
    # Built from constants and computed dates only, never from input.
    ddl = (
        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {table} "
        f"FOR VALUES FROM ('{start} 00:00:00+00') TO ('{end} 00:00:00+00')"
    )
    return name, ddl


async def max_retention_days(conn: Any) -> int:
    days = await conn.fetchval(
        "SELECT max((settings->>'retention_days')::int) FROM projects "
        "WHERE jsonb_typeof(settings->'retention_days') = 'number'"
    )
    return int(days or DEFAULT_RETENTION_DAYS)


async def ensure_partitions(conn: Any, now: datetime | None = None) -> list[str]:
    """Create the partitions that are missing. Returns the names it created."""
    now = now or datetime.now(UTC)
    created: list[str] = []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _LOCK)
        months = partition_months(now, await max_retention_days(conn))
        rows = await conn.fetch(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = ANY($1::text[])",
            list(PARTITIONED_TABLES),
        )
        existing = {r["relname"] for r in rows}
        for table in PARTITIONED_TABLES:
            for month in months:
                name, ddl = partition_ddl(table, month)
                if name not in existing:
                    await conn.execute(ddl)
                    created.append(name)
    return created
