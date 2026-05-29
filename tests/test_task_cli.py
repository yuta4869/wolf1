"""CLI tests for task-extract-* / calendar-draft-* (PR #30)."""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import List, Tuple

from wolf.cli import EXIT_DENIED, EXIT_SUCCESS, main as cli_main


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_inproc(args: List[str]) -> Tuple[int, str, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        code = cli_main(args)
    return code, out_buf.getvalue(), err_buf.getvalue()


class _ProjectFixture:
    """Minimal scratch project root for CLI tests."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "src" / "wolf" / "core").mkdir(parents=True)
        (self.root / "src" / "wolf" / "core" / "types.py").write_text(
            "x = 1\n", encoding="utf-8"
        )
        (self.root / "mail").mkdir()
        # Copy the task-marker fixtures.
        for name in ("tasks_sample.eml", "tasks_sample.mbox"):
            shutil.copy(
                REPO_ROOT / "tests" / "fixtures" / "mail" / name,
                self.root / "mail" / name,
            )

    def cleanup(self) -> None:
        self.tmp.cleanup()


class _AuditAware:
    """Mixin: read the audit jsonl from the fixture."""

    fixture: _ProjectFixture

    def _audit_path(self):
        return self.fixture.root / "var" / "audit" / "audit.jsonl"

    def _events(self):
        p = self._audit_path()
        if not p.exists():
            return []
        with p.open("r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class TaskExtractMailCliTest(unittest.TestCase, _AuditAware):
    def setUp(self) -> None:
        self.fixture = _ProjectFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def _run(self, *extra_args: str):
        return _run_inproc(
            ["--project-root", str(self.fixture.root), *extra_args]
        )

    def test_eml_fake_success(self) -> None:
        code, out, _ = self._run(
            "task-extract-mail",
            "--path", "mail/tasks_sample.eml",
            "--backend", "fake",
        )
        self.assertEqual(code, EXIT_SUCCESS, msg=out)
        payload = json.loads(out)
        r = payload["result"]
        self.assertEqual(r["source_kind"], "local_mail")
        self.assertGreaterEqual(r["task_count"], 2)
        self.assertGreaterEqual(r["event_count"], 1)
        # Tasks include the explicit ISO due dates from the fixture.
        due_dates = {t["due_date"] for t in r["tasks"]}
        self.assertIn("2026-06-10", due_dates)

    def test_mbox_fake_success(self) -> None:
        code, out, _ = self._run(
            "task-extract-mail",
            "--path", "mail/tasks_sample.mbox",
            "--backend", "fake",
        )
        self.assertEqual(code, EXIT_SUCCESS, msg=out)
        payload = json.loads(out)
        # The mbox has two messages each with markers; expect >= 2 tasks.
        self.assertGreaterEqual(payload["result"]["task_count"], 2)

    def test_text_output(self) -> None:
        code, out, _ = self._run(
            "task-extract-mail",
            "--path", "mail/tasks_sample.eml",
            "--backend", "fake",
            "--output", "text",
        )
        self.assertEqual(code, EXIT_SUCCESS, msg=out)
        self.assertIn("TASK", out)

    def test_filter_subject_keeps_match(self) -> None:
        code, out, _ = self._run(
            "task-extract-mail",
            "--path", "mail/tasks_sample.mbox",
            "--filter-subject", "Q3",
            "--backend", "fake",
        )
        self.assertEqual(code, EXIT_SUCCESS, msg=out)
        payload = json.loads(out)
        # Both messages have "Q3" in subject so both pass.
        self.assertGreaterEqual(payload["result"]["message_count"], 1)

    def test_audit_emitted_with_metadata_only(self) -> None:
        code, _out, _ = self._run(
            "task-extract-mail",
            "--path", "mail/tasks_sample.eml",
            "--backend", "fake",
        )
        self.assertEqual(code, EXIT_SUCCESS)
        kinds = [e["action_kind"] for e in self._events()]
        self.assertIn("task.extract_mail", kinds)
        ev = next(
            e for e in reversed(self._events())
            if e["action_kind"] == "task.extract_mail"
        )
        d = ev["detail"]
        self.assertEqual(d["source_kind"], "local_mail")
        self.assertEqual(d["provider"], "fake")
        self.assertGreater(d["task_count"], 0)
        # Body / token never recorded.
        text = self._audit_path().read_text(encoding="utf-8")
        self.assertNotIn("Send the Q3 numbers to Alice", text)
        self.assertNotIn("Bearer ", text)

    def test_audit_failure_returns_audit_log_stage(self) -> None:
        from unittest.mock import patch

        with patch(
            "wolf.cli._audit_task_event",
            side_effect=OSError("disk"),
        ):
            code, out, _ = self._run(
                "task-extract-mail",
                "--path", "mail/tasks_sample.eml",
                "--backend", "fake",
            )
        self.assertEqual(code, EXIT_DENIED)
        payload = json.loads(out)
        self.assertEqual(payload["stage"], "audit_log")


class TaskExtractGmailCliTest(unittest.TestCase, _AuditAware):
    def setUp(self) -> None:
        self.fixture = _ProjectFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def _run(self, *extra_args: str):
        return _run_inproc(
            ["--project-root", str(self.fixture.root), *extra_args]
        )

    def test_query_fake_success(self) -> None:
        code, out, _ = self._run(
            "task-extract-gmail",
            "--gmail-backend", "fake",
            "--query", "meeting",
            "--llm-backend", "fake",
        )
        self.assertEqual(code, EXIT_SUCCESS, msg=out)
        payload = json.loads(out)
        # msg_5 in the fake fixture carries task markers; expect >= 1.
        self.assertGreaterEqual(payload["result"]["task_count"], 1)

    def test_message_id_fake_success(self) -> None:
        code, out, _ = self._run(
            "task-extract-gmail",
            "--gmail-backend", "fake",
            "--message-id", "msg_5",
            "--llm-backend", "fake",
        )
        self.assertEqual(code, EXIT_SUCCESS, msg=out)
        payload = json.loads(out)
        self.assertGreater(payload["result"]["task_count"], 0)
        self.assertGreater(payload["result"]["event_count"], 0)

    def test_no_query_or_message_id_exits_two(self) -> None:
        code, _out, err = self._run(
            "task-extract-gmail",
            "--gmail-backend", "fake",
            "--llm-backend", "fake",
        )
        self.assertEqual(code, EXIT_DENIED)
        self.assertIn("requires", err.lower())

    def test_real_backend_missing_credentials_exits_two(self) -> None:
        code, out, _ = self._run(
            "task-extract-gmail",
            "--gmail-backend", "gmail",
            "--query", "x",
            "--llm-backend", "fake",
        )
        self.assertEqual(code, EXIT_DENIED)
        payload = json.loads(out)
        self.assertEqual(payload["stage"], "gmail_config")

    def test_audit_has_query_fingerprint(self) -> None:
        code, _out, _ = self._run(
            "task-extract-gmail",
            "--gmail-backend", "fake",
            "--query", "meeting UNIQUE-PR30-MARKER",
            "--llm-backend", "fake",
        )
        self.assertEqual(code, EXIT_SUCCESS)
        ev = next(
            e for e in reversed(self._events())
            if e["action_kind"] == "task.extract_gmail"
        )
        self.assertIn("query_fingerprint", ev["detail"])
        self.assertEqual(len(ev["detail"]["query_fingerprint"]), 12)
        text = self._audit_path().read_text(encoding="utf-8")
        self.assertNotIn("UNIQUE-PR30-MARKER", text)


class CalendarDraftMailCliTest(unittest.TestCase, _AuditAware):
    def setUp(self) -> None:
        self.fixture = _ProjectFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def _run(self, *extra_args: str):
        return _run_inproc(
            ["--project-root", str(self.fixture.root), *extra_args]
        )

    def test_ics_output(self) -> None:
        code, out, _ = self._run(
            "calendar-draft-mail",
            "--path", "mail/tasks_sample.eml",
            "--backend", "fake",
            "--output", "ics",
        )
        self.assertEqual(code, EXIT_SUCCESS, msg=out)
        self.assertIn("BEGIN:VCALENDAR", out)
        self.assertIn("END:VCALENDAR", out)
        self.assertIn("BEGIN:VEVENT", out)

    def test_text_output(self) -> None:
        code, out, _ = self._run(
            "calendar-draft-mail",
            "--path", "mail/tasks_sample.eml",
            "--backend", "fake",
            "--output", "text",
        )
        self.assertEqual(code, EXIT_SUCCESS, msg=out)
        self.assertIn("EVENT", out)

    def test_output_file_writes_ics(self) -> None:
        out_path = self.fixture.root / "var" / "out" / "events.ics"
        code, _out, _ = self._run(
            "calendar-draft-mail",
            "--path", "mail/tasks_sample.eml",
            "--backend", "fake",
            "--output", "text",
            "--output-file", str(out_path),
        )
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertTrue(out_path.exists())
        content = out_path.read_text(encoding="utf-8")
        self.assertIn("BEGIN:VCALENDAR", content)

    def test_audit_emitted(self) -> None:
        code, _, _ = self._run(
            "calendar-draft-mail",
            "--path", "mail/tasks_sample.eml",
            "--backend", "fake",
            "--output", "ics",
        )
        self.assertEqual(code, EXIT_SUCCESS)
        kinds = [e["action_kind"] for e in self._events()]
        self.assertIn("calendar.draft_mail", kinds)


class CalendarDraftGmailCliTest(unittest.TestCase, _AuditAware):
    def setUp(self) -> None:
        self.fixture = _ProjectFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def _run(self, *extra_args: str):
        return _run_inproc(
            ["--project-root", str(self.fixture.root), *extra_args]
        )

    def test_query_fake_success(self) -> None:
        code, out, _ = self._run(
            "calendar-draft-gmail",
            "--gmail-backend", "fake",
            "--query", "meeting",
            "--llm-backend", "fake",
            "--output", "ics",
        )
        self.assertEqual(code, EXIT_SUCCESS, msg=out)
        self.assertIn("BEGIN:VCALENDAR", out)

    def test_text_output(self) -> None:
        code, out, _ = self._run(
            "calendar-draft-gmail",
            "--gmail-backend", "fake",
            "--message-id", "msg_5",
            "--llm-backend", "fake",
            "--output", "text",
        )
        self.assertEqual(code, EXIT_SUCCESS, msg=out)
        self.assertIn("EVENT", out)

    def test_token_not_in_stdout_stderr(self) -> None:
        # Token is never set on the fake backend, but we assert that
        # no "Bearer " marker leaks into output.
        code, out, err = self._run(
            "calendar-draft-gmail",
            "--gmail-backend", "fake",
            "--message-id", "msg_5",
            "--llm-backend", "fake",
            "--output", "ics",
        )
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertNotIn("Bearer ", out)
        self.assertNotIn("Bearer ", err)

    def test_audit_emitted_with_query_fingerprint(self) -> None:
        code, _out, _ = self._run(
            "calendar-draft-gmail",
            "--gmail-backend", "fake",
            "--query", "meeting UNIQUE-PR30-CAL",
            "--llm-backend", "fake",
            "--output", "ics",
        )
        self.assertEqual(code, EXIT_SUCCESS)
        ev = next(
            e for e in reversed(self._events())
            if e["action_kind"] == "calendar.draft_gmail"
        )
        self.assertEqual(len(ev["detail"]["query_fingerprint"]), 12)
        text = self._audit_path().read_text(encoding="utf-8")
        self.assertNotIn("UNIQUE-PR30-CAL", text)

    def test_ollama_backend_mocked(self) -> None:
        import urllib.request
        from unittest.mock import patch

        class _Resp:
            def read(self) -> bytes:
                return json.dumps(
                    {
                        "response": json.dumps(
                            {
                                "tasks": [],
                                "events": [
                                    {
                                        "title": "ollama event",
                                        "start_date": "2026-06-18",
                                        "start_time": "14:00:00",
                                        "end_date": "2026-06-18",
                                        "end_time": "15:00:00",
                                        "timezone": "UTC",
                                        "location": "",
                                        "attendees": [],
                                    }
                                ],
                            }
                        ),
                        "done": True,
                    }
                ).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_a) -> None:
                return None

        def fake_urlopen(req, timeout):  # noqa: ARG001
            return _Resp()

        with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            code, out, _ = self._run(
                "calendar-draft-gmail",
                "--gmail-backend", "fake",
                "--message-id", "msg_5",
                "--llm-backend", "ollama",
                "--model", "llama3.1",
                "--output", "ics",
            )
        self.assertEqual(code, EXIT_SUCCESS, msg=out)
        self.assertIn("SUMMARY:ollama event", out)


if __name__ == "__main__":
    unittest.main()
