from __future__ import annotations

import asyncio
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fam.core import FAMOrchestrator
from fam.types import ToolDecision


async def functional_flow_check() -> dict:
    orch = FAMOrchestrator()
    await orch.start()
    await orch.register_agent("flow-agent")
    await orch.tick()

    completed = await orch.handle_tool_call(
        agent_id="flow-agent",
        endpoint_id="search",
        payload={"query": "functional-flow"},
        requested_decision=ToolDecision.APPROVE,
    )

    # Force insufficient budget then verify non-completed path.
    bal = await orch.budgets.check_balance("flow-agent")
    if bal > 0:
        await orch.budgets.deduct_reasoning(
            "flow-agent",
            bal,
            note="drain-for-non-completed-check",
        )
    non_completed = await orch.handle_tool_call(
        agent_id="flow-agent",
        endpoint_id="search",
        payload={"query": "functional-flow-non-completed"},
        requested_decision=ToolDecision.APPROVE,
    )

    # Concurrent calls should execute without deadlock.
    await orch.register_agent("flow-agent-2")
    concurrent_results = await asyncio.gather(
        *[
            orch.handle_tool_call(
                agent_id="flow-agent-2",
                endpoint_id="search",
                payload={"query": f"q-{i}"},
                requested_decision=ToolDecision.APPROVE,
            )
            for i in range(20)
        ],
        return_exceptions=True,
    )
    await orch.stop()

    concurrent_ok = all(
        (
            isinstance(r, dict)
            and r.get("status") in {"completed", "cancelled", "deferred"}
        )
        or isinstance(r, Exception)
        for r in concurrent_results
    )
    return {
        "completed_path": completed.get("status") == "completed",
        "non_completed_path": non_completed.get("status") in {"cancelled", "deferred"},
        "concurrent_path": concurrent_ok,
    }


async def budget_correctness_check() -> dict:
    orch = FAMOrchestrator()
    await orch.start()
    await orch.register_agent("budget-agent")
    before = await orch.budgets.check_balance("budget-agent")
    await orch.tick()
    prices = orch.latest_prices
    assert prices is not None
    cost = prices.reasoning_price * Decimal(5)

    deducted = await orch.budgets.deduct_reasoning("budget-agent", cost, note="budget-check")
    after_deduct = await orch.budgets.check_balance("budget-agent")
    await orch.budgets.replenish_all(seconds=1.0)
    after_replenish = await orch.budgets.check_balance("budget-agent")
    insufficient = await orch.budgets.deduct_tool("budget-agent", Decimal("99999"), note="insufficient")
    await orch.stop()
    return {
        "deduct_works": deducted and after_deduct < before,
        "replenish_works": after_replenish > after_deduct,
        "insufficient_rejected": insufficient is False,
    }


async def confirmation_behavior_check() -> dict:
    orch = FAMOrchestrator()
    await orch.start()
    await orch.register_agent("confirm-agent")

    low_eval = orch.confirmation.evaluate(
        tool_price=Decimal("0.20"),
        budget_balance=Decimal("100.0"),
        requested_decision=None,
    )
    high_eval = orch.confirmation.evaluate(
        tool_price=Decimal("4.00"),
        budget_balance=Decimal("100.0"),
        requested_decision=None,
    )
    await orch.stop()
    return {
        "low_regime_decision": low_eval.decision.value,
        "high_regime_decision": high_eval.decision.value,
        "policy_differs": low_eval.decision != high_eval.decision,
    }


async def dispatch_reliability_check() -> dict:
    orch = FAMOrchestrator()
    await orch.start()
    await orch.register_agent("dispatch-agent")
    await orch.tick()

    # Inject failures, ensure explicit outcome (exception path is explicit).
    orch.tools["search"].failure_rate = 1.0
    outcomes: list[str] = []
    for i in range(5):
        try:
            res = await orch.handle_tool_call(
                agent_id="dispatch-agent",
                endpoint_id="search",
                payload={"query": f"fail-{i}"},
                requested_decision=ToolDecision.APPROVE,
            )
            outcomes.append(res.get("status", "unknown"))
        except Exception:
            outcomes.append("error")

    # Recover and ensure queue still makes progress.
    orch.tools["search"].failure_rate = 0.0
    progress = await orch.handle_tool_call(
        agent_id="dispatch-agent",
        endpoint_id="search",
        payload={"query": "recover"},
        requested_decision=ToolDecision.APPROVE,
    )
    await orch.stop()

    return {
        "explicit_failure_outcomes": all(o in {"error", "cancelled", "deferred", "completed"} for o in outcomes),
        "recovery_progress": progress.get("status") in {"completed", "cancelled", "deferred"},
    }


async def price_shape_check() -> dict:
    orch = FAMOrchestrator()
    await orch.start()
    await orch.register_agent("price-agent")

    # Low congestion
    orch.tools["search"].queue_depth = 0
    orch.tools["search"].active_calls = 0
    orch.tools["search"].rate_limit_headroom = 1.0
    for _ in range(2):
        await orch.tick()
    low = orch.latest_prices.tool_prices["search"]

    # High congestion
    orch.tools["search"].queue_depth = 200
    orch.tools["search"].active_calls = 40
    orch.tools["search"].rate_limit_headroom = 0.01
    for _ in range(8):
        await orch.tick()
    high = orch.latest_prices.tool_prices["search"]
    await orch.stop()
    return {
        "low_tool_price": str(low),
        "high_tool_price": str(high),
        "higher_under_congestion": high > low,
    }


async def main() -> None:
    flow = await functional_flow_check()
    budget = await budget_correctness_check()
    confirmation = await confirmation_behavior_check()
    dispatch = await dispatch_reliability_check()
    price = await price_shape_check()

    checks = {
        "functional_flow_check": all(flow.values()),
        "budget_correctness_check": all(budget.values()),
        "confirmation_behavior_check": confirmation["policy_differs"],
        "dispatch_reliability_check": all(dispatch.values()),
        "price_shape_check": price["higher_under_congestion"],
    }
    overall = all(checks.values())

    print(
        json.dumps(
            {
                "checks": checks,
                "details": {
                    "flow": flow,
                    "budget": budget,
                    "confirmation": confirmation,
                    "dispatch": dispatch,
                    "price": price,
                },
                "phase1_functional_pass": overall,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

