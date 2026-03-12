from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fam.types import PriceUpdate, TelemetrySnapshot


@dataclass
class PIController:
    kp: float
    ki: float
    integral: float = 0.0

    def step(self, error: float, dt: float) -> float:
        self.integral += error * max(dt, 1e-6)
        return self.kp * error + self.ki * self.integral


class PricingEngine:
    """Dual-price controller for MVP."""

    def __init__(
        self,
        reasoning_floor: Decimal = Decimal("0.01"),
        reasoning_ceiling: Decimal = Decimal("2.0"),
        tool_floor: Decimal = Decimal("0.05"),
        tool_ceiling: Decimal = Decimal("5.0"),
        reasoning_target_util: float = 0.70,
        tool_target_util: float = 0.65,
    ) -> None:
        self.reasoning_floor = reasoning_floor
        self.reasoning_ceiling = reasoning_ceiling
        self.tool_floor = tool_floor
        self.tool_ceiling = tool_ceiling
        self.reasoning_target_util = reasoning_target_util
        self.tool_target_util = tool_target_util
        self._reasoning_controller = PIController(kp=0.35, ki=0.08)
        self._tool_controllers: dict[str, PIController] = {}
        self._last_ts: float | None = None
        self.latest: PriceUpdate | None = None

    def update(self, snapshot: TelemetrySnapshot) -> PriceUpdate:
        dt = 1.0 if self._last_ts is None else snapshot.timestamp - self._last_ts
        self._last_ts = snapshot.timestamp

        gpu_util = self._gpu_util(snapshot)
        reasoning_signal = self._reasoning_controller.step(
            gpu_util - self.reasoning_target_util, dt
        )
        reasoning_price = self._clamp(
            self.reasoning_floor + Decimal(str(max(reasoning_signal, 0.0))),
            self.reasoning_floor,
            self.reasoning_ceiling,
        )

        tool_prices: dict[str, Decimal] = {}
        ratios: dict[str, Decimal] = {}
        for endpoint_id, telemetry in snapshot.tools.items():
            ctrl = self._tool_controllers.setdefault(endpoint_id, PIController(0.40, 0.10))
            util = self._tool_util(telemetry)
            signal = ctrl.step(util - self.tool_target_util, dt)
            price = self._clamp(
                self.tool_floor + Decimal(str(max(signal, 0.0))),
                self.tool_floor,
                self.tool_ceiling,
            )
            if self.latest is not None and endpoint_id in self.latest.tool_prices:
                # Keep price movement gradual in MVP to avoid oscillation spikes.
                prev = self.latest.tool_prices[endpoint_id]
                price = (price * Decimal("0.35")) + (prev * Decimal("0.65"))
            tool_prices[endpoint_id] = price
            ratios[endpoint_id] = (price / reasoning_price) if reasoning_price > 0 else Decimal("0")

        update = PriceUpdate(
            reasoning_price=reasoning_price,
            tool_prices=tool_prices,
            tool_price_ratio=ratios,
            timestamp=snapshot.timestamp,
        )
        self.latest = update
        return update

    @staticmethod
    def _gpu_util(snapshot: TelemetrySnapshot) -> float:
        # MVP composite: queue, KV pressure, and active requests.
        queue_norm = min(snapshot.gpu.batch_queue_depth / 64.0, 1.0)
        active_norm = min(snapshot.gpu.active_requests / 128.0, 1.0)
        return (0.4 * queue_norm) + (0.35 * snapshot.gpu.kv_cache_occupancy) + (0.25 * active_norm)

    @staticmethod
    def _tool_util(tool) -> float:
        active = min(tool.active_calls / 20.0, 1.0)
        headroom_pressure = 1.0 - tool.rate_limit_headroom
        queue = min(tool.queue_depth / 30.0, 1.0)
        return (0.45 * active) + (0.35 * headroom_pressure) + (0.20 * queue)

    @staticmethod
    def _clamp(value: Decimal, floor: Decimal, ceiling: Decimal) -> Decimal:
        return min(ceiling, max(floor, value))

