# Spec 15 — Capability Probe

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-10
**Depends On:** Spec 00 (Architecture Overview), Spec 05 (Tiered Signal Formatter), Spec 12 (Configuration & Tuning)
**Consumed By:** Spec 05 (Tiered Signal Formatter), Spec 06 (Confirmation Handler), Spec 14 (Evaluation Harness)

---

## 1. Purpose

The capability probe determines how well an LLM can reason about economic signals — prices, budgets, tradeoffs, and scarcity — so that FAM can communicate with it at the right level of abstraction. A model that can do budget arithmetic gets precise numbers. A model that cannot gets simple directional nudges. Sending quantitative signals to a model that mangles arithmetic wastes context tokens and produces worse decisions than a plain "tools are expensive right now, consider reasoning instead."

This is a standalone assessment, not part of the main orchestration loop. It runs once per model (at registration time or on first use), produces a tier assignment (quantitative, qualitative, or directional), and caches the result. The probe is fast (five short prompt-response exchanges), deterministic (fixed prompts, temperature 0), and self-contained (no dependency on FAM's runtime state).

This spec defines the five probe dimensions and their prompts, the per-dimension scoring rubrics, the aggregate scoring function, the tier assignment thresholds, the probe runner that executes against any model endpoint, and the caching mechanism.

---

## 2. Module Location

```
fam/
├── eval/
│   ├── __init__.py
│   ├── probe.py             # ProbeRunner — main entry point
│   ├── probe_prompts.py     # Prompt templates and expected answers
│   ├── probe_scoring.py     # Scoring rubrics and aggregate function
│   └── probe_cache.py       # Tier assignment cache
```

Tests live in `tests/test_eval/test_probe/`.

---

## 3. Probe Dimensions

The probe tests five dimensions of economic reasoning capability. Each dimension targets a specific cognitive skill that FAM's pricing signals require. The dimensions are ordered from most basic (arithmetic) to most subtle (anchoring robustness), forming a rough difficulty ladder.

### 3.1 Dimension 1 — Budget Arithmetic

**Skill tested:** Given a price and a budget balance, can the model correctly compute the cost of an action and the remaining balance?

**Why it matters:** Quantitative pricing signals include exact costs and balances. If the model cannot do basic multiplication and subtraction with these numbers, it will make incorrect cost assessments and the quantitative tier is counterproductive.

**Prompt template:**

```python
BUDGET_ARITHMETIC_PROMPT = """You have a budget of {balance} units. A tool call costs {price} units.

Question 1: How much will your budget be after making one tool call?
Question 2: How many tool calls can you afford in total?

Provide your answers as:
Remaining balance: <number>
Maximum calls: <number>"""
```

**Parameterization:** The probe generates three variants with different numbers to ensure the model isn't pattern-matching a single example:

```python
BUDGET_ARITHMETIC_VARIANTS: list[dict[str, Any]] = [
    {"balance": 100.0, "price": 15.0, "expected_remaining": 85.0, "expected_max_calls": 6},
    {"balance": 47.5, "price": 8.25, "expected_remaining": 39.25, "expected_max_calls": 5},
    {"balance": 250.0, "price": 33.0, "expected_remaining": 217.0, "expected_max_calls": 7},
]
```

**Scoring:** Each variant is scored independently. Per variant, 0.5 points for correct remaining balance (within tolerance of ±0.01) and 0.5 points for correct maximum calls (exact integer match). The dimension score is the mean across variants, yielding a score in [0.0, 1.0].

### 3.2 Dimension 2 — Replenishment Reasoning

**Skill tested:** Given that budget replenishes over time, can the model reason about whether to spend now or wait?

**Why it matters:** FAM's budget replenishment creates temporal tradeoffs. An agent with 5 units, a tool cost of 10 units, and a replenishment rate of 2 units/second should recognize it can afford the call in 2.5 seconds. Models that cannot reason about replenishment will either waste budget (spending impulsively) or stall indefinitely (failing to realize they can afford future actions).

**Prompt template:**

```python
REPLENISHMENT_PROMPT = """Your current budget is {balance} units. Budget replenishes at {rate} units per second.
A tool call costs {price} units. You need to make this tool call to complete your task.

Question 1: Can you afford the tool call right now? (yes/no)
Question 2: If not, how many seconds until you can afford it?
Question 3: Should you wait for replenishment or attempt to solve without the tool? Consider that waiting {wait_time} seconds is {wait_assessment} relative to your task deadline of {deadline} seconds.

Provide your answers as:
Can afford now: <yes/no>
Wait time: <number or N/A>
Decision: <wait/proceed_without_tool>
Reasoning: <brief explanation>"""
```

**Parameterization:**

```python
REPLENISHMENT_VARIANTS: list[dict[str, Any]] = [
    {
        "balance": 3.0, "rate": 2.0, "price": 10.0,
        "wait_time": 3.5, "wait_assessment": "short", "deadline": 60.0,
        "expected_afford": "no", "expected_wait": 3.5, "expected_decision": "wait",
    },
    {
        "balance": 1.0, "rate": 0.5, "price": 20.0,
        "wait_time": 38.0, "wait_assessment": "very long", "deadline": 45.0,
        "expected_afford": "no", "expected_wait": 38.0, "expected_decision": "proceed_without_tool",
    },
    {
        "balance": 12.0, "rate": 1.0, "price": 10.0,
        "wait_time": 0.0, "wait_assessment": "N/A", "deadline": 60.0,
        "expected_afford": "yes", "expected_wait": 0.0, "expected_decision": "wait",
    },
]
```

**Scoring:** Per variant: 0.25 points for correct affordability check, 0.25 points for correct wait time (within ±0.5 seconds, or exact "N/A" when affordable), 0.25 points for correct decision, 0.25 points for reasoning that references the relationship between wait time and deadline. Dimension score is the mean across variants.

### 3.3 Dimension 3 — Priority Ranking Under Scarcity

**Skill tested:** Given multiple possible tool calls and budget for only one, can the model correctly identify which call provides the most value?

**Why it matters:** Under budget scarcity, agents must prioritize. FAM's confirmation flow presents cost information, but the agent must decide which tool calls are essential and which are discretionary. A model that cannot rank alternatives by expected value will waste its scarce budget on low-value calls.

**Prompt template:**

```python
PRIORITY_RANKING_PROMPT = """You have {balance} units of budget remaining. You need to complete your task.
You have identified three possible tool calls:

Tool A: {tool_a_name}
  - Cost: {tool_a_cost} units
  - Expected value: {tool_a_value}

Tool B: {tool_b_name}
  - Cost: {tool_b_cost} units
  - Expected value: {tool_b_value}

Tool C: {tool_c_name}
  - Cost: {tool_c_cost} units
  - Expected value: {tool_c_value}

You can only afford {affordable_count} of these three calls.

Question 1: Rank these tools from most to least valuable, considering both cost and expected value.
Question 2: Which tool call(s) should you make?
Question 3: Why did you deprioritize the other(s)?

Provide your answers as:
Ranking: <Tool X > Tool Y > Tool Z>
Selected: <Tool X, Tool Y>
Reasoning: <brief explanation>"""
```

**Parameterization:**

```python
PRIORITY_RANKING_VARIANTS: list[dict[str, Any]] = [
    {
        "balance": 15.0, "affordable_count": 1,
        "tool_a_name": "web_search", "tool_a_cost": 10.0, "tool_a_value": "Retrieves the key fact needed to answer the question directly",
        "tool_b_name": "calculator", "tool_b_cost": 5.0, "tool_b_value": "Verifies a computation you can likely do mentally",
        "tool_c_name": "database_query", "tool_c_cost": 12.0, "tool_c_value": "Retrieves supporting data that strengthens but is not essential to the answer",
        "expected_ranking": ["A", "C", "B"],
        "expected_selected": ["A"],
    },
    {
        "balance": 20.0, "affordable_count": 2,
        "tool_a_name": "code_executor", "tool_a_cost": 8.0, "tool_a_value": "Tests your solution against the required test cases",
        "tool_b_name": "documentation_search", "tool_b_cost": 6.0, "tool_b_value": "Looks up an API you already know well",
        "tool_c_name": "code_executor", "tool_c_cost": 8.0, "tool_c_value": "Tests edge cases that could reveal bugs",
        "expected_ranking": ["A", "C", "B"],
        "expected_selected": ["A", "C"],
    },
    {
        "balance": 10.0, "affordable_count": 1,
        "tool_a_name": "search_api", "tool_a_cost": 10.0, "tool_a_value": "Searches for information you already partially know",
        "tool_b_name": "search_api", "tool_b_cost": 10.0, "tool_b_value": "Searches for a critical unknown fact",
        "tool_c_name": "calculator", "tool_c_cost": 3.0, "tool_c_value": "Performs a trivial arithmetic check",
        "expected_ranking": ["B", "A", "C"],
        "expected_selected": ["B"],
    },
]
```

**Scoring:** Per variant: 0.4 points for correct ranking (full credit for exact match, 0.2 for getting the top choice right but wrong order on others), 0.4 points for correct selection of which tools to actually invoke, 0.2 points for reasoning that references value-per-cost rather than just absolute cost or absolute value. Dimension score is the mean across variants.

### 3.4 Dimension 4 — Relative Price Response

**Skill tested:** When reasoning is cheap and tools are expensive (or vice versa), does the model shift its strategy appropriately?

**Why it matters:** The entire FAM pricing mechanism rests on the assumption that agents will substitute between reasoning and tool use in response to relative prices. If a model cannot interpret relative price signals and adjust behavior, the market mechanism provides no benefit over fixed allocation.

**Prompt template:**

```python
RELATIVE_PRICE_PROMPT = """You are working on a task that requires finding specific information.

Current resource prices:
- Reasoning (thinking/planning): {reasoning_price} units per step
- Tool calls (web search): {tool_price} units per call

Your budget: {balance} units.

You can either:
(A) Use a web search tool to find the answer directly (1 tool call)
(B) Reason through the problem using your existing knowledge (estimated {reasoning_steps} reasoning steps)

Option A total cost: {option_a_cost} units
Option B total cost: {option_b_cost} units

The information you need is {certainty_description}.

Which option do you choose and why?

Provide your answer as:
Choice: <A or B>
Reasoning: <brief explanation referencing costs>"""
```

**Parameterization:**

```python
RELATIVE_PRICE_VARIANTS: list[dict[str, Any]] = [
    {
        "reasoning_price": 1.0, "tool_price": 25.0, "balance": 50.0,
        "reasoning_steps": 5, "option_a_cost": 25.0, "option_b_cost": 5.0,
        "certainty_description": "something you have partial knowledge about",
        "expected_choice": "B",
    },
    {
        "reasoning_price": 8.0, "tool_price": 3.0, "balance": 50.0,
        "reasoning_steps": 5, "option_a_cost": 3.0, "option_b_cost": 40.0,
        "certainty_description": "a precise factual lookup you cannot guess",
        "expected_choice": "A",
    },
    {
        "reasoning_price": 2.0, "tool_price": 15.0, "balance": 30.0,
        "reasoning_steps": 4, "option_a_cost": 15.0, "option_b_cost": 8.0,
        "certainty_description": "something you are fairly confident about but not certain",
        "expected_choice": "B",
    },
    {
        "reasoning_price": 5.0, "tool_price": 5.0, "balance": 50.0,
        "reasoning_steps": 3, "option_a_cost": 5.0, "option_b_cost": 15.0,
        "certainty_description": "a precise fact you definitely do not know",
        "expected_choice": "A",
    },
]
```

**Scoring:** Per variant: 0.5 points for the correct choice, 0.5 points for reasoning that explicitly references the cost comparison (not just the absolute cost of one option, but the relative cost of both). A response that picks the right option but gives no cost-based reasoning scores 0.5. Dimension score is the mean across variants.

### 3.5 Dimension 5 — Framing and Anchoring Robustness

**Skill tested:** Does the model's economic decision change when irrelevant numerical anchors are introduced?

**Why it matters:** In FAM's real-time environment, agents see prices that change over time and budget numbers that fluctuate. If the model is susceptible to anchoring effects (e.g., a previously high price makes a moderate price feel "cheap"), its decisions will be inconsistent and suboptimal. This dimension tests whether the model's decisions are driven by the actual economics or by cognitive biases.

**Prompt template:**

```python
ANCHORING_PROMPT = """You are deciding whether to make a tool call.

{anchor_text}

Current situation:
- Tool call cost: {price} units
- Your budget: {balance} units
- Budget after call: {remaining} units
- Replenishment rate: {rate} units/second
- Task importance: {importance}

Should you make this tool call?

Provide your answer as:
Decision: <yes/no>
Reasoning: <brief explanation>"""
```

**Parameterization:** The key design is that the underlying economics are identical across variant pairs — only the anchor text differs. The model should make the same decision regardless of the anchor.

```python
ANCHORING_VARIANT_PAIRS: list[tuple[dict[str, Any], dict[str, Any]]] = [
    # Pair 1: Historical price anchor (high vs. low) — same economics, expected "yes"
    (
        {"anchor_text": "Tool calls earlier today cost 50 units each (expensive period).",
         "price": 10.0, "balance": 30.0, "remaining": 20.0,
         "rate": 2.0, "importance": "moderately important", "expected_decision": "yes"},
        {"anchor_text": "Tool calls earlier today cost 2 units each (cheap period).",
         "price": 10.0, "balance": 30.0, "remaining": 20.0,
         "rate": 2.0, "importance": "moderately important", "expected_decision": "yes"},
    ),
    # Pair 2: Social anchor (high vs. low colleague spend) — expected "yes"
    (
        {"anchor_text": "Your colleague spent 200 units on tool calls for a similar task.",
         "price": 15.0, "balance": 25.0, "remaining": 10.0,
         "rate": 1.0, "importance": "important for task completion", "expected_decision": "yes"},
        {"anchor_text": "Your colleague spent 5 units on tool calls for a similar task.",
         "price": 15.0, "balance": 25.0, "remaining": 10.0,
         "rate": 1.0, "importance": "important for task completion", "expected_decision": "yes"},
    ),
    # Pair 3: Range anchor (high max vs. low min) — expected "no"
    (
        {"anchor_text": "System alert: maximum possible tool price is 500 units.",
         "price": 20.0, "balance": 22.0, "remaining": 2.0,
         "rate": 0.5, "importance": "nice to have but not essential", "expected_decision": "no"},
        {"anchor_text": "System alert: minimum possible tool price is 1 unit.",
         "price": 20.0, "balance": 22.0, "remaining": 2.0,
         "rate": 0.5, "importance": "nice to have but not essential", "expected_decision": "no"},
    ),
]
```

**Scoring:** For each pair, the model is presented with both variants (in separate calls, not in the same context). Scoring has two components:

- **Correctness (0.5 per pair):** Does the model make the economically correct decision in each variant? 0.25 per variant.
- **Consistency (0.5 per pair):** Does the model make the same decision for both variants in the pair? Full 0.5 if decisions match regardless of correctness. 0.0 if they differ (indicating the anchor changed the decision).

Dimension score is the mean across pairs. A model that always makes the correct decision and is never swayed by anchors scores 1.0. A model that is correct but inconsistent (anchor flips its decision) scores at most 0.5.

---

## 4. Scoring Rubric

### 4.1 Per-Dimension Scoring

Each dimension produces a score in [0.0, 1.0]. The scoring rules specific to each dimension are defined above in section 3. The response parser for each dimension uses structured extraction — the model is instructed to produce labeled output (`Remaining balance: <number>`, `Decision: <yes/no>`, etc.), and the parser extracts these labels.

```python
@dataclass(frozen=True)
class DimensionScore:
    """Score for a single probe dimension."""

    dimension: str                   # "budget_arithmetic", "replenishment", etc.
    score: float                     # 0.0–1.0
    variant_scores: list[float]      # Per-variant scores
    raw_responses: list[str]         # Raw model outputs
    parse_success: list[bool]        # Whether each response was parseable
```

If the model's response cannot be parsed (no matching labels found), the variant scores 0.0. The `parse_success` flag records this so that probe failures due to formatting can be distinguished from failures due to incorrect reasoning.

### 4.2 Response Parsing

```python
class ProbeResponseParser:
    """Parses structured responses from probe prompts."""

    @staticmethod
    def extract_labeled_value(response: str, label: str) -> str | None:
        """Extract the value after a label like 'Remaining balance: 85.0'.

        Searches for the pattern '{label}: <value>' or '{label}:<value>'
        (case-insensitive). Returns the stripped value string, or None
        if the label is not found.
        """
        ...

    @staticmethod
    def extract_number(value: str) -> float | None:
        """Parse a numeric value from a string. Handles integers, floats,
        and common formatting (commas, currency symbols). Returns None
        if not parseable."""
        ...

    @staticmethod
    def extract_yes_no(value: str) -> bool | None:
        """Parse a yes/no value. Accepts 'yes', 'no', 'true', 'false',
        'y', 'n' (case-insensitive). Returns None if ambiguous."""
        ...

    @staticmethod
    def extract_choice(value: str, options: list[str]) -> str | None:
        """Parse a choice from a set of options. Returns the matched
        option or None if no match."""
        ...
```

### 4.3 Numeric Tolerance

For budget arithmetic and replenishment timing, the probe allows a configurable tolerance for numeric answers:

```python
@dataclass(frozen=True)
class NumericTolerance:
    """Tolerance for numeric answer comparison."""

    absolute: float = 0.01          # Accept if |actual - expected| <= absolute
    relative: float = 0.01          # Accept if |actual - expected| / |expected| <= relative

    def is_close(self, actual: float, expected: float) -> bool:
        """Return True if actual is within tolerance of expected."""
        if abs(expected) < 1e-9:
            return abs(actual) <= self.absolute
        return (
            abs(actual - expected) <= self.absolute
            or abs(actual - expected) / abs(expected) <= self.relative
        )
```

---

## 5. Aggregate Scoring and Tier Assignment

### 5.1 Aggregate Score

The aggregate probe score is a weighted sum of dimension scores. Weights reflect the relative importance of each skill for effective economic reasoning within FAM.

```python
DIMENSION_WEIGHTS: dict[str, float] = {
    "budget_arithmetic": 0.25,       # Fundamental to quantitative tier
    "replenishment_reasoning": 0.20, # Important for temporal decisions
    "priority_ranking": 0.25,        # Critical for scarcity behavior
    "relative_price_response": 0.20, # Core price-responsiveness signal
    "anchoring_robustness": 0.10,    # Desirable but not blocking
}

def compute_aggregate_score(
    dimension_scores: dict[str, DimensionScore],
) -> float:
    """Compute the weighted aggregate probe score.

    Returns a value in [0.0, 1.0].
    """
    total = 0.0
    for dim, weight in DIMENSION_WEIGHTS.items():
        total += weight * dimension_scores[dim].score
    return total
```

Anchoring robustness receives the lowest weight because it is the most fragile dimension to test (sensitive to prompt phrasing) and the least critical to FAM's core mechanism (a model that is slightly anchoring-susceptible but otherwise competent will still benefit from quantitative signals).

### 5.2 Tier Thresholds

The aggregate score maps to a capability tier via two thresholds:

```python
@dataclass(frozen=True)
class TierThresholds:
    """Thresholds for tier assignment from aggregate probe score."""

    quantitative_min: float = 0.75   # Score >= 0.75 → quantitative tier
    qualitative_min: float = 0.45    # Score >= 0.45 → qualitative tier
                                     # Score < 0.45  → directional tier
```

**Quantitative tier (score ≥ 0.75):** The model handles budget arithmetic, reasons about replenishment timing, ranks priorities correctly, responds to relative prices, and resists anchoring. It receives full numeric pricing signals: exact costs, budget balances, replenishment projections, and cost-benefit framing.

**Qualitative tier (0.45 ≤ score < 0.75):** The model demonstrates partial economic reasoning — it may handle simple arithmetic but struggle with multi-step temporal reasoning or be susceptible to framing effects. It receives descriptive signals: "tool calls are currently expensive," "your budget is running low," "consider reasoning instead of searching."

**Directional tier (score < 0.45):** The model cannot reliably reason about economic quantities. It receives minimal behavioral nudges: "resources are scarce, prefer reasoning," "resources are abundant, tools are available," or no signal at all (for models that perform worse with any injected context).

### 5.3 Tier Override Rules

The threshold-based assignment can be overridden by per-dimension gate rules. Even if the aggregate score meets the quantitative threshold, the model is demoted if specific dimensions are too weak:

```python
@dataclass(frozen=True)
class TierGates:
    """Per-dimension minimum scores that gate tier assignment."""

    quantitative_gates: dict[str, float] = field(default_factory=lambda: {
        "budget_arithmetic": 0.60,    # Must handle basic math
        "relative_price_response": 0.50,  # Must respond to price signals
    })
    qualitative_gates: dict[str, float] = field(default_factory=lambda: {
        "priority_ranking": 0.30,     # Must show some priority awareness
    })

def assign_tier(
    aggregate_score: float,
    dimension_scores: dict[str, DimensionScore],
    thresholds: TierThresholds,
    gates: TierGates,
) -> str:
    """Assign a capability tier based on aggregate score and per-dimension gates.

    Returns one of: "quantitative", "qualitative", "directional".
    """
    if aggregate_score >= thresholds.quantitative_min:
        for dim, min_score in gates.quantitative_gates.items():
            if dimension_scores[dim].score < min_score:
                if aggregate_score >= thresholds.qualitative_min:
                    return _check_qualitative_gates(
                        dimension_scores, gates
                    )
                return "directional"
        return "quantitative"

    if aggregate_score >= thresholds.qualitative_min:
        return _check_qualitative_gates(dimension_scores, gates)

    return "directional"


def _check_qualitative_gates(
    dimension_scores: dict[str, DimensionScore],
    gates: TierGates,
) -> str:
    for dim, min_score in gates.qualitative_gates.items():
        if dimension_scores[dim].score < min_score:
            return "directional"
    return "qualitative"
```

---

## 6. Probe Runner

The `ProbeRunner` is the main class that executes the probe battery against a model endpoint, collects responses, scores them, and produces a tier assignment.

### 6.1 Interface

```python
class ProbeRunner:
    """Executes the capability probe against a model endpoint."""

    def __init__(
        self,
        config: ProbeConfig,
        cache: ProbeCache | None = None,
    ) -> None:
        """
        Args:
            config: Probe configuration (thresholds, gates, tolerances).
            cache: Optional cache for storing and retrieving previous results.
        """
        self._config = config
        self._cache = cache

    async def run(
        self,
        model_endpoint: str,
        model_id: str,
        model_params: ModelParams | None = None,
    ) -> ProbeResult:
        """Run the full probe battery against the specified model.

        If a cached result exists for this model_id and probe version,
        returns the cached result without re-running.

        Args:
            model_endpoint: URL or identifier for the model API.
            model_id: Unique identifier for the model (e.g., "gpt-4o-2024-08-06").
            model_params: Optional model parameters (temperature forced to 0).

        Returns:
            ProbeResult containing per-dimension scores and tier assignment.
        """
        cached = self._check_cache(model_id)
        if cached is not None:
            return cached

        dimension_scores = {}
        for dimension in PROBE_DIMENSIONS:
            score = await self._run_dimension(
                dimension, model_endpoint, model_params
            )
            dimension_scores[dimension.name] = score

        aggregate = compute_aggregate_score(dimension_scores)
        tier = assign_tier(
            aggregate,
            dimension_scores,
            self._config.thresholds,
            self._config.gates,
        )

        result = ProbeResult(
            model_id=model_id,
            probe_version=PROBE_VERSION,
            dimension_scores=dimension_scores,
            aggregate_score=aggregate,
            assigned_tier=tier,
            timestamp=datetime.utcnow().isoformat(),
        )

        if self._cache is not None:
            self._cache.store(result)

        return result

    async def _run_dimension(
        self,
        dimension: ProbeDimension,
        model_endpoint: str,
        model_params: ModelParams | None,
    ) -> DimensionScore:
        """Run all variants for a single dimension and score the results."""
        ...
```

### 6.2 Model Interaction

The probe communicates with model endpoints through a minimal async protocol, deliberately decoupled from LangGraph:

```python
class ModelClient(Protocol):
    async def complete(
        self, prompt: str, system_message: str | None = None,
        temperature: float = 0.0, max_tokens: int = 512,
    ) -> str: ...
```

The probe ships with a `LangChainModelClient` that wraps `BaseChatModel`, but any implementation of the `ModelClient` protocol can be substituted.

### 6.3 System Message

Every probe prompt is preceded by a system message that frames the evaluation context:

```python
PROBE_SYSTEM_MESSAGE = (
    "You are being evaluated on your ability to reason about resource "
    "costs and budgets. Answer each question precisely and concisely, "
    "following the exact output format requested. Show your work for "
    "any calculations."
)
```

### 6.4 ProbeResult

```python
@dataclass(frozen=True)
class ProbeResult:
    """Complete result of a capability probe run."""

    model_id: str
    probe_version: str               # e.g., "1.0" — changes when prompts change
    dimension_scores: dict[str, DimensionScore]
    aggregate_score: float
    assigned_tier: str                # "quantitative", "qualitative", "directional"
    timestamp: str                    # ISO 8601

    def summary(self) -> dict[str, Any]:
        """Return a concise summary suitable for logging."""
        return {
            "model_id": self.model_id,
            "tier": self.assigned_tier,
            "aggregate": round(self.aggregate_score, 3),
            "dimensions": {
                dim: round(score.score, 3)
                for dim, score in self.dimension_scores.items()
            },
        }
```

---

## 7. Caching

Probe results are cached to avoid redundant LLM calls. The cache is keyed by `(model_id, probe_version)` — if the probe prompts or scoring change (incrementing `probe_version`), cached results are invalidated.

### 7.1 Cache Interface

```python
class ProbeCache(Protocol):
    """Interface for storing and retrieving probe results."""

    def get(self, model_id: str, probe_version: str) -> ProbeResult | None:
        """Retrieve a cached result, or None if not cached."""
        ...

    def store(self, result: ProbeResult) -> None:
        """Store a probe result in the cache."""
        ...

    def invalidate(self, model_id: str) -> None:
        """Remove all cached results for a model."""
        ...

    def invalidate_all(self) -> None:
        """Clear the entire cache."""
        ...
```

### 7.2 File-Based Cache

The default cache implementation stores results as JSON files on disk under a configurable directory (default: `.fam_cache/probe/`). Files are named `{sanitized_model_id}_v{probe_version}.json`. The `model_id` is sanitized by replacing `/` and `:` with `_` to produce valid filenames.

```python
class FileProbeCache:
    def __init__(self, cache_dir: str = ".fam_cache/probe") -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, model_id: str, probe_version: str) -> ProbeResult | None:
        path = self._cache_path(model_id, probe_version)
        if not path.exists():
            return None
        return ProbeResult(**json.loads(path.read_text()))

    def store(self, result: ProbeResult) -> None:
        path = self._cache_path(result.model_id, result.probe_version)
        path.write_text(json.dumps(asdict(result), indent=2))
```

### 7.3 In-Memory Cache

For testing and single-session use, an in-memory cache stores results in a `dict[tuple[str, str], ProbeResult]` keyed by `(model_id, probe_version)`. Not persisted across restarts.

### 7.4 Cache Staleness

Cached results do not expire by time. They expire only when `probe_version` changes (indicating the prompts or scoring rubrics have been modified). This is intentional — a model's economic reasoning capability does not change unless the model itself is updated. When a model is updated (new version deployed), the caller should use a new `model_id` that reflects the version, which naturally bypasses the cache.

---

## 8. Probe Execution Flow

The complete flow: agent registration → extract `model_id` → `ProbeRunner.run()` → check cache → on miss, run all 5 dimensions (19 total prompt-response exchanges: 3 + 3 + 3 + 4 + 6) → compute weighted aggregate → apply tier thresholds and gates → cache result → return `ProbeResult` → signal formatter uses `assigned_tier` to select message format.

Total probe cost per model: ~3,800 input tokens and ~1,900 output tokens (19 exchanges × ~200 input + ~100 output each). At typical API pricing this is under $0.10 — a negligible one-time cost.

---

## 9. Configuration

All capability probe configuration is namespaced under `probe` in the global FAM configuration (spec 12).

```yaml
probe:
  version: "1.0"

  # Tier assignment thresholds
  thresholds:
    quantitative_min: 0.75
    qualitative_min: 0.45

  # Per-dimension gates for tier assignment
  gates:
    quantitative:
      budget_arithmetic: 0.60
      relative_price_response: 0.50
    qualitative:
      priority_ranking: 0.30

  # Dimension weights for aggregate score
  weights:
    budget_arithmetic: 0.25
    replenishment_reasoning: 0.20
    priority_ranking: 0.25
    relative_price_response: 0.20
    anchoring_robustness: 0.10

  # Numeric tolerance for arithmetic answers
  tolerance:
    absolute: 0.01
    relative: 0.01

  # Model interaction settings
  model:
    temperature: 0.0
    max_tokens: 512
    timeout_seconds: 30.0
    max_retries: 2

  # Cache configuration
  cache:
    backend: "file"                  # "file", "memory", or "none"
    file_cache_dir: ".fam_cache/probe"

  # Fallback tier for models that cannot be probed
  fallback_tier: "directional"

  # Known model overrides (skip probe, assign tier directly)
  model_overrides:
    # Example:
    # "gpt-4o": "quantitative"
    # "gpt-3.5-turbo": "qualitative"
```

The `model_overrides` section allows pre-assigning tiers for known models without running the probe. This is useful for well-characterized models where probe results are already known, or for models whose API does not support the structured output format the probe requires.

The `fallback_tier` is assigned when the probe cannot execute at all (model endpoint unreachable, all responses unparseable, etc.). Defaulting to "directional" — the simplest signal tier — ensures the system never sends incomprehensible signals to an uncharacterized model.

---

## 10. Error Handling

The probe is designed to degrade gracefully. It must never block agent registration or crash the orchestrator.

**Model timeout:** If a prompt-response exchange does not complete within `timeout_seconds`, the variant scores 0.0 and the probe continues with remaining variants. The timeout is per-exchange, not per-dimension.

**Model error:** If the model API returns an error (rate limit, server error, authentication failure), the runner retries up to `max_retries` times with exponential backoff (1s, 2s). If all retries fail, the variant scores 0.0.

**Parse failure:** If the model's response cannot be parsed (missing labels, garbled output), the variant scores 0.0. The raw response is preserved in `DimensionScore.raw_responses` for debugging.

**All-zero result:** If every variant of every dimension scores 0.0 (total probe failure), the model is assigned the `fallback_tier`. This can happen if the model is completely non-responsive, speaks a different language, or refuses to engage with the prompt format. The probe logs a warning including the raw responses for debugging.

**Cache corruption:** If a cached result file is malformed (invalid JSON, missing fields), the cache entry is deleted and the probe re-runs. The corrupted file is logged at WARNING level.

**Probe version mismatch:** If a cached result has a different `probe_version` than the current version, it is treated as a cache miss and the probe re-runs. The old cached result is not deleted (it may be useful for historical comparison).

---

## 11. Metrics Emitted

The capability probe emits the following metrics to the observability system (spec 13):

**Counters:** `probe.runs.total` (per model_id), `probe.runs.cached` (cache hits), `probe.runs.executed` (cache misses requiring LLM calls), `probe.variants.total` (per dimension), `probe.variants.parsed` (successfully parsed), `probe.variants.parse_failed`, `probe.variants.timed_out`, `probe.tiers.assigned` (per tier value — tracks distribution of tier assignments).

**Gauges:** `probe.cache.size` (number of cached results), `probe.latest_score` (per model_id, the aggregate score from the most recent probe).

**Histograms:** `probe.exchange.duration_ms` (per dimension, time for each prompt-response exchange), `probe.run.duration_ms` (total time for a full probe run), `probe.dimension.score` (per dimension, distribution of scores across all probed models).

---

## 12. Testing Strategy

### 12.1 Unit Tests

**Scoring rubrics:** For each dimension, provide hand-crafted model responses (correct, partially correct, incorrect, unparseable) and verify the scoring function produces the expected score. Test boundary cases: exact-tolerance numeric answers, ambiguous yes/no responses, rankings with ties.

**Response parsing:** Test `ProbeResponseParser` with a variety of response formats: clean formatted output, output with extra whitespace, output with additional explanation mixed in, output in wrong order, output missing labels entirely. Verify correct extraction or graceful None return.

**Aggregate scoring:** Verify weighted sum is computed correctly. Verify that weights summing to less than 1.0 or more than 1.0 are handled (normalized or rejected). Verify boundary cases: all dimensions score 1.0, all score 0.0, one dimension scores 1.0 and rest score 0.0.

**Tier assignment:** Verify threshold-based assignment for scores at exact boundaries (0.75, 0.45). Verify gate rules: a model with aggregate 0.80 but budget_arithmetic 0.50 should be demoted from quantitative. Verify that gate demotion cascades correctly (quantitative gate failure → check qualitative gates → potentially directional).

**Numeric tolerance:** Verify `NumericTolerance.is_close` for exact matches, within-absolute-tolerance, within-relative-tolerance, and outside-both-tolerances. Test with zero expected values.

**Cache operations:** Verify store/get round-trip for both file and in-memory caches. Verify invalidation. Verify cache miss returns None. Verify version-keyed lookup.

### 12.2 Integration Tests

**Full probe run with mock model:** Create a `ModelClient` that returns pre-scripted responses. Run the full probe. Verify the ProbeResult has all dimensions scored, the aggregate is computed, and the tier is assigned. Test with a "perfect" mock (all correct answers → quantitative), a "partial" mock (some correct → qualitative), and a "failing" mock (all wrong → directional).

**Cache integration:** Run the probe with a file cache. Verify the result is cached. Run again, verify the cache is hit (no LLM calls made — assert by checking call count on the mock client). Invalidate cache, verify the probe re-runs.

**Timeout handling:** Create a mock model that sleeps longer than `timeout_seconds`. Verify the probe completes (does not hang), scores timed-out variants as 0.0, and assigns an appropriate (lower) tier.

**End-to-end with orchestrator:** Register an agent with the FAM orchestrator. Verify the probe runs automatically, a tier is assigned, and the signal formatter uses the correct tier for subsequent pricing signals.

### 12.3 Property-Based Tests

Use Hypothesis to verify: for any combination of dimension scores in [0.0, 1.0], the aggregate score is in [0.0, 1.0]. For any aggregate score, exactly one tier is assigned (never zero, never multiple). Tier assignment is monotonically non-decreasing with aggregate score (higher score never produces a lower tier, ignoring gate effects). Cache store followed by get always returns the stored result.

---

## 13. Dependencies

**Internal:** `fam/config/schema.py` (configuration schema), `fam/signals/formatter.py` (spec 05, consumes tier assignments), `fam/metrics/collector.py` (spec 13, for emitting probe metrics).

**External:** `langchain-core` (for the optional `LangChainModelClient` that wraps `BaseChatModel`). The core probe logic (`ProbeRunner`, scoring, caching) depends only on the Python standard library (`asyncio`, `dataclasses`, `json`, `pathlib`, `re`, `datetime`, `logging`). The `ModelClient` protocol can be implemented against any LLM SDK without importing it into the probe module.

---

## 14. Open Questions

**Prompt sensitivity.** The probe's accuracy depends on prompt wording. Small changes to prompt templates can shift scores, especially for models near tier boundaries. The current prompts have not been extensively validated across a wide range of models. A calibration study using 10+ models and measuring inter-prompt reliability would strengthen confidence in the tier assignments. Until then, the `model_overrides` configuration provides a manual escape hatch.

**Dynamic re-probing.** The current design probes once and caches forever (per probe version). If a model is fine-tuned or updated in place (without changing `model_id`), the cached tier may become stale. A potential enhancement is periodic re-probing (e.g., every 24 hours) or re-probing when the signal formatter observes anomalous agent behavior (e.g., a supposedly quantitative-tier agent consistently ignoring numeric signals). This is deferred to avoid complexity.

**Multi-language support.** The probe prompts are in English. Models that perform better in other languages may be unfairly penalized. A potential extension is to provide probe prompts in the model's preferred language, but this requires maintaining translated prompt sets and scoring rubrics. Deferred.

**Reasoning trace scoring.** The current scoring for the "reasoning" component of each dimension is binary (references cost → credit, doesn't → no credit). A more nuanced approach would parse the reasoning chain and check for specific logical steps (e.g., "the model explicitly compared option costs before deciding"). This is fragile and model-dependent, so the initial implementation uses simple keyword/pattern matching for reasoning quality.

**Interaction effects between dimensions.** The probe runs each dimension independently. In practice, economic reasoning is holistic — a model's ability to rank priorities may depend on its arithmetic skills. The current design treats dimensions as independent and combines them via weighted sum. A more sophisticated approach might use a hierarchical model (arithmetic is a prerequisite for replenishment reasoning, which is a prerequisite for price response). Deferred pending empirical data on dimension correlations.
