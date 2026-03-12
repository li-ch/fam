from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal

from fam.metrics.collector import MetricsCollector


@dataclass(order=True)
class DispatchItem:
    priority: float
    created_at: float
    agent_id: str = field(compare=False)
    endpoint_id: str = field(compare=False)
    payload: dict = field(compare=False)
    cost: Decimal = field(compare=False)
    future: asyncio.Future = field(compare=False)


class ToolDispatchQueue:
    def __init__(
        self,
        endpoints: dict[str, object],
        metrics: MetricsCollector,
        workers_per_endpoint: int = 1,
        max_retries: int = 1,
    ) -> None:
        self._endpoints = endpoints
        self._metrics = metrics
        self._workers_per_endpoint = max(1, workers_per_endpoint)
        self._max_retries = max(0, max_retries)
        self._queues: dict[str, asyncio.PriorityQueue] = {
            endpoint_id: asyncio.PriorityQueue() for endpoint_id in endpoints
        }
        self._workers: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for endpoint_id in self._endpoints:
            for worker_idx in range(self._workers_per_endpoint):
                self._workers.append(
                    asyncio.create_task(self._worker(endpoint_id, worker_idx))
                )

    async def stop(self) -> None:
        self._running = False
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._workers.clear()

    async def submit(
        self,
        *,
        agent_id: str,
        endpoint_id: str,
        payload: dict,
        cost: Decimal,
        budget_remaining: Decimal,
    ) -> dict:
        if endpoint_id not in self._queues:
            raise KeyError(f"Unknown endpoint: {endpoint_id}")
        # More remaining budget gets slightly better priority.
        prio = -(float(budget_remaining))
        fut = asyncio.get_running_loop().create_future()
        item = DispatchItem(
            priority=prio,
            created_at=time.monotonic(),
            agent_id=agent_id,
            endpoint_id=endpoint_id,
            payload=payload,
            cost=cost,
            future=fut,
        )
        await self._queues[endpoint_id].put(item)
        self._metrics.inc("dispatch.submitted")
        self._metrics.set_gauge(f"dispatch.queue_depth.{endpoint_id}", self._queues[endpoint_id].qsize())
        return await fut

    async def _worker(self, endpoint_id: str, worker_idx: int) -> None:
        queue = self._queues[endpoint_id]
        endpoint = self._endpoints[endpoint_id]
        self._metrics.inc(f"dispatch.worker_started.{endpoint_id}.{worker_idx}")
        while True:
            item: DispatchItem = await queue.get()
            self._metrics.set_gauge(f"dispatch.queue_depth.{endpoint_id}", queue.qsize())
            try:
                result = await self._call_with_retry(endpoint, item.payload)
                item.future.set_result(result)
                self._metrics.inc("dispatch.success")
            except Exception as exc:  # noqa: BLE001
                item.future.set_exception(exc)
                self._metrics.inc("dispatch.error")
            finally:
                queue.task_done()

    async def _call_with_retry(self, endpoint: object, payload: dict) -> dict:
        attempts = 0
        while True:
            attempts += 1
            try:
                return await endpoint.call(payload)
            except Exception:  # noqa: BLE001
                if attempts > self._max_retries:
                    raise
                await asyncio.sleep(0.01 * attempts)

