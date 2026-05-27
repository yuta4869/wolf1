"""Tests for src/wolf/mail/search.py."""

from __future__ import annotations

import unittest
from pathlib import Path

from wolf.mail.read_local import read_mail_any
from wolf.mail.search import MailHit, search_mail


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "mail"


class MailSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.messages = read_mail_any(FIXTURES / "sample.mbox").messages

    def test_subject_hit(self) -> None:
        hits = search_mail(self.messages, "Quarterly")
        self.assertGreater(len(hits), 0)
        self.assertTrue(any(h.match_field == "subject" for h in hits))

    def test_from_hit(self) -> None:
        hits = search_mail(self.messages, "carol")
        self.assertGreaterEqual(len(hits), 1)
        self.assertTrue(any(h.match_field == "from" for h in hits))

    def test_body_hit(self) -> None:
        hits = search_mail(self.messages, "Q2 review")
        self.assertGreaterEqual(len(hits), 1)
        self.assertTrue(any(h.match_field == "body" for h in hits))
        # Snippet contains the matched text.
        body_hit = next(h for h in hits if h.match_field == "body")
        self.assertIn("Q2 review", body_hit.snippet)

    def test_no_hit(self) -> None:
        self.assertEqual(
            search_mail(self.messages, "no_such_token_42"), []
        )

    def test_empty_query(self) -> None:
        self.assertEqual(search_mail(self.messages, ""), [])

    def test_max_hits(self) -> None:
        hits = search_mail(self.messages, "Quarterly", max_hits=1)
        self.assertEqual(len(hits), 1)

    def test_snippet_is_bounded(self) -> None:
        # Replace one message body with a long body containing the
        # match deep inside; assert the snippet is shorter than the body.
        long_body = ("filler text. " * 200) + "find me here. " + (
            "more text. " * 200
        )
        from wolf.mail.read_local import ParsedMail

        synthetic = (
            ParsedMail(
                subject="long",
                from_="x@y",
                to="",
                cc="",
                date="",
                message_id="",
                body=long_body,
                has_attachments=False,
                content_type="text/plain",
                byte_size=len(long_body),
            ),
        )
        hits = search_mail(synthetic, "find me here")
        self.assertEqual(len(hits), 1)
        self.assertLess(len(hits[0].snippet), len(long_body))

    def test_snippet_does_not_carry_full_body(self) -> None:
        marker = "DEEP_BODY_MARKER_QQQ"
        body = ("a " * 500) + marker + (" b " * 500) + " keyword"
        from wolf.mail.read_local import ParsedMail

        synthetic = (
            ParsedMail(
                subject="s",
                from_="f",
                to="",
                cc="",
                date="",
                message_id="",
                body=body,
                has_attachments=False,
                content_type="text/plain",
                byte_size=len(body),
            ),
        )
        hits = search_mail(synthetic, "keyword")
        # The snippet is anchored at "keyword", not at the marker; the
        # marker is far away and must not appear.
        self.assertNotIn(marker, hits[0].snippet)


class AttachmentInHitTest(unittest.TestCase):
    def test_hit_carries_attachment_meta(self) -> None:
        from wolf.mail.read_local import read_eml

        pm = read_eml(FIXTURES / "attachment_meta.eml")
        hits = search_mail((pm,), "spec")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].has_attachments)
        self.assertEqual(hits[0].attachments_count, 1)

    def test_hit_without_attachment_reports_zero(self) -> None:
        from wolf.mail.read_local import read_eml

        pm = read_eml(FIXTURES / "sample.eml")
        hits = search_mail((pm,), "meeting")
        self.assertEqual(len(hits), 1)
        self.assertFalse(hits[0].has_attachments)
        self.assertEqual(hits[0].attachments_count, 0)


if __name__ == "__main__":
    unittest.main()
