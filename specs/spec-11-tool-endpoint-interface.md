# Spec 11 — Tool Endpoint Interface

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview)
**Consumed By:** Spec 01 (Telemetry Collector), Spec 07 (Tool Dispatch Queue), Spec 13 (Metrics & Observability), Spec 14 (Evaluation Harness)

---

## 1. Purpose

The tool endpoint interface is the boundary between FAM's orchestration logic and the external services that agents invoke as tools — search APIs, code execution sandboxes, database connectors, third-party REST APIs, and any other callable endpoint. FAM does not own or run these services. It wraps them: intercepting outgoing calls to instrument them with timing, error tracking, and telemetry reporting, and abstracting them behind a uniform interface that the dispatch queue (spec 07) and telemetry collector (spec 01) can consume.

This spec defines three things. First, the **tool registry** — the data model for describing a tool endpoint (its ID, URL, rate limits, expected latency profile, idempotency characteristics). Second, the **instrumented wrapper** — the layer that intercepts every tool call, records timing and outcome, reports telemetry events to the collector, and handles transient errors. Third, the **mock tool endpoints** — configurable simulated endpoints for development and testing that generate realistic latency distributions, failure patterns, rate limit behavior, and capacity contention.

The mock endpoints are not an afterthought. They are the primary tool backends for the evaluation harness (spec 14) and must be realistic enough that coordination strategies tested against mocks transfer meaningfully to production deployments.

---

## 2. Module Location

```
fam/
├── interfaces/
│   ├── __init__.py
│   ├── tool_endpoint.py     # ToolEndpoint protocol, ToolRegistry, InstrumentedWrapper
│   ├── tool_mock.py         # MockToolEndpoint — simulated backend
│   └── tool_models.py       # Dataclasses for tool registration and telemetry events
```

Tests live in `tests/test_interfaces/`.

---

## 3. Tool Registry

The tool registry is the central catalog of all tool endpoints known to FAM. Every tool that an agent might call must be registered before the system starts. Registration provides FAM with the metadata needed to price the tool, enforce rate limits, and select the correct dispatch strategy.

### 3.1 Tool Registration Data Model

```python
@dataclass(frozen=True)
class ToolEndpointSpec:
    """Complete specification of a registered tool endpoint."""

    endpoint_id: str                  # Unique identifier (e.g., "search_api", "code_exec")
    display_name: str                 # Human-readable name for signals and logs
    endpoint_url: str                 # Base URL for the endpoint
    method: str = "POST"             # HTTP method (POST, GET, etc.)

    # Rate limit specification
    rate_limit: RateLimitSpec | None = None

    # Expected latency profile (used for pricing baseline and anomaly detection)
    latency_profile: LatencyProfile = field(default_factory=LatencyProfile)

    # Idempotency
    idempotent: bool = False          # If True, safe to retry on transient failure
    idempotency_key_header: str | None = None  # Header for idempotency key if supported

    # Dispatch constraints
    max_concurrent_calls: int = 10    # Maximum simultaneous in-flight calls
    timeout_seconds: float = 30.0     # Per-call timeout

    # Authentication
    auth_type: str = "none"           # 'none', 'bearer', 'api_key', 'header'
    auth_config: dict[str, str] = field(default_factory=dict)

    # Metadata
    tags: list[str] = field(default_factory=list)  # For grouping: ["search", "external"]
    description: str = ""
```

### 3.2 Rate Limit Specification

```python
@dataclass(frozen=True)
class RateLimitSpec:
    """Describes the rate limit enforced by the tool endpoint."""

    requests_per_window: int           # Maximum requests allowed per window
    window_seconds: float              # Duration of the rate limit window
    burst_limit: int | None = None     # Max instantaneous burst (if different from per-window)
    concurrent_limit: int | None = None  # Max simultaneous requests (if enforced server-side)
    retry_after_header: str = "Retry-After"  # Header indicating when to retry after 429
    rate_limit_header: str = "X-RateLimit-Remaining"  # Header with remaining quota
    rate_limit_reset_header: str = "X-RateLimit-Reset"  # Header with window reset time
```

### 3.3 Latency Profile

```python
@dataclass(frozen=True)
class LatencyProfile:
    """Expected latency characteristics of the endpoint under normal conditions."""

    expected_p50_ms: float = 200.0     # Expected median latency
    expected_p90_ms: float = 500.0     # Expected 90th percentile
    expected_p99_ms: float = 2000.0    # Expected 99th percentile
    latency_sla_ms: float = 5000.0     # Latency above which the call is considered degraded
```

The latency profile is used by the pricing engine (spec 02) to compute the latency ratio: `observed_p50 / expected_p50`. A ratio above 1.0 indicates the endpoint is slower than normal, contributing upward pressure on the tool price. The profile is also used by the mock implementation to generate realistic latency distributions.

### 3.4 ToolRegistry Class

```python
class ToolRegistry:
    """Central catalog of registered tool endpoints.

    Populated from configuration at startup. Immutable after startup
    (no runtime registration/deregistration in the initial implementation).
    """

    def __init__(self, config: ToolRegistryConfig) -> None:
        self._endpoints: dict[str, ToolEndpointSpec] = {}

    def register(self, spec: ToolEndpointSpec) -> None:
        """Register a tool endpoint. Raises ValueError if endpoint_id already exists."""

    def get(self, endpoint_id: str) -> ToolEndpointSpec:
        """Look up an endpoint by ID. Raises KeyError if not registered."""

    def get_all(self) -> dict[str, ToolEndpointSpec]:
        """Return all registered endpoints."""

    def find_by_tag(self, tag: str) -> list[ToolEndpointSpec]:
        """Return all endpoints with the given tag."""

    @property
    def endpoint_ids(self) -> list[str]:
        """All registered endpoint IDs."""

    def __contains__(self, endpoint_id: str) -> bool:
        return endpoint_id in self._endpoints

    def __len__(self) -> int:
        return len(self._endpoints)
```

---

## 4. Tool Endpoint Protocol

The abstract interface that all tool endpoint implementations (real and mock) must satisfy. This is the contract consumed by the instrumented wrapper and, through it, by the dispatch queue.

```python
class ToolEndpoint(Protocol):
    """Abstract interface for a callable tool endpoint."""

    async def invoke(
        self,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> ToolCallResult:
        """Execute a tool call against the endpoint.

        Args:
            payload: The tool call arguments (serialized to the endpoint's expected format).
            timeout: Per-call timeout override. If None, uses the spec's timeout_seconds.
            headers: Additional HTTP headers (e.g., idempotency key).

        Returns:
            ToolCallResult containing the response or error information.
        """
        ...

    @property
    def endpoint_id(self) -> str:
        """The endpoint's unique identifier."""
        ...

    async def health_check(self) -> bool:
        """Lightweight health check. Returns True if the endpoint is reachable."""
        ...

    async def start(self) -> None:
        """Initialize the endpoint (open connection pool, etc.)."""
        ...

    async def stop(self) -> None:
        """Tear down the endpoint (close connections, etc.)."""
        ...
```

### 4.1 Tool Call Result

```python
@dataclass(frozen=True)
class ToolCallResult:
    """Result of a single tool call invocation."""

    success: bool                      # True if the call returned a valid result
    result: Any | None                 # The tool's return value (None on failure)
    error: ToolCallError | None        # Error details (None on success)
    status_code: int | None            # HTTP status code (None for non-HTTP tools)
    latency_ms: float                  # Wall-clock time for the call
    headers: dict[str, str]            # Response headers (for rate limit parsing)


@dataclass(frozen=True)
class ToolCallError:
    """Structured error information from a failed tool call."""

    error_type: str                    # 'timeout', 'rate_limit', 'server_error',
                                       # 'client_error', 'connection', 'unknown'
    message: str                       # Human-readable error description
    retryable: bool                    # Whether the error is transient and retryable
    retry_after_seconds: float | None  # Suggested wait before retry (from 429 header)
    raw_error: str | None              # Raw exception or response body for debugging
```

---

## 5. Instrumented Wrapper

The instrumented wrapper is the layer that sits between the dispatch queue and the raw tool endpoint. Every tool call passes through the wrapper, which adds timing, error classification, telemetry reporting, and rate limit tracking. The dispatch queue (spec 07) calls the wrapper, not the raw endpoint.

### 5.1 Wrapper Interface

```python
class InstrumentedToolEndpoint:
    """Wraps a ToolEndpoint with instrumentation, telemetry, and rate limit tracking."""

    def __init__(
        self,
        endpoint: ToolEndpoint,
        spec: ToolEndpointSpec,
        telemetry_receiver: TelemetryReceiver,
    ) -> None:
        """
        Args:
            endpoint: The underlying tool endpoint implementation.
            spec: The endpoint's registration spec (for rate limits, etc.).
            telemetry_receiver: The telemetry collector's push interface
                                for reporting ToolCallEvents.
        """
        self._endpoint = endpoint
        self._spec = spec
        self._telemetry = telemetry_receiver
        self._rate_tracker = RateLimitTracker(spec.rate_limit) if spec.rate_limit else None
        self._active_calls: int = 0
        self._call_counter: int = 0

    async def invoke(
        self,
        payload: dict[str, Any],
        *,
        call_id: str,
        agent_id: str,
        timeout: float | None = None,
    ) -> ToolCallResult:
        """Execute an instrumented tool call.

        1. Check rate limit headroom.
        2. Increment active call counter.
        3. Record start time.
        4. Call underlying endpoint.invoke().
        5. Record end time, compute latency.
        6. Classify result (success/error type).
        7. Parse rate limit headers from response.
        8. Push ToolCallEvent to telemetry collector.
        9. Decrement active call counter.
        10. Return result.
        """
```

### 5.2 Invocation Flow

```python
    async def invoke(self, payload, *, call_id, agent_id, timeout=None):
        effective_timeout = timeout or self._spec.timeout_seconds
        self._active_calls += 1
        self._call_counter += 1

        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._endpoint.invoke(payload, timeout=effective_timeout),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - start) * 1000
            result = ToolCallResult(
                success=False,
                result=None,
                error=ToolCallError("timeout", f"Timed out after {effective_timeout}s", True, None, None),
                status_code=None,
                latency_ms=elapsed,
                headers={},
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            result = ToolCallResult(
                success=False,
                result=None,
                error=ToolCallError("connection", str(exc), True, None, str(exc)),
                status_code=None,
                latency_ms=elapsed,
                headers={},
            )
        finally:
            self._active_calls -= 1

        completed = time.monotonic()

        if self._rate_tracker and result.headers:
            self._rate_tracker.update_from_headers(result.headers)

        event = ToolCallEvent(
            endpoint_id=self._spec.endpoint_id,
            started_at=start,
            completed_at=completed,
            latency_ms=result.latency_ms,
            success=result.success,
            error_type=result.error.error_type if result.error else None,
        )
        await self._telemetry.receive_tool_call_event(event)

        return result
```

### 5.3 Rate Limit Tracker

The rate limit tracker maintains an internal model of the endpoint's rate limit state, updated both from FAM's own call history and from rate limit response headers (when available).

```python
class RateLimitTracker:
    """Tracks rate limit consumption and headroom for a single endpoint."""

    def __init__(self, spec: RateLimitSpec) -> None:
        self._spec = spec
        self._window_calls: deque[float] = deque()  # Timestamps of calls in current window
        self._remaining: int | None = None           # From response header, if available
        self._reset_at: float | None = None          # From response header, if available

    def record_call(self) -> None:
        """Record that a call was made at the current time."""
        now = time.monotonic()
        self._window_calls.append(now)
        self._evict_expired(now)

    def headroom(self) -> float:
        """Return rate limit headroom as a fraction 0.0–1.0.

        If response headers provide remaining quota, use that.
        Otherwise, estimate from internal call tracking.
        """
        if self._remaining is not None and self._reset_at is not None:
            if time.monotonic() < self._reset_at:
                return self._remaining / self._spec.requests_per_window
            else:
                self._remaining = None
                self._reset_at = None

        self._evict_expired(time.monotonic())
        used = len(self._window_calls)
        return max(0.0, 1.0 - (used / self._spec.requests_per_window))

    def update_from_headers(self, headers: dict[str, str]) -> None:
        """Update rate limit state from HTTP response headers."""
        remaining = headers.get(self._spec.rate_limit_header)
        if remaining is not None:
            try:
                self._remaining = int(remaining)
            except ValueError:
                pass

        reset = headers.get(self._spec.rate_limit_reset_header)
        if reset is not None:
            try:
                self._reset_at = float(reset)
            except ValueError:
                pass

    def can_call(self) -> bool:
        """Check if a call can be made without likely hitting the rate limit.

        Returns False if headroom is below a safety margin (5%).
        """
        return self.headroom() > 0.05

    def _evict_expired(self, now: float) -> None:
        cutoff = now - self._spec.window_seconds
        while self._window_calls and self._window_calls[0] < cutoff:
            self._window_calls.popleft()
```

### 5.4 Active Call and Queue Depth Reporting

The dispatch queue (spec 07) needs real-time active call counts and queue depths for its prioritization logic, and the telemetry collector (spec 01) needs them for tool utilization computation. The instrumented wrapper exposes these as synchronous reads:

```python
    @property
    def active_calls(self) -> int:
        """Number of tool calls currently in flight to this endpoint."""
        return self._active_calls

    @property
    def rate_limit_headroom(self) -> float:
        """Current rate limit headroom (0.0–1.0). 0.0 = at limit."""
        return self._rate_tracker.headroom() if self._rate_tracker else 1.0

    @property
    def total_calls(self) -> int:
        """Total number of calls made through this wrapper since startup."""
        return self._call_counter
```

---

## 6. Real HTTP Endpoint Implementation

For production tool endpoints (actual HTTP services), the concrete implementation uses an async HTTP client.

```python
class HttpToolEndpoint:
    """ToolEndpoint implementation for real HTTP services."""

    def __init__(self, spec: ToolEndpointSpec) -> None:
        self._spec = spec
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        headers = {}
        if self._spec.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self._spec.auth_config['token']}"
        elif self._spec.auth_type == "api_key":
            headers[self._spec.auth_config.get("header_name", "X-API-Key")] = (
                self._spec.auth_config["key"]
            )

        self._session = aiohttp.ClientSession(
            base_url=self._spec.endpoint_url,
            headers=headers,
        )

    async def stop(self) -> None:
        if self._session:
            await self._session.close()

    async def invoke(
        self,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> ToolCallResult:
        effective_timeout = timeout or self._spec.timeout_seconds
        start = time.monotonic()

        try:
            async with self._session.request(
                self._spec.method,
                "",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=effective_timeout),
                headers=headers,
            ) as resp:
                elapsed = (time.monotonic() - start) * 1000
                body = await resp.json()
                response_headers = dict(resp.headers)

                if 200 <= resp.status < 300:
                    return ToolCallResult(
                        success=True, result=body, error=None,
                        status_code=resp.status, latency_ms=elapsed,
                        headers=response_headers,
                    )
                elif resp.status == 429:
                    retry_after = response_headers.get("Retry-After")
                    return ToolCallResult(
                        success=False, result=None,
                        error=ToolCallError(
                            "rate_limit", f"Rate limited (429)", True,
                            float(retry_after) if retry_after else None,
                            str(body),
                        ),
                        status_code=429, latency_ms=elapsed,
                        headers=response_headers,
                    )
                elif resp.status >= 500:
                    return ToolCallResult(
                        success=False, result=None,
                        error=ToolCallError(
                            "server_error", f"Server error ({resp.status})", True,
                            None, str(body),
                        ),
                        status_code=resp.status, latency_ms=elapsed,
                        headers=response_headers,
                    )
                else:
                    return ToolCallResult(
                        success=False, result=None,
                        error=ToolCallError(
                            "client_error", f"Client error ({resp.status})", False,
                            None, str(body),
                        ),
                        status_code=resp.status, latency_ms=elapsed,
                        headers=response_headers,
                    )
        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - start) * 1000
            return ToolCallResult(
                success=False, result=None,
                error=ToolCallError("timeout", f"Timed out after {effective_timeout}s", True, None, None),
                status_code=None, latency_ms=elapsed, headers={},
            )

    async def health_check(self) -> bool:
        try:
            async with self._session.get("/health", timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                return resp.status < 500
        except Exception:
            return False

    @property
    def endpoint_id(self) -> str:
        return self._spec.endpoint_id
```

---

## 7. Mock Tool Endpoints

The mock tool endpoint is the primary backend for development and evaluation. It simulates the behavior of a real tool endpoint — responding to calls with configurable latency, failure rates, rate limits, and capacity contention — without requiring any actual external service.

### 7.1 Design Goals

1. **Configurable latency distribution.** Support fixed latency, normal distribution, log-normal distribution (realistic for HTTP services), and heavy-tailed distribution (Pareto) for simulating endpoints with occasional very slow responses.
2. **Configurable failure rates.** A fixed probability of failure on each call, with configurable error types (server error, timeout, rate limit). Failure probability can increase under load to simulate degradation.
3. **Configurable rate limits.** The mock enforces its own rate limit and returns 429 responses with appropriate headers when the limit is exceeded, testing FAM's rate limit handling.
4. **Configurable capacity contention.** The mock limits concurrent calls. When the limit is hit, additional calls either queue (simulating backpressure) or fail immediately (simulating a non-queueing endpoint). Latency increases with concurrency to simulate contention.
5. **Deterministic replay.** Given the same seed, the mock produces identical latency and failure sequences for reproducible experiments.

### 7.2 Class Interface

```python
class MockToolEndpoint:
    """Simulated tool endpoint for development and testing.

    Implements ToolEndpoint. Generates configurable latency, failures,
    and rate limit behavior.
    """

    def __init__(self, spec: ToolEndpointSpec, config: MockToolConfig) -> None: ...

    async def invoke(
        self,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> ToolCallResult:
        """Simulate a tool call with configured latency and failure behavior."""

    async def health_check(self) -> bool:
        """Returns True unless a failure scenario makes the endpoint unreachable."""

    @property
    def endpoint_id(self) -> str:
        return self._spec.endpoint_id

    async def start(self) -> None:
        """Initialize the RNG and rate limit state."""

    async def stop(self) -> None:
        """Reset state."""

    # --- Mock-specific API ---

    def set_failure_rate(self, rate: float) -> None:
        """Dynamically adjust the failure rate (for scenario testing)."""

    def set_latency_multiplier(self, multiplier: float) -> None:
        """Dynamically scale latency (for simulating degradation)."""

    @property
    def total_calls_received(self) -> int:
        """Total calls received since startup."""

    @property
    def current_concurrent(self) -> int:
        """Current number of calls being processed."""
```

### 7.3 Mock Configuration

```python
@dataclass
class MockToolConfig:
    """Configuration for a mock tool endpoint."""

    # Latency distribution
    latency_distribution: str = "lognormal"  # 'fixed', 'normal', 'lognormal', 'pareto'
    latency_params: dict[str, float] = field(default_factory=lambda: {
        "mean_ms": 200.0,
        "stddev_ms": 50.0,
    })

    # Contention-dependent latency scaling
    contention_latency_factor: float = 0.1   # Additional latency per concurrent call (ms)

    # Failure configuration
    base_failure_rate: float = 0.02          # Probability of failure per call
    load_failure_amplification: float = 2.0  # Failure rate multiplier at max concurrency
    failure_type_weights: dict[str, float] = field(default_factory=lambda: {
        "server_error": 0.6,
        "timeout": 0.3,
        "connection": 0.1,
    })

    # Rate limit simulation
    simulate_rate_limit: bool = True
    rate_limit_requests_per_window: int = 100
    rate_limit_window_seconds: float = 60.0

    # Capacity
    max_concurrent: int = 10
    overflow_behavior: str = "queue"         # 'queue' (wait) or 'reject' (immediate 503)
    max_queue_depth: int = 50

    # Result generation
    result_generator: str = "echo"           # 'echo' (return payload), 'random', 'fixed'
    fixed_result: Any = None

    # Determinism
    random_seed: int = 42
```

### 7.4 Latency Generation

The mock generates call latency from the configured distribution:

```python
def _generate_latency(self) -> float:
    """Generate a latency sample in milliseconds from the configured distribution."""
    params = self._config.latency_params
    base_mean = params.get("mean_ms", 200.0)
    stddev = params.get("stddev_ms", 50.0)

    # Add contention-dependent latency
    contention_penalty = self._current_concurrent * self._config.contention_latency_factor
    adjusted_mean = base_mean + contention_penalty

    if self._config.latency_distribution == "fixed":
        return adjusted_mean

    elif self._config.latency_distribution == "normal":
        sample = self._rng.gauss(adjusted_mean, stddev)
        return max(1.0, sample)

    elif self._config.latency_distribution == "lognormal":
        # Convert mean/stddev to log-space parameters
        mu = math.log(adjusted_mean**2 / math.sqrt(stddev**2 + adjusted_mean**2))
        sigma = math.sqrt(math.log(1 + (stddev**2 / adjusted_mean**2)))
        return self._rng.lognormvariate(mu, sigma)

    elif self._config.latency_distribution == "pareto":
        alpha = params.get("alpha", 3.0)
        scale = adjusted_mean * (alpha - 1) / alpha
        sample = (self._rng.paretovariate(alpha)) * scale
        return max(1.0, sample)

    return adjusted_mean
```

### 7.5 Failure Decision

```python
def _should_fail(self) -> tuple[bool, str | None]:
    """Decide whether this call should fail and with what error type.

    Failure rate increases with concurrency to simulate degradation under load.
    """
    load_ratio = self._current_concurrent / max(self._config.max_concurrent, 1)
    effective_rate = self._config.base_failure_rate * (
        1.0 + (self._config.load_failure_amplification - 1.0) * load_ratio
    )

    if self._rng.random() < effective_rate:
        weights = self._config.failure_type_weights
        types = list(weights.keys())
        probs = [weights[t] for t in types]
        total = sum(probs)
        probs = [p / total for p in probs]
        chosen = self._rng.choices(types, weights=probs, k=1)[0]
        return True, chosen

    return False, None
```

### 7.6 Rate Limit Enforcement

When `simulate_rate_limit` is True, the mock tracks calls in its own rate limit window and returns 429 responses when the limit is exceeded:

```python
def _check_rate_limit(self) -> tuple[bool, dict[str, str]]:
    """Check if the call would exceed the simulated rate limit.

    Returns (allowed, response_headers).
    Headers include X-RateLimit-Remaining and X-RateLimit-Reset on 429s.
    """
    now = time.monotonic()
    self._rate_window_calls.append(now)
    cutoff = now - self._config.rate_limit_window_seconds
    while self._rate_window_calls and self._rate_window_calls[0] < cutoff:
        self._rate_window_calls.popleft()

    remaining = self._config.rate_limit_requests_per_window - len(self._rate_window_calls)
    reset_at = now + self._config.rate_limit_window_seconds

    headers = {
        "X-RateLimit-Remaining": str(max(0, remaining)),
        "X-RateLimit-Reset": str(reset_at),
        "X-RateLimit-Limit": str(self._config.rate_limit_requests_per_window),
    }

    if remaining < 0:
        retry_after = self._config.rate_limit_window_seconds
        headers["Retry-After"] = str(retry_after)
        return False, headers

    return True, headers
```

### 7.7 Capacity Contention

The mock enforces `max_concurrent` simultaneous calls. Behavior when the limit is hit depends on `overflow_behavior`:

**Queue mode:** The call awaits a slot using an `asyncio.Semaphore`. If the queue (calls waiting for a slot) exceeds `max_queue_depth`, the call is rejected immediately with a 503 error. This simulates backpressure-aware endpoints.

**Reject mode:** The call is immediately rejected with a 503 error when no slot is available. This simulates endpoints with no internal queueing.

```python
async def invoke(self, payload, *, timeout=None, headers=None):
    rate_ok, rate_headers = self._check_rate_limit()
    if not rate_ok:
        return ToolCallResult(
            success=False, result=None,
            error=ToolCallError(
                "rate_limit", "Rate limit exceeded", True,
                float(rate_headers.get("Retry-After", "60")), None,
            ),
            status_code=429,
            latency_ms=1.0,
            headers=rate_headers,
        )

    if self._config.overflow_behavior == "reject" and self._current_concurrent >= self._config.max_concurrent:
        return ToolCallResult(
            success=False, result=None,
            error=ToolCallError("server_error", "Service at capacity (503)", True, None, None),
            status_code=503, latency_ms=1.0, headers=rate_headers,
        )

    async with self._concurrency_semaphore:
        self._current_concurrent += 1
        try:
            should_fail, fail_type = self._should_fail()
            latency = self._generate_latency()
            await asyncio.sleep(latency / 1000.0)

            if should_fail:
                return ToolCallResult(
                    success=False, result=None,
                    error=ToolCallError(
                        fail_type, f"Simulated {fail_type} error", fail_type != "client_error",
                        None, None,
                    ),
                    status_code=500 if fail_type == "server_error" else None,
                    latency_ms=latency,
                    headers=rate_headers,
                )

            result = self._generate_result(payload)
            return ToolCallResult(
                success=True, result=result, error=None,
                status_code=200, latency_ms=latency, headers=rate_headers,
            )
        finally:
            self._current_concurrent -= 1
```

---

## 8. Pre-Built Mock Profiles

The mock ships with pre-built profiles for common tool endpoint archetypes. These are selected by name in configuration.

```python
MOCK_PROFILES: dict[str, MockToolConfig] = {
    "fast_reliable": MockToolConfig(
        latency_distribution="lognormal",
        latency_params={"mean_ms": 50.0, "stddev_ms": 15.0},
        base_failure_rate=0.005,
        max_concurrent=50,
        rate_limit_requests_per_window=1000,
    ),
    "moderate_api": MockToolConfig(
        latency_distribution="lognormal",
        latency_params={"mean_ms": 200.0, "stddev_ms": 80.0},
        base_failure_rate=0.02,
        max_concurrent=10,
        rate_limit_requests_per_window=100,
    ),
    "slow_expensive": MockToolConfig(
        latency_distribution="lognormal",
        latency_params={"mean_ms": 2000.0, "stddev_ms": 500.0},
        base_failure_rate=0.05,
        max_concurrent=5,
        rate_limit_requests_per_window=30,
    ),
    "flaky_external": MockToolConfig(
        latency_distribution="pareto",
        latency_params={"mean_ms": 300.0, "stddev_ms": 200.0, "alpha": 2.5},
        base_failure_rate=0.15,
        load_failure_amplification=3.0,
        max_concurrent=10,
        rate_limit_requests_per_window=60,
    ),
    "database": MockToolConfig(
        latency_distribution="lognormal",
        latency_params={"mean_ms": 10.0, "stddev_ms": 5.0},
        base_failure_rate=0.001,
        max_concurrent=100,
        simulate_rate_limit=False,
    ),
}
```

---

## 9. Configuration

All configuration for tool endpoints is namespaced under `tool_endpoints` in the global FAM configuration (spec 12). The full schema:

```yaml
tool_endpoints:
  # Tool registry — each entry defines a tool endpoint
  endpoints:
    search_api:
      display_name: "Web Search API"
      endpoint_url: "https://api.search.example.com/v1/search"
      method: POST
      idempotent: true
      max_concurrent_calls: 10
      timeout_seconds: 15.0
      auth_type: api_key
      auth_config:
        header_name: "X-API-Key"
        key: "${SEARCH_API_KEY}"
      tags: ["search", "external"]
      rate_limit:
        requests_per_window: 100
        window_seconds: 60.0
      latency_profile:
        expected_p50_ms: 200.0
        expected_p90_ms: 500.0
        expected_p99_ms: 2000.0
        latency_sla_ms: 5000.0

    code_executor:
      display_name: "Code Execution Sandbox"
      endpoint_url: "http://sandbox:8080/execute"
      method: POST
      idempotent: false
      max_concurrent_calls: 5
      timeout_seconds: 30.0
      auth_type: none
      tags: ["compute", "internal"]
      latency_profile:
        expected_p50_ms: 500.0
        expected_p90_ms: 2000.0
        expected_p99_ms: 10000.0
        latency_sla_ms: 30000.0

  # Mock configuration (used when backend is 'mock')
  mock:
    default_profile: "moderate_api"   # Fallback profile for endpoints without specific config
    endpoint_overrides:
      search_api:
        profile: "fast_reliable"
      code_executor:
        profile: "slow_expensive"
        # Override individual settings:
        latency_distribution: "lognormal"
        latency_params:
          mean_ms: 1000.0
          stddev_ms: 400.0
    random_seed: 42
```

---

## 10. Error Handling

**Endpoint unreachable:** If a real HTTP endpoint is unreachable (DNS failure, connection refused), the `HttpToolEndpoint.invoke()` catches the exception and returns a `ToolCallResult` with `error_type="connection"` and `retryable=True`. The instrumented wrapper reports this to the telemetry collector. The dispatch queue (spec 07) decides whether to retry based on the endpoint's idempotency flag and its retry policy.

**Rate limit exceeded:** If the endpoint returns 429, the error is classified as `rate_limit` with `retryable=True`. The `retry_after_seconds` field is populated from the `Retry-After` header if present. The instrumented wrapper updates its rate limit tracker to reflect the new state. The dispatch queue should honor `retry_after_seconds` before retrying.

**Malformed response:** If the endpoint returns a response that cannot be parsed as JSON, the `HttpToolEndpoint` returns a `ToolCallResult` with `success=False`, `error_type="server_error"`, and the raw response body in `raw_error`. This is classified as `retryable=True` on the assumption that it may be a transient error.

**Mock failures:** The mock's failure injection is deterministic and follows the configured failure rate and type distribution. The mock never raises unexpected exceptions — all failures are returned as structured `ToolCallResult` errors.

**Wrapper invariants:** The instrumented wrapper guarantees that `active_calls` is always non-negative (the `finally` block in `invoke` always decrements). It guarantees that a `ToolCallEvent` is pushed to the telemetry collector for every call, regardless of outcome. It guarantees that the rate limit tracker is updated on every call that returns response headers.

---

## 11. Metrics Emitted

The tool endpoint interface emits the following metrics to the observability system (spec 13):

**Counters (per endpoint_id):** `tool_endpoint.calls.total` (per outcome: success/timeout/rate_limit/server_error/client_error/connection), `tool_endpoint.rate_limit_hits` (429 responses), `tool_endpoint.retries.total`, `tool_endpoint.capacity_rejections` (503s from mock capacity overflow).

**Gauges (per endpoint_id):** `tool_endpoint.active_calls`, `tool_endpoint.rate_limit_headroom` (0.0–1.0), `tool_endpoint.queue_depth` (calls waiting for a concurrency slot in mock queue mode).

**Histograms (per endpoint_id):** `tool_endpoint.latency_ms` (full latency distribution of all calls), `tool_endpoint.latency_success_ms` (latency of successful calls only — useful for separating normal latency from timeout-inflated latency).

**Gauges (registry-level):** `tool_registry.endpoints_registered` (total number of registered endpoints), `tool_registry.endpoints_healthy` (endpoints passing health check).

---

## 12. Testing Strategy

### 12.1 Unit Tests

**Registry operations:** Register three endpoints. Verify `get()` returns the correct spec. Verify `find_by_tag()` returns the correct subset. Verify `__contains__` works. Verify duplicate registration raises `ValueError`.

**Rate limit tracker — internal tracking:** Create a tracker with 10 requests per 60 seconds. Record 5 calls. Verify headroom is approximately 0.5. Record 5 more. Verify headroom is approximately 0.0. Wait for the window to expire (or simulate time). Verify headroom returns to 1.0.

**Rate limit tracker — header updates:** Push response headers with `X-RateLimit-Remaining: 3` and `X-RateLimit-Reset: <future>`. Verify headroom reflects the header value. Verify header values take precedence over internal tracking.

**Instrumented wrapper — telemetry reporting:** Create a wrapper with a mock endpoint and a mock telemetry receiver. Invoke the wrapper. Verify the telemetry receiver received a `ToolCallEvent` with correct endpoint_id, latency, and success status.

**Instrumented wrapper — error classification:** Test with a mock endpoint that returns various HTTP status codes (200, 429, 500, 404, timeout). Verify each is classified into the correct `error_type`.

**Mock latency distributions:** Generate 10,000 latency samples from each distribution (fixed, normal, lognormal, pareto). Verify mean is within 10% of configured `mean_ms`. Verify lognormal and pareto produce occasional high values (p99 significantly above mean). Verify fixed produces identical values.

**Mock failure rates:** Run 10,000 calls against a mock with `base_failure_rate=0.1`. Verify approximately 10% fail. Verify failure types are distributed according to `failure_type_weights`.

**Mock rate limit enforcement:** Create a mock with 10 requests per 10 seconds. Submit 15 rapid calls. Verify the first 10 succeed and the last 5 return 429. Verify response headers contain correct `X-RateLimit-Remaining` values.

**Mock capacity contention (queue mode):** Create a mock with `max_concurrent=2` and queue overflow. Launch 5 simultaneous calls. Verify only 2 are processed concurrently. Verify all 5 eventually complete. Verify latency increases for queued calls.

**Mock capacity contention (reject mode):** Create a mock with `max_concurrent=2` and reject overflow. Launch 5 simultaneous calls. Verify 2 succeed and 3 are rejected immediately with 503.

**Mock determinism:** Run the same mock with the same seed twice. Verify identical latency sequences and identical failure sequences.

### 12.2 Integration Tests

**End-to-end with telemetry collector:** Register a mock endpoint with the telemetry collector as a push-mode tool source. Make 20 calls through the instrumented wrapper. Verify the telemetry collector's rolling window contains the correct latency percentiles and error rate.

**Multi-endpoint interaction:** Register three mock endpoints with different profiles (fast, moderate, slow). Run concurrent calls against all three. Verify telemetry snapshots contain independent metrics for each endpoint. Verify the pricing engine computes independent prices for each.

**Rate limit recovery:** Configure a mock with a tight rate limit. Exhaust the limit. Wait for the window to reset. Verify headroom recovers and subsequent calls succeed.

**Health check integration:** Start a mock endpoint, verify health check passes. Inject a failure. Verify health check reflects the failure. Wait for recovery. Verify health check passes again.

### 12.3 Property-Based Tests

Use Hypothesis to generate random `MockToolConfig` parameters (random latencies, failure rates, concurrency limits). Run 100 calls against each configuration. Verify: (a) all returned `ToolCallResult` objects are well-formed (no None fields where a value is required), (b) `success=True` results have a non-None `result` field and None `error`, (c) `success=False` results have a non-None `error` field, (d) latency is always positive.

Generate random call sequences (varying concurrency, timing) against a mock with `max_concurrent=N`. Verify: (a) `current_concurrent` never exceeds `N`, (b) total completed calls equals total submitted calls (no lost calls), (c) the mock does not deadlock.

---

## 13. Dependencies

**Internal:** `fam/types.py` (shared types including `ToolCallEvent`), `fam/telemetry/collector.py` (`TelemetryReceiver` protocol for push), `fam/config/schema.py` (configuration schema).

**External (real endpoints):** `aiohttp` for HTTP client functionality. This is the only external dependency and is only used by `HttpToolEndpoint`. The mock implementation and all data models use only the Python standard library (`asyncio`, `dataclasses`, `collections.deque`, `time`, `math`, `random`, `logging`, `enum`).

**External (mock):** None. The mock has zero external dependencies.

**Interface contracts:** The `ToolEndpoint` protocol is consumed by the `InstrumentedToolEndpoint` wrapper, which is in turn consumed by the dispatch queue (spec 07). The `InstrumentedToolEndpoint` pushes `ToolCallEvent`s to the telemetry collector (spec 01) via the `TelemetryReceiver` protocol. The `ToolRegistry` is read by the pricing engine (spec 02) for baseline latency profiles and by the confirmation handler (spec 06) for display names and idempotency flags.

---

## 14. Open Questions

**Dynamic tool registration.** The current design requires all tools to be registered at startup. Should FAM support dynamic registration — agents discovering and registering new tools at runtime? This would require live telemetry collector reconfiguration, new pricing controllers being created on the fly, and budget manager awareness of new cost categories. The complexity is significant. For the initial implementation, all tools are statically configured. Deferred.

**Tool substitutability.** If two endpoints serve the same function (e.g., two search APIs with different cost and latency profiles), FAM could route agents to the cheaper or faster one. This requires a notion of "tool groups" or "tool functions" that abstracts over individual endpoints. The current per-endpoint pricing already provides a natural routing signal (agents prefer cheaper endpoints), but explicit substitutability support could enable the dispatch queue to make routing decisions on the agent's behalf. Deferred.

**Endpoint-reported metrics.** Some sophisticated tool endpoints may expose their own metrics (internal queue depth, actual rate limit state, health status) via a separate metrics endpoint. Should FAM read these in addition to its own telemetry? For the initial implementation, FAM relies on its own observations (latency, error rate, rate limit headers) rather than endpoint self-reporting. Endpoint-reported metrics could be added as an optional supplement.

**Mock calibration from production traces.** The mock's latency distributions and failure patterns are configurable but not calibrated against real services. Recording production tool call traces and using them to configure mock parameters would make evaluation results more realistic. The mock's architecture supports this (just set the distribution and parameters), but the tooling to record and convert traces is not built. Deferred.

**Webhook / streaming tool endpoints.** The current model assumes request-response tool calls. Some tools use webhooks (tool calls back to FAM when done) or streaming (tool returns a stream of partial results). These patterns have different latency profiles and capacity implications. Supporting them would require extending the `ToolEndpoint` protocol. Deferred to a future version.
