from __future__ import annotations

from decimal import Decimal

from fam.budget.manager import BudgetManager
from fam.confirmation.handler import ConfirmationHandler
from fam.dispatch.queue import ToolDispatchQueue
from fam.types import PriceUpdate, ToolDecision


class MVPToolCallAdapter:
    """Minimal adapter-compatible handler for tool call interception."""

    def __init__(
        self,
        budget_manager: BudgetManager,
        confirmation_handler: ConfirmationHandler,
        dispatch_queue: ToolDispatchQueue,
    ) -> None:
        self.budget_manager = budget_manager
        self.confirmation_handler = confirmation_handler
        self.dispatch_queue = dispatch_queue

    async def intercept_tool_call(
        self,
        *,
        agent_id: str,
        endpoint_id: str,
        payload: dict,
        prices: PriceUpdate,
        requested_decision: ToolDecision | None = None,
    ) -> dict:
        tool_price = prices.tool_prices.get(endpoint_id)
        if tool_price is None:
            raise KeyError(f"Missing price for endpoint: {endpoint_id}")

        balance = await self.budget_manager.check_balance(agent_id)
        decision = self.confirmation_handler.evaluate(
            tool_price=tool_price,
            budget_balance=balance,
            requested_decision=requested_decision,
        )
        if decision.decision is ToolDecision.CANCEL:
            return {"status": "cancelled", "reason": decision.reason}
        if decision.decision is ToolDecision.REASON_MORE:
            # MVP behavior: do not dispatch; caller can continue reasoning.
            return {"status": "deferred", "reason": decision.reason}

        ok = await self.budget_manager.deduct_tool(
            agent_id=agent_id, amount=tool_price, note=f"endpoint={endpoint_id}"
        )
        if not ok:
            return {"status": "cancelled", "reason": "insufficient_budget_post_check"}

        post_balance = await self.budget_manager.check_balance(agent_id)
        result = await self.dispatch_queue.submit(
            agent_id=agent_id,
            endpoint_id=endpoint_id,
            payload=payload,
            cost=tool_price,
            budget_remaining=post_balance,
        )
        return {"status": "completed", "cost": str(tool_price), "result": result}

    async def meter_reasoning_tokens(
        self,
        agent_id: str,
        token_count: int,
        reasoning_price: Decimal,
        speculative: bool = False,
    ) -> bool:
        cost = Decimal(token_count) * reasoning_price
        return await self.budget_manager.deduct_reasoning(
            agent_id=agent_id,
            amount=cost,
            speculative=speculative,
            note=f"tokens={token_count}",
        )

