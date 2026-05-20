from __future__ import annotations

import unittest

from wolf.adapters.llm import LLMAdapter
from wolf.adapters.mail import MailAdapter, MailDraft
from wolf.adapters.robot_transport import RobotState, RobotTransport
from wolf.core.errors import SafetyViolation
from wolf.fakes.llm import FakeLLM
from wolf.fakes.mail import FakeMail
from wolf.fakes.robot import FakeRobotTransport


class FakeLLMTest(unittest.TestCase):
    def test_implements_protocol(self) -> None:
        self.assertIsInstance(FakeLLM(), LLMAdapter)

    def test_summarize_returns_text(self) -> None:
        result = FakeLLM().summarize("hello world")
        self.assertIn("SUMMARY", result)

    def test_records_calls(self) -> None:
        llm = FakeLLM()
        llm.summarize("x")
        llm.generate("y")
        self.assertEqual(len(llm.calls), 2)


class FakeMailTest(unittest.TestCase):
    def test_implements_protocol(self) -> None:
        self.assertIsInstance(FakeMail(), MailAdapter)

    def test_create_and_send_with_confirmation(self) -> None:
        mail = FakeMail()
        did = mail.create_draft(
            MailDraft(to="a@b.com", subject="hi", body="hello")
        )
        mail.send(did, confirmation_token="ok")
        self.assertEqual(len(mail.sent), 1)
        self.assertNotIn(did, mail.drafts)

    def test_send_without_confirmation_fails(self) -> None:
        mail = FakeMail()
        did = mail.create_draft(
            MailDraft(to="a@b.com", subject="hi", body="hello")
        )
        with self.assertRaises(PermissionError):
            mail.send(did, confirmation_token="")

    def test_send_unknown_draft_fails(self) -> None:
        with self.assertRaises(KeyError):
            FakeMail().send("nope", confirmation_token="ok")


class FakeRobotTest(unittest.TestCase):
    def test_implements_protocol(self) -> None:
        self.assertIsInstance(FakeRobotTransport(), RobotTransport)

    def test_execute_motion_records_command(self) -> None:
        robot = FakeRobotTransport()
        robot.execute_motion(command={"linear": 0.1}, confirmation_token="ok")
        self.assertEqual(robot.executed, [{"linear": 0.1}])

    def test_execute_without_confirmation_fails(self) -> None:
        robot = FakeRobotTransport()
        with self.assertRaises(PermissionError):
            robot.execute_motion(command={"linear": 0.1}, confirmation_token="")

    def test_execute_after_estop_raises_safety_violation(self) -> None:
        robot = FakeRobotTransport()
        robot.emergency_stop()
        with self.assertRaises(SafetyViolation):
            robot.execute_motion(
                command={"linear": 0.1}, confirmation_token="ok"
            )
        self.assertEqual(robot.estop_count, 1)

    def test_manual_override_blocks_execution(self) -> None:
        state = RobotState(
            battery_pct=80.0,
            latency_ms=10.0,
            sensor_health=True,
            lidar_health=True,
            e_stop=False,
            manual_override=True,
        )
        robot = FakeRobotTransport(state=state)
        with self.assertRaises(SafetyViolation):
            robot.execute_motion(
                command={"linear": 0.1}, confirmation_token="ok"
            )


if __name__ == "__main__":
    unittest.main()
