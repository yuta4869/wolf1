"""Unit tests for FakeGmailClient."""

from __future__ import annotations

import unittest

from wolf.gmail import GmailClientError
from wolf.gmail.fake import FakeGmailClient
from wolf.gmail.types import GmailDraft, GmailMessage, GmailSearchHit


class FakeSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeGmailClient()

    def test_empty_query_rejected(self) -> None:
        with self.assertRaises(GmailClientError):
            self.client.search(query="", max_results=10)

    def test_invalid_max_results(self) -> None:
        with self.assertRaises(GmailClientError):
            self.client.search(query="meeting", max_results=0)

    def test_meeting_query_matches_planning_subject(self) -> None:
        hits = self.client.search(query="meeting", max_results=10)
        self.assertGreaterEqual(len(hits), 2)
        ids = {h.message_id for h in hits}
        self.assertIn("msg_1", ids)
        self.assertIn("msg_2", ids)

    def test_returns_search_hit_dataclass(self) -> None:
        hits = self.client.search(query="meeting", max_results=10)
        for h in hits:
            self.assertIsInstance(h, GmailSearchHit)
            self.assertTrue(h.message_id)
            self.assertTrue(h.thread_id)

    def test_max_results_caps_output(self) -> None:
        hits = self.client.search(query="meeting", max_results=1)
        self.assertEqual(len(hits), 1)

    def test_raise_on_search_propagates(self) -> None:
        c = FakeGmailClient(
            raise_on_search=GmailClientError("forced error"),
        )
        with self.assertRaises(GmailClientError):
            c.search(query="meeting", max_results=10)


class FakeReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeGmailClient()

    def test_read_known_id_returns_message(self) -> None:
        m = self.client.read(message_id="msg_1")
        self.assertIsInstance(m, GmailMessage)
        self.assertEqual(m.message_id, "msg_1")
        self.assertIn("Q3 planning", m.body_text)
        self.assertEqual(m.from_, "Alice Example <alice@example.invalid>")

    def test_read_unknown_id_raises(self) -> None:
        with self.assertRaises(GmailClientError):
            self.client.read(message_id="msg_does_not_exist")

    def test_read_attachments_metadata(self) -> None:
        m = self.client.read(message_id="msg_3")
        self.assertTrue(m.has_attachments)
        self.assertEqual(len(m.attachments), 1)
        a = m.attachments[0]
        self.assertEqual(a.filename, "snack-list.pdf")
        self.assertEqual(a.mime_type, "application/pdf")
        self.assertGreater(a.size_bytes, 0)


class FakeCreateDraftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeGmailClient()

    def test_creates_draft_with_sequential_id(self) -> None:
        d1 = self.client.create_draft(
            to="alice@example.invalid",
            source_subject="Quarterly planning meeting",
            body="ありがとうございます。",
            in_reply_to="<fake-001@example.invalid>",
            references="<fake-001@example.invalid>",
            thread_id="thread_1",
        )
        d2 = self.client.create_draft(
            to="bob@example.invalid",
            source_subject="Re: Quarterly planning meeting",
            body="承知しました。",
        )
        self.assertIsInstance(d1, GmailDraft)
        self.assertEqual(d1.draft_id, "fake_draft_1")
        self.assertEqual(d2.draft_id, "fake_draft_2")
        self.assertEqual(d1.thread_id, "thread_1")
        self.assertEqual(d2.thread_id, "")

    def test_drafts_recorded_internally(self) -> None:
        self.client.create_draft(
            to="x@y.invalid",
            source_subject="anything",
            body="hello",
        )
        self.assertEqual(len(self.client.drafts), 1)
        self.assertEqual(self.client.drafts[0]["to"], "x@y.invalid")

    def test_raise_on_create_draft_propagates(self) -> None:
        c = FakeGmailClient(
            raise_on_create_draft=GmailClientError("forced"),
        )
        with self.assertRaises(GmailClientError):
            c.create_draft(
                to="x@y.invalid",
                source_subject="s",
                body="b",
            )


class FakeGetThreadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeGmailClient()

    def test_get_thread_returns_members(self) -> None:
        members = self.client.get_thread(thread_id="thread_1")
        self.assertEqual(len(members), 2)
        ids = [m.message_id for m in members]
        self.assertIn("msg_1", ids)
        self.assertIn("msg_2", ids)

    def test_get_thread_unknown_id_raises(self) -> None:
        with self.assertRaises(GmailClientError):
            self.client.get_thread(thread_id="no_such_thread")

    def test_get_thread_empty_id_raises(self) -> None:
        with self.assertRaises(GmailClientError):
            self.client.get_thread(thread_id="")

    def test_raise_on_get_thread_propagates(self) -> None:
        c = FakeGmailClient(
            raise_on_get_thread=GmailClientError("forced"),
        )
        with self.assertRaises(GmailClientError):
            c.get_thread(thread_id="thread_1")


class FakeHasNoSendTest(unittest.TestCase):
    def test_fake_does_not_expose_send_method(self) -> None:
        c = FakeGmailClient()
        # Hard guarantee: there must be no `send` attribute, and trying to
        # call one must raise AttributeError. The repo's policy forbids
        # implementing mail send.
        self.assertFalse(hasattr(c, "send"))
        with self.assertRaises(AttributeError):
            getattr(c, "send")(message_id="msg_1")  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
