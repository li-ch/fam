# Spec 06 — Confirmation Handler

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview), Spec 02 (Pricing Engine), Spec 03 (Budget Manager), Spec 04 (Agent Communication Protocol), Spec 05 (Tiered Signal Formatter)
**Consumed By:** Spec 07 (Tool Dispatch Queue), Spec 08 (Speculative Continuation Manager), Spec 09 (Framework Adapters), Spec 13 (Metrics & Observability)

---

## 1. Purpose

The confirmation handler is FAM's central decision gate for tool calls. Every tool call that an agent attempts passes through this module, and the confirmation handler decides which of three paths it takes: immediate dispatch (auto-approve), interactive confirmation (ask the agent), or rejection (insufficient budget).

The core insight of the confirmation handler is that confirmation is not always desirable. When resources are cheap and plentiful, requiring confirmation adds latency with no benefit — the agent almost certainly wants to proceed, and making it say "yes" wastes an LLM turn. Confirmation becomes valuable only when resources are scarce enough that the agent should genuinely consider alternatives. The confirmation handler implements this insight through a congestion threshold: below the threshold, tool calls are dispatched without asking; above the threshold, the agent is consulted.

This spec defines the trigger conditions for confirmation, the complete confirmation flow from tool call interception to dispatch or cancellation, timeout behavior, budget sufficiency enforcement, and the metrics emitted by the handler.

---

## 2. Module Location

```
fam/
├── confirmation/
│   ├── __init__.py
│   ├── handler.py          # ConfirmationHandler class — main entry point
│   ├── evaluator.py        # Trigger evaluation logic (should we confirm?)
│   └── timeout.py          # Timeout policy implementation
```

Tests live in `tests/test_confirmation/`.

---

## 3. Confirmation Trigger Conditions

The confirmation handler evaluates four conditions for every intercepted tool call. The evaluation is fast (no I/O, no LLM calls) — it reads current prices and budget from in-memory state.

### 3.1 Decision Tree

```
Tool call intercepted
        │
        ▼
┌──────────────────────┐
│ 1. Budget sufficient? │
│    balance >= cost     │──── NO ──▶ REJECT (insufficient budget)
└──────────┬───────────┘
           │ YES
           ▼
┌──────────────────────┐
│ 2. Agent force-       │
│    confirm enabled?   │──── YES ─▶ CONFIRM (always ask this agent)
└──────────┬───────────┘
           │ NO
           ▼
┌──────────────────────┐
│ 3. Tool price above   │
│    congestion          │──── NO ──▶ AUTO-APPROVE (dispatch immediately)
│    threshold?          │
└──────────┬───────────┘
           │ YES
           ▼
┌──────────────────────┐
│ 4. Cost exceeds       │
│    budget fraction     │──── NO ──▶ AUTO-APPROVE (cost is trivial
│    threshold?          │           relative to budget)
└──────────┬───────────┘
           │ YES
           ▼
      CONFIRM (ask agent)
```

### 3.2 Trigger Evaluation

```python
class ConfirmationDecisionType(str, Enum):
    AUTO_APPROVE = "auto_approve"
    CONFIRM = "confirm"
    REJECT = "reject"

@dataclass(frozen=True)
class TriggerEvaluation:
    """Result of evaluating whether confirmation is needed."""
    decision: ConfirmationDecisionType
    reason: str
    cost: Decimal
    budget_remaining: Decimal
    tool_price: Decimal
    congestion_threshold: Decimal
    budget_fraction_threshold: float

class ConfirmationEvaluator:
    """Determines whether a tool call requires confirmation."""

    def __init__(self, config: ConfirmationConfig) -> None:
        self._config = config

    def evaluate(
        self,
        agent_id: str,
        endpoint_id: str,
        tool_price: Decimal,
        budget_remaining: Decimal,
        agent_force_confirm: bool = False,
    ) -> TriggerEvaluation:
        cost = tool_price

        if budget_remaining < cost:
            return TriggerEvaluation(
                decision=ConfirmationDecisionType.REJECT,
                reason="insufficient_budget",
                cost=cost,
                budget_remaining=budget_remaining,
                tool_price=tool_price,
                congestion_threshold=self._config.congestion_threshold,
                budget_fraction_threshold=self._config.budget_fraction_threshold,
            )

        if agent_force_confirm:
            return TriggerEvaluation(
                decision=ConfirmationDecisionType.CONFIRM,
                reason="agent_force_confirm",
                cost=cost,
                budget_remaining=budget_remaining,
                tool_price=tool_price,
                congestion_threshold=self._config.congestion_threshold,
                budget_fraction_threshold=self._config.budget_fraction_threshold,
            )

        if tool_price <= self._config.congestion_threshold:
            return TriggerEvaluation(
                decision=ConfirmationDecisionType.AUTO_APPROVE,
                reason="below_congestion_threshold",
                cost=cost,
                budget_remaining=budget_remaining,
                tool_price=tool_price,
                congestion_threshold=self._config.congestion_threshold,
                budget_fraction_threshold=self._config.budget_fraction_threshold,
            )

        budget_fraction = float(cost / budget_remaining) if budget_remaining > 0 else 1.0
        if budget_fraction < self._config.budget_fraction_threshold:
            return TriggerEvaluation(
                decision=ConfirmationDecisionType.AUTO_APPROVE,
                reason="cost_trivial_relative_to_budget",
                cost=cost,
                budget_remaining=budget_remaining,
                tool_price=tool_price,
                congestion_threshold=self._config.congestion_threshold,
                budget_fraction_threshold=self._config.budget_fraction_threshold,
            )

        return TriggerEvaluation(
            decision=ConfirmationDecisionType.CONFIRM,
            reason="above_congestion_threshold",
            cost=cost,
            budget_remaining=budget_remaining,
            tool_price=tool_price,
            congestion_threshold=self._config.congestion_threshold,
            budget_fraction_threshold=self._config.budget_fraction_threshold,
        )
```

### 3.3 Congestion Threshold

The congestion threshold is a price value (not a utilization value). It represents the price level above which tool calls are considered "expensive enough to ask about." The default is computed as a fraction of the price range:

```
congestion_threshold = price_floor + congestion_fraction × (price_ceiling - price_floor)
```

With default `congestion_fraction = 0.5`, `price_floor = 0.01`, `price_ceiling = 10.0`, the threshold is `0.01 + 0.5 × 9.99 = 5.005`. This means confirmation is triggered only when tool prices have risen to the midpoint of the price range — roughly corresponding to moderate-to-high utilization. Below this level, tool calls pass through without confirmation.

The congestion threshold can also be set as an absolute value, overriding the fraction-based computation. This is useful when the price range is reconfigured and the operator wants a specific threshold.

### 3.4 Budget Fraction Threshold

Even when the price is above the congestion threshold, a tool call may be auto-approved if it represents a trivial fraction of the agent's budget. The budget fraction threshold (default: 0.05, i.e., 5%) controls this. If the tool cost is less than 5% of the agent's remaining budget, the agent is unlikely to care about the cost, and confirmation is skipped.

This prevents annoying confirmation prompts for wealthy agents in congested conditions. A newly initialized agent with 100 units of budget will auto-approve a 4-unit tool call even if the congestion threshold is 3 units, because 4/100 = 4% < 5%.

---

## 4. Confirmation Flow

When the evaluator returns `CONFIRM`, the confirmation handler orchestrates the full interactive flow.

### 4.1 Flow Implementation

```python
class ConfirmationHandler:
    """Orchestrates the tool call confirmation flow."""

    def __init__(
        self,
        config: ConfirmationConfig,
        signal_formatter: TieredSignalFormatter,
        protocol: AgentCommunicationProtocol,
        budget_manager: BudgetManager,
        tier_assigner: TierAssigner,
        metrics: MetricsCollector,
    ) -> None:
        self._config = config
        self._formatter = signal_formatter
        self._protocol = protocol
        self._budget = budget_manager
        self._tiers = tier_assigner
        self._metrics = metrics
        self._evaluator = ConfirmationEvaluator(config)
        self._pending: dict[str, PendingConfirmation] = {}

    async def handle_tool_call(
        self,
        agent_id: str,
        tool_name: str,
        tool_call_id: str,
        endpoint_id: str,
        tool_args: dict[str, Any],
        tool_price: Decimal,
    ) -> ConfirmationOutcome:
        """Process a tool call through the confirmation flow.

        Returns a ConfirmationOutcome indicating the result:
        DISPATCH, CANCEL, DEFER, or REJECT.
        """
        budget_remaining = self._budget.get_balance(agent_id)
        force_confirm = self._config.force_confirm_agents.get(agent_id, False)

        evaluation = self._evaluator.evaluate(
            agent_id=agent_id,
            endpoint_id=endpoint_id,
            tool_price=tool_price,
            budget_remaining=budget_remaining,
            agent_force_confirm=force_confirm,
        )

        self._metrics.record_trigger_evaluation(agent_id, evaluation)

        if evaluation.decision == ConfirmationDecisionType.REJECT:
            return await self._handle_reject(agent_id, tool_call_id, evaluation)

        if evaluation.decision == ConfirmationDecisionType.AUTO_APPROVE:
            return await self._handle_auto_approve(
                agent_id, tool_call_id, endpoint_id, tool_price,
            )

        return await self._handle_confirm(
            agent_id, tool_name, tool_call_id, endpoint_id,
            tool_args, tool_price, budget_remaining,
        )
```

### 4.2 Auto-Approve Path

```python
async def _handle_auto_approve(
    self,
    agent_id: str,
    tool_call_id: str,
    endpoint_id: str,
    tool_price: Decimal,
) -> ConfirmationOutcome:
    """Fast path: dispatch without asking."""
    success = self._budget.deduct(agent_id, tool_price, reason="tool_call_auto")
    if not success:
        return ConfirmationOutcome(
            tool_call_id=tool_call_id,
            decision=OutcomeType.REJECT,
            reason="budget_deduction_failed_race",
        )

    self._metrics.record_auto_approve(agent_id, endpoint_id, tool_price)
    return ConfirmationOutcome(
        tool_call_id=tool_call_id,
        decision=OutcomeType.DISPATCH,
        reason="auto_approved",
        cost_charged=tool_price,
    )
```

The budget deduction happens atomically before dispatch. If another concurrent tool call exhausted the budget between the evaluation check and the deduction attempt, the deduction fails and the call is rejected. This race is rare (requires exact budget exhaustion between two non-yielding operations in asyncio) but must be handled.

### 4.3 Interactive Confirmation Path

```python
async def _handle_confirm(
    self,
    agent_id: str,
    tool_name: str,
    tool_call_id: str,
    endpoint_id: str,
    tool_args: dict[str, Any],
    tool_price: Decimal,
    budget_remaining: Decimal,
) -> ConfirmationOutcome:
    """Slow path: ask the agent for confirmation."""
    tier = await self._tiers.get_tier(
        self._get_model_id(agent_id)
    )

    budget_after = budget_remaining - tool_price
    replenishment_rate = self._budget.get_replenishment_rate(agent_id)
    replenishment_eta = float(tool_price / replenishment_rate) if replenishment_rate > 0 else float("inf")
    estimated_wait = self._estimate_wait_time(endpoint_id)
    queue_depth = self._get_queue_depth(endpoint_id)
    reasoning_price = self._get_current_reasoning_price()

    confirmation_text = self._formatter.format_confirmation_request(
        tier=tier,
        tool_name=tool_name,
        endpoint_id=endpoint_id,
        cost=tool_price,
        budget_remaining=budget_remaining,
        budget_after_deduction=budget_after,
        estimated_wait_time_seconds=estimated_wait,
        queue_depth=queue_depth,
        replenishment_rate=replenishment_rate,
        replenishment_eta_seconds=replenishment_eta,
        reasoning_price=reasoning_price,
    )

    self._protocol.inject_confirmation_request(
        agent_id=agent_id,
        tool_call_id=tool_call_id,
        confirmation_text=confirmation_text,
    )

    pending = PendingConfirmation(
        tool_call_id=tool_call_id,
        agent_id=agent_id,
        endpoint_id=endpoint_id,
        tool_price=tool_price,
        created_at=time.monotonic(),
    )
    self._pending[tool_call_id] = pending

    response = await self._wait_for_response(agent_id, tool_call_id)

    del self._pending[tool_call_id]

    return await self._process_response(
        agent_id, tool_call_id, endpoint_id, tool_price, response,
    )
```

### 4.4 Response Processing

```python
async def _process_response(
    self,
    agent_id: str,
    tool_call_id: str,
    endpoint_id: str,
    tool_price: Decimal,
    response: ConfirmationResponseMessage,
) -> ConfirmationOutcome:
    """Route the agent's decision to the appropriate handler."""
    self._metrics.record_confirmation_decision(
        agent_id, response.decision, response.confidence,
    )

    if response.decision == ConfirmationDecision.CONFIRM:
        success = self._budget.deduct(agent_id, tool_price, reason="tool_call_confirmed")
        if not success:
            self._protocol.inject_budget_insufficient(agent_id, tool_call_id)
            return ConfirmationOutcome(
                tool_call_id=tool_call_id,
                decision=OutcomeType.REJECT,
                reason="budget_exhausted_during_confirmation",
            )
        self._metrics.record_confirm(agent_id, endpoint_id, tool_price)
        return ConfirmationOutcome(
            tool_call_id=tool_call_id,
            decision=OutcomeType.DISPATCH,
            reason="agent_confirmed",
            cost_charged=tool_price,
        )

    if response.decision == ConfirmationDecision.CANCEL:
        self._metrics.record_cancel(agent_id, endpoint_id, tool_price)
        self._protocol.inject_cancellation_ack(agent_id, tool_call_id)
        return ConfirmationOutcome(
            tool_call_id=tool_call_id,
            decision=OutcomeType.CANCEL,
            reason="agent_cancelled",
        )

    if response.decision == ConfirmationDecision.REASON_MORE:
        self._metrics.record_defer(agent_id, endpoint_id, tool_price)
        return ConfirmationOutcome(
            tool_call_id=tool_call_id,
            decision=OutcomeType.DEFER,
            reason="agent_chose_reasoning",
        )

    return ConfirmationOutcome(
        tool_call_id=tool_call_id,
        decision=OutcomeType.CANCEL,
        reason="unknown_decision",
    )
```

---

## 5. Timeout Behavior

If the agent does not respond to a confirmation request within a configured time or token limit, the system cannot wait indefinitely. The timeout policy determines what happens.

### 5.1 Timeout Triggers

A timeout fires when either condition is met:

- **Time-based:** `timeout_seconds` (default: 30.0) have elapsed since the confirmation request was injected. This handles the case where the agent is stuck or the LLM is experiencing high latency.

- **Token-based:** The agent has generated `timeout_max_tokens` (default: 500) tokens since the confirmation request without producing a parseable decision. This handles the case where the agent is generating text that does not address the confirmation (e.g., continuing its previous reasoning chain, ignoring the confirmation request).

The token-based trigger requires cooperation from the framework adapter, which must count tokens generated since the confirmation injection point.

### 5.2 Timeout Policy

```python
class TimeoutPolicy(str, Enum):
    AUTO_CONFIRM = "auto_confirm"
    AUTO_CANCEL = "auto_cancel"

@dataclass(frozen=True)
class TimeoutConfig:
    timeout_seconds: float = 30.0
    timeout_max_tokens: int = 500
    policy: TimeoutPolicy = TimeoutPolicy.AUTO_CONFIRM
```

**`AUTO_CONFIRM` (default):** The tool call is treated as confirmed. Budget is deducted and the call is dispatched. Rationale: the agent requested the tool call in the first place, so the most likely intent is to proceed. Timeouts are more often caused by the agent not understanding the confirmation prompt than by the agent wanting to cancel.

**`AUTO_CANCEL`:** The tool call is cancelled. No budget is deducted. The agent is notified that its tool call was cancelled due to timeout. Rationale: if the agent could not explicitly confirm, the operator prefers the conservative path. This is appropriate in cost-sensitive deployments where accidental charges are worse than missed tool calls.

### 5.3 Timeout Implementation

```python
class ConfirmationTimeoutManager:
    """Manages timeout tracking for pending confirmations."""

    def __init__(self, config: TimeoutConfig) -> None:
        self._config = config
        self._pending: dict[str, PendingTimeout] = {}

    def start_timer(self, tool_call_id: str) -> None:
        self._pending[tool_call_id] = PendingTimeout(
            started_at=time.monotonic(),
            tokens_since_start=0,
        )

    def record_tokens(self, tool_call_id: str, token_count: int) -> None:
        if tool_call_id in self._pending:
            self._pending[tool_call_id].tokens_since_start += token_count

    def is_timed_out(self, tool_call_id: str) -> bool:
        pending = self._pending.get(tool_call_id)
        if pending is None:
            return False

        elapsed = time.monotonic() - pending.started_at
        if elapsed >= self._config.timeout_seconds:
            return True

        if pending.tokens_since_start >= self._config.timeout_max_tokens:
            return True

        return False

    def clear(self, tool_call_id: str) -> None:
        self._pending.pop(tool_call_id, None)

    def get_timeout_policy(self) -> TimeoutPolicy:
        return self._config.policy


@dataclass
class PendingTimeout:
    started_at: float
    tokens_since_start: int
```

---

## 6. Rejection Handling

When a tool call is rejected due to insufficient budget, the handler injects a rejection message into the agent's conversation explaining why and what the agent can do about it.

```python
async def _handle_reject(
    self,
    agent_id: str,
    tool_call_id: str,
    evaluation: TriggerEvaluation,
) -> ConfirmationOutcome:
    """Handle budget-insufficient rejection."""
    tier = await self._tiers.get_tier(self._get_model_id(agent_id))

    shortfall = evaluation.cost - evaluation.budget_remaining
    replenishment_rate = self._budget.get_replenishment_rate(agent_id)
    if replenishment_rate > 0:
        wait_for_budget = float(shortfall / replenishment_rate)
    else:
        wait_for_budget = float("inf")

    rejection_text = self._formatter.format_budget_warning(
        tier=tier,
        budget_remaining=evaluation.budget_remaining,
        warning_threshold=evaluation.cost,
        replenishment_rate=replenishment_rate,
        estimated_exhaustion_seconds=wait_for_budget,
    )

    self._protocol.inject_rejection(
        agent_id=agent_id,
        tool_call_id=tool_call_id,
        rejection_text=rejection_text,
    )

    self._metrics.record_reject(
        agent_id, evaluation.cost, evaluation.budget_remaining,
    )

    return ConfirmationOutcome(
        tool_call_id=tool_call_id,
        decision=OutcomeType.REJECT,
        reason="insufficient_budget",
        shortfall=shortfall,
        estimated_wait_for_budget=wait_for_budget,
    )
```

---

## 7. Outcome Dataclass

Every tool call through the confirmation handler produces an outcome.

```python
class OutcomeType(str, Enum):
    DISPATCH = "dispatch"
    CANCEL = "cancel"
    DEFER = "defer"
    REJECT = "reject"

@dataclass(frozen=True)
class ConfirmationOutcome:
    tool_call_id: str
    decision: OutcomeType
    reason: str
    cost_charged: Decimal | None = None
    shortfall: Decimal | None = None
    estimated_wait_for_budget: float | None = None
```

The `reason` field provides machine-readable context for debugging and metrics. Possible values: `"auto_approved"`, `"agent_confirmed"`, `"agent_cancelled"`, `"agent_chose_reasoning"`, `"insufficient_budget"`, `"budget_exhausted_during_confirmation"`, `"budget_deduction_failed_race"`, `"timeout_auto_confirm"`, `"timeout_auto_cancel"`, `"unknown_decision"`.

---

## 8. Agent Decision Quality Tracking

The confirmation handler tracks per-agent decision quality to support observability and future tier reassessment.

### 8.1 Decision History

For each agent, the handler maintains a bounded history of recent confirmation decisions.

```python
@dataclass
class AgentDecisionRecord:
    tool_call_id: str
    endpoint_id: str
    tool_price: Decimal
    budget_at_decision: Decimal
    decision: ConfirmationDecision | None   # None for auto-approve and reject
    outcome: OutcomeType
    confidence: float                        # Parser confidence (0 for auto/reject)
    timestamp: float

class AgentDecisionTracker:
    """Tracks confirmation decision history per agent."""

    def __init__(self, max_history: int = 100) -> None:
        self._history: dict[str, deque[AgentDecisionRecord]] = {}
        self._max_history = max_history

    def record(self, agent_id: str, record: AgentDecisionRecord) -> None:
        if agent_id not in self._history:
            self._history[agent_id] = deque(maxlen=self._max_history)
        self._history[agent_id].append(record)

    def get_stats(self, agent_id: str) -> AgentDecisionStats:
        """Compute summary statistics for an agent's decisions."""
        history = self._history.get(agent_id, deque())
        if not history:
            return AgentDecisionStats.empty()

        confirmations = [r for r in history if r.outcome == OutcomeType.DISPATCH and r.decision == ConfirmationDecision.CONFIRM]
        cancellations = [r for r in history if r.outcome == OutcomeType.CANCEL]
        deferrals = [r for r in history if r.outcome == OutcomeType.DEFER]
        auto_approves = [r for r in history if r.decision is None and r.outcome == OutcomeType.DISPATCH]

        total_interactive = len(confirmations) + len(cancellations) + len(deferrals)
        confirm_rate = len(confirmations) / total_interactive if total_interactive > 0 else 0.0
        cancel_rate = len(cancellations) / total_interactive if total_interactive > 0 else 0.0
        defer_rate = len(deferrals) / total_interactive if total_interactive > 0 else 0.0

        avg_confidence = (
            sum(r.confidence for r in history if r.confidence > 0) /
            max(sum(1 for r in history if r.confidence > 0), 1)
        )

        return AgentDecisionStats(
            total_decisions=len(history),
            auto_approve_count=len(auto_approves),
            confirm_count=len(confirmations),
            cancel_count=len(cancellations),
            defer_count=len(deferrals),
            confirm_rate=confirm_rate,
            cancel_rate=cancel_rate,
            defer_rate=defer_rate,
            avg_confidence=avg_confidence,
        )


@dataclass(frozen=True)
class AgentDecisionStats:
    total_decisions: int
    auto_approve_count: int
    confirm_count: int
    cancel_count: int
    defer_count: int
    confirm_rate: float
    cancel_rate: float
    defer_rate: float
    avg_confidence: float

    @classmethod
    def empty(cls) -> "AgentDecisionStats":
        return cls(0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0)
```

### 8.2 Decision Quality Signals

The decision tracker exposes several quality signals that the metrics system and (potentially) the tier reassignment logic can use:

- **High cancel rate:** If an agent cancels more than 50% of confirmation requests, it may be struggling with the pricing information. The signal formatter might benefit from a tier downgrade.
- **Low confidence:** If the parser consistently reports low confidence for an agent's responses, the agent may not understand the confirmation format. A simplified prompt or tier downgrade may help.
- **Budget-irrational confirmations:** If an agent confirms tool calls when its budget is very low (below 10% of initial), it may not be tracking its budget effectively. This is tracked but not acted upon in the initial implementation.

---

## 9. Configuration

All configuration for the confirmation handler is namespaced under `confirmation` in the global FAM configuration (spec 12).

```yaml
confirmation:
  # Congestion threshold
  congestion:
    mode: "fraction"                        # "fraction" or "absolute"
    fraction: 0.5                           # Used when mode is "fraction"
    absolute_threshold: null                # Used when mode is "absolute"
    price_floor: 0.01                       # For fraction computation
    price_ceiling: 10.0                     # For fraction computation

  # Budget fraction threshold
  budget_fraction_threshold: 0.05           # Auto-approve if cost < 5% of budget

  # Per-agent force-confirm (optional)
  force_confirm_agents: {}
    # Example:
    # "agent_debug_1": true

  # Timeout
  timeout:
    timeout_seconds: 30.0
    timeout_max_tokens: 500
    policy: "auto_confirm"                  # "auto_confirm" or "auto_cancel"

  # Decision tracking
  decision_tracking:
    enabled: true
    max_history_per_agent: 100

  # Low-confidence handling (from spec 04 parser config)
  low_confidence_policy: "use_default"      # "use_default", "cancel", or "retry"
  max_retry_count: 1
```

---

## 10. Error Handling

The confirmation handler is designed to always produce an outcome, never leave a tool call in limbo, and never crash the agent's graph execution.

**Budget manager unavailable:** If the budget manager raises an exception during balance check or deduction, the handler treats the call as rejected with reason `"budget_service_error"`. The error is logged at ERROR level. The agent is notified with a generic error message.

**Signal formatter failure:** If the formatter raises an exception while rendering the confirmation request, the handler falls back to a minimal plain-text confirmation: "Tool call costs {cost} units. Budget: {balance} units. Reply CONFIRM, CANCEL, or REASON." This bypasses tiered formatting entirely but preserves the confirmation flow.

**Protocol injection failure:** If message injection fails (e.g., LangGraph state corruption), the handler applies the timeout policy immediately — it acts as if the agent timed out. This ensures the tool call is not left pending indefinitely.

**Concurrent tool calls by same agent:** An agent may have multiple tool calls in the confirmation flow simultaneously (if the agent's graph uses parallel tool calling). The handler supports this — each tool call has an independent `tool_call_id` and independent state. Budget deductions are atomic, so if two concurrent tool calls both try to deduct and the budget is insufficient for both, exactly one succeeds and the other is rejected.

**Handler restart:** If the handler is restarted (e.g., due to an unrecoverable error), all pending confirmations are lost. The corresponding agents experience a timeout, and the timeout policy is applied. This is acceptable because pending confirmations are short-lived (bounded by `timeout_seconds`).

---

## 11. Metrics Emitted

The confirmation handler emits the following metrics to the observability system (spec 13):

**Counters:**
- `confirmation.evaluations.total` (per agent_id, per decision_type: auto_approve, confirm, reject)
- `confirmation.outcomes.total` (per agent_id, per outcome_type: dispatch, cancel, defer, reject)
- `confirmation.timeouts.total` (per agent_id, per policy: auto_confirm, auto_cancel)
- `confirmation.rejections.total` (per agent_id, per reason: insufficient_budget, budget_service_error)
- `confirmation.retries.total` (per agent_id — low-confidence response retries)

**Gauges:**
- `confirmation.pending.count` (total pending confirmations across all agents)
- `confirmation.pending.oldest_seconds` (age of the oldest pending confirmation)

**Histograms:**
- `confirmation.latency_ms` (time from tool call interception to outcome, per outcome_type)
- `confirmation.cost_at_confirm` (tool price when agent confirmed, per endpoint_id)
- `confirmation.cost_at_cancel` (tool price when agent cancelled, per endpoint_id)
- `confirmation.budget_fraction_at_confirm` (cost/budget ratio when agent confirmed)
- `confirmation.parse_confidence` (per agent_id — parser confidence distribution)

**Per-Agent Aggregates (computed from decision tracker):**
- `confirmation.agent.confirm_rate` (per agent_id)
- `confirmation.agent.cancel_rate` (per agent_id)
- `confirmation.agent.defer_rate` (per agent_id)
- `confirmation.agent.avg_confidence` (per agent_id)

These metrics enable key analyses:

1. **Confirmation overhead:** Compare `confirmation.latency_ms` for `auto_approve` vs. `confirm` outcomes to measure the latency cost of interactive confirmation.
2. **Threshold tuning:** Plot `confirmation.cost_at_confirm` vs. `confirmation.cost_at_cancel` to find the price level where agents switch from confirming to cancelling — this is the "effective" congestion threshold from the agent's perspective.
3. **Agent rationality:** Compare `confirmation.agent.confirm_rate` across agents to identify agents that always confirm (may benefit from tier adjustment) or always cancel (may not need the tools they're calling).

---

## 12. Testing Strategy

### 12.1 Unit Tests

**Trigger evaluation:** Test the `ConfirmationEvaluator` with all combinations: budget sufficient below threshold → auto-approve; budget sufficient above threshold → confirm; budget insufficient → reject; force-confirm enabled → always confirm. Verify boundary conditions: cost exactly equals budget, cost exactly equals threshold.

**Budget fraction bypass:** Set budget fraction threshold to 0.05. Create an agent with budget 100, tool price 4 (4% of budget). Verify auto-approve even though price is above congestion threshold. Set tool price to 6 (6% of budget). Verify confirmation is triggered.

**Timeout detection:** Create a `ConfirmationTimeoutManager`, start a timer, advance time past `timeout_seconds`, verify `is_timed_out()` returns True. Reset, add tokens past `timeout_max_tokens`, verify timeout. Verify that clearing the timer prevents timeout.

**Outcome construction:** Verify that all `ConfirmationOutcome` instances are correctly constructed for each path (auto-approve, confirm, cancel, defer, reject, timeout).

**Decision tracking stats:** Feed a known sequence of decisions to `AgentDecisionTracker`. Verify that `get_stats()` returns correct counts and rates. Test with empty history. Test with all same decision type. Test with exactly `max_history` entries.

### 12.2 Integration Tests

**Full confirmation flow (auto-approve):** Set congestion threshold high. Submit a tool call. Verify it is auto-approved without agent interaction. Verify budget is deducted. Verify metrics are emitted.

**Full confirmation flow (interactive):** Set congestion threshold low. Submit a tool call. Verify a confirmation request is injected. Simulate an agent "CONFIRM" response. Verify budget is deducted and outcome is DISPATCH.

**Full confirmation flow (cancel):** Same setup, simulate "CANCEL" response. Verify no budget deduction. Verify outcome is CANCEL.

**Full confirmation flow (defer):** Same setup, simulate "REASON" response. Verify outcome is DEFER. Verify no budget deduction.

**Timeout flow:** Set timeout to 0.1 seconds. Submit a tool call above threshold. Do not provide a response. Verify timeout fires and the configured policy is applied.

**Concurrent tool calls:** Submit two tool calls for the same agent simultaneously. Verify both enter the confirmation flow independently. Set budget to cover exactly one. Confirm both. Verify one succeeds and one is rejected.

**Budget race condition:** Set budget to exactly the tool price. Submit a tool call. Between evaluation (budget sufficient) and deduction, set budget to zero (simulating concurrent deduction). Verify the deduction fails gracefully and the call is rejected.

### 12.3 Property-Based Tests

Use Hypothesis to generate random combinations of tool_price (Decimal in [0, 100]), budget_remaining (Decimal in [0, 200]), congestion_threshold (Decimal in [0, 50]), and budget_fraction_threshold (float in [0, 1]). Verify that the evaluator always returns a valid `ConfirmationDecisionType` and that the following invariants hold:

1. If `budget_remaining < cost`, outcome is REJECT regardless of other parameters.
2. If `tool_price <= congestion_threshold` and `budget_remaining >= cost`, outcome is AUTO_APPROVE.
3. The evaluator never raises an exception.

---

## 13. Dependencies

**Internal:** `fam/types.py` (shared types), `fam/signals/formatter.py` (spec 05, tiered signal formatting), `fam/signals/protocol.py` (spec 04, agent communication protocol), `fam/budget/manager.py` (spec 03, budget manager), `fam/signals/tiers.py` (spec 05, tier assigner), `fam/config/schema.py` (configuration schema), `fam/metrics/collector.py` (spec 13, metrics).

**External:** `decimal` (standard library), `dataclasses` (standard library), `enum` (standard library), `time` (standard library), `collections.deque` (standard library). No third-party dependencies. The confirmation handler is a pure orchestration module — it coordinates between other FAM modules but has no external I/O of its own.

**Interface contracts:** The handler depends on the budget manager's `get_balance()`, `deduct()`, and `get_replenishment_rate()` methods. It depends on the signal formatter's `format_confirmation_request()` and `format_budget_warning()` methods. It depends on the communication protocol's injection methods. All of these are intra-FAM dependencies — no external service calls.

---

## 14. Open Questions

**Adaptive congestion threshold.** Should the congestion threshold adjust dynamically based on observed agent behavior? For example, if agents confirm 99% of requests at the current threshold, the threshold could be raised to reduce unnecessary confirmations. This creates a feedback loop that could be unstable (raising the threshold → fewer confirmations → agents spend more → higher prices → more confirmations → lower threshold → ...). The initial implementation uses a static threshold. Dynamic adjustment is a research question for the evaluation harness.

**Confirmation batching.** If an agent issues multiple tool calls in a single LLM turn (e.g., via parallel tool calling in function-calling-capable models), should the confirmation handler batch them into a single confirmation request ("These 3 tool calls will cost 12.5 units total. Confirm all?")? Batching reduces confirmation overhead but removes per-tool granularity. Deferred to a future version.

**Confirmation-free mode.** Should there be a global configuration to disable confirmation entirely (all tool calls auto-approve regardless of price)? This is useful as a baseline in evaluation experiments. Currently achievable by setting `congestion_threshold` to the price ceiling, but an explicit `enabled: false` flag would be clearer.

**Price staleness during confirmation.** The tool price used in the confirmation request is the price at the time of interception. By the time the agent responds (potentially many seconds later), the price may have changed. Should the handler re-check the price after receiving the response and adjust the charge? The current implementation charges the quoted price (what the agent agreed to), not the current price. This is analogous to a quoted price in a market order — once quoted, it is honored. However, this means the system may undercharge or overcharge relative to real-time conditions. Deferred pending analysis of how frequently prices change significantly within a confirmation window.
