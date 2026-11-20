"""Shared incremental engine used by live streams and historical batches."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Sequence

from gatescope.domain.market import Bar, utc
from gatescope.indicators.models import IndicatorNode, IndicatorPoint, IndicatorValues
from gatescope.indicators.registry import IndicatorRegistry, IndicatorState


class IndicatorStream:
    def __init__(self, registry: IndicatorRegistry, nodes: list[IndicatorNode]) -> None:
        self.registry = registry
        self.plan = registry.plan(nodes)
        self.states: dict[str, IndicatorState] = {}
        self.parameters: dict[str, dict[str, int | float]] = {}
        for node in self.plan:
            definition = registry.definition(node.indicator_id)
            parameters = definition.normalize_params(node.params)
            self.parameters[node.instance_id] = parameters
            self.states[node.instance_id] = definition.factory(parameters)
        self._series: tuple[str, str] | None = None
        self._last_open_time: datetime | None = None

    def update(self, bar: Bar, *, watermark: datetime | None = None) -> tuple[IndicatorPoint, ...]:
        if not bar.is_final:
            raise ValueError("indicators only consume final bars")
        if watermark is not None and not bar.visible_at(utc(watermark)):
            raise ValueError("bar is not available at the requested watermark")
        series = bar.instrument_id, bar.timeframe
        if self._series is not None and series != self._series:
            raise ValueError("one indicator stream can consume only one instrument/timeframe")
        if self._last_open_time is not None and bar.open_time <= self._last_open_time:
            raise ValueError("bar is out of order or revised; rebuild the stream from immutable facts")
        self._series = series
        self._last_open_time = bar.open_time
        current: dict[str, IndicatorValues] = {}
        points: list[IndicatorPoint] = []
        for node in self.plan:
            definition = self.registry.definition(node.indicator_id)
            upstream = {dependency: current[dependency] for dependency in node.depends_on}
            values = dict(self.states[node.instance_id].update(bar, upstream))
            if set(values) != set(definition.outputs):
                raise RuntimeError(f"{node.indicator_id} emitted values outside its registered contract")
            current[node.instance_id] = values
            points.append(
                IndicatorPoint(
                    instance_id=node.instance_id,
                    indicator_id=node.indicator_id,
                    indicator_version=definition.version,
                    instrument_id=bar.instrument_id,
                    timeframe=bar.timeframe,
                    open_time=bar.open_time,
                    available_at=bar.available_at,
                    bar_revision=bar.revision,
                    ready=all(value is not None for value in values.values()),
                    values=values,
                )
            )
        return tuple(points)


class IndicatorBatchCache:
    def __init__(self) -> None:
        self._items: dict[str, tuple[IndicatorPoint, ...]] = {}

    def get(self, key: str) -> tuple[IndicatorPoint, ...] | None:
        return self._items.get(key)

    def put(self, key: str, value: tuple[IndicatorPoint, ...]) -> None:
        self._items[key] = value

    def __len__(self) -> int:
        return len(self._items)


def batch_cache_key(registry: IndicatorRegistry, nodes: Sequence[IndicatorNode], bars: Sequence[Bar]) -> str:
    node_payload = []
    for node in registry.plan(list(nodes)):
        definition = registry.definition(node.indicator_id)
        node_payload.append(
            {
                "depends_on": node.depends_on,
                "indicator_id": node.indicator_id,
                "instance_id": node.instance_id,
                "params": definition.normalize_params(node.params),
                "version": definition.version,
            }
        )
    payload = {
        "bars": [bar.payload_hash for bar in bars],
        "nodes": node_payload,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def compute_batch(
    registry: IndicatorRegistry,
    nodes: Sequence[IndicatorNode],
    bars: Sequence[Bar],
    *,
    watermark: datetime | None = None,
    cache: IndicatorBatchCache | None = None,
) -> tuple[IndicatorPoint, ...]:
    key = batch_cache_key(registry, nodes, bars)
    # A cached later-watermark result cannot bypass causal availability checks.
    if watermark is not None and any(not bar.visible_at(utc(watermark)) for bar in bars):
        raise ValueError("bar is not available at the requested watermark")
    if cache is not None and (cached := cache.get(key)) is not None:
        return cached
    stream = IndicatorStream(registry, list(nodes))
    points = tuple(point for bar in bars for point in stream.update(bar, watermark=watermark))
    if cache is not None:
        cache.put(key, points)
    return points
