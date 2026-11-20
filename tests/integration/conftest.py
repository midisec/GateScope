from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest
from alembic import command
from alembic.config import Config


from tests.integration.test_paper_orders import paperdb as paperdb


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def migration_config(root: Path, database_url: str) -> Config:
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    sqlalchemy_url = database_url
    if sqlalchemy_url.startswith("postgresql://"):
        sqlalchemy_url = sqlalchemy_url.replace("postgresql://", "postgresql+psycopg://", 1)
    config.set_main_option("sqlalchemy.url", sqlalchemy_url.replace("%", "%%"))
    return config


@pytest.fixture
def migrated_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    if TEST_DATABASE_URL is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    database_name = urlparse(TEST_DATABASE_URL).path.removeprefix("/")
    if database_name != "gatescope_test":
        pytest.fail("RLS integration tests only run against a database named gatescope_test")
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", TEST_DATABASE_URL)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
        connection.execute((root / "deploy/postgres/roles.sql").read_text())
    config = migration_config(root, TEST_DATABASE_URL)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    try:
        yield TEST_DATABASE_URL
    finally:
        command.downgrade(config, "base")
