"""Read-only exact snapshot replay; no research trials or settings are written."""
import hashlib
import json
from datetime import datetime
from decimal import Decimal

import psycopg

from prepare_service_envs import SECRET_DIR, read_env
from gatescope.domain.market import Bar
from gatescope.quant.backtest import StrategySpec, run_backtest
from gatescope.quant.artifacts import report_digest
from gatescope.quant.performance import daily_performance, performance_screen


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False)


def main():
    env = read_env(SECRET_DIR/"formal-runtime.env")
    with psycopg.connect(env["GATESCOPE_MIGRATION_DATABASE_URL"]) as conn:
        rows = conn.execute("SELECT report_json FROM research_backtest_results WHERE report_json ? 'study' ORDER BY created_at").fetchall()
    sources = {}
    for (saved,) in rows:
        bars = []
        for value in saved["input_bars"]:
            kwargs = {k:v for k,v in value.items() if k != "payload_hash"}
            for key in ("open_time","close_time","available_at"):
                kwargs[key] = datetime.fromisoformat(kwargs[key])
            for key in ("open","high","low","close","volume"):
                kwargs[key] = Decimal(kwargs[key])
            kwargs["quality_flags"] = frozenset(kwargs["quality_flags"])
            bars.append(Bar(**kwargs))
        replay = run_backtest(bars,StrategySpec.model_validate(saved["spec"]),
            source_manifest=saved["source_manifest"],tested_hypotheses=saved["evidence"]["tested_hypotheses"])
        replay["study"] = saved["study"]
        # Compatibility replay for immutable v1.3.0 facts, including its signed
        # zero serialization. Numeric strategy/metric logic has not changed.
        if saved["engine_version"] == "research-ohlc-1.3.0":
            replay["engine_version"] = saved["engine_version"]
            replay.pop("artifact_digest_method", None)
        elif saved["engine_version"] != replay["engine_version"]:
            raise RuntimeError("This verifier does not implement that engine version")
        replay.pop("artifact_digest")
        original_digest = saved.pop("artifact_digest")
        expected = report_digest(replay) if saved.get("artifact_digest_method") else hashlib.sha256(canonical(replay).encode()).hexdigest()
        assert expected == original_digest and saved == replay, "Snapshot replay does not match immutable report"
        daily = daily_performance(saved["validation_equity_curve"])
        assert all(v == saved["validation"][k] for k,v in daily.items())
        assert performance_screen(saved["validation"], full_drawdown=saved["full"]["metrics"]["max_drawdown"]) == saved["screening"]
        key = (bars[0].instrument_id,bars[0].timeframe,saved["study"]["plan_digest"])
        sources.setdefault(key,set()).add(hashlib.sha256(canonical(saved["source_manifest"]).encode()).hexdigest())
        print("Verified snapshot replay, original digest and metrics: trial",saved["study"]["trial_index"],flush=True)
    assert all(len(values)==1 for values in sources.values())
    print("All reports verified:",len(rows),"; identical frozen datasets:",len(sources))


if __name__ == "__main__":
    main()
