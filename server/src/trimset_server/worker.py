"""The worker process (ADR 0004): periodic jobs. A job queue arrives with the first
on-demand job (T2.6)."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from trimset_server import actions, jobs, outcomes
from trimset_server.auth import DB_ERRORS
from trimset_server.config import Settings
from trimset_server.partitions import ensure_partitions

log = logging.getLogger("trimset.worker")
Job = Callable[[Any], Awaitable[None]]


async def _partitions(conn: Any) -> None:
    if created := await ensure_partitions(conn):
        log.info("created partitions: %s", ", ".join(created))


async def _joins(conn: Any) -> None:
    joined, _ = await outcomes.join_held(conn)  # it logs its own expiries
    applied, dropped = await actions.apply_held(conn)
    if joined or applied:
        log.info("joined %d held outcomes and %d held action reports", joined, applied)
    if dropped:
        log.warning("%d action reports expired without their decision", dropped)


async def _rules(conn: Any) -> None:
    if labelled := await outcomes.apply_default_rules(conn):
        log.info("default rules labelled %d decisions", labelled)


async def _jobs(conn: Any) -> None:
    await jobs.work(conn)


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    jobs: list[tuple[str, float, Job]] = [
        ("partitions", settings.maintenance_interval_s, _partitions),
        ("joins", settings.join_interval_s, _joins),
        ("rules", settings.rules_interval_s, _rules),
        ("jobs", settings.jobs_interval_s, _jobs),
    ]
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2, timeout=5)
    loop = asyncio.get_running_loop()
    due = {name: loop.time() for name, _, _ in jobs}  # every job runs once at start
    log.info("worker started")
    try:
        while not stop.is_set():
            for name, interval, job in jobs:
                if loop.time() < due[name]:
                    continue
                try:
                    async with pool.acquire() as conn:
                        await job(conn)
                except DB_ERRORS as e:
                    log.warning("job %s failed (%s), will retry", name, type(e).__name__)
                due[name] = loop.time() + interval
            wait = max(0.0, min(due.values()) - loop.time())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=wait)
    finally:
        await pool.close()
        log.info("worker stopped")
