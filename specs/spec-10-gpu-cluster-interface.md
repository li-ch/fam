# Spec 10 — GPU Inference Cluster Interface

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview)
**Consumed By:** Spec 01 (Telemetry Collector), Spec 13 (Metrics & Observability), Spec 14 (Evaluation Harness)

---

## 1. Purpose

The GPU inference cluster interface is the boundary between FAM's orchestration logic and the LLM inference backend. FAM does not run inference directly — it reads metrics from whatever system does. This spec defines the abstract interface that all GPU cluster backends must implement, the concrete telemetry that the interface exposes, and a mock implementation that generates realistic synthetic telemetry for development, testing, and evaluation.

The abstraction exists so that FAM can target different inference serving systems — vLLM, TGI (Text Generation Inference), a managed LLM API, or a fully simulated backend — without changing any orchestration code. The mock implementation is not a toy: it is the primary backend for the evaluation harness (spec 14) and must generate telemetry patterns that are realistic enough to produce meaningful experimental results. The mock must simulate queue buildup under load, KV-cache pressure from concurrent long-context conversations, latency distributions that shift with utilization, and burst arrival patterns.

This spec defines the abstract protocol, the expected metrics and their semantics, the ingestion methods available (REST polling, Prometheus scraping, log stream parsing), the mock implementation with its configurable scenarios, and the configuration surface.

---

## 2. Module Location

```
fam/
├── interfaces/
│   ├── __init__.py
│   ├── gpu_cluster.py       # GPUClusterInterface abstract protocol
│   ├── gpu_mock.py          # MockGPUCluster — simulated backend
│   ├── gpu_vllm.py          # vLLM adapter (reads /metrics endpoint)
│   └── gpu_tgi.py           # TGI adapter (reads /health and /metrics)
```

Tests live in `tests/test_interfaces/`.

---

## 3. Abstract Interface

The GPU cluster interface is defined as a Python `Protocol` class. Any backend — real or mock — must implement this protocol to be usable as a telemetry source for the telemetry collector (spec 01).

### 3.1 Protocol Definition

```python
class GPUClusterInterface(Protocol):
    """Abstract interface to a GPU inference serving backend.

    Implementations must provide current metrics on demand (poll model)
    or push metrics to a registered receiver (push model). At minimum,
    the poll model (get_metrics) must be implemented.
    """

    async def get_metrics(self) -> dict[str, Any]:
        """Return current cluster metrics as a flat dict.

        Required keys:
            batch_queue_depth: int
            kv_cache_occupancy: float     (0.0–1.0)
            active_requests: int
            inference_latency_ms: float
            token_rates: dict[str, float] (agent_id → tokens/sec, may be empty)

        Must complete within the configured poll timeout.
        Must not raise on transient errors — return partial dict with
        available metrics and set missing keys to None.
        """
        ...

    async def health_check(self) -> bool:
        """Return True if the cluster is reachable and serving.

        Should be a lightweight check (e.g., HTTP HEAD to the metrics
        endpoint) that completes quickly even under cluster load.
        """
        ...

    @property
    def cluster_id(self) -> str:
        """Unique identifier for this cluster instance."""
        ...

    @property
    def capabilities(self) -> ClusterCapabilities:
        """Static capabilities and capacity limits of this cluster."""
        ...

    async def start(self) -> None:
        """Initialize the interface (open connections, start background tasks)."""
        ...

    async def stop(self) -> None:
        """Tear down the interface (close connections, cancel tasks)."""
        ...
```

### 3.2 Cluster Capabilities

Static metadata about the cluster's capacity, used by the telemetry collector for normalizing raw metrics into utilization scores.

```python
@dataclass(frozen=True)
class ClusterCapabilities:
    """Static capacity metadata for a GPU inference cluster."""

    max_concurrent_requests: int       # Maximum simultaneous inference requests
    queue_depth_capacity: int          # Queue depth at which cluster is "full"
    total_kv_cache_bytes: int          # Total KV-cache memory in bytes
    max_tokens_per_second: float       # Peak aggregate throughput
    supports_per_agent_tracking: bool  # Whether token_rates is populated
    model_id: str                      # Served model identifier
    gpu_count: int                     # Number of GPUs in the cluster
    gpu_type: str                      # GPU model (e.g., "A100-80GB", "H100")
```

These values are read once at startup and assumed static for the lifetime of the FAM process. If the cluster is scaled (GPUs added or removed), FAM must be restarted or the configuration updated.

---

## 4. Metric Semantics

This section defines the precise semantics of each metric the interface returns. These semantics are the contract between the interface implementation and the telemetry collector — every backend must conform to them.

### 4.1 batch_queue_depth

**Type:** `int`
**Unit:** number of requests
**Semantics:** The count of inference requests that have been submitted to the cluster but are not yet being actively processed. These requests are waiting for a GPU slot to become available. A request transitions from "queued" to "active" when the cluster begins allocating KV-cache memory and generating tokens for it.

**vLLM mapping:** `vllm:num_requests_waiting` Prometheus metric.
**TGI mapping:** `tgi_queue_size` Prometheus metric.
**Mock mapping:** Simulated based on arrival rate minus processing rate.

### 4.2 kv_cache_occupancy

**Type:** `float`
**Range:** 0.0–1.0
**Semantics:** The fraction of total KV-cache memory currently allocated to active and queued sequences. This includes memory reserved for sequences that are in the queue but have pre-allocated KV-cache slots (vLLM pre-allocates on admission). A value of 1.0 means no more sequences can be admitted without eviction.

**vLLM mapping:** `vllm:gpu_cache_usage_perc` Prometheus metric.
**TGI mapping:** Not directly exposed; estimated from `tgi_batch_current_size × estimated_kv_per_sequence / total_kv_cache_bytes`.
**Mock mapping:** Simulated as a function of active sequence count and average sequence length.

### 4.3 active_requests

**Type:** `int`
**Unit:** number of requests
**Semantics:** The count of inference requests currently being processed — the cluster is actively generating tokens for these requests on GPUs. This does not include queued requests.

**vLLM mapping:** `vllm:num_requests_running` Prometheus metric.
**TGI mapping:** `tgi_batch_current_size` Prometheus metric.
**Mock mapping:** Simulated based on processing capacity and current load.

### 4.4 inference_latency_ms

**Type:** `float`
**Unit:** milliseconds
**Semantics:** The mean time-to-first-token (TTFT) for requests completed in the most recent reporting window (typically the last 10 seconds or the last 100 requests, whichever is smaller). TTFT is the time from when a request was submitted to the cluster to when the first token of the response was generated. This measures queuing + scheduling + prefill latency, but not per-token generation latency.

**vLLM mapping:** Derived from `vllm:e2e_request_latency_seconds` histogram (p50 or mean of recent bucket).
**TGI mapping:** Derived from `tgi_request_duration` histogram.
**Mock mapping:** Simulated with a base latency plus load-dependent queuing delay.

### 4.5 token_rates

**Type:** `dict[str, float]`
**Unit:** tokens per second, keyed by agent_id
**Semantics:** The per-agent token generation throughput over the most recent reporting window. If the cluster cannot attribute tokens to specific agents (because the cluster does not have agent_id awareness), this dict is empty. An empty dict does not indicate an error — it means per-agent tracking is unavailable.

**vLLM mapping:** Requires custom request metadata passing `agent_id` as a request parameter. Not available in stock vLLM without extension.
**TGI mapping:** Not natively supported.
**Mock mapping:** Simulated per-agent based on assigned generation rates.

---

## 5. Ingestion Methods

Real-world GPU clusters expose metrics through different channels. The interface abstraction supports multiple ingestion methods, selected by configuration per backend.

### 5.1 REST API Polling

The simplest ingestion method. The adapter sends an HTTP GET to the cluster's metrics or health endpoint on each poll tick and parses the JSON response.

```python
class RestPollingConfig:
    endpoint_url: str                  # e.g., "http://vllm-host:8000/metrics"
    auth_header: str | None = None     # e.g., "Bearer <token>"
    verify_ssl: bool = True
    response_format: str = "prometheus"  # "prometheus" or "json"
```

For Prometheus-format responses (the default for both vLLM and TGI), the adapter parses the text-based Prometheus exposition format and extracts the relevant metric names.

### 5.2 Prometheus Scraping

If FAM runs alongside a Prometheus server that already scrapes the inference cluster, the adapter can query Prometheus's HTTP API instead of the cluster directly. This avoids adding additional load to the cluster's metrics endpoint.

```python
class PrometheusScrapeConfig:
    prometheus_url: str                # e.g., "http://prometheus:9090"
    metric_prefix: str = "vllm"       # Prefix for metric names
    query_timeout_seconds: float = 2.0
```

The adapter issues PromQL instant queries for each metric. This adds dependency on a Prometheus server but is standard in production Kubernetes deployments.

### 5.3 Log Stream Parsing

A fallback ingestion method for clusters that expose metrics only through structured log output. The adapter tails a log file or log stream (stdout/stderr pipe) and parses structured log lines (JSON format) containing metric values.

```python
class LogStreamConfig:
    log_source: str                    # File path or "stdout"/"stderr"
    line_format: str = "json"          # "json" or "regex"
    metric_patterns: dict[str, str]    # metric_name → JSON key path or regex group
```

This method is the least reliable and is intended as a last resort for environments where no metrics endpoint exists. It is not recommended for production use.

### 5.4 Method Selection

The ingestion method is selected per cluster backend through configuration:

```yaml
gpu_cluster:
  backend: vllm                       # 'vllm', 'tgi', or 'mock'
  ingestion_method: rest              # 'rest', 'prometheus', or 'log_stream'
```

All methods produce the same `dict[str, Any]` output consumed by the `get_metrics()` contract. The telemetry collector does not know or care which method was used.

---

## 6. vLLM Adapter

The vLLM adapter implements `GPUClusterInterface` for vLLM inference servers.

### 6.1 Metric Mapping

```python
VLLM_METRIC_MAP: dict[str, str] = {
    "batch_queue_depth": "vllm:num_requests_waiting",
    "kv_cache_occupancy": "vllm:gpu_cache_usage_perc",
    "active_requests": "vllm:num_requests_running",
    "inference_latency_ms": "vllm:e2e_request_latency_seconds",  # converted s → ms
}
```

### 6.2 Implementation Notes

- vLLM exposes a Prometheus-compatible `/metrics` endpoint on the API server (default port 8000).
- The latency metric is a histogram; the adapter extracts the sum and count from the most recent scrape and computes mean latency as `sum / count × 1000` (converting seconds to milliseconds). It also subtracts the previous scrape's sum/count to get the delta for the current window.
- Token rates require custom integration: the caller must pass `agent_id` in the request's `metadata` field. If vLLM is not configured to propagate metadata, `token_rates` is returned as an empty dict.
- The adapter caches the `ClusterCapabilities` by querying the vLLM `/v1/models` endpoint at startup to determine model configuration, then reading cluster topology from the configuration file.

```python
class VLLMClusterAdapter:
    def __init__(self, config: VLLMConfig) -> None: ...
    async def get_metrics(self) -> dict[str, Any]: ...
    async def health_check(self) -> bool: ...
    # ... protocol methods ...
```

---

## 7. TGI Adapter

The TGI adapter implements `GPUClusterInterface` for HuggingFace Text Generation Inference servers.

### 7.1 Metric Mapping

```python
TGI_METRIC_MAP: dict[str, str] = {
    "batch_queue_depth": "tgi_queue_size",
    "active_requests": "tgi_batch_current_size",
    "inference_latency_ms": "tgi_request_duration",  # histogram, converted
}
```

### 7.2 Implementation Notes

- TGI exposes metrics at `/metrics` (Prometheus format) and a health check at `/health`.
- KV-cache occupancy is not directly exposed by TGI. The adapter estimates it using `tgi_batch_current_size × average_kv_per_token × average_sequence_length / total_kv_cache_bytes`, where `average_kv_per_token` and `total_kv_cache_bytes` come from configuration. This is an approximation.
- Token rates are not supported by TGI natively. `token_rates` is always an empty dict.

```python
class TGIClusterAdapter:
    def __init__(self, config: TGIConfig) -> None: ...
    async def get_metrics(self) -> dict[str, Any]: ...
    async def health_check(self) -> bool: ...
    # ... protocol methods ...
```

---

## 8. Mock GPU Cluster

The mock implementation is the most important backend for development and experimentation. It generates synthetic telemetry that mimics the behavior of a real GPU inference cluster under varying load conditions, without requiring actual GPU hardware.

### 8.1 Design Goals

1. **Realistic dynamics:** Queue depth, KV-cache occupancy, and latency must covary realistically. When queue depth rises, latency should rise with a delay. When KV-cache fills, new request admission should slow, increasing queue depth further. The mock must capture these feedback loops.
2. **Configurable scenarios:** Operators must be able to define specific load scenarios — steady state, gradual ramp, burst arrival, oscillation, saturation — and replay them deterministically for reproducible experiments.
3. **Deterministic replay:** Given the same configuration and random seed, the mock must produce identical telemetry sequences. This is essential for reproducible evaluation (spec 14).
4. **Responsive to agent behavior:** In integration mode, the mock should reflect the actual inference requests submitted by agents — when agents self-throttle, utilization drops; when agents all request simultaneously, utilization spikes. In standalone mode (for unit testing), the mock follows a scripted scenario independent of agent behavior.

### 8.2 Class Interface

```python
class MockGPUCluster:
    """Simulated GPU inference cluster for development and testing.

    Implements GPUClusterInterface. Generates synthetic telemetry
    based on configurable utilization scenarios.
    """

    def __init__(self, config: MockGPUConfig) -> None: ...

    async def get_metrics(self) -> dict[str, Any]:
        """Return current synthetic metrics.

        In scripted mode, advances the scenario clock and returns
        metrics for the current time step.
        In responsive mode, computes metrics from the simulated
        cluster state (active requests, queue, etc.).
        """

    async def health_check(self) -> bool:
        """Always returns True unless a failure scenario is active."""

    @property
    def cluster_id(self) -> str:
        return f"mock_{self._config.scenario_name}"

    @property
    def capabilities(self) -> ClusterCapabilities: ...

    async def start(self) -> None:
        """Initialize the simulation clock and scenario state."""

    async def stop(self) -> None:
        """Stop the simulation."""

    # --- Mock-specific API (not part of the protocol) ---

    def submit_request(self, agent_id: str, estimated_tokens: int) -> str:
        """Simulate an inference request submission. Returns a request_id.

        Used in responsive mode by the framework adapter to inform
        the mock about actual agent inference requests.
        """

    def complete_request(self, request_id: str, actual_tokens: int) -> None:
        """Simulate completion of an inference request."""

    def inject_load(self, additional_requests: int) -> None:
        """Inject synthetic external load (simulating non-FAM traffic)."""

    @property
    def simulation_time(self) -> float:
        """Current simulation clock time."""
```

### 8.3 Scenario System

The mock supports pre-defined utilization scenarios that drive the synthetic telemetry. Each scenario is a time-series of target utilization levels, with optional noise and transitions.

```python
@dataclass
class MockGPUScenario:
    """Defines a synthetic utilization scenario."""

    name: str
    duration_seconds: float
    phases: list[ScenarioPhase]
    random_seed: int = 42
    noise_stddev: float = 0.02        # Gaussian noise added to utilization


@dataclass
class ScenarioPhase:
    """A phase within a scenario — a period of specific utilization behavior."""

    start_seconds: float
    end_seconds: float
    pattern: str                       # 'constant', 'ramp', 'sine', 'burst', 'step'
    params: dict[str, float]
```

**Supported patterns:**

| Pattern | Parameters | Description |
|---|---|---|
| `constant` | `utilization: float` | Flat utilization for the entire phase |
| `ramp` | `start_util: float, end_util: float` | Linear ramp from start to end |
| `sine` | `center: float, amplitude: float, period_seconds: float` | Sinusoidal oscillation |
| `burst` | `base_util: float, burst_util: float, burst_duration: float, burst_interval: float` | Periodic bursts of high utilization |
| `step` | `before: float, after: float, step_at_fraction: float` | Step change at a fraction of the phase |

### 8.4 Metric Generation

From the scenario's target utilization at time \(t\), the mock derives individual metrics:

**GPU utilization → batch_queue_depth:**
```
queue_depth = int(utilization × queue_depth_capacity × (1 + noise))
```

**GPU utilization → kv_cache_occupancy:**
```
kv_cache = utilization × 0.85 + 0.1  # KV-cache lags slightly, with floor of 0.1
kv_cache = min(kv_cache + noise, 1.0)
```

**GPU utilization → active_requests:**
```
active = int(utilization × max_concurrent_requests × (0.9 + 0.1 × noise))
```

**GPU utilization → inference_latency_ms:**
```
base_latency = config.base_latency_ms                   # e.g., 50ms
congestion_latency = base_latency × (utilization / (1.0 - utilization + 0.01))
latency = base_latency + congestion_latency + noise × 10
```

The congestion formula creates the non-linear latency curve expected from queueing theory: latency rises slowly at low utilization and explodes as utilization approaches 1.0 (the M/M/1 queue approximation).

**GPU utilization → token_rates:**
When `supports_per_agent_tracking` is True in the mock config, the mock distributes the aggregate throughput across registered agent IDs proportionally to their submitted request tokens (responsive mode) or uniformly (scripted mode).

### 8.5 Responsive Mode

In responsive mode, the mock maintains a simulated queue and processing pipeline. Agent inference requests submitted via `submit_request()` enter the queue. A background simulation loop processes requests at a rate determined by `max_tokens_per_second`. Metrics are derived from the actual simulated state (real queue depth, real active count) rather than from a scripted utilization curve.

This mode is activated when the mock detects that `submit_request()` is being called (integration with the framework adapter). It allows the evaluation harness to measure the actual impact of FAM's pricing on agent behavior — when agents self-throttle, the mock's utilization genuinely drops.

```python
@dataclass
class SimulatedRequest:
    request_id: str
    agent_id: str
    estimated_tokens: int
    submitted_at: float
    started_at: float | None = None
    completed_at: float | None = None
    status: str = "queued"            # 'queued', 'processing', 'completed'
```

The responsive mode simulation loop runs as an `asyncio` task at a configurable tick rate (default 100ms). On each tick:

1. **Process active requests.** For each request with status `"processing"`, generate tokens at the per-request rate (`max_tokens_per_second / active_count`). When `actual_tokens >= estimated_tokens`, mark the request as completed.
2. **Admit queued requests.** Move requests from `"queued"` to `"processing"` up to the `max_concurrent_requests` limit. Admission allocates KV-cache proportional to `estimated_tokens × kv_bytes_per_token`.
3. **Update KV-cache occupancy.** Sum KV-cache allocations across all active requests. Divide by `total_kv_cache_bytes`. If occupancy exceeds 0.95, stop admitting new requests (simulating KV-cache pressure blocking).
4. **Compute latency.** Track time-to-first-token for each request as `started_at - submitted_at`. Report the mean across requests completed in the last window.
5. **Compute token rates.** Aggregate tokens generated per agent over the last window.

The simulation respects capacity constraints realistically: when `max_concurrent_requests` slots are full and KV-cache is saturated, queued requests wait, queue depth grows, and latency spikes — exactly as a real cluster would behave.

```python
async def _simulation_loop(self) -> None:
    while self._running:
        await asyncio.sleep(self._tick_interval)
        self._process_active_requests()
        self._admit_queued_requests()
        self._update_kv_cache()
        self._update_latency_window()
        self._update_token_rates()
```

In responsive mode, external load can be injected via `inject_load()` to simulate non-FAM traffic competing for the same GPU resources. This is critical for experiments that test FAM's behavior when only a fraction of total cluster load is under FAM's control.

### 8.6 Failure Injection

The mock supports injecting failures for testing the telemetry collector's health check and fallback behavior:

```python
def inject_failure(self, failure_type: str, duration_seconds: float) -> None:
    """Simulate a cluster failure.

    failure_type: 'metrics_unavailable' — get_metrics raises TimeoutError
                  'health_check_fail' — health_check returns False
                  'partial_metrics' — get_metrics returns dict with None values
                  'stale_metrics' — get_metrics returns the same snapshot repeatedly
    duration_seconds: How long the failure lasts before auto-recovery.
    """
```

---

## 9. Pre-Built Scenarios

The mock ships with several pre-built scenarios for common testing patterns. These are loaded by name from configuration.

```python
BUILT_IN_SCENARIOS: dict[str, MockGPUScenario] = {
    "idle": MockGPUScenario(
        name="idle",
        duration_seconds=300.0,
        phases=[ScenarioPhase(0, 300, "constant", {"utilization": 0.1})],
    ),
    "steady_moderate": MockGPUScenario(
        name="steady_moderate",
        duration_seconds=300.0,
        phases=[ScenarioPhase(0, 300, "constant", {"utilization": 0.5})],
    ),
    "ramp_to_saturation": MockGPUScenario(
        name="ramp_to_saturation",
        duration_seconds=120.0,
        phases=[ScenarioPhase(0, 120, "ramp", {"start_util": 0.2, "end_util": 0.95})],
    ),
    "burst_pattern": MockGPUScenario(
        name="burst_pattern",
        duration_seconds=300.0,
        phases=[ScenarioPhase(0, 300, "burst", {
            "base_util": 0.3,
            "burst_util": 0.9,
            "burst_duration": 10.0,
            "burst_interval": 30.0,
        })],
    ),
    "diurnal": MockGPUScenario(
        name="diurnal",
        duration_seconds=600.0,
        phases=[ScenarioPhase(0, 600, "sine", {
            "center": 0.5,
            "amplitude": 0.35,
            "period_seconds": 300.0,
        })],
    ),
    "congestion_collapse": MockGPUScenario(
        name="congestion_collapse",
        duration_seconds=120.0,
        phases=[
            ScenarioPhase(0, 30, "constant", {"utilization": 0.4}),
            ScenarioPhase(30, 60, "ramp", {"start_util": 0.4, "end_util": 0.98}),
            ScenarioPhase(60, 90, "constant", {"utilization": 0.98}),
            ScenarioPhase(90, 120, "ramp", {"start_util": 0.98, "end_util": 0.3}),
        ],
    ),
}
```

---

## 10. Configuration

All configuration for the GPU cluster interface is namespaced under `gpu_cluster` in the global FAM configuration (spec 12). The full schema:

```yaml
gpu_cluster:
  backend: mock                       # 'vllm', 'tgi', or 'mock'
  ingestion_method: rest              # 'rest', 'prometheus', 'log_stream' (ignored for mock)

  # Cluster capabilities (used for telemetry normalization)
  capabilities:
    max_concurrent_requests: 128
    queue_depth_capacity: 64
    total_kv_cache_bytes: 85899345920  # 80GB
    max_tokens_per_second: 5000.0
    supports_per_agent_tracking: false
    model_id: "meta-llama/Llama-3-70B"
    gpu_count: 4
    gpu_type: "A100-80GB"

  # vLLM-specific configuration
  vllm:
    metrics_url: "http://localhost:8000/metrics"
    models_url: "http://localhost:8000/v1/models"
    auth_header: null
    verify_ssl: true

  # TGI-specific configuration
  tgi:
    metrics_url: "http://localhost:8080/metrics"
    health_url: "http://localhost:8080/health"
    average_kv_per_token: 256         # Bytes of KV-cache per token (model-dependent)

  # Prometheus scraping configuration
  prometheus:
    url: "http://localhost:9090"
    metric_prefix: "vllm"
    query_timeout_seconds: 2.0

  # Mock-specific configuration
  mock:
    scenario: "ramp_to_saturation"    # Built-in scenario name or "custom"
    random_seed: 42
    noise_stddev: 0.02
    base_latency_ms: 50.0
    mode: scripted                    # 'scripted' or 'responsive'

    # Custom scenario definition (used when scenario: "custom")
    custom_scenario:
      duration_seconds: 300.0
      phases:
        - start_seconds: 0
          end_seconds: 100
          pattern: ramp
          params:
            start_util: 0.1
            end_util: 0.8
        - start_seconds: 100
          end_seconds: 200
          pattern: constant
          params:
            utilization: 0.8
        - start_seconds: 200
          end_seconds: 300
          pattern: ramp
          params:
            start_util: 0.8
            end_util: 0.2

    # Failure injection schedule (optional, for testing)
    failure_schedule:
      # - at_seconds: 60
      #   failure_type: metrics_unavailable
      #   duration_seconds: 15
```

---

## 11. Error Handling

**Connection failures (real backends):** If the HTTP request to the metrics endpoint fails (connection refused, timeout, DNS resolution failure), the adapter catches the exception and returns a partial metrics dict with `None` for all metrics that could not be retrieved. The telemetry collector's health tracker handles the degradation. The adapter logs the error at WARNING level on each failure and at INFO level with the error count on recovery.

**Malformed metrics (real backends):** If the metrics endpoint returns data in an unexpected format (missing metrics, wrong types, unparseable Prometheus exposition), the adapter extracts whatever is parseable and returns `None` for the rest. A detailed ERROR-level log is emitted with the raw response for debugging.

**Mock failures:** The mock's `get_metrics()` raises `asyncio.TimeoutError` when a `metrics_unavailable` failure is injected, and returns partial dicts for `partial_metrics` failures. The mock never crashes or raises unexpected exceptions — it is designed for testing failure handling.

**Startup failures:** If the backend is unreachable at startup (e.g., vLLM is not running), the `start()` method logs a WARNING and transitions the interface to an unhealthy state. The telemetry collector will use fallback values. This is intentional — FAM should be startable even if the inference cluster is not yet ready, to avoid brittle boot ordering dependencies.

---

## 12. Metrics Emitted

The GPU cluster interface emits the following metrics to the observability system (spec 13):

**Counters:** `gpu_interface.polls.total` (per outcome: success/failure/timeout/partial), `gpu_interface.health_checks.total` (per outcome: pass/fail).

**Gauges:** `gpu_interface.reachable` (0 or 1 — whether the backend is reachable), `gpu_interface.staleness_seconds` (time since last successful metric read).

**Histograms:** `gpu_interface.poll_duration_ms` (time to complete a get_metrics call), `gpu_interface.health_check_duration_ms`.

For the mock backend specifically:

**Gauges:** `gpu_mock.simulation_time` (current scenario clock), `gpu_mock.scripted_utilization` (target utilization from the scenario, useful for comparing against actual metrics to verify the mock's accuracy), `gpu_mock.simulated_queue_depth` (in responsive mode, the actual simulated queue size), `gpu_mock.simulated_active_requests`.

---

## 13. Testing Strategy

### 13.1 Unit Tests

**Protocol conformance:** Verify that `MockGPUCluster`, `VLLMClusterAdapter`, and `TGIClusterAdapter` all satisfy the `GPUClusterInterface` protocol (runtime protocol check using `isinstance`).

**Mock metric generation — constant scenario:** Configure mock with constant utilization at 0.5. Poll 100 times. Verify all returned metrics are within expected ranges. Verify queue depth is approximately `0.5 × queue_depth_capacity ± noise`. Verify latency follows the congestion formula.

**Mock metric generation — ramp scenario:** Configure mock with a ramp from 0.0 to 1.0 over 100 seconds. Poll at each second. Verify metrics monotonically increase (with noise tolerance). Verify KV-cache occupancy lags utilization slightly.

**Mock scenario phases:** Configure a multi-phase scenario. Verify that metrics transition correctly at phase boundaries. Verify that noise is applied independently per poll.

**Mock determinism:** Run the same scenario twice with the same seed. Verify identical metric sequences.

**Mock failure injection:** Inject `metrics_unavailable`. Verify `get_metrics()` raises `TimeoutError`. Verify recovery after the specified duration. Inject `partial_metrics`. Verify returned dict has `None` values.

**vLLM adapter metric parsing:** Feed sample Prometheus text output to the adapter's parser. Verify correct extraction of all mapped metrics. Feed malformed output. Verify graceful degradation with `None` values.

**TGI adapter KV-cache estimation:** Verify the KV-cache estimation formula produces values in [0.0, 1.0] for valid inputs. Verify edge cases: batch size 0, very large batch, very large sequence length.

### 13.2 Integration Tests

**Mock with telemetry collector:** Register a mock cluster as a telemetry source. Start both. Run for 10 seconds. Verify the telemetry collector publishes `GPUTelemetrySnapshot`s that reflect the mock's scenario. Stop both. Verify clean shutdown.

**Mock responsive mode with simulated agents:** Create a mock in responsive mode. Submit 10 simulated requests. Poll metrics. Verify active requests and queue depth reflect the submissions. Complete 5 requests. Poll again. Verify metrics updated.

**Health check integration:** Start a mock cluster, run health checks (should pass). Inject a `health_check_fail` failure. Verify health check returns False. Wait for recovery. Verify health check returns True.

**Backend switching:** Start FAM with mock backend, verify metrics flow. Stop, reconfigure to a different mock scenario, restart. Verify new scenario drives different metrics. (Real backend switching is tested manually or in deployment tests, not in unit/integration tests.)

### 13.3 Property-Based Tests

Use Hypothesis to generate random scenario configurations (random phase counts, random utilization values, random durations). Verify: (a) all generated metrics are within their valid ranges (`batch_queue_depth >= 0`, `0.0 <= kv_cache_occupancy <= 1.0`, etc.), (b) the scenario completes without exceptions, (c) deterministic replay produces identical output.

Generate random sequences of `submit_request` / `complete_request` calls in responsive mode. Verify: (a) queue depth is always non-negative, (b) active requests never exceed `max_concurrent_requests`, (c) the system drains all requests when no new submissions arrive.

---

## 14. Dependencies

**Internal:** `fam/types.py` (shared types), `fam/config/schema.py` (configuration schema).

**External (real backends):** `aiohttp` for HTTP requests to vLLM/TGI metrics endpoints. `prometheus_client.parser` (optional) for parsing Prometheus exposition format — alternatively, a lightweight custom parser is included to avoid the dependency. The mock implementation uses only the Python standard library (`asyncio`, `dataclasses`, `random`, `math`, `time`, `collections.deque`, `logging`).

**External (mock):** None. The mock has zero external dependencies.

**Interface contracts:** The `GPUClusterInterface` protocol is consumed by the telemetry collector (spec 01) via its `Pollable` interface. The `get_metrics()` method is the only method the telemetry collector calls during normal operation. `health_check()` is called during startup and optionally on periodic health sweeps. `capabilities` is read once at registration time.

---

## 15. Open Questions

**Live backend switching.** Can FAM switch from one backend to another at runtime (e.g., from mock to vLLM when a cluster comes online)? The current design requires restarting FAM. Hot-swapping the backend would require resetting telemetry EMA state, re-reading capabilities, and potentially re-calibrating the pricing engine. This is complex and not needed for the initial implementation. Deferred.

**Multi-cluster support.** The current architecture supports a single GPU cluster. Multi-cluster support (e.g., different clusters for different model sizes) would require per-cluster telemetry, per-cluster pricing, and a routing layer that selects the cluster based on the agent's model requirement. The mock could trivially support multiple clusters (one `MockGPUCluster` instance per simulated cluster). The telemetry collector and pricing engine would need to be extended. Deferred.

**Mock calibration against real clusters.** The mock's metric generation formulas (e.g., the congestion latency curve) are approximations. Ideally, the mock would be calibrated against real cluster telemetry traces — run a real cluster under controlled load, record the telemetry, and fit the mock's parameters to reproduce it. This would make evaluation harness results more transferable to production. Deferred to the evaluation spec (spec 14).

**Prometheus metric staleness.** When scraping Prometheus, metrics may be stale (Prometheus scrapes the cluster at its own interval, and FAM scrapes Prometheus at its interval). The staleness could be 2× the maximum of the two intervals. Should the adapter account for this by reading the scrape timestamp? The current design does not — it trusts Prometheus to keep metrics reasonably fresh. This is a reasonable assumption for well-configured Prometheus deployments.
