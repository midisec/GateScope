"""Original PAPER decision charts from the stored snapshot, never live candles."""
from datetime import datetime
from math import isfinite

from gatescope.application.paper_execution import PaperExecutionError
from gatescope.application.telegram_chart import render_chart
from gatescope.domain.market import TIMEFRAME_SECONDS


def paper_decision_chart_spec(order, *, pattern_index=0):
    snapshot = order.get("decision_snapshot")
    if not isinstance(snapshot, dict):
        raise PaperExecutionError("PAPER_DECISION_UNASSOCIATED", "这笔订单未关联原始决策快照，不使用当前行情冒充开单原图", 404)
    evidence = snapshot["evidence_json"]
    source = evidence["server_evidence"]
    candles = source["candles"]
    if not candles:
        raise PaperExecutionError("PAPER_DECISION_CANDLES_UNAVAILABLE", "当时未取得可冻结的 K 线；分析摘要仍保留，不补造历史图", 409)
    if not 1 <= len(candles) <= 256 or evidence["timeframe"] not in TIMEFRAME_SECONDS:
        raise ValueError("invalid frozen chart dimensions")
    observed = datetime.fromisoformat(evidence["observed_at"])
    rows = []
    for candle in candles:
        if not candle.get("is_final") or datetime.fromisoformat(candle["close_time"]) > observed:
            raise ValueError("invalid frozen chart availability")
        row = {"ts": int(datetime.fromisoformat(candle["open_time"]).timestamp()),
               **{key: float(candle[key]) for key in ("open", "high", "low", "close", "volume")}}
        if any(not isfinite(row[key]) for key in ("open", "high", "low", "close", "volume")):
            raise ValueError("invalid frozen chart value")
        rows.append(row)
    events = source.get("pattern_events", [])
    if isinstance(pattern_index, bool) or not isinstance(pattern_index, int) or not 0 <= pattern_index < max(1, len(events)):
        raise PaperExecutionError("PAPER_DECISION_PATTERN_NOT_FOUND", "快照中没有这个形态，请选择已保存的原始形态", 404)
    event = events[pattern_index]["record"].get("latest") if events else None
    warnings = []
    if source.get("gaps"):
        warnings.append("原始 K 线存在缺口，未补造缺失行情")
    if event:
        if any(point["time"] < rows[0]["ts"] for point in event.get("points", []) if point.get("label") not in ("✓", "2?", "E")):
            warnings.append("部分原始锚点早于冻结窗口，仅显示可见区间")
    else:
        warnings.append("此快照未关联归档形态，不事后识别或补画")
    return {"symbol": evidence["symbol"], "timeframe": evidence["timeframe"], "bars": rows,
        "event": event, "layout": "PATTERN", "values": {}, "indicator_versions": {},
        "subtitle": f"开单分析原图 · {evidence['observed_at']}",
        "summary": "；".join(warnings), "pattern_count": len(events), "pattern_index": pattern_index,
        "decision_snapshot_id": snapshot["id"], "evidence_digest": snapshot["evidence_digest"],
        "footnote": "UTC+8 · 冻结的已收盘 K 线与当时归档线 · 不读取最新行情，不按盈亏重画"}


def render_paper_decision_chart(order, *, pattern_index=0):
    """Return deterministic PNG bytes and public metadata, or a safe error."""
    chart = paper_decision_chart_spec(order, pattern_index=pattern_index)
    return render_chart(chart), {key: chart[key] for key in (
        "symbol", "timeframe", "summary", "pattern_count", "pattern_index", "decision_snapshot_id", "evidence_digest")}
