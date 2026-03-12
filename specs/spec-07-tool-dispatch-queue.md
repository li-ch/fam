# Spec 07 — Tool Dispatch Queue

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview), Spec 01 (Telemetry Collector), Spec 04 (Agent Communication Protocol), Spec 06 (Confirmation Handler), Spec 11 (Tool Endpoint Interface)
**Consumed By:** Spec 01 (Telemetry Collector), Spec 08 (Speculative Continuation Manager), Spec 13 (Metrics & Observability)

---

## 1. Purpose

The tool dispatch queue is the last stop between an agent's confirmed tool call and the actual execution against an external endpoint. It receives tool calls that have passed through the confirmation handler (auto-approved or agent-confirmed), organizes them into per-endpoint priority queues, dispatches them to endpoints while respecting rate limits, and routes results back to the correct agent.

This module serves three functions that would be dangerous to conflate:

**Prioritization.** When multiple agents want to use the same endpoint simultaneously, someone must go first. The dispatch queue orders requests by a composite priority score that considers agent budget remaining, external task priority, and time-in-queue (to prevent starvation). Budget-rich or high-priority agents get earlier service, but no agent waits forever.

**Rate limiting.** External endpoints have rate limits. The dispatch queue tracks rate limit headroom from telemetry and its own internal counters, and it holds back requests when dispatching them would risk hitting the limit. This protective layer prevents FAM from burning through an API's rate quota and triggering HTTP 429 responses that would cascade into retries and delays.

**Deferral and result routing.** When an endpoint's queue grows too deep, new requests are deferred — the agent is notified via the communication protocol and enters speculative continuation (spec 08). When a dispatched call completes, the result must be routed back to the correct agent's conversation, handling the case where the agent has moved on to speculative reasoning since issuing the call.

---

## 2. Module Location

```
fam/
├── dispatch/
│   ├── __init__.py
│   ├── queue.py            # ToolDispatchQueue class — main entry point
│   ├── priority.py         # Priority computation logic
│   ├── rate_limiter.py     # Per-endpoint rate limit tracker
│   ├── retry.py            # Retry policy and backoff logic
│   └── result_router.py    # Result delivery to agent conversations
```

Tests live in `tests/test_dispatch/`.

---

## 3. Queue Architecture

The dispatch queue maintains one priority queue per registered tool endpoint. Each queue is an asyncio-compatible priority heap that orders pending tool calls by composite priority score. A pool of dispatch workers (one per endpoint by default, configurable) consumes from these queues and executes calls against the endpoints.

```
              ┌────────────────┐
              │ Confirmation   │
              │ Handler        │
              │ (approved call)│
              └───────┬────────┘
                      │
                      ▼
         ┌────────────────────────┐
         │   ToolDispatchQueue    │
         │                        │
         │  ┌──────────────────┐  │
         │  │ Endpoint A Queue │  │──▶ Worker A ──▶ Endpoint A
         │  │ (priority heap)  │  │
         │  └──────────────────┘  │
         │                        │
         │  ┌──────────────────┐  │
         │  │ Endpoint B Queue │  │──▶ Worker B ──▶ Endpoint B
         │  │ (priority heap)  │  │
         │  └──────────────────┘  │
         │                        │
         │  ┌──────────────────┐  │
         │  │ Endpoint C Queue │  │──▶ Worker C ──▶ Endpoint C
         │  │ (priority heap)  │  │
         │  └──────────────────┘  │
         │                        │
         │  Rate Limiter          │
         │  Result Router         │
         └────────────────────────┘
```

### 3.1 Queue Entry

```python
@dataclass
class DispatchEntry:
    """A tool call waiting in the dispatch queue."""
    tool_call_id: str
    agent_id: str
    endpoint_id: str
    tool_name: str
    tool_args: dict[str, Any]
    cost_charged: Decimal
    enqueued_at: float                     # time.monotonic()
    priority_score: float                  # Lower is higher priority (min-heap)
    future: asyncio.Future                 # Resolved when tool result is ready
    cancelled: bool = False
    retry_count: int = 0
    max_retries: int = 3

    def __lt__(self, other: "DispatchEntry") -> bool:
        return self.priority_score < other.priority_score
```

The `future` is an `asyncio.Future` that the calling code awaits. It is resolved with the tool result or an exception on completion or permanent failure.

### 3.2 Cancellation

An entry can be cancelled before dispatch by setting `cancelled = True`. Cancelled entries are skipped at dequeue time. Cancellation after dispatch is best-effort — the `asyncio.Task` is cancelled but the endpoint may still process the request.

```python
async def cancel(self, tool_call_id: str) -> bool:
    """Cancel a pending tool call.

    Returns True if the call was found and cancelled, False if it was
    already dispatched or not found.
    """
    entry = self._entries.get(tool_call_id)
    if entry is None:
        return False
    if entry.cancelled:
        return False
    entry.cancelled = True
    entry.future.set_exception(DispatchCancelled(tool_call_id))
    self._metrics.record_cancellation(entry.agent_id, entry.endpoint_id)
    return True
```

---

## 4. Priority Computation

Priority determines the order in which tool calls are dispatched from the per-endpoint queue. Lower priority scores are dispatched first (min-heap convention).

### 4.1 Priority Formula

```
priority_score = -(w_b × normalized_budget + w_p × task_priority + w_t × time_bonus)
```

The negative sign converts the max-priority semantics (higher budget → higher priority) into min-heap semantics (lower score → dequeued first).

**`normalized_budget`** (range 0.0–1.0): The agent's current budget as a fraction of the initial budget. Agents with more budget remaining are prioritized because they are further from exhaustion — they likely have more important remaining work that depends on this tool call. An agent at 80% budget is prioritized over one at 20%.

**`task_priority`** (range 0.0–1.0): An externally assigned priority for the agent's task. Defaults to 0.5 if not specified. This allows callers to mark certain tasks as high-priority (e.g., user-facing requests vs. background batch jobs). The priority is set per-agent at registration time and can be updated dynamically.

**`time_bonus`** (range 0.0–1.0): A starvation prevention mechanism. Increases linearly with time-in-queue, reaching 1.0 after `max_wait_seconds` (default: 60.0). This ensures that even the lowest-priority request is eventually dispatched — it cannot be starved indefinitely by higher-priority requests.

```
time_bonus = min(time_in_queue / max_wait_seconds, 1.0)
```

### 4.2 Priority Weights

```python
@dataclass(frozen=True)
class PriorityWeights:
    budget: float = 0.4
    task_priority: float = 0.3
    time_bonus: float = 0.3
```

The weights must sum to 1.0. The defaults give budget the highest weight (rewarding agents that have budget headroom), followed by task priority and time bonus equally weighted.

### 4.3 Priority Computation Implementation

```python
class PriorityCalculator:
    """Computes dispatch priority for tool call entries."""

    def __init__(
        self,
        weights: PriorityWeights,
        max_wait_seconds: float,
        initial_budget: Decimal,
    ) -> None:
        self._weights = weights
        self._max_wait = max_wait_seconds
        self._initial_budget = initial_budget

    def compute(
        self,
        budget_remaining: Decimal,
        task_priority: float,
        time_in_queue: float,
    ) -> float:
        normalized_budget = float(
            min(budget_remaining / self._initial_budget, Decimal("1.0"))
        ) if self._initial_budget > 0 else 0.0

        time_bonus = min(time_in_queue / self._max_wait, 1.0)

        raw_priority = (
            self._weights.budget * normalized_budget
            + self._weights.task_priority * task_priority
            + self._weights.time_bonus * time_bonus
        )

        return -raw_priority  # Negate for min-heap
```

### 4.4 Priority Re-computation

Priority scores are computed at enqueue time and not updated dynamically. Dynamic re-prioritization would require re-heapifying on every budget change. The time bonus provides sufficient starvation prevention, and budget changes between enqueue and dequeue are typically small (the confirmation handler already deducted the cost). If dynamic re-prioritization proves necessary, re-compute priority at dequeue time rather than maintaining heap invariants incrementally.

---

## 5. Rate Limiting

The dispatch queue implements per-endpoint rate limiting to prevent FAM from exceeding external API quotas.

### 5.1 Rate Limit Tracker

```python
@dataclass(frozen=True)
class EndpointRateLimitConfig:
    max_requests_per_window: int           # e.g., 100
    window_seconds: float                  # e.g., 60.0
    max_concurrent: int                    # Max parallel requests to this endpoint
    headroom_fraction: float = 0.1         # Reserve 10% of rate limit as safety margin

class RateLimitTracker:
    """Tracks rate limit consumption per endpoint."""

    def __init__(self, config: EndpointRateLimitConfig) -> None:
        self._config = config
        self._request_timestamps: deque[float] = deque()
        self._active_count: int = 0

    def can_dispatch(self) -> bool:
        """Check if dispatching another request would respect rate limits."""
        self._evict_old_timestamps()

        effective_limit = int(
            self._config.max_requests_per_window
            * (1.0 - self._config.headroom_fraction)
        )

        if len(self._request_timestamps) >= effective_limit:
            return False

        if self._active_count >= self._config.max_concurrent:
            return False

        return True

    def record_dispatch(self) -> None:
        """Record that a request was dispatched."""
        self._request_timestamps.append(time.monotonic())
        self._active_count += 1

    def record_completion(self) -> None:
        """Record that an active request completed."""
        self._active_count = max(0, self._active_count - 1)

    def update_from_headers(
        self,
        remaining: int | None,
        reset_seconds: float | None,
    ) -> None:
        """Update internal state from endpoint rate limit response headers.

        If the endpoint reports X-RateLimit-Remaining, use it to correct
        the internal counter. This handles cases where other clients share
        the same rate limit.
        """
        if remaining is not None and reset_seconds is not None:
            used_in_window = self._config.max_requests_per_window - remaining
            while len(self._request_timestamps) < used_in_window:
                self._request_timestamps.append(time.monotonic())

    def headroom(self) -> float:
        """Current rate limit headroom as a fraction 0.0–1.0."""
        self._evict_old_timestamps()
        used = len(self._request_timestamps)
        total = self._config.max_requests_per_window
        if total <= 0:
            return 1.0
        return max(0.0, 1.0 - used / total)

    def time_until_available(self) -> float:
        """Seconds until the next dispatch slot opens."""
        if self.can_dispatch():
            return 0.0
        if not self._request_timestamps:
            return 0.0
        oldest = self._request_timestamps[0]
        return max(0.0, oldest + self._config.window_seconds - time.monotonic())

    def _evict_old_timestamps(self) -> None:
        cutoff = time.monotonic() - self._config.window_seconds
        while self._request_timestamps and self._request_timestamps[0] < cutoff:
            self._request_timestamps.popleft()
```

### 5.2 Headroom-Based Throttling

The rate limit tracker reserves a configurable headroom fraction (default 10%) of the rate limit as a safety margin. If the endpoint allows 100 requests per minute, FAM will dispatch at most 90. This accounts for timing imprecision, other clients sharing the same rate limit, and burst absorption. The headroom fraction is configurable per endpoint — set to 0.0 when FAM is the sole consumer, 0.1–0.2 for shared endpoints.

### 5.3 Rate Limit Header Integration

When a dispatched tool call receives standard rate limit headers (`X-RateLimit-Remaining`, `X-RateLimit-Reset`, `Retry-After`), the rate limit tracker updates its internal state via `update_from_headers()`. This feedback loop ensures FAM backs off when the endpoint reports lower headroom than estimated, and dispatches more aggressively when other clients stop calling.

---

## 6. Deferral

When an endpoint's queue depth exceeds a configurable threshold, new tool calls are deferred rather than enqueued. Deferral is the dispatch queue's backpressure signal — it tells the agent "this endpoint is busy, you should do something else while you wait."

### 6.1 Deferral Trigger

```python
@dataclass(frozen=True)
class DeferralConfig:
    max_queue_depth: int = 20              # Per-endpoint queue depth limit
    max_estimated_wait_seconds: float = 30.0  # Estimated wait threshold for deferral
```

A tool call is deferred if either condition is true:

1. The endpoint's queue depth (number of pending entries) exceeds `max_queue_depth`.
2. The estimated wait time (queue depth × average dispatch latency) exceeds `max_estimated_wait_seconds`.

### 6.2 Deferral Flow

```python
async def submit(self, entry: DispatchEntry) -> DispatchResult:
    """Submit a confirmed tool call for dispatch.

    Returns immediately with a DispatchResult. If not deferred, the
    result contains a Future that resolves to the tool output. If
    deferred, the result indicates deferral with estimated wait.
    """
    endpoint_queue = self._queues[entry.endpoint_id]

    if self._should_defer(entry.endpoint_id):
        return DispatchResult(
            tool_call_id=entry.tool_call_id,
            status=DispatchStatus.DEFERRED,
            estimated_wait_seconds=self._estimate_wait(entry.endpoint_id),
            position_in_queue=len(endpoint_queue) + 1,
        )

    heapq.heappush(endpoint_queue, entry)
    self._entries[entry.tool_call_id] = entry
    self._notify_worker(entry.endpoint_id)

    return DispatchResult(
        tool_call_id=entry.tool_call_id,
        status=DispatchStatus.QUEUED,
        future=entry.future,
        position_in_queue=len(endpoint_queue),
    )
```

When a call is deferred, the speculative continuation manager (spec 08) notifies the agent and manages provisional reasoning. The deferred call is not placed in the queue — the agent can re-submit it later or abandon it.

### 6.3 Deferral Result

```python
class DispatchStatus(str, Enum):
    QUEUED = "queued"
    DEFERRED = "deferred"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class DispatchResult:
    tool_call_id: str
    status: DispatchStatus
    future: asyncio.Future | None = None
    result: Any = None
    error: str | None = None
    estimated_wait_seconds: float | None = None
    position_in_queue: int | None = None
    retry_count: int = 0
    latency_ms: float | None = None
```

---

## 7. Dispatch Workers

Each endpoint has one or more dispatch worker tasks that consume from the endpoint's priority queue and execute tool calls.

### 7.1 Worker Loop

```python
async def _worker_loop(self, endpoint_id: str) -> None:
    """Dispatch worker for a single endpoint."""
    rate_limiter = self._rate_limiters[endpoint_id]
    endpoint = self._endpoints[endpoint_id]

    while not self._shutdown:
        entry = await self._dequeue(endpoint_id)

        if entry.cancelled:
            continue

        while not rate_limiter.can_dispatch():
            wait = rate_limiter.time_until_available()
            await asyncio.sleep(max(wait, 0.01))
            if entry.cancelled:
                break

        if entry.cancelled:
            continue

        rate_limiter.record_dispatch()
        try:
            result = await asyncio.wait_for(
                endpoint.execute(entry.tool_name, entry.tool_args),
                timeout=self._config.call_timeout_seconds,
            )
            rate_limiter.record_completion()
            self._record_success(entry, result)
            entry.future.set_result(result)
        except asyncio.TimeoutError:
            rate_limiter.record_completion()
            await self._handle_failure(entry, "timeout", endpoint)
        except Exception as exc:
            rate_limiter.record_completion()
            await self._handle_failure(entry, str(exc), endpoint)
```

### 7.2 Dequeue with Notification

Workers block on an `asyncio.Event` when the queue is empty. The `submit()` method signals the event when a new entry is enqueued, waking the worker.

```python
async def _dequeue(self, endpoint_id: str) -> DispatchEntry:
    """Wait for and return the next entry from the endpoint's queue."""
    queue = self._queues[endpoint_id]
    event = self._queue_events[endpoint_id]

    while True:
        if queue:
            return heapq.heappop(queue)
        event.clear()
        await event.wait()

def _notify_worker(self, endpoint_id: str) -> None:
    """Wake the worker for this endpoint."""
    self._queue_events[endpoint_id].set()
```

---

## 8. Retry Logic

Transient failures (timeouts, HTTP 5xx, connection errors) are retried with exponential backoff. Permanent failures (HTTP 4xx, invalid arguments) are not retried.

### 8.1 Retry Policy

```python
@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 3
    initial_backoff_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0
    retryable_errors: frozenset[str] = frozenset({
        "timeout", "server_error", "connection_error", "rate_limit",
    })
```

### 8.2 Retry Implementation

```python
async def _handle_failure(
    self,
    entry: DispatchEntry,
    error_type: str,
    endpoint: ToolEndpoint,
) -> None:
    """Handle a failed tool call dispatch."""
    self._push_tool_call_event(entry, success=False, error_type=error_type)

    if error_type not in self._config.retry.retryable_errors:
        entry.future.set_exception(
            ToolCallFailed(entry.tool_call_id, error_type, permanent=True)
        )
        self._metrics.record_permanent_failure(
            entry.agent_id, entry.endpoint_id, error_type,
        )
        return

    if entry.retry_count >= entry.max_retries:
        entry.future.set_exception(
            ToolCallFailed(
                entry.tool_call_id, error_type,
                permanent=False,
                message=f"Max retries ({entry.max_retries}) exceeded",
            )
        )
        self._metrics.record_retries_exhausted(
            entry.agent_id, entry.endpoint_id, error_type,
        )
        return

    entry.retry_count += 1
    backoff = min(
        self._config.retry.initial_backoff_seconds
        * (self._config.retry.backoff_multiplier ** (entry.retry_count - 1)),
        self._config.retry.max_backoff_seconds,
    )

    if error_type == "rate_limit":
        rate_limiter = self._rate_limiters[entry.endpoint_id]
        wait = rate_limiter.time_until_available()
        backoff = max(backoff, wait)

    self._metrics.record_retry(
        entry.agent_id, entry.endpoint_id, error_type, entry.retry_count,
    )

    await asyncio.sleep(backoff)

    queue = self._queues[entry.endpoint_id]
    heapq.heappush(queue, entry)
    self._notify_worker(entry.endpoint_id)
```

### 8.3 Rate Limit Retry Special Case

When a tool call fails with a rate limit error (HTTP 429), the retry backoff is at least as long as `time_until_available()` from the rate limiter. If the endpoint returns a `Retry-After` header, that value is used instead if it is larger. This ensures the retry does not immediately hit the rate limit again.

---

## 9. Result Routing

When a dispatched tool call completes, the result must be delivered back to the correct agent's conversation. This is straightforward for synchronous tool calls (the agent's graph node is awaiting the future). For deferred calls that completed while the agent was in speculative continuation, result routing is more complex and involves the speculative continuation manager (spec 08).

### 9.1 Result Delivery

```python
class ResultRouter:
    """Routes tool results back to agent conversations."""

    def __init__(
        self,
        protocol: AgentCommunicationProtocol,
        speculative_manager: SpeculativeContinuationManager | None,
        metrics: MetricsCollector,
    ) -> None:
        self._protocol = protocol
        self._speculative = speculative_manager
        self._metrics = metrics

    async def deliver(
        self,
        entry: DispatchEntry,
        result: Any,
        latency_ms: float,
    ) -> None:
        """Deliver a tool result to the agent.

        If the agent is in speculative continuation for this tool_call_id,
        delegates to the speculative manager for reconciliation.
        Otherwise, injects the result directly via the protocol.
        """
        if (
            self._speculative is not None
            and self._speculative.is_speculating(entry.agent_id, entry.tool_call_id)
        ):
            await self._speculative.on_tool_result(
                agent_id=entry.agent_id,
                tool_call_id=entry.tool_call_id,
                tool_name=entry.tool_name,
                result=result,
                latency_ms=latency_ms,
                cost_charged=entry.cost_charged,
            )
        else:
            self._protocol.inject_tool_result(
                agent_id=entry.agent_id,
                tool_call_id=entry.tool_call_id,
                tool_name=entry.tool_name,
                result=result,
                was_speculative=False,
                speculative_tokens_discarded=0,
                latency_ms=latency_ms,
                cost_charged=entry.cost_charged,
            )

        self._metrics.record_result_delivered(
            entry.agent_id, entry.endpoint_id, latency_ms,
        )
```

### 9.2 Synchronous vs. Deferred Result Paths

**Synchronous path:** The agent's FAM node awaits `entry.future`. When resolved, the node writes the result into LangGraph state as a `ToolMessage`. No result routing is needed — the future is the delivery mechanism.

**Deferred path:** The tool call was deferred, resubmitted, and dispatched while the agent continued speculatively. The `ResultRouter` delivers the result to the speculative continuation manager (spec 08) for reconciliation — checkpointing, potential rollback, and real result injection.

### 9.3 Failure Result Delivery

When a tool call fails permanently (non-retryable error or retries exhausted), the failure is delivered as an error result, not silently dropped. The agent receives a tool result message containing the error information, formatted through the signal formatter:

```python
async def deliver_failure(
    self,
    entry: DispatchEntry,
    error_type: str,
    error_message: str,
) -> None:
    """Deliver a failure result to the agent."""
    error_result = {
        "error": True,
        "error_type": error_type,
        "message": error_message,
        "tool_name": entry.tool_name,
        "retries_attempted": entry.retry_count,
    }
    await self.deliver(entry, error_result, latency_ms=0.0)
```

---

## 10. Telemetry Integration

The dispatch queue pushes telemetry events to the telemetry collector (spec 01) for two purposes: feeding tool endpoint utilization metrics, and enabling the pricing engine to incorporate dispatch state into tool prices.

### 10.1 Tool Call Events

After every tool call completion (success or failure), the dispatch queue pushes a `ToolCallEvent` to the telemetry collector:

```python
def _push_tool_call_event(
    self,
    entry: DispatchEntry,
    success: bool,
    error_type: str | None = None,
) -> None:
    event = ToolCallEvent(
        endpoint_id=entry.endpoint_id,
        started_at=entry.dispatched_at,
        completed_at=time.monotonic(),
        latency_ms=(time.monotonic() - entry.dispatched_at) * 1000,
        success=success,
        error_type=error_type,
    )
    asyncio.create_task(
        self._telemetry.receive_tool_call_event(event)
    )
```

### 10.2 Queue State Reporting

On each telemetry tick, the dispatch queue reports its current state per endpoint (queue depth, active calls, rate limit headroom) via `get_endpoint_state(endpoint_id)` and `get_all_endpoint_states()`. The telemetry collector incorporates these into the `ToolEndpointTelemetrySnapshot`.

---

## 11. ToolDispatchQueue Class

The main class that integrates all components.

### 11.1 Initialization

```python
class ToolDispatchQueue:
    def __init__(
        self,
        config: DispatchConfig,
        telemetry: TelemetryCollector,
        protocol: AgentCommunicationProtocol,
        speculative_manager: SpeculativeContinuationManager | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._config = config
        self._telemetry = telemetry
        self._protocol = protocol
        self._metrics = metrics or NoopMetricsCollector()
        self._priority_calc = PriorityCalculator(
            config.priority_weights,
            config.max_wait_seconds,
            config.initial_budget,
        )
        self._rate_limiters: dict[str, RateLimitTracker] = {}
        self._queues: dict[str, list[DispatchEntry]] = {}
        self._queue_events: dict[str, asyncio.Event] = {}
        self._entries: dict[str, DispatchEntry] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._result_router = ResultRouter(protocol, speculative_manager, self._metrics)
        self._shutdown = False
```

### 11.2 Endpoint Registration

```python
async def register_endpoint(
    self,
    endpoint_id: str,
    endpoint: ToolEndpoint,
    rate_limit_config: EndpointRateLimitConfig | None = None,
    num_workers: int = 1,
) -> None:
    """Register a tool endpoint with the dispatch queue."""
    self._endpoints[endpoint_id] = endpoint
    self._queues[endpoint_id] = []
    self._queue_events[endpoint_id] = asyncio.Event()

    if rate_limit_config:
        self._rate_limiters[endpoint_id] = RateLimitTracker(rate_limit_config)
    else:
        self._rate_limiters[endpoint_id] = RateLimitTracker(
            self._config.default_rate_limit
        )

    for i in range(num_workers):
        task = asyncio.create_task(
            self._worker_loop(endpoint_id),
            name=f"dispatch-{endpoint_id}-{i}",
        )
        self._workers[f"{endpoint_id}-{i}"] = task
```

### 11.3 Lifecycle

```python
async def start(self) -> None:
    """Start the dispatch queue. Workers are started per-endpoint during registration."""
    self._shutdown = False

async def stop(self) -> None:
    """Stop all workers and clean up."""
    self._shutdown = True
    for event in self._queue_events.values():
        event.set()
    for task in self._workers.values():
        task.cancel()
    await asyncio.gather(*self._workers.values(), return_exceptions=True)
    self._workers.clear()

    for entry in self._entries.values():
        if not entry.future.done():
            entry.future.set_exception(
                DispatchShutdown("Dispatch queue is shutting down")
            )
    self._entries.clear()
```

---

## 12. Configuration

All configuration for the tool dispatch queue is namespaced under `dispatch` in the global FAM configuration (spec 12).

```yaml
dispatch:
  priority:
    weights: { budget: 0.4, task_priority: 0.3, time_bonus: 0.3 }
    max_wait_seconds: 60.0
    initial_budget: 100.0

  deferral:
    max_queue_depth: 20
    max_estimated_wait_seconds: 30.0

  retry:
    max_retries: 3
    initial_backoff_seconds: 1.0
    backoff_multiplier: 2.0
    max_backoff_seconds: 30.0
    retryable_errors: ["timeout", "server_error", "connection_error", "rate_limit"]

  call_timeout_seconds: 60.0
  default_workers_per_endpoint: 1

  default_rate_limit:
    max_requests_per_window: 60
    window_seconds: 60.0
    max_concurrent: 5
    headroom_fraction: 0.1

  # Per-endpoint overrides (optional)
  # endpoints:
  #   search_api:
  #     rate_limit: { max_requests_per_window: 100, window_seconds: 60.0, max_concurrent: 10 }
  #     workers: 3
```

---

## 13. Error Handling

**Worker crash:** If a dispatch worker raises an unhandled exception, it is caught, logged at CRITICAL level, and the worker is restarted automatically. Pending queue entries are preserved.

**Queue corruption:** If a heap operation raises, the affected queue is rebuilt via `heapq.heapify()` and dispatch continues.

**Endpoint unreachable:** If all retries are exhausted for every call to an endpoint, the worker continues processing — it does not self-disable. The telemetry collector's health check (spec 01) tracks endpoint health independently.

**Memory pressure:** Per-endpoint queues are bounded by `max_queue_depth` (via deferral). The rate limit tracker's timestamp deque is bounded by the window. No unbounded data structures exist in the dispatch path.

**Future resolution safety:** All code paths guarantee `entry.future` is resolved (result, exception, or cancellation). The `stop()` method resolves all pending futures with `DispatchShutdown`.

---

## 14. Metrics Emitted

The dispatch queue emits the following metrics to the observability system (spec 13):

**Counters:**
- `dispatch.submitted.total` (per endpoint_id)
- `dispatch.dispatched.total` (per endpoint_id)
- `dispatch.deferred.total` (per endpoint_id)
- `dispatch.completed.total` (per endpoint_id, per outcome: success, failure)
- `dispatch.cancelled.total` (per endpoint_id)
- `dispatch.retries.total` (per endpoint_id, per error_type)
- `dispatch.retries_exhausted.total` (per endpoint_id)
- `dispatch.results_delivered.total` (per endpoint_id)

**Gauges:**
- `dispatch.queue_depth` (per endpoint_id)
- `dispatch.active_calls` (per endpoint_id)
- `dispatch.rate_limit_headroom` (per endpoint_id)
- `dispatch.pending_total` (total entries across all queues)

**Histograms:**
- `dispatch.queue_wait_ms` (time from enqueue to dispatch start, per endpoint_id)
- `dispatch.call_latency_ms` (time for the actual tool call, per endpoint_id)
- `dispatch.total_latency_ms` (time from submit to result delivery, per endpoint_id)
- `dispatch.retry_backoff_ms` (backoff duration per retry, per endpoint_id)
- `dispatch.priority_score` (distribution of priority scores at enqueue time)

---

## 15. Testing Strategy

### 15.1 Unit Tests

**Priority computation:** Verify that higher budget → lower priority score (higher priority). Verify that time bonus increases priority linearly. Verify that task_priority > 0.5 increases priority. Test normalization boundaries: zero budget, budget exceeding initial, negative values.

**Rate limit tracker:** Create a tracker with max 10 requests per 60 seconds. Dispatch 9 requests. Verify `can_dispatch()` returns True. Dispatch 1 more. Verify `can_dispatch()` returns False (effective limit 9 with 10% headroom). Advance time past the window. Verify `can_dispatch()` returns True. Test `update_from_headers` with rate limit data.

**Deferral decision:** Create a queue with `max_queue_depth=5`. Enqueue 5 entries. Submit a 6th. Verify it is deferred. Dequeue one. Submit another. Verify it is enqueued (not deferred).

**Retry backoff:** Verify exponential backoff: first retry = 1s, second = 2s, third = 4s (with multiplier 2.0). Verify capping at `max_backoff_seconds`. Verify that rate limit retries use the longer of backoff and `time_until_available`.

**Cancellation:** Enqueue an entry, cancel it by `tool_call_id`. Verify the future is resolved with `DispatchCancelled`. Verify the entry is skipped during dequeue.

**Heap ordering:** Enqueue entries with known priorities. Dequeue them. Verify they come out in priority order (lowest score first).

### 15.2 Integration Tests

**Full dispatch cycle:** Register a mock endpoint. Submit a tool call. Verify it is enqueued, dispatched, and the future resolves with the mock result. Verify a `ToolCallEvent` is pushed to the telemetry collector.

**Rate-limited dispatch:** Register an endpoint with max 2 concurrent. Submit 3 calls simultaneously. Verify the first 2 are dispatched immediately, the 3rd waits until one completes.

**Deferral-to-completion:** Set `max_queue_depth=2`. Enqueue 2 calls. Submit a 3rd. Verify deferral. Complete one of the enqueued calls. Resubmit the deferred call. Verify it is now enqueued and dispatched.

**Retry flow:** Register a mock endpoint that fails twice then succeeds. Submit a call. Verify 2 retries occur with appropriate backoff, then the call succeeds.

**Worker restart on crash:** Register a mock endpoint that raises an unhandled exception. Verify the worker restarts and subsequent calls are processed.

**Graceful shutdown:** Register an endpoint, enqueue several calls, call `stop()`. Verify all pending futures are resolved with `DispatchShutdown`. Verify all worker tasks are cancelled.

### 15.3 Property-Based Tests

Use Hypothesis to generate random sequences of submit, cancel, and complete events for multiple endpoints. Verify that:

1. Every submitted entry that is not cancelled is eventually dispatched or deferred.
2. Every dispatched entry has its future resolved (with result or error).
3. Queue depth never exceeds `max_queue_depth` (deferred entries are not counted).
4. The number of active calls per endpoint never exceeds `max_concurrent`.
5. Priority ordering is respected (entries dequeued in priority order, verified by comparing enqueue priority scores).

---

## 16. Dependencies

**Internal:** `fam/types.py` (shared types including `ToolCallEvent`), `fam/telemetry/collector.py` (spec 01, telemetry push interface), `fam/signals/protocol.py` (spec 04, agent communication protocol), `fam/speculative/manager.py` (spec 08, speculative continuation — optional), `fam/config/schema.py` (configuration schema), `fam/metrics/collector.py` (spec 13, metrics).

**External:** `asyncio` (standard library), `heapq` (standard library), `dataclasses` (standard library), `collections.deque` (standard library), `time` (standard library), `enum` (standard library). No third-party dependencies. The dispatch queue's only I/O is through the `ToolEndpoint` protocol (spec 11), which is injected at registration time. This makes the dispatch queue fully testable with mock endpoints.

**Interface contracts:** The dispatch queue depends on `ToolEndpoint.execute()` (spec 11) for actual tool call execution. It depends on the telemetry collector's `receive_tool_call_event()` method for pushing completion events. It depends on the speculative continuation manager's `is_speculating()` and `on_tool_result()` for deferred result routing. All dependencies are protocol-based.

---

## 17. Open Questions

**Dynamic worker scaling.** Should the number of dispatch workers per endpoint scale dynamically based on queue depth? Dynamic scaling would improve throughput during bursts but adds complexity (worker lifecycle management, scale-down coordination). Deferred pending evidence of throughput bottlenecks.

**Deferred call re-pricing.** When a deferred call is resubmitted, should it be re-priced at the current price or the original quoted price? Re-pricing rewards agents who defer; using the original price maintains revenue neutrality. The current implementation routes resubmissions through the confirmation handler, which prices at the current level.

**Queue persistence.** All queue state is in-memory. Should the dispatch queue support persistent queueing (e.g., via SQLite or Redis) for crash recovery? Deferred to production readiness phase.

**Adaptive rate limit detection.** Should the dispatch queue automatically detect rate limits by observing HTTP 429 responses, even without explicit configuration? This "probe-based" rate limiting would allow FAM to work with arbitrary endpoints. Deferred — manual configuration is sufficient for the initial implementation.
