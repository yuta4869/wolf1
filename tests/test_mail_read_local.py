"""Tests for src/wolf/mail/read_local.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wolf.mail.read_local import (
    MailReadError,
    ParsedMail,
    read_eml,
    read_mbox,
    read_mail_any,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "mail"


class ReadEmlTest(unittest.TestCase):
    def test_plain_eml(self) -> None:
        pm = read_eml(FIXTURES / "sample.eml")
        self.assertIsInstance(pm, ParsedMail)
        self.assertIn("Quarterly planning", pm.subject)
        self.assertIn("Alice", pm.from_)
        self.assertEqual(pm.content_type, "text/plain")
        self.assertFalse(pm.has_attachments)
        self.assertIn("Q3 priorities", pm.body)

    def test_html_only_is_text_extracted(self) -> None:
        pm = read_eml(FIXTURES / "html_only.eml")
        self.assertEqual(pm.content_type, "text/html")
        self.assertIn("Widget weekly", pm.body)
        # HTML tags must be stripped.
        self.assertNotIn("<h1>", pm.body)
        self.assertNotIn("<p>", pm.body)
        self.assertIn("- Item one", pm.body)
        self.assertIn("- Item two", pm.body)

    def test_attachment_meta_only(self) -> None:
        pm = read_eml(FIXTURES / "attachment_meta.eml")
        self.assertTrue(pm.has_attachments)
        self.assertIn("attached the spec", pm.body)
        # Base64 payload must NOT appear in the body.
        self.assertNotIn("iVBORw0KG", pm.body)

    def test_missing_path_raises(self) -> None:
        with self.assertRaises(MailReadError):
            read_eml(FIXTURES / "no_such.eml")

    def test_directory_path_raises(self) -> None:
        with self.assertRaises(MailReadError):
            read_eml(FIXTURES)

    def test_oversize_body_rejected(self) -> None:
        # Create an oversized .eml in a tempdir.
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "big.eml"
            p.write_text(
                "Subject: big\n\n" + ("x" * 2000),
                encoding="utf-8",
            )
            with self.assertRaises(MailReadError) as cm:
                read_eml(p, max_bytes=500)
            self.assertIn("exceeds", cm.exception.label.lower())

    def test_error_does_not_leak_body(self) -> None:
        marker = "SENSITIVE_MAIL_BODY_LEAK_PROBE_42"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "big.eml"
            p.write_text(
                f"Subject: x\n\n{marker}\n" + ("y" * 2000),
                encoding="utf-8",
            )
            try:
                read_eml(p, max_bytes=200)
            except MailReadError as exc:
                rendered = repr(exc) + "|" + str(exc) + "|" + exc.label
                self.assertNotIn(marker, rendered)


class ReadMboxTest(unittest.TestCase):
    def test_reads_all_messages(self) -> None:
        r = read_mbox(FIXTURES / "sample.mbox")
        self.assertEqual(len(r.messages), 3)
        subjects = [m.subject for m in r.messages]
        self.assertIn("Quarterly planning meeting", subjects)

    def test_limit(self) -> None:
        r = read_mbox(FIXTURES / "sample.mbox", limit=1)
        self.assertEqual(len(r.messages), 1)

    def test_filter_subject(self) -> None:
        r = read_mbox(
            FIXTURES / "sample.mbox", filter_subject="Lunch"
        )
        self.assertEqual(len(r.messages), 1)
        self.assertIn("Lunch", r.messages[0].subject)

    def test_filter_from(self) -> None:
        r = read_mbox(
            FIXTURES / "sample.mbox", filter_from="carol"
        )
        self.assertEqual(len(r.messages), 1)

    def test_filter_body(self) -> None:
        r = read_mbox(
            FIXTURES / "sample.mbox", filter_body_contains="Q2 review"
        )
        self.assertEqual(len(r.messages), 1)

    def test_missing_mbox_raises(self) -> None:
        with self.assertRaises(MailReadError):
            read_mbox(FIXTURES / "missing.mbox")


class ReadAnyTest(unittest.TestCase):
    def test_dispatches_by_extension(self) -> None:
        r1 = read_mail_any(FIXTURES / "sample.eml")
        self.assertEqual(len(r1.messages), 1)
        r2 = read_mail_any(FIXTURES / "sample.mbox")
        self.assertEqual(len(r2.messages), 3)

    def test_dispatches_to_maildir(self) -> None:
        r = read_mail_any(FIXTURES / "sample_maildir")
        self.assertEqual(len(r.messages), 3)
        subjects = {m.subject for m in r.messages}
        self.assertIn("Maildir meeting agenda", subjects)

    def test_directory_without_cur_new_tmp_rejected(self) -> None:
        # tests/fixtures/mail itself has files, not a Maildir layout.
        from wolf.mail.read_local import MailReadError
        with self.assertRaises(MailReadError):
            read_mail_any(FIXTURES)


class MaildirTest(unittest.TestCase):
    def test_reads_all_messages(self) -> None:
        r = read_mail_any(FIXTURES / "sample_maildir")
        self.assertEqual(len(r.messages), 3)

    def test_limit_caps_messages(self) -> None:
        r = read_mail_any(FIXTURES / "sample_maildir", limit=1)
        self.assertEqual(len(r.messages), 1)

    def test_filter_from_narrows(self) -> None:
        r = read_mail_any(
            FIXTURES / "sample_maildir",
            filter_from="alice",
        )
        self.assertEqual(len(r.messages), 1)
        self.assertIn("Alice", r.messages[0].from_)

    def test_filter_mismatch_returns_empty(self) -> None:
        r = read_mail_any(
            FIXTURES / "sample_maildir",
            filter_from="nobody@example",
        )
        self.assertEqual(r.messages, ())


class AttachmentsTest(unittest.TestCase):
    def test_attachment_meta_eml_has_attachment_metadata(self) -> None:
        pm = read_mail_any(FIXTURES / "attachment_meta.eml").messages[0]
        self.assertTrue(pm.has_attachments)
        self.assertEqual(len(pm.attachments), 1)
        a = pm.attachments[0]
        self.assertEqual(a.filename, "spec.bin")
        self.assertEqual(a.content_type, "application/octet-stream")
        self.assertGreater(a.size_bytes, 0)

    def test_plain_eml_has_no_attachments(self) -> None:
        pm = read_mail_any(FIXTURES / "sample.eml").messages[0]
        self.assertFalse(pm.has_attachments)
        self.assertEqual(pm.attachments, ())

    def test_attachment_payload_bytes_not_in_body(self) -> None:
        pm = read_mail_any(FIXTURES / "attachment_meta.eml").messages[0]
        # The base64 PNG signature would start "iVBORw0KG" when decoded
        # from the eml fixture; it must not appear in the body.
        self.assertNotIn("iVBORw0KG", pm.body)


class OrFilterTest(unittest.TestCase):
    def test_filter_list_or_combines(self) -> None:
        r = read_mail_any(
            FIXTURES / "sample.mbox",
            filter_from=["alice", "carol"],
        )
        # Alice + Carol = 2 messages (Bob excluded).
        froms = [m.from_ for m in r.messages]
        self.assertEqual(len(froms), 2)
        self.assertTrue(any("Alice" in f for f in froms))
        self.assertTrue(any("Carol" in f for f in froms))

    def test_filter_list_with_empty_strings_is_no_filter(self) -> None:
        r = read_mail_any(
            FIXTURES / "sample.mbox",
            filter_from=["", ""],
        )
        self.assertEqual(len(r.messages), 3)

    def test_mixed_filter_kinds_are_anded(self) -> None:
        # filter_from "alice" OR "carol", AND subject contains "Lunch".
        r = read_mail_any(
            FIXTURES / "sample.mbox",
            filter_from=["alice", "carol"],
            filter_subject="Lunch",
        )
        self.assertEqual(len(r.messages), 1)
        self.assertIn("Lunch", r.messages[0].subject)


if __name__ == "__main__":
    unittest.main()
