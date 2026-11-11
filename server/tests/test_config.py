import pytest

from trimset_server.config import Settings


def test_database_url_is_required() -> None:
    with pytest.raises(ValueError, match="TRIMSET_DATABASE_URL"):
        Settings.from_env({})


def test_values_come_from_the_environment() -> None:
    s = Settings.from_env(
        {
            "TRIMSET_DATABASE_URL": "postgresql://x",
            "TRIMSET_LOG_LEVEL": "debug",
            "TRIMSET_DB_POOL_MAX": "4",
            "TRIMSET_AUTO_MIGRATE": "true",
            "TRIMSET_MAINTENANCE_INTERVAL_S": "60",
        }
    )
    assert s == Settings(
        database_url="postgresql://x",
        log_level="DEBUG",
        db_pool_max=4,
        auto_migrate=True,
        maintenance_interval_s=60.0,
    )


def test_auto_migrate_is_off_by_default() -> None:
    assert Settings.from_env({"TRIMSET_DATABASE_URL": "postgresql://x"}).auto_migrate is False
