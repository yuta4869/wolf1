"""Lightweight presence + content checks for v0.1 docs.

Goals:
- README.md and the three usage docs exist.
- README mentions every CLI subcommand a v0.1 user would reach for.
- README states the v0.1 scope: what works and what doesn't.
- Known limitations doc enumerates the v0.1 gaps that we want users to
  see before they hit them (PDF, OCR, Gmail, robot, auto-refresh,
  embedding-model mismatch).
- No AI attribution markers in the new docs.

The test file itself is on the attribution guard's allowlist
(`tests/test_no_ai_attribution.py` covers the test infrastructure
boundary); fragment-built strings avoid tripping any other scanner.
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# --- presence ---


class DocsPresenceTest(unittest.TestCase):
    REQUIRED = (
        "README.md",
        "docs/usage/quickstart.md",
        "docs/usage/commands.md",
        "docs/usage/known_limitations.md",
    )

    def test_each_doc_exists_and_is_nonempty(self) -> None:
        for rel in self.REQUIRED:
            with self.subTest(doc=rel):
                p = REPO_ROOT / rel
                self.assertTrue(p.is_file(), f"missing {rel}")
                self.assertGreater(
                    len(p.read_text(encoding="utf-8").strip()),
                    0,
                    f"{rel} is empty",
                )


# --- README content ---


class ReadmeContentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.body = _read("README.md")
        self.lower = self.body.lower()

    def test_states_v0_1_scope(self) -> None:
        self.assertIn("v0.1", self.body)
        # Mentions both what works and what doesn't.
        self.assertIn("does not do", self.lower)

    def test_lists_every_user_facing_subcommand(self) -> None:
        for cmd in (
            "summarize-file",
            "summarize-dir",
            "index-files",
            "search-files",
            "search-summarize",
        ):
            with self.subTest(cmd=cmd):
                self.assertIn(cmd, self.body, f"README missing {cmd}")

    def test_mentions_semantic_and_embedding(self) -> None:
        self.assertIn("--semantic", self.body)
        self.assertIn("--embed", self.body)

    def test_documents_install(self) -> None:
        self.assertIn("python3 -m venv", self.body)
        self.assertIn('pip install -e ".[dev]"', self.body)

    def test_documents_test_run(self) -> None:
        self.assertIn("python3 -m unittest", self.body)
        self.assertIn("docker compose run --rm wolf", self.body)

    def test_documents_ollama_setup(self) -> None:
        self.assertIn("ollama.com", self.lower)
        self.assertIn("ollama pull", self.lower)

    def test_documents_privacy_posture(self) -> None:
        for marker in (
            "network_mode: none",
            "ProjectBoundaryGuard",
            "SensitivePathGuard",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.body)

    def test_links_to_usage_docs(self) -> None:
        for rel in (
            "docs/usage/commands.md",
            "docs/usage/quickstart.md",
            "docs/usage/known_limitations.md",
        ):
            with self.subTest(rel=rel):
                self.assertIn(rel, self.body, f"README does not link to {rel}")


# --- quickstart content ---


class QuickstartContentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.body = _read("docs/usage/quickstart.md")

    def test_walks_seven_flows(self) -> None:
        # The doc walks through seven numbered steps.
        for n in range(1, 8):
            with self.subTest(step=n):
                self.assertIn(
                    f"## {n}.",
                    self.body,
                    f"quickstart missing step {n}",
                )

    def test_includes_fake_and_ollama_paths(self) -> None:
        self.assertIn("--backend fake", self.body)
        self.assertIn("--backend ollama", self.body)
        self.assertIn("--embedding-backend fake", self.body)
        self.assertIn("--embedding-backend ollama", self.body)


# --- commands content ---


class CommandsContentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.body = _read("docs/usage/commands.md")

    def test_has_section_per_subcommand(self) -> None:
        for cmd in (
            "summarize-file",
            "summarize-dir",
            "index-files",
            "search-files",
            "search-summarize",
            "robot-preflight",
            "check-path",
        ):
            with self.subTest(cmd=cmd):
                self.assertIn(
                    f"`{cmd}`",
                    self.body,
                    f"commands.md missing section for {cmd}",
                )

    def test_documents_exit_codes(self) -> None:
        self.assertIn("`0`", self.body)
        self.assertIn("`2`", self.body)
        self.assertIn("`1`", self.body)

    def test_documents_json_and_text_output(self) -> None:
        self.assertIn("--output", self.body)
        self.assertIn("json", self.body.lower())
        self.assertIn("text", self.body.lower())


# --- known limitations content ---


class KnownLimitationsContentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.body = _read("docs/usage/known_limitations.md")
        self.lower = self.body.lower()

    def test_covers_pdf_ocr_gap(self) -> None:
        self.assertIn("pdf", self.lower)
        self.assertIn("ocr", self.lower)

    def test_covers_gmail_and_calendar_gap(self) -> None:
        self.assertIn("gmail", self.lower)
        self.assertIn("calendar", self.lower)

    def test_covers_robot_dry_run(self) -> None:
        self.assertIn("robot", self.lower)
        self.assertIn("dry-run", self.lower)

    def test_covers_substring_limits(self) -> None:
        self.assertIn("substring", self.lower)

    def test_covers_embedding_model_mismatch(self) -> None:
        self.assertIn("embedding-model", self.lower)
        self.assertIn("mismatch", self.lower)

    def test_covers_chunked_embedding_gap(self) -> None:
        self.assertIn("chunked embedding", self.lower)

    def test_covers_index_refresh_gap(self) -> None:
        self.assertIn("auto-refresh", self.lower)


# --- attribution hygiene for the new docs ---


class DocsAttributionHygieneTest(unittest.TestCase):
    def _check(self, rel: str) -> None:
        body = _read(rel)
        # Fragment-built needles so this test file does not itself trip
        # the attribution guard.
        coauth = "Co-" + "Authored-By: " + "Claude"
        gen_with = "Generated " + "with " + "Claude"
        gen_by = "Generated " + "by " + "Claude"
        robot_gen = "\U0001F916" + " Generated"
        for needle in (coauth, gen_with, gen_by, robot_gen):
            self.assertNotIn(needle, body, f"{rel} contains forbidden {needle!r}")

    def test_readme(self) -> None:
        self._check("README.md")

    def test_quickstart(self) -> None:
        self._check("docs/usage/quickstart.md")

    def test_commands(self) -> None:
        self._check("docs/usage/commands.md")

    def test_known_limitations(self) -> None:
        self._check("docs/usage/known_limitations.md")


if __name__ == "__main__":
    unittest.main()
