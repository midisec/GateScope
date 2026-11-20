"""Phase 1 read API over immutable market facts and versioned indicators."""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from gatescope.application.pagination import CursorCodec, InvalidCursor
from gatescope.application.market_payloads import canonical_bar as _canonical_bar, bar_payload as _bar_payload
from gatescope.auth import SESSION_COOKIE
from gatescope.domain.market import Bar, TIMEFRAME_SECONDS
from gatescope.indicators import IndicatorNode, compute_batch, default_registry

TOPIC_RE = re.compile(r"^(market\.bar\.delta|indicator\.delta):([^:]{1,120}):(1m|5m|15m|30m|1h|4h|8h|1d)$")


class LegacyReadDatabase(Protocol):
    def search_instruments(self, query: str, market: str, limit: int, offset: int) -> list[dict[str, Any]]: ...
    def instrument_count(self) -> int: ...
    def get_instrument(self, instrument_id: str) -> dict[str, Any] | None: ...
    def get_canonical_candles(
        self, instrument_id: str, interval: str, start_ts: int | None, end_ts: int | None, limit: int,
    ) -> list[dict[str, Any]]: ...
    def candle_bounds(self, instrument_id: str, interval: str) -> tuple[int | None, int | None]: ...


class SessionReader(Protocol):
    def session(self, token: str | None) -> Any | None: ...


class IndicatorNodeRequest(BaseModel):
    instance_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    indicator_id: str = Field(min_length=1, max_length=64)
    params: dict[str, int | float] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=16)


class IndicatorPreviewRequest(BaseModel):
    instrument_id: str = Field(min_length=1, max_length=200)
    timeframe: str = Field(default="15m", pattern=r"^(1m|5m|15m|30m|1h|4h|8h|1d)$")
    from_time: datetime | None = None
    to_time: datetime | None = None
    limit: int = Field(default=1_000, ge=1, le=5_000)
    nodes: list[IndicatorNodeRequest] = Field(min_length=1, max_length=32)


class WebSocketRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    action: str = Field(pattern=r"^(subscribe|unsubscribe)$")
    topic: str = Field(min_length=1, max_length=240)
    cursor: str | None = Field(default=None, max_length=256)


def build_v2_router(
    database: LegacyReadDatabase,
    auth: SessionReader,
    *,
    cursor_secret: bytes | None = None,
) -> APIRouter:
    router = APIRouter(tags=["v2"])
    rest = APIRouter(prefix="/api/v2")
    cursors = CursorCodec(cursor_secret or secrets.token_bytes(32))
    registry = default_registry()

    @rest.get("/instruments")
    def instruments(
        request: Request,
        q: str = Query(default="", max_length=100),
        market: str = Query(default="all", pattern=r"^(all|spot|stock|futures:usdt|futures:btc)$"),
        limit: int = Query(default=100, ge=1, le=500),
        cursor: str | None = Query(default=None, max_length=1_024),
    ) -> dict[str, Any]:
        binding = {"q": q, "market": market, "limit": limit}
        offset = _cursor_integer(cursors, cursor, binding, "offset") if cursor else 0
        rows = database.search_instruments(q, market, limit + 1, offset)
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = cursors.encode({"offset": offset + len(items)}, binding) if has_more else None
        return _page(items, next_cursor, has_more, request.state.trace_id)

    @rest.get("/bars")
    def bars(
        request: Request,
        instrument_id: str = Query(min_length=1, max_length=200),
        timeframe: str = Query(default="15m", pattern=r"^(1m|5m|15m|30m|1h|4h|8h|1d)$"),
        from_time: Annotated[datetime | None, Query(alias="from")] = None,
        to_time: Annotated[datetime | None, Query(alias="to")] = None,
        limit: int = Query(default=1_000, ge=1, le=5_000),
        latest: bool = Query(default=False),
        cursor: str | None = Query(default=None, max_length=1_024),
    ) -> dict[str, Any]:
        instrument = database.get_instrument(instrument_id)
        if instrument is None:
            raise HTTPException(404, "交易标的不存在")
        start_ts = _utc_epoch(from_time) if from_time else None
        end_ts = _utc_epoch(to_time) if to_time else None
        if start_ts is not None and end_ts is not None and start_ts > end_ts:
            raise HTTPException(422, "from 必须早于或等于 to")
        binding = {
            "instrument_id": instrument_id, "timeframe": timeframe,
            "from": start_ts, "to": end_ts, "limit": limit, "latest": latest,
        }
        if latest and cursor is not None:
            raise HTTPException(422, "latest 视图不接受 cursor")
        if cursor:
            after = _cursor_integer(cursors, cursor, binding, "after")
            start_ts = max(start_ts or after + 1, after + 1)
        if latest and start_ts is None and end_ts is None and hasattr(database,"latest_chart_snapshot"):
            prepared=database.latest_chart_snapshot(instrument_id,timeframe,limit)
            if prepared:
                items=prepared["items"];watermark=items[-1]["available_at"] if items else None
                return {**_page(items,None,False,request.state.trace_id),"as_of":datetime.now(timezone.utc).isoformat(),
                    "data_watermark":watermark,"staleness_ms":_staleness_ms(watermark),
                    "quality_flags":sorted({q for b in items for q in b["quality_flags"]}),
                    "cache":database.cache_status(instrument_id,timeframe),"read_source":"BACKGROUND_SNAPSHOT",
                    "snapshot_built_at":prepared["built_at"]}
        if latest and start_ts is None:
            if hasattr(database, "latest_candle_time"):
                maximum = database.latest_candle_time(instrument_id, timeframe)
            else:
                _, maximum = database.candle_bounds(instrument_id, timeframe)
            if maximum is not None:
                start_ts = max(0, maximum - TIMEFRAME_SECONDS[timeframe] * (limit - 1))
        rows = database.get_canonical_candles(instrument_id, timeframe, start_ts, end_ts, limit + 1)
        has_more = not latest and len(rows) > limit
        selected = rows[:limit]
        canonical = [_canonical_bar(instrument, instrument_id, timeframe, row) for row in selected]
        items = [_bar_payload(bar) for bar in canonical]
        next_cursor = cursors.encode({"after": int(selected[-1]["ts"])}, binding) if has_more else None
        watermark = items[-1]["available_at"] if items else None
        cache = database.cache_status(instrument_id, timeframe) if hasattr(database, "cache_status") else None
        return {
            **_page(items, next_cursor, has_more, request.state.trace_id),
            "as_of": datetime.now(timezone.utc).isoformat(),
            "data_watermark": watermark,
            "staleness_ms": _staleness_ms(watermark),
            "quality_flags": _quality_flags(canonical),
            "cache": cache,
        }

    @rest.get("/market/watchlist-cache")
    def watchlist_cache(request: Request) -> dict[str, Any]:
        session = request.state.auth_session
        items = database.watchlist_cache(session.tenant_id, session.user_id) if hasattr(database, "watchlist_cache") else []
        return {"items": items}

    @rest.get("/indicators/catalog")
    def indicator_catalog(request: Request) -> dict[str, Any]:
        items = []
        for identifier in registry.identifiers():
            definition = registry.definition(identifier)
            defaults = {name: spec.default for name, spec in definition.params.items()}
            items.append({
                "indicator_id": identifier,
                "version": definition.version,
                "outputs": definition.outputs,
                "parameters": {
                    name: {
                        "type": "integer" if spec.kind is int else "number",
                        "default": spec.default, "minimum": spec.minimum, "maximum": spec.maximum,
                    }
                    for name, spec in definition.params.items()
                },
                "warmup_bars": definition.warmup(defaults),
            })
        return {"items": items, "trace_id": request.state.trace_id}

    @rest.get("/patterns/catalog")
    def pattern_catalog(request: Request) -> dict[str, Any]:
        from gatescope.quant.pattern_study import catalog
        from gatescope.quant.structure_detection import VERSION

        return {
            "items": [
                {
                    "pattern_id": str(item["id"]),
                    "name": str(item["name"]),
                    "group": str(item["group"]),
                    "direction": (
                        "bearish" if item["id"] == "elliott_impulse_bull" else
                        "bullish" if item["id"] == "elliott_impulse_bear" else str(item["direction"])
                    ),
                    "structural_direction": str(item["direction"]),
                    "experimental": bool(item.get("experimental", False)),
                }
                for item in catalog()
            ],
            "detector_version": VERSION,
            "trace_id": request.state.trace_id,
        }

    @rest.post("/indicators/preview")
    def indicator_preview(payload: IndicatorPreviewRequest, request: Request) -> dict[str, Any]:
        instrument = database.get_instrument(payload.instrument_id)
        if instrument is None:
            raise HTTPException(404, "交易标的不存在")
        start = _utc_epoch(payload.from_time) if payload.from_time else None
        end = _utc_epoch(payload.to_time) if payload.to_time else None
        if start is not None and end is not None and start > end:
            raise HTTPException(422, "from_time 必须早于或等于 to_time")
        raw_bars = database.get_canonical_candles(
            payload.instrument_id, payload.timeframe, start, end, payload.limit,
        )
        canonical = [
            _canonical_bar(instrument, payload.instrument_id, payload.timeframe, row)
            for row in raw_bars
        ]
        nodes = [
            IndicatorNode(node.instance_id, node.indicator_id, node.params, tuple(node.depends_on))
            for node in payload.nodes
        ]
        try:
            points = compute_batch(registry, nodes, canonical)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        watermark = canonical[-1].available_at.isoformat() if canonical else None
        return {
            "items": [
                {
                    "instance_id": point.instance_id,
                    "indicator_id": point.indicator_id,
                    "indicator_version": point.indicator_version,
                    "open_time": point.open_time.isoformat(),
                    "available_at": point.available_at.isoformat(),
                    "bar_revision": point.bar_revision,
                    "ready": point.ready,
                    "values": point.values,
                }
                for point in points
            ],
            "as_of": datetime.now(timezone.utc).isoformat(),
            "data_watermark": watermark,
            "staleness_ms": _staleness_ms(watermark),
            "quality_flags": _quality_flags(canonical),
            "trace_id": request.state.trace_id,
        }

    @router.websocket("/ws/v2")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        session = auth.session(websocket.cookies.get(SESSION_COOKIE))
        if session is None or not bool(getattr(session, "fully_authenticated", True)):
            await websocket.close(code=4401, reason="authentication required")
            return
        await websocket.accept()
        sequences: dict[str, int] = {}
        try:
            while True:
                try:
                    message = WebSocketRequest.model_validate(await websocket.receive_json())
                except WebSocketDisconnect:
                    raise
                except Exception:
                    await _ws_send(websocket, sequences, "error", "protocol", {"code": "INVALID_MESSAGE"})
                    continue
                if message.topic != "system.health" and TOPIC_RE.fullmatch(message.topic) is None:
                    await websocket.close(code=4403, reason="topic forbidden")
                    return
                stream_key = f"{session.user_id}:{message.topic}"
                if message.action == "unsubscribe":
                    await _ws_send(
                        websocket, sequences, "unsubscribed", stream_key,
                        {"request_id": message.request_id, "topic": message.topic},
                    )
                    continue
                watermark, snapshot_url = _topic_snapshot(database, message.topic)
                if message.cursor is not None and message.cursor != watermark:
                    await _ws_send(
                        websocket, sequences, "resync_required", stream_key,
                        {"request_id": message.request_id, "snapshot_url": snapshot_url, "watermark": watermark},
                    )
                    continue
                await _ws_send(
                    websocket, sequences, "subscribed", stream_key,
                    {
                        "request_id": message.request_id, "topic": message.topic,
                        "watermark": watermark, "snapshot_url": snapshot_url, "buffer_limit": 1_000,
                    },
                )
        except WebSocketDisconnect:
            return

    router.include_router(rest)
    return router


def _cursor_integer(codec: CursorCodec, cursor: str, binding: dict[str, Any], field: str) -> int:
    try:
        value = codec.decode(cursor, binding).get(field)
    except InvalidCursor as exc:
        raise HTTPException(422, str(exc)) from exc
    if not isinstance(value, int) or value < 0:
        raise HTTPException(422, "cursor position is invalid")
    return value


def _utc_epoch(value: datetime) -> int:
    if value.tzinfo is None:
        raise HTTPException(422, "timestamps must include a timezone")
    return int(value.astimezone(timezone.utc).timestamp())




def _quality_flags(bars: list[Bar]) -> list[str]:
    return sorted({flag for bar in bars for flag in bar.quality_flags})


def _page(items: list[Any], next_cursor: str | None, has_more: bool, trace_id: str) -> dict[str, Any]:
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more, "trace_id": trace_id}


def _staleness_ms(watermark: str | None) -> int | None:
    if watermark is None:
        return None
    observed = datetime.fromisoformat(watermark)
    return max(0, int((datetime.now(timezone.utc) - observed).total_seconds() * 1_000))


def _topic_snapshot(database: LegacyReadDatabase, topic: str) -> tuple[str, str]:
    match = TOPIC_RE.fullmatch(topic)
    if match is None:
        return str(int(time.time())), "/health/ready"
    _, instrument_id, timeframe = match.groups()
    _, maximum = database.candle_bounds(instrument_id, timeframe)
    watermark = str(maximum or 0)
    return watermark, f"/api/v2/bars?instrument_id={instrument_id}&timeframe={timeframe}"


async def _ws_send(
    websocket: WebSocket,
    sequences: dict[str, int],
    message_type: str,
    stream_key: str,
    payload: dict[str, Any],
) -> None:
    sequence = sequences.get(stream_key, 0) + 1
    sequences[stream_key] = sequence
    await websocket.send_json({
        "type": message_type,
        "stream_key": stream_key,
        "sequence": sequence,
        "watermark": str(payload.get("watermark", "")),
        "server_time": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
        "payload": payload,
    })
