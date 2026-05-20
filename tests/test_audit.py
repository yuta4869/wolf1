from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Optional

from wolf.core.audit import AuditLogger
from wolf.core.types import AuditEvent


def _event(detail: Optional[Mapping[str, Any]] = None) -> AuditEvent:
    return AuditEvent(
        ts="2026-05-20T00:00:00.000000Z",
        actor="orchestrator",
        action_kind="mail.send",
        decision="allow",
        target="alice@example.com",
        outcome="executed",
        detail=detail or {},
    )


class AuditLoggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "audit.jsonl"
        self.logger = AuditLogger(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_log_appends_single_json_line(self) -> None:
        self.logger.log(_event())
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["actor"], "orchestrator")
        self.assertEqual(parsed["action_kind"], "mail.send")

    def test_log_appends_multiple_lines(self) -> None:
        self.logger.log(_event())
        self.logger.log(_event())
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)

    def test_log_masks_sensitive_top_level_keys(self) -> None:
        self.logger.log(
            _event(
                detail={
                    "api_key": "sk-xxx",
                    "password": "p@ss",
                    "TOKEN": "abc",
                    "user": "alice",
                }
            )
        )
        parsed = json.loads(self.path.read_text(encoding="utf-8").strip())
        self.assertEqual(parsed["detail"]["api_key"], "***REDACTED***")
        self.assertEqual(parsed["detail"]["password"], "***REDACTED***")
        self.assertEqual(parsed["detail"]["TOKEN"], "***REDACTED***")
        self.assertEqual(parsed["detail"]["user"], "alice")

    def test_log_masks_nested_sensitive_keys(self) -> None:
        self.logger.log(
            _event(
                detail={
                    "auth": {"secret": "value"},
                    "items": [{"private_key": "pk"}],
                }
            )
        )
        parsed = json.loads(self.path.read_text(encoding="utf-8").strip())
        self.assertEqual(parsed["detail"]["auth"], "***REDACTED***")
        self.assertEqual(
            parsed["detail"]["items"][0]["private_key"], "***REDACTED***"
        )

    def test_tail_returns_last_n_events(self) -> None:
        for _ in range(5):
            self.logger.log(_event())
        tail = self.logger.tail(2)
        self.assertEqual(len(tail), 2)

    def test_tail_on_missing_file_returns_empty(self) -> None:
        missing_path = Path(self.tmp.name) / "missing.jsonl"
        logger = AuditLogger(missing_path)
        self.assertEqual(logger.tail(10), [])


if __name__ == "__main__":
    unittest.main()
