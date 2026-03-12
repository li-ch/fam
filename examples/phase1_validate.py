from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fam.core import FAMOrchestrator
from fam.types import ToolDecision
from fam.interfaces.tool_mock import ToolMockEndpoint


@dataclass
class RunStats:
    completed: int
    failed: int
    deferred: int
    cancelled: int
    p95_wait_ms: float


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(0.95 * (len(values) - 1)))
    return values[idx]


async def run_baseline(agent_count: int, duration_s: float) -> RunStats:
    endpoint = ToolMockEndpoint(
        endpoint_id="search",
        base_latency_ms=110.0,
        failure_rate=0.08,
        capacity=6,
    )
    completed = 0
    failed = 0
    waits: list[float] = []
    deadline = time.monotonic() + duration_s

    async def agent_loop() -> None:
        nonlocal completed, failed
        while time.monotonic() < deadline:
            start = time.monotonic()
            try:
                await endpoint.call({"query": "mvp-validation"})
                completed += 1
                waits.append((time.monotonic() - start) * 1000)
            except Exception:
                failed += 1

    await asyncio.wait_for(
        asyncio.gather(*(agent_loop() for _ in range(agent_count))),
        timeout=duration_s + 5,
    )
    return RunStats(
        completed=completed,
        failed=failed,
        deferred=0,
        cancelled=0,
        p95_wait_ms=p95(waits),
    )


async def run_fam(agent_count: int, duration_s: float) -> tuple[RunStats, bool]:
    orch = FAMOrchestrator()
    # Match baseline endpoint stress profile.
    orch.tools["search"].base_latency_ms = 110.0
    orch.tools["search"].failure_rate = 0.08
    orch.tools["search"].capacity = 6

    await orch.start()
    for i in range(agent_count):
        await orch.register_agent(f"agent-{i}")

    completed = 0
    failed = 0
    deferred = 0
    cancelled = 0
    waits: list[float] = []
    deadline = time.monotonic() + duration_s
    negative_balance = False

    async def price_tick_loop() -> None:
        while time.monotonic() < deadline:
            await orch.tick()
            await asyncio.sleep(0.25)

    async def agent_loop(agent_id: str) -> None:
        nonlocal completed, failed, deferred, cancelled
        while time.monotonic() < deadline:
            start = time.monotonic()
            try:
                result = await orch.handle_tool_call(
                    agent_id=agent_id,
                    endpoint_id="search",
                    payload={"query": "mvp-validation"},
                    requested_decision=None,
                )
                waits.append((time.monotonic() - start) * 1000)
                status = result["status"]
                if status == "completed":
                    completed += 1
                elif status == "deferred":
                    deferred += 1
                elif status == "cancelled":
                    cancelled += 1
            except Exception:
                failed += 1

    await asyncio.wait_for(
        asyncio.gather(
            price_tick_loop(),
            *(agent_loop(f"agent-{i}") for i in range(agent_count)),
        ),
        timeout=duration_s + 7,
    )

    for i in range(agent_count):
        balance = await orch.budgets.check_balance(f"agent-{i}")
        if balance < 0:
            negative_balance = True
            break

    await orch.stop()
    return (
        RunStats(
            completed=completed,
            failed=failed,
            deferred=deferred,
            cancelled=cancelled,
            p95_wait_ms=p95(waits),
        ),
        negative_balance,
    )


async def run_adaptation_check() -> dict:
    orch = FAMOrchestrator()
    orch.tools["search"].base_latency_ms = 90.0
    orch.tools["search"].failure_rate = 0.03
    orch.tools["search"].capacity = 8
    await orch.start()
    await orch.register_agent("probe-agent")

    # Low-price regime: keep prices fresh but very early in load curve.
    low_completed = 0
    low_attempts = 0
    for _ in range(8):
        await orch.tick()
        try:
            result = await orch.handle_tool_call(
                agent_id="probe-agent",
                endpoint_id="search",
                payload={"query": "low"},
                requested_decision=None,
            )
        except Exception:
            result = {"status": "error"}
        low_attempts += 1
        if result["status"] == "completed":
            low_completed += 1

    # High-price regime: create explicit burst congestion first.
    async def burst_call() -> None:
        try:
            await orch.handle_tool_call(
                agent_id="probe-agent",
                endpoint_id="search",
                payload={"query": "burst"},
                requested_decision=ToolDecision.APPROVE,
            )
        except Exception:
            return

    for _ in range(6):
        await orch.tick()
        await asyncio.gather(*(burst_call() for _ in range(20)))

    high_completed = 0
    high_attempts = 0
    for _ in range(8):
        try:
            result = await orch.handle_tool_call(
                agent_id="probe-agent",
                endpoint_id="search",
                payload={"query": "high"},
                requested_decision=None,
            )
        except Exception:
            result = {"status": "error"}
        high_attempts += 1
        if result["status"] == "completed":
            high_completed += 1

    await orch.stop()
    return {
        "low_dispatch_rate": low_completed / max(low_attempts, 1),
        "high_dispatch_rate": high_completed / max(high_attempts, 1),
    }


async def main() -> None:
    random.seed(42)
    results: dict[str, dict] = {}
    negative_balance_any = False
    fam_better_all = True

    for count in (1, 8, 32):
        baseline = await run_baseline(agent_count=count, duration_s=3.0)
        fam, negative_balance = await run_fam(agent_count=count, duration_s=3.0)
        negative_balance_any = negative_balance_any or negative_balance

        fam_better = (
            fam.p95_wait_ms < baseline.p95_wait_ms
            and fam.completed > baseline.completed
        )
        fam_better_all = fam_better_all and fam_better
        results[str(count)] = {
            "baseline": baseline.__dict__,
            "fam": fam.__dict__,
            "fam_beats_baseline": fam_better,
        }

    adaptation = await run_adaptation_check()
    adaptation_pass = adaptation["high_dispatch_rate"] < adaptation["low_dispatch_rate"]

    report = {
        "runs": results,
        "adaptation": {
            **adaptation,
            "pass": adaptation_pass,
        },
        "safety": {
            "no_negative_balances": not negative_balance_any,
            "pass": not negative_balance_any,
        },
        "phase1_validation": {
            "fam_beats_baseline_all_counts": fam_better_all,
            "adaptation_pass": adaptation_pass,
            "safety_pass": (not negative_balance_any),
            "overall_pass": fam_better_all and adaptation_pass and (not negative_balance_any),
        },
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

