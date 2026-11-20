"""Versioned indicator contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

IndicatorScalar = float | None
IndicatorValues = Mapping[str, IndicatorScalar]


@dataclass(frozen=True)
class IndicatorPoint:
    instance_id: str
    indicator_id: str
    indicator_version: str
    instrument_id: str
    timeframe: str
    open_time: datetime
    available_at: datetime
    bar_revision: int
    ready: bool
    values: IndicatorValues


@dataclass(frozen=True)
class IndicatorNode:
    instance_id: str
    indicator_id: str
    params: Mapping[str, int | float]
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.instance_id or not self.indicator_id:
            raise ValueError("indicator node ids are required")
        if self.instance_id in self.depends_on:
            raise ValueError("indicator node cannot depend on itself")
