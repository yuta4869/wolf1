"""Tests for src/wolf/mail/draft.py."""

from __future__ import annotations

import unittest
from pathlib import Path

from wolf.mail.draft import (
    DraftPromptParts,
    build_draft_prompt,
    default_subject_for_reply,
)
from wolf.mail.read_local import read_mail_any


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "mail"


class SubjectForReplyTest(unittest.TestCase):
    def test_prepends_re(self) -> None:
        self.assertEqual(
            default_subject_for_reply("Quarterly planning meeting"),
            "Re: Quarterly planning meeting",
        )

    def test_no_double_re(self) -> None:
        self.assertEqual(
            default_subject_for_reply("Re: Quarterly planning meeting"),
            "Re: Quarterly planning meeting",
        )

    def test_empty_subject_fallback(self) -> None:
        self.assertEqual(
            default_subject_for_reply(""),
            "Re: (no subject)",
        )

    def test_handles_fw(self) -> None:
        self.assertEqual(
            default_subject_for_reply("Fwd: spec"), "Fwd: spec"
        )


class BuildDraftPromptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pm = read_mail_any(FIXTURES / "sample.eml").messages[0]

    def test_returns_parts(self) -> None:
        parts = build_draft_prompt(self.pm, "丁寧に返信して")
        self.assertIsInstance(parts, DraftPromptParts)
        self.assertEqual(parts.instruction, "丁寧に返信して")
        self.assertEqual(parts.mail_body, self.pm.body)

    def test_composed_has_clear_boundary(self) -> None:
        parts = build_draft_prompt(self.pm, "do X")
        self.assertIn("<INSTRUCTION>", parts.composed)
        self.assertIn("</INSTRUCTION>", parts.composed)
        self.assertIn("<EMAIL_METADATA>", parts.composed)
        self.assertIn("</EMAIL_METADATA>", parts.composed)

    def test_composed_separates_instruction_from_body(self) -> None:
        parts = build_draft_prompt(self.pm, "do X")
        # The instruction lives BEFORE the email body section starts.
        i_pos = parts.composed.find("<INSTRUCTION>")
        e_pos = parts.composed.find("<EMAIL_METADATA>")
        self.assertGreater(e_pos, i_pos)

    def test_empty_instruction_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_draft_prompt(self.pm, "")
        with self.assertRaises(ValueError):
            build_draft_prompt(self.pm, "   ")

    def test_composed_does_not_inline_body(self) -> None:
        # build_draft_prompt returns parts; mail_body is separate from
        # composed. The caller is expected to concatenate them via the
        # Router. Verify composed itself does NOT contain the body
        # (only the boundary scaffolding + headers).
        parts = build_draft_prompt(self.pm, "do X")
        self.assertNotIn(self.pm.body.strip().split("\n")[2], parts.composed)


if __name__ == "__main__":
    unittest.main()
