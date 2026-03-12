# Spec 12 — Configuration and Tuning

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview)
**Consumed By:** All other specs (01–15)

---

## 1. Purpose

Every behavioral parameter in FAM — pricing curve gains, budget replenishment rates, confirmation thresholds, smoothing alphas, prompt templates, queue weights — lives in configuration, not in code. This spec defines the complete configuration surface: every parameter, its type, its default value, its valid range, which module consumes it, and whether it can be changed at runtime.

The configuration system serves three audiences. Developers use it during development and testing to quickly iterate on parameter values without code changes. Researchers use it to define experiment configurations for the evaluation harness (spec 14), where each experiment may use a different parameter set. Operators (future) would use it to tune a production deployment to match their workload characteristics.

The configuration file format is YAML. A single file `fam/config/defaults.yaml` ships with sensible defaults for a "getting started" deployment — a single-process, mock-backend development environment with moderate agent concurrency. Users override defaults by providing their own YAML file that is merged on top.

This spec also defines configuration validation (type checking, range checking, cross-parameter constraints), the hot-reload mechanism for runtime parameter changes, and the configuration schema as a Python dataclass tree.

---

## 2. Module Location

```
fam/
├── config/
│   ├── __init__.py
│   ├── schema.py          # Pydantic/dataclass config schema
│   ├── loader.py          # YAML loading, merging, validation
│   ├── defaults.yaml      # Default configuration file
│   └── hot_reload.py      # Runtime reload watcher
```

Tests live in `tests/test_config/`.

---

## 3. Configuration File Format

Configuration files use YAML for readability and comment support. The top-level structure mirrors the module organization: each module has its own namespace.

```yaml
# fam-config.yaml — FAM orchestrator configuration
# Lines starting with # are comments.

telemetry:
  # ... (spec 01 parameters)

pricing:
  # ... (spec 02 parameters)

budget:
  # ... (spec 03 parameters)

signals:
  # ... (specs 04, 05 parameters)

confirmation:
  # ... (spec 06 parameters)

dispatch:
  # ... (spec 07 parameters)

speculative:
  # ... (spec 08 parameters)

adapter:
  # ... (spec 09 parameters)

interfaces:
  # ... (specs 10, 11 parameters)

metrics:
  # ... (spec 13 parameters)

eval:
  # ... (spec 14 parameters)
```

### 3.1 File Loading and Merging

FAM loads configuration in the following order, with later sources overriding earlier ones:

1. **Built-in defaults** (`fam/config/defaults.yaml`) — always loaded.
2. **User config file** — path specified via `FAM_CONFIG` environment variable or `--config` CLI argument.
3. **Environment variable overrides** — environment variables of the form `FAM__SECTION__KEY=value` override individual parameters (double underscore as separator).

```python
from pathlib import Path


class ConfigLoader:
    """Loads, merges, and validates FAM configuration."""

    def __init__(
        self,
        defaults_path: Path | None = None,
        user_config_path: Path | None = None,
    ) -> None:
        self._defaults_path = defaults_path or Path(__file__).parent / "defaults.yaml"
        self._user_path = user_config_path

    def load(self) -> "OrchestratorConfig":
        """Load and merge configuration from all sources.

        1. Load defaults.yaml.
        2. If user config path exists, deep-merge user values over defaults.
        3. Apply environment variable overrides.
        4. Validate the merged config.
        5. Return a frozen OrchestratorConfig instance.
        """

    def _deep_merge(self, base: dict, override: dict) -> dict:
        """Recursively merge override dict into base dict.

        Scalar values in override replace base values.
        Dict values are merged recursively.
        List values in override replace base values entirely (no list merging).
        """

    def _apply_env_overrides(self, config: dict) -> dict:
        """Apply FAM__SECTION__KEY environment variable overrides."""

    def _validate(self, config: dict) -> "OrchestratorConfig":
        """Validate types, ranges, and cross-parameter constraints.

        Raises ConfigValidationError with a list of all violations
        (not just the first one found).
        """
```

### 3.2 Environment Variable Override Syntax

Environment variables use double-underscore separators to represent nesting:

```
FAM__PRICING__REASONING__KP=1.5
FAM__BUDGET__INITIAL_BALANCE=200.0
FAM__TELEMETRY__GPU__POLL_INTERVAL_SECONDS=0.25
```

Values are parsed as YAML scalars (so `true`, `false`, numbers, and strings are handled correctly). Lists and dicts cannot be set via environment variables — use a config file for those.

---

## 4. Configuration Schema

The complete configuration is represented as a tree of frozen dataclasses. Each dataclass corresponds to a YAML section. The root is `OrchestratorConfig`.

### 4.1 Root Configuration

```python
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class OrchestratorConfig:
    """Root configuration for the FAM orchestrator."""

    telemetry: "TelemetryConfig"
    pricing: "PricingConfig"
    budget: "BudgetConfig"
    signals: "SignalsConfig"
    confirmation: "ConfirmationConfig"
    dispatch: "DispatchConfig"
    speculative: "SpeculativeConfig"
    adapter: "AdapterConfig"
    interfaces: "InterfacesConfig"
    metrics: "MetricsConfig"
    eval: "EvalConfig"
```

### 4.2 Telemetry Configuration (spec 01)

```python
@dataclass(frozen=True)
class TelemetryConfig:
    tick_interval_seconds: float = 0.5

    gpu: "GPUTelemetryConfig" = field(default_factory=lambda: GPUTelemetryConfig())
    tools: "ToolTelemetryConfig" = field(default_factory=lambda: ToolTelemetryConfig())
    history: "HistoryConfig" = field(default_factory=lambda: HistoryConfig())


@dataclass(frozen=True)
class GPUTelemetryConfig:
    poll_interval_seconds: float = 0.5
    poll_timeout_seconds: float = 2.0
    max_consecutive_failures: int = 5
    staleness_threshold_seconds: float = 1.5
    unhealthy_threshold_seconds: float = 5.0
    recovery_success_count: int = 2
    queue_depth_capacity: int = 64
    max_concurrent_requests: int = 128

    utilization_weights: "UtilizationWeightsGPU" = field(
        default_factory=lambda: UtilizationWeightsGPU()
    )
    smoothing: "GPUSmoothingConfig" = field(
        default_factory=lambda: GPUSmoothingConfig()
    )
    fallback: "GPUFallbackConfig" = field(
        default_factory=lambda: GPUFallbackConfig()
    )


@dataclass(frozen=True)
class UtilizationWeightsGPU:
    queue_depth: float = 0.4
    kv_cache: float = 0.35
    active_requests: float = 0.25


@dataclass(frozen=True)
class GPUSmoothingConfig:
    batch_queue_depth: float = 0.3
    kv_cache_occupancy: float = 0.2
    active_requests: float = 0.3
    inference_latency_ms: float = 0.1
    gpu_utilization: float = 0.25


@dataclass(frozen=True)
class GPUFallbackConfig:
    kv_cache_occupancy: float = 0.9
    gpu_utilization: float = 0.9
    latency_multiplier: float = 2.0


@dataclass(frozen=True)
class ToolTelemetryConfig:
    poll_interval_seconds: float = 1.0
    poll_timeout_seconds: float = 2.0
    max_consecutive_failures: int = 5
    staleness_threshold_seconds: float = 3.0
    unhealthy_threshold_seconds: float = 10.0
    recovery_success_count: int = 2
    default_max_concurrent_calls: int = 10

    utilization_weights: "UtilizationWeightsTool" = field(
        default_factory=lambda: UtilizationWeightsTool()
    )
    smoothing: "ToolSmoothingConfig" = field(
        default_factory=lambda: ToolSmoothingConfig()
    )
    rolling_window: "RollingWindowConfig" = field(
        default_factory=lambda: RollingWindowConfig()
    )
    fallback: "ToolFallbackConfig" = field(
        default_factory=lambda: ToolFallbackConfig()
    )
    endpoints: dict = field(default_factory=dict)


@dataclass(frozen=True)
class UtilizationWeightsTool:
    active_calls: float = 0.5
    rate_limit_headroom: float = 0.35
    error_rate: float = 0.15


@dataclass(frozen=True)
class ToolSmoothingConfig:
    active_calls: float = 0.3
    latency_p50_ms: float = 0.15
    error_rate: float = 0.1
    rate_limit_headroom: float = 0.4
    tool_utilization: float = 0.25


@dataclass(frozen=True)
class RollingWindowConfig:
    max_events: int = 100
    max_age_seconds: float = 60.0


@dataclass(frozen=True)
class ToolFallbackConfig:
    error_rate: float = 0.5
    rate_limit_headroom: float = 0.1
    tool_utilization: float = 0.85
    latency_multiplier: float = 2.0


@dataclass(frozen=True)
class HistoryConfig:
    enabled: bool = True
    max_snapshots: int = 600
```

### 4.3 Pricing Engine Configuration (spec 02)

```python
@dataclass(frozen=True)
class PricingConfig:
    update_interval_seconds: float = 1.0

    reasoning: "ReasoningPriceConfig" = field(
        default_factory=lambda: ReasoningPriceConfig()
    )
    tool: "ToolPriceConfig" = field(
        default_factory=lambda: ToolPriceConfig()
    )


@dataclass(frozen=True)
class ReasoningPriceConfig:
    kp: float = 1.0                        # Proportional gain
    ki: float = 0.0                        # Integral gain (0 = P-only controller)
    floor: Decimal = Decimal("0.01")       # Minimum price per token
    ceiling: Decimal = Decimal("10.0")     # Maximum price per token
    congestion_threshold: float = 0.7      # Utilization above which price rises steeply
    smoothing_alpha: float = 0.3           # Output price EMA smoothing


@dataclass(frozen=True)
class ToolPriceConfig:
    kp: float = 1.0
    ki: float = 0.0
    floor: Decimal = Decimal("0.1")
    ceiling: Decimal = Decimal("50.0")
    congestion_threshold: float = 0.7
    smoothing_alpha: float = 0.3
```

### 4.4 Budget Manager Configuration (spec 03)

```python
@dataclass(frozen=True)
class BudgetConfig:
    initial_balance: Decimal = Decimal("100.0")
    max_balance: Decimal = Decimal("200.0")
    replenishment_rate: Decimal = Decimal("1.0")    # Units per tick
    replenishment_interval_seconds: float = 1.0
    soft_limit: Decimal = Decimal("5.0")            # Warning threshold
    hard_limit: Decimal = Decimal("0.0")            # Absolute floor
    track_history: bool = True
    history_max_entries: int = 10000
```

### 4.5 Signals Configuration (specs 04, 05)

```python
@dataclass(frozen=True)
class SignalsConfig:
    default_tier: str = "directional"      # Default capability tier for unprobed agents

    # Tier assignment thresholds (from capability probe, spec 15)
    tier_thresholds: "TierThresholds" = field(
        default_factory=lambda: TierThresholds()
    )

    # Pricing signal injection frequency
    inject_every_n_turns: int = 1          # Inject signal before every Nth LLM call
    max_signal_history: int = 20           # Max signals retained in agent context

    # Message formatting
    message_prefix: str = "[FAM]"

    templates: "SignalTemplates" = field(
        default_factory=lambda: SignalTemplates()
    )


@dataclass(frozen=True)
class TierThresholds:
    quantitative_min_score: float = 0.8    # Probe score >= this → quantitative tier
    qualitative_min_score: float = 0.5     # Probe score >= this → qualitative tier
    # Below qualitative_min_score → directional tier


@dataclass(frozen=True)
class SignalTemplates:
    quantitative_pricing: str = (
        "[FAM] Reasoning: {reasoning_price}/tok | Tools: {tool_price}/call | "
        "Budget: {balance} ({runway} tok remaining)"
    )
    qualitative_pricing: str = (
        "[FAM] Reasoning cost: {reasoning_level}. Tool cost: {tool_level}. "
        "Budget: {budget_level}."
    )
    directional_pricing: str = "[FAM] {nudge}"
```

### 4.6 Confirmation Handler Configuration (spec 06)

```python
@dataclass(frozen=True)
class ConfirmationConfig:
    enabled: bool = True

    # Tool price threshold below which calls are auto-approved
    auto_approve_threshold: Decimal = Decimal("1.0")

    # Timeout waiting for agent decision
    timeout_seconds: float = 30.0
    timeout_policy: str = "auto_cancel"    # "auto_approve" or "auto_cancel"

    # Budget check behavior
    block_on_insufficient_budget: bool = True

    # Confirmation request template (uses tiered formatting)
    include_alternatives: bool = True      # Suggest "reason more" as alternative
    include_wait_estimate: bool = True     # Include estimated wait time in request
```

### 4.7 Dispatch Queue Configuration (spec 07)

```python
@dataclass(frozen=True)
class DispatchConfig:
    # Per-endpoint dispatch worker count
    workers_per_endpoint: int = 3

    # Priority weights for queue ordering
    priority_weights: "PriorityWeights" = field(
        default_factory=lambda: PriorityWeights()
    )

    # Retry configuration
    max_retries: int = 3
    retry_backoff_base_seconds: float = 1.0
    retry_backoff_max_seconds: float = 30.0

    # Deferral threshold (queue depth beyond which new calls are deferred)
    deferral_queue_depth_threshold: int = 20

    # Rate limiting
    default_rate_limit_rpm: int = 60       # Default requests per minute per endpoint
    rate_limit_buffer_fraction: float = 0.1  # Reserve 10% of rate limit as headroom


@dataclass(frozen=True)
class PriorityWeights:
    budget_remaining: float = 0.4          # Higher budget → higher priority
    task_priority: float = 0.3             # External priority assignment
    time_in_queue: float = 0.3             # Longer wait → higher priority (anti-starvation)
```

### 4.8 Speculative Continuation Configuration (spec 08)

```python
@dataclass(frozen=True)
class SpeculativeConfig:
    enabled: bool = True
    reconciliation_strategy: str = "always_rollback"  # or "consistency_check"
    max_speculation_duration_seconds: float = 60.0
    timeout_policy: str = "auto_dispatch"  # "auto_dispatch", "cancel", or "notify"
    max_speculative_tokens_per_session: int = 2000
    max_concurrent_sessions: int = 50
    session_history_size: int = 1000

    consistency_check: "ConsistencyCheckConfig" = field(
        default_factory=lambda: ConsistencyCheckConfig()
    )
    budget: "SpeculativeBudgetConfig" = field(
        default_factory=lambda: SpeculativeBudgetConfig()
    )
    prompts: "SpeculativePromptConfig" = field(
        default_factory=lambda: SpeculativePromptConfig()
    )


@dataclass(frozen=True)
class ConsistencyCheckConfig:
    threshold: float = 0.7
    max_tokens_to_analyze: int = 500


@dataclass(frozen=True)
class SpeculativeBudgetConfig:
    min_balance_to_start: Decimal = Decimal("5.0")
    stop_at_soft_limit: bool = True


@dataclass(frozen=True)
class SpeculativePromptConfig:
    quantitative: str | None = None        # None = use built-in default
    qualitative: str | None = None
    directional: str | None = None
```

### 4.9 Adapter Configuration (spec 09)

```python
@dataclass(frozen=True)
class AdapterConfig:
    name: str = "langgraph"

    langgraph: "LangGraphAdapterConfig" = field(
        default_factory=lambda: LangGraphAdapterConfig()
    )
    openai: "OpenAIAdapterConfig" = field(
        default_factory=lambda: OpenAIAdapterConfig()
    )
    parsing: "ParsingConfig" = field(
        default_factory=lambda: ParsingConfig()
    )


@dataclass(frozen=True)
class LangGraphAdapterConfig:
    default_tool_node_name: str = "tools"
    default_llm_node_name: str = "agent"
    fallback_tokens_per_char: float = 0.25
    confirmation_timeout_seconds: float = 30.0
    confirmation_timeout_policy: str = "auto_cancel"
    inject_pricing_signals: bool = True
    max_fam_messages: int = 50


@dataclass(frozen=True)
class OpenAIAdapterConfig:
    inject_pricing_signals: bool = True
    max_fam_system_messages: int = 10
    confirmation_max_retries: int = 3


@dataclass(frozen=True)
class ParsingConfig:
    min_parse_confidence: float = 0.3
    default_decision_on_low_confidence: str = "auto_cancel"
    structured_hint_enabled: bool = True
```

### 4.10 Interfaces Configuration (specs 10, 11)

```python
@dataclass(frozen=True)
class InterfacesConfig:
    gpu: "GPUInterfaceConfig" = field(
        default_factory=lambda: GPUInterfaceConfig()
    )
    tools: "ToolInterfaceConfig" = field(
        default_factory=lambda: ToolInterfaceConfig()
    )


@dataclass(frozen=True)
class GPUInterfaceConfig:
    backend: str = "mock"                  # "mock", "vllm", "tgi"
    endpoint_url: str = ""                 # URL for real backends
    mock: "GPUMockConfig" = field(
        default_factory=lambda: GPUMockConfig()
    )


@dataclass(frozen=True)
class GPUMockConfig:
    base_utilization: float = 0.3
    utilization_noise: float = 0.05
    burst_probability: float = 0.1
    burst_magnitude: float = 0.4
    burst_duration_seconds: float = 5.0
    kv_cache_base: float = 0.4
    latency_base_ms: float = 50.0
    latency_noise_ms: float = 10.0


@dataclass(frozen=True)
class ToolInterfaceConfig:
    endpoints: dict[str, "ToolEndpointConfig"] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolEndpointConfig:
    url: str = ""
    backend: str = "mock"                  # "mock" or "http"
    rate_limit_rpm: int = 60
    max_concurrent: int = 10
    idempotent: bool = False
    mock: "ToolMockConfig" = field(
        default_factory=lambda: ToolMockConfig()
    )


@dataclass(frozen=True)
class ToolMockConfig:
    latency_mean_ms: float = 200.0
    latency_std_ms: float = 50.0
    latency_distribution: str = "normal"   # "normal", "fixed", "heavy_tailed"
    failure_rate: float = 0.02
    rate_limit_simulate: bool = True
```

### 4.11 Metrics Configuration (spec 13)

```python
@dataclass(frozen=True)
class MetricsConfig:
    enabled: bool = True

    # Prometheus export
    prometheus: "PrometheusConfig" = field(
        default_factory=lambda: PrometheusConfig()
    )

    # Structured logging
    logging: "LoggingConfig" = field(
        default_factory=lambda: LoggingConfig()
    )

    # Internal metrics buffer
    buffer_max_events: int = 100000
    flush_interval_seconds: float = 5.0


@dataclass(frozen=True)
class PrometheusConfig:
    enabled: bool = True
    port: int = 9090
    path: str = "/metrics"
    namespace: str = "fam"


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"                    # Python logging level
    format: str = "json"                   # "json" or "text"
    output: str = "stderr"                 # "stderr", "stdout", or file path
    include_pricing_decisions: bool = True
    include_budget_mutations: bool = True
    include_confirmation_flow: bool = True
```

### 4.12 Evaluation Harness Configuration (spec 14)

```python
@dataclass(frozen=True)
class EvalConfig:
    task_suite: str = "default"
    max_concurrent_agents: int = 10
    max_task_duration_seconds: float = 300.0
    results_dir: str = "eval_results"
    record_full_traces: bool = True
    baseline_configs: dict[str, str] = field(default_factory=dict)
```

---

## 5. Default Configuration File

The `defaults.yaml` file contains production-quality defaults suitable for a development environment with mock backends and moderate agent concurrency.

```yaml
# fam/config/defaults.yaml
# Default FAM configuration — development/testing environment.

telemetry:
  tick_interval_seconds: 0.5
  gpu:
    poll_interval_seconds: 0.5
    poll_timeout_seconds: 2.0
    max_consecutive_failures: 5
    staleness_threshold_seconds: 1.5
    unhealthy_threshold_seconds: 5.0
    recovery_success_count: 2
    queue_depth_capacity: 64
    max_concurrent_requests: 128
    utilization_weights:
      queue_depth: 0.4
      kv_cache: 0.35
      active_requests: 0.25
    smoothing:
      batch_queue_depth: 0.3
      kv_cache_occupancy: 0.2
      active_requests: 0.3
      inference_latency_ms: 0.1
      gpu_utilization: 0.25
    fallback:
      kv_cache_occupancy: 0.9
      gpu_utilization: 0.9
      latency_multiplier: 2.0
  tools:
    poll_interval_seconds: 1.0
    poll_timeout_seconds: 2.0
    max_consecutive_failures: 5
    staleness_threshold_seconds: 3.0
    unhealthy_threshold_seconds: 10.0
    recovery_success_count: 2
    default_max_concurrent_calls: 10
    utilization_weights:
      active_calls: 0.5
      rate_limit_headroom: 0.35
      error_rate: 0.15
    smoothing:
      active_calls: 0.3
      latency_p50_ms: 0.15
      error_rate: 0.1
      rate_limit_headroom: 0.4
      tool_utilization: 0.25
    rolling_window:
      max_events: 100
      max_age_seconds: 60.0
    fallback:
      error_rate: 0.5
      rate_limit_headroom: 0.1
      tool_utilization: 0.85
      latency_multiplier: 2.0
  history:
    enabled: true
    max_snapshots: 600

pricing:
  update_interval_seconds: 1.0
  reasoning:
    kp: 1.0
    ki: 0.0
    floor: 0.01
    ceiling: 10.0
    congestion_threshold: 0.7
    smoothing_alpha: 0.3
  tool:
    kp: 1.0
    ki: 0.0
    floor: 0.1
    ceiling: 50.0
    congestion_threshold: 0.7
    smoothing_alpha: 0.3

budget:
  initial_balance: 100.0
  max_balance: 200.0
  replenishment_rate: 1.0
  replenishment_interval_seconds: 1.0
  soft_limit: 5.0
  hard_limit: 0.0
  track_history: true
  history_max_entries: 10000

signals:
  default_tier: "directional"
  tier_thresholds:
    quantitative_min_score: 0.8
    qualitative_min_score: 0.5
  inject_every_n_turns: 1
  max_signal_history: 20
  message_prefix: "[FAM]"

confirmation:
  enabled: true
  auto_approve_threshold: 1.0
  timeout_seconds: 30.0
  timeout_policy: "auto_cancel"
  block_on_insufficient_budget: true
  include_alternatives: true
  include_wait_estimate: true

dispatch:
  workers_per_endpoint: 3
  priority_weights:
    budget_remaining: 0.4
    task_priority: 0.3
    time_in_queue: 0.3
  max_retries: 3
  retry_backoff_base_seconds: 1.0
  retry_backoff_max_seconds: 30.0
  deferral_queue_depth_threshold: 20
  default_rate_limit_rpm: 60
  rate_limit_buffer_fraction: 0.1

speculative:
  enabled: true
  reconciliation_strategy: "always_rollback"
  max_speculation_duration_seconds: 60.0
  timeout_policy: "auto_dispatch"
  max_speculative_tokens_per_session: 2000
  max_concurrent_sessions: 50
  session_history_size: 1000
  consistency_check:
    threshold: 0.7
    max_tokens_to_analyze: 500
  budget:
    min_balance_to_start: 5.0
    stop_at_soft_limit: true

adapter:
  name: "langgraph"
  langgraph:
    default_tool_node_name: "tools"
    default_llm_node_name: "agent"
    fallback_tokens_per_char: 0.25
    confirmation_timeout_seconds: 30.0
    confirmation_timeout_policy: "auto_cancel"
    inject_pricing_signals: true
    max_fam_messages: 50
  openai:
    inject_pricing_signals: true
    max_fam_system_messages: 10
    confirmation_max_retries: 3
  parsing:
    min_parse_confidence: 0.3
    default_decision_on_low_confidence: "auto_cancel"
    structured_hint_enabled: true

interfaces:
  gpu:
    backend: "mock"
    endpoint_url: ""
    mock:
      base_utilization: 0.3
      utilization_noise: 0.05
      burst_probability: 0.1
      burst_magnitude: 0.4
      burst_duration_seconds: 5.0
      kv_cache_base: 0.4
      latency_base_ms: 50.0
      latency_noise_ms: 10.0
  tools:
    endpoints: {}

metrics:
  enabled: true
  prometheus:
    enabled: true
    port: 9090
    path: "/metrics"
    namespace: "fam"
  logging:
    level: "INFO"
    format: "json"
    output: "stderr"
    include_pricing_decisions: true
    include_budget_mutations: true
    include_confirmation_flow: true
  buffer_max_events: 100000
  flush_interval_seconds: 5.0

eval:
  task_suite: "default"
  max_concurrent_agents: 10
  max_task_duration_seconds: 300.0
  results_dir: "eval_results"
  record_full_traces: true
```

---

## 6. Validation Rules

Configuration validation runs after merging all sources. Validation catches type errors, range violations, and cross-parameter constraint violations. All violations are collected and reported together rather than failing on the first error.

### 6.1 Type and Range Rules

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationError:
    path: str          # Dotted path, e.g., "pricing.reasoning.kp"
    message: str
    value: object      # The invalid value


class ConfigValidator:
    """Validates a merged config dict against the schema."""

    def validate(self, config: dict) -> list[ValidationError]:
        """Return all validation errors. Empty list means valid."""
```

Range constraints by parameter category:

| Parameter Type | Constraint | Example |
|---|---|---|
| Time intervals (`*_seconds`) | > 0.0 | `poll_interval_seconds > 0` |
| EMA alphas (`smoothing.*`) | 0.0 < α ≤ 1.0 | `smoothing.batch_queue_depth in (0, 1]` |
| Utilization weights | Each ≥ 0, sum = 1.0 | `utilization_weights sum to 1.0` |
| Prices (floor, ceiling) | floor > 0, ceiling > floor | `reasoning.floor < reasoning.ceiling` |
| Budget values | initial ≤ max, soft > hard | `initial_balance <= max_balance` |
| Counts (max_retries, etc.) | ≥ 0 | `max_retries >= 0` |
| Ratios (0–1) | 0.0 ≤ x ≤ 1.0 | `error_rate in [0, 1]` |
| Enum strings | Must be valid enum value | `timeout_policy in {"auto_approve", "auto_cancel"}` |

### 6.2 Cross-Parameter Constraints

Some constraints span multiple parameters:

- `telemetry.gpu.staleness_threshold_seconds` should be ≥ 2× `telemetry.gpu.poll_interval_seconds` (warn if not).
- `telemetry.gpu.unhealthy_threshold_seconds` should be > `staleness_threshold_seconds` (error if not).
- `budget.soft_limit` must be > `budget.hard_limit` (error if not).
- `budget.initial_balance` must be ≤ `budget.max_balance` (error if not).
- `pricing.reasoning.floor` must be < `pricing.reasoning.ceiling` (error if not).
- `dispatch.priority_weights` values must sum to 1.0 within tolerance of 0.001 (error if not).
- `adapter.name` must be one of the registered adapter names (error if not).

### 6.3 Validation Output

```python
class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""

    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        messages = [f"  {e.path}: {e.message} (got: {e.value!r})" for e in errors]
        super().__init__(
            f"Configuration validation failed with {len(errors)} error(s):\n"
            + "\n".join(messages)
        )
```

---

## 7. Hot Reload

Some parameters can be changed at runtime without restarting the orchestrator. Others require a restart because they affect initialization state that cannot be reconstructed.

### 7.1 Hot-Reloadable Parameters

The following parameters can be changed at runtime. When the config file changes, the hot-reload watcher detects the change, re-validates the new file, and applies the new values to the running system.

| Category | Parameters | Mechanism |
|---|---|---|
| Pricing gains and limits | `pricing.reasoning.*`, `pricing.tool.*` | Pricing engine reads config on each tick |
| Budget replenishment | `budget.replenishment_rate`, `budget.soft_limit` | Budget manager reads config on each tick |
| Confirmation threshold | `confirmation.auto_approve_threshold` | Confirmation handler reads config per call |
| Signal templates | `signals.templates.*` | Formatter reads templates on each format call |
| Speculative prompts | `speculative.prompts.*` | Prompt renderer reads config on each render |
| Dispatch priorities | `dispatch.priority_weights.*` | Queue reads weights on each enqueue |
| Logging level | `metrics.logging.level` | Logger level updated immediately |
| Smoothing alphas | `telemetry.*.smoothing.*` | EMA states updated on next tick |

### 7.2 Restart-Required Parameters

These parameters cannot be changed at runtime because they affect initialization state:

| Category | Parameters | Reason |
|---|---|---|
| Adapter type | `adapter.name` | Adapter is instantiated at startup |
| Interface backend | `interfaces.gpu.backend` | Backend connection established at startup |
| Prometheus port | `metrics.prometheus.port` | HTTP server bound at startup |
| Telemetry tick interval | `telemetry.tick_interval_seconds` | Asyncio task created at startup |
| Budget initial balance | `budget.initial_balance` | Applied once per agent at registration |
| Evaluation settings | `eval.*` | Harness configured at experiment start |

### 7.3 Hot-Reload Watcher

```python
import asyncio
from pathlib import Path


class HotReloadWatcher:
    """Watches the config file for changes and applies hot-reloadable parameters."""

    def __init__(
        self,
        config_path: Path,
        orchestrator: "Orchestrator",
        check_interval_seconds: float = 2.0,
    ) -> None:
        self._path = config_path
        self._orchestrator = orchestrator
        self._interval = check_interval_seconds
        self._last_mtime: float = 0.0
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the file watcher loop."""
        self._last_mtime = self._path.stat().st_mtime
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        """Stop the file watcher."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _watch_loop(self) -> None:
        """Periodically check config file mtime and reload if changed."""
        while True:
            await asyncio.sleep(self._interval)
            try:
                current_mtime = self._path.stat().st_mtime
                if current_mtime > self._last_mtime:
                    self._last_mtime = current_mtime
                    await self._reload()
            except FileNotFoundError:
                pass
            except Exception as e:
                import logging
                logging.getLogger("fam.config").error(
                    "Hot reload failed: %s", e
                )

    async def _reload(self) -> None:
        """Reload config and apply hot-reloadable parameters."""
```

The watcher compares the config file's modification time (`st_mtime`) on each check. When a change is detected, it reloads the file, validates it, and applies only the hot-reloadable parameters. If validation fails, the reload is rejected and the current configuration is retained. A log entry records the reload attempt and its outcome.

---

## 8. Error Handling

**Missing config file.** If the user-specified config file does not exist, startup fails with a clear error message indicating the expected path. If only the defaults file is used (no user file specified), this is not an error.

**Invalid YAML syntax.** If the config file contains invalid YAML, the `ConfigLoader.load()` method raises a `ConfigLoadError` with the YAML parser's error message and the line number. This error is fatal at startup and logged at ERROR level during hot reload (reload rejected, existing config retained).

**Unknown keys.** Keys in the config file that do not match any schema field are logged as warnings but do not cause validation failure. This allows forward compatibility — a newer config file can be used with an older version of FAM that does not recognize new keys.

**Type mismatches.** If a value has the wrong type (e.g., a string where a float is expected), validation produces a `ValidationError` with the path, expected type, and actual type. If the value can be safely coerced (e.g., int to float), it is coerced silently. If not (e.g., string to float where the string is not numeric), it is an error.

**Environment variable parse failure.** If a `FAM__*` environment variable contains a value that cannot be parsed as a YAML scalar, it is ignored and a warning is logged.

**Hot-reload of restart-required parameter.** If the user changes a restart-required parameter in the config file, the hot-reload watcher logs a warning indicating which parameters were changed but cannot be applied, and suggests restarting the orchestrator.

---

## 9. Metrics Emitted

The configuration system emits the following metrics to the observability system (spec 13):

**Counters:** `config.loads.total` (per outcome: success/failure), `config.hot_reloads.total` (per outcome: applied/rejected/partial), `config.validation_errors.total` (per error path).

**Gauges:** `config.hot_reload.last_success_timestamp`, `config.parameters.overridden_count` (number of parameters overridden from defaults).

**Events:** `config.loaded` (emitted once at startup with the full config summary), `config.hot_reloaded` (emitted on each successful hot reload with the changed parameters).

---

## 10. Testing Strategy

### 10.1 Unit Tests

**Default loading:** Load `defaults.yaml`, verify all fields parse to the correct types, verify all defaults match the schema's default values.

**Deep merge:** Test merging a partial override dict into a full config dict. Verify that overridden values replace defaults. Verify that non-overridden values retain defaults. Verify that nested dicts merge correctly. Verify that list values are replaced entirely (not concatenated).

**Environment variable parsing:** Set `FAM__PRICING__REASONING__KP=2.0`, load config, verify the value is applied. Test with float, int, bool, and string values. Test with an unparseable value, verify it is ignored with a warning.

**Validation — valid config:** Load defaults, validate, verify zero errors.

**Validation — range violations:** Set `pricing.reasoning.floor` to -1.0, validate, verify an error referencing that path. Set `telemetry.gpu.smoothing.batch_queue_depth` to 1.5, verify error (must be ≤ 1.0).

**Validation — cross-parameter violations:** Set `budget.soft_limit` to 0.0 and `budget.hard_limit` to 5.0, verify error (soft must be > hard). Set utilization weights that do not sum to 1.0, verify error.

**Frozen dataclass enforcement:** Attempt to mutate a field on a loaded `OrchestratorConfig`, verify `FrozenInstanceError` is raised.

### 10.2 Integration Tests

**Full config lifecycle:** Create a config file, load it, verify the resulting `OrchestratorConfig` has the expected values. Modify the file, trigger hot reload, verify hot-reloadable parameters are updated. Verify restart-required parameters are not applied and a warning is logged.

**Orchestrator startup with config:** Start the orchestrator with a custom config, verify that each module receives its config section and uses the configured values. Change pricing gains via hot reload, verify the pricing engine uses the new values on the next tick.

**Config file errors during hot reload:** Start with a valid config, replace the file with invalid YAML, verify the watcher logs an error and retains the previous config. Replace with a valid but constraint-violating config, verify the watcher rejects the reload.

### 10.3 Property-Based Tests

Use Hypothesis to generate random valid configuration dicts (within schema constraints). Verify that `ConfigLoader.load()` succeeds for all of them. Generate random deep-merge pairs and verify that the merge result always contains all keys from both inputs. Generate random environment variable names matching the `FAM__*` pattern and verify they either parse successfully or are ignored gracefully.

---

## 11. Dependencies

**Internal:** None. The config module is a leaf dependency — it is consumed by every other module but depends on none of them (except for the type definitions of the config dataclasses, which are self-contained).

**External:** `pyyaml` — for YAML parsing. This is the only external dependency of the config module. `pyyaml` is a mature, well-maintained library with no transitive dependencies. It is used for loading and parsing only — config files are never written programmatically.

**Standard library:** `dataclasses`, `decimal`, `pathlib`, `os` (for environment variables), `logging`, `asyncio` (for hot-reload watcher), `typing`.

---

## 12. Open Questions

**Pydantic vs. plain dataclasses.** The schema is currently defined using frozen dataclasses. Pydantic would provide automatic validation, type coercion, JSON schema generation, and better error messages. The tradeoff is an additional dependency and the opinionated behavior of Pydantic's validation (e.g., silent type coercion that may mask errors). The initial implementation uses plain dataclasses with manual validation to minimize dependencies; switching to Pydantic can be evaluated after the initial implementation is stable.

**Config inheritance for experiments.** The evaluation harness (spec 14) needs to run multiple experiments with different configurations. Currently this requires separate config files per experiment. A config inheritance mechanism (e.g., `extends: base_config.yaml`) would reduce duplication. Deferred — the evaluation harness can manage config generation programmatically.

**Secrets management.** The current config system has no concept of secrets (API keys, authentication tokens). When the system connects to real backends (production GPU clusters, real tool APIs), secrets should not live in plaintext YAML files. A future enhancement should support environment variable references within the YAML (e.g., `api_key: ${OPENAI_API_KEY}`) or integration with a secrets manager. Deferred because the initial implementation uses mock backends that require no authentication.

**Config diffing for hot reload.** The current hot-reload mechanism reloads the entire config and applies hot-reloadable parameters. A more sophisticated approach would diff the old and new configs and only apply changed parameters, logging each change individually. This provides better observability but adds implementation complexity. Deferred.

**Multi-file config.** Should the config support splitting across multiple files (e.g., `pricing.yaml`, `budget.yaml`)? This would help organize large configurations but adds loading complexity. The single-file approach is simpler and sufficient for the expected config size. Deferred.
