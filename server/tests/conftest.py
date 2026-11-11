import os
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

ADMIN_URL = os.environ.get("TRIMSET_TEST_DATABASE_URL", "")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def dsn() -> AsyncIterator[str]:
    """A fresh, empty database for one test. CI provides Postgres; locally this skips."""
    if not ADMIN_URL:
        pytest.skip("needs TRIMSET_TEST_DATABASE_URL (CI has Postgres)")
    name = "trimset_test_" + uuid.uuid4().hex[:12]
    admin = await asyncpg.connect(ADMIN_URL)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        yield urlunsplit(urlsplit(ADMIN_URL)._replace(path="/" + name))
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
