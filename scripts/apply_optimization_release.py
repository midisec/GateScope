"""Apply the reviewed additive optimization migrations without logging secrets.

Run from the staged release so the serving process keeps its previous code
until the schema is ready. Historical compaction is a separate explicit step.
"""

import argparse
import contextlib
import io
import json
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine import make_url

from prepare_service_envs import ROOT, read_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.add_argument("--runtime-root", type=Path, default=ROOT)
    args = parser.parse_args()
    old, target = "0046_paper_order_execution", "0051_stage_environment"
    try:
        source = read_env(args.runtime_root / "data/runtime-secrets/formal-runtime.env")
        url = source["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
        if conninfo_to_dict(url).get("dbname") != "gatescope":
            raise ValueError("unexpected production database")
        with psycopg.connect(url, connect_timeout=10) as connection:
            current = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        if current not in {old, target}:
            raise ValueError("unexpected migration starting point")
        configured = (
            make_url(url)
            .set(drivername="postgresql+psycopg")
            .update_query_dict(
                {
                    "options": "-c lock_timeout=2s -c statement_timeout=30s "
                    "-c idle_in_transaction_session_timeout=60s",
                    "connect_timeout": "10",
                }
            )
        )
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        config.set_main_option(
            "sqlalchemy.url", configured.render_as_string(hide_password=False).replace("%", "%%")
        )
        config.attributes["database_url_is_explicit"] = True
        # DDL is transactional. A lock timeout rolls back this attempt; do not
        # automatically retry or kill blocking production transactions.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            command.upgrade(config, target)
        with psycopg.connect(url, connect_timeout=10) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        print(
            json.dumps(
                {"ok": revision == target, "from": current, "to": revision, "historical_records_repacked": 0}
            )
        )
        return 0 if revision == target else 1
    except Exception as exc:
        original = getattr(exc, "orig", exc)
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "sqlstate": getattr(original, "sqlstate", None),
                          "private_details_suppressed": True}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
