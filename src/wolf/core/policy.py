from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet

from .types import Action, Decision, RiskLevel


CRITICAL_CONTEXT_FLAGS = (
    "near_people",
    "near_animals",
    "near_fragile",
    "near_water",
    "near_fire",
    "near_chemicals",
    "near_heavy",
)


@dataclass(frozen=True)
class PolicyEngine:
    known_actions: FrozenSet[str]
    approved_high_risk: FrozenSet[str] = field(default_factory=frozenset)

    def evaluate(self, action: Action) -> Decision:
        if action.kind not in self.known_actions:
            return Decision.DENY

        if any(bool(action.context.get(flag)) for flag in CRITICAL_CONTEXT_FLAGS):
            return Decision.DENY

        if action.risk is RiskLevel.CRITICAL:
            return Decision.DENY

        if action.risk is RiskLevel.HIGH:
            if action.kind in self.approved_high_risk:
                return Decision.ALLOW
            return Decision.REQUIRE_CONFIRMATION

        if action.risk in (RiskLevel.LOW, RiskLevel.MEDIUM):
            return Decision.ALLOW

        return Decision.DENY
