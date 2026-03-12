# Spec 00 — Architecture Overview

**Project:** FAM (Free Agent Market)
**Status:** Living Document
**Owner:** System Architect
**Last Updated:** 2026-03-09
**References:** All other spec files (01–15)

---

## 1. Purpose

This document defines the system-level architecture for FAM — a market-based multi-agent orchestrator built on top of LangGraph. It is the canonical reference for how the system is structured, what each component does, where the boundaries lie, and how data flows between layers. Every other spec refines a specific module described here.

Read this document first before building or modifying any module.

---

## 2. Problem Statement

When multiple LLM agents share a finite pool of GPU inference capacity and external tool endpoints, uncoordinated access creates resource contention. Agents issue tool calls and inference requests without knowledge of system load, leading to cascading latency spikes, wasted retries, and starvation of lower-priority work. Hard rate limiting is a blunt instrument — it treats all requests equally regardless of urgency or value, and it gives agents no information to adapt their behavior.

FAM solves this by establishing a resource market. Every shared resource has a price that fluctuates with real-time scarcity. Every agent has a budget that constrains its spending. Agents receive pricing signals before committing to expensive operations, and they can choose to proceed, defer, or abandon based on cost. The system achieves decentralized coordination without centralized scheduling: when load is high, prices rise, agents self-throttle, and contention drops. When load is low, prices fall, and agents exploit cheap resources aggressively.

---

## 3. Design Principles

**Price signals over hard limits.** Agents make locally optimal decisions using price information rather than being blocked by opaque rate limiters. This preserves agent autonomy while achieving globally efficient allocation.

**Intercept, don't modify.** FAM wraps LangGraph execution rather than forking or patching it. Tools remain pure functions. Agent prompts remain unmodified except for injected pricing signals. FAM is a layer around the agent, not inside it.

**Budget as backpressure.** Per-agent budgets serve as the ultimate throttle. An agent that exhausts its budget must wait for replenishment. This prevents any single agent from monopolizing shared resources regardless of how aggressively it bids.

**Graceful degradation by tier.** Not all LLMs can reason about quantitative prices. The system probes model capability and adjusts signal complexity accordingly — from precise cost breakdowns for capable models to simple directional nudges ("resources are scarce, consider waiting") for weaker ones.

**Observability is not optional.** Every pricing decision, budget mutation, confirmation exchange, and dispatch event is logged with full context. The system must be debuggable in production and experimentally evaluable in research.

**Configuration over code changes.** All pricing curves, budget parameters, thresholds, and timing constants live in configuration. Tuning the system should never require editing Python files.

---

## 4. Project Structure

FAM follows the same packaging conventions used by LangGraph itself — a flat Python package directory with no `src/` prefix. The project root is `fam/` and the main package is `fam/fam/`.

```
fam/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml
│   │   └── release.yml
│   └── PULL_REQUEST_TEMPLATE.md
├── docs/
│   └── ...
├── examples/
│   ├── basic_multi_agent.py
│   ├── budget_pressure.ipynb
│   └── ...
├── specs/
│   ├── 00-architecture-overview.md      ◄── this document
│   ├── 01-telemetry-collector.md
│   ├── 02-pricing-engine.md
│   ├── 03-budget-manager.md
│   ├── 04-agent-communication-protocol.md
│   ├── 05-tiered-signal-formatter.md
│   ├── 06-confirmation-handler.md
│   ├── 07-tool-dispatch-queue.md
│   ├── 08-speculative-continuation.md
│   ├── 09-framework-adapters.md
│   ├── 10-gpu-cluster-interface.md
│   ├── 11-tool-endpoint-interface.md
│   ├── 12-configuration-and-tuning.md
│   ├── 13-metrics-and-observability.md
│   ├── 14-evaluation-harness.md
│   └── 15-capability-probe.md
├── fam/
│   ├── __init__.py
│   ├── core.py                          # Orchestrator entry point
│   ├── types.py                         # Shared dataclasses & protocols
│   ├── telemetry/
│   │   ├── __init__.py
│   │   ├── collector.py                 # spec 01
│   │   └── snapshot.py
│   ├── pricing/
│   │   ├── __init__.py
│   │   ├── engine.py                    # spec 02
│   │   └── controllers.py
│   ├── budget/
│   │   ├── __init__.py
│   │   └── manager.py                   # spec 03
│   ├── signals/
│   │   ├── __init__.py
│   │   ├── protocol.py                  # spec 04
│   │   ├── formatter.py                 # spec 05
│   │   └── templates.py
│   ├── confirmation/
│   │   ├── __init__.py
│   │   └── handler.py                   # spec 06
│   ├── dispatch/
│   │   ├── __init__.py
│   │   └── queue.py                     # spec 07
│   ├── speculative/
│   │   ├── __init__.py
│   │   └── manager.py                   # spec 08
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   └── langgraph/
│   │       ├── __init__.py
│   │       ├── adapter.py               # spec 09
│   │       ├── nodes.py
│   │       └── state.py
│   ├── interfaces/
│   │   ├── __init__.py
│   │   ├── gpu_cluster.py               # spec 10 (abstract)
│   │   ├── gpu_mock.py                  # spec 10 (mock)
│   │   ├── tool_endpoint.py             # spec 11 (abstract)
│   │   └── tool_mock.py                 # spec 11 (mock)
│   ├── config/
│   │   ├── __init__.py
│   │   ├── schema.py                    # spec 12
│   │   └── defaults.yaml
│   ├── metrics/
│   │   ├── __init__.py
│   │   └── collector.py                 # spec 13
│   └── eval/
│       ├── __init__.py
│       ├── harness.py                   # spec 14
│       ├── tasks.py
│       ├── baselines.py
│       └── probe.py                     # spec 15
├── tests/
│   ├── conftest.py
│   ├── test_telemetry/
│   ├── test_pricing/
│   ├── test_budget/
│   ├── test_signals/
│   ├── test_confirmation/
│   ├── test_dispatch/
│   ├── test_speculative/
│   ├── test_adapters/
│   ├── test_interfaces/
│   ├── test_config/
│   ├── test_metrics/
│   └── test_eval/
├── pyproject.toml
├── Makefile
├── AGENTS.md
├── CLAUDE.md
├── README.md
└── LICENSE
```

All imports within the codebase use the `fam` package prefix: `from fam.pricing.engine import PricingEngine`, `from fam.budget.manager import BudgetManager`, etc.

---

## 5. System Layers

The system is organized into three layers. Each layer has a clear responsibility boundary and communicates with adjacent layers through defined interfaces.

```
┌─────────────────────────────────────────────────────────────────────┐
│                          AGENT LAYER                                │
│                                                                     │
│   ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐      │
│   │  Agent 1  │  │  Agent 2  │  │  Agent 3  │  │  Agent N  │      │
│   │ (LangGraph│  │ (LangGraph│  │ (LangGraph│  │ (LangGraph│      │
│   │  Graph)   │  │  Graph)   │  │  Graph)   │  │  Graph)   │      │
│   └─────┬─────┘  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘      │
│         │              │              │              │              │
│         └──────────────┼──────────────┼──────────────┘              │
│                        │              │                              │
│                        ▼              ▼                              │
│              ┌─────────────────────────────────┐                    │
│              │     LangGraph Adapter Layer      │                    │
│              │  fam/adapters/langgraph/         │                    │
│              │  (nodes.py, state.py, adapter.py)│                    │
│              └───────────────┬─────────────────┘                    │
│                              │                                      │
├──────────────────────────────┼──────────────────────────────────────┤
│                     ORCHESTRATION LAYER                              │
│                              │                                      │
│    ┌─────────────────────────┼──────────────────────────────┐      │
│    │                         ▼                               │      │
│    │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │      │
│    │  │  Telemetry   │  │   Pricing    │  │   Budget     │ │      │
│    │  │  Collector   │──▶   Engine     │──▶   Manager    │ │      │
│    │  │  (spec 01)   │  │  (spec 02)   │  │  (spec 03)  │ │      │
│    │  └──────────────┘  └──────┬───────┘  └──────┬───────┘ │      │
│    │                           │                  │          │      │
│    │                           ▼                  ▼          │      │
│    │                    ┌──────────────┐  ┌──────────────┐  │      │
│    │                    │   Signal     │  │ Confirmation │  │      │
│    │                    │  Formatter   │──▶  Handler     │  │      │
│    │                    │  (spec 05)   │  │  (spec 06)  │  │      │
│    │                    └──────────────┘  └──────┬───────┘  │      │
│    │                                             │          │      │
│    │                           ┌─────────────────┤          │      │
│    │                           ▼                  ▼          │      │
│    │                    ┌──────────────┐  ┌──────────────┐  │      │
│    │                    │ Speculative  │  │    Tool      │  │      │
│    │                    │ Continuation │  │  Dispatch    │  │      │
│    │                    │  (spec 08)   │  │   Queue      │  │      │
│    │                    └──────────────┘  │  (spec 07)  │  │      │
│    │                                      └──────┬───────┘  │      │
│    └─────────────────────────────────────────────┼──────────┘      │
│                                                  │                  │
├──────────────────────────────────────────────────┼──────────────────┤
│                      EXECUTION LAYER             │                  │
│                                                  ▼                  │
│    ┌──────────────────────────────────────────────────────────┐     │
│    │                                                          │     │
│    │  ┌──────────────────┐       ┌──────────────────┐        │     │
│    │  │   GPU Inference  │       │  Tool Endpoints   │        │     │
│    │  │     Cluster      │       │  (search, code,   │        │     │
│    │  │    (spec 10)     │       │   APIs, etc.)     │        │     │
│    │  │                  │       │    (spec 11)      │        │     │
│    │  └──────────────────┘       └──────────────────┘        │     │
│    │                                                          │     │
│    └──────────────────────────────────────────────────────────┘     │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘

Cross-cutting concerns:
  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
  │  Configuration   │  │   Metrics &      │  │   Capability     │
  │   & Tuning       │  │  Observability   │  │     Probe        │
  │   (spec 12)      │  │   (spec 13)      │  │   (spec 15)      │
  └──────────────────┘  └──────────────────┘  └──────────────────┘

  ┌──────────────────┐  ┌──────────────────┐
  │   Agent Comms    │  │   Evaluation     │
  │   Protocol       │  │    Harness       │
  │   (spec 04)      │  │   (spec 14)      │
  └──────────────────┘  └──────────────────┘
```

### 5.1 Agent Layer

The agent layer consists of LangGraph `StateGraph` instances — one per concurrent agent. Each graph encodes an agent's reasoning loop: receive a task, think, decide to call a tool (or not), observe the tool result, think again, repeat until done.

FAM does not define or own these graphs. They are user-defined. FAM's LangGraph adapter wraps them by inserting additional nodes at tool-call boundaries and extending the state schema with orchestrator-managed fields.

**What the agent layer owns:** task definition, prompt engineering, tool selection logic, reasoning chains, final output.

**What the agent layer does NOT own:** tool execution timing, resource pricing, confirmation flow, budget enforcement. These are delegated downward to the orchestration layer.

An agent is identified by a unique `agent_id: str` assigned at graph instantiation. The agent_id is the key for budget lookup, metric attribution, and state isolation.

### 5.2 Orchestration Layer

The orchestration layer is the core of FAM. It sits between agents and shared resources, mediating every interaction. It contains seven modules.

**Telemetry Collector (spec 01) — `fam/telemetry/collector.py`:** Periodically polls GPU cluster and tool endpoint interfaces for current metrics. Normalizes raw metrics into standardized snapshots with consistent units and timestamps. Applies exponential moving average (EMA) smoothing to reduce noise. Publishes snapshots that downstream components (primarily the pricing engine) consume.

**Pricing Engine (spec 02) — `fam/pricing/engine.py`:** Consumes telemetry snapshots and computes two prices — `reasoning_price` (cost per inference token under current GPU load) and `tool_price` (cost per tool invocation under current endpoint load). Prices are computed per pricing interval (default 1 second). The engine implements a control-theoretic approach: a proportional controller as the default, with a PI (proportional-integral) controller available for smoother response. Prices are clamped between configurable floor and ceiling values to prevent pathological extremes.

**Budget Manager (spec 03) — `fam/budget/manager.py`:** Maintains a per-agent budget balance using `Decimal` arithmetic. Handles four operations: check balance, deduct, replenish, and query history. Replenishment occurs at a configurable rate per tick (e.g., 1.0 units per second). Deductions are atomic — if the balance is insufficient, the deduction fails entirely. The budget manager does not decide policy (e.g., what to do when a budget is exhausted). It reports the state; the confirmation handler decides the action.

**Agent Communication Protocol (spec 04) — `fam/signals/protocol.py`:** Defines the structured message format for all orchestrator-to-agent and agent-to-orchestrator communication within the LangGraph state. All orchestrator messages are system messages prefixed with `[FAM]`. Defines message types: pricing signal, confirmation request, confirmation response, tool result, deferral notice, cancellation notice, budget warning.

**Tiered Signal Formatter (spec 05) — `fam/signals/formatter.py`:** Translates raw pricing data into human-readable (LLM-readable) messages appropriate for the agent's capability tier. Three tiers exist — quantitative (full numeric data, cost/benefit framing), qualitative (descriptive labels like "expensive" or "cheap," relative comparisons), and directional (simple "go/wait" guidance with no numbers). Tier assignment is per-agent and determined by the capability probe (spec 15) or manual configuration.

**Confirmation Handler (spec 06) — `fam/confirmation/handler.py`:** The central decision point for tool calls. When an agent's LangGraph graph reaches a tool-call node, the confirmation handler evaluates whether confirmation is needed (based on current tool price, agent budget, and configured thresholds). If no confirmation is needed (price is below the auto-approve threshold), the tool call proceeds immediately. If confirmation is needed, the handler formats a confirmation request using the signal formatter, injects it into the agent's state via LangGraph's interrupt mechanism, waits for the agent's decision, and routes to dispatch (approved), speculative continuation (deferred), or cancellation. If the agent's budget is insufficient, the call is blocked regardless of the agent's preference.

**Tool Dispatch Queue (spec 07) — `fam/dispatch/queue.py`:** A priority-based async queue that manages the actual execution of approved tool calls against external endpoints. Implements per-endpoint rate limiting to respect external API quotas. Priority is a function of agent priority tier and time-in-queue (to prevent starvation). Handles retries with exponential backoff for transient failures. Reports dispatch metrics (queue depth, wait time, success rate) back to the telemetry collector.

**Speculative Continuation Manager (spec 08) — `fam/speculative/manager.py`:** Handles the deferred-tool-call path. When an agent defers a tool call (choosing to continue reasoning rather than pay the current price), the speculative continuation manager checkpoints the current LangGraph state, injects a "tool result pending" message, and allows the agent to continue reasoning provisionally. When the deferred tool call eventually executes (either because the agent later approves it at a lower price, or a timeout fires), the manager reconciles the actual tool result with the agent's provisional reasoning. If the provisional reasoning is invalidated by the actual result, the manager rolls back to the checkpoint and replays with the real result. This is the most complex module in the system and should be built last.

### 5.3 Execution Layer

The execution layer contains the actual shared resources that agents compete over. FAM does not own these resources — it interfaces with them through abstract protocol classes.

**GPU Inference Cluster (spec 10) — `fam/interfaces/gpu_cluster.py`:** The LLM inference backend. Could be a vLLM cluster, a TGI deployment, or a managed API. FAM reads metrics from it (batch queue depth, KV-cache utilization, request latency, throughput) to feed the pricing engine. FAM does not control scheduling within the cluster — it only controls whether and when an agent submits a request. In the development and testing environment, this is replaced by `fam/interfaces/gpu_mock.py`, a mock that generates configurable synthetic utilization curves.

**Tool Endpoints (spec 11) — `fam/interfaces/tool_endpoint.py`:** External services invoked by agent tools — search APIs, code execution sandboxes, database connectors, third-party APIs. FAM reads endpoint-level metrics (queue depth, latency, error rate, rate limit headroom) and dispatches calls through the dispatch queue. In development, `fam/interfaces/tool_mock.py` simulates configurable latency distributions and failure modes.

---

## 6. Data Flow

The following describes the complete data flow for a single tool call by a single agent, end to end.

### 6.1 Telemetry Cycle (Background)

This runs continuously on a fixed interval, independent of any individual agent's execution.

```
GPU Cluster ──metrics──▶ Telemetry Collector ──GPUTelemetry──▶ Pricing Engine
                                                                    │
Tool Endpoints ─metrics─▶ Telemetry Collector ─ToolTelemetry─▶ Pricing Engine
                                                                    │
                                                           ┌────────┘
                                                           ▼
                                                    PriceUpdate
                                                (reasoning_price,
                                                  tool_price,
                                                  timestamp)
                                                           │
                                               ┌───────────┼──────────┐
                                               ▼           ▼          ▼
                                          Budget      Confirmation   Signal
                                          Manager      Handler     Formatter
```

The telemetry cycle produces a `PriceUpdate` on every tick. The `PriceUpdate` is the fundamental input to all downstream orchestration decisions. It is a frozen dataclass defined in `fam/types.py`:

```python
@dataclass(frozen=True)
class PriceUpdate:
    reasoning_price: Decimal
    tool_price: Decimal
    gpu_utilization: float        # 0.0–1.0
    tool_utilization: dict[str, float]  # endpoint_id → 0.0–1.0
    timestamp: float
```

### 6.2 Tool Call Interception Flow

This is the per-agent, per-tool-call flow. It occurs within the LangGraph graph execution.

```
Agent LLM Node
    │
    │  (LLM output includes a tool call)
    ▼
┌─────────────────────────┐
│  FAM Tool Node          │
│  (adapters/langgraph/   │
│   nodes.py)             │
│                         │
│  1. Extract tool call   │
│     from state          │
│                         │
│  2. Look up current     │
│     tool_price for      │
│     this endpoint       │
│                         │
│  3. Look up agent's     │
│     budget balance      │
│                         │
│  4. Check: is balance   │──── NO ──▶ Inject "insufficient budget"
│     >= tool_price?      │           message, return to LLM node
│                         │
│  5. Check: is           │
│     tool_price below    │──── YES ─▶ Auto-approve, skip to step 8
│     auto-approve        │
│     threshold?          │
│                         │
│  6. Format confirmation │
│     request via Signal  │
│     Formatter at the    │
│     agent's tier        │
│                         │
│  7. Interrupt graph,    │
│     inject confirmation │
│     request, wait for   │
│     agent decision      │
│         │               │
│         ├─ APPROVE ─────│──▶ step 8
│         ├─ DEFER ───────│──▶ step 10
│         └─ CANCEL ──────│──▶ Inject cancellation message,
│                         │    return to LLM node
│                         │
│  8. Deduct tool_price   │
│     from agent budget   │
│                         │
│  9. Submit to Dispatch  │
│     Queue, await result │──▶ Inject tool result message,
│                         │    return to LLM node
│                         │
│  10. Checkpoint state,  │
│      hand off to        │
│      Speculative        │
│      Continuation Mgr   │──▶ Inject "pending" message,
│                         │    return to LLM node for
│                         │    provisional reasoning
└─────────────────────────┘
```

### 6.3 Budget Lifecycle

Each agent begins with an initial budget balance set by configuration. The budget changes through three mechanisms.

**Deduction:** When a tool call is approved and dispatched, the current `tool_price` is deducted atomically. When an inference call is made, the `reasoning_price × estimated_tokens` is deducted. (Reasoning price deduction is a future enhancement; the initial implementation focuses on tool price deduction only.)

**Replenishment:** On each budget tick (configurable interval, default 1 second), a fixed `replenishment_rate` is added to each agent's balance, up to a configurable `max_balance` ceiling. This provides steady-state income that prevents permanent budget exhaustion.

**Adjustment (future):** Manual or policy-driven balance adjustments for priority changes, task completion bonuses, etc. Not implemented in the initial version.

```
Time ──────────────────────────────────────────────────────▶

Balance
  ▲
  │  ╭─╮     ╭─╮        ╭─╮
  │ ╱   ╲   ╱   ╲      ╱   ╲               ╭──── max_balance
  │╱     ╲ ╱     ╲    ╱     ╲             ╱
  │       ╳       ╲  ╱       ╲           ╱
  │      ╱ ╲       ╲╱         ╲         ╱
  │     ╱   ╲       │          ╲       ╱
  │    ╱     ╲      │           ╲     ╱
  │   ╱       ╲     │            ╲   ╱
  │  ╱         ╲    │             ╲ ╱
  │ ╱           ╲   │              ╳
  │╱  replenish  deduct   replenish  deduct
  └────────────────────────────────────────▶ Time

  Sawtooth pattern: gradual replenishment punctuated
  by discrete deductions at tool call time.
```

### 6.4 Price Formation

Prices respond to utilization. The relationship is non-linear: prices remain near the floor when utilization is low, rise steeply as utilization crosses a congestion threshold (default 0.7), and approach the ceiling as utilization nears 1.0.

```
Price
  ▲
  │                                          ╭── ceiling
  │                                      ╭───╯
  │                                  ╭───╯
  │                              ╭───╯
  │                          ╭───╯
  │                      ╭───╯
  │                  ╭───╯
  │            ╭─────╯
  │      ╭─────╯
  │──────╯                                   ── floor
  └──────────────────────────────────────────▶ Utilization
  0.0                 0.7                 1.0
           congestion threshold
```

The pricing engine computes this per resource type. GPU utilization drives `reasoning_price`. Per-endpoint utilization drives per-endpoint `tool_price`. When multiple tool endpoints exist, each has an independent price.

---

## 7. Component Inventory

Every component listed below has a dedicated spec file. The spec number is the reference.

### 7.1 Core Components

| Component | Spec | Module Path | Responsibility |
|---|---|---|---|
| Telemetry Collector | 01 | `fam/telemetry/` | Ingest, normalize, and smooth resource metrics |
| Pricing Engine | 02 | `fam/pricing/` | Compute dynamic prices from telemetry |
| Budget Manager | 03 | `fam/budget/` | Track and enforce per-agent budgets |
| Agent Communication Protocol | 04 | `fam/signals/protocol.py` | Define message format between FAM and agents |
| Tiered Signal Formatter | 05 | `fam/signals/formatter.py` | Adapt pricing signals to model capability |
| Confirmation Handler | 06 | `fam/confirmation/` | Intercept tool calls, request and process confirmations |
| Tool Dispatch Queue | 07 | `fam/dispatch/` | Rate-limited priority dispatch of tool calls |
| Speculative Continuation Manager | 08 | `fam/speculative/` | Manage deferred tool calls and provisional reasoning |

### 7.2 Adapter Components

| Component | Spec | Module Path | Responsibility |
|---|---|---|---|
| LangGraph Adapter | 09 | `fam/adapters/langgraph/` | Hook FAM into LangGraph graphs |

### 7.3 Interface Components

| Component | Spec | Module Path | Responsibility |
|---|---|---|---|
| GPU Cluster Interface | 10 | `fam/interfaces/gpu_cluster.py`, `gpu_mock.py` | Abstract + mock GPU inference backend |
| Tool Endpoint Interface | 11 | `fam/interfaces/tool_endpoint.py`, `tool_mock.py` | Abstract + mock tool endpoints |

### 7.4 Cross-Cutting Components

| Component | Spec | Module Path | Responsibility |
|---|---|---|---|
| Configuration & Tuning | 12 | `fam/config/` | All configurable parameters, YAML loading, validation |
| Metrics & Observability | 13 | `fam/metrics/` | Structured logging, metrics export, dashboards |
| Evaluation Harness | 14 | `fam/eval/harness.py` | Benchmark tasks, baselines, experiment runner |
| Capability Probe | 15 | `fam/eval/probe.py` | Assess LLM economic reasoning ability |

---

## 8. Ownership Boundaries

This section clarifies what FAM owns, what it delegates, and what it explicitly does not do.

### FAM OWNS:

- Dynamic pricing of shared resources based on real-time telemetry.
- Per-agent budget accounting (balances, deductions, replenishment).
- The confirmation flow: deciding when to ask, formatting the question, parsing the answer.
- Tiered signal formatting adapted to model capability.
- Tool dispatch ordering and rate limiting.
- Speculative continuation when tool calls are deferred.
- All orchestration metrics and observability.

### FAM DELEGATES:

- Actual tool execution to external endpoints via the tool endpoint interface.
- Actual LLM inference to the GPU cluster or managed LLM API.
- Agent reasoning, planning, and tool selection to the LangGraph graph.
- Graph definition and prompt engineering to the user.

### FAM DOES NOT:

- Schedule GPU inference requests within the cluster. It only controls admission (whether to submit) and timing (when to submit).
- Modify tool implementations. Tools are opaque callables.
- Replace or override agent decisions. If an agent approves a costly tool call and has sufficient budget, FAM executes it. FAM informs; the agent decides.
- Persist state across process restarts. All state is in-memory. Persistence is a future concern.
- Handle authentication, authorization, or multi-tenancy. It assumes a trusted single-tenant environment.

---

## 9. State Management

### 9.1 Orchestrator-Level State

The `Orchestrator` class in `fam/core.py` holds all system-level state:

- `latest_price_update: PriceUpdate` — most recent pricing snapshot.
- `budget_manager: BudgetManager` — contains all agent balances.
- `dispatch_queue: ToolDispatchQueue` — active dispatch queue with pending calls.
- `speculative_manager: SpeculativeContinuationManager` — active speculative sessions.
- `config: OrchestratorConfig` — immutable after startup.
- `metrics_collector: MetricsCollector` — accumulates all events.

This state is shared across all concurrent agent executions. The `BudgetManager` and `DispatchQueue` must be safe for concurrent async access. Since FAM uses `asyncio` (single-threaded concurrency), this means operations must not yield between check-and-mutate steps. Critical sections use atomic operations or are structured as non-yielding synchronous blocks within async methods.

### 9.2 Per-Agent State (LangGraph)

Each agent's LangGraph state is extended with orchestrator fields via the `FAMState` schema defined in `fam/adapters/langgraph/state.py`. These fields are managed exclusively by FAM nodes — the agent's LLM and user-defined nodes should not write to them directly.

FAM-managed state fields:

- `agent_id: str` — immutable, set at graph creation.
- `budget_balance: Decimal` — refreshed from BudgetManager before each FAM node runs.
- `current_reasoning_price: Decimal` — latest reasoning price at time of node execution.
- `current_tool_prices: dict[str, Decimal]` — latest per-endpoint tool prices.
- `pricing_signal: str` — formatted signal for the agent's capability tier.
- `pending_tool_calls: list[dict]` — tool calls awaiting dispatch or deferred.
- `speculative_mode: bool` — whether the agent is in speculative continuation.
- `fam_messages: list[str]` — history of FAM injections for observability.
- `capability_tier: str` — one of "quantitative", "qualitative", "directional".

### 9.3 Checkpoint Strategy

LangGraph's built-in checkpointing is used for speculative continuation rollback. Checkpoints are taken at two points: before a tool call enters the confirmation flow (so we can roll back if the deferral's speculative reasoning is invalidated), and before speculative continuation begins (so we have a clean rollback target).

Checkpoints are stored in-memory using LangGraph's `MemorySaver`. Persistent checkpoint backends (SQLite, PostgreSQL) are supported by LangGraph but are not required for the initial implementation.

---

## 10. Concurrency Model

FAM uses `asyncio` for all concurrency. There are no threads, no multiprocessing, and no external message queues in the initial implementation.

The system runs the following concurrent tasks:

**Telemetry loop:** An `asyncio` task that runs every `telemetry_interval` seconds (default 0.5s). Polls all resource interfaces, builds telemetry snapshots, feeds the pricing engine, publishes `PriceUpdate`.

**Budget replenishment loop:** An `asyncio` task that runs every `budget_tick_interval` seconds (default 1.0s). Iterates all agent budgets and applies replenishment.

**Agent graph executions:** Each active agent is a LangGraph `ainvoke` or `astream` call. Multiple agents run concurrently as `asyncio` tasks. When an agent hits a FAM node (tool interception), it interacts with the shared orchestrator state (budget checks, price lookups, dispatch submission) through async method calls on the `Orchestrator` instance.

**Dispatch workers:** A pool of `asyncio` tasks (one per tool endpoint, or configurable) that consume from the dispatch queue and execute tool calls against endpoints. Results are delivered back to the waiting agent graph node via `asyncio.Future`.

All shared state mutations go through the `Orchestrator` instance's methods, which are structured to be safe under `asyncio` concurrency (no `await` between read and write of shared state in critical sections).

---

## 11. Deployment Topology

### 11.1 Development and Testing

Everything runs in a single Python process. GPU cluster and tool endpoints are replaced by mocks. Multiple agents are simulated as concurrent `asyncio` tasks within the same event loop. This is the primary development mode and the mode used by the evaluation harness (spec 14).

```
┌──────────────────────────────────────────┐
│           Single Python Process          │
│                                          │
│  asyncio event loop                      │
│  ├── Telemetry loop (mock sources)       │
│  ├── Budget replenishment loop           │
│  ├── Agent 1 graph execution             │
│  ├── Agent 2 graph execution             │
│  ├── ...                                 │
│  ├── Agent N graph execution             │
│  └── Dispatch workers (mock endpoints)   │
│                                          │
│  Shared: Orchestrator instance           │
│          (pricing, budgets, queue)        │
└──────────────────────────────────────────┘
```

### 11.2 Production (Future)

In production, FAM runs as a sidecar or middleware layer co-located with the agent runtime. The telemetry collector reads from real monitoring endpoints (Prometheus, custom metric APIs). The dispatch queue calls real tool endpoints. The GPU cluster interface reads from the inference server's metrics endpoint (e.g., vLLM's `/metrics`).

FAM itself remains a single-process, single-threaded async application. Horizontal scaling is achieved by running one FAM instance per agent cohort (e.g., per-tenant or per-workload-class), not by distributing FAM across multiple processes. This avoids the need for distributed consensus on pricing and budget state.

Production deployment is out of scope for the initial implementation. The architecture supports it without structural changes — only the interface implementations change (mocks → real backends).

---

## 12. Extension Points

The architecture is designed to be extended at the following points without modifying core orchestrator logic.

**New tool endpoints:** Implement the `ToolEndpoint` protocol from spec 11. Register with FAM at startup. The pricing engine, dispatch queue, and telemetry collector automatically pick it up.

**New pricing controllers:** Implement a new controller class conforming to the controller interface in spec 02. Swap via configuration.

**New signal tiers:** Add a new tier to the `SignalTier` enum and a corresponding template in `fam/signals/templates.py`. Assign to agents via capability probe or manual config.

**New agent frameworks:** Implement a new adapter conforming to the abstract adapter interface in `fam/adapters/base.py`. However, the initial implementation targets LangGraph exclusively; do not build adapter abstractions speculatively.

**New evaluation tasks:** Add task definitions to `fam/eval/tasks.py`. The harness (spec 14) automatically discovers and runs registered tasks.

---

## 13. Spec Index

| Spec | Title | Summary |
|---|---|---|
| 00 | Architecture Overview | This document. System-level architecture, layers, data flow, boundaries. |
| 01 | Telemetry Collector | GPU and tool endpoint metric ingestion, normalization, smoothing. |
| 02 | Pricing Engine | Dual dynamic pricing from telemetry using control-theoretic approach. |
| 03 | Budget Manager | Per-agent balance tracking, deduction, replenishment, history. |
| 04 | Agent Communication Protocol | Message types and format for FAM-agent interaction. |
| 05 | Tiered Signal Formatter | Capability-adapted pricing signal formatting (quantitative/qualitative/directional). |
| 06 | Confirmation Handler | Tool call interception, confirmation request/response flow. |
| 07 | Tool Dispatch Queue | Priority-based, rate-limited async tool call dispatch. |
| 08 | Speculative Continuation Manager | Deferred tool calls, provisional reasoning, checkpoint/rollback. |
| 09 | Framework Adapters | LangGraph adapter: node wrapping, state extension, graph builder. |
| 10 | GPU Inference Cluster Interface | Abstract GPU cluster interface and mock implementation. |
| 11 | Tool Endpoint Interface | Abstract tool endpoint interface and mock implementation. |
| 12 | Configuration & Tuning | All parameters, YAML schema, defaults, validation. |
| 13 | Metrics & Observability | Structured logging, metric events, export, dashboards. |
| 14 | Evaluation Harness | Benchmark tasks, baselines, experiment runner, analysis. |
| 15 | Capability Probe | LLM economic reasoning assessment for tier assignment. |

---

## 14. Key Decisions and Rationale

**Why prices instead of priorities or quotas?** Priorities create a rigid ordering that doesn't adapt to changing conditions. Quotas waste allocation when some agents don't use their share. Prices are a continuous signal that naturally adapts to load and communicates scarcity information that agents can act on intelligently.

**Why dual prices (reasoning + tool) instead of a single resource price?** GPU inference and tool endpoints are different resources with different scarcity dynamics. GPU utilization might be high while a particular tool endpoint is idle, or vice versa. Separate prices let agents make fine-grained tradeoffs (e.g., "reasoning is cheap right now, I'll think longer before making an expensive tool call").

**Why per-agent budgets with replenishment instead of a shared pool?** Shared pools create tragedy-of-the-commons dynamics where aggressive agents starve conservative ones. Per-agent budgets with steady replenishment ensure fairness while allowing agents to save up for expensive operations or spend quickly when resources are cheap.

**Why LangGraph specifically?** LangGraph provides the right abstraction level: explicit graph structure with controllable nodes, built-in state management, checkpoint/rollback support, and interrupt mechanisms. It allows FAM to intercept execution at tool-call boundaries without modifying the LLM or tools. LangChain's AgentExecutor is too opaque; raw API calls provide too little structure.

**Why asyncio and not threads?** FAM's concurrency is I/O-bound (waiting for tool calls, waiting for LLM responses, polling metrics). Asyncio handles this efficiently without the complexity of thread synchronization. The single-threaded event loop also eliminates race conditions on shared state, which is critical for budget correctness.

**Why mocks first, real backends later?** FAM's value proposition is in its coordination logic, not in its ability to call a specific API. Building and testing against mocks lets us iterate on pricing curves, budget parameters, and confirmation flows without incurring real API costs or depending on infrastructure availability. The evaluation harness (spec 14) requires deterministic, reproducible experiments, which only mocks can provide.

**Why the name FAM?** Free Agent Market. Agents are "free" in the economic sense — they participate in a market where they make autonomous decisions about resource consumption based on price signals and budget constraints, rather than being centrally commanded.

---

## 15. Glossary

**Agent:** A LangGraph StateGraph instance executing a task. Identified by `agent_id`.

**Budget:** A per-agent numeric balance (Decimal) that is spent on resource usage and replenished over time.

**Capability Tier:** Classification of an LLM's ability to reason about pricing signals. One of: quantitative, qualitative, directional.

**Confirmation:** A FAM-initiated exchange where the agent is presented with a cost and asked to approve, defer, or cancel a tool call.

**Congestion Threshold:** The utilization level above which prices begin to rise steeply. Default 0.7.

**Deferral:** An agent's decision to postpone a tool call rather than pay the current price. Triggers speculative continuation.

**Dispatch:** The act of executing an approved tool call against an external endpoint.

**FAM:** Free Agent Market. The name of this project and the orchestrator system.

**PriceUpdate:** An immutable snapshot of current prices and utilization, produced by the pricing engine on each tick.

**Replenishment:** Periodic addition of budget balance to an agent. Provides steady-state income.

**Signal:** A formatted message injected into agent state conveying pricing and resource information.

**Speculative Continuation:** The agent continues reasoning after deferring a tool call, under the assumption the result will arrive later. May be rolled back if the actual result invalidates provisional reasoning.

**Telemetry Snapshot:** A timestamped reading of resource utilization metrics from GPU cluster or tool endpoints.

**Tick:** One iteration of a periodic loop (telemetry tick, budget tick, pricing tick).

**Tool Price:** The current cost to execute one tool call against a specific endpoint.

**Reasoning Price:** The current cost per token of LLM inference under current GPU load.
