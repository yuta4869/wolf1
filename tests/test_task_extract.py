"""Tests for src/wolf/tasks/extract.py."""

from __future__ import annotations

import json
import unittest

from wolf.tasks import (
    ExtractionResult,
    extract_candidates,
    extract_candidates_from_text,
)
from wolf.tasks.extract import _truncate_evidence
from wolf.tasks.types import CalendarEventCandidate, TaskCandidate


class _StubLLM:
    def __init__(self, output: str = "") -> None:
        self._output = output
        self.calls = 0

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        self.calls += 1
        return self._output


class ExtractFromValidJsonTest(unittest.TestCase):
    def test_tasks_and_events_parsed(self) -> None:
        out = json.dumps(
            {
                "tasks": [
                    {
                        "title": "Send Q3 numbers",
                        "due_date": "2026-06-10",
                        "due_time": "",
                        "timezone": "",
                        "evidence_snippet": "Action item: Send Q3...",
                    }
                ],
                "events": [
                    {
                        "title": "planning sync",
                        "start_date": "2026-06-18",
                        "start_time": "14:00:00",
                        "end_date": "2026-06-18",
                        "end_time": "15:00:00",
                        "timezone": "UTC",
                        "location": "room A",
                        "attendees": ["alice@x", "bob@y"],
                    }
                ],
            }
        )
        res = extract_candidates_from_text(
            llm_output=out,
            body="raw mail body",
            source_kind="local_mail",
            source_id="mid-1",
            source_subject="Q3 prep",
            source_from="alice@x",
        )
        self.assertEqual(len(res.tasks), 1)
        self.assertEqual(res.tasks[0].title, "Send Q3 numbers")
        self.assertEqual(res.tasks[0].due_date, "2026-06-10")
        self.assertEqual(len(res.events), 1)
        self.assertEqual(res.events[0].attendees, ("alice@x", "bob@y"))
        self.assertFalse(res.used_fallback)

    def test_markdown_fenced_json_parsed(self) -> None:
        out = "```json\n" + json.dumps({"tasks": [{"title": "X", "due_date": "2026-06-01"}], "events": []}) + "\n```\n"
        res = extract_candidates_from_text(
            llm_output=out,
            body="",
            source_kind="local_mail",
            source_id="m",
            source_subject="s",
            source_from="f",
        )
        self.assertEqual(len(res.tasks), 1)
        self.assertEqual(res.tasks[0].title, "X")

    def test_task_without_title_skipped(self) -> None:
        out = json.dumps(
            {"tasks": [{"title": "", "due_date": "2026-06-01"}], "events": []}
        )
        res = extract_candidates_from_text(
            llm_output=out, body="", source_kind="local_mail",
            source_id="m", source_subject="s", source_from="f",
        )
        self.assertEqual(len(res.tasks), 0)

    def test_event_without_start_date_skipped(self) -> None:
        out = json.dumps(
            {"tasks": [], "events": [{"title": "x", "start_date": ""}]}
        )
        res = extract_candidates_from_text(
            llm_output=out, body="", source_kind="local_mail",
            source_id="m", source_subject="s", source_from="f",
        )
        self.assertEqual(len(res.events), 0)


class ExtractWithHeuristicFallbackTest(unittest.TestCase):
    BODY = (
        "Hi team,\n\n"
        "Action item: Send the Q3 numbers to Alice by 2026-06-10\n"
        "Action item: Confirm the budget freeze on 2026-06-12\n"
        "Due: 2026-06-15\n"
        "Meeting: planning sync on 2026-06-18 14:00\n"
    )

    def test_non_json_output_triggers_fallback(self) -> None:
        res = extract_candidates_from_text(
            llm_output="not JSON at all",
            body=self.BODY,
            source_kind="local_mail",
            source_id="mid-2",
            source_subject="Q3",
            source_from="alice@x",
        )
        self.assertTrue(res.used_fallback)
        self.assertIn("llm_output_not_json", res.warnings)
        # 2 "Action item:" + 1 "Due:" → 3 tasks; 1 Meeting → 1 event.
        self.assertEqual(len(res.tasks), 3)
        self.assertEqual(len(res.events), 1)

    def test_empty_string_output_triggers_fallback(self) -> None:
        res = extract_candidates_from_text(
            llm_output="",
            body=self.BODY,
            source_kind="local_mail",
            source_id="mid-3",
            source_subject="Q3",
            source_from="alice@x",
        )
        self.assertTrue(res.used_fallback)
        self.assertGreater(len(res.tasks), 0)

    def test_valid_json_with_no_candidates_still_falls_back(self) -> None:
        res = extract_candidates_from_text(
            llm_output=json.dumps({"tasks": [], "events": []}),
            body=self.BODY,
            source_kind="local_mail",
            source_id="mid-4",
            source_subject="Q3",
            source_from="alice@x",
        )
        # Empty JSON falls back to heuristic so the user gets something.
        self.assertTrue(res.used_fallback)
        self.assertGreater(len(res.tasks), 0)


class ExtractCandidatesViaLLMAdapterTest(unittest.TestCase):
    def test_llm_failure_falls_back(self) -> None:
        class _BoomLLM:
            def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
                raise RuntimeError("oops")

        res = extract_candidates(
            llm=_BoomLLM(),
            body="Action item: Send by 2026-06-10\n",
            source_kind="local_mail",
            source_id="m",
            source_subject="s",
        )
        self.assertTrue(res.used_fallback)
        self.assertTrue(any(w.startswith("llm_error:") for w in res.warnings))
        self.assertEqual(len(res.tasks), 1)

    def test_empty_body_returns_empty(self) -> None:
        res = extract_candidates(
            llm=_StubLLM(""),
            body="",
            source_kind="local_mail",
            source_id="m",
            source_subject="s",
        )
        self.assertEqual(res.tasks, [])
        self.assertEqual(res.events, [])
        self.assertFalse(res.used_fallback)


class EvidenceBoundedTest(unittest.TestCase):
    def test_truncate_evidence_under_limit_unchanged(self) -> None:
        self.assertEqual(_truncate_evidence("short"), "short")

    def test_truncate_evidence_caps_bytes(self) -> None:
        long = "x" * 1000
        out = _truncate_evidence(long, max_bytes=50)
        self.assertEqual(len(out.encode("utf-8")), 50)

    def test_truncate_evidence_utf8_safe(self) -> None:
        s = "日本語テスト" * 50
        out = _truncate_evidence(s, max_bytes=20)
        # Must be a valid UTF-8 string (decode back without error).
        out.encode("utf-8").decode("utf-8")
        self.assertLessEqual(len(out.encode("utf-8")), 20)


class RawBodyNotInReprTest(unittest.TestCase):
    """The dataclasses keep evidence_snippet bounded; the full body
    must never appear in repr / dict output."""

    def test_task_dict_does_not_expose_full_body(self) -> None:
        # Stuff the body with a long unique-marker prefix, then drop
        # the actionable line on a fresh line so the regex picks it
        # up. The dict serialization should NOT contain the body
        # filler.
        filler = "SECRET-BODY-MARKER " * 100
        body = filler + "\nAction item: A by 2026-06-10\n"
        res = extract_candidates_from_text(
            llm_output="not json",
            body=body,
            source_kind="local_mail",
            source_id="m",
            source_subject="s",
            source_from="f",
        )
        self.assertGreater(len(res.tasks), 0)
        d = res.tasks[0].to_dict()
        # evidence_snippet is bounded; the raw filler must not appear.
        self.assertNotIn("SECRET-BODY-MARKER SECRET-BODY-MARKER", d["evidence_snippet"])


if __name__ == "__main__":
    unittest.main()
