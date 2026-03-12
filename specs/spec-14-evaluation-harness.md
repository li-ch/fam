# Spec 14 — Evaluation Harness

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview), Spec 10 (GPU Cluster Interface), Spec 11 (Tool Endpoint Interface), Spec 12 (Configuration & Tuning), Spec 13 (Metrics & Observability), Spec 15 (Capability Probe)
**Consumed By:** Research paper, external benchmarks, CI regression suite

---

## 1. Purpose

The evaluation harness is how FAM proves it works. It defines the experiment framework that runs reproducible benchmarks against FAM and its baselines, collects every metric defined in spec 13, and produces structured output that can be fed directly into paper tables and plots.

Without a rigorous evaluation framework, FAM is an interesting architecture document with no empirical grounding. The harness must answer five specific questions — each mapped to one experiment type: (1) does FAM maintain quality and throughput as agent count scales? (2) do agents actually respond to price signals by substituting reasoning for tool use? (3) do FAM's dynamic prices converge to the theoretically optimal shadow prices? (4) which individual modules contribute measurable value? (5) does performance differ across LLM capability tiers? The harness must answer these reproducibly, with controlled baselines, and with sufficient statistical rigor to support claims in a peer-reviewed paper.

This spec defines the benchmark task suite, the five experiment types and their protocols, the baseline implementations, the agent scaling protocol, the metric collection harness, the ablation configurations, the shadow price ground truth solver, and the structured output format.

---

## 2. Module Location

```
fam/
├── eval/
│   ├── __init__.py
│   ├── harness.py          # ExperimentHarness — main entry point
│   ├── runner.py            # ExperimentRunner — single experiment execution
│   ├── tasks.py             # Benchmark task definitions and registry
│   ├── baselines.py         # Baseline orchestration strategies
│   ├── scaling.py           # Agent scaling protocol and ramp controller
│   ├── ablations.py         # Ablation configuration presets
│   ├── solver.py            # Offline shadow price solver (ground truth)
│   ├── metrics_harness.py   # Metric collection wrapper around spec 13
│   └── output.py            # Structured results and comparison tables
```

Tests live in `tests/test_eval/`.

---

## 3. Benchmark Task Suite

The evaluation harness requires a suite of benchmark tasks that exercise interleaved reasoning and tool use. Tasks must be deterministic in their optimal solution path (so that completion accuracy is well-defined) while allowing multiple valid strategies (so that agent adaptation to pricing pressure can be observed).

### 3.1 Task Requirements

Every benchmark task must satisfy these properties:

**Multi-step.** At least three sequential reasoning-then-act steps. A single-tool-call task never exercises FAM's pricing feedback loop.

**Interleaved tool use.** Requires both reasoning and tool calls. Pure reasoning or pure tool-chaining tasks don't test the tradeoff pricing is designed to influence.

**Substitutable strategy.** At least one point where the agent can choose between a tool call and extended reasoning. This substitution margin is where price signals change behavior.

**Deterministic ground truth.** Correct answer is known and verifiable programmatically.

**Parameterizable difficulty.** A difficulty knob (e.g., number of hops, search space size) that scales independently of agent count.

### 3.2 Task Definitions

```python
@dataclass(frozen=True)
class BenchmarkTask:
    """A single benchmark task instance."""

    task_id: str
    task_type: str                  # One of the registered task types
    description: str                # Human-readable task description
    parameters: dict[str, Any]      # Task-type-specific parameters
    ground_truth: Any               # Expected correct answer
    required_tools: list[str]       # Tool endpoint IDs needed
    min_reasoning_steps: int        # Minimum reasoning steps in optimal path
    min_tool_calls: int             # Minimum tool calls in optimal path
    substitution_points: int        # Points where reasoning can replace a tool call
    difficulty: int                 # 1–5 scale, used for stratified analysis
    max_steps: int                  # Hard cutoff for agent execution steps
    timeout_seconds: float          # Wall-clock timeout for task completion
```

### 3.3 Task Types

The harness ships with four task types. New types can be registered via the task registry (section 3.4).

**Multi-Hop QA with Search (`multihop_qa`).** Chain multiple search queries to answer a question (e.g., "What is the population of the city where the director of Inception was born?" → search director → extract birthplace → search population). Substitution point: answer from parametric knowledge instead of issuing a later search. Parameters: `num_hops` (2–5), `domain` (geography, history, science), `distractor_count`.

**Code Generation with Execution (`code_exec`).** Write code and execute it to verify correctness. Substitution point: reason through test cases mentally instead of running them. Parameters: `problem_difficulty` (1–5), `num_test_cases` (3–10), `language` (python).

**Data Analysis with Database Queries (`data_analysis`).** Answer analytical questions by writing and executing SQL queries. Substitution point: after seeing schema, reason about the answer without running exploratory queries. Parameters: `num_tables` (2–8), `num_joins_required` (1–4), `aggregation_complexity` (simple vs. window functions).

**Tool-Augmented Reasoning (`tool_reasoning`).** Solve logical reasoning problems where some facts must be looked up and others can be deduced. Substitution point: infer a fact from context instead of looking it up. Parameters: `num_facts` (4–12), `lookup_fraction` (0.3–0.7), `chain_depth` (2–6).

### 3.4 Task Registry

Tasks are registered and discovered through a simple registry pattern:

```python
class TaskRegistry:
    _generators: ClassVar[dict[str, TaskGenerator]] = {}

    @classmethod
    def register(cls, task_type: str, generator: TaskGenerator) -> None:
        cls._generators[task_type] = generator

    @classmethod
    def generate(cls, task_type: str, count: int,
                 difficulty: int, seed: int) -> list[BenchmarkTask]:
        rng = random.Random(seed)
        return [cls._generators[task_type].create(difficulty=difficulty, rng=rng)
                for _ in range(count)]

class TaskGenerator(Protocol):
    def create(self, difficulty: int, rng: random.Random) -> BenchmarkTask: ...
```

Built-in generators for all four task types are auto-registered on import of `fam.eval.tasks`.

### 3.5 Mock Tool Endpoints for Tasks

Each task type defines the mock tool endpoints it requires. These mocks (built on spec 11's `MockToolEndpoint`) are pre-configured with deterministic responses keyed to the task parameters, configurable latency distributions, and configurable capacity limits. The evaluation harness instantiates one mock endpoint per required tool type and wires them into the FAM orchestrator before each experiment run.

```python
@dataclass(frozen=True)
class MockEndpointSpec:
    """Specification for a mock tool endpoint used in benchmarks."""

    endpoint_id: str
    latency_mean_ms: float          # Mean latency for normal responses
    latency_std_ms: float           # Std dev of latency (normal distribution)
    rate_limit_rpm: int             # Requests per minute limit
    max_concurrent: int             # Max concurrent requests
    error_rate: float               # Baseline error probability (0.0–1.0)
    capacity: int                   # Max outstanding requests before queueing
```

---

## 4. Experiment Types

The harness implements five experiment types. Each is a self-contained protocol that specifies what is varied, what is held constant, what is measured, and how results are compared.

### 4.1 Experiment Type 1 — Scaling Experiment

**Question:** How does FAM perform as concurrent agent count increases from 1 to N?

**Independent variable:** Number of concurrent agents (`agent_count`).

**Controlled variables:** Task suite (fixed), mock endpoint capacities (fixed), budget parameters (fixed per agent), pricing engine configuration (fixed).

**Protocol:**

```python
@dataclass(frozen=True)
class ScalingExperimentConfig:
    """Configuration for a scaling experiment."""

    agent_counts: list[int]         # e.g., [1, 2, 4, 8, 16, 32]
    task_type: str                  # Task type to use
    tasks_per_agent: int            # Number of tasks each agent executes
    task_difficulty: int            # Fixed difficulty for all tasks
    repetitions: int                # Runs per agent_count for statistical power
    seed_base: int                  # Base seed (incremented per repetition)
    warmup_tasks: int               # Tasks to discard from metrics (cold start)
    mock_gpu_capacity: int          # Simulated GPU cluster capacity
    mock_tool_specs: list[MockEndpointSpec]
```

For each `agent_count` in the list, the harness:

1. Instantiates a fresh FAM orchestrator with mock backends sized to `mock_gpu_capacity` and `mock_tool_specs`.
2. Creates `agent_count` LangGraph agent instances, each assigned `tasks_per_agent` tasks from the task suite.
3. Starts all agents concurrently via `asyncio.gather`.
4. Collects all metrics from spec 13 throughout the run.
5. Waits for all agents to complete or timeout.
6. Discards metrics from the first `warmup_tasks` per agent.
7. Records: task completion rate, task accuracy, mean completion time, per-agent budget utilization, aggregate GPU utilization over time, aggregate tool utilization over time, pricing engine price trajectories, confirmation rates, speculative token counts.
8. Repeats `repetitions` times with different seeds.

**Key metric:** Task completion rate × accuracy at each agent count, normalized to the single-agent baseline. If FAM's pricing mechanism works, this product should degrade gracefully (sub-linearly) rather than cliff-diving as contention rises.

### 4.2 Experiment Type 2 — Price Response Experiment

**Question:** Do agents adjust behavior in response to price changes — specifically, do they substitute reasoning for tool use when tool prices rise?

**Independent variable:** Exogenous tool price multiplier injected at known time points.

**Controlled variables:** Agent count (fixed, moderate — e.g., 8), task suite (fixed), GPU capacity (fixed at low contention).

**Protocol:**

```python
@dataclass(frozen=True)
class PriceResponseExperimentConfig:
    """Configuration for a price response experiment."""

    agent_count: int                # Fixed moderate count
    task_type: str
    tasks_per_agent: int
    task_difficulty: int
    price_multipliers: list[float]  # e.g., [1.0, 2.0, 5.0, 10.0]
    multiplier_hold_seconds: float  # How long each multiplier is held
    transition_ramp_seconds: float  # Ramp time between multipliers
    repetitions: int
    seed_base: int
```

The harness runs a single sustained experiment. At `multiplier_hold_seconds` intervals, it injects a tool price multiplier into the pricing engine (overriding the telemetry-derived price with `base_price × multiplier`). The multiplier is ramped over `transition_ramp_seconds` to avoid discontinuities.

**Key metrics:** (a) Tool call rate per agent in each price regime — should decrease as multiplier increases. (b) Reasoning tokens per task step in each price regime — should increase as agents substitute reasoning for tool calls. (c) Task accuracy in each price regime — measures whether substitution degrades quality. (d) Confirmation cancel rate — should increase at higher prices if agents are price-sensitive.

The harness computes the **price elasticity of tool demand**: \((\Delta q/q) / (\Delta p/p)\) where \(q\) is tool call rate and \(p\) is price. A negative elasticity indicates agents are price-responsive. The harness also tracks the corresponding change in reasoning tokens (substitution effect) and task accuracy (quality impact) between each pair of price regimes.

### 4.3 Experiment Type 3 — Shadow Price Convergence

**Question:** Do FAM's dynamic prices converge to the theoretically optimal shadow prices from a centralized solver?

**Independent variable:** Time (observation of convergence trajectory).

**Controlled variables:** Agent count (fixed), task suite (fixed), capacities (fixed and known to the solver).

**Protocol:**

```python
@dataclass(frozen=True)
class ShadowPriceExperimentConfig:
    """Configuration for shadow price convergence experiment."""

    agent_count: int
    task_type: str
    tasks_per_agent: int
    task_difficulty: int
    solver_time_horizon_seconds: float  # Solver optimizes over this window
    solver_granularity_seconds: float   # Solver time step
    price_sample_interval_seconds: float  # How often to sample FAM prices
    convergence_threshold: float         # Relative error threshold for "converged"
    repetitions: int
    seed_base: int
```

This experiment runs in two phases:

**Phase 1 — Online run:** Execute the full experiment with FAM's pricing engine active. Record the pricing engine's output prices at `price_sample_interval_seconds` intervals. Also record the complete telemetry trace (utilization, queue depths, agent decisions).

**Phase 2 — Offline solve:** Feed the recorded telemetry trace (agent utilities, resource capacities, demand patterns) into the offline shadow price solver (section 8). The solver computes the theoretically optimal prices for each time step given perfect information.

**Phase 3 — Comparison:** Compute the convergence trajectory — at each time step, the relative error between FAM's online price and the solver's optimal price. Plot the error over time and report the time-to-convergence (time until error stays below `convergence_threshold`).

**Key metrics:** (a) Mean relative price error over the run. (b) Time-to-convergence. (c) Correlation between FAM price trajectory and optimal price trajectory. (d) Social welfare comparison — total task utility achieved under FAM prices vs. theoretical maximum under optimal prices.

### 4.4 Experiment Type 4 — Ablation Study

**Question:** Which individual FAM modules contribute measurable value?

**Independent variable:** Module configuration (enabled/disabled).

**Controlled variables:** Agent count (fixed), task suite (fixed), infrastructure capacities (fixed).

**Protocol:**

```python
@dataclass(frozen=True)
class AblationExperimentConfig:
    """Configuration for an ablation study."""

    agent_count: int
    task_type: str
    tasks_per_agent: int
    task_difficulty: int
    ablation_configs: list[AblationConfig]  # Which modules to enable/disable
    repetitions: int
    seed_base: int


@dataclass(frozen=True)
class AblationConfig:
    """Defines which modules are enabled for a single ablation run."""

    name: str                        # Human-readable name (e.g., "no_confirmation")
    description: str
    pricing_engine_enabled: bool     # If False, prices are fixed at floor
    budget_manager_enabled: bool     # If False, budgets are infinite
    confirmation_enabled: bool       # If False, all tool calls auto-approve
    tiered_signals_enabled: bool     # If False, no pricing signals injected
    speculative_enabled: bool        # If False, no speculative continuation
    signal_tier_override: str | None # Force all agents to this tier, or None
```

The harness ships with these standard ablation configurations:

| Name | Pricing | Budget | Confirmation | Signals | Speculative | Purpose |
|---|---|---|---|---|---|---|
| `full_fam` | ✓ | ✓ | ✓ | ✓ | ✓ | All modules — the full system |
| `no_pricing` | ✗ | ✓ | ✓ | ✓ | ✓ | Value of dynamic pricing |
| `no_budget` | ✓ | ✗ | ✓ | ✓ | ✓ | Value of budget constraint |
| `no_confirmation` | ✓ | ✓ | ✗ | ✓ | ✓ | Value of asking before dispatch |
| `no_signals` | ✓ | ✓ | ✓ | ✗ | ✓ | Value of price information |
| `no_speculative` | ✓ | ✓ | ✓ | ✓ | ✗ | Value of speculative continuation |
| `pricing_only` | ✓ | ✗ | ✗ | ✓ | ✗ | Minimal FAM — pricing alone |

"✗" for pricing means prices fixed at floor. "✗" for budget means infinite balance. "✗" for confirmation means all calls auto-approved. "✗" for signals means no pricing messages injected. "✗" for speculative means deferred calls block the agent.

For each ablation configuration, the harness runs the full task suite and collects all metrics. The output is a comparison table with one row per ablation and columns for each key metric.

**Key metrics:** Task completion rate, task accuracy, mean completion time, total tool calls, total reasoning tokens, aggregate resource utilization, budget utilization efficiency.

### 4.5 Experiment Type 5 — Capability Tier Comparison

**Question:** Does FAM's performance differ when agents use different capability tiers (quantitative, qualitative, directional)?

**Independent variable:** Capability tier assignment.

**Controlled variables:** Agent count (fixed), task suite (fixed), infrastructure (fixed), all modules enabled.

**Protocol:**

```python
@dataclass(frozen=True)
class TierComparisonExperimentConfig:
    """Configuration for a capability tier comparison experiment."""

    agent_count: int
    task_type: str
    tasks_per_agent: int
    task_difficulty: int
    tiers_to_test: list[str]        # e.g., ["quantitative", "qualitative", "directional"]
    models_per_tier: list[str]      # Model identifiers to test per tier
    repetitions: int
    seed_base: int
```

For each tier in `tiers_to_test`, the harness:

1. Forces all agents to that tier via `signal_tier_override` (bypassing the probe).
2. Runs the full task suite.
3. Collects metrics with special attention to price-responsive behavior per tier.

In a separate sub-experiment, the harness runs with mixed tiers — agents are assigned their natural tier via the capability probe (spec 15) — and measures whether the system still functions when agents of different capabilities coexist.

**Key metrics:** Per-tier: tool call rate, reasoning token rate, confirmation acceptance rate, task accuracy, task completion time. Cross-tier: variance in budget utilization, fairness of resource access (Gini coefficient of tool calls across agents).

---

## 5. Baseline Implementations

To demonstrate that FAM improves over alternative coordination strategies, the harness includes four baseline orchestrators. Each baseline replaces FAM's orchestration layer with a simpler mechanism while keeping the same agent graphs, task suite, and mock infrastructure.

### 5.1 Baseline Interface

All baselines implement a common interface so the harness can swap them transparently:

```python
class BaselineOrchestrator(Protocol):
    """Interface for baseline orchestration strategies."""

    async def setup(
        self,
        agent_count: int,
        tool_endpoints: dict[str, MockEndpointSpec],
        gpu_capacity: int,
        config: dict[str, Any],
    ) -> None:
        """Initialize the baseline with the given infrastructure."""
        ...

    async def intercept_tool_call(
        self,
        agent_id: str,
        tool_call: ToolCallRequest,
    ) -> ToolCallDecision:
        """Decide what to do with an agent's tool call."""
        ...

    async def get_agent_context(self, agent_id: str) -> str | None:
        """Return any context string to inject into agent state, or None."""
        ...

    def get_metrics(self) -> dict[str, Any]:
        """Return baseline-specific metrics for the current run."""
        ...


class ToolCallDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    QUEUE = "queue"
```

### 5.2 Uncoordinated Dispatch

The null baseline. Every tool call is approved immediately with no interception, no pricing, no budgets, and no signals. Agents execute as fast as the infrastructure allows. This answers: "What happens without any orchestration at all?"

```python
class UncoordinatedBaseline:
    async def intercept_tool_call(self, agent_id, tool_call) -> ToolCallDecision:
        return ToolCallDecision.APPROVE

    async def get_agent_context(self, agent_id) -> str | None:
        return None
```

### 5.3 System-Prompt Nudging

The agent receives a static system prompt instructing it to be conservative with tool use. No actual load information is provided — the nudge is unconditional and identical regardless of system state. This tests whether vague instructions alone can substitute for real-time pricing.

The nudge text: *"The system is currently experiencing high load. Before making tool calls, consider whether you can answer from your existing knowledge. Only use tools when strictly necessary."* All tool calls are auto-approved — the nudge is advisory only.

### 5.4 Hard Rate Limiting

Each agent is allowed a fixed number of tool calls per time window (`calls_per_window` per `window_seconds`). Once the limit is reached, tool calls are rejected until the window resets. The agent is told how many calls remain. No pricing information is provided. This tests whether blunt quotas can substitute for market-based coordination.

```python
class RateLimitBaseline:
    def __init__(self, calls_per_window: int, window_seconds: float) -> None:
        self._calls_per_window = calls_per_window
        self._window_seconds = window_seconds
        self._agent_windows: dict[str, _AgentWindow] = {}

    async def intercept_tool_call(self, agent_id, tool_call) -> ToolCallDecision:
        window = self._get_or_create_window(agent_id)
        window.evict_expired()
        if window.count >= self._calls_per_window:
            return ToolCallDecision.REJECT
        window.record()
        return ToolCallDecision.APPROVE
```

### 5.5 Tool-Only Pricing

Prices are computed and communicated to agents, but only for tool calls — there is no reasoning price, no budget system, and no confirmation flow. The agent sees the current tool price in its context and decides independently whether to call. All calls are auto-approved regardless of price. This tests whether pricing without the full budget-and-confirmation mechanism is sufficient.

```python
class ToolOnlyPricingBaseline:
    def __init__(self, pricing_engine: PricingEngine) -> None:
        self._pricing = pricing_engine

    async def intercept_tool_call(self, agent_id, tool_call) -> ToolCallDecision:
        return ToolCallDecision.APPROVE

    async def get_agent_context(self, agent_id) -> str | None:
        price = self._pricing.current_prices().tool_price
        return f"Current tool call price: {price:.2f} units. Higher = heavier load."
```

---

## 6. Agent Scaling Protocol

The scaling protocol defines how the harness ramps from 1 to N concurrent agents in a controlled manner. This is not simply "start N agents at once" — the protocol must ensure that measurements at each scale point are stable and comparable.

### 6.1 Ramp Strategy

```python
@dataclass(frozen=True)
class ScalingRampConfig:
    """Controls how agents are ramped up during scaling experiments."""

    ramp_mode: str                   # "step" or "gradual"
    stabilization_seconds: float     # Wait time after reaching target count
    agent_start_stagger_ms: float    # Delay between starting successive agents
    drain_timeout_seconds: float     # Max time to wait for agents to finish
```

**Step mode (`ramp_mode="step"`):** For each agent count in the scaling sequence, the harness tears down the previous run, creates a fresh orchestrator, starts exactly `agent_count` agents simultaneously (with `agent_start_stagger_ms` between each start to avoid a thundering herd), waits `stabilization_seconds` for the system to reach steady state, then begins recording metrics. This mode gives the cleanest per-scale-point measurements but requires a full restart between scale points.

**Gradual mode (`ramp_mode="gradual"`):** The harness starts with 1 agent and progressively adds agents until reaching the maximum count, without restarting the orchestrator. New agents are added at `agent_start_stagger_ms` intervals. After each addition, the system stabilizes for `stabilization_seconds` before recording metrics for the current agent count. This mode captures the system's adaptation behavior during scaling but produces noisier per-point measurements because the system state is not reset.

### 6.2 Agent Instantiation

Each agent is instantiated with:

- A unique `agent_id` following the pattern `eval_agent_{run_id}_{index}`.
- A task queue drawn from the benchmark task suite (deterministic given the seed).
- A LangGraph graph built from a standard agent template that implements the tool-calling loop with FAM integration.
- An assigned capability tier (either forced by experiment config or determined by the probe).

```python
class EvalAgent:
    def __init__(self, agent_id: str, task_queue: list[BenchmarkTask],
                 graph: StateGraph, tier: str) -> None:
        self.agent_id = agent_id
        self.task_queue = task_queue
        self.graph = graph
        self.tier = tier
        self.results: list[TaskResult] = []

    async def run(self, orchestrator: Any) -> list[TaskResult]:
        for task in self.task_queue:
            self.results.append(await self._execute_task(task, orchestrator))
        return self.results
```

### 6.3 Agent Graph Template

The harness provides `build_eval_agent_graph(agent_id, tools, model_endpoint, tier) -> StateGraph` that constructs a standard ReAct-style graph (reason → act → observe → repeat) with FAM's tool interception nodes injected at the `act` boundary. The agent's LLM node is backed by a configurable model endpoint (real or mock).

---

## 7. Metric Collection Harness

The metric collection harness wraps spec 13's metrics system to provide experiment-aware metric recording. It adds experiment metadata (experiment type, run ID, agent count, baseline name, ablation config) to every metric event, partitions metrics by experiment phase, and supports start/stop semantics for clean measurement windows.

### 7.1 MetricSession

```python
class MetricSession:
    def __init__(self, run_id: str, experiment_type: str,
                 metadata: dict[str, Any], metrics_collector: MetricsCollector) -> None:
        self.run_id = run_id
        self.experiment_type = experiment_type
        self.metadata = metadata
        self._collector = metrics_collector
        self._start_time: float | None = None
        self._end_time: float | None = None
        self._phase: str = "warmup"

    def start(self) -> None: ...       # Sets phase to "active", records start time
    def stop(self) -> None: ...        # Sets phase to "complete", records end time
    def set_phase(self, phase: str) -> None: ...  # "warmup", "active", "cooldown"
    def snapshot(self) -> MetricSessionSnapshot: ...
```

### 7.2 Recorded Metrics

The harness records every metric defined in spec 13, plus experiment-specific derived metrics:

**Per-agent metrics:** budget balance over time, reasoning token count, tool call count, tool call confirmation rate (approved / total), tool call cancel rate, speculative token count, task completion count, task accuracy, mean task completion time, budget utilization (spend / (spend + remaining)).

**Aggregate metrics:** GPU utilization over time, per-endpoint tool utilization over time, reasoning price trajectory, tool price trajectory, price ratio trajectory, total task throughput (tasks completed / wall time), aggregate accuracy, system-wide confirmation rate, system-wide cancel rate.

**Derived experiment metrics:** price elasticity (type 2), convergence error (type 3), ablation delta (type 4), tier fairness coefficient (type 5).

### 7.3 Metric Sampling

For time-series metrics (prices, utilization, budget balances), the harness samples at a configurable interval (default: 0.5 seconds). Each sample is timestamped relative to the session start. The sampling runs as an `asyncio` task that reads from the orchestrator's live state.

```python
@dataclass(frozen=True)
class TimeSeriesSample:
    """A single time-series observation."""

    relative_time_seconds: float
    metric_name: str
    value: float
    labels: dict[str, str]          # e.g., {"agent_id": "eval_agent_0_3"}
```

---

## 8. Shadow Price Ground Truth Solver

The shadow price solver computes the theoretically optimal resource prices for Experiment Type 3. Given complete knowledge of agent utilities, resource capacities, and demand patterns, it solves the centralized resource allocation problem and extracts the dual variables (shadow prices) from the optimal solution.

### 8.1 Problem Formulation

The centralized problem is a social welfare maximization:

Maximize the sum of agent utilities \(\sum_i U_i(r_i, t_i)\) subject to:
- GPU capacity constraint: \(\sum_i r_i \leq R_{\text{gpu}}\) (total reasoning tokens per time step ≤ cluster capacity)
- Tool capacity constraint per endpoint \(j\): \(\sum_i t_{ij} \leq T_j\) (total tool calls per time step ≤ endpoint capacity)
- Non-negativity: \(r_i \geq 0\), \(t_{ij} \geq 0\) for all \(i, j\)

where \(r_i\) is the reasoning tokens allocated to agent \(i\) and \(t_{ij}\) is the tool calls to endpoint \(j\) allocated to agent \(i\).

The dual variables of the capacity constraints at the optimal solution are the shadow prices — the marginal value of one additional unit of each resource. Under convexity, these are the prices that, if announced to agents, would cause individually rational agents to choose exactly the socially optimal allocation.

### 8.2 Utility Estimation

Agent utilities are not directly observable — they must be inferred from the experiment trace. The solver uses a revealed-preference approach: from the recorded sequence of agent decisions (tool calls made, reasoning steps taken, tasks completed), it fits a parametric utility function per agent.

```python
@dataclass
class AgentUtilityModel:
    """Parametric utility model for one agent, fitted from trace data."""

    agent_id: str
    reasoning_productivity: float    # Task progress per reasoning token
    tool_productivity: dict[str, float]  # Task progress per tool call, per endpoint
    completion_value: float          # Utility gained from completing a task
    time_discount: float             # Discount factor for delayed completion

    def utility(
        self,
        reasoning_tokens: float,
        tool_calls: dict[str, float],
        tasks_completed: float,
        elapsed_time: float,
    ) -> float:
        """Compute utility for given resource consumption and outcomes."""
        progress = (
            self.reasoning_productivity * reasoning_tokens
            + sum(
                self.tool_productivity.get(ep, 0.0) * calls
                for ep, calls in tool_calls.items()
            )
        )
        return (
            self.completion_value * tasks_completed
            + progress
            - self.time_discount * elapsed_time
        )
```

### 8.3 Solver Implementation

The solver discretizes time into `solver_granularity_seconds` steps, formulates the linear program for each time step using the estimated utility models and known capacities, and solves using `scipy.optimize.linprog`. The dual variables from each time step give the shadow prices.

```python
class ShadowPriceSolver:
    def __init__(self, config: ShadowPriceExperimentConfig) -> None:
        self._config = config

    def solve(
        self,
        trace: ExperimentTrace,
        gpu_capacity: float,
        tool_capacities: dict[str, float],
    ) -> ShadowPriceSolution:
        """Compute optimal shadow prices from an experiment trace."""
        utility_models = self._fit_utility_models(trace)
        time_steps = self._discretize(trace)
        prices = [
            self._solve_step(utility_models, self._extract_demand(trace, s),
                             gpu_capacity, tool_capacities)
            for s in time_steps
        ]
        return ShadowPriceSolution(
            time_steps=[s.timestamp for s in time_steps],
            optimal_reasoning_prices=[p.reasoning_price for p in prices],
            optimal_tool_prices=[p.tool_prices for p in prices],
            optimal_welfare=sum(p.welfare for p in prices),
        )

@dataclass(frozen=True)
class ShadowPriceSolution:
    time_steps: list[float]
    optimal_reasoning_prices: list[float]
    optimal_tool_prices: list[dict[str, float]]
    optimal_welfare: float
```

### 8.4 Convergence Metric

The convergence metric between FAM's online prices and the solver's optimal prices is the normalized root mean square error (NRMSE):

$$
\text{NRMSE}(t) = \frac{1}{t} \sum_{\tau=1}^{t} \sqrt{ \left(\frac{p_{\text{fam}}(\tau) - p^*(\tau)}{p^*(\tau)}\right)^2 }
$$

The harness reports this as a time series and as a scalar (the final value at the end of the run). It also reports time-to-convergence: the first time step after which NRMSE stays below `convergence_threshold` for the remainder of the run.

---

## 9. ExperimentRunner

The `ExperimentRunner` is the core execution engine. It takes an experiment configuration, instantiates the appropriate orchestrator (FAM or baseline), creates agents, runs the experiment protocol, and returns structured results.

### 9.1 Interface

```python
class ExperimentRunner:
    def __init__(self, config: ExperimentConfig) -> None: ...
    async def run(self) -> ExperimentResult: ...

@dataclass(frozen=True)
class ExperimentResult:
    run_id: str
    experiment_type: str
    config: dict[str, Any]
    started_at: str                  # ISO 8601
    completed_at: str
    duration_seconds: float
    agent_results: list[AgentResult]
    metric_session: MetricSessionSnapshot
    time_series: list[TimeSeriesSample]
    summary: dict[str, float]

@dataclass(frozen=True)
class AgentResult:
    agent_id: str
    tier: str
    tasks_attempted: int
    tasks_completed: int
    tasks_correct: int
    total_reasoning_tokens: int
    total_tool_calls: int
    total_tool_cancels: int
    total_speculative_tokens: int
    mean_task_time_seconds: float
    final_budget_balance: float
    budget_utilization: float
```

### 9.2 Lifecycle

1. **Setup:** Create mock GPU cluster and tool endpoints from config. Instantiate FAM orchestrator (or baseline). Configure ablation overrides if applicable. Instantiate MetricSession.
2. **Agent creation:** Generate tasks from the registry. Build agent graphs. Create `EvalAgent` instances.
3. **Warmup:** Run `warmup_tasks` per agent with MetricSession in "warmup" phase (metrics recorded but excluded from analysis).
4. **Active phase:** Switch MetricSession to "active." Run all remaining tasks. Sample time-series metrics at the configured interval.
5. **Drain:** Wait for all agents to finish or hit `drain_timeout_seconds`. Record any agent that timed out.
6. **Collection:** Stop MetricSession. Aggregate per-agent results. Compute derived metrics. Build `ExperimentResult`.
7. **Teardown:** Stop orchestrator, cancel async tasks, release resources.

---

## 10. Output Format

The harness produces structured output in two formats: machine-readable JSON for programmatic analysis and auto-generated Markdown tables for direct inclusion in papers.

### 10.1 JSON Results File

Each experiment run produces a single JSON file:

```python
@dataclass
class ExperimentOutput:
    version: str                     # Schema version, e.g., "1.0"
    experiment_type: str
    run_id: str
    timestamp: str                   # ISO 8601
    config: dict[str, Any]
    results: list[ExperimentResult]  # One per repetition
    comparison: ComparisonTable | None
    metadata: dict[str, Any]         # Runtime info (Python version, host, etc.)
```

Output files are written to `eval_results/` with naming convention `{experiment_type}_{run_id}_{timestamp}.json`.

### 10.2 Comparison Tables

For experiments that compare multiple conditions, the harness auto-generates comparison tables with one row per condition (scaling point, baseline, ablation config, or tier) and one column per key metric. Each cell contains `mean ± stddev` across repetitions. The primary condition (FAM with all modules) is highlighted.

```python
@dataclass
class ComparisonTable:
    title: str
    columns: list[str]
    rows: list[ComparisonRow]
    notes: list[str]

@dataclass
class ComparisonRow:
    condition: str                    # e.g., "8 agents", "no_pricing"
    values: dict[str, str]           # Metric name → "0.92 ± 0.03"
    highlight: bool                  # True if this is the FAM condition
```

### 10.3 Markdown Rendering

The `output.py` module renders `ComparisonTable` into Markdown tables suitable for direct inclusion in papers. The primary condition row is bolded. Example:

```
| Agents | Completion | Accuracy   | Time (s)    | Tool Calls | Reasoning Tokens |
|--------|-----------|------------|-------------|------------|-----------------|
| 1      | 1.00±0.00 | 0.95±0.02  | 12.3±1.1    | 4.2±0.5    | 1820±210        |
| **8**  |**0.95±0.02**|**0.91±0.04**|**18.7±2.3**|**3.5±0.7** |**2450±350**     |
| 16     | 0.89±0.04 | 0.87±0.05  | 25.2±4.1    | 2.8±0.9    | 3100±520        |
```

---

## 11. Configuration

All evaluation harness configuration is namespaced under `eval` in the global FAM configuration (spec 12).

```yaml
eval:
  results_directory: "eval_results/"
  default_seed: 42

  tasks:
    default_tasks_per_agent: 10
    default_difficulty: 3
    default_timeout_seconds: 120.0
    default_max_steps: 50
    warmup_tasks: 2

  mock_infrastructure:
    gpu_capacity: 128
    default_tool_endpoint:
      latency_mean_ms: 200.0
      latency_std_ms: 50.0
      rate_limit_rpm: 60
      max_concurrent: 10
      error_rate: 0.01
      capacity: 20

  scaling:
    agent_counts: [1, 2, 4, 8, 16, 32]
    repetitions: 5
    ramp_mode: "step"
    stabilization_seconds: 5.0
    agent_start_stagger_ms: 100.0
    drain_timeout_seconds: 300.0

  price_response:
    agent_count: 8
    price_multipliers: [1.0, 2.0, 5.0, 10.0]
    multiplier_hold_seconds: 60.0
    transition_ramp_seconds: 5.0
    repetitions: 5

  shadow_price:
    agent_count: 8
    solver_time_horizon_seconds: 300.0
    solver_granularity_seconds: 1.0
    price_sample_interval_seconds: 0.5
    convergence_threshold: 0.1
    repetitions: 3

  ablation:
    agent_count: 8
    repetitions: 5

  tier_comparison:
    agent_count: 8
    tiers_to_test: ["quantitative", "qualitative", "directional"]
    repetitions: 5

  metrics:
    sample_interval_seconds: 0.5
```

---

## 12. Error Handling

The evaluation harness is designed to be robust against individual run failures without losing the entire experiment batch.

**Agent timeout:** If an agent does not complete within `timeout_seconds`, it is cancelled. The tasks it completed are recorded; remaining tasks are marked as timed out. The run continues with the remaining agents.

**Agent crash:** If an agent raises an unhandled exception, it is caught and logged. The agent's results up to the crash are preserved. The crash is recorded in the `AgentResult` with a `crash_error` field.

**Orchestrator failure:** If the FAM orchestrator itself fails during a run, the run is aborted and marked as failed. The harness proceeds to the next run in the batch (next repetition or next condition).

**Solver failure:** If the shadow price solver fails (infeasible LP, numerical instability), the solver result is marked as failed and the convergence comparison is omitted. The online FAM results are still valid and recorded.

**Partial results:** The harness writes results incrementally — after each completed run, the partial results file is updated. If the harness process is killed, all completed runs are recoverable from the output file.

**Determinism:** All randomness in task generation, agent ordering, and mock endpoint behavior is seeded. Given the same seed, configuration, and runtime environment, the harness produces identical results. Non-determinism from LLM sampling is controlled by setting temperature to 0 and using fixed seeds where the model API supports it.

---

## 13. Metrics Emitted

The evaluation harness emits the following metrics to the observability system (spec 13):

**Counters:** `eval.runs.started` (per experiment_type), `eval.runs.completed`, `eval.runs.failed`, `eval.tasks.attempted` (per task_type), `eval.tasks.completed`, `eval.tasks.correct`, `eval.tasks.timed_out`, `eval.agents.started`, `eval.agents.completed`, `eval.agents.crashed`.

**Gauges:** `eval.active_agents` (current count of running agents), `eval.active_runs` (current count of in-progress experiment runs).

**Histograms:** `eval.task.duration_seconds` (per task_type), `eval.run.duration_seconds` (per experiment_type), `eval.solver.duration_seconds`.

---

## 14. Testing Strategy

### 14.1 Unit Tests

**Task generation:** Verify that each task generator produces valid `BenchmarkTask` instances. Verify determinism — same seed produces same tasks. Verify difficulty parameterization changes task properties appropriately. Verify that generated tasks have correct ground truth answers.

**Baseline implementations:** Verify that `UncoordinatedBaseline` always approves. Verify that `RateLimitBaseline` correctly rejects after quota exhaustion and resets after the window. Verify that `NudgingBaseline` injects the prompt. Verify that `ToolOnlyPricingBaseline` passes through prices.

**Shadow price solver:** Verify against hand-computed cases. For a 2-agent, 1-resource problem with known linear utilities, verify the solver produces the analytically correct shadow price. Verify that infeasible problems are handled gracefully.

**Output formatting:** Verify that `ComparisonTable` renders to valid Markdown. Verify that JSON output is valid and matches the schema. Verify that `±` values are computed correctly from repetition data.

**Metric session:** Verify that warmup-phase metrics are excluded from the active snapshot. Verify that phase transitions are recorded with correct timestamps.

### 14.2 Integration Tests

**End-to-end single run:** Execute a minimal scaling experiment (2 agent counts, 1 task per agent, 1 repetition) with mock infrastructure. Verify that the result file is produced, contains valid data, and the metrics are internally consistent (e.g., `tasks_completed ≤ tasks_attempted`).

**Baseline comparison run:** Execute the same task suite under FAM and the uncoordinated baseline. Verify that both produce results with the same schema. Verify that the comparison table is generated.

**Ablation run:** Execute a minimal ablation experiment with 2 configs (full FAM and no_pricing). Verify both configs execute and produce distinct metric values.

**Crash recovery:** Start an experiment, kill one agent mid-run, verify the harness completes with the remaining agents and records the crashed agent's partial results.

### 14.3 Property-Based Tests

Use Hypothesis to verify: generated task seeds always produce valid tasks (no exceptions, all fields populated). AgentResult aggregation is consistent (sum of per-task metrics equals agent totals). ComparisonTable rendering never produces malformed Markdown regardless of input values. Price elasticity computation handles edge cases (zero price change, zero quantity change).

---

## 15. Dependencies

**Internal:** `fam/types.py` (shared types), `fam/config/schema.py` (configuration), `fam/metrics/collector.py` (spec 13, metric collection), `fam/interfaces/gpu_mock.py` (spec 10, mock GPU cluster), `fam/interfaces/tool_mock.py` (spec 11, mock tool endpoints), `fam/eval/probe.py` (spec 15, capability tier assignment), `fam/core.py` (Orchestrator for FAM runs), `fam/pricing/engine.py` (spec 02, for tool-only pricing baseline), `fam/adapters/langgraph/` (spec 09, for building agent graphs).

**External:** `scipy` (for `scipy.optimize.linprog` in the shadow price solver), `numpy` (for statistical computations — mean, std, percentiles), `hypothesis` (for property-based testing, test-only dependency). No other third-party dependencies. The harness does not import LLM provider SDKs directly — all LLM interaction goes through the LangGraph adapter.

---

## 16. Open Questions

**Utility function form.** The shadow price solver assumes agent utilities can be estimated from trace data using a parametric model. The current model is linear in resource consumption, which makes the LP tractable but may not capture the diminishing returns that real agents exhibit (e.g., the 10th search query is less valuable than the 1st). A concave utility model would require convex programming instead of LP. The initial implementation uses the linear model; the concave extension is deferred.

**LLM non-determinism.** Even with temperature=0, some LLM APIs exhibit non-determinism (different outputs for identical inputs across calls). This introduces noise into repetition-based statistics. The harness does not currently control for this beyond seeding. A potential mitigation is to run more repetitions and report confidence intervals, or to use a mock LLM with deterministic outputs for mechanism-testing experiments (types 3 and 4) and reserve real LLMs for agent-behavior experiments (types 1, 2, and 5).

**Task suite coverage.** The four built-in task types may not cover all patterns of interest. Tasks with hard real-time constraints (e.g., "answer within 5 seconds") are not represented. Adding a latency-sensitive task type would test FAM's behavior when task utility has a time component.

**Multi-model experiments.** The current framework assumes all agents use the same model. A more realistic evaluation would mix models (e.g., GPT-4-class alongside smaller models) to test whether tier-adaptive signals maintain fairness across heterogeneous populations.
