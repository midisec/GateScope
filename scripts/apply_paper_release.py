"""Apply only the reviewed 0045 -> 0046 PAPER release; keep credentials private."""
import argparse
import contextlib
import io
import json

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.conninfo import conninfo_to_dict

from prepare_service_envs import ROOT, SECRET_DIR, read_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.parse_args()
    old, target = "0045_mcp_research_read", "0046_paper_order_execution"
    try:
        source = read_env(SECRET_DIR / "formal-runtime.env")
        url = source["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
        if conninfo_to_dict(url).get("dbname") != "gatescope":
            raise ValueError("unexpected production database")
        with psycopg.connect(url, connect_timeout=10) as connection:
            current = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        if current not in {old, target}:
            raise ValueError("unexpected migration starting point")
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("postgresql://", "postgresql+psycopg://", 1).replace("%", "%%"))
        config.attributes["database_url_is_explicit"] = True
        # Tracebacks/SQL/DSNs stay out of the deployment transcript on any error.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            command.upgrade(config, target)
        with psycopg.connect(url, connect_timeout=10) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        print(json.dumps({"ok": revision == target, "from": current, "to": revision}))
        return 0 if revision == target else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "private_details_suppressed": True}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
