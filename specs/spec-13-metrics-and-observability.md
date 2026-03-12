# Spec 13 — Metrics and Observability

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview), Spec 12 (Configuration and Tuning)
**Consumed By:** Spec 14 (Evaluation Harness), all other specs (01–11) emit metrics defined here

---

## 1. Purpose

FAM is a research system built to test the hypothesis that market-based resource allocation can coordinate multi-agent systems more effectively than hard limits or heuristic scheduling. Proving or disproving this hypothesis requires rigorous measurement. Every pricing decision, budget mutation, confirmation exchange, dispatch event, and speculative continuation episode must be recorded with enough detail to reconstruct what happened, why it happened, and how it affected system-level outcomes.

This spec defines the observability surface of FAM: what metrics are emitted, how they are structured, how they are exported, and how they are visualized. It covers three layers — structured event logging (for debugging and trace reconstruction), numeric metrics (for real-time dashboards and statistical analysis), and a dashboard specification (defining what graphs should be displayed for the key experiments in spec 14).

The metrics system is not optional and not an afterthought. Every module in FAM emits metrics as part of its normal operation, and the metrics system is designed to handle the full throughput of a multi-agent evaluation run without becoming a bottleneck.

---

## 2. Module Location

```
fam/
├── metrics/
│   ├── __init__.py
│   ├── collector.py         # MetricsCollector — central metric sink
│   ├── events.py            # Structured event dataclasses
│   ├── prometheus.py        # Prometheus-compatible metric export
│   ├── logger.py            # Structured logging output
│   └── dashboard.py         # Dashboard specification and rendering helpers
```

Tests live in `tests/test_metrics/`.

---

## 3. Architecture

The metrics system uses a centralized collector pattern. Every module in FAM holds a reference to a single `MetricsCollector` instance and calls its methods to emit events and metric updates. The collector buffers incoming data and dispatches it to configured backends (Prometheus exporter, structured log writer, in-memory time-series store for the evaluation harness).

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Telemetry   │  │   Pricing    │  │   Budget     │
│  Collector   │  │   Engine     │  │   Manager    │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                  │
       │  emit()         │  emit()          │  emit()
       ▼                 ▼                  ▼
┌─────────────────────────────────────────────────────┐
│                  MetricsCollector                     │
│                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │ Event Buffer │  │ Metric Store │  │ Time Series│ │
│  │  (deque)     │  │ (counters,   │  │  (for eval │ │
│  │              │  │  gauges,     │  │   harness) │ │
│  │              │  │  histograms) │  │            │ │
│  └──────┬───────┘  └──────┬───────┘  └─────┬──────┘ │
│         │                 │                 │        │
│    ┌────┴────┐       ┌────┴────┐      ┌────┴────┐   │
│    │ Log     │       │Promethe-│      │ Eval    │   │
│    │ Writer  │       │us Export│      │ Export  │   │
│    └─────────┘       └─────────┘      └─────────┘   │
└─────────────────────────────────────────────────────┘
```

The collector is thread-safe within asyncio (single-threaded, but multiple coroutines may emit concurrently). All emit operations are non-blocking — they write to in-memory buffers and return immediately. Background tasks drain the buffers to backends.

---

## 4. Structured Events

Every significant FAM action is recorded as a structured event. Events are the primary debugging and trace reconstruction mechanism. Each event is a frozen dataclass with a standard header and module-specific fields.

### 4.1 Event Header

```python
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


class EventSeverity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True)
class EventHeader:
    """Standard header present on every FAM event."""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    event_type: str = ""                   # e.g., "pricing.update", "budget.deduct"
    timestamp: float = field(default_factory=time.monotonic)
    wall_clock: str = ""                   # ISO 8601 wall-clock time for human readers
    severity: EventSeverity = EventSeverity.INFO
    agent_id: str | None = None            # None for system-level events
    module: str = ""                       # e.g., "pricing_engine", "budget_manager"
```

### 4.2 Pricing Events

```python
@dataclass(frozen=True)
class PriceUpdateEvent:
    """Emitted by the pricing engine on each price computation."""

    header: EventHeader
    reasoning_price: float
    tool_price: float                      # Aggregate or per-endpoint
    tool_prices: dict[str, float]          # Per-endpoint prices
    gpu_utilization: float
    tool_utilization: dict[str, float]     # Per-endpoint utilization
    reasoning_price_delta: float           # Change from previous update
    tool_price_delta: float
    price_ratio: float                     # reasoning_price / tool_price
    controller_p_term: float               # Proportional term value
    controller_i_term: float               # Integral term value


@dataclass(frozen=True)
class PriceTrendEvent:
    """Emitted when a price crosses a threshold or changes direction."""

    header: EventHeader
    resource: str                          # "reasoning" or "tool:{endpoint_id}"
    trend: str                             # "rising", "falling", "stable"
    current_price: float
    previous_price: float
    utilization: float
```

### 4.3 Budget Events

```python
@dataclass(frozen=True)
class BudgetDeductEvent:
    """Emitted on every budget deduction."""

    header: EventHeader
    amount: float
    reason: str                            # "tool_call", "reasoning", "speculative"
    tool_name: str | None                  # For tool_call deductions
    price_at_deduction: float
    balance_before: float
    balance_after: float


@dataclass(frozen=True)
class BudgetReplenishEvent:
    """Emitted on every budget replenishment."""

    header: EventHeader
    amount: float
    balance_before: float
    balance_after: float
    capped: bool                           # True if replenishment was capped at max_balance


@dataclass(frozen=True)
class BudgetExhaustedEvent:
    """Emitted when an agent's budget hits a limit."""

    header: EventHeader
    limit_type: str                        # "soft" or "hard"
    balance: float
    attempted_deduction: float
```

### 4.4 Confirmation Events

```python
@dataclass(frozen=True)
class ConfirmationRequestEvent:
    """Emitted when a confirmation request is sent to an agent."""

    header: EventHeader
    tool_name: str
    tool_price: float
    budget_balance: float
    tier: str                              # Agent's capability tier
    auto_approved: bool                    # True if below threshold (no actual confirmation)


@dataclass(frozen=True)
class ConfirmationDecisionEvent:
    """Emitted when an agent responds to a confirmation request."""

    header: EventHeader
    tool_name: str
    decision: str                          # "approve", "defer", "cancel", "timeout"
    parse_confidence: float
    response_time_ms: float                # Time from request to decision
    tool_price_at_decision: float
    budget_at_decision: float


@dataclass(frozen=True)
class ConfirmationSummaryEvent:
    """Periodic summary of confirmation activity across all agents."""

    header: EventHeader
    window_seconds: float
    total_requests: int
    approvals: int
    deferrals: int
    cancellations: int
    timeouts: int
    auto_approvals: int
    mean_response_time_ms: float
```

### 4.5 Dispatch Events

```python
@dataclass(frozen=True)
class ToolDispatchEvent:
    """Emitted when a tool call is dispatched to an endpoint."""

    header: EventHeader
    tool_name: str
    endpoint_id: str
    queue_depth_at_dispatch: int
    priority_score: float
    time_in_queue_ms: float


@dataclass(frozen=True)
class ToolResultEvent:
    """Emitted when a dispatched tool call completes."""

    header: EventHeader
    tool_name: str
    endpoint_id: str
    success: bool
    latency_ms: float
    error_type: str | None
    retry_count: int
```

### 4.6 Speculative Continuation Events

```python
@dataclass(frozen=True)
class SpeculationStartEvent:
    """Emitted when speculative continuation begins for an agent."""

    header: EventHeader
    session_id: str
    tool_name: str
    reasoning_price_at_start: float
    budget_at_start: float


@dataclass(frozen=True)
class SpeculationResolvedEvent:
    """Emitted when a speculative session is resolved."""

    header: EventHeader
    session_id: str
    resolution: str                        # "commit", "rollback", "cancel", "timeout"
    tokens_generated: int
    tokens_retained: int
    tokens_discarded: int
    speculative_cost: float
    duration_seconds: float
    consistency_score: float | None
```

### 4.7 Telemetry Health Events

```python
@dataclass(frozen=True)
class TelemetryHealthEvent:
    """Emitted on telemetry source health state transitions."""

    header: EventHeader
    source_id: str
    previous_state: str                    # "healthy", "stale", "unhealthy"
    new_state: str
    staleness_seconds: float
    consecutive_failures: int
```

### 4.8 Agent Phase Events

```python
@dataclass(frozen=True)
class AgentPhaseEvent:
    """Emitted when an agent transitions between phases."""

    header: EventHeader
    previous_phase: str                    # "reasoning", "tool_waiting", "speculating", "idle"
    new_phase: str
    time_in_previous_phase_ms: float
```

---

## 5. Numeric Metrics

Numeric metrics are aggregated values suitable for time-series dashboards and Prometheus scraping. They fall into three categories: counters (monotonically increasing), gauges (point-in-time values), and histograms (distribution summaries).

### 5.1 Metric Registry

```python
from enum import Enum


class MetricType(str, Enum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass
class MetricDefinition:
    """Defines a metric that the system emits."""

    name: str
    metric_type: MetricType
    description: str
    labels: list[str]                      # Label names (e.g., ["agent_id", "tool_name"])
    buckets: list[float] | None = None     # For histograms only
```

### 5.2 Complete Metric Inventory

#### Budget Metrics

| Name | Type | Labels | Description |
|---|---|---|---|
| `fam_budget_balance` | gauge | `agent_id` | Current budget balance per agent |
| `fam_budget_deductions_total` | counter | `agent_id`, `reason` | Cumulative deductions (reason: tool_call, reasoning, speculative) |
| `fam_budget_replenishments_total` | counter | `agent_id` | Cumulative replenishments |
| `fam_budget_exhaustions_total` | counter | `agent_id`, `limit_type` | Times budget hit soft/hard limit |
| `fam_budget_spend_rate` | gauge | `agent_id`, `resource` | Current spend rate (units/sec, by resource type) |

#### Pricing Metrics

| Name | Type | Labels | Description |
|---|---|---|---|
| `fam_price_reasoning` | gauge | | Current reasoning price per token |
| `fam_price_tool` | gauge | `endpoint_id` | Current tool price per endpoint |
| `fam_price_ratio` | gauge | | reasoning_price / tool_price (aggregate) |
| `fam_price_updates_total` | counter | | Total pricing engine ticks |
| `fam_price_at_floor_seconds` | counter | `resource` | Time spent at price floor |
| `fam_price_at_ceiling_seconds` | counter | `resource` | Time spent at price ceiling |

#### Utilization Metrics

| Name | Type | Labels | Description |
|---|---|---|---|
| `fam_gpu_utilization` | gauge | | Composite GPU utilization (0–1) |
| `fam_gpu_batch_queue_depth` | gauge | | GPU batch queue depth |
| `fam_gpu_kv_cache_occupancy` | gauge | | KV-cache occupancy (0–1) |
| `fam_tool_utilization` | gauge | `endpoint_id` | Composite tool utilization per endpoint (0–1) |
| `fam_tool_queue_depth` | gauge | `endpoint_id` | FAM dispatch queue depth per endpoint |
| `fam_tool_active_calls` | gauge | `endpoint_id` | In-flight calls per endpoint |

#### Confirmation Metrics

| Name | Type | Labels | Description |
|---|---|---|---|
| `fam_confirmations_total` | counter | `decision` | Confirmation outcomes (approve, defer, cancel, timeout) |
| `fam_auto_approvals_total` | counter | | Tool calls auto-approved below threshold |
| `fam_confirmation_response_time_seconds` | histogram | | Time for agent to respond to confirmation |
| `fam_confirmation_rate` | gauge | | Fraction of tool calls requiring confirmation (windowed) |

#### Speculative Continuation Metrics

| Name | Type | Labels | Description |
|---|---|---|---|
| `fam_speculative_sessions_total` | counter | `resolution` | Sessions by resolution (commit, rollback, cancel, timeout) |
| `fam_speculative_tokens_total` | counter | `outcome` | Tokens by outcome (retained, discarded) |
| `fam_speculative_active_sessions` | gauge | | Currently active speculative sessions |
| `fam_speculative_cost_total` | counter | `outcome` | Cumulative speculative cost (retained vs. discarded) |
| `fam_speculative_session_duration_seconds` | histogram | | Session duration distribution |
| `fam_speculative_waste_rate` | gauge | | Fraction of speculative tokens discarded (windowed) |

#### Dispatch Metrics

| Name | Type | Labels | Description |
|---|---|---|---|
| `fam_dispatch_total` | counter | `endpoint_id`, `outcome` | Dispatched calls by outcome (success, error, timeout) |
| `fam_dispatch_latency_seconds` | histogram | `endpoint_id` | Tool call latency distribution |
| `fam_dispatch_queue_wait_seconds` | histogram | `endpoint_id` | Time waiting in dispatch queue |
| `fam_dispatch_retries_total` | counter | `endpoint_id` | Retry attempts |

#### Agent Phase Metrics

| Name | Type | Labels | Description |
|---|---|---|---|
| `fam_agent_phase` | gauge | `agent_id`, `phase` | 1 if agent is in this phase, 0 otherwise |
| `fam_agent_phase_distribution` | gauge | `phase` | Fraction of agents in each phase (reasoning, tool_waiting, speculating, idle) |
| `fam_agent_phase_duration_seconds` | histogram | `phase` | Time spent in each phase |

#### Task Completion Metrics

| Name | Type | Labels | Description |
|---|---|---|---|
| `fam_tasks_completed_total` | counter | `outcome` | Tasks by outcome (success, failure, timeout) |
| `fam_task_duration_seconds` | histogram | | Task completion time distribution |
| `fam_task_tool_calls_total` | counter | `agent_id` | Total tool calls per agent per task |
| `fam_task_tokens_total` | counter | `agent_id` | Total tokens per agent per task |

#### System Health Metrics

| Name | Type | Labels | Description |
|---|---|---|---|
| `fam_telemetry_source_healthy` | gauge | `source_id` | 1 if healthy, 0 if not |
| `fam_telemetry_snapshot_age_seconds` | gauge | | Age of the latest telemetry snapshot |
| `fam_config_hot_reloads_total` | counter | `outcome` | Config hot-reload attempts |
| `fam_uptime_seconds` | gauge | | Orchestrator uptime |

---

## 6. MetricsCollector Class

The `MetricsCollector` is the single entry point for all metric emission. Every module receives a reference to this instance at initialization.

### 6.1 Core Interface

```python
from collections import deque
from typing import Any


class MetricsCollector:
    """Central metric collection and dispatch."""

    def __init__(self, config: "MetricsConfig") -> None:
        self._config = config
        self._event_buffer: deque = deque(maxlen=config.buffer_max_events)
        self._counters: dict[str, dict[tuple, float]] = {}
        self._gauges: dict[str, dict[tuple, float]] = {}
        self._histograms: dict[str, dict[tuple, list[float]]] = {}
        self._time_series: dict[str, list[tuple[float, float]]] = {}
        self._backends: list["MetricsBackend"] = []

    # --- Event emission ---

    def emit_event(self, event: Any) -> None:
        """Record a structured event.

        The event is buffered and dispatched to registered backends
        on the next flush cycle. This call is non-blocking.
        """
        self._event_buffer.append(event)

    # --- Numeric metrics ---

    def increment_counter(
        self,
        name: str,
        value: float = 1.0,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Increment a counter metric."""
        key = tuple(sorted((labels or {}).items()))
        if name not in self._counters:
            self._counters[name] = {}
        self._counters[name][key] = self._counters[name].get(key, 0.0) + value

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Set a gauge metric to a specific value."""
        key = tuple(sorted((labels or {}).items()))
        if name not in self._gauges:
            self._gauges[name] = {}
        self._gauges[name][key] = value

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Record an observation in a histogram metric."""
        key = tuple(sorted((labels or {}).items()))
        if name not in self._histograms:
            self._histograms[name] = {}
        if key not in self._histograms[name]:
            self._histograms[name][key] = []
        self._histograms[name][key].append(value)

    # --- Time series (for eval harness) ---

    def record_time_series(
        self,
        name: str,
        value: float,
        timestamp: float | None = None,
    ) -> None:
        """Append a value to a named time series.

        Used by the evaluation harness for post-hoc analysis and plotting.
        """
        ts = timestamp or time.monotonic()
        if name not in self._time_series:
            self._time_series[name] = []
        self._time_series[name].append((ts, value))

    # --- Backend management ---

    def register_backend(self, backend: "MetricsBackend") -> None:
        """Register a metrics backend (Prometheus exporter, log writer, etc.)."""
        self._backends.append(backend)

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start the flush loop that dispatches buffered data to backends."""

    async def stop(self) -> None:
        """Flush remaining data and stop."""

    async def flush(self) -> None:
        """Immediately flush all buffered data to backends."""
```

### 6.2 Convenience Methods

To reduce boilerplate in calling modules, the collector provides convenience methods for common metric patterns:

```python
def record_tool_dispatch(
    self,
    agent_id: str,
    tool_name: str,
    endpoint_id: str,
    latency_ms: float,
    success: bool,
    queue_depth: int,
    time_in_queue_ms: float,
) -> None:
    """Record a complete tool dispatch cycle — updates all relevant metrics."""
    self.increment_counter("fam_dispatch_total", labels={
        "endpoint_id": endpoint_id,
        "outcome": "success" if success else "error",
    })
    self.observe_histogram("fam_dispatch_latency_seconds", latency_ms / 1000.0, labels={
        "endpoint_id": endpoint_id,
    })
    self.observe_histogram("fam_dispatch_queue_wait_seconds", time_in_queue_ms / 1000.0, labels={
        "endpoint_id": endpoint_id,
    })
    self.set_gauge("fam_tool_queue_depth", float(queue_depth), labels={
        "endpoint_id": endpoint_id,
    })

def record_budget_deduction(
    self,
    agent_id: str,
    amount: float,
    reason: str,
    balance_after: float,
) -> None:
    """Record a budget deduction — updates balance gauge and deduction counter."""
    self.increment_counter("fam_budget_deductions_total", amount, labels={
        "agent_id": agent_id,
        "reason": reason,
    })
    self.set_gauge("fam_budget_balance", balance_after, labels={
        "agent_id": agent_id,
    })

def record_price_update(
    self,
    reasoning_price: float,
    tool_prices: dict[str, float],
    gpu_utilization: float,
    tool_utilizations: dict[str, float],
) -> None:
    """Record a pricing engine update — updates all pricing and utilization gauges."""
    self.set_gauge("fam_price_reasoning", reasoning_price)
    self.set_gauge("fam_gpu_utilization", gpu_utilization)
    for endpoint_id, price in tool_prices.items():
        self.set_gauge("fam_price_tool", price, labels={"endpoint_id": endpoint_id})
    for endpoint_id, util in tool_utilizations.items():
        self.set_gauge("fam_tool_utilization", util, labels={"endpoint_id": endpoint_id})
    if tool_prices:
        avg_tool_price = sum(tool_prices.values()) / len(tool_prices)
        if avg_tool_price > 0:
            self.set_gauge("fam_price_ratio", reasoning_price / avg_tool_price)

    self.record_time_series("reasoning_price", reasoning_price)
    self.record_time_series("gpu_utilization", gpu_utilization)
```

---

## 7. Prometheus Export

FAM exposes metrics in Prometheus text exposition format via an HTTP endpoint. This allows standard Prometheus/Grafana monitoring stacks to scrape FAM metrics.

### 7.1 Exporter

```python
from aiohttp import web


class PrometheusExporter:
    """Serves metrics in Prometheus text format via HTTP."""

    def __init__(
        self,
        collector: MetricsCollector,
        config: "PrometheusConfig",
    ) -> None:
        self._collector = collector
        self._config = config
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        """Start the HTTP server on the configured port."""
        self._app = web.Application()
        self._app.router.add_get(self._config.path, self._handle_metrics)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._config.port)
        await site.start()

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._runner:
            await self._runner.cleanup()

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Render all metrics in Prometheus text format."""
        lines = []
        namespace = self._config.namespace

        for name, label_values in self._collector._counters.items():
            lines.append(f"# TYPE {name} counter")
            for labels, value in label_values.items():
                label_str = self._format_labels(labels)
                lines.append(f"{name}{label_str} {value}")

        for name, label_values in self._collector._gauges.items():
            lines.append(f"# TYPE {name} gauge")
            for labels, value in label_values.items():
                label_str = self._format_labels(labels)
                lines.append(f"{name}{label_str} {value}")

        for name, label_values in self._collector._histograms.items():
            lines.append(f"# TYPE {name} histogram")
            for labels, observations in label_values.items():
                label_str = self._format_labels(labels)
                if observations:
                    lines.append(
                        f"{name}_count{label_str} {len(observations)}"
                    )
                    lines.append(
                        f"{name}_sum{label_str} {sum(observations)}"
                    )

        return web.Response(
            text="\n".join(lines) + "\n",
            content_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @staticmethod
    def _format_labels(labels: tuple) -> str:
        if not labels:
            return ""
        pairs = [f'{k}="{v}"' for k, v in labels]
        return "{" + ",".join(pairs) + "}"
```

### 7.2 Histogram Buckets

Default histogram buckets for different metric categories:

```python
LATENCY_BUCKETS = [
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0
]

DURATION_BUCKETS = [
    0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0
]

TOKEN_BUCKETS = [
    10, 50, 100, 250, 500, 1000, 2000, 5000, 10000
]

PRICE_BUCKETS = [
    0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0
]
```

---

## 8. Structured Logging

Structured logs complement numeric metrics by providing detailed context for individual events. Every structured event emitted via `emit_event()` is written to the log output in a machine-parseable format.

### 8.1 Log Format

The default log format is JSON Lines (one JSON object per line). Each log entry contains the event header fields plus event-specific fields, flattened into a single JSON object.

```python
import json
import logging
import sys


class StructuredLogWriter:
    """Writes structured events as JSON Lines."""

    def __init__(self, config: "LoggingConfig") -> None:
        self._config = config
        self._logger = logging.getLogger("fam.events")
        self._configure_handler()

    def _configure_handler(self) -> None:
        handler: logging.Handler
        if self._config.output == "stderr":
            handler = logging.StreamHandler(sys.stderr)
        elif self._config.output == "stdout":
            handler = logging.StreamHandler(sys.stdout)
        else:
            handler = logging.FileHandler(self._config.output)

        handler.setFormatter(self._JsonFormatter())
        self._logger.addHandler(handler)
        self._logger.setLevel(getattr(logging, self._config.level.upper()))

    def write_event(self, event: object) -> None:
        """Serialize an event to JSON and write to log output."""
        record = self._event_to_dict(event)
        level = self._severity_to_level(record.get("severity", "info"))
        self._logger.log(level, json.dumps(record, default=str))

    @staticmethod
    def _event_to_dict(event: object) -> dict:
        """Flatten a dataclass event (with nested header) into a flat dict."""
        from dataclasses import asdict
        d = asdict(event)
        if "header" in d:
            header = d.pop("header")
            d.update(header)
        return d

    @staticmethod
    def _severity_to_level(severity: str) -> int:
        return {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }.get(severity, logging.INFO)

    class _JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            return record.getMessage()
```

### 8.2 Log Filtering

Not all events need to be logged at all times. The configuration controls which event categories are included:

- `include_pricing_decisions: true` — log `PriceUpdateEvent` (high volume, useful for pricing engine debugging).
- `include_budget_mutations: true` — log `BudgetDeductEvent` and `BudgetReplenishEvent`.
- `include_confirmation_flow: true` — log confirmation request and decision events.

All events are always buffered in the `MetricsCollector` regardless of log filtering. Filtering only affects the structured log output. The evaluation harness always has access to the full event stream.

### 8.3 Text Format (Alternative)

For human-readable debugging, a text format is available:

```
[2026-03-10T14:23:01.123Z] INFO pricing.update reasoning=0.42 tool=3.81 gpu_util=0.67 ratio=0.11
[2026-03-10T14:23:01.456Z] INFO budget.deduct agent=agent_1 amount=3.81 reason=tool_call balance=96.19
[2026-03-10T14:23:02.789Z] INFO confirmation.decision agent=agent_2 tool=search decision=defer confidence=0.95
```

Selected via `metrics.logging.format: "text"` in configuration.

---

## 9. Evaluation Harness Integration

The evaluation harness (spec 14) needs rich time-series data for post-hoc analysis and comparison across experiment configurations. The metrics collector provides this through the `_time_series` store and a dedicated export interface.

### 9.1 Time Series Export

```python
@dataclass(frozen=True)
class TimeSeriesData:
    """Exported time series for analysis."""

    name: str
    timestamps: list[float]
    values: list[float]


class MetricsCollector:
    # ... (continued from above)

    def export_time_series(self, name: str) -> TimeSeriesData | None:
        """Export a named time series for analysis."""
        if name not in self._time_series:
            return None
        points = self._time_series[name]
        return TimeSeriesData(
            name=name,
            timestamps=[t for t, _ in points],
            values=[v for _, v in points],
        )

    def export_all_time_series(self) -> dict[str, TimeSeriesData]:
        """Export all time series."""
        return {
            name: self.export_time_series(name)
            for name in self._time_series
            if self._time_series[name]
        }

    def export_events(
        self,
        event_type: str | None = None,
        agent_id: str | None = None,
    ) -> list[dict]:
        """Export buffered events, optionally filtered by type or agent."""
        from dataclasses import asdict
        results = []
        for event in self._event_buffer:
            d = asdict(event)
            header = d.get("header", {})
            if event_type and header.get("event_type") != event_type:
                continue
            if agent_id and header.get("agent_id") != agent_id:
                continue
            if "header" in d:
                header = d.pop("header")
                d.update(header)
            results.append(d)
        return results

    def export_snapshot(self) -> dict:
        """Export a complete snapshot of all metrics for experiment recording."""
        return {
            "counters": {
                name: {str(k): v for k, v in vals.items()}
                for name, vals in self._counters.items()
            },
            "gauges": {
                name: {str(k): v for k, v in vals.items()}
                for name, vals in self._gauges.items()
            },
            "histogram_counts": {
                name: {str(k): len(obs) for k, obs in vals.items()}
                for name, vals in self._histograms.items()
            },
        }
```

### 9.2 Key Time Series for Experiments

The following time series are automatically recorded and are the primary data sources for the experiments defined in spec 14:

| Time Series Name | Source | Use |
|---|---|---|
| `reasoning_price` | Pricing engine | Price dynamics over time |
| `tool_price.{endpoint_id}` | Pricing engine | Per-endpoint price dynamics |
| `gpu_utilization` | Telemetry collector | GPU load over time |
| `tool_utilization.{endpoint_id}` | Telemetry collector | Per-endpoint load |
| `budget_balance.{agent_id}` | Budget manager | Per-agent budget trajectory |
| `phase_distribution.reasoning` | Agent phase tracker | Fraction of agents reasoning |
| `phase_distribution.tool_waiting` | Agent phase tracker | Fraction waiting for tools |
| `phase_distribution.speculating` | Agent phase tracker | Fraction speculating |
| `confirmation_rate` | Confirmation handler | Confirmation activity over time |
| `speculative_waste_rate` | Speculative manager | Speculative token waste |
| `task_completion_rate` | Task tracker | Tasks completed per unit time |

---

## 10. Dashboard Specification

This section defines the graphs and panels that should be displayed for monitoring FAM during experiments and debugging. The dashboard is specified declaratively — the actual rendering can use Grafana (via the Prometheus exporter), a custom web UI, or matplotlib for evaluation reports.

### 10.1 Overview Dashboard

**Panel 1: Resource Prices Over Time** (line chart, dual Y-axis)
- Left Y: `fam_price_reasoning` (reasoning price per token)
- Right Y: `fam_price_tool` (tool price per call, one line per endpoint)
- X: time

**Panel 2: Resource Utilization Over Time** (line chart)
- `fam_gpu_utilization` (0–1)
- `fam_tool_utilization` per endpoint (0–1)
- Horizontal reference line at congestion threshold (0.7)

**Panel 3: Price-Utilization Correlation** (scatter plot)
- X: `fam_gpu_utilization`, Y: `fam_price_reasoning`
- Shows whether prices respond correctly to utilization changes

**Panel 4: Agent Budget Balances** (multi-line chart)
- One line per agent: `fam_budget_balance{agent_id=...}`
- Horizontal reference lines at soft_limit and hard_limit

### 10.2 Confirmation Dashboard

**Panel 5: Confirmation Decisions** (stacked area chart)
- `fam_confirmations_total` by decision (approve, defer, cancel, timeout)
- Shows how agent decisions shift as prices change

**Panel 6: Confirmation Response Time** (histogram / heatmap)
- `fam_confirmation_response_time_seconds` distribution over time

**Panel 7: Auto-Approve Rate** (line chart)
- `fam_auto_approvals_total / (fam_auto_approvals_total + fam_confirmations_total)`
- Shows what fraction of tool calls bypass confirmation

### 10.3 Speculative Continuation Dashboard

**Panel 8: Speculative Tokens Generated vs. Discarded** (stacked area chart)
- `fam_speculative_tokens_total{outcome="retained"}` (green)
- `fam_speculative_tokens_total{outcome="discarded"}` (red)

**Panel 9: Speculative Waste Rate** (line chart)
- `fam_speculative_waste_rate` (0–1)
- Useful for deciding whether consistency-check reconciliation is worth implementing

**Panel 10: Speculative Session Duration** (histogram)
- `fam_speculative_session_duration_seconds` distribution

### 10.4 Agent Behavior Dashboard

**Panel 11: Agent Phase Distribution** (stacked area chart, 100% stacked)
- `fam_agent_phase_distribution{phase=...}` for reasoning, tool_waiting, speculating, idle
- Shows aggregate agent behavior shifts over time

**Panel 12: Per-Agent Spend Rate** (multi-line chart)
- `fam_budget_spend_rate{agent_id=..., resource="tool_call"}` and `resource="reasoning"`
- Shows individual agent spending patterns

**Panel 13: Task Completion** (line chart + counter)
- `fam_tasks_completed_total` cumulative
- `fam_task_duration_seconds` average over sliding window

### 10.5 System Health Dashboard

**Panel 14: Telemetry Source Health** (status panel)
- `fam_telemetry_source_healthy{source_id=...}` — green/red per source

**Panel 15: Telemetry Snapshot Age** (line chart)
- `fam_telemetry_snapshot_age_seconds` — should be near zero; spikes indicate telemetry lag

**Panel 16: Dispatch Queue Depths** (multi-line chart)
- `fam_tool_queue_depth{endpoint_id=...}` per endpoint

### 10.6 Dashboard as Code

```python
@dataclass(frozen=True)
class PanelSpec:
    """Declarative specification for a dashboard panel."""

    title: str
    panel_type: str              # "line", "scatter", "histogram", "stacked_area", "status"
    metrics: list[str]           # Metric names to display
    labels_filter: dict[str, str] | None = None
    y_axis_label: str = ""
    x_axis_label: str = "Time"
    reference_lines: list[float] | None = None
    stacking: str = "none"       # "none", "normal", "percent"


@dataclass(frozen=True)
class DashboardSpec:
    """Complete dashboard specification."""

    title: str
    panels: list[PanelSpec]
    refresh_interval_seconds: float = 5.0


OVERVIEW_DASHBOARD = DashboardSpec(
    title="FAM Overview",
    panels=[
        PanelSpec(
            title="Resource Prices Over Time",
            panel_type="line",
            metrics=["fam_price_reasoning", "fam_price_tool"],
            y_axis_label="Price",
        ),
        PanelSpec(
            title="Resource Utilization",
            panel_type="line",
            metrics=["fam_gpu_utilization", "fam_tool_utilization"],
            y_axis_label="Utilization (0-1)",
            reference_lines=[0.7],
        ),
        PanelSpec(
            title="Agent Budget Balances",
            panel_type="line",
            metrics=["fam_budget_balance"],
            y_axis_label="Balance",
        ),
        PanelSpec(
            title="Agent Phase Distribution",
            panel_type="stacked_area",
            metrics=["fam_agent_phase_distribution"],
            stacking="percent",
            y_axis_label="Fraction of Agents",
        ),
    ],
)
```

---

## 11. Configuration

All configuration for the metrics and observability system is namespaced under `metrics` in the global FAM configuration (spec 12).

```yaml
metrics:
  enabled: true

  prometheus:
    enabled: true
    port: 9090
    path: "/metrics"
    namespace: "fam"

  logging:
    level: "INFO"
    format: "json"                    # "json" or "text"
    output: "stderr"                  # "stderr", "stdout", or file path
    include_pricing_decisions: true
    include_budget_mutations: true
    include_confirmation_flow: true

  buffer_max_events: 100000
  flush_interval_seconds: 5.0
```

---

## 12. Error Handling

**Event emission failure.** If the event buffer is full (exceeds `buffer_max_events`), the oldest events are evicted silently (deque with maxlen). No exception is raised — metric emission must never interrupt the calling module's operation. A `fam_metrics_events_dropped_total` counter tracks evictions.

**Prometheus endpoint failure.** If the HTTP server fails to start (port in use, permission denied), the error is logged at ERROR level and the orchestrator continues without Prometheus export. Metrics are still collected in-memory and available to the evaluation harness.

**Log write failure.** If the structured log writer encounters an I/O error (disk full, permission denied), the error is logged via Python's root logger (which may also fail, but that is a catastrophic case). Log write failures do not affect metric collection.

**Malformed events.** If a module passes an object that is not a dataclass to `emit_event()`, the collector catches the `TypeError` from `dataclasses.asdict()`, logs a warning, and discards the event. The calling module is not affected.

**Histogram memory.** Histogram observations accumulate in memory. For long-running experiments with high event rates, this could consume significant memory. The collector periodically compacts histograms by computing summary statistics (count, sum, percentiles) and discarding raw observations older than `flush_interval_seconds × 2`. The Prometheus exporter reads from these summaries.

---

## 13. Testing Strategy

### 13.1 Unit Tests

**Counter operations.** Increment a counter, verify the value. Increment with labels, verify label isolation. Increment the same counter from multiple calls, verify cumulative value.

**Gauge operations.** Set a gauge, read it back. Set with different label combinations, verify isolation. Overwrite a gauge, verify new value.

**Histogram operations.** Observe multiple values, verify count and sum. Verify that observations are recorded accurately for known input sequences.

**Event serialization.** Create each event type (PriceUpdateEvent, BudgetDeductEvent, etc.), serialize to JSON, verify all fields are present. Verify that nested EventHeader is flattened correctly.

**Prometheus format.** Render counters, gauges, and histograms to Prometheus text format. Verify output matches the Prometheus exposition format specification. Verify label formatting with special characters (quotes, backslashes).

**Time series recording.** Record a sequence of values, export, verify timestamps and values match. Verify that time series are independent (recording to one does not affect another).

### 13.2 Integration Tests

**End-to-end metric flow.** Start an orchestrator with mock backends, run a single agent through a tool call, verify that all expected metrics are emitted: price update, budget deduction, confirmation, dispatch, tool result. Verify Prometheus endpoint returns these metrics. Verify structured log contains the corresponding events.

**Evaluation harness export.** Run a short experiment, export all time series and events, verify completeness. Verify that event filtering by type and agent_id works correctly. Verify that the metric snapshot contains all expected counters, gauges, and histogram summaries.

**High-throughput event emission.** Emit 100,000 events rapidly, verify that the collector does not block callers. Verify that the event buffer evicts oldest events when full. Verify the `events_dropped` counter is incremented.

**Prometheus scrape under load.** Start the Prometheus endpoint, emit metrics continuously, scrape the endpoint concurrently. Verify that scrapes return consistent data (no partial writes). Verify that scraping does not block metric emission.

### 13.3 Property-Based Tests

Use Hypothesis to generate random sequences of counter increments with random label combinations. Verify that the final counter value equals the sum of all increments for each label combination. Generate random event dataclasses with random field values, serialize to JSON and deserialize, verify round-trip fidelity. Generate random time series with random timestamps, export and verify ordering is preserved.

---

## 14. Dependencies

**Internal:** `fam/config/schema.py` (MetricsConfig). The metrics module is consumed by every other module but depends only on the config schema. Event dataclasses are defined within the metrics module itself — they are not shared types because they are specific to the observability concern.

**External:** `aiohttp` — for the Prometheus HTTP endpoint. This is a well-established async HTTP library already used by LangGraph's ecosystem. If `aiohttp` is not desired, a minimal HTTP server using only `asyncio` and the standard library could be substituted, but `aiohttp` provides cleaner routing and response handling.

**Standard library:** `asyncio`, `dataclasses`, `collections.deque`, `json`, `logging`, `time`, `enum`, `uuid`, `sys`.

---

## 15. Open Questions

**OpenTelemetry integration.** Should FAM export metrics and traces via OpenTelemetry (OTLP) in addition to or instead of the custom Prometheus exporter? OpenTelemetry would provide automatic integration with a broader ecosystem of observability backends (Jaeger, Zipkin, Datadog, etc.) and support distributed tracing if FAM is eventually deployed across multiple processes. The tradeoff is a heavier dependency and more complex configuration. Deferred — the current Prometheus exporter is sufficient for research use.

**Trace IDs across agent conversations.** Each agent's execution could be assigned a trace ID that correlates all events (pricing, budget, confirmation, dispatch, speculation) for that agent's task. This would enable trace-level analysis in addition to metric-level analysis. The `EventHeader.agent_id` provides agent-level correlation today, but a task-level trace ID would be more precise for agents that execute multiple tasks. Deferred.

**Metric cardinality limits.** With many agents and many tool endpoints, the number of unique label combinations can grow large. Prometheus handles high cardinality poorly (memory and CPU cost scale with unique time series count). A cardinality limiter that aggregates per-agent metrics above a configured agent count threshold would help. Deferred — the development/testing environment has a manageable number of agents.

**Real-time dashboard rendering.** The dashboard specification is declarative but no rendering implementation is defined. The initial plan is to use Grafana with the Prometheus data source for real-time monitoring, and matplotlib for evaluation report generation. A built-in web UI could be added later if Grafana is too heavy for lightweight development use. Deferred.

**Cross-correlation metrics.** The meta-spec calls out "cross-correlation between GPU and CPU utilization" as a metric. CPU utilization is not currently collected by the telemetry system (spec 01) because FAM's own CPU usage is a function of asyncio event loop overhead, not a shared resource that agents compete for. If the evaluation harness reveals that FAM's CPU becomes a bottleneck at high agent counts, CPU telemetry should be added. Deferred.
