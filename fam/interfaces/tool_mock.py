from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

from fam.types import ToolTelemetry


@dataclass
class ToolMockEndpoint:
    endpoint_id: str
    base_latency_ms: float = 80.0
    failure_rate: float = 0.05
    capacity: int = 8
    rate_limit_headroom: float = 1.0
    queue_depth: int = 0
    active_calls: int = 0
    recent_latency_ms: list[float] = field(default_factory=list)
    recent_errors: int = 0
    recent_total: int = 0

    async def call(self, payload: dict) -> dict:
        self.active_calls += 1
        jitter = random.uniform(0.8, 1.3)
        delay_s = (self.base_latency_ms * jitter) / 1000.0
        if self.active_calls > self.capacity:
            self.queue_depth += 1
            delay_s *= 1.5
            self.rate_limit_headroom = max(0.05, self.rate_limit_headroom - 0.05)

        start = time.monotonic()
        await asyncio.sleep(delay_s)
        self.recent_total += 1
        if random.random() < self.failure_rate:
            self.recent_errors += 1
            self.active_calls = max(0, self.active_calls - 1)
            self.recent_latency_ms.append((time.monotonic() - start) * 1000)
            raise RuntimeError(f"{self.endpoint_id} temporary failure")

        latency = (time.monotonic() - start) * 1000
        self.recent_latency_ms.append(latency)
        self.active_calls = max(0, self.active_calls - 1)
        self.queue_depth = max(0, self.queue_depth - 1)
        self.rate_limit_headroom = min(1.0, self.rate_limit_headroom + 0.02)
        return {"endpoint_id": self.endpoint_id, "payload": payload, "latency_ms": latency}

    async def get_telemetry(self) -> ToolTelemetry:
        latencies = self.recent_latency_ms[-30:] or [self.base_latency_ms]
        sorted_vals = sorted(latencies)
        idx = max(0, int(0.9 * (len(sorted_vals) - 1)))
        p90 = sorted_vals[idx]
        error_rate = 0.0 if self.recent_total == 0 else self.recent_errors / self.recent_total
        return ToolTelemetry(
            endpoint_id=self.endpoint_id,
            queue_depth=self.queue_depth,
            active_calls=self.active_calls,
            latency_p90_ms=p90,
            rate_limit_headroom=self.rate_limit_headroom,
            error_rate=error_rate,
            timestamp=time.monotonic(),
        )

