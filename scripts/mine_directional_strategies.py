"""Frozen indicator/chart/wave experiment. Public files only, no business writes."""

import argparse
import bisect
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import gc
import hashlib
import json
import os
from pathlib import Path
import time

from gatescope.domain.market import Bar
from gatescope.quant.artifacts import report_digest
from gatescope.quant.backtest import metrics
from gatescope.quant.directional import (
    FAMILIES,
    Rule,
    VERSION,
    funding_series,
    rules,
    signal_book,
    simulate_directional,
)
from gatescope.quant.performance import daily_performance, performance_screen

SOURCE = Path("data/research-market/binance-directional-20210101-20260911")
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "LTCUSDT", "DOGEUSDT")
NEW_SYMBOLS = SYMBOLS[2:]
DEVELOP_START = "2021-07-01"
DEVELOP_END = "2024-01-01"
VALIDATE_END = "2026-09-11"


def write(path, payload):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, allow_nan=False, ensure_ascii=False)


def load_bars(symbol, timeframe):
    raw = json.loads((SOURCE / f"{symbol}-1h.json").read_text())
    observed = datetime.fromisoformat(raw["fetched_at"])
    multiplier = 1 if timeframe == "1h" else 4
    result = []
    for i in range(0, len(raw["rows"]), multiplier):
        group = raw["rows"][i : i + multiplier]
        if len(group) != multiplier:
            raise ValueError("Incomplete resampling group")
        opened = datetime.fromtimestamp(group[0][0] / 1000, timezone.utc)
        result.append(
            Bar(
                "BINANCE",
                "FUTURES_USDT",
                symbol,
                timeframe,
                opened,
                opened + timedelta(hours=multiplier),
                Decimal(group[0][1]),
                max(Decimal(r[2]) for r in group),
                min(Decimal(r[3]) for r in group),
                Decimal(group[-1][4]),
                sum(Decimal(r[5]) for r in group),
                True,
                1,
                observed,
                quality_flags=frozenset({"HISTORICAL_BACKFILL"}),
            )
        )
    return result


def boundary(bars, day):
    stamp = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return bisect.bisect_left([b.open_time for b in bars], stamp)


def scored(report):
    curve = report["equity_curve"]
    cut = bisect.bisect_left([p["time"] for p in curve], "2023-01-01")
    halves = [daily_performance(curve[: cut + 1]), daily_performance(curve[cut:])]
    return min(m["sharpe_ratio"] if m["sharpe_ratio"] is not None else -100.0 for m in halves), halves


def portfolio(reports):
    assert len(reports) == 4
    keys = [p["time"] for p in reports[0]["equity_curve"]]
    assert all([p["time"] for p in r["equity_curve"]] == keys for r in reports)
    curve = [
        dict(time=t, equity=sum(r["equity_curve"][i]["equity"] for r in reports) / 4)
        for i, t in enumerate(keys)
    ]
    trades = []
    for r in reports:
        for t in r["trades"]:
            item = {**t, "symbol": r["symbol"]}
            for key in ("quantity", "entry_fee", "funding", "net_pnl", "fees"):
                item[key] /= 4
            trades.append(item)
    trades.sort(key=lambda t: (t["entry_time"], t["symbol"]))
    m = {**metrics(trades, curve, 10000.0), **daily_performance(curve)}
    peak, drawdown = 10000.0, 0.0
    for values in zip(*(r["_mtm"] for r in reports), strict=True):
        equity = sum(values) / 4
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    m["max_drawdown"] = drawdown
    return dict(
        metrics=m,
        trades=trades,
        equity_curve=curve,
        screening=performance_screen(m),
        hedged=False,
        deployable=False,
        drawdown_basis="synchronized signal-timeframe MTM, daily chart display",
        symbols=list(NEW_SYMBOLS),
    )


def dataset(symbol, timeframe):
    bars = load_bars(symbol, timeframe)
    points, book = signal_book(bars)
    funding = json.loads((SOURCE / f"{symbol}-funding.json").read_text())["rows"]
    return bars, points, book, funding_series(bars, funding)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    target = args.directory or Path("data/research-studies") / datetime.now(timezone.utc).strftime(
        "directional-%Y%m%dT%H%M%SZ"
    )
    target.mkdir(parents=True, exist_ok=False)
    sources = {}
    for symbol in SYMBOLS:
        for kind in ("1h", "funding"):
            p = SOURCE / f"{symbol}-{kind}.json"
            data = json.loads(p.read_text())
            assert (
                hashlib.sha256(json.dumps(data["rows"], separators=(",", ":")).encode()).hexdigest()
                == data["sha256"]
            )
            sources[p.name] = data["sha256"]
    del data
    plan = dict(
        engine_version=VERSION,
        symbols=list(SYMBOLS),
        timeframes=["1h", "4h"],
        families=FAMILIES,
        rules=[r.payload() for r in rules()],
        develop_from=DEVELOP_START,
        develop_to=DEVELOP_END,
        validate_to=VALIDATE_END,
        holdout_description="2024-01-01—2026-09-11, only BNB/ADA/LTC/DOGE eligible for this round's withheld-price evaluation; BTC/ETH previously seen, development diagnostic only",
        selection="Top 3 distinct symbol/timeframe datasets per family by weaker development-half HAC Sharpe; at least 30 development trades, positive total net return and DD<15%; freeze all before holdout; no replacement after holdout",
        basket="Fixed equal initial-cash BNB/ADA/LTC/DOGE sleeves; same LONG-only rule, no rebalancing, no hedging; include as a separately counted development model",
        execution="Next open; 0.5% risk, <=1x gross account notional, structural stop when present; QUICK 0.8R/24bars, SWING 1.4R/72bars, MEASURED observed target/72bars; absent structure ATR2/3 stop",
        costs="5bp fee + 5bp adverse slippage each side, doubled in stress; actual historical signed funding except no credit on ambiguous entry/exit bars; missing funding mark uses local candle open proxy",
        caveats=[
            "historical backfill, not received-at-time data",
            "bar-close MTM drawdown and daily HAC stats; no tick-level fills",
            "no liquidation, ADL, exchange outage or delisting model",
            "surviving liquid symbols are a fixed convenience universe, not an unbiased exchange universe",
        ],
        module_sha256=hashlib.sha256(Path("src/gatescope/quant/directional.py").read_bytes()).hexdigest(),
        source_hashes=sources,
        expected_development_trials=12 * 144 + 2 * 72,
        minimum_days=90,
        minimum_trades=30,
        hedged=False,
        live=False,
    )
    plan["artifact_digest"] = report_digest(plan)
    write(target / "plan.json", plan)
    print("FROZEN directional plan", target, plan["artifact_digest"], flush=True)
    all_rows = []
    basket_parts = defaultdict(list)
    clock = time.monotonic()
    for symbol in SYMBOLS:
        for tf in plan["timeframes"]:
            bars, points, book, funding = dataset(symbol, tf)
            lo, hi = boundary(bars, DEVELOP_START), boundary(bars, DEVELOP_END)
            for index, rule in enumerate(rules()):
                result = simulate_directional(
                    bars,
                    points,
                    book[rule.family],
                    rule,
                    lo,
                    hi,
                    funding,
                    capture_mtm=symbol in NEW_SYMBOLS and rule.direction == "LONG",
                )
                score, halves = scored(result)
                all_rows.append(
                    dict(
                        dataset=f"{symbol}-{tf}",
                        symbol=symbol,
                        timeframe=tf,
                        rule=rule.payload(),
                        metrics=result["metrics"],
                        score=score,
                        halves=halves,
                    )
                )
                if symbol in NEW_SYMBOLS and rule.direction == "LONG":
                    result["symbol"] = symbol
                    basket_parts[(tf, rule)].append(result)
                if (index + 1) % 48 == 0:
                    print(
                        "Development",
                        symbol,
                        tf,
                        index + 1,
                        "/144; elapsed",
                        round(time.monotonic() - clock),
                        flush=True,
                    )
            del bars, points, book, funding, result
            gc.collect()
    for (tf, rule), parts in basket_parts.items():
        result = portfolio(parts)
        score, halves = scored(result)
        all_rows.append(
            dict(
                dataset=f"FIXED_LONG_BASKET-{tf}",
                symbol="FIXED_LONG_BASKET",
                timeframe=tf,
                rule=rule.payload(),
                metrics=result["metrics"],
                score=score,
                halves=halves,
            )
        )
    del basket_parts
    assert len(all_rows) == plan["expected_development_trials"]
    write(target / "development.json", all_rows)
    finalists = []
    for family in FAMILIES:
        eligible = [
            r
            for r in all_rows
            if r["rule"]["family"] == family
            and r["symbol"] not in ("BTCUSDT", "ETHUSDT")
            and r["metrics"]["trade_count"] >= 30
            and r["metrics"]["total_return"] > 0
            and r["metrics"]["max_drawdown"] < 0.15
        ]
        eligible.sort(key=lambda r: (r["score"], r["metrics"]["sharpe_ratio"] or -100), reverse=True)
        chosen = set()
        for row in eligible:
            if row["dataset"] in chosen:
                continue
            finalists.append(row)
            chosen.add(row["dataset"])
            if len(chosen) == 3:
                break
    write(target / "frozen-finalists.json", finalists)
    print("Development frozen; finalists", len(finalists), "before withheld evaluation", flush=True)
    # Load each dataset only once during validation; emit detailed reports only
    # for frozen models, including every failure. Nothing re-enters selection.
    results = {i: {**choice, "validation": {}, "full": {}} for i, choice in enumerate(finalists, 1)}
    part_reports = defaultdict(list)
    for symbol in NEW_SYMBOLS:
        for tf in plan["timeframes"]:
            selected = [
                (i, c)
                for i, c in enumerate(finalists, 1)
                if c["timeframe"] == tf and c["symbol"] in (symbol, "FIXED_LONG_BASKET")
            ]
            if not selected:
                continue
            bars, points, book, funding = dataset(symbol, tf)
            for i, c in selected:
                rule = Rule(**c["rule"])
                for segment, left in (("validation", DEVELOP_END), ("full", DEVELOP_START)):
                    # One max-hold embargo follows the development split.
                    lo = boundary(bars, left) + (72 if segment == "validation" else 0)
                    for cost in (1, 2):
                        r = simulate_directional(
                            bars,
                            points,
                            book[rule.family],
                            rule,
                            lo,
                            len(bars),
                            funding,
                            cost=cost,
                            detailed=True,
                            daily_curve=True,
                            capture_mtm=c["symbol"] == "FIXED_LONG_BASKET",
                        )
                        r["symbol"] = symbol
                        r["timeframe"] = tf
                        if c["symbol"] == "FIXED_LONG_BASKET":
                            part_reports[(i, segment, cost)].append(r)
                        else:
                            results[i][segment][str(cost)] = r
                print("Evaluated frozen", i, symbol, tf, rule.family, flush=True)
            del bars, points, book, funding
            gc.collect()
    for (i, segment, cost), parts in part_reports.items():
        results[i][segment][str(cost)] = portfolio(parts)
    summaries = []
    for i, r in results.items():
        r["plan_digest"] = plan["artifact_digest"]
        for cost in ("1", "2"):
            screening = performance_screen(
                r["validation"][cost]["metrics"], full_drawdown=r["full"][cost]["metrics"]["max_drawdown"]
            )
            screening["validation_scope"] = "FROZEN_LATER_PERIOD_RESEARCH"
            r["validation"][cost]["screening"] = screening
        r["artifact_digest"] = report_digest(r)
        write(target / f"finalist-{i:02d}.json", r)
        summaries.append(
            dict(
                id=i,
                dataset=r["dataset"],
                rule=r["rule"],
                development=r["metrics"],
                validation={c: v["metrics"] for c, v in r["validation"].items()},
                screening={c: v["screening"] for c, v in r["validation"].items()},
                artifact_digest=r["artifact_digest"],
            )
        )
        m = r["validation"]["1"]["metrics"]
        print(
            "RESULT",
            i,
            r["dataset"],
            r["rule"],
            "S",
            round(m["sharpe_ratio"] or 0, 3),
            "DD",
            round(m["max_drawdown"], 4),
            "win",
            round(m["win_rate"] or 0, 3),
            "t",
            round(m["t_statistic"] or 0, 3),
            "n",
            m["trade_count"],
            r["validation"]["1"]["screening"]["status"],
            flush=True,
        )
    summary = dict(
        plan=plan,
        development_trials=len(all_rows),
        finalists=summaries,
        total_trial_count=len(all_rows) + 4 * len(finalists),
        qualified=[r["id"] for r in summaries if r["screening"]["1"]["shortlisted"]],
        stress_qualified=[
            r["id"]
            for r in summaries
            if r["screening"]["1"]["shortlisted"] and r["screening"]["2"]["shortlisted"]
        ],
    )
    write(target / "summary.json", summary)
    print(
        "FINISHED",
        target,
        "qualified",
        summary["qualified"],
        "stress-qualified",
        summary["stress_qualified"],
        "elapsed",
        round(time.monotonic() - clock),
        flush=True,
    )


if __name__ == "__main__":
    main()
