"""The data generator against the real server and Postgres (CI): every scenario's data
goes in through the API and its labels join (FR-60)."""

from datetime import UTC, datetime

import asyncpg
import pytest
from fastapi.testclient import TestClient

from trimset_server import keys, outcomes
from trimset_server.app import create_app
from trimset_server.config import Settings
from trimset_server.migrate import run_migrations
from trimset_tools.datagen import SCENARIOS, generate, send

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.req("FR-60")
@pytest.mark.parametrize("key", sorted(SCENARIOS))
async def test_every_scenario_goes_in_through_the_api(dsn: str, key: str) -> None:
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        pid = await keys.create_project(conn, "demo")
        ingest = (await keys.create_key(conn, pid, "ingest", "datagen"))[1]
        admin = (await keys.create_key(conn, pid, "admin", "datagen"))[1]
    finally:
        await conn.close()

    now = datetime.now(UTC)
    # Refunds span more than the 120-day default rule; the rest a week, enough to see
    # their rarer labels.
    days, per_day = (150, 20) if key == "refund-fraud" else (7, 100)
    data = generate(key, seed=1, end=now, days=days, per_day=per_day)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        totals = send(data, c, ingest, admin_key=admin, now=now)

    arrived = sum(t <= now for t, _ in data.outcomes)
    assert totals["stored"] == len(data.records)
    assert totals["rejected"] == totals["outcomes_rejected"] == totals["held"] == 0
    assert totals["matched"] == arrived > 0

    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM decisions") == len(data.records)
        joined = await conn.fetchval("SELECT count(*) FROM outcome_history")
        assert joined == arrived
        if data.scenario.default_rule:
            labelled = await outcomes.apply_default_rules(conn)
            assert labelled > 0
            assert await conn.fetchval("SELECT count(*) FROM outcomes WHERE source = 'rule'") == (
                labelled
            )
    finally:
        await conn.close()
