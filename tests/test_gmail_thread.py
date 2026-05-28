"""Tests for src/wolf/gmail/thread.py."""

from __future__ import annotations

import unittest

from wolf.gmail import FakeGmailClient
from wolf.gmail.thread import GmailThread, GmailThreadMessage, build_threads
from wolf.gmail.types import GmailMessage


def _msg(
    *,
    message_id: str,
    thread_id: str = "",
    subject: str = "",
    from_: str = "x@y.invalid",
    date: str = "Mon, 1 Jan 2026 09:00:00 +0000",
    rfc822_message_id: str = "",
    body_text: str = "b",
) -> GmailMessage:
    return GmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        subject=subject,
        from_=from_,
        to="",
        cc="",
        date=date,
        rfc822_message_id=rfc822_message_id,
        snippet="",
        body_text=body_text,
        has_attachments=False,
    )


class ThreadIdGroupingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.messages = FakeGmailClient().messages

    def test_two_threads(self) -> None:
        # The fake fixture has thread_1 (planning, 2 msgs) and thread_2
        # (snacks, 2 msgs).
        ts = build_threads(self.messages)
        self.assertEqual(len(ts), 2)
        ids = {t.thread_id for t in ts}
        self.assertEqual(ids, {"thread_1", "thread_2"})

    def test_thread_1_grouping(self) -> None:
        ts = build_threads(self.messages)
        t1 = next(t for t in ts if t.thread_id == "thread_1")
        self.assertEqual(t1.message_count, 2)
        # Sorted by date, oldest first.
        self.assertEqual(t1.messages[0].gmail_message_id, "msg_1")
        self.assertEqual(t1.messages[1].gmail_message_id, "msg_2")

    def test_participants_aggregation(self) -> None:
        ts = build_threads(self.messages)
        t1 = next(t for t in ts if t.thread_id == "thread_1")
        joined = " ".join(t1.participants)
        for who in ("Alice", "Bob"):
            self.assertIn(who, joined)

    def test_no_body_in_thread_output(self) -> None:
        ts = build_threads(self.messages)
        for t in ts:
            for m in t.messages:
                # GmailThreadMessage has no `body` field at all.
                self.assertNotIn("body", dir(m))
                self.assertNotIn("body_text", dir(m))

    def test_subject_is_normalized(self) -> None:
        ts = build_threads(self.messages)
        t1 = next(t for t in ts if t.thread_id == "thread_1")
        # representative_subject strips leading Re:/Fwd: etc.
        self.assertNotIn("Re:", t1.subject)


class RfcAndSubjectFallbackTest(unittest.TestCase):
    """When thread_id is missing, fall back to rfc822 / subject."""

    def test_subject_fallback_groups_without_thread_id(self) -> None:
        msgs = [
            _msg(
                message_id="a",
                thread_id="",
                subject="Lunch tomorrow",
                rfc822_message_id="<a@x>",
                date="Mon, 1 Jan 2026 09:00:00 +0000",
            ),
            _msg(
                message_id="b",
                thread_id="",
                subject="Re: Lunch tomorrow",
                rfc822_message_id="<b@x>",
                date="Mon, 1 Jan 2026 10:00:00 +0000",
            ),
            _msg(
                message_id="c",
                thread_id="",
                subject="Fwd: Lunch tomorrow",
                rfc822_message_id="<c@x>",
                date="Mon, 1 Jan 2026 11:00:00 +0000",
            ),
        ]
        ts = build_threads(msgs)
        self.assertEqual(len(ts), 1)
        self.assertEqual(ts[0].message_count, 3)

    def test_different_subjects_remain_separate(self) -> None:
        msgs = [
            _msg(message_id="a", thread_id="", subject="Lunch tomorrow"),
            _msg(message_id="b", thread_id="", subject="Dinner tonight"),
        ]
        ts = build_threads(msgs)
        self.assertEqual(len(ts), 2)

    def test_mixed_threadid_and_fallback(self) -> None:
        # Some messages have a thread_id, others don't. They should
        # cluster into separate threads (threadId wins; subject is the
        # fallback only for the leftovers).
        msgs = [
            _msg(message_id="a", thread_id="t1", subject="Hi"),
            _msg(message_id="b", thread_id="t1", subject="Re: Hi"),
            _msg(message_id="c", thread_id="", subject="Foo"),
            _msg(message_id="d", thread_id="", subject="Re: Foo"),
        ]
        ts = build_threads(msgs)
        self.assertEqual(len(ts), 2)
        counts = sorted(t.message_count for t in ts)
        self.assertEqual(counts, [2, 2])


class EmptyAndEdgeCasesTest(unittest.TestCase):
    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(build_threads([]), [])

    def test_single_message_makes_one_thread(self) -> None:
        msgs = [_msg(message_id="only", thread_id="t")]
        ts = build_threads(msgs)
        self.assertEqual(len(ts), 1)
        self.assertEqual(ts[0].message_count, 1)
        self.assertEqual(ts[0].thread_id, "t")

    def test_threads_sorted_by_first_date(self) -> None:
        msgs = [
            _msg(
                message_id="late",
                thread_id="t-late",
                subject="X",
                date="Mon, 5 Jan 2026 09:00:00 +0000",
            ),
            _msg(
                message_id="early",
                thread_id="t-early",
                subject="Y",
                date="Mon, 1 Jan 2026 09:00:00 +0000",
            ),
        ]
        ts = build_threads(msgs)
        self.assertEqual([t.thread_id for t in ts], ["t-early", "t-late"])


if __name__ == "__main__":
    unittest.main()
