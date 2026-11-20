from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from gatescope.application import mcp_chart as chart


def snapshot(*, timeframe="5m", count=40, forming=False, flat=False):
    start = datetime(2026, 9, 13, tzinfo=timezone.utc)
    step = timedelta(seconds=chart.TIMEFRAME_SECONDS[timeframe])
    rows = []
    overlays = {name: [] for name in ("ema9", "ema21", "rsi14")}
    for index in range(count):
        opened = start + index * step
        closed = opened + step
        price = 100 if flat else 100 + index * .4
        final = not (forming and index == count - 1)
        row = {"open_time": opened.isoformat(), "close_time": closed.isoformat(),
               "open": str(price), "high": str(price if flat else price + 1),
               "low": str(price if flat else price - 1), "close": str(price if flat else price + .5),
               "volume": "0" if flat else str(100 + index * 5), "is_final": final,
               "revision": 1, "available_at": (closed if final else opened + step / 2).isoformat()}
        rows.append(row)
        for name, period in (("ema9", 9), ("ema21", 21), ("rsi14", 14)):
            overlays[name].append({"open_time": opened.isoformat(), "value": None if index < period else str(50 if name == "rsi14" else price)})
    return {"instrument": {"id": "fixture-id", "symbol": "龙虾_USDT", "venue": "GATE", "market": "futures:usdt"},
            "timeframe": timeframe, "captured_at": (datetime.fromisoformat(rows[-1]["available_at"]) + timedelta(seconds=1)).isoformat(),
            "last_closed_at": rows[-2 if forming else -1]["close_time"] if count > 1 or not forming else None,
            "includes_forming_candle": forming, "candles": rows, "overlays": overlays}


@pytest.mark.parametrize("timeframe", ["5m", "15m", "1h"])
def test_png_contains_exact_metadata_and_fixed_bounded_dimensions(timeframe):
    data = snapshot(timeframe=timeframe)
    frozen = deepcopy(data)
    result = chart.render_research_chart(data)
    assert result.startswith(b"\x89PNG\r\n\x1a\n") and len(result) < 2_000_000
    with Image.open(BytesIO(result)) as image:
        image.load()
        assert image.size == (1200, 900) and image.mode == "RGB"
        description = image.info["Description"]
        assert "GATE | 龙虾_USDT | futures:usdt" in description and timeframe in description
        assert "captured=" in description and "last_closed=" in description and "forming=false" in description
    assert data == frozen
    assert chart.render_research_chart(data) == result


def test_empty_warmup_flat_prices_zero_volume_and_single_forming_bar_render():
    for data in (snapshot(count=1, flat=True), snapshot(count=1, forming=True), snapshot(count=300)):
        with Image.open(BytesIO(chart.render_research_chart(data))) as image:
            assert image.size == (1200, 900)


def test_overlay_coordinates_use_exact_candle_index_and_no_line_bridges_missing_data(monkeypatch):
    data = snapshot(count=30)
    removed = data["candles"].pop(17)["open_time"]
    for values in data["overlays"].values():
        values[:] = [value for value in values if value["open_time"] != removed]
    data["overlays"]["ema9"][22]["value"] = None
    rows, overlays, _ = chart._validate(data)
    segments = chart._segments(rows, overlays["ema9"], 300)
    assert [[index for index, _ in segment] for segment in segments] == [list(range(9, 17)), list(range(17, 22)), list(range(23, 29))]
    paths = []
    original = ImageDraw.ImageDraw.line

    def capture(self, xy, fill=None, width=0, joint=None):
        if fill == chart.EMA9:
            paths.append(xy)
        return original(self, xy, fill=fill, width=width, joint=joint)

    monkeypatch.setattr(ImageDraw.ImageDraw, "line", capture)
    chart.render_research_chart(data)
    assert len(paths) == 3
    slot = (chart.RIGHT - chart.LEFT) / len(rows)
    for path, segment in zip(paths, segments, strict=True):
        assert [point[0] for point in path] == pytest.approx([chart.LEFT + (index + .5) * slot for index, _ in segment])


def test_forming_is_pale_and_excluded_from_indicators_and_price_scale():
    data = snapshot(forming=True)
    # A valid but extreme supplied forming EMA must not affect scale or lines.
    data["overlays"]["ema9"][-1]["value"] = "99999999"
    rows, overlays, metadata = chart._validate(data)
    assert metadata["forming"] is True and overlays["ema9"][rows[-1].open_time] is None
    for name in overlays:
        assert all(index < len(rows) - 1 for segment in chart._segments(rows, overlays[name], 300) for index, _ in segment)
    with Image.open(BytesIO(chart.render_research_chart(data))) as image:
        pale = tuple(bytes.fromhex(chart.FORMING[1:]))
        x = round(chart.LEFT + (len(rows) - .5) * (chart.RIGHT - chart.LEFT) / len(rows))
        assert any(image.getpixel((x, y)) == pale for y in range(chart.PRICE_TOP, chart.PRICE_BOTTOM))
        assert "forming=true" in image.info["Description"]
    other = deepcopy(data)
    other["overlays"]["ema9"][-1]["value"] = "100"
    assert chart.render_research_chart(data) == chart.render_research_chart(other)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999", "1e-999", "0", "-1", True, {}, "9" * 129])
def test_rejects_nonfinite_or_invalid_prices(value):
    data = snapshot()
    data["candles"][0]["open"] = value
    with pytest.raises(ValueError, match="MCP_CHART_"):
        chart.render_research_chart(data)


@pytest.mark.parametrize("field,value", [("low", "110"), ("high", "90"), ("volume", "-1"),
    ("revision", -1), ("revision", True), ("is_final", "true"),
    ("open_time", "2026-09-13T00:00:00"), ("open_time", "2026-09-13T08:00:00+08:00"),
    ("close_time", "2026-09-13T00:06:00Z"), ("available_at", "2099-01-01T00:00:00Z")])
def test_rejects_inconsistent_ohlcv_and_timing(field, value):
    data = snapshot()
    data["candles"][0][field] = value
    with pytest.raises(ValueError, match="MCP_CHART_"):
        chart.render_research_chart(data)


@pytest.mark.parametrize("mutation", ["empty", "too_many", "unordered", "duplicate", "forming_middle", "flag", "closed", "capture", "text", "timeframe"])
def test_rejects_snapshot_identity_and_candle_contract_failures(mutation):
    data = snapshot()
    if mutation == "empty":
        data["candles"] = []
    elif mutation == "too_many":
        data["candles"] *= 8
    elif mutation == "unordered":
        data["candles"][0], data["candles"][1] = data["candles"][1], data["candles"][0]
    elif mutation == "duplicate":
        data["candles"][1] = data["candles"][0]
    elif mutation == "forming_middle":
        data["candles"][0]["is_final"] = False
    elif mutation == "flag":
        data["includes_forming_candle"] = True
    elif mutation == "closed":
        data["last_closed_at"] = data["candles"][-2]["close_time"]
    elif mutation == "capture":
        data["captured_at"] = data["candles"][0]["open_time"]
    elif mutation == "text":
        data["instrument"]["symbol"] = "fake\nexchange"
    else:
        data["timeframe"] = "forever"
    with pytest.raises(ValueError, match="MCP_CHART_"):
        chart.render_research_chart(data)


@pytest.mark.parametrize("mutation", ["unknown_time", "duplicate", "out_of_order", "too_many", "rsi_range", "nonfinite"])
def test_overlay_malformed_or_misaligned_values_are_not_silently_shifted(mutation):
    data = snapshot()
    if mutation == "unknown_time":
        data["overlays"]["ema9"][0]["open_time"] = "2026-09-13T00:01:00Z"
    elif mutation == "duplicate":
        data["overlays"]["ema9"][1] = data["overlays"]["ema9"][0]
    elif mutation == "out_of_order":
        data["overlays"]["ema9"].reverse()
    elif mutation == "too_many":
        data["overlays"]["ema9"] *= 8
    elif mutation == "rsi_range":
        data["overlays"]["rsi14"][-1]["value"] = "101"
    else:
        data["overlays"]["ema21"][-1]["value"] = "NaN"
    with pytest.raises(ValueError, match="MCP_CHART_"):
        chart.render_research_chart(data)


def test_pixel_cap_is_checked_before_image_allocation(monkeypatch):
    monkeypatch.setattr(chart, "WIDTH", 100_000)
    monkeypatch.setattr(chart.Image, "new", lambda *args, **kwargs: pytest.fail("image allocated before validation"))
    with pytest.raises(ValueError, match="PIXEL_LIMIT"):
        chart.render_research_chart(snapshot())


def test_decimal_invalid_bound_cannot_disappear_through_float_rounding():
    data = snapshot(count=1, flat=True)
    data["candles"][0]["low"] = "100.00000000000000000000001"
    with pytest.raises(ValueError, match="OHLC_INVALID"):
        chart.render_research_chart(data)


def test_metadata_cannot_spoof_direction_with_unicode_control_characters():
    data = snapshot()
    data["instrument"]["symbol"] = "BTC\u202eTEST"
    with pytest.raises(ValueError, match="TEXT_INVALID"):
        chart.render_research_chart(data)
