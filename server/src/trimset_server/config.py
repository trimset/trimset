"""Server settings, read once from the environment."""

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    log_level: str = "INFO"
    db_pool_max: int = 10
    auto_migrate: bool = False  # compose turns it on; operators may run `migrate` instead
    maintenance_interval_s: float = 3600.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        url = env.get("TRIMSET_DATABASE_URL")
        if not url:
            raise ValueError("TRIMSET_DATABASE_URL is not set")
        return cls(
            database_url=url,
            log_level=env.get("TRIMSET_LOG_LEVEL", "INFO").upper(),
            db_pool_max=int(env.get("TRIMSET_DB_POOL_MAX", "10")),
            auto_migrate=env.get("TRIMSET_AUTO_MIGRATE", "false").lower() in ("1", "true", "yes"),
            maintenance_interval_s=float(env.get("TRIMSET_MAINTENANCE_INTERVAL_S", "3600")),
        )
