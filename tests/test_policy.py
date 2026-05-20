from __future__ import annotations

import unittest

from wolf.core.policy import PolicyEngine
from wolf.core.types import Action, Decision, RiskLevel


KNOWN = frozenset({"llm.summarize", "mail.send", "robot.move", "file.delete"})


class PolicyEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PolicyEngine(known_actions=KNOWN)

    def test_low_risk_is_allowed(self) -> None:
        action = Action(kind="llm.summarize", risk=RiskLevel.LOW)
        self.assertEqual(self.engine.evaluate(action), Decision.ALLOW)

    def test_medium_risk_is_allowed(self) -> None:
        action = Action(kind="llm.summarize", risk=RiskLevel.MEDIUM)
        self.assertEqual(self.engine.evaluate(action), Decision.ALLOW)

    def test_high_risk_requires_confirmation_by_default(self) -> None:
        action = Action(kind="mail.send", risk=RiskLevel.HIGH)
        self.assertEqual(
            self.engine.evaluate(action), Decision.REQUIRE_CONFIRMATION
        )

    def test_high_risk_with_explicit_policy_is_allowed(self) -> None:
        engine = PolicyEngine(
            known_actions=KNOWN,
            approved_high_risk=frozenset({"mail.send"}),
        )
        action = Action(kind="mail.send", risk=RiskLevel.HIGH)
        self.assertEqual(engine.evaluate(action), Decision.ALLOW)

    def test_critical_risk_is_denied(self) -> None:
        action = Action(kind="file.delete", risk=RiskLevel.CRITICAL)
        self.assertEqual(self.engine.evaluate(action), Decision.DENY)

    def test_unknown_action_is_denied_fail_closed(self) -> None:
        action = Action(kind="anything.weird", risk=RiskLevel.LOW)
        self.assertEqual(self.engine.evaluate(action), Decision.DENY)

    def test_near_people_context_denies_even_low_risk(self) -> None:
        action = Action(
            kind="robot.move",
            risk=RiskLevel.LOW,
            context={"near_people": True},
        )
        self.assertEqual(self.engine.evaluate(action), Decision.DENY)

    def test_near_fragile_context_denies(self) -> None:
        action = Action(
            kind="robot.move",
            risk=RiskLevel.MEDIUM,
            context={"near_fragile": True},
        )
        self.assertEqual(self.engine.evaluate(action), Decision.DENY)

    def test_near_water_context_denies_high_risk(self) -> None:
        action = Action(
            kind="robot.move",
            risk=RiskLevel.HIGH,
            context={"near_water": True},
        )
        self.assertEqual(self.engine.evaluate(action), Decision.DENY)

    def test_falsy_critical_flag_does_not_trigger(self) -> None:
        action = Action(
            kind="robot.move",
            risk=RiskLevel.LOW,
            context={"near_people": False, "near_fire": 0},
        )
        self.assertEqual(self.engine.evaluate(action), Decision.ALLOW)


if __name__ == "__main__":
    unittest.main()
