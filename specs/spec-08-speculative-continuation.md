# Spec 08 — Speculative Continuation Manager

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview), Spec 03 (Budget Manager), Spec 04 (Agent Communication Protocol), Spec 05 (Tiered Signal Formatter), Spec 07 (Tool Dispatch Queue)
**Consumed By:** Spec 06 (Confirmation Handler), Spec 09 (Framework Adapters), Spec 13 (Metrics & Observability)

---

## 1. Purpose

When an agent defers a tool call — choosing not to pay the current tool price — the agent would normally stall, blocked on a result that is not coming yet. Stalling wastes the agent's share of GPU inference capacity: the KV-cache remains allocated, the agent's slot in the batch queue is occupied, and no useful work is produced.

The speculative continuation manager solves this by allowing the agent to keep reasoning provisionally while the deferred tool call is pending. The agent is informed that the tool result is not yet available, given the current reasoning price, and invited to continue thinking under the assumption that the result will arrive later. The tokens generated during this speculative phase are tracked separately, metered at the current reasoning price through the budget manager, and tagged with the pending tool call they depend on.

When the deferred tool result eventually arrives — because the agent later approved the call at a lower price, because a timeout triggered automatic dispatch, or because another pathway produced the result — the manager must reconcile. If the speculative reasoning is consistent with the actual result, the speculative tokens can be retained (an optimization). If not, the speculative tokens must be discarded and the conversation state rolled back to the checkpoint taken before speculation began, with the actual tool result injected cleanly.

This module is the most complex in the orchestration layer. It touches state management, checkpoint/rollback, prompt injection, budget metering, and framework-specific conversation manipulation. It should be built last among the core modules and tested extensively.

---

## 2. Module Location

```
fam/
├── speculative/
│   ├── __init__.py
│   ├── manager.py          # SpeculativeContinuationManager — main entry point
│   ├── session.py           # SpeculativeSession dataclass and lifecycle
│   ├── checkpoint.py        # Checkpoint creation and rollback logic
│   ├── reconciler.py        # Result reconciliation and consistency checking
│   └── prompts.py           # Speculative prompt templates
```

Tests live in `tests/test_speculative/`.

---

## 3. Core Concepts

### 3.1 Speculative Session

A speculative session is created when an agent defers a tool call. The session encapsulates everything needed to manage the speculative phase and reconcile when the result arrives.

```python
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class SpeculativeSessionState(str, Enum):
    ACTIVE = "active"             # Agent is generating speculative tokens
    AWAITING_RESULT = "awaiting"  # Session paused, waiting for tool result
    RECONCILING = "reconciling"   # Tool result arrived, determining consistency
    COMMITTED = "committed"       # Speculative tokens retained
    ROLLED_BACK = "rolled_back"   # Speculative tokens discarded, state rewound
    CANCELLED = "cancelled"       # Session cancelled (agent cancelled the tool call)


@dataclass
class SpeculativeSession:
    """Tracks one speculative continuation episode for one agent."""

    session_id: str
    agent_id: str
    deferred_tool_call_id: str
    tool_name: str
    tool_args: dict

    # Checkpoint reference for rollback
    checkpoint_id: str
    checkpoint_thread_id: str

    # State tracking
    state: SpeculativeSessionState = SpeculativeSessionState.ACTIVE
    created_at: float = 0.0
    resolved_at: float | None = None

    # Token accounting
    speculative_tokens_generated: int = 0
    speculative_cost: Decimal = Decimal("0")
    reasoning_price_at_start: Decimal = Decimal("0")

    # Speculative content tracking
    speculative_message_ids: list[str] = field(default_factory=list)
    speculative_content_hashes: list[str] = field(default_factory=list)

    # Resolution
    tool_result: str | None = None
    consistency_score: float | None = None
    resolution: str | None = None  # "commit", "rollback", "cancel"
```

Each agent can have at most one active speculative session at a time. If an agent defers a second tool call while already speculating on a first, the second deferral is queued and the agent is notified that it is already in speculative mode for a pending call.

### 3.2 Checkpoint

A checkpoint is a saved snapshot of the agent's LangGraph state taken immediately before speculative continuation begins. It captures the full conversation history, all FAM-managed state fields, and the graph execution position. Checkpoints use LangGraph's built-in `MemorySaver` checkpointing mechanism.

```python
@dataclass(frozen=True)
class SpeculativeCheckpoint:
    """Reference to a LangGraph state checkpoint for rollback."""

    checkpoint_id: str                # LangGraph checkpoint ID
    thread_id: str                    # LangGraph thread ID
    agent_id: str
    created_at: float
    message_count_at_checkpoint: int  # Number of messages in conversation at checkpoint time
    budget_balance_at_checkpoint: Decimal
```

The checkpoint is the rollback target. If speculation must be discarded, the LangGraph state is restored to this checkpoint and execution resumes from this point with the actual tool result injected.

### 3.3 The Speculative Lifecycle

The full lifecycle of a speculative continuation episode:

```
Agent defers tool call
        │
        ▼
┌─────────────────────────────┐
│  1. Create checkpoint       │
│     Save current LangGraph  │
│     state via MemorySaver   │
│                             │
│  2. Create speculative      │
│     session                 │
│                             │
│  3. Format and inject       │
│     speculative prompt      │
│     into agent conversation │
│                             │
│  4. Set agent's             │
│     speculative_mode = True │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  SPECULATIVE PHASE          │
│                             │
│  Agent reasons freely.      │
│  Each generation step:      │
│  - Tokens counted and       │
│    attributed to session    │
│  - Cost = tokens × current  │
│    reasoning_price          │
│  - Deducted from budget     │
│    via BudgetManager        │
│  - Budget exhaustion stops  │
│    speculation naturally    │
│                             │
│  Agent may:                 │
│  - Continue reasoning       │
│  - Request another tool     │
│    call (handled normally)  │
│  - Declare it cannot        │
│    proceed without result   │
└──────────┬──────────────────┘
           │
           │  Tool result arrives (or timeout/cancel)
           ▼
┌─────────────────────────────┐
│  RECONCILIATION             │
│                             │
│  Option A: Always rollback  │
│  (simple, default)          │
│  - Discard speculative      │
│    tokens                   │
│  - Restore checkpoint       │
│  - Inject actual tool       │
│    result                   │
│  - Resume from checkpoint   │
│                             │
│  Option B: Consistency      │
│  check (advanced, optional) │
│  - Hash speculative content │
│  - Compare with expected    │
│    patterns                 │
│  - If consistent: commit    │
│    (keep speculative tokens,│
│    append tool result)      │
│  - If inconsistent: rollback│
└──────────┬──────────────────┘
           │
           ▼
     Session closed,
     metrics emitted
```

---

## 4. Speculative Prompt Templates

When an agent enters speculative continuation, a prompt is injected into its conversation to inform it of the situation. The prompt is formatted according to the agent's capability tier (spec 05) and includes the current reasoning price so the agent can self-regulate how much speculation to perform.

### 4.1 Quantitative Tier Prompt

```python
SPECULATIVE_PROMPT_QUANTITATIVE = """\
[FAM] Tool call deferred: `{tool_name}({tool_args_summary})`
Status: The tool result is pending. Estimated wait: {estimated_wait}.

Current reasoning price: {reasoning_price} per token.
Your budget balance: {budget_balance} ({budget_runway} tokens at current price).

You may continue reasoning provisionally while waiting. Speculative tokens are \
metered at the current reasoning price. If the tool result invalidates your \
provisional reasoning, it will be discarded and you will receive the actual result.

Consider:
- Is there useful reasoning you can do without this result?
- At {reasoning_price}/token, how many tokens of speculation are worth the risk?
- Can you formulate contingency plans (if result is X, then ...; if Y, then ...)?

Proceed with provisional reasoning, or state that you need the result to continue.\
"""
```

### 4.2 Qualitative Tier Prompt

```python
SPECULATIVE_PROMPT_QUALITATIVE = """\
[FAM] Tool call deferred: `{tool_name}`
The tool result is not yet available. Reasoning cost is currently {price_level}.

You can continue thinking provisionally while waiting. Your provisional reasoning \
costs budget and may be discarded if the tool result contradicts it.

If reasoning is {price_adjective}, consider exploring the problem further while \
waiting. If reasoning is {price_adjective_inverse}, you may want to wait for the \
result before continuing.

Proceed with provisional reasoning, or state that you need the result to continue.\
"""
```

### 4.3 Directional Tier Prompt

```python
SPECULATIVE_PROMPT_DIRECTIONAL = """\
[FAM] Waiting for tool result: `{tool_name}`
You may continue thinking while waiting. Your thoughts may be revised when the \
result arrives. {nudge}\
"""
```

The `{nudge}` field is either `"Resources are available — feel free to think ahead."` (when reasoning is cheap) or `"Resources are scarce — keep provisional reasoning brief."` (when reasoning is expensive).

### 4.4 Template Rendering

```python
from fam.signals.formatter import TieredSignalFormatter


class SpeculativePromptRenderer:
    """Renders speculative continuation prompts at the agent's capability tier."""

    def __init__(self, formatter: TieredSignalFormatter) -> None:
        self._formatter = formatter

    def render(
        self,
        agent_id: str,
        tool_name: str,
        tool_args: dict,
        reasoning_price: Decimal,
        budget_balance: Decimal,
        estimated_wait_seconds: float | None,
    ) -> str:
        """Render the speculative prompt for the given agent's tier."""
```

The renderer delegates tier lookup to the `TieredSignalFormatter` and selects the appropriate template. Tool args are summarized (truncated to 120 characters) to avoid injecting excessively long function arguments into the prompt.

---

## 5. Token Tracking and Budget Metering

Speculative tokens are real tokens — they consume GPU inference capacity and should be priced accordingly. The key design property is that speculative tokens are metered at the current reasoning price through the budget manager, so agents naturally self-regulate speculation volume. When GPU is scarce (reasoning price is high), speculation becomes expensive and agents will speculate less. When GPU is abundant (reasoning price is low), speculation is cheap and agents will speculate freely.

### 5.1 Token Counting

The speculative continuation manager receives token count updates from the framework adapter (spec 09) after each generation step. The adapter reports:

```python
@dataclass(frozen=True)
class GenerationStepReport:
    """Report from the framework adapter after one LLM generation step."""

    agent_id: str
    tokens_generated: int
    is_speculative: bool       # True if agent is in speculative mode
    session_id: str | None     # Speculative session ID, if speculative
    timestamp: float
```

When `is_speculative` is True, the manager updates the session's token counter:

```python
async def record_speculative_tokens(
    self,
    agent_id: str,
    tokens: int,
    current_reasoning_price: Decimal,
) -> TokenRecordResult:
    """Record speculative tokens and deduct from budget.

    Returns a result indicating whether the deduction succeeded
    and whether the agent should stop speculating (budget exhausted).
    """
```

### 5.2 Budget Deduction

For each batch of speculative tokens, the cost is computed as:

```
cost = tokens × current_reasoning_price
```

This cost is deducted from the agent's budget via `BudgetManager.deduct()`. The deduction is tagged as speculative so that budget history distinguishes speculative spend from regular reasoning spend.

```python
@dataclass(frozen=True)
class TokenRecordResult:
    tokens_recorded: int
    cost_deducted: Decimal
    budget_remaining: Decimal
    should_stop: bool          # True if budget is at or below soft limit
    session_total_tokens: int
    session_total_cost: Decimal
```

If the budget is insufficient for the deduction, the deduction fails and `should_stop` is True. The agent is not forcibly stopped — the framework adapter sees `should_stop` and may inject a budget warning, but the agent ultimately decides whether to continue. If the budget hits the hard limit, the budget manager blocks further deductions and the agent cannot generate more tokens regardless of intent.

### 5.3 Speculative Cost Refund on Rollback

When a speculative session is rolled back (speculative tokens discarded), the tokens are gone — they consumed real GPU inference. The budget is **not** refunded. This is intentional: the GPU capacity was genuinely consumed, and refunding would create a moral hazard where agents speculate recklessly knowing costs are reversible. The speculative tokens were metered at the prevailing reasoning price precisely so that agents internalize the real cost of speculation.

The budget accounting shows these tokens as "speculative spend (rolled back)" in the agent's budget history, distinguished from "speculative spend (committed)" for sessions where tokens were retained.

---

## 6. Reconciliation

When the deferred tool result arrives, the speculative continuation manager must decide what to do with the speculative tokens the agent has generated. There are two strategies, selected by configuration.

### 6.1 Strategy: Always Rollback (Default)

The simplest and most conservative strategy. When the tool result arrives:

1. Restore the LangGraph state to the checkpoint taken before speculation began.
2. Inject the actual tool result into the restored state as a tool-result message.
3. Resume agent execution from the restored state.
4. All speculative messages are discarded from the conversation.

This strategy is always correct — it never allows inconsistent reasoning to persist. The cost is that useful speculative work is thrown away even when it was consistent with the actual result. For the initial implementation, this tradeoff is acceptable. The evaluation harness (spec 14) will measure how often speculative tokens are discarded to assess whether the consistency-check strategy would provide meaningful savings.

```python
class AlwaysRollbackReconciler:
    """Reconciliation strategy that always discards speculative tokens."""

    async def reconcile(
        self,
        session: SpeculativeSession,
        tool_result: str,
        graph_state_manager: "GraphStateManager",
    ) -> ReconciliationResult:
        """Roll back to checkpoint and inject tool result."""
```

### 6.2 Strategy: Consistency Check (Advanced, Optional)

An optimization that attempts to retain speculative tokens when they are consistent with the actual tool result. Consistency is evaluated by a heuristic that examines the speculative content and the tool result.

```python
class ConsistencyCheckReconciler:
    """Reconciliation strategy that checks speculative content against result."""

    def __init__(
        self,
        consistency_threshold: float = 0.7,
        max_speculative_tokens_to_check: int = 500,
    ) -> None:
        self._threshold = consistency_threshold
        self._max_tokens = max_speculative_tokens_to_check

    async def reconcile(
        self,
        session: SpeculativeSession,
        tool_result: str,
        graph_state_manager: "GraphStateManager",
    ) -> ReconciliationResult:
        """Check consistency and decide whether to commit or roll back."""
```

The consistency check heuristic works as follows:

**Step 1 — Contradiction Detection:** Scan the speculative content for explicit references to the pending tool call's expected result. If the speculative content makes concrete claims about what the tool result *should be* (e.g., "the search probably returns X"), check whether the actual result contradicts those claims. Simple string matching and negation detection are used — this is not a semantic analysis.

**Step 2 — Dependency Assessment:** Assess how heavily the speculative reasoning depends on the unknown tool result. If the speculative content is largely independent of the tool result (e.g., the agent explored a different aspect of the problem), it is more likely consistent. If the speculative content branches on assumed tool results (e.g., "if the API returns an error, then ..."), the consistency score is lower.

**Step 3 — Scoring:** Produce a consistency score between 0.0 (definitely inconsistent) and 1.0 (definitely consistent). If the score exceeds `consistency_threshold`, the speculative tokens are committed. Otherwise, rollback occurs.

```python
@dataclass(frozen=True)
class ReconciliationResult:
    action: str                    # "commit" or "rollback"
    consistency_score: float       # 0.0–1.0
    speculative_tokens_retained: int
    speculative_tokens_discarded: int
    speculative_cost_retained: Decimal
    speculative_cost_discarded: Decimal
    reconciliation_duration_ms: float
```

When tokens are committed (retained):

1. The checkpoint is discarded (no longer needed for rollback).
2. The actual tool result is appended to the conversation after the speculative messages.
3. A brief injection message tells the agent: `"[FAM] Tool result received for {tool_name}. Your provisional reasoning has been retained."`.

When tokens are rolled back even under this strategy, the process is identical to the always-rollback path.

### 6.3 Reconciler Protocol

Both strategies implement a common protocol so they can be swapped via configuration:

```python
from typing import Protocol


class Reconciler(Protocol):
    async def reconcile(
        self,
        session: SpeculativeSession,
        tool_result: str,
        graph_state_manager: "GraphStateManager",
    ) -> ReconciliationResult:
        """Reconcile speculative tokens with the actual tool result."""
        ...
```

---

## 7. Checkpoint and Rollback

### 7.1 Checkpoint Creation

Checkpoints are created by the `CheckpointManager`, which wraps LangGraph's `MemorySaver` with FAM-specific metadata.

```python
class CheckpointManager:
    """Manages LangGraph state checkpoints for speculative rollback."""

    def __init__(self, checkpointer: "BaseCheckpointSaver") -> None:
        self._checkpointer = checkpointer
        self._active_checkpoints: dict[str, SpeculativeCheckpoint] = {}

    async def create_checkpoint(
        self,
        agent_id: str,
        thread_id: str,
        graph_state: dict,
        budget_balance: Decimal,
    ) -> SpeculativeCheckpoint:
        """Save current graph state and return a checkpoint reference.

        The checkpoint includes the full conversation history, all FAM-managed
        state fields, and the graph execution position.
        """

    async def restore_checkpoint(
        self,
        checkpoint: SpeculativeCheckpoint,
    ) -> dict:
        """Restore graph state to the checkpoint and return the restored state.

        This operation:
        1. Loads the saved state from the checkpointer.
        2. Verifies integrity (message count matches expected).
        3. Returns the restored state dict for the framework adapter
           to apply to the running graph.
        """

    async def discard_checkpoint(
        self,
        checkpoint: SpeculativeCheckpoint,
    ) -> None:
        """Delete a checkpoint that is no longer needed.

        Called after successful commit (speculative tokens retained)
        or after successful rollback (state already restored).
        """
```

### 7.2 Rollback Process

The rollback process is the critical path that must execute correctly to maintain conversation integrity. The steps are:

1. **Pause agent execution.** The framework adapter suspends the agent's LangGraph graph execution using LangGraph's interrupt mechanism. No new tokens are generated during rollback.

2. **Restore state.** The `CheckpointManager.restore_checkpoint()` loads the saved state. The restored state has the conversation as it was before speculation began — no speculative messages are present.

3. **Inject tool result.** The actual tool result is formatted as a tool-result message (matching the format the agent would have received if the tool call had completed normally) and appended to the restored conversation.

4. **Inject reconciliation notice.** A brief system message informs the agent that speculation was discarded:

```python
ROLLBACK_NOTICE = """\
[FAM] Tool result received for `{tool_name}`. Your provisional reasoning \
while waiting has been discarded. The actual result is provided above. \
Continue from here.\
"""
```

5. **Reset FAM state.** The agent's `speculative_mode` is set to False. The speculative session is marked as `ROLLED_BACK`. The budget balance in the FAM state is synced to the current balance (which reflects the speculative spend — this is not refunded).

6. **Resume execution.** The framework adapter resumes the graph from the updated state. The agent sees the tool result and the rollback notice, and continues reasoning from there.

### 7.3 State Integrity

The rollback must leave the conversation in a state that is indistinguishable (from the agent's perspective) from the case where the tool call completed normally with some delay. This means:

- No speculative messages remain in the conversation after rollback.
- The tool result message is in the same format and position as a normal tool result.
- The agent's budget balance reflects reality (speculative spend already deducted, not refunded).
- No orphaned FAM state fields reference the defunct speculative session.

The `CheckpointManager` verifies integrity by comparing the restored state's message count against the expected count recorded at checkpoint time. If they do not match (indicating the checkpoint was corrupted or the wrong checkpoint was loaded), the rollback is aborted and the session is marked as failed. The agent receives an error message and continues from the current (speculative) state as a fallback — better to keep potentially-inconsistent speculation than to corrupt the conversation entirely.

---

## 8. SpeculativeContinuationManager Class

The `SpeculativeContinuationManager` is the central class of this module. It coordinates session creation, prompt injection, token tracking, reconciliation, and checkpoint management.

### 8.1 Initialization

```python
class SpeculativeContinuationManager:
    def __init__(
        self,
        checkpoint_manager: CheckpointManager,
        budget_manager: "BudgetManager",
        prompt_renderer: SpeculativePromptRenderer,
        reconciler: Reconciler,
        config: SpeculativeConfig,
    ) -> None:
        self._checkpoint_mgr = checkpoint_manager
        self._budget_mgr = budget_manager
        self._prompt_renderer = prompt_renderer
        self._reconciler = reconciler
        self._config = config
        self._active_sessions: dict[str, SpeculativeSession] = {}  # agent_id → session
        self._session_history: deque[SpeculativeSession] = deque(maxlen=1000)
```

### 8.2 Session Lifecycle API

```python
async def begin_speculation(
    self,
    agent_id: str,
    tool_call_id: str,
    tool_name: str,
    tool_args: dict,
    thread_id: str,
    graph_state: dict,
    current_reasoning_price: Decimal,
    budget_balance: Decimal,
    estimated_wait_seconds: float | None = None,
) -> SpeculationStartResult:
    """Start a speculative continuation session for the given agent.

    Creates a checkpoint, creates a session, renders the speculative
    prompt, and returns the prompt for injection into the agent's
    conversation.

    Returns an error result if the agent already has an active session.
    """

async def record_speculative_tokens(
    self,
    agent_id: str,
    tokens: int,
    current_reasoning_price: Decimal,
) -> TokenRecordResult:
    """Record speculative tokens generated by the agent.

    Deducts cost from budget. Returns whether the agent should stop.
    """

async def deliver_tool_result(
    self,
    agent_id: str,
    tool_call_id: str,
    tool_result: str,
    graph_state_manager: "GraphStateManager",
) -> ReconciliationResult:
    """Deliver a deferred tool result and reconcile with speculation.

    Looks up the active session for the agent, verifies the tool_call_id
    matches, and delegates to the configured reconciler.
    """

async def cancel_session(
    self,
    agent_id: str,
    reason: str = "agent_cancelled",
) -> None:
    """Cancel an active speculative session.

    Called when the agent cancels the deferred tool call or when
    a timeout fires. Discards the checkpoint and marks the session
    as cancelled. Speculative tokens already generated and paid for
    remain in the conversation (they become regular reasoning).
    """

def get_active_session(self, agent_id: str) -> SpeculativeSession | None:
    """Return the active speculative session for the agent, if any."""

def is_speculating(self, agent_id: str) -> bool:
    """Check whether the agent is currently in speculative mode."""
```

### 8.3 Result Types

```python
@dataclass(frozen=True)
class SpeculationStartResult:
    success: bool
    session_id: str | None
    checkpoint_id: str | None
    speculative_prompt: str | None
    error: str | None              # Non-None if success is False
```

### 8.4 Timeout Handling

If a speculative session exceeds `max_speculation_duration_seconds` (default: 60.0), the manager fires a timeout. The timeout behavior is configurable:

- **`auto_dispatch`** (default): The deferred tool call is automatically submitted to the dispatch queue at whatever the current tool price is. The agent's budget is deducted and the tool result is delivered through the normal reconciliation path when it completes.

- **`cancel`**: The deferred tool call is cancelled. The speculative session is closed via `cancel_session()`. The agent receives a timeout notice and continues from its current (speculative) state. The speculative tokens become regular reasoning tokens.

- **`notify`**: The agent is informed that the speculation has timed out but no automatic action is taken. The agent can choose to approve the tool call, cancel it, or continue speculating. This extends the session with a new timeout window.

```python
async def _handle_timeout(self, agent_id: str) -> None:
    """Handle speculation timeout for the given agent.

    Executes the configured timeout policy (auto_dispatch, cancel, or notify).
    """
```

The timeout is implemented as an `asyncio.Task` created when the session starts, using `asyncio.sleep()` for the delay. If the session is resolved before the timeout fires (tool result arrives or agent cancels), the timeout task is cancelled.

---

## 9. Graph State Manager Interface

The speculative continuation manager needs to manipulate LangGraph graph state — inject messages, read conversation history, restore checkpoints. Rather than depending directly on LangGraph internals, it uses an abstraction provided by the framework adapter (spec 09).

```python
class GraphStateManager(Protocol):
    """Interface for manipulating an agent's graph state.

    Implemented by the framework adapter. Abstracts away LangGraph-specific
    state manipulation so the speculative module is framework-agnostic.
    """

    async def inject_system_message(
        self,
        agent_id: str,
        content: str,
        metadata: dict | None = None,
    ) -> str:
        """Inject a system message into the agent's conversation.

        Returns the message ID of the injected message.
        """
        ...

    async def inject_tool_result(
        self,
        agent_id: str,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> str:
        """Inject a tool result message into the agent's conversation.

        Formats the result as if the tool call completed normally.
        Returns the message ID.
        """
        ...

    async def remove_messages_after(
        self,
        agent_id: str,
        message_id: str,
    ) -> int:
        """Remove all messages after the given message ID.

        Used during rollback to strip speculative messages.
        Returns the number of messages removed.
        """
        ...

    async def get_message_count(self, agent_id: str) -> int:
        """Return the number of messages in the agent's conversation."""
        ...

    async def suspend_agent(self, agent_id: str) -> None:
        """Suspend the agent's graph execution (interrupt)."""
        ...

    async def resume_agent(self, agent_id: str) -> None:
        """Resume the agent's graph execution after suspension."""
        ...
```

---

## 10. Interaction with Other Modules

### 10.1 Confirmation Handler (spec 06)

The confirmation handler triggers speculative continuation when an agent chooses DEFER in response to a confirmation request. The confirmation handler calls `begin_speculation()` and receives the speculative prompt, which it injects into the agent's conversation before returning control to the LLM node.

### 10.2 Tool Dispatch Queue (spec 07)

When a deferred tool call is eventually dispatched (either by auto-dispatch timeout or agent re-approval), the dispatch queue executes the call and returns the result to the speculative continuation manager via `deliver_tool_result()`. The dispatch queue does not know or care whether the call was deferred — it processes it like any other call. The routing from dispatch result back to the speculative manager is handled by the orchestrator core (`fam/core.py`).

### 10.3 Budget Manager (spec 03)

Every speculative token batch triggers a `BudgetManager.deduct()` call with a `speculative=True` flag. The budget manager records these deductions separately in the agent's budget history. When the budget approaches the soft limit, the `TokenRecordResult.should_stop` flag signals the adapter to inject a budget warning. When the budget hits the hard limit, further deductions fail and the agent cannot generate more speculative tokens.

### 10.4 Pricing Engine (spec 02)

The speculative manager reads the current `reasoning_price` from the latest `PriceUpdate` to compute per-token cost. It does not interact with the pricing engine directly — it receives prices through the orchestrator's shared state.

### 10.5 Agent Communication Protocol (spec 04)

All messages injected by the speculative manager — speculative prompts, rollback notices, timeout notices, commit notices — follow the message format defined in spec 04. They are prefixed with `[FAM]` and use the system message injection path.

---

## 11. Configuration

All configuration for the speculative continuation manager is namespaced under `speculative` in the global FAM configuration (spec 12).

```yaml
speculative:
  # Enable or disable speculative continuation entirely
  enabled: true

  # Reconciliation strategy: "always_rollback" or "consistency_check"
  reconciliation_strategy: "always_rollback"

  # Maximum duration of a speculative session before timeout fires
  max_speculation_duration_seconds: 60.0

  # Timeout policy: "auto_dispatch", "cancel", or "notify"
  timeout_policy: "auto_dispatch"

  # Maximum speculative tokens per session (hard cap regardless of budget)
  max_speculative_tokens_per_session: 2000

  # Maximum concurrent speculative sessions across all agents
  max_concurrent_sessions: 50

  # Session history buffer size (for metrics and debugging)
  session_history_size: 1000

  # Consistency check settings (only used when strategy is "consistency_check")
  consistency_check:
    threshold: 0.7
    max_tokens_to_analyze: 500

  # Prompt templates (override defaults)
  prompts:
    # Templates can be overridden per tier
    quantitative: null   # null means use built-in default
    qualitative: null
    directional: null

  # Budget interaction
  budget:
    # Minimum budget balance required to start speculation
    min_balance_to_start: 5.0
    # Whether to stop speculation at soft budget limit
    stop_at_soft_limit: true
```

---

## 12. Error Handling

The speculative continuation manager handles the following error cases. The guiding principle is to preserve conversation integrity above all else — if anything goes wrong, fail safe by not speculating rather than speculating with corrupted state.

**Checkpoint creation failure:** If LangGraph's checkpointer fails to save state, the speculative session is not created. The agent is notified that speculation is unavailable and the deferred tool call proceeds to the dispatch queue for normal execution (the deferral is effectively converted to an approval). An error is logged at ERROR level.

**Checkpoint restoration failure:** If the checkpoint cannot be loaded during rollback, the rollback is aborted. The speculative messages remain in the conversation. The tool result is appended after the speculative messages with a notice: `"[FAM] Warning: Rollback failed. Tool result appended after provisional reasoning. Some earlier reasoning may be inconsistent with this result."` This is a degraded but safe fallback. An error is logged at CRITICAL level.

**Budget deduction failure during speculation:** If a budget deduction fails (insufficient balance), the `TokenRecordResult.should_stop` flag is set to True. The adapter injects a budget warning. No tokens are lost — they were already generated by the LLM; the budget deduction is an after-the-fact accounting step. The session tracks the unpaid tokens for metrics purposes.

**Duplicate session request:** If `begin_speculation()` is called for an agent that already has an active session, the request is rejected with an error result. The caller (confirmation handler) falls back to blocking the deferral and requiring the agent to approve or cancel.

**Tool result for unknown session:** If `deliver_tool_result()` is called with a `tool_call_id` that does not match any active session, the result is logged as a warning and passed through to the agent's conversation as a normal tool result. This handles race conditions where the session was cancelled just before the result arrived.

**Timeout task cancellation race:** If the tool result arrives at nearly the same time as the timeout fires, both `deliver_tool_result()` and `_handle_timeout()` may attempt to resolve the session. The session state is checked atomically (no await between check and mutation) and only the first resolver succeeds. The second sees the session is no longer ACTIVE and returns without action.

---

## 13. Metrics Emitted

The speculative continuation manager emits the following metrics to the observability system (spec 13):

**Counters:** `speculative.sessions.started` (per agent_id), `speculative.sessions.committed` (speculative tokens retained), `speculative.sessions.rolled_back` (speculative tokens discarded), `speculative.sessions.cancelled`, `speculative.sessions.timed_out` (per timeout_policy), `speculative.tokens.generated` (total speculative tokens across all sessions), `speculative.tokens.retained` (tokens kept after commit), `speculative.tokens.discarded` (tokens lost after rollback), `speculative.rollback.failures` (checkpoint restoration errors).

**Gauges:** `speculative.sessions.active` (current number of active sessions), `speculative.budget.speculative_spend` (per agent_id, cumulative speculative cost).

**Histograms:** `speculative.session.duration_seconds` (time from session start to resolution), `speculative.session.tokens` (speculative tokens per session), `speculative.session.cost` (speculative cost per session), `speculative.reconciliation.duration_ms` (time to execute reconciliation), `speculative.consistency.score` (consistency scores when using consistency-check strategy).

The ratio `speculative.tokens.discarded / speculative.tokens.generated` is the speculative waste rate — a key metric for evaluating whether the consistency-check strategy would provide value. If the waste rate is consistently low (most speculation is consistent), the always-rollback strategy is suboptimal. If the waste rate is high, always-rollback is appropriate.

---

## 14. Testing Strategy

### 14.1 Unit Tests

**Session lifecycle:** Create a session via `begin_speculation()`, verify state transitions through ACTIVE → RECONCILING → ROLLED_BACK. Verify ACTIVE → RECONCILING → COMMITTED path. Verify ACTIVE → CANCELLED path. Verify that duplicate session requests are rejected.

**Token recording:** Record tokens in an active session, verify counter increments, verify cost computation at various reasoning prices, verify budget deduction is called correctly, verify `should_stop` when budget is exhausted.

**Prompt rendering:** Render speculative prompts at each tier, verify correct template selection, verify variable substitution (tool name, price, budget, estimated wait), verify tool args truncation for long arguments.

**Consistency scoring:** For the consistency-check reconciler, provide known speculative content and tool results. Verify that clearly contradictory content scores below threshold. Verify that independent content scores above threshold. Verify that borderline cases are handled by the threshold parameter.

**Checkpoint manager:** Create a checkpoint, verify it stores the expected state. Restore a checkpoint, verify the returned state matches the original. Discard a checkpoint, verify it cannot be restored after discarding.

### 14.2 Integration Tests

**Full speculation-and-rollback cycle:** Register an agent, start a speculative session, record tokens, deliver a tool result, verify rollback occurs (state is restored, speculative messages removed, tool result injected). Verify budget reflects speculative spend (not refunded).

**Full speculation-and-commit cycle:** Enable consistency-check strategy, create a session with speculation independent of the tool result, deliver the result, verify commit occurs (speculative messages retained, tool result appended).

**Timeout handling:** Start a session with a short timeout, wait for timeout to fire, verify configured timeout policy executes correctly (auto_dispatch / cancel / notify). Verify timeout task is cancelled when result arrives before timeout.

**Budget exhaustion during speculation:** Start a session with a nearly-exhausted budget, record tokens until budget is insufficient, verify `should_stop` is returned, verify the session can still be resolved normally when the tool result arrives.

**Concurrent sessions across agents:** Start speculative sessions for multiple agents concurrently, verify session isolation (one agent's reconciliation does not affect another's), verify concurrent checkpoint creation and restoration.

### 14.3 Property-Based Tests

Use Hypothesis to generate random sequences of `record_speculative_tokens` calls with random token counts and reasoning prices. Verify that session totals match sum of individual recordings. Verify that cost is always `tokens × price` for each recording. Verify that session state transitions are monotonic (never go backward in the lifecycle).

Generate random speculative content and tool results. Verify that the consistency scorer always returns a value in [0.0, 1.0]. Verify that the reconciliation result always reports tokens_retained + tokens_discarded = total tokens generated.

---

## 15. Dependencies

**Internal:** `fam/types.py` (shared types, `PriceUpdate`), `fam/budget/manager.py` (budget deductions), `fam/signals/formatter.py` (tier lookup for prompt rendering), `fam/signals/protocol.py` (message format), `fam/config/schema.py` (configuration schema).

**External:** `langgraph` — specifically the checkpoint infrastructure (`BaseCheckpointSaver`, `MemorySaver`). This is the only module in the orchestration layer with a direct LangGraph dependency (other modules interact with LangGraph through the adapter). The dependency is on the checkpointing interface, not on graph execution, so it is narrow and stable.

**Standard library:** `asyncio`, `dataclasses`, `collections.deque`, `decimal`, `hashlib` (for content hashing in consistency check), `time`, `logging`, `enum`, `uuid` (for session IDs).

---

## 16. Open Questions

**Partial rollback.** The current design rolls back the entire speculative sequence or keeps it entirely. A more nuanced approach would roll back only the portion of speculation that depends on the tool result, retaining independent reasoning. This requires dependency tracking within the speculative content, which is a hard problem for free-form text. Deferred to a future version — the evaluation harness should first measure how large the speculative sequences are in practice.

**Multi-tool speculation.** What happens when an agent defers two tool calls and wants to speculate on both? The current design supports only one active session per agent. Supporting multiple concurrent speculative sessions would require tracking which speculative tokens depend on which pending tool call, and handling partial rollbacks when one result arrives before the other. This is a significant complexity increase with unclear benefit. Deferred.

**Speculative branching.** Instead of a single speculative sequence, the agent could explore multiple contingency branches ("if the tool returns X, then ...; if Y, then ..."). On result arrival, the matching branch is committed and others are discarded. This requires a tree-structured conversation state, which LangGraph does not natively support. Deferred.

**Consistency check using the LLM itself.** Instead of a heuristic string-matching consistency check, the actual LLM could be asked "Is your provisional reasoning consistent with this tool result?" This is more accurate but costs additional inference tokens and introduces a recursive pricing problem (the consistency check itself consumes GPU). Deferred, but could be a useful experiment for spec 14.

**Speculative cost discounting.** Should speculative tokens be priced at a discount relative to normal reasoning tokens, since they might be discarded? A discount would encourage more speculation (useful when GPU is underutilized) but complicate the pricing model. The current design charges full reasoning price, which is simple and creates clean incentives. The pricing engine could be extended with a `speculative_discount_factor` if experiments show that agents speculate too little.
