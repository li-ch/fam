from __future__ import annotations

import asyncio
from decimal import Decimal

from fam.types import AgentBudget


class BudgetManager:
    def __init__(self, default_balance: Decimal = Decimal("20"), replenishment_rate: Decimal = Decimal("1")) -> None:
        self.default_balance = default_balance
        self.replenishment_rate = replenishment_rate
        self._budgets: dict[str, AgentBudget] = {}
        self._lock = asyncio.Lock()

    async def register_agent(self, agent_id: str) -> AgentBudget:
        async with self._lock:
            budget = self._budgets.get(agent_id)
            if budget is not None:
                return budget
            budget = AgentBudget(
                agent_id=agent_id,
                balance=self.default_balance,
                replenishment_rate=self.replenishment_rate,
            )
            self._budgets[agent_id] = budget
            return budget

    async def check_balance(self, agent_id: str) -> Decimal:
        async with self._lock:
            return self._ensure(agent_id).balance

    async def deduct_tool(self, agent_id: str, amount: Decimal, note: str = "") -> bool:
        async with self._lock:
            budget = self._ensure(agent_id)
            if budget.balance < amount:
                return False
            budget.balance -= amount
            budget.cumulative_tool_spend += amount
            budget.history.append(f"tool:{amount}:{note}")
            return True

    async def deduct_reasoning(
        self, agent_id: str, amount: Decimal, speculative: bool = False, note: str = ""
    ) -> bool:
        async with self._lock:
            budget = self._ensure(agent_id)
            if budget.balance < amount:
                return False
            budget.balance -= amount
            if speculative:
                budget.cumulative_speculative_spend += amount
                budget.history.append(f"spec:{amount}:{note}")
            else:
                budget.cumulative_reasoning_spend += amount
                budget.history.append(f"reason:{amount}:{note}")
            return True

    async def replenish_all(self, seconds: float = 1.0) -> None:
        if seconds <= 0:
            return
        delta = self.replenishment_rate * Decimal(str(seconds))
        async with self._lock:
            for budget in self._budgets.values():
                budget.balance += delta
                budget.history.append(f"replenish:{delta}")

    def _ensure(self, agent_id: str) -> AgentBudget:
        budget = self._budgets.get(agent_id)
        if budget is None:
            budget = AgentBudget(
                agent_id=agent_id,
                balance=self.default_balance,
                replenishment_rate=self.replenishment_rate,
            )
            self._budgets[agent_id] = budget
        return budget

