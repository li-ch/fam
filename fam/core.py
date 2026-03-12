from __future__ import annotations

from decimal import Decimal

from fam.adapters.langgraph.adapter import MVPToolCallAdapter
from fam.budget.manager import BudgetManager
from fam.confirmation.handler import ConfirmationHandler
from fam.dispatch.queue import ToolDispatchQueue
from fam.interfaces.gpu_mock import GPUMock
from fam.interfaces.tool_mock import ToolMockEndpoint
from fam.metrics.collector import MetricsCollector
from fam.pricing.engine import PricingEngine
from fam.telemetry.collector import TelemetryCollector
from fam.types import PriceUpdate, ToolDecision


class FAMOrchestrator:
    """Phase 1 MVP orchestrator wiring."""

    def __init__(self) -> None:
        self.metrics = MetricsCollector()
        self.telemetry = TelemetryCollector(tick_interval_seconds=0.5)
        self.pricing = PricingEngine()
        self.budgets = BudgetManager(default_balance=Decimal("40"), replenishment_rate=Decimal("1.0"))
        self.confirmation = ConfirmationHandler(
            congestion_threshold=Decimal("1.10"),
            severe_multiplier=Decimal("2.0"),
        )

        self.gpu = GPUMock()
        self.tools = {
            "search": ToolMockEndpoint(endpoint_id="search", base_latency_ms=70, failure_rate=0.02),
            "code": ToolMockEndpoint(endpoint_id="code", base_latency_ms=150, failure_rate=0.05),
        }
        self.dispatch = ToolDispatchQueue(
            self.tools,
            self.metrics,
            workers_per_endpoint=64,
            max_retries=2,
        )
        self.adapter = MVPToolCallAdapter(self.budgets, self.confirmation, self.dispatch)
        self.latest_prices: PriceUpdate | None = None

        self.telemetry.register_gpu_source(self.gpu)
        for endpoint_id, endpoint in self.tools.items():
            self.telemetry.register_tool_source(endpoint_id, endpoint)

    async def start(self) -> None:
        await self.dispatch.start()
        snapshot = await self.telemetry.poll_once()
        self.latest_prices = self.pricing.update(snapshot)

    async def stop(self) -> None:
        await self.dispatch.stop()

    async def tick(self) -> PriceUpdate:
        snapshot = await self.telemetry.poll_once()
        self.latest_prices = self.pricing.update(snapshot)
        self.metrics.set_gauge("pricing.reasoning", float(self.latest_prices.reasoning_price))
        for endpoint_id, price in self.latest_prices.tool_prices.items():
            self.metrics.set_gauge(f"pricing.tool.{endpoint_id}", float(price))
        return self.latest_prices

    async def register_agent(self, agent_id: str) -> None:
        await self.budgets.register_agent(agent_id)

    async def handle_tool_call(
        self,
        *,
        agent_id: str,
        endpoint_id: str,
        payload: dict,
        requested_decision: ToolDecision | None = None,
    ) -> dict:
        if self.latest_prices is None:
            await self.tick()
        assert self.latest_prices is not None
        return await self.adapter.intercept_tool_call(
            agent_id=agent_id,
            endpoint_id=endpoint_id,
            payload=payload,
            prices=self.latest_prices,
            requested_decision=requested_decision,
        )

