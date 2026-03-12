# Spec 03 — Budget Manager

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview), Spec 02 (Pricing Engine)
**Consumed By:** Spec 06 (Confirmation Handler), Spec 08 (Speculative Continuation Manager), Spec 09 (Framework Adapters), Spec 13 (Metrics & Observability)

---

## 1. Purpose

The budget manager is FAM's enforcement arm. While the pricing engine (spec 02) computes what resources *cost*, the budget manager tracks what each agent can *afford*. Every agent in the system holds a budget object with a numeric balance that decreases when the agent consumes resources and increases through periodic replenishment. The budget is the ultimate backpressure mechanism — an agent that exhausts its budget cannot spend, regardless of whether it would be willing to pay the current price.

The budget manager handles four categories of resource metering: reasoning tokens (continuous, per-token deduction at the current reasoning price), tool calls (discrete, lump-sum deduction at the current tool price), speculative continuation tokens (per-token at reasoning price, flagged as speculative and potentially refundable), and replenishment (periodic credit at a configurable rate). It tracks cumulative spending by category, maintains a history of all balance mutations for observability, and exposes an API surface that the confirmation handler, speculative continuation manager, and framework adapters consume.

This spec defines the budget data model, the metering rules for each resource category, the replenishment mechanism, the exhaustion behavior (soft and hard limits), the complete API surface, and the concurrency model for budget mutations.

---

## 2. Module Location

```
fam/
├── budget/
│   ├── __init__.py
│   ├── manager.py         # BudgetManager class — owns all agent budgets
│   ├── ledger.py          # AgentBudget and BudgetLedgerEntry dataclasses
│   └── policy.py          # Exhaustion policies (soft limit, hard limit)
```

Tests live in `tests/test_budget/`.

---

## 3. Budget Data Model

### 3.1 Agent Budget

Each agent registered with FAM has exactly one `AgentBudget` instance. The budget is the single source of truth for the agent's financial state within the orchestrator.

```python
@dataclass
class AgentBudget:
    """Mutable per-agent budget state.

    All monetary values use Decimal for exact arithmetic.
    Mutations must go through BudgetManager methods to maintain invariants.
    """

    agent_id: str

    # Current balance — the amount available for spending
    balance: Decimal

    # Cumulative spending by category (monotonically increasing)
    cumulative_reasoning_spend: Decimal
    cumulative_tool_spend: Decimal
    cumulative_speculative_spend: Decimal

    # Cumulative replenishment received
    cumulative_replenished: Decimal

    # Configuration
    initial_balance: Decimal
    max_balance: Decimal              # Balance ceiling for replenishment
    replenishment_rate: Decimal       # Units per replenishment tick
    soft_limit: Decimal               # Balance below which warnings are issued
    hard_limit: Decimal               # Balance below which spending is blocked

    # State flags
    is_paused: bool                   # True when hard limit is hit
    warning_issued: bool              # True when soft limit warning has been sent

    # Timestamps
    created_at: float                 # time.monotonic() of budget creation
    last_deduction_at: float | None   # time.monotonic() of last deduction
    last_replenishment_at: float | None

    # Ledger — bounded history of balance mutations
    ledger: deque[BudgetLedgerEntry]
```

### 3.2 Budget Ledger Entry

Every mutation to an agent's balance is recorded as a ledger entry. The ledger provides a complete audit trail for debugging, observability, and the evaluation harness.

```python
@dataclass(frozen=True)
class BudgetLedgerEntry:
    """Immutable record of a single balance mutation."""

    timestamp: float                   # time.monotonic()
    entry_type: BudgetEntryType        # Category of mutation
    amount: Decimal                    # Positive = credit, negative = debit
    balance_before: Decimal            # Balance immediately before this mutation
    balance_after: Decimal             # Balance immediately after this mutation
    metadata: dict[str, Any]           # Context-specific metadata


class BudgetEntryType(str, Enum):
    INITIAL = "initial"                # Initial balance at creation
    REASONING_DEDUCTION = "reasoning"  # Per-token reasoning cost
    TOOL_DEDUCTION = "tool"            # Lump-sum tool call cost
    SPECULATIVE_DEDUCTION = "speculative"  # Speculative continuation tokens
    SPECULATIVE_REFUND = "speculative_refund"  # Refund for discarded speculation
    REPLENISHMENT = "replenishment"    # Periodic replenishment credit
    ADJUSTMENT = "adjustment"          # Manual or policy-driven adjustment
```

### 3.3 Metadata by Entry Type

Each entry type carries specific metadata:

**REASONING_DEDUCTION:** `{"token_count": int, "price_per_token": str, "generation_step": int}` — the number of tokens consumed, the reasoning price at the time, and the generation step (sequential counter within the agent's execution).

**TOOL_DEDUCTION:** `{"endpoint_id": str, "tool_name": str, "tool_price": str, "call_id": str}` — identifies which tool call caused the deduction, the price at dispatch time, and a unique call identifier for correlation.

**SPECULATIVE_DEDUCTION:** `{"token_count": int, "price_per_token": str, "pending_tool_call_id": str}` — same as reasoning, plus a reference to the deferred tool call that triggered speculative continuation.

**SPECULATIVE_REFUND:** `{"refund_amount": str, "pending_tool_call_id": str, "reason": str}` — the amount refunded, which deferred call it relates to, and why (e.g., "speculation_discarded", "tool_result_consistent").

**REPLENISHMENT:** `{"rate": str, "tick_number": int}` — the replenishment rate applied and the sequential tick counter.

---

## 4. Metering Rules

### 4.1 Reasoning Token Metering

Reasoning tokens are metered per-token at the current reasoning price. Because LLM inference generates tokens in a stream, the deduction is not applied per-token in real time (which would be impractically granular). Instead, reasoning cost is batched per generation step.

A **generation step** is one invocation of the LLM within the agent's LangGraph graph — one call to the model node that produces a response. Each step generates some number of tokens. At the end of the step, the framework adapter (spec 09) reports the token count to the budget manager, which computes the cost and deducts it.

```
reasoning_cost = token_count × reasoning_price_at_step_start
```

The reasoning price used is the price that was current when the generation step began, not when it ended. This is because the agent received a pricing signal at the start of the step and made its reasoning decisions based on that price. Charging a different price retroactively would undermine the agent's ability to reason about costs.

If the deduction would bring the balance below the hard limit, the deduction still proceeds (the generation has already happened — tokens cannot be un-generated). The budget is marked as paused, and the hard limit behavior (section 6) activates on the *next* action, not retroactively.

### 4.2 Tool Call Metering

Tool calls are metered as a lump-sum deduction at the current tool price for the target endpoint. The deduction occurs at the moment the tool call is **confirmed and dispatched**, not when the result returns. This is economically correct — the resource is consumed (the endpoint slot is occupied) at dispatch time.

```
tool_cost = tool_price[endpoint_id] at dispatch time
```

Unlike reasoning deductions, tool deductions are pre-validated. Before dispatching, the confirmation handler (spec 06) checks `budget.balance >= tool_cost`. If the balance is insufficient, the call is rejected and the agent is notified. This prevents negative balances from tool spending (though not from reasoning spending — see 4.1).

### 4.3 Speculative Continuation Metering

When an agent defers a tool call and enters speculative continuation (spec 08), the tokens it generates during speculation are metered at the current reasoning price, just like regular reasoning tokens. However, they are tracked separately as `cumulative_speculative_spend` and flagged with `BudgetEntryType.SPECULATIVE_DEDUCTION` in the ledger.

This separate tracking serves two purposes:

1. **Observability:** Operators can see how much budget is going to speculative reasoning vs. productive reasoning.
2. **Refunds:** If the speculative continuation is discarded (because the eventual tool result invalidates the provisional reasoning), the speculative spend *may* be refunded (partially or fully) depending on the configured refund policy.

```python
class SpeculativeRefundPolicy(str, Enum):
    NO_REFUND = "no_refund"            # Speculative tokens are never refunded
    FULL_REFUND_ON_DISCARD = "full"    # Full refund if speculation is discarded
    HALF_REFUND_ON_DISCARD = "half"    # 50% refund if speculation is discarded
```

The default is `NO_REFUND`. Speculation is a gamble — the agent chose to reason speculatively rather than wait, and the cost of that reasoning was real (GPU tokens were consumed). Refunding encourages reckless speculation. However, the option exists for experimental configurations where speculation should be encouraged.

### 4.4 Metering Atomicity

All balance mutations are atomic in the `asyncio` sense: the check-and-deduct sequence does not yield between reading the balance and writing the new balance. This prevents race conditions where two concurrent deductions could both read the same balance and both succeed, overdrawing the account.

```python
def _deduct(self, budget: AgentBudget, amount: Decimal, entry_type: BudgetEntryType, metadata: dict) -> bool:
    """Atomic balance deduction. Returns True if successful.

    For tool deductions: fails if balance < amount (pre-validated).
    For reasoning/speculative deductions: always succeeds (post-hoc).
    """
    # No await between balance read and write — atomic under asyncio
    balance_before = budget.balance
    new_balance = balance_before - amount

    if entry_type == BudgetEntryType.TOOL_DEDUCTION and new_balance < budget.hard_limit:
        return False

    budget.balance = new_balance
    # ... record ledger entry, update cumulative counters ...
    return True
```

---

## 5. Replenishment

Replenishment is the mechanism that prevents permanent budget exhaustion. On each replenishment tick, every active agent receives a credit to its balance at a configurable rate.

### 5.1 Constant Rate Replenishment

The default replenishment mode. On each tick, every agent's balance increases by `replenishment_rate`, up to `max_balance`:

```
new_balance = min(balance + replenishment_rate, max_balance)
actual_replenishment = new_balance - balance
```

The `max_balance` ceiling prevents balance accumulation beyond a cap. An agent that is idle (not spending) will accumulate budget up to `max_balance` and then stop. This prevents agents from hoarding large war chests that let them monopolize resources during a burst.

### 5.2 Load-Adjusted Replenishment

An alternative replenishment mode where the rate is adjusted based on system load. When the system is under heavy load, replenishment is reduced (making budgets tighter, amplifying the price signal). When load is light, replenishment is increased (loosening budgets, allowing more aggressive resource use).

```
adjusted_rate = base_rate × (1.0 - load_factor × system_utilization)
```

Where `system_utilization` is the current GPU utilization from telemetry and `load_factor` is a configurable sensitivity parameter in `[0.0, 1.0]`. At `load_factor = 0.0`, this reduces to constant-rate replenishment. At `load_factor = 1.0`, replenishment drops to zero at full utilization.

This mode is experimental and disabled by default. Constant-rate replenishment is simpler, more predictable, and sufficient for the initial implementation. Load-adjusted replenishment creates a second coordination channel (budgets tighten under load, in addition to prices rising), which may strengthen or interfere with the price signal depending on the parameter settings.

### 5.3 Replenishment Loop

The replenishment loop is an `asyncio` task managed by the `BudgetManager`. It runs at `replenishment_interval_seconds` (default: 1.0 second).

```python
async def _replenishment_loop(self) -> None:
    tick_number = 0
    while self._running:
        await asyncio.sleep(self._config.replenishment_interval_seconds)
        tick_number += 1

        rate = self._compute_replenishment_rate()
        for budget in self._budgets.values():
            if budget.balance < budget.max_balance:
                actual = min(rate, budget.max_balance - budget.balance)
                if actual > Decimal("0"):
                    budget.balance += actual
                    budget.cumulative_replenished += actual
                    budget.ledger.append(BudgetLedgerEntry(
                        timestamp=time.monotonic(),
                        entry_type=BudgetEntryType.REPLENISHMENT,
                        amount=actual,
                        balance_before=budget.balance - actual,
                        balance_after=budget.balance,
                        metadata={"rate": str(rate), "tick_number": tick_number},
                    ))

            # Check soft/hard limit transitions
            self._check_limit_transitions(budget)
```

### 5.4 Paused Agent Replenishment

When an agent is paused (hard limit hit), replenishment continues. The agent's balance will gradually recover through replenishment credits. Once the balance rises above the hard limit threshold, the pause is lifted automatically. This provides a natural recovery mechanism: an agent that overspent simply waits for replenishment to restore its budget.

The number of ticks to recover from zero to the hard limit is:

```
recovery_ticks = ceil(hard_limit / replenishment_rate)
```

For default values (hard_limit = 0.0 meaning any positive balance lifts the pause, replenishment_rate = 1.0), recovery takes one tick.

---

## 6. Budget Exhaustion Behavior

### 6.1 Soft Limit

The soft limit is a warning threshold. When an agent's balance drops below `soft_limit`, a warning event is emitted and the agent receives a budget warning signal through the communication protocol (spec 04). The agent is not blocked — it can continue spending. But the warning gives capable agents (quantitative and qualitative tiers) the information to self-regulate.

The soft limit warning is issued once per crossing. If the balance drops below the soft limit, recovers above it (through replenishment), and drops below again, a new warning is issued. The `warning_issued` flag tracks whether the current below-soft-limit episode has already been warned about.

```python
def _check_limit_transitions(self, budget: AgentBudget) -> None:
    if budget.balance < budget.soft_limit and not budget.warning_issued:
        budget.warning_issued = True
        self._emit_event(BudgetWarningEvent(
            agent_id=budget.agent_id,
            balance=budget.balance,
            soft_limit=budget.soft_limit,
            estimated_ticks_until_exhaustion=self._estimate_runway(budget),
        ))
    elif budget.balance >= budget.soft_limit and budget.warning_issued:
        budget.warning_issued = False

    if budget.balance <= budget.hard_limit and not budget.is_paused:
        budget.is_paused = True
        self._emit_event(BudgetPauseEvent(
            agent_id=budget.agent_id,
            balance=budget.balance,
            hard_limit=budget.hard_limit,
        ))
    elif budget.balance > budget.hard_limit and budget.is_paused:
        budget.is_paused = False
        self._emit_event(BudgetResumeEvent(
            agent_id=budget.agent_id,
            balance=budget.balance,
        ))
```

### 6.2 Hard Limit

The hard limit is a blocking threshold. When an agent's balance drops to or below `hard_limit`, the agent is paused:

- **Tool calls are blocked.** The confirmation handler will reject any tool call attempt with an "insufficient budget" response.
- **Reasoning continues.** The agent can still generate tokens (the generation has already started and cannot be interrupted mid-stream). However, the budget warning signal injected at the next opportunity tells the agent to stop.
- **Speculative continuation is blocked.** An agent cannot enter speculative continuation if it is paused.

The hard limit defaults to `Decimal("0.0")` — the agent is paused only when balance hits zero. Setting the hard limit to a positive value (e.g., `Decimal("1.0")`) creates a safety margin.

### 6.3 Exhaustion Events

```python
@dataclass(frozen=True)
class BudgetWarningEvent:
    agent_id: str
    balance: Decimal
    soft_limit: Decimal
    estimated_ticks_until_exhaustion: int
    timestamp: float = field(default_factory=time.monotonic)

@dataclass(frozen=True)
class BudgetPauseEvent:
    agent_id: str
    balance: Decimal
    hard_limit: Decimal
    timestamp: float = field(default_factory=time.monotonic)

@dataclass(frozen=True)
class BudgetResumeEvent:
    agent_id: str
    balance: Decimal
    timestamp: float = field(default_factory=time.monotonic)
```

These events are consumed by the metrics system (spec 13) and the agent communication protocol (spec 04).

---

## 7. BudgetManager Class

The `BudgetManager` is the central class. It owns all `AgentBudget` instances and provides the public API for budget operations. No other module directly mutates `AgentBudget` fields — all mutations go through `BudgetManager` methods.

### 7.1 Initialization

```python
class BudgetManager:
    def __init__(self, config: BudgetConfig) -> None:
        """
        Args:
            config: Budget configuration (initial balance, replenishment rate, limits, etc.)
                    Loaded from the global FAM config (spec 12).
        """
        self._config = config
        self._budgets: dict[str, AgentBudget] = {}
        self._pricing_engine: PricingEngine | None = None
        self._event_handlers: list[Callable] = []
        self._running: bool = False
```

### 7.2 Agent Registration

```python
def register_agent(
    self,
    agent_id: str,
    initial_balance: Decimal | None = None,
    max_balance: Decimal | None = None,
    replenishment_rate: Decimal | None = None,
    soft_limit: Decimal | None = None,
    hard_limit: Decimal | None = None,
) -> AgentBudget:
    """Create a budget for a new agent.

    Parameters default to values from config if not specified.
    Raises ValueError if agent_id is already registered.
    """

def unregister_agent(self, agent_id: str) -> AgentBudget:
    """Remove an agent's budget. Returns the final budget state.

    Raises KeyError if agent_id is not registered.
    The returned AgentBudget is a snapshot — further mutations are impossible.
    """
```

### 7.3 Core API

```python
def check_balance(self, agent_id: str) -> Decimal:
    """Return the agent's current balance.

    Raises KeyError if agent_id is not registered.
    """

def can_afford(self, agent_id: str, amount: Decimal) -> bool:
    """Check if the agent can afford a deduction of `amount`.

    Returns True if balance - amount >= hard_limit.
    Returns False if the agent is paused, regardless of balance.
    """

def deduct_reasoning(
    self,
    agent_id: str,
    token_count: int,
    price_per_token: Decimal,
    generation_step: int,
) -> Decimal:
    """Deduct reasoning token cost from agent's budget.

    Always succeeds (post-hoc deduction — tokens already generated).
    Returns the amount deducted.
    May trigger soft/hard limit transitions.
    """

def deduct_tool(
    self,
    agent_id: str,
    endpoint_id: str,
    tool_name: str,
    tool_price: Decimal,
    call_id: str,
) -> bool:
    """Deduct tool call cost from agent's budget.

    Pre-validated: returns False if balance is insufficient or agent is paused.
    Returns True if deduction was successful.
    """

def deduct_speculative(
    self,
    agent_id: str,
    token_count: int,
    price_per_token: Decimal,
    pending_tool_call_id: str,
) -> Decimal:
    """Deduct speculative continuation token cost.

    Same as deduct_reasoning but tracked separately.
    Always succeeds (post-hoc). Returns amount deducted.
    """

def refund_speculative(
    self,
    agent_id: str,
    amount: Decimal,
    pending_tool_call_id: str,
    reason: str,
) -> Decimal:
    """Refund speculative spend (fully or partially).

    Returns the actual amount refunded (may be less than requested
    if capped at max_balance).
    """

def replenish(self, agent_id: str, amount: Decimal) -> Decimal:
    """Manually replenish an agent's budget.

    Returns the actual amount added (may be less if capped at max_balance).
    Records a ledger entry with entry_type=REPLENISHMENT.
    """
```

### 7.4 Query API

```python
def get_budget(self, agent_id: str) -> AgentBudget:
    """Return the agent's full budget object (read-only access intended).

    Raises KeyError if agent_id is not registered.
    """

def get_summary(self, agent_id: str) -> BudgetSummary:
    """Return an aggregated summary of the agent's budget state."""

def get_remaining_runway(self, agent_id: str) -> RunwayEstimate:
    """Estimate how long the agent can continue at current spend rate."""

def get_all_budgets(self) -> dict[str, AgentBudget]:
    """Return all agent budgets. For observability only."""

def get_system_summary(self) -> SystemBudgetSummary:
    """Aggregate budget statistics across all agents."""
```

### 7.5 Summary and Runway Types

```python
@dataclass(frozen=True)
class BudgetSummary:
    """Aggregated view of an agent's budget for the signal formatter."""
    agent_id: str
    balance: Decimal
    initial_balance: Decimal
    max_balance: Decimal
    cumulative_reasoning_spend: Decimal
    cumulative_tool_spend: Decimal
    cumulative_speculative_spend: Decimal
    cumulative_replenished: Decimal
    replenishment_rate: Decimal
    is_paused: bool
    soft_limit: Decimal
    hard_limit: Decimal
    balance_fraction: float          # balance / max_balance
    total_spend: Decimal             # sum of all cumulative spends
    net_flow: Decimal                # replenished - total_spend

@dataclass(frozen=True)
class RunwayEstimate:
    """Projection of budget longevity at current rates."""
    agent_id: str
    balance: Decimal
    reasoning_spend_rate: Decimal     # Units per second over recent window
    tool_spend_rate: Decimal          # Units per second over recent window
    total_spend_rate: Decimal         # Combined rate
    replenishment_rate: Decimal       # Units per second
    net_rate: Decimal                 # replenishment - total_spend (positive = accumulating)
    estimated_ticks_until_exhaustion: int | None  # None if net_rate >= 0
    estimated_seconds_until_exhaustion: float | None

@dataclass(frozen=True)
class SystemBudgetSummary:
    """System-wide budget statistics."""
    total_agents: int
    active_agents: int                # Not paused
    paused_agents: int
    total_balance: Decimal            # Sum of all agent balances
    total_reasoning_spend: Decimal
    total_tool_spend: Decimal
    total_speculative_spend: Decimal
    total_replenished: Decimal
    mean_balance: Decimal
    min_balance: Decimal
    max_balance_value: Decimal
```

### 7.6 Spend Rate Computation

The `RunwayEstimate` requires computing the agent's current spend rate. This is derived from the ledger: the budget manager looks at deductions in a recent time window (default: last 30 seconds) and computes the rate.

```python
def _compute_spend_rate(
    self, budget: AgentBudget, window_seconds: float = 30.0
) -> tuple[Decimal, Decimal]:
    """Compute reasoning and tool spend rates from recent ledger entries.

    Returns (reasoning_rate, tool_rate) in units per second.
    """
    now = time.monotonic()
    cutoff = now - window_seconds
    reasoning_total = Decimal("0")
    tool_total = Decimal("0")

    for entry in reversed(budget.ledger):
        if entry.timestamp < cutoff:
            break
        if entry.entry_type == BudgetEntryType.REASONING_DEDUCTION:
            reasoning_total += abs(entry.amount)
        elif entry.entry_type == BudgetEntryType.TOOL_DEDUCTION:
            tool_total += abs(entry.amount)
        elif entry.entry_type == BudgetEntryType.SPECULATIVE_DEDUCTION:
            reasoning_total += abs(entry.amount)

    window = Decimal(str(window_seconds))
    return (reasoning_total / window, tool_total / window)
```

### 7.7 Lifecycle

```python
async def start(self, pricing_engine: PricingEngine | None = None) -> None:
    """Start the replenishment loop.

    Optionally receives a pricing engine reference for load-adjusted replenishment.
    """

async def stop(self) -> None:
    """Stop the replenishment loop. Budgets remain accessible for final reads."""
```

---

## 8. Concurrency Model

The budget manager operates within FAM's single-threaded `asyncio` event loop. Concurrency comes from multiple agent graph executions running as concurrent coroutines that may call budget operations "simultaneously" (interleaved by the event loop scheduler).

The critical invariant is: **no balance mutation may be interleaved with another mutation on the same agent.** Since Python's `asyncio` does not preempt coroutines (a coroutine runs until it hits an `await`), this invariant is maintained by ensuring that the check-and-mutate sequence in each deduction method contains no `await` between reading the balance and writing the new balance.

All public methods (`check_balance`, `can_afford`, `deduct_*`, `replenish`) are synchronous. They do not `await` anything. This guarantees atomicity under `asyncio` concurrency without locks.

The replenishment loop is the only async component. It `await`s `asyncio.sleep()` between ticks, but the actual replenishment (iterating budgets and crediting them) is a synchronous block within the async method. During replenishment, no other coroutine can run, so balance mutations from deduction calls cannot interleave with replenishment credits.

If FAM ever moves to a multi-threaded concurrency model, the budget manager will need explicit locking (one lock per agent budget). This is noted as a future concern, not a current requirement.

---

## 9. Configuration

All configuration for the budget manager is namespaced under `budget` in the global FAM configuration (spec 12). The full schema:

```yaml
budget:
  # Default values for new agents (can be overridden per-agent at registration)
  defaults:
    initial_balance: "100.0"
    max_balance: "200.0"
    replenishment_rate: "1.0"        # Units per replenishment tick
    soft_limit: "10.0"              # Balance warning threshold
    hard_limit: "0.0"              # Balance blocking threshold

  # Replenishment configuration
  replenishment:
    interval_seconds: 1.0           # Time between replenishment ticks
    mode: constant                  # 'constant' or 'load_adjusted'
    load_factor: 0.5               # Sensitivity to system load (load_adjusted mode)

  # Speculative refund policy
  speculative_refund_policy: no_refund  # 'no_refund', 'full', or 'half'

  # Spend rate computation window for runway estimates
  spend_rate_window_seconds: 30.0

  # Ledger configuration
  ledger:
    max_entries: 1000               # Per-agent ledger history size

  # Per-agent overrides (optional)
  agents:
    # Example:
    # agent_priority_high:
    #   initial_balance: "500.0"
    #   max_balance: "1000.0"
    #   replenishment_rate: "5.0"
```

---

## 10. Error Handling

The budget manager is designed to be safe and predictable. Budget operations never raise exceptions for normal business logic (insufficient balance returns `False`, not an exception). Exceptions are reserved for programming errors (unregistered agent_id, invalid amount).

**Unregistered agent:** `check_balance`, `can_afford`, `deduct_*`, `get_budget`, `get_summary`, `get_remaining_runway` raise `KeyError` if the `agent_id` is not registered. This is a programming error — the caller should have registered the agent first.

**Negative amount:** `deduct_*` and `replenish` raise `ValueError` if called with a negative amount. Deductions must be positive (the sign is applied internally). Replenishments must be positive.

**Decimal precision:** All amounts are quantized to 4 decimal places on input. If a caller passes a `Decimal` with more precision, it is rounded (ROUND_HALF_UP) and a debug-level log message is emitted. This prevents accumulation of tiny fractional errors.

**Balance underflow:** Reasoning and speculative deductions can cause the balance to go below the hard limit (because they are post-hoc). The balance is allowed to go negative. The hard limit check triggers on the *next* budget check, not retroactively. This design avoids the complexity of interrupting an in-progress generation.

**Replenishment overflow:** Replenishment is clamped to `max_balance`. If `balance + replenishment_rate > max_balance`, only the difference is credited. The `actual_replenishment` in the ledger entry reflects the clamped amount.

**Ledger overflow:** The ledger deque has a `maxlen` from configuration. When full, the oldest entry is silently evicted. No data loss warning is emitted (this is expected behavior for a bounded ring buffer).

---

## 11. Metrics Emitted

The budget manager emits the following metrics to the observability system (spec 13):

**Gauges (per agent_id):** `budget.balance`, `budget.cumulative_reasoning_spend`, `budget.cumulative_tool_spend`, `budget.cumulative_speculative_spend`, `budget.cumulative_replenished`, `budget.balance_fraction` (balance / max_balance), `budget.is_paused` (0 or 1).

**Gauges (system-wide):** `budget.total_agents`, `budget.active_agents`, `budget.paused_agents`, `budget.total_balance`, `budget.mean_balance`.

**Counters:** `budget.deductions.total` (per agent_id, per entry_type), `budget.deductions.rejected` (per agent_id — tool deductions that failed due to insufficient balance), `budget.replenishments.total` (per agent_id), `budget.warnings.issued` (per agent_id — soft limit crossings), `budget.pauses.total` (per agent_id — hard limit activations), `budget.resumes.total` (per agent_id — hard limit recoveries), `budget.refunds.total` (per agent_id — speculative refunds).

**Histograms:** `budget.deduction_amount` (per entry_type — distribution of deduction sizes), `budget.replenishment_amount` (distribution of replenishment credits, useful for load-adjusted mode).

---

## 12. Testing Strategy

### 12.1 Unit Tests

**Registration and initial state:** Register an agent. Verify balance equals `initial_balance`. Verify all cumulative counters are zero. Verify `is_paused` is False. Verify ledger has exactly one entry (INITIAL).

**Reasoning deduction:** Register agent with balance 100. Deduct 50 tokens at price 0.5 (cost = 25). Verify balance is 75. Verify `cumulative_reasoning_spend` is 25. Verify ledger entry has correct metadata.

**Tool deduction — success:** Register agent with balance 100. Deduct tool call at price 10. Verify returns True. Verify balance is 90.

**Tool deduction — insufficient balance:** Register agent with balance 5. Attempt tool deduction at price 10. Verify returns False. Verify balance is still 5. Verify no ledger entry was added.

**Tool deduction — paused agent:** Register agent, force pause. Attempt tool deduction. Verify returns False regardless of balance.

**Speculative deduction and refund:** Register agent with balance 100. Deduct 20 speculative tokens at price 0.5 (cost = 10). Verify balance is 90 and `cumulative_speculative_spend` is 10. Refund 10. Verify balance is 100. Verify ledger has both entries.

**Replenishment with max_balance cap:** Register agent with max_balance 200 and balance 195. Replenish at rate 10. Verify balance is 200 (capped), not 205. Verify ledger entry shows actual amount of 5.

**Soft limit warning:** Register agent with soft_limit 10 and balance 15. Deduct 6 (balance drops to 9, below soft_limit). Verify warning event is emitted. Deduct another 1. Verify no second warning (already issued for this episode). Replenish back above 10. Deduct to below 10 again. Verify a new warning is issued.

**Hard limit pause and resume:** Register agent with hard_limit 0 and balance 5. Deduct reasoning tokens costing 6 (balance goes to -1). Verify `is_paused` becomes True. Verify `can_afford` returns False. Replenish 2 (balance to 1, above hard_limit). Verify `is_paused` becomes False.

**Runway estimation:** Register agent. Perform several deductions over simulated time. Verify the runway estimate is approximately correct (within 20% of manual calculation).

### 12.2 Integration Tests

**Replenishment loop:** Start the budget manager with a 0.1s replenishment interval. Register an agent with balance 0 and rate 10. Wait 0.5 seconds. Verify balance is approximately 50 (5 ticks × 10). Stop manager. Verify loop has terminated.

**Concurrent deductions:** Register one agent. Launch 10 concurrent coroutines that each deduct 1 unit. Verify the final balance is exactly initial_balance - 10 (no race conditions, no double-counting).

**Budget with pricing engine:** Connect budget manager to a pricing engine. Drive price changes. Deduct reasoning tokens. Verify the deduction amount reflects the price at deduction time, not the current price.

**Full lifecycle:** Register agent, start manager, perform mixed deductions and replenishments over several seconds, stop manager, read final summary. Verify all cumulative counters are consistent (balance = initial + replenished - reasoning_spend - tool_spend - speculative_spend + refunds).

### 12.3 Property-Based Tests

Use Hypothesis to generate random sequences of (deduct_reasoning, deduct_tool, deduct_speculative, refund_speculative, replenish) operations with random amounts. After all operations, verify: (a) balance is exactly `initial_balance + cumulative_replenished - cumulative_reasoning_spend - cumulative_tool_spend - cumulative_speculative_spend + cumulative_refunds`, (b) all cumulative counters are non-negative, (c) the ledger reconstructs the correct balance at every point (each entry's `balance_after` equals the next entry's `balance_before`).

Generate random agent configurations and verify that no configuration causes a crash, negative cumulative counters, or a balance that exceeds `max_balance` after replenishment.

---

## 13. Dependencies

**Internal:** `fam/types.py` (shared types), `fam/pricing/engine.py` (PricingEngine reference for load-adjusted replenishment and price lookups), `fam/config/schema.py` (configuration schema).

**External:** `decimal` (Python standard library). The budget manager uses only the Python standard library (`decimal`, `dataclasses`, `collections.deque`, `asyncio`, `time`, `logging`, `enum`). No third-party dependencies. This is intentional — budget accounting must be maximally reliable with no external failure modes.

**Interface contracts:** The budget manager is consumed by the confirmation handler (spec 06) for pre-dispatch balance checks, the speculative continuation manager (spec 08) for speculative deductions and refunds, and the framework adapters (spec 09) for reasoning token metering. These consumers call the public API methods defined in section 7.

---

## 14. Open Questions

**Interest / decay on idle balance.** Should idle agents pay a holding cost on unspent budget? A decay mechanism would prevent agents from free-riding by accumulating budget during high-price periods and spending it all during low-price periods. However, it adds complexity and may penalize agents that are legitimately paused (waiting for a user, waiting for a long external process). Deferred.

**Budget transfer between agents.** Should agents be able to transfer budget to each other? This would enable cooperative strategies (one agent funds another's expensive tool call). It also opens abuse vectors (colluding agents pool budgets to monopolize resources). This is a research-level question, not a feature for the initial implementation.

**Dynamic initial balance based on task priority.** Should high-priority tasks receive a larger initial budget? The current system supports per-agent overrides at registration time, so this is mechanically possible. The policy question is whether initial balance should be the mechanism for priority differentiation, or whether a separate priority system is cleaner. The current recommendation is to use per-agent overrides for critical-path agents and leave the default for others.

**Budget visualization for agents.** Should agents receive a periodic "budget statement" showing their spending breakdown (how much went to reasoning vs. tools vs. speculation)? This could help capable agents optimize their strategy. It could also consume context window space with low-value information for weaker models. The tiered signal formatter (spec 05) is the right place to decide what budget information to surface and at what level of detail.

**Negative balance thresholds.** The current design allows reasoning deductions to push balance below zero. Should there be a "maximum overdraft" limit below which even reasoning is blocked (by injecting a "stop generating" signal)? This is mechanically possible through the framework adapter but raises questions about mid-stream interruption of LLM generation. Deferred to the adapter spec (spec 09).
