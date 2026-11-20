"""Causal, unlevered OHLC research simulator. No execution or certification port."""

from __future__ import annotations

import math
import time
from collections import deque
from datetime import datetime
from itertools import islice
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gatescope.domain.market import Bar, TIMEFRAME_SECONDS
from gatescope.indicators import IndicatorNode, default_registry
from gatescope.indicators.engine import IndicatorStream
from gatescope.quant.reporting import drawdown_analysis, trade_dimensions, win_probability
from gatescope.quant.performance import daily_performance, performance_screen
from gatescope.quant.artifacts import DIGEST_METHOD, report_digest
from gatescope.quant.research_rules import PatternReplay, combine_rules, rule_catalog, PATTERN_WINDOW, PIVOT_ORDER
from gatescope.quant.validation import CircularBlockBootstrapPolicy, circular_block_bootstrap_mean

ENGINE_VERSION = "research-ohlc-1.5.0"
MAX_INDICATOR_BARS = 550000
CATALOG = [
    {"id": "pullback", "name": "趋势回踩再入", "rule": "ADX 达标、快慢线同向；前收盘在前快线下、本收盘重新站上快线且 RSI 45–65 做多；反向 RSI 35–55 做空。", "indicators": ["EMA", "ADX", "RSI", "ATR"]},
    {"id": "volume_breakout", "name": "放量通道突破", "rule": "收盘突破此前 N 根通道，成交量超过此前 N 根均量 1.5 倍，ADX 达标且 EMA 同向；反向做空。", "indicators": ["EMA", "Donchian", "VOLUME", "ADX", "ATR"]},
    {"id": "momentum", "name": "波动过滤动量", "rule": "N 根收盘变化超过 2 ATR，快慢线、慢线斜率及 ADX 同向确认；反向做空。", "indicators": ["EMA", "MOMENTUM", "ADX", "ATR"]},
    {"id": "trend", "name": "趋势跟随", "rule": "EMA 快线高于慢线、ADX 达标且 RSI 处于 50–70 时做多；反向条件做空。", "indicators": ["EMA", "ADX", "RSI", "ATR"]},
    {"id": "breakout", "name": "通道突破", "rule": "收盘突破此前 N 根高点且 EMA 向上时做多；跌破此前低点且 EMA 向下时做空。", "indicators": ["EMA", "Donchian", "ATR"]},
    {"id": "reversion", "name": "震荡回归", "rule": "ADX 较低时，收盘低于布林下轨且 RSI < 30 做多；高于上轨且 RSI > 70 做空。", "indicators": ["BOLL", "RSI", "ADX", "ATR"]},
]


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    engine_mode: Literal["INDICATOR", "STRUCTURE"] = "INDICATOR"
    structure_patterns: list[str] = Field(default_factory=list, max_length=28)
    structure_entry_phase: Literal["FORMED", "CONFIRMED"] = "CONFIRMED"
    entry_delay_bars: int = Field(default=0, ge=0, le=10)
    walk_forward_folds: int = Field(default=4, ge=2, le=8)
    strategy: Literal["trend", "breakout", "reversion", "pullback", "volume_breakout", "momentum", "composite"] = "trend"
    rule_ids: list[str] = Field(default_factory=list, max_length=26)
    match_mode: Literal["ANY", "ALL"] = "ANY"
    regimes: list[Literal["UP", "DOWN", "RANGE"]] = Field(default_factory=list, max_length=3)
    pattern_confidence: float = Field(default=0.65, ge=0, le=1)
    coincidence_bars: int = Field(default=3, ge=1, le=20)
    availability_mode: Literal["AS_RECORDED", "HISTORICAL_CLOSE"] = "AS_RECORDED"
    direction: Literal["LONG", "SHORT", "BOTH"] = "LONG"
    fast_period: int = Field(default=20, ge=3, le=100)
    slow_period: int = Field(default=50, ge=10, le=200)
    lookback: int = Field(default=20, ge=5, le=100)
    adx_threshold: float = Field(default=20, ge=5, le=50)
    stop_atr: float = Field(default=2, ge=0.5, le=6)
    reward_risk: float = Field(default=2, ge=0.5, le=5)
    max_holding_bars: int = Field(default=48, ge=1, le=200)
    initial_equity: float = Field(default=10000, ge=100, le=1e8)
    risk_fraction: float = Field(default=0.01, ge=0.001, le=0.03)
    fee_bps: float = Field(default=5, ge=0, le=100)
    slippage_bps: float = Field(default=5, ge=0, le=100)
    funding_bps_per_8h: float = Field(default=1, ge=0, le=100)

    @model_validator(mode="after")
    def periods(self) -> "StrategySpec":
        from gatescope.quant.pattern_study import catalog as structure_catalog
        if len(set(self.structure_patterns)) != len(self.structure_patterns) or any(
            item not in {row["id"] for row in structure_catalog()} for item in self.structure_patterns
        ):
            raise ValueError("请选择受支持且不重复的形态或波浪")
        if self.fast_period >= self.slow_period:
            raise ValueError("快线周期必须小于慢线周期")
        if len(set(self.regimes)) != len(self.regimes) or len(set(self.rule_ids)) != len(self.rule_ids):
            raise ValueError("规则与趋势筛选不能重复")
        if self.strategy == "composite":
            catalog = {r["id"]: r for r in rule_catalog()}
            allowed = set(catalog)
            if not self.rule_ids or any(r not in allowed for r in self.rule_ids):
                raise ValueError("请选择至少一项受支持的具体策略或形态")
            if self.match_mode == "ALL":
                directions = {catalog[r]["direction"] for r in self.rule_ids} - {"neutral"}
                if len(directions) > 1 or {"trend", "reversion"}.issubset(self.rule_ids):
                    raise ValueError("所选规则方向或趋势条件互斥，请改用任一满足")
        elif self.rule_ids:
            raise ValueError("多项规则请使用组合策略模式")
        return self


def available_time(bar: Bar, spec: StrategySpec) -> datetime:
    # Research assumption only. Never alter the source Bar or its recorded availability.
    return bar.close_time if spec.availability_mode == "HISTORICAL_CLOSE" else max(bar.close_time, bar.available_at)


def features(bars: Sequence[Bar], spec: StrategySpec, *, progress: Callable[[int, int], None] | None = None,
             output: Any = None) -> Any:
    stream = IndicatorStream(default_registry(), [
        IndicatorNode("fast", "ema", {"period": spec.fast_period}),
        IndicatorNode("slow", "ema", {"period": spec.slow_period}),
        IndicatorNode("rsi", "rsi", {"period": 14}),
        IndicatorNode("adx", "adx", {"period": 14}),
        IndicatorNode("atr", "atr", {"period": 14}),
        IndicatorNode("boll", "boll", {"period": spec.lookback, "deviations": 2}),
    ])
    result = [] if output is None else output
    latest_available: datetime | None = None
    selected: list[str] = spec.rule_ids if spec.strategy == "composite" else [spec.strategy]
    patterns = [key.removeprefix("pattern:") for key in selected if key.startswith("pattern:")]
    replay = PatternReplay(patterns, spec.pattern_confidence) if patterns else None
    recent: list[dict[str, Any]] = []
    # Only prior bars belong in channel/volume/momentum calculations. Keeping
    # this bounded prefix also avoids reloading a previous compressed chunk on
    # every bar while a lookback window straddles a spill-storage boundary.
    prior: deque[Bar] = deque(maxlen=spec.lookback)
    started, last_progress = time.monotonic(), 0.0
    for index, bar in enumerate(bars):
        now = time.monotonic()
        if replay and now - started > 120:
            raise ValueError("形态回测超过 120 秒计算预算，请减少形态或样本数量")
        if progress and now - last_progress > 5:
            progress(index, len(bars))
            last_progress = now
        values: dict[str, Any] = {}
        for point in stream.update(bar):
            for key, value in point.values.items():
                values[f"{point.instance_id}.{key}"] = value
        values["channel.high"] = max((float(b.high) for b in prior), default=None)
        values["channel.low"] = min((float(b.low) for b in prior), default=None)
        ready = index >= max(spec.slow_period, spec.lookback, 28) and all(v is not None for v in values.values())
        matched: list[str] = []
        side, conflict, regime = 0, False, "UNKNOWN"
        latest_available = max(available_time(bar, spec), latest_available or available_time(bar, spec))
        new_patterns = replay.append(bar, index, latest_available.isoformat()) if replay else []
        recent = [event for event in recent if index - event["observed_index"] < spec.coincidence_bars] + new_patterns
        used_patterns: list[dict] = []
        if ready:
            fast, slow, adx, rsi = (values[k] for k in ("fast.ema", "slow.ema", "adx.adx", "rsi.rsi"))
            close = float(bar.close)
            regime = "RANGE" if adx < spec.adx_threshold else "UP" if fast > slow else "DOWN"
            votes = {
                "trend": (1 if fast > slow and 50 <= rsi <= 70 else -1 if fast < slow and 30 <= rsi <= 50 else 0) if adx >= spec.adx_threshold else 0,
                "breakout": 1 if fast > slow and close > values["channel.high"] else -1 if fast < slow and close < values["channel.low"] else 0,
                "reversion": (1 if close < values["boll.lower"] and rsi < 30 else -1 if close > values["boll.upper"] and rsi > 70 else 0) if adx < spec.adx_threshold else 0,
            }
            previous = result[index - 1]["values"]
            previous_close = float(prior[-1].close)
            previous_fast, previous_slow = previous.get("fast.ema"), previous.get("slow.ema")
            values["volume.prior_mean"] = sum(float(b.volume) for b in prior) / len(prior)
            values["momentum.change"] = close - float(prior[0].close)
            votes["pullback"] = (1 if fast > slow and previous_fast is not None and previous_close <= previous_fast and close > fast and 45 <= rsi <= 65 else
                -1 if fast < slow and previous_fast is not None and previous_close >= previous_fast and close < fast and 35 <= rsi <= 55 else 0) if adx >= spec.adx_threshold else 0
            votes["volume_breakout"] = votes["breakout"] if adx >= spec.adx_threshold and values["volume.prior_mean"] > 0 and float(bar.volume) > 1.5 * values["volume.prior_mean"] else 0
            votes["momentum"] = (1 if fast > slow and previous_slow is not None and slow > previous_slow and values["momentum.change"] > 2 * values["atr.atr"] else
                -1 if fast < slow and previous_slow is not None and slow < previous_slow and values["momentum.change"] < -2 * values["atr.atr"] else 0) if adx >= spec.adx_threshold else 0
            pattern_conflict = False
            for name in patterns:
                sides = {1 if e.get("signal_direction", e.get("direction")) == "bullish" else -1 if e.get("signal_direction", e.get("direction")) == "bearish" else 0 for e in recent if e["pattern"] == name} - {0}
                pattern_conflict |= len(sides) > 1
                votes[f"pattern:{name}"] = next(iter(sides)) if len(sides) == 1 else 0
            side, matched, conflict = combine_rules(votes, selected, spec.match_mode, spec.direction)
            conflict |= pattern_conflict
            if conflict or (spec.regimes and regime not in spec.regimes):
                side, matched = 0, []
            # A held confirmation cannot fire again after the first signal consumes it.
            used_patterns = [e for e in recent if f"pattern:{e['pattern']}" in matched and (1 if e.get("signal_direction", e.get("direction")) == "bullish" else -1) == side]
            if side and used_patterns:
                consumed = {e["evidence_id"] for e in used_patterns}
                recent = [e for e in recent if e["evidence_id"] not in consumed]
        result.append({"time": bar.open_time.isoformat(), "available_at": latest_available.isoformat(), "ready": ready,
            "side": side, "values": values, "regime": regime, "matched_rules": matched, "direction_conflict": conflict,
            "pattern_refs": [e["evidence_id"] for e in used_patterns], "pattern_confirmations": new_patterns})
        prior.append(bar)
    if progress:
        progress(len(bars), len(bars))
    return result


def simulate(bars: Sequence[Bar], signals: Sequence[dict[str, Any]], spec: StrategySpec, start: int, end: int,
             *, sequence_factory: Callable[[str], Any] | None = None, name: str = "full",
             progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    cash = spec.initial_equity
    trades = sequence_factory(f"{name}_trades") if sequence_factory else []
    curve = sequence_factory(f"{name}_curve") if sequence_factory else []
    curve.append({"time": bars[start].open_time.isoformat(), "bar_open_time": bars[max(0, start - 1)].open_time.isoformat(), "equity": cash, "initial": True})
    position: dict[str, Any] | None = None
    pending: int | None = None
    fee, slip = spec.fee_bps / 10000, spec.slippage_bps / 10000
    diagnostics = {"signal_bars": 0, "expired_signals": 0, "risk_rejections": 0, "entries": 0}
    for index in range(start, end):
        if progress and index % 1024 == 0:
            progress(index, len(bars))
        diagnostics["signal_bars"] += int(bool(signals[index]["side"]))
        bar = bars[index]
        if pending is not None and index - pending > 2:
            diagnostics["expired_signals"] += 1
            pending = None
        if position is None and pending is not None and index < end - 1:
            source = bars[pending]
            available = max(available_time(source, spec),
                datetime.fromisoformat(signals[pending].get("available_at", available_time(source, spec).isoformat())))
            if available <= bar.open_time:
                signal = signals[pending]
                side = signal["side"]
                entry = float(bar.open) * (1 + side * slip)
                distance = float(signal["values"]["atr.atr"]) * spec.stop_atr
                if cash > 0 and 0 < distance < entry / max(1, spec.reward_risk):
                    quantity = min(cash * spec.risk_fraction / distance, cash / (entry * (1 + fee)))
                    position = {"side": side, "entry_time": bar.open_time.isoformat(), "signal_time": source.close_time.isoformat(), "signal_bar_open_time": source.open_time.isoformat(), "signal_available_at": available.isoformat(), "entry": entry, "quantity": quantity, "stop": entry - side * distance, "target": entry + side * distance * spec.reward_risk, "entry_index": index, "entry_fee": quantity * entry * fee, "indicators": signal["values"], "signal_bar_hash": source.payload_hash}
                    cash -= position["entry_fee"]
                    position.update(matched_rules=list(signal.get("matched_rules", [])), pattern_refs=list(signal.get("pattern_refs", [])), regime=signal.get("regime", "UNKNOWN"))
                    diagnostics["entries"] += 1
                else:
                    diagnostics["risk_rejections"] += 1
                pending = None
        if position is not None:
            p = position
            side, stop, target = p["side"], p["stop"], p["target"]
            opened, high, low = float(bar.open), float(bar.high), float(bar.low)
            stop_hit = low <= stop if side == 1 else high >= stop
            target_hit = high >= target if side == 1 else low <= target
            price, reason = None, None
            # Opening gaps precede intrabar ambiguity; ambiguous OHLC uses stop first.
            if (side == 1 and opened <= stop) or (side == -1 and opened >= stop):
                price, reason = opened, "STOP_GAP"
            elif (side == 1 and opened >= target) or (side == -1 and opened <= target):
                price, reason = target, "TARGET"
            elif stop_hit:
                price, reason = stop, "STOP"
            elif target_hit:
                price, reason = target, "TARGET"
            elif index - p["entry_index"] + 1 >= spec.max_holding_bars:
                price, reason = float(bar.close), "TIME_EXIT"
            elif index == end - 1:
                price, reason = float(bar.close), "SEGMENT_END"
            held_hours = (bar.close_time - datetime.fromisoformat(p["entry_time"])).total_seconds() / 3600
            funding = p["quantity"] * p["entry"] * (0 if bar.market == "SPOT" else spec.funding_bps_per_8h) / 10000 * held_hours / 8
            if price is not None:
                exit_price = price * (1 - side * slip)
                gross = side * p["quantity"] * (exit_price - p["entry"])
                exit_fee = p["quantity"] * exit_price * fee
                net = gross - p["entry_fee"] - exit_fee - funding
                cash += gross - exit_fee - funding
                trades.append({**{k: v for k, v in p.items() if k != "entry_index"}, "exit_time": bar.close_time.isoformat(), "exit_bar_open_time": bar.open_time.isoformat(), "exit_time_basis": "INTRABAR_OHLC" if reason in ("STOP", "STOP_GAP", "TARGET") else "BAR_CLOSE", "holding_bars": index - p["entry_index"] + 1, "holding_seconds": held_hours * 3600, "exit_price": exit_price, "exit_reason": reason, "net_pnl": net, "net_return": net / (p["quantity"] * p["entry"]), "fees": p["entry_fee"] + exit_fee, "funding": funding})
                position = None
                equity = cash
            else:
                equity = cash + side * p["quantity"] * (float(bar.close) - p["entry"]) - funding - p["quantity"] * float(bar.close) * fee
        else:
            equity = cash
        curve.append({"time": bar.close_time.isoformat(), "bar_open_time": bar.open_time.isoformat(), "equity": equity})
        # Signals are formed after this candle closes; pending signals never use its open.
        if position is None and signals[index]["side"] and pending is None:
            pending = index
    diagnostics["pending_at_end"] = int(pending is not None)
    return {"metrics": {**metrics(trades, curve, spec.initial_equity), **daily_performance(curve)}, "trades": trades, "equity_curve": curve, "diagnostics": diagnostics}


def metrics(trades: list[dict[str, Any]], curve: list[dict[str, Any]], initial: float) -> dict[str, Any]:
    profits = [t["net_pnl"] for t in trades]
    gains, losses = sum(max(p, 0) for p in profits), -sum(min(p, 0) for p in profits)
    peak, drawdown = initial, 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        drawdown = max(drawdown, (peak - point["equity"]) / peak)
    return {"trade_count": len(trades), "win_rate": sum(p > 0 for p in profits) / len(profits) if profits else None, "net_pnl": curve[-1]["equity"] - initial, "total_return": curve[-1]["equity"] / initial - 1, "max_drawdown": drawdown, "profit_factor": gains / losses if losses else None, "mean_trade_return": sum(t["net_return"] for t in trades) / len(trades) if trades else None, "fees": sum(t["fees"] for t in trades), "funding": sum(t["funding"] for t in trades)}


def run_backtest(bars: Sequence[Bar], spec: StrategySpec, *, tested_hypotheses: int = 1,
                 source_manifest: Sequence[dict[str, Any]] | None = None,
                 progress: Callable[[int, int], None] | None = None,
                 sequence_factory: Callable[[str], Any] | None = None) -> dict[str, Any]:
    compact = sequence_factory is not None
    if not 300 <= len(bars) <= MAX_INDICATOR_BARS:
        raise ValueError("需要 300–550000 根已收盘 K 线")
    if len(bars) > 5000 and any(rule.startswith("pattern:") for rule in spec.rule_ids):
        raise ValueError("包含形态扫描的组合最多 5000 根；年度形态请使用同源形态回测")
    if len(bars) > 5000 and not compact:
        raise ValueError("长历史回测需要分片存储，不能一次加载完整报告")
    step = TIMEFRAME_SECONDS[bars[0].timeframe]
    for previous, current in zip(bars, islice(bars, 1, None), strict=False):
        if previous.instrument_id != current.instrument_id or previous.timeframe != current.timeframe or (current.open_time - previous.open_time).total_seconds() != step:
            raise ValueError("所选历史 K 线不连续，请缩短区间或补齐行情")
    signals = features(bars, spec, progress=progress,
        output=sequence_factory("signals") if sequence_factory else None)
    warmup = max(spec.slow_period, spec.lookback, 28) + 2
    split = int(len(bars) * .7)
    validation_start = split + spec.max_holding_bars
    if split <= warmup or len(bars) - validation_start < 50:
        raise ValueError("样本不足以分割验证区间，请增加 K 线数量或减少持仓期限")
    training = simulate(bars, signals, spec, warmup, split, sequence_factory=sequence_factory, name="training", progress=progress)
    validation = simulate(bars, signals, spec, validation_start, len(bars), sequence_factory=sequence_factory, name="validation", progress=progress)
    full = simulate(bars, signals, spec, warmup, len(bars), sequence_factory=sequence_factory, progress=progress)
    returns = [t["net_return"] for t in validation["trades"]]
    bound = None
    alpha = .05 / max(1, tested_hypotheses)
    if 20 <= len(returns) <= 20000 and tested_hypotheses <= 50:
        # Diagnostic only: trades may be dependent and the split is user-visible.
        bound = circular_block_bootstrap_mean(returns, CircularBlockBootstrapPolicy("research-trades-cbb-v1", max(2, math.ceil(len(returns) ** (1 / 3))), resamples=1000, alpha=alpha, seed=1729))
    flags = sorted({flag for bar in bars for flag in bar.quality_flags})
    if spec.availability_mode == "HISTORICAL_CLOSE":
        flags = sorted(set(flags) | {"HISTORICAL_AVAILABILITY_ASSUMED"})
    reasons = ["未封存的时间切分验证", "手续费、滑点及资金费率为用户设定的成本模型", "历史快照不证明未来收益"]
    if len(returns) < 20:
        reasons.append("验证交易少于 20 笔，无法估计收益下界")
    if flags:
        reasons.append("行情包含质量标记，详见数据记录")
    if spec.availability_mode == "HISTORICAL_CLOSE":
        reasons.append("历史收盘重建：假设收盘数据当时可用，使用当前保存的行情版本；非真实接收时间回放，不可用于实盘资格认证")
    if tested_hypotheses > 50:
        reasons.append("累计尝试超过当前抽样分辨率，不输出统计下界")
    if len(returns) > 20000:
        reasons.append("验证交易超过自助抽样计算预算，保留全部交易与精确指标，不输出抽样收益下界")
    result = {"engine_version": ENGINE_VERSION, "spec": spec.model_dump(), "bar_count": len(bars), "range": {"from": bars[0].open_time.isoformat(), "to": bars[-1].close_time.isoformat(), "validation_from": bars[validation_start].open_time.isoformat(), "embargo_bars": spec.max_holding_bars}, "full": full, "training": training["metrics"], "validation": validation["metrics"], "latest_signal": signals[-1], "evidence": {"decision": "RESEARCH_ONLY", "bootstrap_mean_lcb": bound.mean_lcb if bound else None, "confidence_level": .95, "resamples": 1000 if bound else 0, "positive_validation_evidence": bool(bound and bound.mean_lcb > 0), "sealed_holdout": False, "reasons": reasons}, "quality_flags": flags,
        "data_manifest": [] if compact else [{"open_time": b.open_time.isoformat(), "revision": b.revision, "hash": b.payload_hash} for b in bars],
        "candles": [] if compact else [{"time": b.open_time.isoformat(), "open": float(b.open), "high": float(b.high), "low": float(b.low), "close": float(b.close)} for b in bars]}
    result["evidence"].update({"tested_hypotheses": tested_hypotheses, "alpha_per_attempt": alpha,
        "confidence_level": 1 - alpha, "resamples": 1000 if bound else 0})
    wins = [r for r in returns if r > 0]
    losses = [-r for r in returns if r <= 0]
    average_win = sum(wins) / len(wins) if wins else None
    average_loss = sum(losses) / len(losses) if losses else None
    result["evidence"]["payoff"] = {"average_net_win": average_win, "average_net_loss": average_loss,
        "empirical_break_even_win_rate": average_loss / (average_loss + average_win) if average_win and average_loss else None,
        "formula": "E[R] = p * E[R|R>0] - (1-p) * E[-R|R<=0]"}
    # Preserve every field needed to reconstruct the input, even after vendor revisions.
    result["input_bars"] = [] if compact else [{"venue": b.venue, "market": b.market, "instrument_id": b.instrument_id,
        "timeframe": b.timeframe, "open_time": b.open_time.isoformat(), "close_time": b.close_time.isoformat(),
        "open": str(b.open), "high": str(b.high), "low": str(b.low), "close": str(b.close), "volume": str(b.volume),
        "is_final": b.is_final, "revision": b.revision, "available_at": b.available_at.isoformat(),
        "source_sequence": b.source_sequence, "quality_flags": sorted(b.quality_flags), "payload_hash": b.payload_hash} for b in bars]
    result["drawdown"] = drawdown_analysis(full["equity_curve"],
        output=sequence_factory("drawdown") if sequence_factory else None)
    result["dimensions"] = trade_dimensions(full["trades"], (bars[-1].close_time - bars[warmup].open_time).total_seconds())
    result["probability"] = {"full": win_probability(full["trades"], tested_hypotheses),
        "validation": win_probability(validation["trades"], tested_hypotheses)}
    result["time_convention"] = "入场使用信号可用后的开盘；OHLC 内触发退出无法确定秒级时刻，按所在 K 线终结记账；持仓时长为该记账口径"
    result["source_manifest"] = [] if compact else list(source_manifest) if source_manifest is not None else result["data_manifest"]
    registry = default_registry()
    result["indicator_versions"] = {name: registry.definition(name).version for name in ("ema", "rsi", "adx", "atr", "boll")}
    result["indicator_series"] = [] if compact else [{k: v for k, v in signal.items() if k != "pattern_confirmations"} for signal in signals]
    selected_ids = spec.rule_ids if spec.strategy == "composite" else [spec.strategy]
    result["selection"] = {"rules": [r for r in rule_catalog() if r["id"] in selected_ids], "match_mode": spec.match_mode,
        "regimes": spec.regimes, "coincidence_bars": spec.coincidence_bars, "pattern_confidence": spec.pattern_confidence,
        "pattern_window_bars": PATTERN_WINDOW, "pivot_order": PIVOT_ORDER, "conflict_policy": "ABSTAIN"}
    result["pattern_events"] = [] if compact else [e for signal in signals for e in signal["pattern_confirmations"]]
    result["availability"] = {"mode": spec.availability_mode,
        "delayed_bars": sum((b.available_at - b.close_time).total_seconds() >= step for b in bars),
        "assumed": spec.availability_mode == "HISTORICAL_CLOSE"}
    attribution = {r["id"]: {**r, "trade_count": 0, "wins": 0, "net_pnl": 0.0} for r in result["selection"]["rules"]}
    for trade in full["trades"]:
        for key in trade["matched_rules"]:
            row = attribution[key]
            row["trade_count"] += 1
            row["wins"] += int(trade["net_pnl"] > 0)
            row["net_pnl"] += trade["net_pnl"]
    result["rule_attribution"] = [{key: value for key, value in row.items() if key != "wins"} |
        {"win_rate": row["wins"] / row["trade_count"] if row["trade_count"] else None} for row in attribution.values()]
    result["screening"] = performance_screen(result["validation"], full_drawdown=full["metrics"]["max_drawdown"])
    result["validation_equity_curve"] = [] if compact else validation["equity_curve"]
    if compact:
        # All exact rows stay in compressed, paged artifacts. Summary JSON never
        # repeats a full year of OHLC, feature values or mark-to-market points.
        result["detail_storage"] = {"version": "indicator-chunks-1", "chunk_rows": 512,
            "counts": {"inputs": len(bars), "signals": len(signals), "trades": len(full["trades"]),
                "curve": len(full["equity_curve"]), "drawdown": len(result["drawdown"]["series"])},
            "warmup_bars": warmup, "summary_only": True}
        result["full"] = {**full, "trades": [], "equity_curve": []}
        result["drawdown"] = {**result["drawdown"], "series": []}
    result["artifact_digest_method"] = DIGEST_METHOD
    result["artifact_digest"] = report_digest(result)
    return result
