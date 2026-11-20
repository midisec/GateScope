"""Fixed, mechanism-based carry experiments. No parameter fitting or live writes."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from gatescope.quant.carry import carry_backtest
from gatescope.quant.artifacts import report_digest

SOURCE = Path("data/research-market/binance-carry-20210101-20260911")


def stamp(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def write(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, allow_nan=False)


def main():
    os.umask(0o077)
    target = Path("data/research-studies") / datetime.now(timezone.utc).strftime("carry-%Y%m%dT%H%M%SZ")
    target.mkdir(parents=True)
    plan = dict(
        version="funded-carry-v1.0.1",
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"],
        families=["calendar", "payback", "basis"],
        cost_multipliers=[1, 2],
        from_time="2022-01-01",
        to_time="2026-09-11",
        pre_development="2021 data used for warmup only; no optimized parameters",
        trial_count=24,
        definition="Each actual hedge unwinds, pays both-leg costs, and sits flat for one day before possible reentry. Cycles are correlated, not independent assets.",
        validation_scope="FULL_SAMPLE_RESEARCH",
        revision_note="v1.0.1 corrects inherited OOS screening text only; rules, costs, prices and performance unchanged. Not a new unseen trial.",
    )
    write(target / "plan.json", plan)
    print("Carry plan frozen:", target, flush=True)
    rows = []
    for symbol in plan["symbols"]:
        data = {
            kind: json.loads((SOURCE / f"{symbol}-{kind}.json").read_text())
            for kind in ("spot", "perp", "mark", "funding")
        }
        for family in plan["families"]:
            for cost in plan["cost_multipliers"]:
                r = carry_backtest(
                    data,
                    family=family,
                    start=stamp(plan["from_time"]),
                    end=stamp(plan["to_time"]),
                    cost_multiplier=cost,
                )
                r["source_digests"] = {k: v["sha256"] for k, v in data.items()}
                r["symbol"] = symbol
                r["plan"] = plan
                r["artifact_digest"] = report_digest(r)
                write(target / f"{symbol}-{family}-{cost}.json", r)
                rows.append(
                    {
                        k: r[k]
                        for k in (
                            "symbol",
                            "family",
                            "metrics",
                            "screening",
                            "margin_failure",
                            "funding_mark_proxy_count",
                            "cost_multiplier",
                            "artifact_digest",
                        )
                    }
                )
                m = r["metrics"]
                print(
                    symbol,
                    family,
                    cost,
                    "sharpe",
                    round(m["sharpe_ratio"] or 0, 3),
                    "t",
                    round(m["t_statistic"] or 0, 3),
                    "win",
                    round(m["win_rate"] or 0, 3),
                    "dd",
                    round(m["max_drawdown"], 4),
                    "trades",
                    m["trade_count"],
                    "return",
                    round(m["total_return"], 4),
                    r["screening"]["status"],
                    flush=True,
                )
    write(target / "summary.json", dict(plan=plan, results=rows))
    print("Finished", target, flush=True)


if __name__ == "__main__":
    main()
