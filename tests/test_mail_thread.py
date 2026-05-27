"""Tests for src/wolf/mail/thread.py."""

from __future__ import annotations

import unittest
from pathlib import Path

from wolf.mail.read_local import ParsedMail, read_mail_any
from wolf.mail.thread import Thread, ThreadMessage, build_threads


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "mail"


class ThreadByMessageIdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.messages = read_mail_any(FIXTURES / "thread.mbox").messages

    def test_thread_count(self) -> None:
        # Q3 planning (3 messages) + 2 unrelated "Office snacks" mails.
        threads = build_threads(self.messages)
        self.assertEqual(len(threads), 3)

    def test_q3_thread_groups_via_in_reply_to(self) -> None:
        threads = build_threads(self.messages)
        q3 = next(t for t in threads if "Q3 planning" in t.subject)
        self.assertEqual(q3.message_count, 3)
        ids = [m.message_id for m in q3.messages]
        self.assertEqual(
            ids,
            [
                "<thread-001@example.invalid>",
                "<thread-002@example.invalid>",
                "<thread-003@example.invalid>",
            ],
        )

    def test_q3_thread_participants(self) -> None:
        threads = build_threads(self.messages)
        q3 = next(t for t in threads if "Q3 planning" in t.subject)
        self.assertEqual(len(q3.participants), 3)
        joined = " ".join(q3.participants)
        for name in ("Alice", "Bob", "Carol"):
            self.assertIn(name, joined)

    def test_q3_thread_first_and_last_date(self) -> None:
        threads = build_threads(self.messages)
        q3 = next(t for t in threads if "Q3 planning" in t.subject)
        self.assertIn("09:00", q3.first_date)
        self.assertIn("11:00", q3.last_date)

    def test_thread_does_not_carry_body(self) -> None:
        threads = build_threads(self.messages)
        q3 = next(t for t in threads if "Q3 planning" in t.subject)
        for m in q3.messages:
            # ThreadMessage has no `body` field at all — assert via dir.
            self.assertNotIn("body", dir(m))

    def test_thread_messages_are_sorted_by_date(self) -> None:
        threads = build_threads(self.messages)
        q3 = next(t for t in threads if "Q3 planning" in t.subject)
        dates = [m.date for m in q3.messages]
        self.assertEqual(dates, sorted(dates))


class ThreadFallbackSubjectTest(unittest.TestCase):
    def test_normalized_subject_groups_replies_without_in_reply_to(self) -> None:
        # Two messages with the same normalized subject but no
        # In-Reply-To / References should still cluster together.
        def make(subject: str, msgid: str, from_: str, date: str) -> ParsedMail:
            return ParsedMail(
                subject=subject,
                from_=from_,
                to="",
                cc="",
                date=date,
                message_id=msgid,
                body="hello",
                has_attachments=False,
                content_type="text/plain",
                byte_size=5,
            )

        msgs = [
            make("Lunch tomorrow", "<a@x>", "alice@x", "Mon, 1 Jan 2026 09:00:00 +0000"),
            make("Re: Lunch tomorrow", "<b@x>", "bob@x", "Mon, 1 Jan 2026 10:00:00 +0000"),
            make("Fwd: Lunch tomorrow", "<c@x>", "carol@x", "Mon, 1 Jan 2026 11:00:00 +0000"),
        ]
        threads = build_threads(msgs)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].message_count, 3)

    def test_different_subjects_remain_separate(self) -> None:
        def make(subject: str, msgid: str) -> ParsedMail:
            return ParsedMail(
                subject=subject,
                from_="x@y",
                to="",
                cc="",
                date="Mon, 1 Jan 2026 09:00:00 +0000",
                message_id=msgid,
                body="b",
                has_attachments=False,
                content_type="text/plain",
                byte_size=1,
            )

        msgs = [make("Lunch tomorrow", "<a@x>"), make("Dinner tonight", "<b@x>")]
        threads = build_threads(msgs)
        self.assertEqual(len(threads), 2)


class EmptyInputTest(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        self.assertEqual(build_threads([]), [])


if __name__ == "__main__":
    unittest.main()
