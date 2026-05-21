"""Tests for .github/workflows/ files.

The CI workflow files are scanned with stdlib-only string / regex checks
(no PyYAML dependency) to keep the test harness consistent with the rest of
the suite. The checks confirm structure, triggers, permissions, attribution
hygiene, and absence of outbound network calls.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
NO_ATTR_WORKFLOW = WORKFLOWS_DIR / "no-attribution.yml"
DOCS_PR_WORKFLOW = REPO_ROOT / "docs" / "dev" / "pr_workflow.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class WorkflowPresenceTest(unittest.TestCase):
    def test_workflows_dir_exists(self) -> None:
        self.assertTrue(WORKFLOWS_DIR.is_dir())

    def test_no_attribution_workflow_exists(self) -> None:
        self.assertTrue(NO_ATTR_WORKFLOW.is_file())

    def test_workflow_is_nonempty(self) -> None:
        content = _read(NO_ATTR_WORKFLOW)
        self.assertGreater(len(content.strip()), 0)


class TriggersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _read(NO_ATTR_WORKFLOW)

    def test_has_pull_request_trigger(self) -> None:
        self.assertRegex(self.content, r"(?m)^\s*pull_request\s*:")

    def test_has_push_trigger(self) -> None:
        self.assertRegex(self.content, r"(?m)^\s*push\s*:")

    def test_uses_on_block(self) -> None:
        # Confirm the workflow has a top-level "on:" block (not e.g. on a job).
        self.assertRegex(self.content, r"(?m)^on\s*:")


class PermissionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _read(NO_ATTR_WORKFLOW)

    def test_declares_permissions_block(self) -> None:
        self.assertRegex(self.content, r"(?m)^permissions\s*:")

    def test_contents_is_read_only(self) -> None:
        self.assertRegex(self.content, r"contents\s*:\s*read\b")
        # Forbid write escalation.
        self.assertNotRegex(self.content, r"contents\s*:\s*write\b")

    def test_pull_requests_is_read_only(self) -> None:
        self.assertRegex(self.content, r"pull-requests\s*:\s*read\b")
        self.assertNotRegex(self.content, r"pull-requests\s*:\s*write\b")

    def test_no_write_all_or_administrative_perms(self) -> None:
        # Banned shortcuts and high-privilege scopes.
        forbidden = (
            r"permissions\s*:\s*write-all\b",
            r"\bid-token\s*:\s*write\b",
            r"\bactions\s*:\s*write\b",
            r"\bdeployments\s*:\s*write\b",
            r"\bpackages\s*:\s*write\b",
            r"\bsecurity-events\s*:\s*write\b",
        )
        for pat in forbidden:
            self.assertNotRegex(
                self.content,
                pat,
                f"workflow must not declare {pat!r}",
            )


class StepsAndGuardInvocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _read(NO_ATTR_WORKFLOW)

    def test_uses_actions_checkout(self) -> None:
        self.assertRegex(self.content, r"uses:\s*actions/checkout@")

    def test_runs_guard_commit_head(self) -> None:
        self.assertIn("--commit HEAD", self.content)

    def test_runs_guard_identity(self) -> None:
        self.assertIn("--identity", self.content)

    def test_runs_guard_file_against_pr_body(self) -> None:
        self.assertIn("--file", self.content)
        # The file must come from the PR body extraction step.
        self.assertIn("wolf-ci-pr-body", self.content)

    def test_pr_body_extraction_uses_python_stdlib(self) -> None:
        # No jq, no external CLI.
        self.assertNotRegex(self.content, r"\bjq\b")
        self.assertIn("python3", self.content)

    def test_pr_body_step_conditional_on_pull_request_event(self) -> None:
        # The PR body extraction and scan must only run for pull_request events
        # because GITHUB_EVENT_PATH on a push event does not have a body.
        self.assertRegex(
            self.content,
            r"if:\s*github\.event_name\s*==\s*'pull_request'",
        )


class NetworkAndSecretsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _read(NO_ATTR_WORKFLOW)

    def test_no_curl(self) -> None:
        self.assertNotRegex(self.content, r"\bcurl\b")

    def test_no_wget(self) -> None:
        self.assertNotRegex(self.content, r"\bwget\b")

    def test_no_secrets_referenced(self) -> None:
        # ${{ secrets.* }} should not be needed for this workflow.
        self.assertNotRegex(self.content, r"\$\{\{\s*secrets\.")

    def test_does_not_call_github_api(self) -> None:
        # api.github.com and gh CLI should not appear. We only read the
        # event payload.
        self.assertNotIn("api.github.com", self.content)
        self.assertNotRegex(self.content, r"(?m)^\s*-\s*run:\s*gh\b")


class AttributionHygieneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _read(NO_ATTR_WORKFLOW)

    def test_no_coauthored_by_claude(self) -> None:
        # Use fragment-build to keep this test file itself clean.
        needle = "Co-" + "Authored-By: " + "Claude"
        self.assertNotIn(needle, self.content)

    def test_no_generated_with_claude(self) -> None:
        needle = "Generated " + "with " + "Claude"
        self.assertNotIn(needle, self.content)

    def test_no_robot_emoji_generated(self) -> None:
        needle = "\U0001F916" + " Generated"
        self.assertNotIn(needle, self.content)


class DocsCrossReferenceTest(unittest.TestCase):
    def test_pr_workflow_doc_mentions_ci_guard(self) -> None:
        self.assertTrue(DOCS_PR_WORKFLOW.is_file())
        body = _read(DOCS_PR_WORKFLOW)
        # Doc must describe the CI guard layer somewhere.
        # Use a flexible match: it mentions GitHub Actions / workflows / CI.
        lowered = body.lower()
        markers_seen = sum(
            1
            for m in (
                "github actions",
                "no-attribution.yml",
                "ci guard",
                "ci-level",
                "workflow",
            )
            if m in lowered
        )
        self.assertGreaterEqual(
            markers_seen,
            2,
            f"pr_workflow.md must describe the CI guard layer "
            f"(found {markers_seen} relevant markers)",
        )


if __name__ == "__main__":
    unittest.main()
