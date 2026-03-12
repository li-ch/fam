# Spec 05 — Tiered Signal Formatter

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview), Spec 02 (Pricing Engine), Spec 03 (Budget Manager), Spec 15 (Capability Probe)
**Consumed By:** Spec 04 (Agent Communication Protocol), Spec 06 (Confirmation Handler), Spec 08 (Speculative Continuation Manager)

---

## 1. Purpose

Not all LLMs are equally capable of reasoning about quantitative economic information. A frontier model can parse a table of prices, compute remaining budget after a deduction, and compare cost-benefit tradeoffs numerically. A smaller model may be confused by precise numbers, misinterpret decimal arithmetic, or anchor on irrelevant figures. Presenting quantitative pricing signals to a model that cannot reason about them is worse than useless — it wastes context tokens and may actively degrade decision quality.

The tiered signal formatter solves this by adapting the complexity of pricing signals to the capability of the receiving agent. It defines three tiers of signal complexity, a protocol for assessing which tier a model belongs to, and the formatting logic that maps raw pricing data into tier-appropriate messages.

This module sits between the pricing engine (which produces raw numeric prices) and the agent communication protocol (which injects formatted messages into agent conversations). It is the translation layer that makes the market mechanism accessible to agents of varying sophistication.

---

## 2. Module Location

```
fam/
├── signals/
│   ├── __init__.py
│   ├── formatter.py       # TieredSignalFormatter class — main entry point
│   ├── templates.py        # Template strings per tier per message type
│   ├── tiers.py            # SignalTier enum, tier assignment logic
│   └── labels.py           # Qualitative label mappings and directional nudge selection
```

Tests live in `tests/test_signals/`.

---

## 3. Signal Tiers

### 3.1 Tier Definitions

```python
class SignalTier(str, Enum):
    QUANTITATIVE = "quantitative"
    QUALITATIVE = "qualitative"
    DIRECTIONAL = "directional"
```

**Quantitative (highest capability):** The agent receives full numerical data — exact prices in decimal units, exact budget balance, replenishment rate, estimated runway, cost-after-deduction calculations, queue depths, and wait time estimates. The message assumes the agent can perform arithmetic, compare magnitudes, and make cost-benefit decisions using the numbers provided.

Models expected to qualify: GPT-4-class, Claude 3.5-class, Gemini 1.5 Pro-class, and similar frontier models. These models consistently handle multi-step arithmetic, budget tracking, and relative price comparison in context.

**Qualitative (moderate capability):** The agent receives descriptive labels instead of raw numbers. Prices are described as "low," "moderate," "high," or "very high." Budget status is described as "comfortable," "adequate," "low," or "critically low." Wait times are "minimal," "short," "moderate," or "long." The message includes directional advice ("reasoning is cheaper than tools right now") but no specific numbers except where unavoidable (e.g., the tool name).

Models expected to qualify: GPT-3.5-class, Claude 3 Haiku-class, Mistral Medium-class, and similar mid-tier models. These models can reason about relative comparisons and descriptive categories but may make arithmetic errors or be distracted by precise figures.

**Directional (lowest capability):** The agent receives simple behavioral nudges with no numeric or categorical detail. Messages are single sentences like "consider reasoning more before calling tools" or "resources are abundant right now." The agent is given a general direction (conserve, spend freely, wait, proceed) but no specific information about why.

Models expected to qualify: Small open-source models (7B–13B parameter range), heavily quantized models, or any model that fails the capability probe. These models may not understand the concept of budgets or prices but can follow simple behavioral instructions.

### 3.2 Tier Capability Matrix

| Capability | Quantitative | Qualitative | Directional |
|---|---|---|---|
| Multi-step arithmetic | Yes | No | No |
| Relative comparison ("X is more than Y") | Yes | Yes | No |
| Categorical reasoning ("high" vs. "low") | Yes | Yes | Partial |
| Following behavioral instructions | Yes | Yes | Yes |
| Budget tracking across turns | Yes | Limited | No |
| Cost-benefit tradeoff analysis | Yes | Partial | No |

---

## 4. Capability Assessment Protocol

Tier assignment is determined by a lightweight probe that tests the agent's economic reasoning capabilities. The probe is run once per model (not per agent instance — all agents using the same model share a tier assignment) and the result is cached.

### 4.1 Probe Design

The capability probe (fully specified in spec 15) consists of five short prompt-response exchanges that test specific reasoning skills. The signal formatter uses the probe's aggregate score to assign a tier. The probe tests:

1. **Budget arithmetic:** Given a balance and a cost, can the model correctly compute the remaining balance?
2. **Relative price comparison:** Given two prices, can the model identify which resource is cheaper and by how much?
3. **Priority ranking under scarcity:** Given three tool options and budget for only one, can the model rank them by value?
4. **Replenishment reasoning:** Given a cost, a replenishment rate, and a budget, should the model wait or spend now?
5. **Anchoring robustness:** Does an irrelevant number in the prompt change the model's decision?

Each test is scored 0 (fail) or 1 (pass). The aggregate score is 0–5.

### 4.2 Score-to-Tier Mapping

```python
@dataclass(frozen=True)
class TierAssignmentThresholds:
    """Minimum probe scores required for each tier."""
    quantitative_min_score: int = 4    # Must pass at least 4 of 5 tests
    qualitative_min_score: int = 2     # Must pass at least 2 of 5 tests
    # Below qualitative_min_score → directional
```

The mapping is:

```
score >= quantitative_min_score → QUANTITATIVE
score >= qualitative_min_score  → QUALITATIVE
score < qualitative_min_score   → DIRECTIONAL
```

### 4.3 Probe Execution Flow

```python
class TierAssigner:
    """Determines the signal tier for a model based on capability probe results."""

    def __init__(
        self,
        probe_runner: ProbeRunner,       # From spec 15
        config: TierAssignmentConfig,
        cache: TierCache,
    ) -> None:
        self._probe = probe_runner
        self._config = config
        self._cache = cache

    async def get_tier(self, model_id: str) -> SignalTier:
        """Return the signal tier for the given model.

        Checks cache first. If not cached, runs the probe.
        """
        cached = self._cache.get(model_id)
        if cached is not None:
            return cached

        score = await self._probe.run(model_id)
        tier = self._score_to_tier(score)
        self._cache.set(model_id, tier, score)
        return tier

    def _score_to_tier(self, score: int) -> SignalTier:
        if score >= self._config.thresholds.quantitative_min_score:
            return SignalTier.QUANTITATIVE
        if score >= self._config.thresholds.qualitative_min_score:
            return SignalTier.QUALITATIVE
        return SignalTier.DIRECTIONAL

    def override_tier(self, model_id: str, tier: SignalTier) -> None:
        """Manually set the tier for a model, bypassing the probe."""
        self._cache.set(model_id, tier, score=None)
```

### 4.4 Tier Cache

The tier cache stores model_id → (tier, score, timestamp) mappings. It supports three storage backends: in-memory (default, lost on restart), file-based (JSON file, persists across restarts), or manual (configuration-only, no probe is ever run).

```python
class TierCache:
    """Caches tier assignments per model_id."""

    def __init__(self, config: TierCacheConfig) -> None:
        self._entries: dict[str, TierCacheEntry] = {}
        self._config = config
        if config.persistence_path:
            self._load_from_file(config.persistence_path)

    def get(self, model_id: str) -> SignalTier | None:
        entry = self._entries.get(model_id)
        if entry is None:
            return None
        if self._is_expired(entry):
            return None
        return entry.tier

    def set(
        self,
        model_id: str,
        tier: SignalTier,
        score: int | None,
    ) -> None:
        self._entries[model_id] = TierCacheEntry(
            tier=tier,
            score=score,
            timestamp=time.time(),
            source="probe" if score is not None else "manual",
        )
        if self._config.persistence_path:
            self._save_to_file(self._config.persistence_path)

    def _is_expired(self, entry: TierCacheEntry) -> bool:
        if entry.source == "manual":
            return False  # Manual overrides never expire
        age = time.time() - entry.timestamp
        return age > self._config.ttl_seconds


@dataclass(frozen=True)
class TierCacheEntry:
    tier: SignalTier
    score: int | None
    timestamp: float
    source: str           # "probe" or "manual"


@dataclass(frozen=True)
class TierCacheConfig:
    ttl_seconds: float = 86400.0          # 24 hours
    persistence_path: str | None = None   # Path to JSON cache file
```

### 4.5 Re-evaluation Policy

Cached tier assignments expire after `ttl_seconds` (default: 24 hours). On the next `get_tier()` call after expiry, the probe is re-run. This handles model updates — if a model is upgraded or fine-tuned, its capability may change.

Re-evaluation can also be triggered manually via `TierAssigner.invalidate(model_id)` or by setting `ttl_seconds` to 0 (always re-probe, expensive but useful for evaluation experiments).

### 4.6 Fallback Tier for Unknown Models

If the probe cannot be run (e.g., the model endpoint is unavailable, or the probe times out), the fallback tier is applied. The default fallback is `QUALITATIVE` — a middle-ground that avoids overwhelming a weak model with numbers while still providing useful directional information.

```python
@dataclass(frozen=True)
class TierAssignmentConfig:
    thresholds: TierAssignmentThresholds = field(
        default_factory=TierAssignmentThresholds
    )
    fallback_tier: SignalTier = SignalTier.QUALITATIVE
    probe_timeout_seconds: float = 30.0
```

---

## 5. Formatting Engine

The formatter is the core of this module. It takes raw pricing state (from the pricing engine and budget manager) plus a target tier, and produces a formatted string ready for injection by the communication protocol.

### 5.1 Formatter Interface

```python
class TieredSignalFormatter:
    """Formats pricing signals adapted to the agent's capability tier."""

    def __init__(self, config: FormatterConfig) -> None:
        self._config = config
        self._label_mapper = QualitativeLabelMapper(config.label_thresholds)
        self._nudge_selector = DirectionalNudgeSelector(config.nudge_rules)

    def format_pricing_update(
        self,
        tier: SignalTier,
        reasoning_price: Decimal,
        tool_prices: dict[str, Decimal],
        budget_remaining: Decimal,
        replenishment_rate: Decimal,
        gpu_utilization: float,
        tool_utilizations: dict[str, float],
    ) -> str:
        """Format a pricing update message for the given tier."""
        if tier == SignalTier.QUANTITATIVE:
            return self._format_pricing_quantitative(
                reasoning_price, tool_prices, budget_remaining,
                replenishment_rate, gpu_utilization, tool_utilizations,
            )
        if tier == SignalTier.QUALITATIVE:
            return self._format_pricing_qualitative(
                reasoning_price, tool_prices, budget_remaining,
                gpu_utilization, tool_utilizations,
            )
        return self._format_pricing_directional(
            reasoning_price, tool_prices, gpu_utilization, tool_utilizations,
        )

    def format_confirmation_request(
        self,
        tier: SignalTier,
        tool_name: str,
        endpoint_id: str,
        cost: Decimal,
        budget_remaining: Decimal,
        budget_after_deduction: Decimal,
        estimated_wait_time_seconds: float,
        queue_depth: int,
        replenishment_rate: Decimal,
        replenishment_eta_seconds: float,
        reasoning_price: Decimal,
    ) -> str:
        """Format a confirmation request for the given tier."""
        ...

    def format_speculative_prompt(
        self,
        tier: SignalTier,
        tool_name: str,
        position_in_queue: int | None,
        reasoning_price: Decimal,
        budget_remaining: Decimal,
    ) -> str:
        """Format a speculative continuation prompt for the given tier."""
        ...

    def format_budget_warning(
        self,
        tier: SignalTier,
        budget_remaining: Decimal,
        warning_threshold: Decimal,
        replenishment_rate: Decimal,
        estimated_exhaustion_seconds: float | None,
    ) -> str:
        """Format a budget warning for the given tier."""
        ...

    def format_tool_result(
        self,
        result: Any,
        was_speculative: bool,
        speculative_tokens_discarded: int,
    ) -> str:
        """Format a tool result injection. Tier-independent."""
        ...
```

### 5.2 Quantitative Formatting

Quantitative formatting is straightforward: insert numbers into templates.

```python
def _format_pricing_quantitative(
    self,
    reasoning_price: Decimal,
    tool_prices: dict[str, Decimal],
    budget_remaining: Decimal,
    replenishment_rate: Decimal,
    gpu_utilization: float,
    tool_utilizations: dict[str, float],
) -> str:
    tool_lines = []
    for eid, price in sorted(tool_prices.items()):
        util = tool_utilizations.get(eid, 0.0)
        tool_lines.append(f"  {eid}: {price} units (utilization: {util:.0%})")
    tool_summary = "\n".join(tool_lines) if tool_lines else "  (no tools registered)"

    spend_rate = self._estimate_spend_rate(reasoning_price, tool_prices)
    runway = (
        float(budget_remaining / spend_rate) if spend_rate > 0 else float("inf")
    )

    return PRICING_UPDATE_QUANTITATIVE.format(
        reasoning_price=reasoning_price,
        tool_price_summary=tool_summary,
        budget_remaining=budget_remaining,
        replenishment_rate=replenishment_rate,
        estimated_runway_seconds=runway,
        gpu_utilization_pct=gpu_utilization * 100,
    )
```

### 5.3 Qualitative Formatting

Qualitative formatting maps numbers to labels using the `QualitativeLabelMapper`.

```python
class QualitativeLabelMapper:
    """Maps numeric values to human-readable categorical labels."""

    def __init__(self, thresholds: QualitativeLabelThresholds) -> None:
        self._thresholds = thresholds

    def cost_label(self, normalized_cost: float) -> str:
        """Map a 0.0–1.0 normalized cost to a label like 'low', 'moderate', 'high'."""
        return self._lookup(normalized_cost, self._thresholds.cost)

    def budget_label(self, normalized_budget: float) -> str:
        """Map a 0.0–1.0 normalized budget to a label."""
        return self._lookup(normalized_budget, self._thresholds.budget)

    def wait_label(self, wait_seconds: float) -> str:
        """Map wait time in seconds to a label."""
        return self._lookup(wait_seconds, self._thresholds.wait)

    def utilization_label(self, utilization: float) -> str:
        """Map 0.0–1.0 utilization to a label."""
        return self._lookup(utilization, self._thresholds.utilization)

    @staticmethod
    def _lookup(value: float, thresholds: list[tuple[float, str]]) -> str:
        for threshold, label in thresholds:
            if value <= threshold:
                return label
        return thresholds[-1][1] if thresholds else "unknown"
```

The qualitative formatter normalizes raw prices to a 0.0–1.0 range using the price floor and ceiling from the pricing engine configuration. A price at the floor maps to 0.0 ("low"), a price at the ceiling maps to 1.0 ("very high"). Budget is normalized against the configured initial budget (so 50% remaining maps to 0.5).

```python
def _normalize_price(self, price: Decimal) -> float:
    """Normalize a price to 0.0–1.0 using configured floor and ceiling."""
    floor = self._config.price_floor
    ceiling = self._config.price_ceiling
    if ceiling <= floor:
        return 0.5
    return float(min(max((price - floor) / (ceiling - floor), 0), 1))

def _normalize_budget(self, budget: Decimal) -> float:
    """Normalize budget to 0.0–1.0 using configured initial budget."""
    initial = self._config.initial_budget
    if initial <= 0:
        return 0.0
    return float(min(budget / initial, 1))
```

### 5.4 Directional Formatting

Directional formatting selects a single nudge sentence from a predefined set based on the overall resource state.

```python
class DirectionalNudgeSelector:
    """Selects behavioral nudges based on aggregate resource state."""

    def __init__(self, config: NudgeConfig) -> None:
        self._config = config

    def select_nudge(
        self,
        reasoning_price: Decimal,
        avg_tool_price: Decimal,
        gpu_utilization: float,
        avg_tool_utilization: float,
        budget_normalized: float,
    ) -> str:
        """Select the most relevant nudge for the current state."""
        if budget_normalized < self._config.budget_low_threshold:
            return self._config.nudges["budget_low"]

        tools_expensive = avg_tool_utilization > self._config.high_utilization_threshold
        reasoning_expensive = gpu_utilization > self._config.high_utilization_threshold
        tools_cheap = avg_tool_utilization < self._config.low_utilization_threshold
        reasoning_cheap = gpu_utilization < self._config.low_utilization_threshold

        if tools_expensive and reasoning_cheap:
            return self._config.nudges["tools_expensive_reasoning_cheap"]
        if tools_cheap and reasoning_expensive:
            return self._config.nudges["tools_cheap_reasoning_expensive"]
        if tools_expensive and reasoning_expensive:
            return self._config.nudges["both_expensive"]
        if tools_cheap and reasoning_cheap:
            return self._config.nudges["both_cheap"]
        return self._config.nudges["neutral"]
```

When the nudge is the empty string (the `"neutral"` case), the directional pricing update is suppressed — no message is injected. This avoids injecting meaningless "everything is fine" messages that waste context tokens.

### 5.5 Alternative Suggestion Generation

For confirmation requests, the formatter generates an `alternative_suggestion` when a cheaper path exists. The logic compares the tool price with the reasoning price to determine if the agent could save budget by reasoning longer instead of calling the tool.

```python
def _generate_alternative(
    self,
    tier: SignalTier,
    tool_cost: Decimal,
    reasoning_price: Decimal,
    estimated_reasoning_tokens: int,
) -> str | None:
    """Generate a suggestion for a cheaper alternative, if one exists.

    Returns None if no meaningful alternative exists.
    """
    estimated_reasoning_cost = reasoning_price * estimated_reasoning_tokens
    if tool_cost <= estimated_reasoning_cost:
        return None  # Tool is already cheaper; no alternative to suggest

    ratio = float(tool_cost / estimated_reasoning_cost) if estimated_reasoning_cost > 0 else float("inf")
    if ratio < self._config.alternative_suggestion_min_ratio:
        return None  # Difference too small to be worth mentioning

    if tier == SignalTier.QUANTITATIVE:
        return (
            f"Reasoning is currently {ratio:.1f}× cheaper than this tool call. "
            f"~{estimated_reasoning_tokens} reasoning tokens would cost "
            f"{estimated_reasoning_cost} units vs. {tool_cost} units for the tool."
        )
    if tier == SignalTier.QUALITATIVE:
        return "Reasoning is significantly cheaper than tools right now. Consider thinking further."
    return "Consider reasoning more before calling tools."
```

---

## 6. Formatting Pipeline

The complete pipeline from raw pricing state to injected message:

```
PricingEngine.current_prices ──┐
                                ├──▶ TieredSignalFormatter.format_*()
BudgetManager.get_balance() ───┘          │
                                          ▼
                                   Formatted string
                                          │
                    TierAssigner          │
                   .get_tier() ──────────▶│
                                          │
                                          ▼
                               FAMMessageEnvelope
                                   (spec 04)
                                          │
                                          ▼
                               InjectionStrategy
                              .inject_*() (spec 04)
```

The formatter is stateless. It receives all inputs per call and produces a string. It does not cache formatted messages or maintain per-agent state. Caching is unnecessary because formatting is fast (string interpolation) and inputs change on every pricing tick.

---

## 7. Configuration

All configuration for the tiered signal formatter is namespaced under `signals` in the global FAM configuration (spec 12).

```yaml
signals:
  # Tier assignment
  tier_assignment:
    thresholds:
      quantitative_min_score: 4
      qualitative_min_score: 2
    fallback_tier: "qualitative"
    probe_timeout_seconds: 30.0

  # Tier cache
  tier_cache:
    ttl_seconds: 86400                      # 24 hours
    persistence_path: null                  # Set to a file path to persist across restarts

  # Manual tier overrides (bypasses probe)
  tier_overrides:
    # Example:
    # "gpt-4o": "quantitative"
    # "gpt-3.5-turbo": "qualitative"
    # "phi-3-mini": "directional"

  # Normalization parameters for qualitative tier
  normalization:
    price_floor: 0.01
    price_ceiling: 10.0
    initial_budget: 100.0

  # Qualitative label thresholds
  qualitative_labels:
    cost:
      - [0.3, "low"]
      - [0.6, "moderate"]
      - [0.8, "high"]
      - [1.0, "very high"]
    budget:
      - [0.2, "critically low"]
      - [0.4, "low"]
      - [0.7, "adequate"]
      - [1.0, "comfortable"]
    wait:
      - [1.0, "minimal"]
      - [3.0, "short"]
      - [10.0, "moderate"]
      - [.inf, "long"]
    utilization:
      - [0.3, "low"]
      - [0.6, "moderate"]
      - [0.8, "high"]
      - [1.0, "very high"]

  # Directional nudge configuration
  directional:
    high_utilization_threshold: 0.7
    low_utilization_threshold: 0.3
    budget_low_threshold: 0.2
    nudges:
      tools_expensive_reasoning_cheap: "Consider reasoning more before calling tools."
      tools_cheap_reasoning_expensive: "Tool calls are cheap right now — good time to use them."
      both_expensive: "Resources are scarce. Be conservative."
      both_cheap: "Resources are abundant right now."
      budget_low: "Your budget is running low. Conserve resources."
      neutral: ""

  # Alternative suggestion
  alternative_suggestion:
    enabled: true
    min_ratio: 2.0                          # Tool must be 2× more expensive than reasoning
    estimated_reasoning_tokens: 200         # Assumed reasoning tokens for comparison
```

---

## 8. Error Handling

**Normalization edge cases:** If the price ceiling equals the price floor (misconfiguration), normalization returns 0.5 for all prices. If the initial budget is zero or negative, budget normalization returns 0.0. These degenerate cases are logged at WARNING level.

**Missing tool prices:** If the `tool_prices` dict is empty (no tools registered or pricing engine has not yet produced a tool price), the formatter uses a placeholder: "No tool pricing data available" for quantitative, "Tool pricing unavailable" for qualitative, and an empty string for directional (suppressed). The formatter never raises an exception for missing data.

**Template rendering errors:** If a template format string fails (e.g., missing key), the formatter catches `KeyError` and `ValueError`, logs the error, and returns a generic fallback message: "[FAM] Pricing signal formatting error. Using defaults." This ensures the communication protocol always has a string to inject, even if it is not ideally formatted.

**Probe failures:** If the capability probe fails (timeout, model error, network error), the tier assigner returns the fallback tier and logs the failure. The probe is not retried within the same `get_tier()` call — the cache TTL governs when the next attempt occurs.

**Invalid tier override:** If the configuration specifies a `tier_overrides` entry with an invalid tier name, the entry is ignored and a warning is logged at startup. Valid tier names are "quantitative", "qualitative", and "directional" (case-insensitive).

---

## 9. Metrics Emitted

The formatter emits the following metrics to the observability system (spec 13):

**Counters:** `signals.format.total` (per tier, per message_type), `signals.format.suppressed` (directional messages suppressed due to neutral state), `signals.alternative.generated` (alternative suggestions produced), `signals.alternative.suppressed` (alternatives not generated because ratio was too low), `signals.probe.run` (per model_id), `signals.probe.failed` (per model_id), `signals.tier.assigned` (per model_id, per tier).

**Gauges:** `signals.tier.cache_size` (number of models with cached tier assignments), `signals.tier.current` (per agent_id, current tier as enum ordinal: 0=quantitative, 1=qualitative, 2=directional).

**Histograms:** `signals.format.duration_us` (time to format a single message, per tier), `signals.probe.duration_ms` (time to run the capability probe, per model_id).

---

## 10. Testing Strategy

### 10.1 Unit Tests

**Label mapping boundary tests:** For each threshold list, verify that values exactly at the boundary map to the correct label. Test: 0.0, each threshold value, midpoints between thresholds, and 1.0. Verify that out-of-range values (negative, >1.0) map gracefully.

**Normalization tests:** Verify that price normalization maps the floor to 0.0 and the ceiling to 1.0. Verify that prices below the floor clamp to 0.0 and above the ceiling clamp to 1.0. Verify budget normalization with zero, negative, and very large budgets.

**Quantitative formatting tests:** Format a pricing update with known inputs, verify the output contains exact numeric values. Format a confirmation request, verify all fields are present and correctly formatted.

**Qualitative formatting tests:** Format a pricing update with known inputs, verify the output contains the correct labels (not numbers). Verify that a high price maps to "high" or "very high" label in the output.

**Directional formatting tests:** Verify that each resource state combination (both cheap, both expensive, mixed, budget low) produces the correct nudge. Verify that the neutral state produces an empty string.

**Alternative suggestion tests:** Verify that no suggestion is generated when the tool is cheaper than reasoning. Verify that a suggestion is generated when the ratio exceeds `min_ratio`. Verify tier-appropriate phrasing.

**Score-to-tier mapping tests:** Verify that scores 0, 1 → directional, scores 2, 3 → qualitative, scores 4, 5 → quantitative (with default thresholds). Test with custom thresholds.

### 10.2 Integration Tests

**Tier assignment with mock probe:** Create a mock probe runner that returns deterministic scores. Verify that `TierAssigner.get_tier()` returns the correct tier. Verify caching: second call does not run the probe.

**Cache expiry:** Set TTL to 0 seconds, verify that every `get_tier()` call runs the probe. Set TTL to a large value, verify the probe runs only once.

**Manual override:** Set a tier override in configuration. Verify that `get_tier()` returns the override without running the probe. Verify that overrides never expire.

**Full pipeline:** Wire the formatter to a mock pricing engine and budget manager. Call `format_pricing_update()` for each tier, verify the output is well-formed and non-empty.

### 10.3 Property-Based Tests

Use Hypothesis to generate random combinations of prices (Decimal in [0, 100]), utilizations (float in [0, 1]), and budgets (Decimal in [0, 1000]). For each tier, verify that `format_pricing_update()` returns a non-empty string and does not raise an exception.

Generate random probe scores (int in [0, 5]) and verify that `_score_to_tier()` always returns a valid `SignalTier`.

Generate random threshold lists (sorted floats with string labels) and verify that `QualitativeLabelMapper._lookup()` always returns a string and never raises.

---

## 11. Dependencies

**Internal:** `fam/types.py` (shared types), `fam/config/schema.py` (configuration schema), `fam/eval/probe.py` (spec 15, capability probe runner — optional dependency, only imported if probing is enabled).

**External:** `decimal` (standard library), `dataclasses` (standard library), `enum` (standard library), `time` (standard library), `json` (standard library, for cache persistence). No third-party dependencies. The formatter is a pure computation module with no I/O except optional file-based cache persistence.

**Interface contracts:** The formatter depends on the pricing engine (spec 02) and budget manager (spec 03) for input data, but only through the values passed to its format methods — it does not import or reference those modules directly. The tier assigner depends on the probe runner (spec 15) through the `ProbeRunner` protocol.

---

## 12. Open Questions

**Dynamic tier adjustment.** Should the tier assignment change during a session based on observed agent behavior? For example, if a quantitative-tier agent consistently makes budget-irrational decisions (approving tool calls when budget is nearly exhausted), should the system downgrade it to qualitative? This would require a feedback loop from the confirmation handler to the tier assigner, adding complexity. The initial implementation uses static tier assignment per model.

**Per-tool tier formatting.** The current design applies the same tier to all messages for an agent. Should the system support per-message-type tier selection (e.g., quantitative for confirmation requests but directional for pricing updates)? This could reduce context overhead for agents that only need detailed information at decision points. Deferred for simplicity.

**Template customization by users.** Should users be able to provide custom message templates (e.g., via Jinja2 template files)? This would allow domain-specific formatting but adds a template language dependency and a template debugging burden. The initial implementation uses hardcoded Python f-string templates. If customization proves necessary, the recommended approach is to subclass `TieredSignalFormatter` and override the format methods.

**Tier gradation.** Are three tiers sufficient, or should there be a finer gradation (e.g., five tiers)? The current three-tier system is a pragmatic starting point. Adding more tiers increases the number of templates to maintain and the complexity of the probe. If evidence shows that certain models fall between tiers, additional tiers can be added without changing the architecture — just add enum values, threshold ranges, and templates.
