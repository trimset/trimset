"""The worker process (ADR 0004). For now it keeps partitions ready; jobs arrive later."""

import asyncio
import contextlib
import logging

import asyncpg

from trimset_server.config import Settings
from trimset_server.partitions import ensure_partitions

log = logging.getLogger("trimset.worker")


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2, timeout=5)
    log.info("worker started")
    try:
        while not stop.is_set():
            try:
                async with pool.acquire() as conn:
                    created = await ensure_partitions(conn)
                if created:
                    log.info("created partitions: %s", ", ".join(created))
            except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                log.warning("partition maintenance failed (%s), will retry", type(e).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.maintenance_interval_s)
    finally:
        await pool.close()
        log.info("worker stopped")
