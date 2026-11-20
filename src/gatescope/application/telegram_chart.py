"""Deterministic PNG charts from immutable OHLC, never AI-imagined geometry."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo
import math

from PIL import Image, ImageDraw, ImageFont
from gatescope.domain.market import Bar, TIMEFRAME_SECONDS
from gatescope.indicators import IndicatorNode, compute_batch, default_registry

NODES = [
    IndicatorNode("ema20", "ema", {"period": 20}),
    IndicatorNode("ema60", "ema", {"period": 60}),
    IndicatorNode("boll", "boll", {"period": 20}),
    IndicatorNode("rsi", "rsi", {"period": 14}),
    IndicatorNode("macd", "macd", {}),
]


def stamp(ts):
    return datetime.fromtimestamp(ts, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M") + " UTC+8"


def chart_spec(symbol, tf, rows, event, subtitle, *, layout=None):
    if not rows:
        raise ValueError("MARKET_HISTORY_EMPTY")
    layout = layout or ("PATTERN" if event else "INDICATORS")
    if layout not in ("PATTERN", "INDICATORS"):
        raise ValueError("CHART_LAYOUT")
    if layout == "PATTERN":
        # Keep the original anchors and every subsequent candle. Do not shift
        # a long historical pattern into the recent 480-candle tail.
        visible = rows[-120:]
        if event:
            times = [p["time"] for p in event.get("points", []) if p.get("label") not in ("✓", "2?", "E")]
            start = min(times) if times else event.get("start_time", rows[0]["ts"])
            first = next((i for i, r in enumerate(rows) if r["ts"] >= start), 0)
            visible = rows[max(0, min(first - 12, len(rows) - 96)) :]
        if len(visible) > 1200:
            raise ValueError("CHART_EVIDENCE_WINDOW_TOO_LONG")
        return {
            "symbol": symbol,
            "timeframe": tf,
            "bars": visible,
            "event": event,
            "subtitle": subtitle,
            "layout": layout,
            "values": {},
            "indicator_versions": {},
            "summary": f"收盘 {rows[-1]['close']:.8g} · {stamp(rows[-1]['ts'] + TIMEFRAME_SECONDS[tf])}",
        }
    step = TIMEFRAME_SECONDS[tf]
    bars = []
    for r in rows[-600:]:
        t = datetime.fromtimestamp(r["ts"], timezone.utc)
        bars.append(
            Bar(
                venue="GATE",
                market="FUTURES_USDT",
                instrument_id=symbol,
                timeframe=tf,
                open_time=t,
                close_time=t + timedelta(seconds=step),
                available_at=t + timedelta(seconds=step),
                is_final=True,
                revision=1,
                open=Decimal(str(r["open"])), high=Decimal(str(r["high"])), low=Decimal(str(r["low"])), close=Decimal(str(r["close"])), volume=Decimal(str(r["volume"])),
            )
        )
    points = compute_batch(default_registry(), NODES, bars)
    values: dict[int, dict] = {}
    last = {}
    for p in points:
        values.setdefault(int(p.open_time.timestamp()), {})[p.instance_id] = dict(p.values)
        last[p.instance_id] = dict(p.values)

    def num(node, key):
        v = last.get(node, {}).get(key)
        return f"{v:.7g}" if v is not None else "样本不足"

    summary = f"收盘 {float(bars[-1].close):.8g} · {stamp(int(bars[-1].close_time.timestamp()))}\nEMA20 {num('ema20', 'ema')} / EMA60 {num('ema60', 'ema')}\nRSI14 {num('rsi', 'rsi')} · MACD {num('macd', 'macd')} / 信号 {num('macd', 'signal')}"
    # Evidence keeps the complete frozen plotting window, not just the recent tail.
    visible = rows[-480:] if event else rows[-240:]
    return {
        "symbol": symbol,
        "timeframe": tf,
        "layout": layout,
        "bars": visible,
        "values": {str(b["ts"]): values.get(b["ts"], {}) for b in visible},
        "event": event,
        "subtitle": subtitle,
        "summary": summary,
        "indicator_versions": {p.indicator_id: p.indicator_version for p in points},
    }


def _font(size):
    for path in (
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def render_chart(spec):
    rows = spec["bars"]
    step = TIMEFRAME_SECONDS[spec["timeframe"]]
    pattern = spec.get("layout") == "PATTERN"
    if not 1 <= len(rows) <= (1200 if pattern else 600):
        raise ValueError("CHART_BAR_LIMIT")
    if any(b["ts"] >= a["ts"] for b, a in zip(rows, rows[1:], strict=False)):
        raise ValueError("CHART_TIME_ORDER")
    width, height = 1400, 960 if pattern else 1120
    background = "#0c1421"
    grid = "#223044"
    muted = "#91a4be"
    im = Image.new("RGB", (width, height), background)
    d = ImageDraw.Draw(im)
    font = _font(20)
    small = _font(17)
    title = _font(30)
    d.text((45, 22), f"{spec['symbol']}  ·  {spec['timeframe']}  |  GateScope", font=title, fill="#e2eaf5")
    d.text((45, 68), spec["subtitle"][:90], font=font, fill=muted)
    left, right = 50, 1270
    top, bottom = 150, 850 if pattern else 660
    begin, end = rows[0]["ts"] - step, rows[-1]["ts"] + step

    def x(ts):
        return left + (ts - begin) / (end - begin) * (right - left)

    price_values = [r[k] for r in rows for k in ("low", "high")]
    for entry in ({} if pattern else spec["values"]).values():
        for node, keys in (("ema20", ("ema",)), ("ema60", ("ema",)), ("boll", ("upper", "lower"))):
            price_values.extend(
                v for key in keys if (v := entry.get(node, {}).get(key)) is not None and math.isfinite(v)
            )
    low = min(price_values)
    high = max(price_values)
    pad = max((high - low) * 0.08, abs(high) * 0.0005)
    low -= pad
    high += pad

    def y(price):
        return bottom - (price - low) / (high - low) * (bottom - top)

    for i in range(7):
        price = low + (high - low) * i / 6
        py = y(price)
        d.line((left, py, right, py), fill=grid)
        d.text((right + 10, py - 10), f"{price:.7g}", font=small, fill=muted)
    candle_width = max(1, min(9, (x(rows[0]["ts"] + step) - x(rows[0]["ts"])) * 0.65))
    for r in rows:
        px = x(r["ts"])
        color = "#21bc9b" if r["close"] >= r["open"] else "#ed647b"
        d.line((px, y(r["high"]), px, y(r["low"])), fill=color, width=1)
        a, b = sorted((y(r["open"]), y(r["close"])))
        d.rectangle((px - candle_width / 2, a, px + candle_width / 2, max(a + 1, b)), fill=color)
    values: dict[str, dict] = {} if pattern else spec["values"]

    def series(node, key, mapper, color, line_width=2):
        line = []
        for r in rows:
            value = values.get(str(r["ts"]), {}).get(node, {}).get(key)
            if value is not None and math.isfinite(value):
                line.append((x(r["ts"]), mapper(value)))
            else:
                if len(line) > 1:
                    d.line(line, fill=color, width=line_width)
                line = []
        if len(line) > 1:
            d.line(line, fill=color, width=line_width)

    for node, key, color in [
        ("ema20", "ema", "#f7c56b"),
        ("ema60", "ema", "#6fa4ff"),
        ("boll", "upper", "#657993"),
        ("boll", "lower", "#657993"),
    ]:
        series(node, key, y, color)
    d.text(
        (left, 112),
        "K 线 / 形态原理线 / 原始锚点" if pattern else "EMA20  /  EMA60  /  BOLL20 (2σ)",
        font=font,
        fill=muted,
    )
    event = spec.get("event")
    if event:
        # Dedicated clipped drawing layer: never pull a historical anchor to the
        # nearest candle, never shift prices to make an overlay fit the viewport.
        layer = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for line in event.get("lines", []):
            a, b = line.get("from", {}), line.get("to", {})
            if not all(
                isinstance(p.get(k), (int, float)) and math.isfinite(p[k])
                for p in (a, b)
                for k in ("time", "price")
            ):
                continue
            xy = (x(a["time"]) - left, y(a["price"]) - top, x(b["time"]) - left, y(b["price"]) - top)
            if line.get("curve"):
                vertex = line["curve"]["vertex"]
                rim = max((a, b), key=lambda p: abs(p["time"] - vertex["time"]))
                path = []
                if rim["time"] != vertex["time"]:
                    for row in rows:
                        if min(a["time"], b["time"]) <= row["ts"] <= max(a["time"], b["time"]):
                            price = (
                                vertex["price"]
                                + (rim["price"] - vertex["price"])
                                * ((row["ts"] - vertex["time"]) / (rim["time"] - vertex["time"])) ** 2
                            )
                            path.append((x(row["ts"]) - left, y(price) - top))
                if len(path) > 1:
                    ld.line(path, fill="#f1ba56", width=3)
            elif line.get("style") == "dashed":
                distance = math.hypot(xy[2] - xy[0], xy[3] - xy[1])
                pieces = max(1, min(3000, int(distance / 10)))
                for j in range(0, pieces, 2):
                    ld.line(
                        tuple(
                            xy[k] + (xy[k + 2] - xy[k]) * fraction
                            for fraction in (j / pieces, min(1, (j + 1) / pieces))
                            for k in (0, 1)
                        ),
                        fill="#f1ba56",
                        width=3,
                    )
            else:
                ld.line(xy, fill="#f1ba56", width=3)
        for p in event.get("points", []):
            if not begin <= p["time"] <= end:
                continue
            px, py = x(p["time"]) - left, y(p["price"]) - top
            ld.ellipse((px - 4, py - 4, px + 4, py + 4), fill="#f1ba56")
            ld.text((px + 5, py - 25), str(p.get("label", ""))[:12], font=small, fill="#f1ba56")
        im.paste(layer, (left, top), layer)
        d.text((720, 112), event.get("name", "")[:28], font=font, fill="#f1ba56")
    if pattern:
        for index in range(5):
            r = rows[round((len(rows) - 1) * index / 4)]
            label = datetime.fromtimestamp(r["ts"], ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
            d.text((min(right - 110, max(left, x(r["ts"]) - 50)), 873), label, font=small, fill=muted)
        d.text(
            (left, 920),
            spec.get("footnote", "UTC+8 · 已收盘 K 线 · 金色：形态原理线与锚点 · 首次预测保留，不随结果改画"),
            font=small,
            fill=muted,
        )
        result = BytesIO()
        im.save(result, format="PNG", optimize=False)
        return result.getvalue()
    volume_top, volume_bottom = 695, 785
    maxvol = max(max(r["volume"] for r in rows), 1e-12)
    d.text((left, 670), "成交量", font=small, fill=muted)
    for r in rows:
        px = x(r["ts"])
        v = volume_bottom - r["volume"] / maxvol * (volume_bottom - volume_top)
        d.line(
            (px, volume_bottom, px, v),
            fill="#23846e" if r["close"] >= r["open"] else "#925566",
            width=max(1, int(candle_width)),
        )
    d.text((left, 797), "RSI14", font=small, fill=muted)
    def ry(v):
        return 905 - v / 100 * 80
    for v in (30, 70):
        d.line((left, ry(v), right, ry(v)), fill=grid)
        d.text((right + 10, ry(v) - 10), str(v), font=small, fill=muted)
    series("rsi", "rsi", ry, "#b28deb")
    d.text((left, 919), "MACD (12,26,9)", font=small, fill=muted)
    macd = [abs(v) for entry in values.values() for v in entry.get("macd", {}).values() if v is not None]
    extent = max(max(macd, default=0), 1e-12)
    def my(v):
        return 993 - v / extent * 42
    d.line((left, 993, right, 993), fill=grid)
    for r in rows:
        v = values.get(str(r["ts"]), {}).get("macd", {}).get("histogram")
        if v is not None:
            d.line(
                (x(r["ts"]), 993, x(r["ts"]), my(v)),
                fill="#21bc9b" if v >= 0 else "#ed647b",
                width=max(1, int(candle_width)),
            )
    series("macd", "macd", my, "#6fa4ff")
    series("macd", "signal", my, "#f7c56b")
    for index in range(5):
        r = rows[round((len(rows) - 1) * index / 4)]
        label = datetime.fromtimestamp(r["ts"], ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
        d.text((min(right - 110, max(left, x(r["ts"]) - 50)), 1049), label, font=small, fill=muted)
    d.text((left, 1083), "UTC+8 · 已收盘数据 · 原理线不代表未来行情 · 仅研究参考", font=small, fill=muted)
    result = BytesIO()
    im.save(result, format="PNG", optimize=False)
    return result.getvalue()
