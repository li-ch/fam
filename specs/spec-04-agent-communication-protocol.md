# Spec 04 — Agent Communication Protocol

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview), Spec 05 (Tiered Signal Formatter), Spec 09 (Framework Adapters)
**Consumed By:** Spec 06 (Confirmation Handler), Spec 08 (Speculative Continuation Manager), Spec 09 (Framework Adapters)

---

## 1. Purpose

The agent communication protocol defines how the FAM orchestrator talks to agents and how agents talk back. Every interaction between the orchestrator and an agent — pricing updates, confirmation requests, deferral notices, tool result injections — passes through this protocol. It is the linguistic contract between the market mechanism and the LLM.

This is a fundamentally unusual protocol because one side is a deterministic software system and the other is a stochastic language model. The orchestrator sends structured messages that must be parseable by an LLM within a conversational context. The agent responds in free-form natural language that must be reliably parsed back into discrete decisions. The protocol must bridge this impedance mismatch without modifying the agent's core prompts or requiring agents to be trained on FAM-specific syntax.

This spec defines every message type, the injection mechanism for each framework adapter, the message templates for each capability tier, and the parsing logic for extracting agent decisions from free-form text.

---

## 2. Module Location

```
fam/
├── signals/
│   ├── __init__.py
│   ├── protocol.py        # MessageType enum, message dataclasses, protocol constants
│   ├── formatter.py        # Tiered signal formatting (spec 05)
│   ├── templates.py        # Message template strings per tier per message type
│   ├── injection.py        # Injection strategy per framework adapter
│   └── parser.py           # Response parsing logic for agent decisions
```

Tests live in `tests/test_signals/`.

---

## 3. Message Types

FAM defines eight message types. Each type has a specific direction (orchestrator-to-agent, agent-to-orchestrator, or bidirectional), a trigger condition, and a structured payload.

### 3.1 Message Type Enum

```python
class MessageType(str, Enum):
    PRICING_UPDATE = "pricing_update"
    TOOL_INTERCEPT_ACK = "tool_intercept_ack"
    CONFIRMATION_REQUEST = "confirmation_request"
    CONFIRMATION_RESPONSE = "confirmation_response"
    DEFERRAL_NOTICE = "deferral_notice"
    SPECULATIVE_PROMPT = "speculative_prompt"
    TOOL_RESULT_INJECTION = "tool_result_injection"
    BUDGET_WARNING = "budget_warning"
```

### 3.2 Pricing Update

**Direction:** Orchestrator → Agent
**Trigger:** Published on each pricing tick (default 1 second), injected into agent context before the next LLM invocation.

The pricing update informs the agent of current resource costs. The content varies by capability tier (spec 05) — from precise numeric tables to simple directional nudges.

```python
@dataclass(frozen=True)
class PricingUpdateMessage:
    message_type: MessageType = field(default=MessageType.PRICING_UPDATE, init=False)
    reasoning_price: Decimal
    tool_prices: dict[str, Decimal]       # endpoint_id → current price
    budget_remaining: Decimal
    replenishment_rate: Decimal            # units per second
    gpu_utilization: float                 # 0.0–1.0
    tool_utilizations: dict[str, float]    # endpoint_id → 0.0–1.0
    estimated_runway_seconds: float        # budget_remaining / current_spend_rate
    timestamp: float
```

Not every pricing update is injected into the agent. To avoid flooding the context window, pricing updates are injected only when the price has changed by more than `price_change_threshold` (default: 10% relative change) since the last injected update, or when `max_update_interval_seconds` (default: 10.0) has elapsed since the last injection, whichever comes first. The injection filter is applied per-agent because each agent may have received its last update at a different time.

### 3.3 Tool Intercept Acknowledgment

**Direction:** Orchestrator → Agent
**Trigger:** Immediately after the orchestrator intercepts a tool call from the agent, before confirmation evaluation begins.

This message acknowledges that the tool call was received and is being evaluated. It exists to prevent the agent from interpreting silence as a failure. For fast confirmation paths (auto-approve), this message and the tool result may arrive in the same LLM turn, effectively collapsing the acknowledgment into the result.

```python
@dataclass(frozen=True)
class ToolInterceptAckMessage:
    message_type: MessageType = field(default=MessageType.TOOL_INTERCEPT_ACK, init=False)
    tool_name: str
    endpoint_id: str
    agent_id: str
    timestamp: float
```

### 3.4 Confirmation Request

**Direction:** Orchestrator → Agent
**Trigger:** When a tool call's price exceeds the auto-approve threshold and the agent has sufficient budget.

This is the most important message type. It presents the agent with the cost of proceeding and enough context to make an informed decision. The agent must respond with one of three decisions: confirm, cancel, or reason-more (defer to speculative continuation).

```python
@dataclass(frozen=True)
class ConfirmationRequestMessage:
    message_type: MessageType = field(default=MessageType.CONFIRMATION_REQUEST, init=False)
    tool_name: str
    endpoint_id: str
    tool_call_id: str                     # Unique ID for this specific tool call
    cost: Decimal                          # Current price for this tool call
    budget_remaining: Decimal              # Agent's balance before deduction
    budget_after_deduction: Decimal        # Balance if agent confirms
    estimated_wait_time_seconds: float     # Predicted dispatch latency
    queue_depth: int                       # Current queue depth for this endpoint
    alternative_suggestion: str | None     # Formatted suggestion for cheaper path
    replenishment_rate: Decimal
    replenishment_eta_seconds: float       # Time to replenish the cost if deferred
    timestamp: float
```

The `alternative_suggestion` field is populated when the signal formatter determines a cheaper alternative exists — for example, "Reasoning tokens are currently 3× cheaper than tool calls. Consider thinking through the problem further before calling the tool." The suggestion is tier-aware: quantitative agents see numeric comparisons, directional agents see simple behavioral nudges.

### 3.5 Confirmation Response

**Direction:** Agent → Orchestrator
**Trigger:** After the agent receives a confirmation request.

The agent's response is free-form text. The protocol parser (section 7) extracts a structured decision from it.

```python
class ConfirmationDecision(str, Enum):
    CONFIRM = "confirm"
    CANCEL = "cancel"
    REASON_MORE = "reason_more"

@dataclass(frozen=True)
class ConfirmationResponseMessage:
    message_type: MessageType = field(default=MessageType.CONFIRMATION_RESPONSE, init=False)
    tool_call_id: str
    decision: ConfirmationDecision
    raw_text: str                          # The agent's unprocessed response
    confidence: float                      # Parser's confidence in the decision (0.0–1.0)
    timestamp: float
```

The `confidence` field reflects how unambiguous the agent's text was. If the agent says "Yes, proceed with the tool call," confidence is high (>0.9). If the agent says something ambiguous like "I suppose we could try," confidence is moderate (0.5–0.8). If the parser cannot determine a decision, confidence is 0.0 and the `decision` field falls back to the configured default policy (section 7.4).

### 3.6 Deferral Notice

**Direction:** Orchestrator → Agent
**Trigger:** When a confirmed tool call is deferred by the dispatch queue because the endpoint is congested.

```python
@dataclass(frozen=True)
class DeferralNoticeMessage:
    message_type: MessageType = field(default=MessageType.DEFERRAL_NOTICE, init=False)
    tool_call_id: str
    tool_name: str
    reason: str                            # "queue_depth_exceeded", "rate_limit", "endpoint_unhealthy"
    estimated_wait_time_seconds: float | None
    position_in_queue: int | None
    timestamp: float
```

### 3.7 Speculative Continuation Prompt

**Direction:** Orchestrator → Agent
**Trigger:** When the speculative continuation manager (spec 08) initiates provisional reasoning after a deferral or after the agent chooses "reason_more."

```python
@dataclass(frozen=True)
class SpeculativeContinuationPromptMessage:
    message_type: MessageType = field(default=MessageType.SPECULATIVE_PROMPT, init=False)
    tool_call_id: str
    tool_name: str
    current_reasoning_price: Decimal
    budget_remaining: Decimal
    instruction: str                       # Tier-formatted instruction to continue reasoning
    timestamp: float
```

The `instruction` field contains the formatted prompt encouraging the agent to continue reasoning while the tool result is pending. Example for the quantitative tier: "Your call to `search_api` is queued (position 3). Current reasoning cost: 0.02/token. Budget remaining: 45.3. You can continue reasoning provisionally — speculative tokens are charged at the current reasoning price and may be rolled back when the tool result arrives."

### 3.8 Deferred Tool Result Injection

**Direction:** Orchestrator → Agent
**Trigger:** When a previously deferred tool call completes and the result needs to be injected into the agent's conversation.

```python
@dataclass(frozen=True)
class ToolResultInjectionMessage:
    message_type: MessageType = field(default=MessageType.TOOL_RESULT_INJECTION, init=False)
    tool_call_id: str
    tool_name: str
    result: Any                            # The tool's return value
    was_speculative: bool                  # Whether agent generated speculative tokens
    speculative_tokens_discarded: int      # Tokens rolled back (0 if none)
    latency_ms: float                      # Total time from dispatch to result
    cost_charged: Decimal                  # Actual price charged (may differ from quoted)
    timestamp: float
```

When `was_speculative` is True and `speculative_tokens_discarded > 0`, the injection message includes an explanatory prefix informing the agent that some of its provisional reasoning was discarded because the actual tool result arrived.

### 3.9 Budget Warning

**Direction:** Orchestrator → Agent
**Trigger:** When the agent's budget drops below the configured soft warning threshold.

```python
@dataclass(frozen=True)
class BudgetWarningMessage:
    message_type: MessageType = field(default=MessageType.BUDGET_WARNING, init=False)
    agent_id: str
    budget_remaining: Decimal
    warning_threshold: Decimal
    replenishment_rate: Decimal
    estimated_exhaustion_seconds: float | None   # None if spend rate is zero
    recommendation: str                          # Tier-formatted conservation advice
    timestamp: float
```

---

## 4. Message Envelope

All FAM messages are wrapped in a common envelope before injection into the agent's conversation. The envelope provides a consistent framing that agents can learn to recognize.

```python
@dataclass(frozen=True)
class FAMMessageEnvelope:
    """Wrapper for all FAM messages injected into agent conversations."""
    prefix: str = "[FAM]"                  # Fixed identifier prefix
    message_type: MessageType = MessageType.PRICING_UPDATE
    payload: str = ""                      # The formatted, tier-appropriate message body
    metadata: dict[str, Any] = field(default_factory=dict)  # Machine-readable fields for parsing
```

The `prefix` is always `[FAM]`. This is a hard-coded constant — it is never configurable, because agents and parsers both rely on it for message boundary detection. The `payload` is the human/LLM-readable message body, formatted by the tiered signal formatter (spec 05). The `metadata` dict contains the raw structured data from the message dataclass, serialized as key-value pairs, for use by the response parser and for logging.

The rendered form of a message (what the LLM actually sees) is:

```
[FAM] {message_type_label}
{payload}
```

Example for a quantitative-tier confirmation request:

```
[FAM] Confirmation Required
Tool: search_api ("web_search")
Cost: 3.45 units
Your budget: 52.10 units (48.65 after this call)
Queue depth: 7 | Estimated wait: 2.3s
Replenishment: +1.0 units/sec (cost recovers in 3.5s)
Alternative: Reasoning is currently cheap (0.02/token). Consider thinking further before calling.
→ Reply CONFIRM to proceed, CANCEL to abort, or REASON to continue thinking.
```

---

## 5. Injection Mechanisms

Messages must be injected into the agent's conversational context in a way that the LLM can perceive and respond to. The injection mechanism depends on the agent framework. This section defines the abstract injection interface and the LangGraph-specific implementation.

### 5.1 Injection Strategy Interface

```python
class InjectionStrategy(Protocol):
    """How a FAM message is placed into the agent's conversation context."""

    def inject_system_message(
        self,
        agent_id: str,
        envelope: FAMMessageEnvelope,
    ) -> None:
        """Inject as a system message. Used for pricing updates and warnings."""
        ...

    def inject_user_message(
        self,
        agent_id: str,
        envelope: FAMMessageEnvelope,
    ) -> None:
        """Inject as a user message. Used for confirmation requests."""
        ...

    def inject_tool_result(
        self,
        agent_id: str,
        envelope: FAMMessageEnvelope,
        tool_call_id: str,
    ) -> None:
        """Inject as a tool-result-formatted message. Used for tool result injection."""
        ...

    def read_agent_response(
        self,
        agent_id: str,
    ) -> str | None:
        """Read the agent's most recent text response for parsing.

        Returns None if no new response is available.
        """
        ...
```

### 5.2 Injection Type by Message Type

Different message types use different injection strategies because LLMs interpret message roles differently.

| Message Type | Injection As | Rationale |
|---|---|---|
| `PRICING_UPDATE` | System message | Pricing context should inform without prompting a direct reply |
| `TOOL_INTERCEPT_ACK` | System message | Informational; agent should not respond to it |
| `CONFIRMATION_REQUEST` | User message | Agent must respond to it; user-role messages elicit responses |
| `DEFERRAL_NOTICE` | System message | Informational context for the agent's next reasoning step |
| `SPECULATIVE_PROMPT` | User message | Prompts the agent to continue generating; user-role triggers generation |
| `TOOL_RESULT_INJECTION` | Tool result | Must appear in the tool-result slot so the agent processes it as tool output |
| `BUDGET_WARNING` | System message | Advisory context, no response required |

### 5.3 LangGraph Injection

In LangGraph, injection works through the graph state. The LangGraph adapter (spec 09) extends the agent's `MessagesState` with a `fam_messages` channel. FAM nodes write to this channel, and a state reducer merges FAM messages into the agent's message history at the appropriate positions.

For **system messages**, the FAM node appends a `SystemMessage` with the rendered envelope to the messages list. LangGraph's message reducer handles deduplication and ordering.

For **user messages** (confirmation requests, speculative prompts), the FAM node uses LangGraph's `interrupt()` mechanism. The graph execution pauses, the confirmation request is presented as a `HumanMessage`, the agent's LLM generates a response, and the FAM node captures the response for parsing before resuming graph execution.

For **tool result messages**, the FAM node constructs a `ToolMessage` with the appropriate `tool_call_id` matching the original tool call. This ensures the LLM's tool-use protocol is respected — the model sees a well-formed tool invocation → tool result sequence.

```python
@dataclass
class LangGraphInjector:
    """LangGraph-specific message injection implementation."""

    def inject_system_message(
        self,
        agent_id: str,
        envelope: FAMMessageEnvelope,
    ) -> None:
        rendered = self._render(envelope)
        msg = SystemMessage(content=rendered, additional_kwargs={"fam": True})
        self._append_to_state(agent_id, msg)

    def inject_user_message(
        self,
        agent_id: str,
        envelope: FAMMessageEnvelope,
    ) -> None:
        rendered = self._render(envelope)
        msg = HumanMessage(content=rendered, additional_kwargs={"fam": True})
        self._append_to_state(agent_id, msg)

    def inject_tool_result(
        self,
        agent_id: str,
        envelope: FAMMessageEnvelope,
        tool_call_id: str,
    ) -> None:
        rendered = self._render(envelope)
        msg = ToolMessage(
            content=rendered,
            tool_call_id=tool_call_id,
            additional_kwargs={"fam": True},
        )
        self._append_to_state(agent_id, msg)

    def _render(self, envelope: FAMMessageEnvelope) -> str:
        return f"{envelope.prefix} {envelope.message_type.value}\n{envelope.payload}"

    def _append_to_state(self, agent_id: str, msg: BaseMessage) -> None:
        """Append message to the agent's LangGraph state messages list.

        The actual implementation accesses the graph state via the adapter's
        state reference, which is set by the LangGraph adapter at node entry.
        """
        ...
```

The `additional_kwargs={"fam": True}` tag on every injected message allows downstream processing (and the agent itself, if instructed) to distinguish FAM-injected messages from organic conversation messages. This tag is also used by the metrics system to count injection frequency.

---

## 6. Message Templates

Each message type has a template for each capability tier. Templates are stored in `fam/signals/templates.py` as plain Python string constants (not Jinja2 or other template engines — the templates are simple enough that f-string formatting suffices, and avoiding a template engine dependency keeps the module lightweight).

### 6.1 Confirmation Request Templates

**Quantitative tier:**

```python
CONFIRMATION_QUANTITATIVE = """\
Tool: {endpoint_id} ("{tool_name}")
Cost: {cost} units
Your budget: {budget_remaining} units ({budget_after_deduction} after this call)
Queue depth: {queue_depth} | Estimated wait: {estimated_wait_time_seconds:.1f}s
Replenishment: +{replenishment_rate} units/sec (cost recovers in {replenishment_eta_seconds:.1f}s)
{alternative_line}
→ Reply CONFIRM to proceed, CANCEL to abort, or REASON to continue thinking."""
```

**Qualitative tier:**

```python
CONFIRMATION_QUALITATIVE = """\
Tool: {tool_name}
Cost: {cost_label} ({cost_context})
Budget status: {budget_label}
Wait time: {wait_label}
{alternative_line}
→ Would you like to proceed, cancel, or continue reasoning instead?"""
```

**Directional tier:**

```python
CONFIRMATION_DIRECTIONAL = """\
Calling {tool_name} is currently {cost_direction}.
{nudge}
→ Proceed, cancel, or think more?"""
```

### 6.2 Pricing Update Templates

**Quantitative tier:**

```python
PRICING_UPDATE_QUANTITATIVE = """\
Current prices — Reasoning: {reasoning_price}/token | Tools: {tool_price_summary}
Budget: {budget_remaining} units | Replenishment: +{replenishment_rate}/sec
Runway: ~{estimated_runway_seconds:.0f}s at current spend rate
GPU load: {gpu_utilization_pct:.0f}%"""
```

**Qualitative tier:**

```python
PRICING_UPDATE_QUALITATIVE = """\
Resource status — Reasoning: {reasoning_label} | Tools: {tool_label}
Budget: {budget_label}
{advice}"""
```

**Directional tier:**

```python
PRICING_UPDATE_DIRECTIONAL = """\
{nudge}"""
```

Directional pricing updates are minimal — a single sentence like "Resources are abundant right now" or "Tool calls are expensive; consider reasoning more." If neither resource is notably scarce or abundant, the directional update is suppressed entirely (not injected).

### 6.3 Speculative Continuation Prompt Templates

**Quantitative tier:**

```python
SPECULATIVE_QUANTITATIVE = """\
Your call to {tool_name} is queued (position {position}).
Current reasoning cost: {reasoning_price}/token | Budget: {budget_remaining} units
Continue reasoning provisionally. Speculative tokens cost {reasoning_price}/token \
and may be rolled back when the result arrives."""
```

**Qualitative tier:**

```python
SPECULATIVE_QUALITATIVE = """\
Your {tool_name} call is waiting. Reasoning is {reasoning_label} right now.
Continue thinking — your provisional reasoning may be revised when the result arrives."""
```

**Directional tier:**

```python
SPECULATIVE_DIRECTIONAL = """\
{tool_name} is busy. Keep thinking for now."""
```

### 6.4 Budget Warning Templates

**Quantitative tier:**

```python
BUDGET_WARNING_QUANTITATIVE = """\
Budget alert: {budget_remaining} units remaining (threshold: {warning_threshold})
At current spend rate, exhaustion in ~{estimated_exhaustion_seconds:.0f}s
Replenishment: +{replenishment_rate}/sec
Consider reducing tool calls or waiting for replenishment."""
```

**Qualitative tier:**

```python
BUDGET_WARNING_QUALITATIVE = """\
Budget is {budget_label}. {recommendation}"""
```

**Directional tier:**

```python
BUDGET_WARNING_DIRECTIONAL = """\
Budget is low. Conserve resources."""
```

### 6.5 Tool Result Injection Templates

Tool result injection templates are consistent across tiers because the tool result itself is the primary content. The FAM wrapper is a brief preamble:

```python
TOOL_RESULT_STANDARD = """\
{result}"""

TOOL_RESULT_AFTER_SPECULATION = """\
[Tool result arrived. {discarded_note}]
{result}"""
```

The `discarded_note` is included only when speculative tokens were rolled back: "Your last {speculative_tokens_discarded} tokens of provisional reasoning were discarded."

### 6.6 Label Mappings for Qualitative and Directional Tiers

The qualitative tier maps numeric values to natural-language labels. The directional tier maps to simple behavioral nudges.

```python
@dataclass(frozen=True)
class QualitativeLabels:
    """Thresholds for mapping numeric values to qualitative labels."""
    cost_thresholds: tuple[tuple[float, str], ...] = (
        (0.3, "low"),
        (0.6, "moderate"),
        (0.8, "high"),
        (1.0, "very high"),
    )
    budget_thresholds: tuple[tuple[float, str], ...] = (
        (0.2, "critically low"),
        (0.4, "low"),
        (0.7, "adequate"),
        (1.0, "comfortable"),
    )
    wait_thresholds: tuple[tuple[float, str], ...] = (
        (1.0, "minimal"),
        (3.0, "short"),
        (10.0, "moderate"),
        (float("inf"), "long"),
    )
    utilization_thresholds: tuple[tuple[float, str], ...] = (
        (0.3, "low"),
        (0.6, "moderate"),
        (0.8, "high"),
        (1.0, "very high"),
    )
```

The label is determined by finding the first threshold the value falls below. For example, a cost of 0.45 maps to "moderate" because 0.45 < 0.6. These thresholds are configurable (spec 12).

Directional nudges are selected from a fixed set based on the overall resource state:

```python
DIRECTIONAL_NUDGES: dict[str, str] = {
    "tools_expensive_reasoning_cheap": "Consider reasoning more before calling tools.",
    "tools_cheap_reasoning_expensive": "Tool calls are cheap right now — good time to use them.",
    "both_expensive": "Resources are scarce. Be conservative.",
    "both_cheap": "Resources are abundant right now.",
    "budget_low": "Your budget is running low. Conserve resources.",
    "neutral": "",  # Empty string → suppress injection
}
```

---

## 7. Response Parsing

When the agent responds to a confirmation request, its response is free-form text. The parser must extract a `ConfirmationDecision` from this text reliably. This is the hardest part of the protocol — the parsing logic must handle the full range of LLM response styles.

### 7.1 Parsing Strategy

The parser uses a multi-stage approach, from most specific to most general:

1. **Exact keyword match:** Scan for the exact tokens `CONFIRM`, `CANCEL`, `REASON` (case-insensitive). These are the keywords suggested in the confirmation request template. If found, return the corresponding decision with confidence 0.95.

2. **Synonym match:** Scan for synonyms and common phrasings. "Yes," "proceed," "go ahead," "do it," "approve" → CONFIRM. "No," "stop," "abort," "don't," "skip" → CANCEL. "Think," "wait," "defer," "let me think," "reason more" → REASON_MORE. Return with confidence 0.85.

3. **Sentiment analysis (lightweight):** If no keywords or synonyms are found, check the first sentence for affirmative or negative framing. Sentences starting with "I'll," "Let's," "Sure" lean CONFIRM. Sentences starting with "I don't," "No," "Actually" lean CANCEL. Sentences with "maybe," "let me," "I could" lean REASON_MORE. Return with confidence 0.60.

4. **Fallback:** If none of the above yields a result, return the configured default decision with confidence 0.0.

### 7.2 Parser Implementation

```python
class ConfirmationResponseParser:
    """Extracts structured decisions from free-form agent text."""

    def __init__(self, config: ParserConfig) -> None:
        self._config = config
        self._confirm_patterns: list[re.Pattern] = self._compile_patterns(
            config.confirm_keywords
        )
        self._cancel_patterns: list[re.Pattern] = self._compile_patterns(
            config.cancel_keywords
        )
        self._reason_patterns: list[re.Pattern] = self._compile_patterns(
            config.reason_keywords
        )

    def parse(self, raw_text: str) -> ConfirmationResponseMessage:
        """Parse the agent's free-form text into a structured decision.

        Returns a ConfirmationResponseMessage with the extracted decision
        and confidence score.
        """
        text = raw_text.strip()
        if not text:
            return self._make_response(
                text, self._config.default_decision, confidence=0.0
            )

        # Stage 1: exact keyword match
        decision, confidence = self._exact_match(text)
        if decision is not None:
            return self._make_response(text, decision, confidence)

        # Stage 2: synonym match
        decision, confidence = self._synonym_match(text)
        if decision is not None:
            return self._make_response(text, decision, confidence)

        # Stage 3: sentiment heuristic
        decision, confidence = self._sentiment_heuristic(text)
        if decision is not None:
            return self._make_response(text, decision, confidence)

        # Stage 4: fallback
        return self._make_response(
            text, self._config.default_decision, confidence=0.0
        )

    def _exact_match(self, text: str) -> tuple[ConfirmationDecision | None, float]:
        upper = text.upper()
        if "CONFIRM" in upper:
            return ConfirmationDecision.CONFIRM, 0.95
        if "CANCEL" in upper:
            return ConfirmationDecision.CANCEL, 0.95
        if "REASON" in upper:
            return ConfirmationDecision.REASON_MORE, 0.95
        return None, 0.0

    def _synonym_match(
        self, text: str
    ) -> tuple[ConfirmationDecision | None, float]:
        lower = text.lower()
        for pattern in self._confirm_patterns:
            if pattern.search(lower):
                return ConfirmationDecision.CONFIRM, 0.85
        for pattern in self._cancel_patterns:
            if pattern.search(lower):
                return ConfirmationDecision.CANCEL, 0.85
        for pattern in self._reason_patterns:
            if pattern.search(lower):
                return ConfirmationDecision.REASON_MORE, 0.85
        return None, 0.0

    def _sentiment_heuristic(
        self, text: str
    ) -> tuple[ConfirmationDecision | None, float]:
        first_sentence = text.split(".")[0].strip().lower()
        affirmative_starts = ("i'll", "let's", "sure", "okay", "ok", "yes", "yeah")
        negative_starts = ("i don't", "no", "actually", "i won't", "don't", "nah")
        deferring_starts = ("maybe", "let me", "i could", "perhaps", "i need to think")

        for prefix in affirmative_starts:
            if first_sentence.startswith(prefix):
                return ConfirmationDecision.CONFIRM, 0.60
        for prefix in negative_starts:
            if first_sentence.startswith(prefix):
                return ConfirmationDecision.CANCEL, 0.60
        for prefix in deferring_starts:
            if first_sentence.startswith(prefix):
                return ConfirmationDecision.REASON_MORE, 0.60
        return None, 0.0

    @staticmethod
    def _compile_patterns(keywords: list[str]) -> list[re.Pattern]:
        return [re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE) for kw in keywords]

    def _make_response(
        self,
        raw_text: str,
        decision: ConfirmationDecision,
        confidence: float,
    ) -> ConfirmationResponseMessage:
        return ConfirmationResponseMessage(
            tool_call_id="",  # Set by caller
            decision=decision,
            raw_text=raw_text,
            confidence=confidence,
            timestamp=0.0,    # Set by caller
        )
```

### 7.3 Default Keyword Lists

```python
@dataclass(frozen=True)
class ParserConfig:
    confirm_keywords: list[str] = field(default_factory=lambda: [
        "yes", "proceed", "go ahead", "do it", "approve", "confirmed",
        "execute", "run it", "continue with", "let's do it", "affirmative",
    ])
    cancel_keywords: list[str] = field(default_factory=lambda: [
        "no", "stop", "abort", "skip", "cancel", "don't", "do not",
        "negative", "pass", "forget it", "never mind",
    ])
    reason_keywords: list[str] = field(default_factory=lambda: [
        "think", "wait", "defer", "reason more", "let me think",
        "hold on", "reconsider", "think about", "reason first",
        "think further", "think more", "reasoning",
    ])
    default_decision: ConfirmationDecision = ConfirmationDecision.CONFIRM
    low_confidence_threshold: float = 0.5
```

### 7.4 Low-Confidence Handling

When the parser's confidence is below `low_confidence_threshold` (default 0.5), the decision is logged as ambiguous. The confirmation handler (spec 06) applies the configured policy for ambiguous responses:

- **`use_default`:** Apply `default_decision` (default: CONFIRM). Rationale: most agents intend to use the tool they requested, and ambiguity often comes from verbose agreement.
- **`cancel`:** Treat ambiguity as cancellation. More conservative — avoids spending budget on uncertain intent.
- **`retry`:** Re-inject the confirmation request with a simplified prompt: "Please reply with exactly CONFIRM, CANCEL, or REASON." This costs an extra LLM turn but improves parsing reliability. Max one retry to prevent infinite loops.

---

## 8. Message Ordering and Deduplication

### 8.1 Ordering Guarantees

Within a single agent's conversation, FAM messages are injected in the order they are generated. The protocol guarantees:

1. A `TOOL_INTERCEPT_ACK` always precedes the corresponding `CONFIRMATION_REQUEST`.
2. A `CONFIRMATION_REQUEST` always precedes the corresponding `CONFIRMATION_RESPONSE`.
3. A `DEFERRAL_NOTICE` always precedes the corresponding `SPECULATIVE_PROMPT`.
4. A `TOOL_RESULT_INJECTION` is always the last message for a given `tool_call_id`.

These invariants are enforced by the confirmation handler and speculative continuation manager, not by the protocol module itself. The protocol module is stateless — it formats and injects messages but does not track message sequences.

### 8.2 Deduplication

Pricing updates are deduplicated by the injection filter (section 3.2): if the new pricing update is identical to the last injected one (within a configurable epsilon for float comparison), it is suppressed. All other message types are never deduplicated — every confirmation request, every deferral notice, and every tool result is injected exactly once.

### 8.3 Context Window Management

Injected messages consume context window tokens. To prevent FAM messages from crowding out the agent's reasoning, the protocol enforces a `max_fam_messages_in_context` limit (default: 20). When this limit is reached, the oldest FAM system messages (pricing updates and budget warnings) are evicted from the message history. Confirmation requests/responses and tool results are never evicted, as they are part of the logical tool-use flow.

The eviction logic is implemented in the LangGraph adapter (spec 09) via a custom message reducer.

---

## 9. Configuration

All configuration for the agent communication protocol is namespaced under `protocol` in the global FAM configuration (spec 12).

```yaml
protocol:
  injection:
    price_change_threshold: 0.10          # Relative price change to trigger re-injection
    max_update_interval_seconds: 10.0     # Maximum time between pricing injections
    max_fam_messages_in_context: 20       # Eviction threshold for FAM messages

  parser:
    confirm_keywords: ["yes", "proceed", "go ahead", "do it", "approve",
                        "confirmed", "execute", "run it", "affirmative"]
    cancel_keywords: ["no", "stop", "abort", "skip", "cancel", "don't",
                       "negative", "pass"]
    reason_keywords: ["think", "wait", "defer", "reason more",
                       "let me think", "hold on", "reason first"]
    default_decision: "confirm"            # "confirm", "cancel", or "reason_more"
    low_confidence_threshold: 0.5
    low_confidence_policy: "use_default"   # "use_default", "cancel", or "retry"
    max_retry_count: 1

  qualitative_labels:
    cost:   [[0.3, "low"], [0.6, "moderate"], [0.8, "high"], [1.0, "very high"]]
    budget: [[0.2, "critically low"], [0.4, "low"], [0.7, "adequate"], [1.0, "comfortable"]]
    wait:   [[1.0, "minimal"], [3.0, "short"], [10.0, "moderate"], [.inf, "long"]]

  prefix: "[FAM]"
```

---

## 10. Error Handling

The communication protocol is designed to degrade gracefully when message injection or parsing fails.

**Injection failures:** If message injection raises an exception (e.g., the LangGraph state is in an unexpected shape), the error is caught and logged at ERROR level. The message is dropped. For confirmation requests specifically, a dropped injection means the agent never sees the request — the confirmation handler applies the default policy (typically auto-confirm) after a timeout. The failed injection is counted in metrics.

**Parse failures:** If the parser encounters text it cannot process (e.g., empty string, binary data, extremely long text), it returns the default decision with confidence 0.0. The raw text is logged in full for debugging. No exception is raised.

**Template rendering failures:** If a template references a field not present in the payload (e.g., due to a version mismatch), the renderer catches `KeyError` and falls back to a generic template that presents only the fields available. This prevents a template change from breaking the entire confirmation flow.

**Encoding issues:** All text is UTF-8. If the agent's response contains non-UTF-8 bytes (should not happen with standard LLMs), the parser uses `errors="replace"` decoding and logs a warning.

---

## 11. Metrics Emitted

The protocol module emits the following metrics to the observability system (spec 13):

**Counters:** `protocol.messages.injected` (per message_type, per agent_id), `protocol.messages.suppressed` (pricing updates that were filtered out), `protocol.messages.evicted` (old FAM messages removed from context), `protocol.parse.total` (per agent_id), `protocol.parse.fallback` (per agent_id, times the parser used the default decision), `protocol.parse.retry` (per agent_id, times a retry prompt was needed), `protocol.injection.failed` (per message_type, per agent_id).

**Gauges:** `protocol.fam_messages_in_context` (per agent_id, current count of FAM messages in the agent's context), `protocol.parse.confidence` (per agent_id, last parse confidence score).

**Histograms:** `protocol.parse.latency_us` (per agent_id, time to parse a response in microseconds), `protocol.injection.latency_us` (per message_type, time to inject a message).

---

## 12. Testing Strategy

### 12.1 Unit Tests

**Message dataclass construction:** Verify all message dataclasses can be constructed with valid inputs and that frozen invariants hold. Verify `MessageType` enum values match expected strings.

**Template rendering:** For each message type × each tier, render the template with known inputs and verify the output matches expected strings exactly. Test edge cases: zero budget, very large numbers, empty tool prices dict, None alternative suggestion.

**Parser exact match:** Feed the parser strings containing exact keywords ("CONFIRM," "CANCEL," "REASON") in various positions (start of text, middle, end) and verify correct decision extraction with high confidence.

**Parser synonym match:** Feed common phrasings ("Yes, proceed with the tool call," "No, let's skip this one," "Let me think about it") and verify correct decisions.

**Parser sentiment heuristic:** Feed ambiguous text that falls through to sentiment analysis and verify reasonable decisions. Test edge cases: single-word responses, all-caps, all-lowercase, text with no clear sentiment.

**Parser fallback:** Feed completely unrelated text ("The quick brown fox") and verify the default decision is returned with confidence 0.0.

**Label mapping:** Verify that numeric values map to the correct qualitative labels at threshold boundaries. Test exact boundary values (0.3 should map to "low," 0.30001 should map to "moderate").

### 12.2 Integration Tests

**Injection round-trip (LangGraph):** Create a minimal LangGraph graph, inject a system message via the LangGraph injector, verify the message appears in the graph state's messages list with the correct role and content.

**Confirmation flow end-to-end:** Inject a confirmation request, simulate an agent response ("Yes, proceed"), parse the response, verify the extracted decision is CONFIRM with high confidence. Repeat for CANCEL and REASON_MORE responses.

**Context eviction:** Inject more than `max_fam_messages_in_context` pricing updates, verify that the oldest are evicted and the total count stays at the limit. Verify that confirmation messages are not evicted.

**Pricing update filtering:** Set `price_change_threshold` to 0.1, inject a pricing update, then inject another with a 5% price change — verify it is suppressed. Inject another with a 15% change — verify it passes through.

### 12.3 Property-Based Tests

Use Hypothesis to generate random strings and verify that the parser never raises an exception and always returns a valid `ConfirmationResponseMessage` with a valid `ConfirmationDecision` and confidence in [0.0, 1.0].

Generate random `Decimal` values for all numeric fields in message dataclasses and verify that template rendering never raises an exception (robust to extreme values).

---

## 13. Dependencies

**Internal:** `fam/types.py` (shared types including `PriceUpdate`), `fam/signals/formatter.py` (spec 05, tiered signal formatting), `fam/config/schema.py` (configuration schema).

**External:** `re` (standard library, for parser regex), `dataclasses` (standard library), `enum` (standard library), `decimal` (standard library). The protocol module does not import LangGraph directly — LangGraph-specific injection is in `fam/signals/injection.py`, which imports `langchain_core.messages` for `SystemMessage`, `HumanMessage`, `ToolMessage`, and `BaseMessage`. This is the only external dependency, and it is isolated to the injection submodule.

**Interface contracts:** The protocol depends on the `InjectionStrategy` being implemented by framework adapters (spec 09). It also depends on the tiered signal formatter (spec 05) for rendering tier-appropriate message bodies.

---

## 14. Open Questions

**Structured output parsing.** Some modern LLMs support structured output (JSON mode, function calling with constrained schemas). Should the confirmation request ask the agent to respond in JSON (e.g., `{"decision": "confirm"}`) instead of free text? This would make parsing trivial and eliminate ambiguity, but it requires the framework adapter to configure structured output mode, and it may confuse weaker models or interfere with the agent's natural tool-calling flow. Worth investigating as an optional high-confidence parsing mode.

**Multi-turn confirmation.** The current protocol supports a single confirmation exchange per tool call (with at most one retry). Some agents might benefit from a clarifying exchange — "The tool costs 5 units, but a cheaper alternative exists at 1.2 units. Would you prefer the alternative?" This would require a multi-turn confirmation flow with additional message types. Deferred to a future version.

**Non-English agents.** The parser's keyword lists and templates are English-only. If FAM is used with agents prompted in other languages, the parser will fail. Supporting multilingual parsing requires either per-language keyword lists or a more sophisticated NLU approach. Not in scope for the initial implementation.

**Message compression.** For agents with small context windows, even minimal FAM messages may consume a meaningful fraction of available context. Should the protocol support an ultra-compact mode that uses abbreviated messages (e.g., single-line codes instead of multi-line formatted messages)? Deferred pending evidence of context pressure in practice.
