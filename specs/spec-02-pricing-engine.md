# Spec 02 — Pricing Engine

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview), Spec 01 (Telemetry Collector)
**Consumed By:** Spec 03 (Budget Manager), Spec 05 (Tiered Signal Formatter), Spec 06 (Confirmation Handler), Spec 08 (Speculative Continuation Manager), Spec 13 (Metrics & Observability)

---

## 1. Purpose

The pricing engine is the mechanism that translates real-time resource scarcity into actionable economic signals. It consumes normalized telemetry snapshots from the telemetry collector (spec 01) and produces two continuously-updated prices — a reasoning price reflecting GPU inference scarcity and a tool price reflecting external endpoint scarcity. These prices are the foundation of every downstream orchestration decision: budget deduction amounts, confirmation thresholds, agent-facing cost signals, and speculative continuation incentives.

The pricing engine implements a dual proportional-integral (PI) controller architecture. Each price is computed by an independent controller that tracks the error between current resource utilization and a target utilization setpoint. The proportional term reacts to the instantaneous gap; the integral term accumulates persistent deviations to eliminate steady-state error. This control-theoretic framing is not incidental — the prices the engine produces are approximations of the shadow prices (Lagrange multipliers) that would emerge from solving the centralized resource allocation problem, under the assumption that agents are rational budget-constrained optimizers. The connection to shadow prices is formalized in section 8.

This spec defines the controller architecture, the exact update rules for both prices, the clamping and smoothing mechanisms, the price ratio computation, the interface the engine exposes to other modules, and the full configuration surface.

---

## 2. Module Location

```
fam/
├── pricing/
│   ├── __init__.py
│   ├── engine.py          # PricingEngine class — main entry point
│   ├── controllers.py     # PIController, ProportionalController implementations
│   └── shadow.py          # Shadow price reference computation (for evaluation)
```

Tests live in `tests/test_pricing/`.

---

## 3. Price Semantics

FAM maintains two independent prices that map to the two categories of shared resources.

### 3.1 Reasoning Price

**`reasoning_price: Decimal`** — the cost per token of LLM inference under current GPU load conditions. When the GPU inference cluster is lightly loaded, the reasoning price is near its floor (making inference cheap and encouraging agents to think deeply). When the cluster is congested, the reasoning price rises toward its ceiling (discouraging speculative reasoning and encouraging agents to be concise or defer).

The reasoning price is not a literal dollar cost. It is an abstract unit internal to FAM's budget system. One unit of reasoning price deducted from an agent's budget represents one token of inference consumed at the current scarcity level. The absolute value matters only relative to the tool price and to the agent's budget balance and replenishment rate.

Inputs to the reasoning price controller:
- **GPU utilization** (`smoothed_utilization` from `GPUTelemetrySnapshot`) — the composite utilization score combining queue depth, KV-cache occupancy, and active requests. This is the primary input.
- **KV-cache pressure** (`smoothed_kv_cache_occupancy`) — used as a secondary signal with its own gain, because KV-cache exhaustion is a hard constraint that requires aggressive pricing even when overall utilization appears moderate.
- **Batch queue depth** (`smoothed_queue_depth`) — used as a leading indicator with a small additional gain, to make price react to queueing before it manifests in utilization.

### 3.2 Tool Price

**`tool_price: dict[str, Decimal]`** — the cost per invocation of a specific tool endpoint. Each registered tool endpoint has an independent price. When an endpoint is idle with ample rate limit headroom, its price is near the floor. When it is congested, error-prone, or nearing its rate limit, the price rises.

Inputs to each endpoint's tool price controller:
- **Tool utilization** (`smoothed_utilization` from `ToolEndpointTelemetrySnapshot`) — the composite score combining active calls, rate limit headroom, and error rate. This is the primary input.
- **Latency ratio** — the ratio of current `smoothed_latency_p50_ms` to the endpoint's expected baseline latency. A ratio above 1.0 indicates degradation and contributes upward price pressure.
- **Rate limit headroom** (`smoothed_rate_limit_headroom`) — given an independent term because running out of rate limit is a hard failure with potentially long recovery (must wait for the rate limit window to reset).

### 3.3 Price Ratio

The **relative price ratio** `tool_price / reasoning_price` is a key signal for agent decision-making. When the ratio is high, tools are expensive relative to thinking — agents should reason more and call tools less. When the ratio is low, tools are cheap relative to thinking — agents should use tools aggressively.

The pricing engine computes and exposes this ratio per endpoint. The signal formatter (spec 05) uses it to generate natural-language signals like "tools are currently 3× more expensive than reasoning" or "reasoning is currently scarce."

---

## 4. Controller Architecture

### 4.1 PI Controller

The default controller is a discrete-time proportional-integral (PI) controller. Given a utilization signal \(u_t \in [0, 1]\) and a target setpoint \(u^* \in [0, 1]\), the controller computes a price \(p_t\) as follows.

**Error signal:**

$$
e_t = u_t - u^*
$$

When \(e_t > 0\), utilization exceeds the target — the resource is overloaded and the price should rise. When \(e_t < 0\), there is spare capacity and the price should fall.

**Proportional term:**

$$
P_t = K_p \cdot e_t
$$

The proportional gain \(K_p\) determines how aggressively the price reacts to the current error. A higher \(K_p\) means sharper price moves for the same utilization change.

**Integral term:**

$$
I_t = I_{t-1} + K_i \cdot e_t \cdot \Delta t
$$

The integral gain \(K_i\) determines how quickly the accumulated error drives price. The integral term eliminates steady-state error — if utilization persistently sits above the setpoint, the integral accumulates and pushes the price up even if the proportional term alone is insufficient. \(\Delta t\) is the time (in seconds) since the last update, ensuring the integral is time-normalized regardless of update frequency.

**Integral windup prevention:**

The integral term is clamped to a configurable range \([I_{\min}, I_{\max}]\) to prevent windup — a condition where the integral accumulates to extreme values during prolonged saturation and then takes a long time to unwind when conditions improve.

$$
I_t = \text{clamp}(I_t, I_{\min}, I_{\max})
$$

**Raw price output:**

$$
p_t^{\text{raw}} = p_{\text{base}} + P_t + I_t
$$

where \(p_{\text{base}}\) is a configurable base price that the controller output is added to. The base price represents the "fair" price at the setpoint — the price when utilization equals the target.

**Clamped price output:**

$$
p_t = \text{clamp}(p_t^{\text{raw}}, p_{\text{floor}}, p_{\text{ceiling}})
$$

The floor prevents prices from going to zero (which would make resources appear free and could cause pathological overuse). The ceiling prevents prices from going to infinity (which could drain budgets in a single tick and make the system unusable).

### 4.2 Proportional-Only Controller

A simpler alternative controller that omits the integral term. Useful for initial deployments where the additional complexity of integral tuning is undesired.

$$
p_t = \text{clamp}(p_{\text{base}} + K_p \cdot e_t, \; p_{\text{floor}}, \; p_{\text{ceiling}})
$$

The proportional-only controller will exhibit steady-state error — the price will not perfectly converge to a level that holds utilization at the setpoint. For many practical scenarios this is acceptable, because the goal is directional correctness (prices up when congested, down when idle) rather than precise setpoint tracking.

### 4.3 Controller Dynamics and Tuning Guidance

Choosing gain parameters is the most important tuning decision for the pricing engine. Poorly chosen gains produce pathological behavior — oscillating prices that confuse agents, sluggish prices that fail to prevent congestion, or prices that overshoot and create artificial scarcity.

**Proportional gain \(K_p\):** Controls the steepness of the price response to utilization error. A useful starting heuristic: set \(K_p\) so that when utilization is at the ceiling (error = 1.0 - setpoint), the proportional term alone pushes the price to approximately 70% of the range between base price and ceiling price.

$$
K_p \approx 0.7 \times \frac{p_{\text{ceiling}} - p_{\text{base}}}{1.0 - u^*}
$$

For the defaults (\(p_{\text{ceiling}} = 10.0\), \(p_{\text{base}} = 0.5\), \(u^* = 0.7\)): \(K_p \approx 0.7 \times 9.5 / 0.3 \approx 22.2\). The configured default of 1.0 is deliberately conservative — it requires sustained congestion (accumulated via the integral term) to reach high prices. A higher \(K_p\) makes the system more aggressive but also more volatile.

**Integral gain \(K_i\):** Controls how quickly persistent utilization error accumulates into price pressure. The ratio \(K_i / K_p\) determines the "integral time constant" \(\tau_i = K_p / K_i\) — the time in seconds for the integral term to equal the proportional term under constant error. For the defaults (\(K_p = 1.0\), \(K_i = 0.1\)): \(\tau_i = 10\) seconds. This means that if utilization sits 10% above the setpoint for 10 seconds, the integral term will equal the proportional term, effectively doubling the price pressure.

**Setpoint \(u^*\):** The target utilization. Setting this too high (e.g., 0.9) leaves little headroom for bursts — the system is always near the edge and prices oscillate around the congestion point. Setting it too low (e.g., 0.3) wastes capacity — resources sit idle while prices stay elevated. The default of 0.7 provides a 30% headroom buffer, which is standard in queueing-theory-informed capacity planning.

**Stability criterion:** For the PI controller to be stable (prices converge rather than oscillate), the gains must satisfy the following rough condition, adapted from discrete-time PI controller theory:

$$
K_p \cdot \Delta t + K_i \cdot \Delta t^2 < 2 \cdot \frac{p_{\text{ceiling}} - p_{\text{floor}}}{\max(\text{utilization change per tick})}
$$

This is validated at configuration load time. If the condition is violated, a WARNING is logged recommending gain reduction.

### 4.4 Controller Selection

The controller type is selected per price channel (reasoning or tool) via configuration. The default is PI for both. Switching to proportional-only requires only a configuration change — no code changes.

```python
class ControllerType(str, Enum):
    PROPORTIONAL = "proportional"
    PI = "pi"
```

---

## 5. Controller Implementation

### 5.1 Base Controller Protocol

```python
class PriceController(Protocol):
    """Interface for all price controller implementations."""

    def update(
        self,
        utilization: float,
        timestamp: float,
    ) -> Decimal:
        """Compute a new price given current utilization.

        Args:
            utilization: Current resource utilization, 0.0–1.0.
            timestamp: time.monotonic() of this update.

        Returns:
            The new price as a Decimal, already clamped to [floor, ceiling].
        """
        ...

    def reset(self) -> None:
        """Reset controller state (integral accumulator, etc.)."""
        ...

    @property
    def current_price(self) -> Decimal:
        """The most recently computed price."""
        ...

    @property
    def trend(self) -> str:
        """Price trend direction: 'rising', 'falling', or 'stable'."""
        ...
```

### 5.2 PI Controller Implementation

```python
@dataclass
class PIControllerConfig:
    """Configuration for a single PI controller instance."""

    setpoint: float = 0.7             # Target utilization
    kp: float = 1.0                   # Proportional gain
    ki: float = 0.1                   # Integral gain
    base_price: Decimal = Decimal("0.5")
    floor: Decimal = Decimal("0.01")
    ceiling: Decimal = Decimal("10.0")
    integral_min: float = -5.0        # Integral windup lower clamp
    integral_max: float = 5.0         # Integral windup upper clamp
    trend_threshold: float = 0.005    # Min price change to register as rising/falling


class PIController:
    """Discrete-time PI controller for price computation."""

    def __init__(self, config: PIControllerConfig) -> None:
        self._config = config
        self._integral: float = 0.0
        self._current_price: Decimal = config.base_price
        self._previous_price: Decimal = config.base_price
        self._last_timestamp: float | None = None

    def update(self, utilization: float, timestamp: float) -> Decimal:
        error = utilization - self._config.setpoint

        if self._last_timestamp is not None:
            dt = max(timestamp - self._last_timestamp, 0.001)
        else:
            dt = 1.0

        self._integral += self._config.ki * error * dt
        self._integral = max(
            self._config.integral_min,
            min(self._config.integral_max, self._integral),
        )

        proportional = self._config.kp * error
        raw = float(self._config.base_price) + proportional + self._integral

        clamped = Decimal(str(max(
            float(self._config.floor),
            min(float(self._config.ceiling), raw),
        ))).quantize(Decimal("0.0001"))

        self._previous_price = self._current_price
        self._current_price = clamped
        self._last_timestamp = timestamp
        return clamped

    def reset(self) -> None:
        self._integral = 0.0
        self._current_price = self._config.base_price
        self._previous_price = self._config.base_price
        self._last_timestamp = None

    @property
    def current_price(self) -> Decimal:
        return self._current_price

    @property
    def trend(self) -> str:
        delta = float(self._current_price - self._previous_price)
        if delta > self._config.trend_threshold:
            return "rising"
        elif delta < -self._config.trend_threshold:
            return "falling"
        return "stable"
```

### 5.3 Multi-Signal Input Composition

The controllers described above accept a single `utilization` float. In practice, the reasoning price depends on multiple telemetry signals (GPU utilization, KV-cache pressure, queue depth). These are composed into a single effective utilization signal before being fed to the controller.

```python
@dataclass(frozen=True)
class ReasoningInputWeights:
    """Weights for composing multiple GPU signals into a single utilization input."""
    gpu_utilization: float = 0.6      # Composite utilization from telemetry
    kv_cache_pressure: float = 0.25   # Direct KV-cache occupancy
    queue_depth_signal: float = 0.15  # Normalized queue depth

@dataclass(frozen=True)
class ToolInputWeights:
    """Weights for composing tool endpoint signals into a single utilization input."""
    tool_utilization: float = 0.55    # Composite utilization from telemetry
    latency_ratio: float = 0.25      # current_p50 / baseline_p50, clamped to [0, 1]
    headroom_pressure: float = 0.20   # 1.0 - rate_limit_headroom
```

The composition function:

```python
def compose_utilization(
    signals: dict[str, float],
    weights: dict[str, float],
) -> float:
    """Weighted sum of signals, clamped to [0.0, 1.0].

    All signals are expected to be in [0.0, 1.0].
    Weights must sum to 1.0 (validated at config load time).
    """
    total = sum(weights[k] * signals[k] for k in weights)
    return max(0.0, min(1.0, total))
```

---

## 6. PricingEngine Class

The `PricingEngine` is the main class of this module. It owns the controllers for all price channels, consumes telemetry snapshots, orchestrates updates, and exposes prices to downstream modules.

### 6.1 Initialization

```python
class PricingEngine:
    def __init__(self, config: PricingConfig) -> None:
        """
        Args:
            config: Pricing engine configuration (gains, floors, ceilings, etc.)
                    Loaded from the global FAM config (spec 12).
        """
```

The constructor creates a reasoning price controller and a dict of per-endpoint tool price controllers. Tool controllers are created lazily — when a new endpoint appears in telemetry for the first time, a controller is instantiated with the default (or endpoint-specific override) configuration.

### 6.2 Update Cycle

```python
async def update(self, snapshot: SystemTelemetrySnapshot) -> PriceUpdate:
    """Compute new prices from a telemetry snapshot.

    Called on each pricing tick (typically aligned with the telemetry tick).
    Returns a PriceUpdate that is published to downstream consumers.
    """
```

The update cycle is the core loop of the pricing engine — it runs once per tick and must complete quickly (target: under 1ms for up to 50 tool endpoints). All operations are synchronous within the async method — no I/O is performed. The only input is the telemetry snapshot, which is read from memory.

The update cycle proceeds as follows:

1. **Validate snapshot.** If the snapshot is `None` or has the same timestamp as the last processed snapshot, return the current `PriceUpdate` unchanged (idempotent on unchanged input).

2. **Compose reasoning utilization.** Extract `smoothed_utilization`, `smoothed_kv_cache_occupancy`, and normalized `smoothed_queue_depth` from the GPU snapshot. Apply `ReasoningInputWeights` to produce a single utilization float.

2. **Update reasoning controller.** Pass composed utilization to the reasoning controller. Receive new `reasoning_price`.

3. **For each tool endpoint in the snapshot:**
   a. Look up or create the endpoint's tool price controller.
   b. Compute the latency ratio as `smoothed_latency_p50_ms / baseline_latency_ms`, clamped to `[0.0, 1.0]`. If the endpoint has no baseline (first observation), use `1.0`.
   c. Compose tool utilization from `smoothed_utilization`, latency ratio, and `1.0 - smoothed_rate_limit_headroom`, using `ToolInputWeights`.
   d. Update the endpoint's controller. Receive new `tool_price` for this endpoint.

4. **Assemble PriceUpdate.** Build the frozen `PriceUpdate` dataclass with both prices, utilization data, and timestamp.

5. **Record in price history.** Append to the bounded price history deque.

6. **Detect trend direction.** Compare the new price to a short trailing average (last 5 updates). Classify the trend as `"rising"` (new price > trailing average + threshold), `"falling"` (new price < trailing average - threshold), or `"stable"`.

7. **Publish.** Store as `latest_update`, invoke callbacks.

8. **Emit metrics.** Push pricing metrics to the observability system (spec 13).

### 6.3 Price History and Trend Detection

The pricing engine maintains a bounded deque of recent `PriceUpdate` snapshots. This history serves three purposes: the signal formatter (spec 05) uses it to describe price trends ("prices have been rising for the last 30 seconds"), the confirmation handler (spec 06) uses it to decide whether to advise agents to wait ("price is falling, you may want to defer"), and the evaluation harness (spec 14) uses it to analyze pricing dynamics post-experiment.

Trend detection uses a simple trailing-average comparison. The trend for each price channel is computed independently:

```python
def _compute_trend(self, current: Decimal, history: deque[Decimal], threshold: float) -> str:
    if len(history) < 3:
        return "stable"
    trailing = sum(history) / len(history)
    delta = float(current - trailing)
    if delta > threshold:
        return "rising"
    elif delta < -threshold:
        return "falling"
    return "stable"
```

The trailing window for trend detection is the last 5 price updates (not time-based). This means the trend reflects the last ~2.5 seconds at the default 0.5-second telemetry tick rate. A longer window would be less responsive; a shorter window would be noisier. The window length is configurable.

The trend direction is a qualitative signal — it tells agents whether to expect prices to continue in the current direction. It is deliberately coarse (three states) rather than a numeric rate-of-change, because even quantitative-tier agents benefit more from a clear directional signal than from a noisy derivative estimate.

### 6.4 Public Interface

```python
@property
def latest_update(self) -> PriceUpdate | None:
    """The most recently computed PriceUpdate, or None if not yet computed."""

@property
def reasoning_price(self) -> Decimal:
    """Current reasoning price. Returns floor if no update yet."""

def tool_price(self, endpoint_id: str) -> Decimal:
    """Current tool price for a specific endpoint.

    Returns the default floor if the endpoint has no controller yet.
    """

def price_ratio(self, endpoint_id: str) -> float:
    """Current tool_price / reasoning_price for an endpoint.

    Returns float('inf') if reasoning_price is zero (should never happen
    with a positive floor, but handled defensively).
    """

def all_tool_prices(self) -> dict[str, Decimal]:
    """Current tool prices for all known endpoints."""

def reasoning_trend(self) -> str:
    """Reasoning price trend: 'rising', 'falling', or 'stable'."""

def tool_trend(self, endpoint_id: str) -> str:
    """Tool price trend for a specific endpoint."""

def price_history(self, limit: int = 100) -> list[PriceUpdate]:
    """Recent price history, most recent first. Capped at `limit` entries."""

def price_summary(self) -> PriceSummary:
    """Aggregated pricing summary for the signal formatter."""
```

### 6.4 PriceUpdate and PriceSummary

```python
@dataclass(frozen=True)
class PriceUpdate:
    """Immutable snapshot of current prices, produced on each pricing tick."""
    reasoning_price: Decimal
    tool_price: dict[str, Decimal]       # endpoint_id → price
    gpu_utilization: float               # 0.0–1.0
    tool_utilization: dict[str, float]   # endpoint_id → 0.0–1.0
    timestamp: float

@dataclass(frozen=True)
class PriceSummary:
    """Aggregated summary for the signal formatter and confirmation handler."""
    reasoning_price: Decimal
    reasoning_trend: str                 # 'rising', 'falling', 'stable'
    tool_prices: dict[str, Decimal]
    tool_trends: dict[str, str]
    price_ratios: dict[str, float]       # endpoint_id → tool/reasoning ratio
    gpu_utilization: float
    tool_utilizations: dict[str, float]
    reasoning_controller_state: dict     # For debugging: setpoint, error, integral
    timestamp: float
```

### 6.5 Callback Registration

The pricing engine supports the same callback pattern as the telemetry collector:

```python
PriceCallback = Callable[[PriceUpdate], Awaitable[None]]

def on_price_update(self, callback: PriceCallback) -> None:
    """Register a callback invoked on every new PriceUpdate."""

def remove_callback(self, callback: PriceCallback) -> None:
    """Unregister a previously registered callback."""
```

### 6.6 Lifecycle

```python
async def start(self, telemetry_collector: TelemetryCollector) -> None:
    """Start the pricing engine.

    Registers a telemetry snapshot callback on the collector so
    that price updates are computed on every telemetry tick.
    Alternatively, if the pricing engine runs on its own tick,
    it reads collector.latest_snapshot on each tick.
    """

async def stop(self) -> None:
    """Stop the pricing engine and clean up."""
```

The pricing engine can operate in two modes:

**Callback-driven (default):** The engine registers a callback with the telemetry collector via `collector.on_snapshot(self._on_telemetry)`. When a new telemetry snapshot arrives, the engine's update method is invoked immediately. This minimizes latency between telemetry arrival and price publication.

**Tick-driven (alternative):** The engine runs its own `asyncio` loop at a configurable interval and reads `collector.latest_snapshot` on each tick. This decouples the pricing tick from the telemetry tick, allowing a different update frequency (e.g., price updates every 1 second while telemetry arrives every 0.5 seconds). Useful if price smoothness is more important than responsiveness.

The mode is selected via configuration. The default is callback-driven.

---

## 7. Stale Telemetry Handling

When the telemetry collector marks a source as stale or unhealthy, the pricing engine must adjust its behavior. Using prices derived from stale data is dangerous — they may not reflect current conditions.

### 7.1 Stale GPU Telemetry

If `snapshot.gpu.source_healthy` is `False`:

- The reasoning price controller freezes — it does not update, and `reasoning_price` holds its last known value.
- If staleness exceeds `max_stale_hold_seconds` (default: 10.0), the reasoning price is set to `ceiling × stale_price_fraction` (default: 0.8 × ceiling). This is a conservative penalty: if GPU telemetry is lost, assume the worst and make reasoning expensive. This discourages agents from pile-on inference that could overwhelm an already-struggling cluster.
- The `PriceSummary.reasoning_trend` is set to `"unknown"` while frozen.

### 7.2 Stale Tool Telemetry

If `snapshot.tools[endpoint_id].source_healthy` is `False` for a specific endpoint:

- That endpoint's tool price controller freezes.
- After `max_stale_hold_seconds`, the endpoint's price rises to its stale penalty level (same formula as GPU).
- Other endpoints with healthy telemetry continue updating normally. Stale telemetry on one endpoint does not affect pricing for other endpoints.

### 7.3 Recovery

When a stale source becomes healthy again, the controller resumes normal updates. The integral term is **not** reset on recovery (unlike telemetry EMA, which is reset). This is because the integral represents accumulated pricing pressure that may still be relevant — resetting it would cause a sudden price drop that could invite a burst of requests into a just-recovered resource. The proportional term naturally corrects as live utilization data flows in.

---

## 8. Shadow Price Interpretation

This section formalizes the economic interpretation of FAM's prices. It is included for theoretical grounding and to support the evaluation framework (spec 14, Experiment Type 3). Implementors can treat this section as informational context rather than a code specification.

### 8.1 The Centralized Problem

Consider \(N\) agents sharing a GPU cluster with capacity \(C_{\text{gpu}}\) (tokens/second) and \(M\) tool endpoints with capacities \(C_1, \ldots, C_M\) (calls/second). Each agent \(i\) has a utility function \(U_i(r_i, t_{i1}, \ldots, t_{iM})\) that depends on the inference tokens \(r_i\) and tool calls \(t_{ij}\) it consumes. The centralized social planner solves:

$$
\max \sum_{i=1}^{N} U_i(r_i, t_{i1}, \ldots, t_{iM})
$$

subject to:

$$
\sum_{i=1}^{N} r_i \leq C_{\text{gpu}}, \quad \sum_{i=1}^{N} t_{ij} \leq C_j \; \forall j
$$

The KKT conditions for this problem yield Lagrange multipliers \(\lambda_{\text{gpu}}\) and \(\lambda_j\) for each constraint — these are the shadow prices. At optimality, \(\lambda_{\text{gpu}}\) equals the marginal value of one additional unit of GPU capacity, and \(\lambda_j\) equals the marginal value of one additional unit of tool endpoint \(j\)'s capacity.

### 8.2 What FAM Prices Approximate

FAM's `reasoning_price` approximates \(\lambda_{\text{gpu}}\) and each `tool_price[j]` approximates \(\lambda_j\). The approximation is not exact because:

1. FAM does not know agent utility functions \(U_i\). It uses utilization as a proxy for aggregate demand.
2. FAM uses a feedback controller rather than solving the optimization problem. The controller iteratively adjusts prices based on observed excess demand (utilization above setpoint).
3. Agents are not perfectly rational optimizers. They respond to prices heuristically through LLM reasoning, not through exact marginal utility calculations.

Despite these gaps, the mechanism works in the same qualitative direction. When a resource is over-demanded relative to capacity, the shadow price is positive and FAM's price rises. When there is slack, the shadow price is zero and FAM's price approaches the floor. The PI controller's integral term plays the role of iterative price adjustment in tâtonnement — the classical economic process of finding equilibrium prices through iterative adjustment to excess demand.

### 8.3 Assumptions for Shadow Price Validity

FAM's prices are a reasonable shadow price approximation when:

- Agent utility functions are concave (diminishing returns to resource use).
- Agents respond monotonically to prices (higher price → less demand).
- Telemetry reflects actual utilization (not gamed or stale).
- The system is near steady state (prices do not track well during rapid transients).

The evaluation harness (spec 14, Experiment Type 3) computes the true shadow prices from a known problem instance (with known utility functions) and compares them to FAM's prices to validate the approximation quality.

---

## 9. Configuration

All configuration for the pricing engine is namespaced under `pricing` in the global FAM configuration (spec 12). The full schema:

```yaml
pricing:
  # Update mode: 'callback' (driven by telemetry) or 'tick' (independent loop)
  update_mode: callback

  # Tick interval if using tick mode (ignored in callback mode)
  tick_interval_seconds: 1.0

  # Reasoning price controller
  reasoning:
    controller_type: pi            # 'pi' or 'proportional'
    setpoint: 0.7                  # Target GPU utilization
    kp: 1.0                        # Proportional gain
    ki: 0.1                        # Integral gain (ignored if proportional)
    base_price: "0.5"              # Base price at setpoint (Decimal string)
    floor: "0.01"                  # Minimum price
    ceiling: "10.0"                # Maximum price
    integral_min: -5.0             # Integral windup lower clamp
    integral_max: 5.0              # Integral windup upper clamp
    trend_threshold: 0.005         # Min delta to register as rising/falling

    # Input composition weights (must sum to 1.0)
    input_weights:
      gpu_utilization: 0.6
      kv_cache_pressure: 0.25
      queue_depth_signal: 0.15

  # Tool price controller defaults (per-endpoint overrides possible)
  tools:
    controller_type: pi
    setpoint: 0.7
    kp: 1.2
    ki: 0.15
    base_price: "1.0"
    floor: "0.05"
    ceiling: "20.0"
    integral_min: -5.0
    integral_max: 5.0
    trend_threshold: 0.005

    # Input composition weights (must sum to 1.0)
    input_weights:
      tool_utilization: 0.55
      latency_ratio: 0.25
      headroom_pressure: 0.20

    # Baseline latency used to compute latency ratio (ms)
    default_baseline_latency_ms: 200.0

    # Per-endpoint overrides (optional)
    endpoints:
      # Example:
      # search_api:
      #   kp: 1.5
      #   ceiling: "30.0"
      #   baseline_latency_ms: 500.0

  # Stale telemetry handling
  stale:
    max_stale_hold_seconds: 10.0       # Hold last price for this long
    stale_price_fraction: 0.8          # Fraction of ceiling used as stale penalty

  # Price history
  history:
    max_entries: 600                   # Max price history entries retained
```

---

## 10. Error Handling

The pricing engine is designed to always produce a price. Even in degraded conditions, every call to `reasoning_price`, `tool_price()`, or `update()` returns a valid Decimal within `[floor, ceiling]`. There are no error return values and no exceptions propagated to callers.

**Telemetry snapshot is None:** If the engine is asked to update with a `None` snapshot (no telemetry available yet), it returns the current price unchanged. No controller update is performed.

**Controller arithmetic errors:** If the controller produces a `NaN` or `Inf` (which should not happen with clamped inputs, but is handled defensively), the price is set to the ceiling and an error is logged. The controller's integral term is reset to prevent the bad state from persisting.

**Missing endpoint in snapshot:** If a tool endpoint has a controller but is absent from the latest telemetry snapshot, the controller is not updated — the price holds. If a new endpoint appears in telemetry that has no controller, one is created with the default tool configuration.

**Decimal precision:** All price arithmetic uses `Decimal` with explicit quantization to 4 decimal places. This prevents floating-point drift from accumulating across thousands of ticks.

**Callback failures:** Same policy as the telemetry collector — caught, logged, not propagated. A callback that fails more than 10 consecutive times is de-registered.

---

## 11. Metrics Emitted

The pricing engine emits the following metrics to the observability system (spec 13) on each update:

**Gauges:** `pricing.reasoning_price` (current reasoning price), `pricing.tool_price` (per endpoint_id), `pricing.price_ratio` (per endpoint_id), `pricing.reasoning_utilization` (composed utilization input to reasoning controller), `pricing.tool_utilization` (per endpoint_id, composed input), `pricing.reasoning_error` (controller error signal), `pricing.reasoning_integral` (integral accumulator value), `pricing.tool_error` (per endpoint_id), `pricing.tool_integral` (per endpoint_id).

**Counters:** `pricing.updates.total` (total price update cycles), `pricing.stale_freezes` (per source, incremented each tick a controller is frozen due to stale telemetry), `pricing.controller_resets` (per channel, incremented on NaN/Inf recovery).

**Histograms:** `pricing.update_duration_ms` (time to compute all prices on one tick).

The controller-internal metrics (error, integral) are essential for debugging pricing behavior. If prices are too volatile, the proportional gain may be too high. If prices drift upward without returning, the integral may be winding up. These metrics make the controller's internal state visible.

---

## 12. Testing Strategy

### 12.1 Unit Tests

**PI controller step response:** Feed a step change in utilization (0.3 → 0.9) to a PI controller and verify that the price rises over subsequent ticks, converging toward the ceiling. Verify the integral term accumulates. Verify that when utilization drops back to 0.3, the price falls (the integral unwinds) and converges toward the floor. Plot the transient to visually confirm damped convergence (test as documentation).

**Proportional controller linearity:** For the proportional-only controller, verify that price is a linear function of utilization error. Verify clamping at floor and ceiling.

**Integral windup prevention:** Saturate the controller at utilization = 1.0 for many ticks. Verify the integral does not exceed `integral_max`. Then drop utilization to 0.0. Verify the price recovers within a bounded number of ticks (not stuck high due to a massive integral).

**Clamping:** Verify that no combination of utilization values and controller parameters can produce a price outside `[floor, ceiling]`.

**Composition function:** Verify that `compose_utilization` correctly weights signals. Verify clamping to [0.0, 1.0]. Verify that weights summing to != 1.0 raises a validation error at config load time.

**Price ratio:** Verify the ratio computation, including the edge case where `reasoning_price` is at the floor (should not divide by zero).

**Decimal precision:** Verify that prices are quantized to 4 decimal places. Run 10,000 update ticks and verify no precision drift.

### 12.2 Integration Tests

**Telemetry-to-price pipeline:** Connect a pricing engine to a telemetry collector with a mock GPU source. Drive the mock through a utilization ramp (0.0 → 1.0 over 60 seconds). Verify that the reasoning price rises proportionally. Drive it back down. Verify price falls.

**Multi-endpoint pricing:** Register three tool endpoints with different utilization levels. Verify that each has an independent price. Verify that changing one endpoint's utilization does not affect the others' prices.

**Stale telemetry handling:** Start with healthy telemetry. Make the GPU source go unhealthy. Verify the reasoning price freezes, then rises to the stale penalty after `max_stale_hold_seconds`. Restore the source. Verify the controller resumes from its integral state (not reset).

**Callback delivery:** Register a price callback. Drive a telemetry update. Verify the callback receives the correct `PriceUpdate` with matching prices and timestamp.

**Lifecycle:** Start engine, run several ticks, stop engine. Verify clean shutdown with no orphan tasks. Restart engine, verify it resumes from initial state (not from where it left off — controllers are reset on start).

### 12.3 Property-Based Tests

Use Hypothesis to generate random utilization sequences in `[0.0, 1.0]` and verify: (a) the price is always in `[floor, ceiling]`, (b) monotonicity — for a fixed sequence, a higher `kp` never produces a lower price for the same utilization, (c) the integral term is always in `[integral_min, integral_max]`, (d) `compose_utilization` is always in `[0.0, 1.0]`.

Generate random controller configurations and verify that no configuration causes a crash or NaN/Inf price.

---

## 13. Dependencies

**Internal:** `fam/types.py` (shared types including `PriceUpdate`), `fam/telemetry/snapshot.py` (telemetry snapshot types), `fam/config/schema.py` (configuration schema).

**External:** `decimal` (Python standard library). No third-party dependencies. The pricing engine uses only the Python standard library (`decimal`, `dataclasses`, `asyncio`, `collections.deque`, `enum`, `logging`, `time`, `math`).

**Interface contracts:** The pricing engine depends on `SystemTelemetrySnapshot` from spec 01 as its input. It produces `PriceUpdate` consumed by specs 03, 05, 06, and 08. The `PriceController` protocol enables swapping controller implementations via configuration without modifying the engine.

---

## 14. Open Questions

**Derivative term.** Should the controller be extended to PID (proportional-integral-derivative)? The derivative term would react to the *rate of change* of utilization, providing early damping for rapid changes. However, derivative terms amplify noise in the input signal, which is problematic even with EMA-smoothed telemetry. The initial implementation omits the derivative term; it can be added as a controller variant if PI proves insufficiently responsive to fast transients.

**Per-agent pricing.** The current design computes global prices — all agents face the same reasoning price and tool prices. Could per-agent pricing (e.g., higher prices for agents with higher remaining budgets) improve allocation efficiency? In theory, yes — it would implement price discrimination that captures more agent surplus. In practice, it adds complexity and raises fairness concerns. Deferred to future research.

**Non-linear price curves.** The PI controller produces approximately linear price changes for linear utilization changes (modulo clamping). An alternative is a non-linear mapping — e.g., prices that are nearly flat below the setpoint and rise exponentially above it (as sketched in spec 00's price-vs-utilization diagram). This could be implemented as a post-controller transformation. Worth investigating if the linear response proves too aggressive at low utilization or too gentle at high utilization.

**Coordinated pricing across endpoints.** Currently each tool endpoint has an independent price controller. If two endpoints serve the same logical function (e.g., two search APIs), their prices are independent even though an agent could substitute one for the other. Coordinated pricing (pricing the *function* rather than the *endpoint*) is an interesting optimization but requires knowledge of endpoint substitutability that FAM does not currently have. Deferred.

**Price announcement lag.** There is an inherent delay between a price change and agents reacting to it (the agent must receive the signal, process it, and act). During this lag, agents are making decisions based on stale prices. The PI integral naturally compensates for this (persistent excess demand accumulates), but understanding and quantifying this lag is important for tuning. The evaluation harness (spec 14) should measure price-to-reaction latency.
