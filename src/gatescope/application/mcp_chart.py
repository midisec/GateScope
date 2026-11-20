"""Bounded, deterministic research PNGs; no network, database or file output.

The caller supplies indicator evidence computed from closed candles. Exact UTC
open-time keys align that evidence to candle centres; missing data is never
interpolated and forming candles never receive indicator overlays.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any
import unicodedata

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

from gatescope.domain.market import TIMEFRAME_SECONDS

WIDTH, HEIGHT = 1200, 900
MAX_PIXELS = 1_080_000
MAX_BARS = 300
LEFT, RIGHT = 44, 1064
PRICE_TOP, PRICE_BOTTOM = 186, 522
VOLUME_TOP, VOLUME_BOTTOM = 567, 661
RSI_TOP, RSI_BOTTOM = 706, 812
BACKGROUND = "#0b1220"
PANEL = "#101b2b"
GRID = "#233248"
MUTED = "#91a4bb"
TEXT = "#e2eaf5"
UP, DOWN = "#2bc5a0", "#f06f85"
FORMING = "#ccd6e5"
EMA9, EMA21, RSI = "#ffc966", "#71adff", "#ba9afb"


@dataclass(frozen=True)
class _Candle:
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_final: bool


def _error(reason: str) -> None:
    raise ValueError(f"MCP_CHART_{reason}")


def _text(value: Any, maximum: int = 120) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or any(unicodedata.category(character).startswith("C") for character in value)):
        _error("TEXT_INVALID")
    return value


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        _error("TIME_INVALID")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _error("TIME_INVALID")
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        _error("TIME_NOT_UTC")
    return result


def _number(value: Any, *, zero_allowed: bool = False) -> float:
    if (isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal))
            or len(str(value)) > 128):
        _error("NUMBER_INVALID")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        _error("NUMBER_INVALID")
    if (not number.is_finite() or number < 0 or number > Decimal("1e100")
            or (number != 0 and number < Decimal("1e-100"))
            or (not zero_allowed and number == 0)):
        _error("NUMBER_RANGE")
    result = float(number)
    if number != 0 and result == 0:
        _error("NUMBER_RANGE")
    return result


def _validate(snapshot: dict) -> tuple[list[_Candle], dict[str, dict[datetime, float | None]], dict[str, Any]]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("instrument"), dict):
        _error("SNAPSHOT_INVALID")
    timeframe = snapshot.get("timeframe")
    if not isinstance(timeframe, str) or timeframe not in TIMEFRAME_SECONDS:
        _error("TIMEFRAME_INVALID")
    step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    captured = _utc(snapshot.get("captured_at"))
    source = snapshot.get("candles")
    if not isinstance(source, list) or not 1 <= len(source) <= MAX_BARS:
        _error("BAR_LIMIT")
    rows = []
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            _error("CANDLE_INVALID")
        opened, closed = _utc(item.get("open_time")), _utc(item.get("close_time"))
        final = item.get("is_final")
        if (not isinstance(final, bool) or closed - opened != step or opened > captured
                or (final and closed > captured) or (not final and index != len(source) - 1)):
            _error("CANDLE_TIME_INVALID")
        if rows and (opened <= rows[-1].open_time or opened < rows[-1].close_time
                     or (opened - rows[-1].open_time) % step != timedelta(0)):
            _error("TIME_ORDER")
        if "available_at" in item:
            available = _utc(item["available_at"])
            if available < (closed if final else opened) or available > captured:
                _error("AVAILABILITY_INVALID")
        if "revision" in item and (type(item["revision"]) is not int or not 0 <= item["revision"] <= 2**63 - 1):
            _error("REVISION_INVALID")
        prices = {name: _number(item.get(name)) for name in ("open", "high", "low", "close")}
        # Validate decimal evidence before drawing's float conversion: sub-ULP
        # invalid OHLC bounds must not disappear through float rounding.
        precise = {name: Decimal(str(item[name])) for name in prices}
        if precise["low"] > min(precise["open"], precise["close"]) or precise["high"] < max(precise["open"], precise["close"]):
            _error("OHLC_INVALID")
        rows.append(_Candle(opened, closed, **prices, volume=_number(item.get("volume"), zero_allowed=True), is_final=final))
    forming = not rows[-1].is_final
    if type(snapshot.get("includes_forming_candle")) is not bool or snapshot["includes_forming_candle"] != forming:
        _error("FORMING_FLAG_INVALID")
    last_closed = next((row.close_time for row in reversed(rows) if row.is_final), None)
    declared_closed = None if snapshot.get("last_closed_at") is None else _utc(snapshot["last_closed_at"])
    if declared_closed != last_closed:
        _error("LAST_CLOSED_INVALID")
    raw_overlays = snapshot.get("overlays", {})
    if not isinstance(raw_overlays, dict):
        _error("OVERLAY_INVALID")
    by_time = {row.open_time: row for row in rows}
    overlays: dict[str, dict[datetime, float | None]] = {}
    for name in ("ema9", "ema21", "rsi14"):
        values = raw_overlays.get(name, [])
        if not isinstance(values, list) or len(values) > MAX_BARS:
            _error("OVERLAY_LIMIT")
        normalized: dict[datetime, float | None] = {}
        previous = None
        for item in values:
            if not isinstance(item, dict):
                _error("OVERLAY_INVALID")
            opened = _utc(item.get("open_time"))
            if opened not in by_time or (previous is not None and opened <= previous):
                _error("OVERLAY_ALIGNMENT")
            previous = opened
            raw = item.get("value")
            value = None if raw is None else _number(raw, zero_allowed=name == "rsi14")
            if name == "rsi14" and value is not None and value > 100:
                _error("RSI_RANGE")
            normalized[opened] = value if by_time[opened].is_final else None
        overlays[name] = normalized
    instrument = snapshot["instrument"]
    metadata = {"symbol": _text(instrument.get("symbol")), "market": _text(instrument.get("market"), 80),
                "venue": _text(instrument.get("venue") or instrument.get("exchange") or "未提供", 80),
                "timeframe": timeframe, "captured": captured, "last_closed": last_closed, "forming": forming}
    return rows, overlays, metadata


def _segments(rows: list[_Candle], values: dict[datetime, float | None], step: int) -> list[list[tuple[int, float]]]:
    """Indices are shared by all panels; missing bars and missing values break lines."""
    result: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for index, row in enumerate(rows):
        value = values.get(row.open_time) if row.is_final else None
        contiguous = index == 0 or row.open_time - rows[index - 1].open_time == timedelta(seconds=step)
        if value is None or not contiguous:
            if current:
                result.append(current)
            current = []
        if value is not None:
            current.append((index, value))
    if current:
        result.append(current)
    return result


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # Same deployment font order as telegram_chart; Chinese symbols stay legible.
    for path in ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _fit(draw: ImageDraw.ImageDraw, value: str, font: Any, width: int) -> str:
    if draw.textlength(value, font=font) <= width:
        return value
    while value and draw.textlength(value + "…", font=font) > width:
        value = value[:-1]
    return value + "…"


def _stamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC") if value is not None else "暂无已收盘数据"


def _format(value: float) -> str:
    return f"{value:.6g}"


def render_research_chart(snapshot: dict) -> bytes:
    """Render validated raw candles and supplied closed-only overlays to PNG bytes."""
    rows, overlays, meta = _validate(snapshot)
    if WIDTH * HEIGHT > MAX_PIXELS or WIDTH <= 0 or HEIGHT <= 0:
        _error("PIXEL_LIMIT")
    with Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND) as image:
        draw = ImageDraw.Draw(image)
        title, font, small = _font(27), _font(17), _font(15)
        draw.text((LEFT, 19), _fit(draw, f"{meta['symbol']}  ·  {meta['timeframe']}  |  GateScope", title, 1088), font=title, fill=TEXT)
        draw.text((LEFT, 59), _fit(draw, f"交易所 {meta['venue']}  /  市场 {meta['market']}", font, 1090), font=font, fill=MUTED)
        draw.text((LEFT, 88), f"截图 {_stamp(meta['captured'])}", font=small, fill=MUTED)
        draw.text((LEFT, 111), f"最后收盘 {_stamp(meta['last_closed'])}", font=small, fill=MUTED)
        forming_text = "包含未收盘：是 · 末根浅色，仅作观察" if meta["forming"] else "包含未收盘：否 · 全部已收盘"
        draw.text((687, 111), forming_text, font=small, fill=FORMING if meta["forming"] else UP)
        latest = rows[-1]
        summary = f"{'未收盘观察' if meta['forming'] else '末根已收盘'}  O {_format(latest.open)}   H {_format(latest.high)}   L {_format(latest.low)}   C {_format(latest.close)}   V {_format(latest.volume)}"
        draw.text((LEFT, 139), _fit(draw, summary, small, 1090), font=small, fill=TEXT)

        slot = (RIGHT - LEFT) / len(rows)
        centres = [LEFT + (index + 0.5) * slot for index in range(len(rows))]
        bar_width = max(1.0, min(11.0, slot * 0.66))
        for top, bottom in ((PRICE_TOP, PRICE_BOTTOM), (VOLUME_TOP, VOLUME_BOTTOM), (RSI_TOP, RSI_BOTTOM)):
            draw.rectangle((LEFT, top, RIGHT, bottom), fill=PANEL)
            if meta["forming"]:
                start = max(LEFT, centres[-1] - slot / 2)
                draw.rectangle((start, top, RIGHT, bottom), fill="#203044")
                draw.line((start, top, start, bottom), fill="#526478")
        step = TIMEFRAME_SECONDS[meta["timeframe"]]
        gaps = []
        for index in range(1, len(rows)):
            if rows[index].open_time - rows[index - 1].open_time != timedelta(seconds=step):
                gap_x = (centres[index - 1] + centres[index]) / 2
                gaps.append(gap_x)
                for top, bottom in ((PRICE_TOP, PRICE_BOTTOM), (VOLUME_TOP, VOLUME_BOTTOM), (RSI_TOP, RSI_BOTTOM)):
                    for y in range(top, bottom, 10):
                        draw.line((gap_x, y, gap_x, min(y + 4, bottom)), fill="#566078")
        values = [value for row in rows for value in (row.low, row.high)]
        values.extend(value for name in ("ema9", "ema21") for value in overlays[name].values() if value is not None)
        low, high = min(values), max(values)
        padding = max((high - low) * .08, high * .0005)
        lower, upper = max(0, low - padding), high + padding

        def price_y(value: float) -> float:
            return PRICE_BOTTOM - (value - lower) / (upper - lower) * (PRICE_BOTTOM - PRICE_TOP)

        def rsi_y(value: float) -> float:
            return RSI_BOTTOM - value / 100 * (RSI_BOTTOM - RSI_TOP)

        for index in range(6):
            value = lower + (upper - lower) * index / 5
            y = price_y(value)
            draw.line((LEFT, y, RIGHT, y), fill=GRID)
            draw.text((RIGHT + 10, y - 9), _format(value), font=small, fill=MUTED)
        volume_max = max((row.volume for row in rows), default=0) or 1
        for index, row in enumerate(rows):
            x = centres[index]
            color = FORMING if not row.is_final else UP if row.close >= row.open else DOWN
            draw.line((x, price_y(row.high), x, price_y(row.low)), fill=color)
            top, bottom = sorted((price_y(row.open), price_y(row.close)))
            draw.rectangle((x - bar_width / 2, top, x + bar_width / 2, min(PRICE_BOTTOM, max(top + 1, bottom))), fill=color)
            if row.volume:
                volume_y = VOLUME_BOTTOM - row.volume / volume_max * (VOLUME_BOTTOM - VOLUME_TOP)
                draw.rectangle((x - bar_width / 2, volume_y, x + bar_width / 2, VOLUME_BOTTOM), fill=color)
        for value in (0, 30, 50, 70, 100):
            y = rsi_y(value)
            for x in range(LEFT, RIGHT, 12 if value in (30, 70) else RIGHT - LEFT):
                draw.line((x, y, min(RIGHT, x + (6 if value in (30, 70) else RIGHT - LEFT)), y), fill="#45516d" if value in (30, 70) else GRID)
            draw.text((RIGHT + 10, y - 9), str(value), font=small, fill=MUTED)
        draw.text((LEFT, VOLUME_TOP - 27), "成交量 · 原始 OHLCV volume", font=font, fill=MUTED)
        draw.text((RIGHT + 10, VOLUME_TOP - 9), _format(volume_max) if any(row.volume for row in rows) else "0", font=small, fill=MUTED)
        for name, color, mapper in (("ema9", EMA9, price_y), ("ema21", EMA21, price_y), ("rsi14", RSI, rsi_y)):
            for segment in _segments(rows, overlays[name], step):
                points = [(centres[index], mapper(value)) for index, value in segment]
                if len(points) > 1:
                    draw.line(points, fill=color, width=2)
                elif points:
                    x, y = points[0]
                    draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=color)
        for name, color, x in (("ema9", EMA9, LEFT), ("ema21", EMA21, LEFT + 190)):
            value = overlays[name].get(next((row.open_time for row in reversed(rows) if row.is_final), None))
            draw.text((x, PRICE_TOP - 25), f"{name.upper()}  {_format(value) if value is not None else '未就绪 / 缺失'}", font=small, fill=color)
        rsi_latest = overlays["rsi14"].get(next((row.open_time for row in reversed(rows) if row.is_final), None))
        draw.text((LEFT, RSI_TOP - 27), f"RSI14  {_format(rsi_latest) if rsi_latest is not None else '未就绪 / 缺失'}   ·   30 / 70", font=font, fill=RSI)
        for index in sorted({round((len(rows) - 1) * tick / 4) for tick in range(5)}):
            label = rows[index].open_time.strftime("%m-%d %H:%M")
            label_width = draw.textlength(label, font=small)
            x = min(RIGHT - label_width, max(LEFT, centres[index] - label_width / 2))
            draw.text((x, 827), label, font=small, fill=MUTED)
        footer = f"UTC · {len(rows)} 根 · 指标仅基于已收盘数据 · 缺口 {len(gaps)} 处（断线） · 非交易执行指令"
        draw.text((LEFT, 869), footer, font=small, fill=MUTED)
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Description", f"{meta['venue']} | {meta['symbol']} | {meta['market']} | {meta['timeframe']} | captured={_stamp(meta['captured'])} | last_closed={_stamp(meta['last_closed'])} | forming={str(meta['forming']).lower()}")
        result = BytesIO()
        image.save(result, format="PNG", optimize=False, pnginfo=metadata)
        return result.getvalue()
