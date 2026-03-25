**`specs/00-architecture-overview.md`**
System-level architecture document. Describes the three layers (agent layer, orchestration layer, execution layer), how they connect, the data flow between them, and the deployment topology. Defines the boundaries of what the orchestrator owns vs. what it delegates. Includes a component diagram showing every module below and how they interact. References all other spec files. This is the file you point Cursor at first to establish the mental model before building anything.

---

**`specs/01-telemetry-collector.md`**
Defines the telemetry ingestion subsystem. Two telemetry sources: GPU inference cluster (batch queue depth, KV-cache occupancy, per-agent token generation rate, inference latency, active request count) and tool endpoint pool (per-endpoint queue depth, observed latency percentiles, active concurrent calls, error rates, rate limit headroom). Specifies the polling/push interface for each, the normalized telemetry schema, the smoothing/windowing strategy (exponential moving average with configurable window), and the internal event bus or shared state structure that downstream modules (pricing engine, speculative continuation manager) read from. Defines health checks and fallback behavior when telemetry is stale or missing.

---

**`specs/02-pricing-engine.md`**
Defines the dual pricing engine. Two price computations running on independent but synchronized update cycles. The reasoning price is a function of GPU utilization, KV-cache pressure, and batch queue depth. The tool price is a function of endpoint queue depth, latency, and rate limit headroom. Specifies the exact update rule for each (proportional-integral controller style, with configurable gains, floor/ceiling clamps, and smoothing). Defines how the relative price ratio is computed and exposed. Defines the price update frequency, the interface the pricing engine exposes to other modules (current prices, price history, price trend direction), and the configuration surface (gain parameters, floor/ceiling values, update interval). Includes the formal connection to shadow price approximation — what the prices are *trying* to approximate and under what assumptions.

---

**`specs/03-budget-manager.md`**
Defines the per-agent unified dispatch budget system. Each agent gets a budget object tracking: current balance, cumulative reasoning spend, cumulative tool spend, replenishment rate, and budget history. Specifies how reasoning tokens are metered (per-token deduction at current reasoning price, batched per generation step), how tool calls are metered (lump-sum deduction at current tool price upon confirmed dispatch), and how speculative continuation tokens are metered (per-token at current reasoning price, flagged as speculative). Defines the replenishment mechanism (constant rate, or system-load-adjusted rate, configurable). Defines budget exhaustion behavior (soft limit with warning vs. hard limit with forced pause). Defines the API surface: check balance, deduct, replenish, get summary, get remaining runway estimate.

---

**`specs/04-agent-communication-protocol.md`**
Defines the message protocol between the orchestrator and the agent's conversational interface. Specifies every message type: pricing update message (injected into agent context), tool call interception acknowledgment, confirmation request (with cost, budget remaining, estimated wait time, and alternative suggestion), agent confirmation response parsing (confirm, cancel, reason-more), deferred result notification, speculative continuation prompt, and deferred tool result injection. Defines the message templates for each tier (quantitative, qualitative, directional). Defines how messages are injected into the agent's conversation (system message, user message, or tool-result-formatted message) depending on the framework adapter. Defines the parsing logic for extracting the agent's decision from free-form text responses.

---

**`specs/05-tiered-signal-formatter.md`**
Defines the capability-tiered pricing signal system. Three tiers: quantitative (full numerical prices, budget math, replenishment projections), qualitative (natural language descriptions of scarcity levels like "high," "moderate," "low" with directional advice), and directional (simple behavioral nudges like "consider reasoning more before calling tools"). Specifies the mapping from internal pricing state to each tier's output format. Defines the capability assessment protocol — a lightweight probe run at agent registration that tests budget arithmetic, relative price comparison, and priority ranking — and the scoring rubric that maps probe results to a tier assignment. Defines the fallback tier for unknown models. Defines how tier assignment is cached and when it is re-evaluated.

---

**`specs/06-confirmation-handler.md`**
Defines the priced confirmation protocol. Specifies the trigger conditions (confirmation is activated only when tool price exceeds a configurable congestion threshold; below threshold, tool calls are dispatched without confirmation to avoid unnecessary latency). Defines the confirmation flow: intercept tool call → compute cost → check budget sufficiency → format confirmation request using tiered signal formatter → inject into agent conversation → parse agent response → dispatch, cancel, or redirect to reasoning. Defines timeout behavior (if agent doesn't respond within N tokens or T seconds, apply default policy: configurable as auto-confirm or auto-cancel). Defines metrics emitted: confirmation rate, cancel rate, timeout rate, per-agent decision quality tracking.

---

**`specs/07-tool-dispatch-queue.md`**
Defines the tool call dispatch and queueing subsystem. Receives confirmed tool calls from the confirmation handler. Maintains a priority queue per tool endpoint. Priority is a function of agent budget remaining, task priority (if externally specified), and time-in-queue. Implements dispatch rate limiting per endpoint (respecting rate limit headroom from telemetry). Implements deferral: when queue depth exceeds threshold, new calls are deferred and the agent is notified via the communication protocol to enter speculative continuation. Defines retry logic for transient failures. Defines the result routing path: tool result → orchestrator → injected into correct agent conversation, with handling for the case where the agent has generated speculative tokens since the call was issued. Defines call cancellation (agent cancels during confirmation or after deferral).

---

**`specs/08-speculative-continuation-manager.md`**
Defines the speculative continuation mechanism. When a tool call is deferred, this module manages the agent's transition to speculative reasoning. Specifies the speculative prompt template (informs agent that the tool result is pending, provides current reasoning price, suggests the agent continue reasoning provisionally). Tracks speculative token sequences per agent, tagged with the pending tool call they depend on. When the deferred tool result arrives, determines whether speculative tokens are consistent with the result (simple: always discard and re-inject result; advanced: consistency check heuristic). Defines the priced aspect: speculative tokens are metered at current reasoning price through the budget manager, so agents self-regulate speculation volume based on GPU scarcity. Defines the rollback mechanism if speculation must be discarded: how the conversation state is rewound and the tool result is injected cleanly.

---

**`specs/09-framework-adapters.md`**
Defines the adapter layer that integrates the orchestrator with specific agent frameworks. Each adapter implements a common interface: intercept outgoing tool calls, inject messages into agent conversation, read agent responses, track token generation counts, and register/deregister agents. Specifies adapters for at minimum two frameworks (e.g., LangGraph and AutoGen, or LangChain and CrewAI — pick based on what you want to support). Defines the adapter interface contract so new frameworks can be added. Defines how the adapter hooks into the framework's tool-calling mechanism without requiring framework source modification (middleware pattern, monkey-patching, callback registration, or wrapper classes depending on framework).

---

**`specs/10-inference-cluster-interface.md`**
Defines the interface to the GPU inference serving layer. Specifies what telemetry is read (batch queue depth, KV-cache memory usage per agent and aggregate, tokens-per-second throughput, request latency distribution, active vs. queued requests) and how it is obtained (polling an API endpoint on vLLM/TGI, reading Prometheus metrics, or parsing log streams). Defines a mock/simulated inference cluster for development and testing that generates realistic telemetry patterns (configurable utilization curves, burst patterns, latency distributions). Defines the abstraction layer so the system can target vLLM, TGI, or a mock backend through configuration.

---

**`specs/11-tool-endpoint-interface.md`**
Defines the interface to tool execution endpoints. Specifies the tool registry: each tool has an ID, endpoint URL, rate limit spec, expected latency profile, and idempotency flag. Defines the telemetry collection per endpoint (response time, queue depth if available, HTTP status codes, rate limit headers). Defines the wrapper that instruments tool calls with timing, error tracking, and telemetry reporting to the telemetry collector. Defines mock tool endpoints for development: configurable latency (fixed, normal distribution, or heavy-tailed), configurable failure rates, configurable rate limits, and configurable capacity to simulate contention.

---

**`specs/12-configuration-and-tuning.md`**
Defines every configurable parameter in the system, organized by module. Includes: pricing engine gains, floors, ceilings, update intervals; budget initial values, replenishment rates, hard/soft limits; confirmation congestion thresholds; dispatch queue priority weights; speculative continuation prompt templates; tiered signal templates and tier thresholds; telemetry polling intervals and smoothing windows; adapter-specific settings. Defines the configuration file format (YAML), validation rules, and hot-reload behavior (which parameters can be changed at runtime vs. requiring restart). Defines sensible defaults for a "getting started" deployment.

---

**`specs/13-metrics-and-observability.md`**
Defines the metrics, logging, and observability surface. Metrics emitted: per-agent budget balance over time, per-agent reasoning/tool spend rates, aggregate GPU utilization over time, aggregate tool utilization over time, pricing engine output (both prices, relative ratio) over time, confirmation decisions (confirm/cancel/timeout counts and rates), speculative tokens generated vs. discarded, tool dispatch queue depths, tool call latencies, phase distribution (fraction of agents in reasoning/tool-waiting/speculating at each time step), cross-correlation between GPU and CPU utilization, task completion rate and accuracy. Defines the metrics export format (Prometheus-compatible). Defines structured logging format for debugging the pricing engine and confirmation handler. Defines a dashboard spec (what graphs to show for the key experiments).

---

**`specs/14-evaluation-harness.md`**
Defines the benchmarking and experiment framework. Implements the five experiment types from the paper. Specifies the benchmark task suite (multi-step reasoning tasks requiring interleaved tool use — e.g., multi-hop QA with search, code generation with execution, data analysis with database queries). Defines the baseline implementations: uncoordinated dispatch, system-prompt nudging, hard rate limiting, tool-only pricing. Defines the agent scaling protocol (how to ramp from 1 to N concurrent agents). Defines the metric collection harness that records all metrics from spec 13 during experiment runs. Defines the ablation configurations (which modules to enable/disable for each ablation). Defines the shadow price ground truth computation for Experiment Type 3 (offline solver for the centralized problem given known utilities and capacities). Defines output format: structured results files and auto-generated comparison tables.

---

**`specs/15-capability-probe.md`**
Defines the standalone LLM economic reasoning capability assessment. A battery of short prompts testing: budget arithmetic (given price and balance, can the model compute cost and remaining balance?), replenishment reasoning (should the model wait for budget to replenish or spend now?), priority ranking under scarcity (given three tool calls and budget for only one, which to choose?), relative price response (given that reasoning is cheap and tools are expensive, does the model shift strategy?), and framing/anchoring robustness (does the model's decision change based on irrelevant numerical anchors?). Defines the scoring rubric per dimension, the aggregate scoring function, and the tier assignment thresholds. Defines the probe runner that executes this against any model endpoint and caches the result.

---

That's 16 spec files. I'd suggest building them roughly in dependency order: start with 00, then 01 and the interfaces (10, 11), then the engine (02, 03), then the protocol and agent-facing modules (04, 05, 06, 08), then dispatch (07), then adapters (09), then configuration and observability (12, 13), then the evaluation layer (14, 15). Each spec is sized so that Cursor can implement the module from that single file plus the architecture overview.