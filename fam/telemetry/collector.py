from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from fam.types import TelemetrySnapshot

SnapshotCallback = Callable[[TelemetrySnapshot], Awaitable[None]]


class TelemetryCollector:
    """Simple async collector for MVP polling loops."""

    def __init__(self, tick_interval_seconds: float = 0.5) -> None:
        self.tick_interval_seconds = tick_interval_seconds
        self.latest_snapshot: TelemetrySnapshot | None = None
        self._callbacks: list[SnapshotCallback] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._gpu_source = None
        self._tool_sources: dict[str, object] = {}

    def register_gpu_source(self, source: object) -> None:
        self._gpu_source = source

    def register_tool_source(self, endpoint_id: str, source: object) -> None:
        self._tool_sources[endpoint_id] = source

    def on_snapshot(self, callback: SnapshotCallback) -> None:
        self._callbacks.append(callback)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run_loop(self) -> None:
        while self._running:
            await self.poll_once()
            await asyncio.sleep(self.tick_interval_seconds)

    async def poll_once(self) -> TelemetrySnapshot:
        if self._gpu_source is None:
            raise RuntimeError("GPU source is not registered")

        gpu = await self._gpu_source.get_metrics()
        tools = {}
        for endpoint_id, source in self._tool_sources.items():
            tools[endpoint_id] = await source.get_telemetry()

        snapshot = TelemetrySnapshot(gpu=gpu, tools=tools, timestamp=time.monotonic())
        self.latest_snapshot = snapshot
        for callback in self._callbacks:
            await callback(snapshot)
        return snapshot

