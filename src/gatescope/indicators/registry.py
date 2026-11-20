"""Indicator registration, parameter validation and DAG planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from gatescope.domain.market import Bar
from gatescope.indicators.models import IndicatorNode, IndicatorValues


class IndicatorState(Protocol):
    def update(self, bar: Bar, upstream: Mapping[str, IndicatorValues]) -> IndicatorValues: ...


@dataclass(frozen=True)
class ParamSpec:
    kind: type[int] | type[float]
    default: int | float
    minimum: int | float
    maximum: int | float

    def validate(self, name: str, value: object) -> int | float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be numeric")
        if self.kind is int and not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if not self.minimum <= value <= self.maximum:
            raise ValueError(f"{name} must be between {self.minimum} and {self.maximum}")
        return value


@dataclass(frozen=True)
class IndicatorDefinition:
    indicator_id: str
    version: str
    outputs: tuple[str, ...]
    params: Mapping[str, ParamSpec]
    warmup: Callable[[Mapping[str, int | float]], int]
    factory: Callable[[Mapping[str, int | float]], IndicatorState]

    def normalize_params(self, supplied: Mapping[str, int | float]) -> dict[str, int | float]:
        unknown = set(supplied) - set(self.params)
        if unknown:
            raise ValueError(f"unknown parameters for {self.indicator_id}: {', '.join(sorted(unknown))}")
        return {
            name: spec.validate(name, supplied.get(name, spec.default))
            for name, spec in self.params.items()
        }


class IndicatorRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, IndicatorDefinition] = {}

    def register(self, definition: IndicatorDefinition) -> None:
        if not definition.indicator_id or not definition.version or not definition.outputs:
            raise ValueError("indicator definition id, version and outputs are required")
        if definition.indicator_id in self._definitions:
            raise ValueError(f"duplicate indicator definition: {definition.indicator_id}")
        self._definitions[definition.indicator_id] = definition

    def definition(self, indicator_id: str) -> IndicatorDefinition:
        try:
            return self._definitions[indicator_id]
        except KeyError as exc:
            raise ValueError(f"unknown indicator: {indicator_id}") from exc

    def identifiers(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def plan(self, nodes: list[IndicatorNode]) -> tuple[IndicatorNode, ...]:
        by_id = {node.instance_id: node for node in nodes}
        if len(by_id) != len(nodes):
            raise ValueError("indicator instance ids must be unique")
        for node in nodes:
            definition = self.definition(node.indicator_id)
            definition.normalize_params(node.params)
            missing = set(node.depends_on) - set(by_id)
            if missing:
                raise ValueError(f"missing indicator dependencies: {', '.join(sorted(missing))}")

        ordered: list[IndicatorNode] = []
        temporary: set[str] = set()
        permanent: set[str] = set()

        def visit(instance_id: str) -> None:
            if instance_id in permanent:
                return
            if instance_id in temporary:
                raise ValueError("indicator dependency graph contains a cycle")
            temporary.add(instance_id)
            node = by_id[instance_id]
            for dependency in node.depends_on:
                visit(dependency)
            temporary.remove(instance_id)
            permanent.add(instance_id)
            ordered.append(node)

        for node in nodes:
            visit(node.instance_id)
        return tuple(ordered)
