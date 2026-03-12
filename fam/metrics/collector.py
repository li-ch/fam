from __future__ import annotations

from collections import defaultdict


class MetricsCollector:
    def __init__(self) -> None:
        self.counters: dict[str, float] = defaultdict(float)
        self.gauges: dict[str, float] = {}

    def inc(self, name: str, value: float = 1.0) -> None:
        self.counters[name] += value

    def set_gauge(self, name: str, value: float) -> None:
        self.gauges[name] = value

