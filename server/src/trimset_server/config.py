"""Server settings, read once from the environment."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from trimset_server.upstreams import Upstream, upstreams_from_env


@dataclass(frozen=True)
class Settings:
    database_url: str
    log_level: str = "INFO"
    db_pool_max: int = 10
    auto_migrate: bool = False  # compose turns it on; operators may run `migrate` instead
    maintenance_interval_s: float = 3600.0  # partitions
    join_interval_s: float = 60.0  # held outcomes and action reports (FR-11, FR-33)
    rules_interval_s: float = 3600.0  # default labels by rule (FR-13)
    jobs_interval_s: float = 1.0  # fits and compiles (T2.6)
    key_cache_ttl_s: float = 30.0  # a revoked key is refused everywhere within this (FR-52)
    secret: bytes | None = field(default=None, repr=False)  # derives state-hash keys (ADR 0009)
    upstreams: Mapping[str, Upstream] = field(default_factory=dict)
    proxy_buffer: int = 50_000  # records held while the database is slow or down (NFR-06)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        url = env.get("TRIMSET_DATABASE_URL")
        if not url:
            raise ValueError("TRIMSET_DATABASE_URL is not set")
        secret = env.get("TRIMSET_SECRET")
        if secret is not None and len(secret) < 32:
            raise ValueError("TRIMSET_SECRET must be at least 32 characters (openssl rand -hex 32)")
        return cls(
            database_url=url,
            log_level=env.get("TRIMSET_LOG_LEVEL", "INFO").upper(),
            db_pool_max=int(env.get("TRIMSET_DB_POOL_MAX", "10")),
            auto_migrate=env.get("TRIMSET_AUTO_MIGRATE", "false").lower() in ("1", "true", "yes"),
            maintenance_interval_s=float(env.get("TRIMSET_MAINTENANCE_INTERVAL_S", "3600")),
            join_interval_s=float(env.get("TRIMSET_JOIN_INTERVAL_S", "60")),
            rules_interval_s=float(env.get("TRIMSET_RULES_INTERVAL_S", "3600")),
            jobs_interval_s=float(env.get("TRIMSET_JOBS_INTERVAL_S", "1")),
            key_cache_ttl_s=min(float(env.get("TRIMSET_KEY_CACHE_TTL_S", "30")), 60.0),
            secret=secret.encode("utf-8") if secret else None,
            upstreams=upstreams_from_env(env),
            proxy_buffer=int(env.get("TRIMSET_PROXY_BUFFER", "50000")),
        )
