# Spec 01 — Telemetry Collector

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-09
**Depends On:** Spec 00 (Architecture Overview), Spec 10 (GPU Cluster Interface), Spec 11 (Tool Endpoint Interface)
**Consumed By:** Spec 02 (Pricing Engine), Spec 07 (Tool Dispatch Queue), Spec 08 (Speculative Continuation Manager), Spec 13 (Metrics & Observability)

---

## 1. Purpose

The telemetry collector is FAM's sensory system. It ingests raw resource utilization metrics from two categories of infrastructure — GPU inference clusters and tool endpoints — normalizes them into a consistent schema, smooths out transient noise, and publishes structured snapshots that downstream modules consume to make pricing and scheduling decisions.

Without accurate, timely telemetry, the pricing engine cannot set meaningful prices, the dispatch queue cannot prioritize effectively, and the entire market mechanism collapses into blind guessing. The telemetry collector is therefore the first module that must be built and the last that should be allowed to fail silently.

This spec defines the telemetry sources and their raw metrics, the polling and push ingestion interfaces, the normalized telemetry schema, the smoothing strategy, the snapshot publication mechanism, and the health check and fallback behavior for degraded telemetry conditions.

---

## 2. Module Location

```
fam/
├── telemetry/
│   ├── __init__.py
│   ├── collector.py       # TelemetryCollector class — main entry point
│   ├── snapshot.py        # Dataclasses for raw and smoothed snapshots
│   ├── smoothing.py       # EMA implementation and windowing logic
│   └── health.py          # Source health tracking and staleness detection
```

Tests live in `tests/test_telemetry/`.

---

## 3. Telemetry Sources

FAM monitors two categories of shared resources. Each category has a distinct set of raw metrics, a distinct polling cadence, and distinct failure modes.

### 3.1 GPU Inference Cluster

The GPU inference cluster is the LLM inference backend — a vLLM deployment, a TGI cluster, or any system that serves model inference requests. FAM reads the following metrics from it.

**Batch Queue Depth (`batch_queue_depth: int`):** The number of inference requests currently waiting in the cluster's scheduling queue, not yet assigned to a GPU for execution. A rising queue depth indicates that inference demand is exceeding throughput capacity. This is the primary leading indicator of GPU congestion — it signals future latency increases before they materialize in observed latency.

**KV-Cache Occupancy (`kv_cache_occupancy: float`, range 0.0–1.0):** The fraction of the cluster's aggregate KV-cache memory currently allocated to active sequences. KV-cache exhaustion is a hard bottleneck: when occupancy approaches 1.0, the cluster must either evict active sequences (degrading quality) or reject new requests entirely. This metric captures a resource constraint that queue depth alone does not reveal — a cluster can have a short queue but near-full KV-cache if it is serving many long-context conversations simultaneously.

**Per-Agent Token Generation Rate (`token_rates: dict[str, float]`, tokens/second per agent_id):** The rate at which each agent is consuming inference throughput, measured in tokens generated per second. This is an attribution metric — it tells the system which agents are heavy inference consumers. The pricing engine does not currently use per-agent rates for pricing (prices are global, not per-agent), but the budget manager and metrics system use them for accounting and observability. If the cluster does not support per-agent attribution, this field is an empty dict and per-agent inference cost tracking is disabled.

**Inference Latency (`inference_latency_ms: float`):** The cluster's reported mean time-to-first-token (TTFT) in milliseconds over the most recent reporting window. This is a lagging indicator — by the time latency rises, congestion has already occurred. It is useful for validation (confirming that queue depth signals correlate with actual user-visible degradation) and for the pricing engine's integral term (spec 02), but it should not be the sole driver of pricing because it reacts too slowly.

**Active Request Count (`active_requests: int`):** The number of inference requests currently being processed (not queued, but actively generating tokens on a GPU). Combined with batch queue depth, this gives a complete picture of cluster load: `active_requests` represents current throughput consumption, while `batch_queue_depth` represents excess demand.

These five metrics together capture both the current state (active requests, KV-cache occupancy) and the trajectory (queue depth, latency, token rates) of GPU inference load.

### 3.2 Tool Endpoints

Tool endpoints are external services invoked by agent tools — search APIs, code execution sandboxes, databases, third-party APIs. FAM monitors each registered endpoint independently. The following metrics are collected per endpoint, keyed by `endpoint_id: str`.

**Queue Depth (`queue_depth: int`):** The number of tool call requests currently waiting in FAM's dispatch queue for this endpoint. Unlike GPU queue depth (which is reported by the cluster), tool endpoint queue depth is measured locally by FAM's dispatch queue. This is because external endpoints typically do not expose their internal queue state. FAM's queue depth is a proxy for endpoint load as seen from FAM's perspective.

**Latency Percentiles (`latency_p50_ms: float`, `latency_p90_ms: float`, `latency_p99_ms: float`):** Observed latency percentiles for completed tool calls over a rolling window. These are computed by the telemetry collector from dispatch result timestamps, not reported by the endpoint itself. p50 represents typical latency. p90 represents the latency experienced by the slower-but-not-outlier calls. p99 captures tail latency that affects worst-case agent wait times. The rolling window size is configurable (default: last 100 completed calls or last 60 seconds, whichever is smaller).

**Active Concurrent Calls (`active_calls: int`):** The number of tool calls currently in flight to this endpoint — dispatched but not yet completed. This is the real-time load indicator. Combined with the endpoint's known concurrency limit, it yields a utilization ratio.

**Error Rate (`error_rate: float`, range 0.0–1.0):** The fraction of tool calls to this endpoint that returned an error (HTTP 5xx, timeout, connection refused, etc.) over the same rolling window used for latency percentiles. An elevated error rate signals endpoint degradation that should be reflected in pricing — agents should be discouraged from calling a flaky endpoint at full price.

**Rate Limit Headroom (`rate_limit_headroom: float`, range 0.0–1.0):** The fraction of the endpoint's rate limit that remains available in the current rate limit window. If the endpoint allows 100 requests per minute and 73 have been made, headroom is 0.27. This is computed from FAM's internal rate limit tracker in the dispatch queue (spec 07), not from the endpoint itself (though if the endpoint reports rate limit headers like `X-RateLimit-Remaining`, those are preferred). When headroom approaches 0.0, the pricing engine should make the endpoint very expensive to prevent rate limit violations.

### 3.3 Summary of Raw Metrics

| Source | Metric | Type | Unit | Origin |
|---|---|---|---|---|
| GPU Cluster | `batch_queue_depth` | int | requests | Cluster API |
| GPU Cluster | `kv_cache_occupancy` | float | ratio (0–1) | Cluster API |
| GPU Cluster | `token_rates` | dict[str, float] | tokens/sec per agent | Cluster API |
| GPU Cluster | `inference_latency_ms` | float | milliseconds | Cluster API |
| GPU Cluster | `active_requests` | int | requests | Cluster API |
| Tool Endpoint | `queue_depth` | int | requests | FAM dispatch queue |
| Tool Endpoint | `latency_p50_ms` | float | milliseconds | FAM computation |
| Tool Endpoint | `latency_p90_ms` | float | milliseconds | FAM computation |
| Tool Endpoint | `latency_p99_ms` | float | milliseconds | FAM computation |
| Tool Endpoint | `active_calls` | int | requests | FAM dispatch queue |
| Tool Endpoint | `error_rate` | float | ratio (0–1) | FAM computation |
| Tool Endpoint | `rate_limit_headroom` | float | ratio (0–1) | FAM rate tracker / endpoint headers |

---

## 4. Ingestion Interface

The telemetry collector supports two ingestion modes: **poll** and **push**. Each telemetry source is configured to use one mode. Both modes produce the same raw snapshot dataclass — the downstream pipeline does not know or care which mode was used.

### 4.1 Poll Mode

In poll mode, the telemetry collector actively queries the source on a fixed interval. This is the default mode for GPU clusters (which typically expose a metrics endpoint like `/metrics` or `/health`) and the internal mode for tool endpoints (where FAM computes metrics from its own dispatch records).

The poll loop is an `asyncio` task managed by the `TelemetryCollector`. On each tick, it calls the source's async `get_metrics()` method, which returns a raw metric dict. The collector wraps this in a timestamped raw snapshot and passes it to the normalization and smoothing pipeline.

```python
# Polling interface contract — implemented by GPU cluster and tool endpoint interfaces

class Pollable(Protocol):
    async def get_metrics(self) -> dict[str, Any]:
        """Return current raw metrics as a flat dict.

        Keys and value types must match the source's metric schema.
        Must complete within `poll_timeout` seconds or raise asyncio.TimeoutError.
        Must not raise on transient errors — return a partial dict with
        available metrics and set missing keys to None.
        """
        ...
```

Poll configuration per source:

```python
@dataclass(frozen=True)
class PollConfig:
    interval_seconds: float    # Time between polls. Default: 0.5 for GPU, 1.0 for tools.
    timeout_seconds: float     # Max wait for a single poll call. Default: 2.0.
    max_consecutive_failures: int  # Failures before source is marked unhealthy. Default: 5.
```

The poll interval determines the telemetry collector's temporal resolution. A 0.5-second GPU poll interval means the pricing engine can react to GPU load changes within approximately 1 second (one poll interval plus one pricing engine tick). A shorter interval increases responsiveness but also increases polling load on the cluster's metrics endpoint. The default of 0.5 seconds is a reasonable starting point; it should be tuned based on the cluster's tolerance for metrics queries.

### 4.2 Push Mode

In push mode, the telemetry source delivers metrics to the collector on its own schedule. This is appropriate for systems that emit events or publish to a message bus (e.g., a cluster that pushes metrics to a Redis stream, or a tool endpoint proxy that emits latency events per-call).

The push interface is a callback registration pattern. The telemetry collector exposes a `receive_metrics(source_id: str, metrics: dict[str, Any])` method that sources call whenever new data is available.

```python
# Push interface — sources call this to deliver metrics

class TelemetryReceiver(Protocol):
    async def receive_metrics(
        self,
        source_id: str,
        metrics: dict[str, Any],
        timestamp: float | None = None,
    ) -> None:
        """Accept a pushed metric payload from a source.

        source_id: Unique identifier for the source (e.g., "gpu_cluster_0", "tool_search_api").
        metrics: Raw metric dict matching the source's schema.
        timestamp: When the metrics were measured. If None, uses time of receipt.
        """
        ...
```

Push mode does not replace the poll loop for a source — it supplements it. If a source is configured for push mode, the poll loop for that source is disabled, but the health check timer still runs. If no push arrives within `max_push_silence_seconds` (default: 5.0), the source is marked potentially stale and the collector may fall back to polling or to stale-data behavior (section 9).

### 4.3 Tool Endpoint Self-Reporting

Tool endpoint metrics (queue depth, active calls, latency percentiles, error rate, rate limit headroom) are unusual in that most of them are computed internally by FAM rather than reported by the external endpoint. The dispatch queue (spec 07) is the authoritative source for these metrics.

The telemetry collector does not poll the dispatch queue in the traditional sense. Instead, the dispatch queue implements the push interface: after each tool call completion (success or failure), the dispatch queue pushes an event to the telemetry collector containing the endpoint_id, latency, and outcome. The telemetry collector maintains per-endpoint rolling windows from these events and recomputes aggregate metrics (percentiles, error rate) on each telemetry tick.

For queue depth, active calls, and rate limit headroom, the dispatch queue pushes current values on each tick via a lightweight synchronous read (no I/O involved — these are in-memory counters).

```python
@dataclass(frozen=True)
class ToolCallEvent:
    endpoint_id: str
    started_at: float          # time.monotonic() timestamp
    completed_at: float        # time.monotonic() timestamp
    latency_ms: float          # completed_at - started_at, in ms
    success: bool              # True if tool call returned a result, False on error
    error_type: str | None     # "timeout", "rate_limit", "server_error", "connection", None
```

The collector accumulates these events in a per-endpoint deque (bounded by window size) and derives percentile and error rate metrics from them.

---

## 5. Normalized Telemetry Schema

Raw metrics from different sources arrive in different shapes, at different times, with different units and semantics. The telemetry collector normalizes everything into two canonical snapshot dataclasses — one for GPU telemetry and one for tool endpoint telemetry — plus a combined system-wide snapshot that the pricing engine consumes.

### 5.1 GPU Telemetry Snapshot

```python
@dataclass(frozen=True)
class GPUTelemetrySnapshot:
    """Normalized snapshot of GPU inference cluster state at a point in time."""

    timestamp: float                    # time.monotonic() when snapshot was created

    # Raw (instantaneous) values
    batch_queue_depth: int              # Requests waiting in cluster queue
    kv_cache_occupancy: float           # 0.0–1.0
    active_requests: int                # Requests currently being processed
    inference_latency_ms: float         # Mean TTFT in ms

    # Per-agent attribution (may be empty if cluster doesn't support it)
    token_rates: dict[str, float]       # agent_id → tokens/sec

    # Derived values (computed by collector)
    gpu_utilization: float              # 0.0–1.0, composite utilization score

    # Smoothed values (EMA-smoothed versions of raw values)
    smoothed_queue_depth: float         # EMA of batch_queue_depth
    smoothed_kv_cache_occupancy: float  # EMA of kv_cache_occupancy
    smoothed_latency_ms: float          # EMA of inference_latency_ms
    smoothed_utilization: float         # EMA of gpu_utilization

    # Source health
    source_healthy: bool                # Whether the GPU metrics source is responsive
    staleness_seconds: float            # Time since last successful metric read
```

**`gpu_utilization`** is a composite score computed from the raw metrics. It is not a single hardware metric (like nvidia-smi's GPU utilization percentage) — it is a FAM-defined score that captures the *effective demand pressure* on the inference cluster. The formula is:

```
gpu_utilization = w_q * normalized_queue_depth
               + w_k * kv_cache_occupancy
               + w_a * normalized_active_requests

where:
  normalized_queue_depth = min(batch_queue_depth / queue_depth_capacity, 1.0)
  normalized_active_requests = min(active_requests / max_concurrent_requests, 1.0)

  w_q = 0.4  (queue depth weight — leading indicator, highest weight)
  w_k = 0.35 (KV-cache weight — hard constraint, high weight)
  w_a = 0.25 (active requests weight — current load, moderate weight)
```

The weights and capacity normalization constants (`queue_depth_capacity`, `max_concurrent_requests`) are configurable via spec 12. The defaults above prioritize the leading indicator (queue depth) and the hard constraint (KV-cache) over the steady-state measure (active requests).

Inference latency is deliberately excluded from the composite utilization score. Latency is a lagging indicator — including it in the utilization signal would cause the pricing engine to react after congestion has already manifested, which defeats the purpose of price-based preemption. Latency is included in the snapshot for observability and for the pricing engine's optional integral term, but it does not contribute to the `gpu_utilization` score.

### 5.2 Tool Endpoint Telemetry Snapshot

```python
@dataclass(frozen=True)
class ToolEndpointTelemetrySnapshot:
    """Normalized snapshot of a single tool endpoint's state."""

    timestamp: float
    endpoint_id: str

    # Raw (instantaneous) values
    queue_depth: int                    # Calls waiting in FAM dispatch queue
    active_calls: int                   # Calls currently in flight
    latency_p50_ms: float              # 50th percentile latency over window
    latency_p90_ms: float              # 90th percentile latency over window
    latency_p99_ms: float              # 99th percentile latency over window
    error_rate: float                   # 0.0–1.0 over window
    rate_limit_headroom: float          # 0.0–1.0

    # Derived values
    tool_utilization: float             # 0.0–1.0, composite utilization score

    # Smoothed values
    smoothed_queue_depth: float
    smoothed_active_calls: float
    smoothed_latency_p50_ms: float
    smoothed_error_rate: float
    smoothed_utilization: float

    # Source health
    source_healthy: bool
    staleness_seconds: float
    calls_in_window: int                # Number of calls in the rolling window
```

**`tool_utilization`** is a composite score per endpoint:

```
tool_utilization = w_c * normalized_active_calls
                 + w_h * (1.0 - rate_limit_headroom)
                 + w_e * error_rate_penalty

where:
  normalized_active_calls = min(active_calls / max_concurrent_calls, 1.0)
  error_rate_penalty = min(error_rate / error_rate_critical, 1.0)

  w_c = 0.5  (concurrency weight — primary load indicator)
  w_h = 0.35 (headroom weight — rate limit proximity)
  w_e = 0.15 (error rate weight — degradation signal)
```

Note that `queue_depth` is not included in the composite because it is FAM's internal queue, not the endpoint's. FAM's queue depth reflects FAM's dispatch policy (which may intentionally hold requests back), not the endpoint's actual capacity pressure. However, `queue_depth` is included in the snapshot for observability and for the dispatch queue's own prioritization logic.

### 5.3 System Telemetry Snapshot

The combined snapshot that the pricing engine and other downstream consumers receive:

```python
@dataclass(frozen=True)
class SystemTelemetrySnapshot:
    """Combined telemetry from all sources at a point in time."""

    timestamp: float

    gpu: GPUTelemetrySnapshot
    tools: dict[str, ToolEndpointTelemetrySnapshot]  # endpoint_id → snapshot

    # System-level health
    any_source_unhealthy: bool
    unhealthy_sources: list[str]        # List of source_ids that are unhealthy
```

This is the primary output of the telemetry collector. It is published on every telemetry tick and consumed by the pricing engine (spec 02) to compute `PriceUpdate` values.

---

## 6. Smoothing Strategy

Raw telemetry is noisy. A single poll might catch a transient spike (e.g., a burst of requests that clears within milliseconds) or a transient dip (e.g., a momentary lull between batches). If the pricing engine reacted to every raw fluctuation, prices would oscillate rapidly, confusing agents and preventing stable decision-making.

The telemetry collector applies exponential moving average (EMA) smoothing to all numeric telemetry values. EMA is chosen over simple moving average (SMA) because it weights recent observations more heavily (making it responsive to genuine trend changes) while still dampening short-lived noise. It also requires no window buffer — only the previous smoothed value and the current raw value — making it memory-efficient and trivial to implement.

### 6.1 EMA Definition

For a metric \(x\) with smoothing factor \(\alpha \in (0, 1]\):

$$
\text{EMA}_t = \alpha \cdot x_t + (1 - \alpha) \cdot \text{EMA}_{t-1}
$$

A higher \(\alpha\) means faster response to new data (less smoothing). A lower \(\alpha\) means more smoothing (slower response). The relationship between \(\alpha\) and the effective "half-life" (the number of observations for the influence of a single observation to decay to 50%) is:

$$
\text{half\_life} = \frac{\ln(2)}{\ln(1 / (1 - \alpha))}
$$

Or equivalently, to achieve a desired half-life of \(h\) observations:

$$
\alpha = 1 - \exp\left(\frac{-\ln(2)}{h}\right)
$$

### 6.2 Configurable Smoothing Parameters

Each metric has an independent smoothing factor, allowing different responsiveness for different signals. The defaults are:

| Metric | Default \(\alpha\) | Approx. Half-Life | Rationale |
|---|---|---|---|
| `batch_queue_depth` | 0.3 | ~2 observations | Leading indicator, should be responsive |
| `kv_cache_occupancy` | 0.2 | ~3 observations | Changes slowly in practice, moderate smoothing |
| `active_requests` | 0.3 | ~2 observations | Should track current state fairly closely |
| `inference_latency_ms` | 0.1 | ~7 observations | Lagging indicator, smooth heavily |
| `gpu_utilization` (composite) | 0.25 | ~2.5 observations | Derived from already-smoothed components, light additional smoothing |
| Tool: `active_calls` | 0.3 | ~2 observations | Should be responsive to load changes |
| Tool: `latency_p50_ms` | 0.15 | ~4 observations | Moderate smoothing |
| Tool: `error_rate` | 0.1 | ~7 observations | Smooth heavily to avoid overreacting to single errors |
| Tool: `rate_limit_headroom` | 0.4 | ~1.5 observations | Fast response needed as rate limit is a hard wall |
| Tool: `tool_utilization` (composite) | 0.25 | ~2.5 observations | Light additional smoothing on derived value |

These are all configurable through the configuration system (spec 12). The metric identifiers used in configuration match the field names above.

### 6.3 Implementation

```python
class EMAState:
    """Maintains EMA state for a single numeric metric."""

    __slots__ = ("alpha", "value", "initialized")

    def __init__(self, alpha: float) -> None:
        if not (0.0 < alpha <= 1.0):
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        self.alpha: float = alpha
        self.value: float = 0.0
        self.initialized: bool = False

    def update(self, raw: float) -> float:
        """Incorporate a new raw observation and return the updated EMA value."""
        if not self.initialized:
            self.value = raw
            self.initialized = True
        else:
            self.value = self.alpha * raw + (1.0 - self.alpha) * self.value
        return self.value

    def reset(self) -> None:
        """Clear state. Next update will initialize from scratch."""
        self.initialized = False
        self.value = 0.0
```

The telemetry collector maintains an `EMAState` instance per metric per source. On each telemetry tick, each raw metric value is passed through its corresponding `EMAState.update()`, and the returned smoothed value is written into the snapshot.

### 6.4 Time-Corrected EMA

The basic EMA formula assumes observations arrive at regular intervals. If the poll interval varies (due to system load, GC pauses, or push-mode sources that deliver irregularly), the effective smoothing changes — longer gaps between observations should decay the previous value more than shorter gaps.

The telemetry collector implements time-corrected EMA for sources that may deliver at irregular intervals:

$$
\alpha_{\text{adjusted}} = 1 - (1 - \alpha)^{\Delta t / \Delta t_{\text{expected}}}
$$

where \(\Delta t\) is the actual time since the last observation and \(\Delta t_{\text{expected}}\) is the configured poll interval. When \(\Delta t = \Delta t_{\text{expected}}\), this reduces to the base \(\alpha\). When \(\Delta t > \Delta t_{\text{expected}}\) (observation arrived late), the adjusted \(\alpha\) is larger (more weight on the new observation, since the old one is stale). When \(\Delta t < \Delta t_{\text{expected}}\), the adjusted \(\alpha\) is smaller (less weight, since less time has passed).

This correction is applied automatically. The `EMAState` class has a `time_corrected_update(raw, dt, expected_dt)` method used when the collector has timing information.

```python
def time_corrected_update(
    self, raw: float, dt: float, expected_dt: float
) -> float:
    """EMA update with time-correction for irregular observation intervals."""
    if not self.initialized:
        self.value = raw
        self.initialized = True
    else:
        if expected_dt <= 0 or dt <= 0:
            adjusted_alpha = self.alpha
        else:
            adjusted_alpha = 1.0 - (1.0 - self.alpha) ** (dt / expected_dt)
        self.value = adjusted_alpha * raw + (1.0 - adjusted_alpha) * self.value
    return self.value
```

---

## 7. Snapshot Publication

The telemetry collector publishes a `SystemTelemetrySnapshot` on every telemetry tick. Downstream consumers access the snapshot through one of two mechanisms: direct read of a shared reference, or async callback notification.

### 7.1 Shared State (Primary Mechanism)

The `TelemetryCollector` holds the latest snapshot as a mutable attribute:

```python
class TelemetryCollector:
    latest_snapshot: SystemTelemetrySnapshot | None
```

Downstream consumers (primarily the pricing engine) read `collector.latest_snapshot` whenever they need current telemetry. Because FAM uses single-threaded `asyncio`, there is no data race — the snapshot reference is updated atomically (Python object reference assignment is atomic in CPython, and even under alternative interpreters, there is no concurrent reader thread).

This is the simplest and most efficient publication mechanism. The pricing engine reads the latest snapshot on its own tick, which may or may not coincide with the telemetry tick. If the pricing tick is faster than the telemetry tick, it may read the same snapshot twice (this is harmless — the pricing engine is idempotent on unchanged input). If the pricing tick is slower, it may skip snapshots (this is also harmless — it always sees the most recent one).

### 7.2 Callback Notification (Secondary Mechanism)

For consumers that need to react immediately when new telemetry arrives (rather than polling on their own schedule), the collector supports an async callback registration pattern:

```python
SnapshotCallback = Callable[[SystemTelemetrySnapshot], Awaitable[None]]

class TelemetryCollector:
    _callbacks: list[SnapshotCallback]

    def on_snapshot(self, callback: SnapshotCallback) -> None:
        """Register a callback to be invoked on every new snapshot."""
        self._callbacks.append(callback)

    def remove_callback(self, callback: SnapshotCallback) -> None:
        """Unregister a previously registered callback."""
        self._callbacks.remove(callback)
```

After publishing a new snapshot to `latest_snapshot`, the collector iterates `_callbacks` and awaits each one. Callbacks must complete quickly (ideally sub-millisecond) to avoid delaying the next poll cycle. If a callback raises an exception, the collector logs the error and continues to the next callback — a faulty consumer must not break telemetry for the rest of the system.

Callbacks are invoked in registration order. There is no priority mechanism. If ordering matters, register in the desired order.

### 7.3 Snapshot History (Optional, Bounded)

For observability and debugging, the collector optionally maintains a bounded history of recent snapshots:

```python
class TelemetryCollector:
    snapshot_history: deque[SystemTelemetrySnapshot]  # maxlen from config
```

The default `maxlen` is 600 (5 minutes at 2 snapshots/second). This history is read-only for external consumers and is primarily used by the metrics and observability system (spec 13) to render time-series charts and detect trends. It is not used by the pricing engine or any real-time decision path.

---

## 8. TelemetryCollector Class

The `TelemetryCollector` is the central class of this module. It manages source registration, poll loop scheduling, push reception, snapshot assembly, smoothing, health tracking, publication, and lifecycle.

### 8.1 Initialization

```python
class TelemetryCollector:
    def __init__(self, config: TelemetryConfig) -> None:
        """
        Args:
            config: Telemetry configuration (intervals, smoothing params, etc.)
                    Loaded from the global FAM config (spec 12).
        """
```

The constructor does not start any async tasks. It initializes EMA state, creates empty snapshot slots, and prepares the callback list. Async work begins when `start()` is called.

### 8.2 Source Registration

Sources are registered before the collector is started. Each source has a unique `source_id`, a type (`"gpu"` or `"tool"`), and either a `Pollable` reference (for poll mode) or nothing (for push mode, where the source will call `receive_metrics` directly).

```python
async def register_gpu_source(
    self,
    source_id: str,
    source: Pollable,
    poll_config: PollConfig | None = None,
) -> None:
    """Register a GPU cluster as a telemetry source."""

async def register_tool_source(
    self,
    endpoint_id: str,
    source: Pollable | None = None,
    poll_config: PollConfig | None = None,
    max_concurrent_calls: int = 10,
) -> None:
    """Register a tool endpoint as a telemetry source.

    If source is None, the endpoint is push-only (dispatch queue
    will push ToolCallEvent instances).
    max_concurrent_calls is used to normalize active_calls into utilization.
    """
```

Only one GPU source is supported in the initial implementation. Multiple tool endpoint sources are supported, each identified by `endpoint_id`.

### 8.3 Lifecycle

```python
async def start(self) -> None:
    """Start the telemetry collection loop.

    Launches one asyncio task for the main poll loop and one
    per-source poll task (for poll-mode sources). Must be called
    after all sources are registered.
    """

async def stop(self) -> None:
    """Stop all collection tasks and clean up.

    Cancels running tasks and waits for them to finish.
    After stop(), the collector can be restarted with start().
    """
```

The main loop runs at the fastest configured poll interval (typically the GPU poll interval, 0.5s). On each iteration:

1. For each poll-mode source, issue an async poll with timeout.
2. Collect results (or timeout/error markers).
3. For each push-mode source, check if a new push has arrived since the last tick. If yes, use the latest pushed data. If no, use the previous data and increment staleness.
4. For tool endpoints, consume any queued `ToolCallEvent`s and update rolling windows.
5. Compute derived metrics (composite utilization scores).
6. Apply EMA smoothing to all metrics.
7. Assemble `GPUTelemetrySnapshot`, per-endpoint `ToolEndpointTelemetrySnapshot`s, and `SystemTelemetrySnapshot`.
8. Update health status for all sources.
9. Publish: set `latest_snapshot`, append to `snapshot_history`, invoke callbacks.
10. Emit telemetry tick metrics to the metrics system (spec 13).

### 8.4 Push Reception

```python
async def receive_metrics(
    self,
    source_id: str,
    metrics: dict[str, Any],
    timestamp: float | None = None,
) -> None:
    """Accept pushed metrics from a source.

    Thread-safe within asyncio (not thread-safe across OS threads).
    Stores the metrics in a per-source buffer. The main loop will
    consume them on the next tick.
    """

async def receive_tool_call_event(self, event: ToolCallEvent) -> None:
    """Accept a tool call completion event from the dispatch queue.

    Appends to the per-endpoint rolling window deque.
    """
```

Pushed metrics are buffered (one slot per source — only the latest push is kept). The main loop consumes the buffer on each tick. This means push-mode sources are still sampled at the main loop frequency, but with zero polling overhead — the data is already waiting.

### 8.5 Public Read API

```python
@property
def latest(self) -> SystemTelemetrySnapshot | None:
    """The most recent published snapshot, or None if no snapshot yet."""

def get_gpu_snapshot(self) -> GPUTelemetrySnapshot | None:
    """Convenience: latest GPU telemetry."""

def get_tool_snapshot(self, endpoint_id: str) -> ToolEndpointTelemetrySnapshot | None:
    """Convenience: latest telemetry for a specific tool endpoint."""

def get_all_tool_snapshots(self) -> dict[str, ToolEndpointTelemetrySnapshot]:
    """Convenience: latest telemetry for all tool endpoints."""

def is_healthy(self) -> bool:
    """True if all registered sources are healthy."""

def unhealthy_sources(self) -> list[str]:
    """List of source_ids that are currently unhealthy."""
```

---

## 9. Health Checks and Fallback Behavior

Telemetry sources can fail. The GPU cluster's metrics endpoint might go down. A tool endpoint might become unreachable. The telemetry collector must detect these failures and degrade gracefully rather than propagating garbage data to the pricing engine.

### 9.1 Health States

Each telemetry source is in one of three states at any time:

**Healthy:** The source has delivered data within the expected interval. The most recent data is fresh and complete. All computed metrics use live data.

**Stale:** The source has not delivered data for longer than `staleness_threshold_seconds` (default: 3× the poll interval) but less than `unhealthy_threshold_seconds` (default: 10× the poll interval). The most recent data is still used, but the snapshot's `staleness_seconds` field reflects the age, and the `source_healthy` flag is set to `False`. The pricing engine should treat stale data with reduced confidence — spec 02 defines how.

**Unhealthy:** The source has not delivered data for longer than `unhealthy_threshold_seconds`, OR has exceeded `max_consecutive_failures` poll attempts. The collector stops using the source's data entirely and substitutes fallback values.

```python
class SourceHealthState(str, Enum):
    HEALTHY = "healthy"
    STALE = "stale"
    UNHEALTHY = "unhealthy"
```

State transitions are tracked by the `SourceHealthTracker` class in `fam/telemetry/health.py`:

```python
class SourceHealthTracker:
    def __init__(
        self,
        source_id: str,
        staleness_threshold: float,
        unhealthy_threshold: float,
        max_consecutive_failures: int,
    ) -> None: ...

    def record_success(self, timestamp: float) -> None:
        """Record a successful metric retrieval."""

    def record_failure(self, timestamp: float, error: Exception) -> None:
        """Record a failed metric retrieval attempt."""

    def check(self, current_time: float) -> SourceHealthState:
        """Return current health state based on time and failure history."""

    @property
    def staleness_seconds(self) -> float:
        """Seconds since last successful retrieval."""

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive failed poll attempts."""
```

### 9.2 Fallback Values

When a source is unhealthy, the collector substitutes conservative fallback values designed to make the pricing engine err on the side of caution (higher prices, discouraging resource use) rather than optimism (lower prices, encouraging use of a potentially broken resource).

**GPU cluster fallback values:**

| Metric | Fallback Value | Rationale |
|---|---|---|
| `batch_queue_depth` | `queue_depth_capacity` (max) | Assume full congestion |
| `kv_cache_occupancy` | 0.9 | Near-full, but not 1.0 to avoid hard-block semantics |
| `active_requests` | `max_concurrent_requests` | Assume fully loaded |
| `inference_latency_ms` | Last known value × 2.0 | Pessimistic extrapolation |
| `gpu_utilization` | 0.9 | High but not maximum |

**Tool endpoint fallback values (per unhealthy endpoint):**

| Metric | Fallback Value | Rationale |
|---|---|---|
| `queue_depth` | Last known value (from FAM's queue — still available) | FAM's queue is local, always available |
| `active_calls` | Last known value | Conservative |
| `latency_p50_ms` | Last known value × 2.0 | Pessimistic extrapolation |
| `error_rate` | 0.5 | High error rate discourages use |
| `rate_limit_headroom` | 0.1 | Near rate limit, strongly discourages use |
| `tool_utilization` | 0.85 | High but allows essential calls through |

The fallback multiplier (2.0 for latency, 0.5 for error rate, etc.) and the fixed fallback values are configurable through spec 12.

### 9.3 Recovery

When an unhealthy source starts responding again, it transitions back to healthy after `recovery_success_count` (default: 2) consecutive successful polls. The EMA state for that source is reset on recovery to prevent the stale smoothed values from dominating the new data. The first successful post-recovery poll initializes the EMA from scratch (as if the source were just registered).

The recovery path is: Unhealthy → (first successful poll) → Stale → (second successful poll) → Healthy. This two-step recovery prevents a single lucky response from immediately declaring a flaky source healthy.

### 9.4 Health Change Events

Health state transitions are emitted as events to the metrics system (spec 13):

```python
@dataclass(frozen=True)
class SourceHealthEvent:
    source_id: str
    previous_state: SourceHealthState
    new_state: SourceHealthState
    timestamp: float
    consecutive_failures: int
    staleness_seconds: float
    last_error: str | None         # String representation of most recent error
```

These events enable alerting and dashboarding on telemetry health.

---

## 10. Rolling Window for Tool Endpoint Metrics

Latency percentiles and error rates for tool endpoints are computed from a rolling window of recent `ToolCallEvent`s. The window is bounded by both count and time.

### 10.1 Window Parameters

```python
@dataclass(frozen=True)
class RollingWindowConfig:
    max_events: int = 100          # Maximum events retained in window
    max_age_seconds: float = 60.0  # Events older than this are evicted
```

On each telemetry tick, the collector evicts events older than `max_age_seconds` from the front of each endpoint's deque, then truncates to `max_events` if still over capacity (keeping the most recent events).

### 10.2 Percentile Computation

Latency percentiles are computed from the `latency_ms` field of events in the window using the nearest-rank method. For a window with \(n\) events, the \(p\)-th percentile is the value at index \(\lceil p \cdot n / 100 \rceil - 1\) in the sorted latency array.

If the window contains fewer than 5 events, percentile values are reported as the maximum observed latency (pessimistic) rather than attempting statistical inference from too few samples. The `calls_in_window` field in the snapshot allows downstream consumers to gauge confidence.

### 10.3 Error Rate Computation

Error rate is the simple ratio of `sum(not event.success for event in window) / len(window)`. If the window is empty (no recent calls to this endpoint), error rate defaults to 0.0 (optimistic) because the absence of calls does not indicate errors. However, if the window is empty because the endpoint was previously returning errors and callers stopped trying, this optimistic default is acceptable — the pricing engine will have already raised the price during the error period, and callers will naturally probe the endpoint as its price eventually decays.

---

## 11. Configuration

All configuration for the telemetry collector is namespaced under `telemetry` in the global FAM configuration (spec 12). The full schema:

```yaml
telemetry:
  # Main loop interval — the tick rate for snapshot publication
  tick_interval_seconds: 0.5

  # GPU source configuration
  gpu:
    poll_interval_seconds: 0.5
    poll_timeout_seconds: 2.0
    max_consecutive_failures: 5
    staleness_threshold_seconds: 1.5    # 3× poll interval
    unhealthy_threshold_seconds: 5.0    # 10× poll interval
    recovery_success_count: 2

    # Normalization capacities for composite utilization
    queue_depth_capacity: 64
    max_concurrent_requests: 128

    # Composite utilization weights (must sum to 1.0)
    utilization_weights:
      queue_depth: 0.4
      kv_cache: 0.35
      active_requests: 0.25

    # EMA smoothing alphas
    smoothing:
      batch_queue_depth: 0.3
      kv_cache_occupancy: 0.2
      active_requests: 0.3
      inference_latency_ms: 0.1
      gpu_utilization: 0.25

    # Fallback values when source is unhealthy
    fallback:
      kv_cache_occupancy: 0.9
      gpu_utilization: 0.9
      latency_multiplier: 2.0

  # Tool endpoint defaults (per-endpoint overrides possible)
  tools:
    poll_interval_seconds: 1.0
    poll_timeout_seconds: 2.0
    max_consecutive_failures: 5
    staleness_threshold_seconds: 3.0
    unhealthy_threshold_seconds: 10.0
    recovery_success_count: 2

    # Default max concurrent calls (used for utilization normalization)
    default_max_concurrent_calls: 10

    # Composite utilization weights (must sum to 1.0)
    utilization_weights:
      active_calls: 0.5
      rate_limit_headroom: 0.35
      error_rate: 0.15

    # EMA smoothing alphas
    smoothing:
      active_calls: 0.3
      latency_p50_ms: 0.15
      error_rate: 0.1
      rate_limit_headroom: 0.4
      tool_utilization: 0.25

    # Rolling window for latency/error computation
    rolling_window:
      max_events: 100
      max_age_seconds: 60.0

    # Fallback values when source is unhealthy
    fallback:
      error_rate: 0.5
      rate_limit_headroom: 0.1
      tool_utilization: 0.85
      latency_multiplier: 2.0

    # Per-endpoint overrides (optional)
    endpoints:
      # Example:
      # search_api:
      #   max_concurrent_calls: 20
      #   smoothing:
      #     active_calls: 0.4

  # Snapshot history
  history:
    enabled: true
    max_snapshots: 600
```

---

## 12. Error Handling

The telemetry collector is designed to never crash and to never propagate exceptions to callers. Every failure is caught, logged, counted, and handled.

**Poll failures:** If a `get_metrics()` call raises an exception or times out, the failure is recorded in the `SourceHealthTracker`. The previous snapshot data is retained (with incremented staleness). The exception is logged at WARNING level. No retry is attempted within the same tick — the next scheduled poll serves as the retry.

**Push failures:** If `receive_metrics()` is called with invalid data (wrong types, missing keys), the data is rejected, an error is logged, and no state is mutated. The push call returns without raising — the pusher is not punished for bad data, but the data is not used.

**Callback failures:** If a snapshot callback raises an exception, it is caught and logged at ERROR level. The callback is not removed (transient errors should not cause permanent de-registration). If a callback fails more than `max_callback_failures` (default: 10) consecutive times, it is automatically de-registered and a warning is logged.

**Smoothing edge cases:** If a raw metric is `None` (because the source returned partial data), the EMA state is not updated for that metric — the previous smoothed value is retained. If a raw metric is negative or otherwise invalid (e.g., negative latency), it is clamped to the valid range before smoothing.

**Clock issues:** The collector uses `time.monotonic()` for all timing, never `time.time()`. Monotonic time is immune to wall-clock adjustments (NTP jumps, DST changes, manual clock sets) that could cause negative time deltas and break EMA time-correction.

---

## 13. Metrics Emitted

The telemetry collector emits the following metrics to the observability system (spec 13) on each tick:

**Counters:** `telemetry.polls.total` (per source_id, per outcome: success/failure/timeout), `telemetry.pushes.received` (per source_id), `telemetry.events.tool_call` (per endpoint_id), `telemetry.callbacks.invoked`, `telemetry.callbacks.failed`.

**Gauges:** `telemetry.source.staleness_seconds` (per source_id), `telemetry.source.health_state` (per source_id, encoded as 0=healthy, 1=stale, 2=unhealthy), `telemetry.snapshot.age_seconds` (age of current published snapshot), `telemetry.tool_window.size` (per endpoint_id, number of events in rolling window).

**Histograms:** `telemetry.poll.duration_ms` (per source_id, time spent in each poll call), `telemetry.tick.duration_ms` (total time to execute one main loop tick, including all polls, smoothing, and publication).

These metrics enable monitoring the telemetry system's own health — if `telemetry.tick.duration_ms` approaches `tick_interval_seconds`, the collector is falling behind and the tick interval should be increased or the number of sources reduced.

---

## 14. Testing Strategy

### 14.1 Unit Tests

**EMA correctness:** Verify that `EMAState.update()` produces mathematically correct EMA values for known input sequences. Test edge cases: first observation (should return raw value), alpha=1.0 (should always return raw value), alpha approaching 0 (should barely move).

**Time-corrected EMA:** Verify that `time_corrected_update()` with `dt == expected_dt` produces the same result as `update()`. Verify that `dt > expected_dt` increases effective alpha. Verify that `dt < expected_dt` decreases effective alpha.

**Composite utilization:** Verify that `gpu_utilization` and `tool_utilization` are computed correctly from component metrics with the configured weights. Verify clamping to [0.0, 1.0]. Verify normalization against capacity values.

**Percentile computation:** Test with known data sets. Verify p50 returns median. Verify behavior with fewer than 5 events (should return max). Verify with exactly 1 event. Verify with an empty window.

**Rolling window eviction:** Verify that events older than `max_age_seconds` are evicted. Verify that excess events beyond `max_events` are evicted (oldest first). Verify that eviction does not corrupt the deque.

### 14.2 Integration Tests

**Poll loop lifecycle:** Start collector with a mock GPU source, verify snapshots are published at the configured interval, stop collector, verify tasks are cleaned up.

**Push reception:** Register a push-mode source, push metrics, verify they appear in the next snapshot.

**Health state transitions:** Register a poll-mode source, make it fail repeatedly, verify transition from Healthy → Stale → Unhealthy. Make it recover, verify Unhealthy → Stale → Healthy. Verify fallback values are used during unhealthy periods. Verify EMA reset on recovery.

**Callback mechanism:** Register a callback, publish a snapshot, verify callback was invoked with the correct snapshot. Register a failing callback, verify it is de-registered after max failures.

**Multi-source snapshot assembly:** Register one GPU source and three tool endpoints, publish metrics from all, verify the `SystemTelemetrySnapshot` contains all sources. Mark one source unhealthy, verify `any_source_unhealthy` is True and `unhealthy_sources` lists the correct source.

### 14.3 Property-Based Tests

Use Hypothesis to generate random sequences of raw metric values and verify that EMA output is always within the range [min(inputs), max(inputs)] (EMA cannot exceed the range of its inputs). Verify that composite utilization is always in [0.0, 1.0] regardless of input metric values. Verify that error rate is always in [0.0, 1.0] for any combination of success/failure events.

---

## 15. Dependencies

**Internal:** `fam/types.py` (shared types), `fam/config/schema.py` (configuration schema).

**External:** None. The telemetry collector uses only the Python standard library (`asyncio`, `collections.deque`, `dataclasses`, `time`, `math`, `logging`, `enum`). It does not import LangGraph, LangChain, or any third-party libraries. This is intentional — the telemetry layer should be maximally portable and testable without external dependencies.

**Interface contracts:** The collector depends on the `Pollable` protocol being implemented by GPU cluster interfaces (spec 10) and optionally by tool endpoint interfaces (spec 11). It also depends on the dispatch queue (spec 07) pushing `ToolCallEvent`s. These are protocol-based dependencies — no concrete implementation is imported.

---

## 16. Open Questions

**Adaptive smoothing.** Should the EMA alpha be dynamically adjusted based on the rate of change in the underlying signal? When a metric is changing rapidly, higher alpha (more responsiveness) may be appropriate. When it is stable, lower alpha (more smoothing) may be better. This is a potential enhancement but adds complexity. The initial implementation uses fixed alphas.

**Multiple GPU clusters.** The current design supports a single GPU cluster source. If FAM is deployed against a heterogeneous inference backend (e.g., different model sizes on different GPU pools), each pool would need separate telemetry, separate pricing, and separate utilization scores. The schema can accommodate this by making the GPU snapshot a dict keyed by cluster_id (mirroring the tool endpoint pattern), but this is deferred to a future version.

**External telemetry sinks.** Should the collector publish snapshots to an external sink (e.g., Prometheus push gateway, OpenTelemetry collector) in addition to the in-memory mechanisms? This would enable historical analysis beyond the bounded in-memory history. Deferred — the metrics system (spec 13) is the right place for external export, not the telemetry collector itself.
