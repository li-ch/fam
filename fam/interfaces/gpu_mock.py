from __future__ import annotations

import time
from dataclasses import dataclass

from fam.types import GpuTelemetry


@dataclass
class GPUMock:
    queue_depth: int = 5
    kv_occupancy: float = 0.30
    active_requests: int = 12
    latency_ms: float = 120.0
    drift: float = 0.02

    async def get_metrics(self) -> GpuTelemetry:
        # Deterministic drift to emulate load variation.
        self.kv_occupancy = min(0.98, self.kv_occupancy + self.drift)
        self.queue_depth = min(80, self.queue_depth + 1)
        self.active_requests = min(128, self.active_requests + 2)
        self.latency_ms = min(2000.0, self.latency_ms * 1.03)
        return GpuTelemetry(
            batch_queue_depth=self.queue_depth,
            kv_cache_occupancy=self.kv_occupancy,
            active_requests=self.active_requests,
            inference_latency_ms=self.latency_ms,
            timestamp=time.monotonic(),
        )

