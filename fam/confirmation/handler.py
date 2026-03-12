from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fam.types import ToolDecision


@dataclass(frozen=True)
class ConfirmationResult:
    decision: ToolDecision
    reason: str
    price: Decimal


class ConfirmationHandler:
    """MVP confirmation policy.

    If tool price is below threshold, auto-approve.
    If above threshold and no explicit decision provided, default to reason_more.
    """

    def __init__(
        self,
        congestion_threshold: Decimal = Decimal("0.70"),
        severe_multiplier: Decimal = Decimal("2.5"),
    ) -> None:
        self.congestion_threshold = congestion_threshold
        self.severe_multiplier = severe_multiplier

    def evaluate(
        self,
        tool_price: Decimal,
        budget_balance: Decimal,
        requested_decision: ToolDecision | None = None,
    ) -> ConfirmationResult:
        if budget_balance < tool_price:
            return ConfirmationResult(ToolDecision.CANCEL, "insufficient_budget", tool_price)

        if tool_price < self.congestion_threshold:
            return ConfirmationResult(ToolDecision.APPROVE, "auto_approve_low_congestion", tool_price)

        if requested_decision is None:
            severe_threshold = self.congestion_threshold * self.severe_multiplier
            if tool_price < severe_threshold:
                return ConfirmationResult(
                    ToolDecision.APPROVE,
                    "soft_congestion_auto_approve",
                    tool_price,
                )
            return ConfirmationResult(
                ToolDecision.REASON_MORE,
                "high_price_default_reason_more",
                tool_price,
            )
        return ConfirmationResult(requested_decision, "agent_selected", tool_price)

