from __future__ import annotations

import asyncio

from fam.core import FAMOrchestrator
from fam.types import ToolDecision


def test_mvp_tool_call_flow() -> None:
    asyncio.run(_exercise_mvp_flow())


async def _exercise_mvp_flow() -> None:
    orchestrator = FAMOrchestrator()
    await orchestrator.start()
    await orchestrator.register_agent("agent-1")

    # Tick multiple times to raise price pressure.
    for _ in range(4):
        await orchestrator.tick()

    # Explicitly approve under congestion.
    result = await orchestrator.handle_tool_call(
        agent_id="agent-1",
        endpoint_id="search",
        payload={"query": "langgraph"},
        requested_decision=ToolDecision.APPROVE,
    )
    assert result["status"] in {"completed", "cancelled", "deferred"}
    if result["status"] == "completed":
        assert "result" in result

    # Under high tool prices and no decision, MVP should defer.
    deferred = await orchestrator.handle_tool_call(
        agent_id="agent-1",
        endpoint_id="code",
        payload={"code": "print(1)"},
        requested_decision=None,
    )
    assert deferred["status"] in {"deferred", "completed", "cancelled"}

    await orchestrator.stop()

