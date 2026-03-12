from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class ToolDecision(str, Enum):
    APPROVE = "approve"
    CANCEL = "cancel"
    REASON_MORE = "reason_more"


@dataclass(frozen=True)
class GpuTelemetry:
    batch_queue_depth: int
    kv_cache_occupancy: float
    active_requests: int
    inference_latency_ms: float
    timestamp: float


@dataclass(frozen=True)
class ToolTelemetry:
    endpoint_id: str
    queue_depth: int
    active_calls: int
    latency_p90_ms: float
    rate_limit_headroom: float
    error_rate: float
    timestamp: float


@dataclass(frozen=True)
class TelemetrySnapshot:
    gpu: GpuTelemetry
    tools: dict[str, ToolTelemetry]
    timestamp: float


@dataclass(frozen=True)
class PriceUpdate:
    reasoning_price: Decimal
    tool_prices: dict[str, Decimal]
    tool_price_ratio: dict[str, Decimal]
    timestamp: float


@dataclass
class AgentBudget:
    agent_id: str
    balance: Decimal
    cumulative_reasoning_spend: Decimal = Decimal("0")
    cumulative_tool_spend: Decimal = Decimal("0")
    cumulative_speculative_spend: Decimal = Decimal("0")
    replenishment_rate: Decimal = Decimal("1")
    history: list[str] = field(default_factory=list)

