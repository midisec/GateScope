"""Programmatic Alembic entrypoint used by the legacy composition root."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def alembic_config(root: Path, db_path: Path) -> Config:
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    url_path = db_path.resolve().as_posix()
    config.set_main_option("sqlalchemy.url", f"sqlite:///{url_path}".replace("%", "%%"))
    # An explicitly requested local database must never be redirected to an
    # ambient PostgreSQL URL (particularly on upgrade/downgrade commands).
    config.attributes["database_url_is_explicit"] = True
    return config


def upgrade_database(root: Path, db_path: Path) -> None:
    command.upgrade(alembic_config(root, db_path), "head")
