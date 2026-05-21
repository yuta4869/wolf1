"""Tests for the opt-in git hook installer and the hooks themselves.

The tests build isolated fixture repositories and copy the wolf source tree
(scripts/, the attribution guard, and the hook templates) into them. They
then exercise install-git-hooks.sh and confirm that the installed hooks
block bad commits and allow good ones.

Forbidden marker fixtures are built at runtime from string fragments so
this file stays clean to other scanners. The file is also on the
attribution guard's allowlist.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
INSTALL_SCRIPT = SCRIPTS_DIR / "install-git-hooks.sh"
GUARD_SCRIPT = SCRIPTS_DIR / "check-no-ai-attribution.sh"
HOOKS_SRC_DIR = SCRIPTS_DIR / "git-hooks"
PRE_COMMIT_HOOK = HOOKS_SRC_DIR / "pre-commit"
COMMIT_MSG_HOOK = HOOKS_SRC_DIR / "commit-msg"


def _coauth() -> str:
    return "Co-" + "Authored-By: " + "Claude"


def _is_executable(p: Path) -> bool:
    return p.exists() and os.access(p, os.X_OK)


def _run(cmd, *, cwd=None, env=None, input_text=None, timeout=20):
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=timeout,
    )


class ScriptPresenceTest(unittest.TestCase):
    def test_install_script_is_executable(self) -> None:
        self.assertTrue(_is_executable(INSTALL_SCRIPT))

    def test_pre_commit_hook_is_executable(self) -> None:
        self.assertTrue(_is_executable(PRE_COMMIT_HOOK))

    def test_commit_msg_hook_is_executable(self) -> None:
        self.assertTrue(_is_executable(COMMIT_MSG_HOOK))

    def test_pre_commit_invokes_guard_staged(self) -> None:
        body = PRE_COMMIT_HOOK.read_text(encoding="utf-8")
        self.assertIn("--staged", body)
        self.assertIn("check-no-ai-attribution.sh", body)

    def test_pre_commit_invokes_guard_identity(self) -> None:
        body = PRE_COMMIT_HOOK.read_text(encoding="utf-8")
        self.assertIn("--identity", body)

    def test_commit_msg_invokes_guard_with_message_file(self) -> None:
        body = COMMIT_MSG_HOOK.read_text(encoding="utf-8")
        # The hook reads $1 (commit message file) and passes contents to guard.
        self.assertIn("$1", body)
        self.assertIn("check-no-ai-attribution.sh", body)
        # The hook uses --text with the message contents (not --file, since
        # the commit message tempfile may not match the allowlist semantics).
        self.assertIn("--text", body)

    def test_commit_msg_invokes_guard_identity(self) -> None:
        body = COMMIT_MSG_HOOK.read_text(encoding="utf-8")
        self.assertIn("--identity", body)


class InstallScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "fixture-repo"
        self.repo.mkdir()
        # Populate the fixture with the real wolf scripts + guard so the
        # hooks' relative lookups (git rev-parse --show-toplevel) resolve
        # the same as in the real repo.
        (self.repo / "scripts").mkdir()
        shutil.copy(INSTALL_SCRIPT, self.repo / "scripts" / "install-git-hooks.sh")
        shutil.copy(GUARD_SCRIPT, self.repo / "scripts" / "check-no-ai-attribution.sh")
        (self.repo / "scripts" / "git-hooks").mkdir()
        shutil.copy(PRE_COMMIT_HOOK, self.repo / "scripts" / "git-hooks" / "pre-commit")
        shutil.copy(COMMIT_MSG_HOOK, self.repo / "scripts" / "git-hooks" / "commit-msg")
        for p in (
            self.repo / "scripts" / "install-git-hooks.sh",
            self.repo / "scripts" / "check-no-ai-attribution.sh",
            self.repo / "scripts" / "git-hooks" / "pre-commit",
            self.repo / "scripts" / "git-hooks" / "commit-msg",
        ):
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _run(["git", "init", "-q", "-b", "main"], cwd=self.repo)
        _run(["git", "config", "user.name", "fixture-user"], cwd=self.repo)
        _run(["git", "config", "user.email", "fixture@example.com"], cwd=self.repo)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _hooks_dir(self) -> Path:
        return self.repo / ".git" / "hooks"

    def test_help_exits_zero(self) -> None:
        r = _run(["./scripts/install-git-hooks.sh", "--help"], cwd=self.repo)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("usage", r.stderr.lower())

    def test_unknown_flag_is_error(self) -> None:
        r = _run(["./scripts/install-git-hooks.sh", "--bogus"], cwd=self.repo)
        self.assertEqual(r.returncode, 1)

    def test_not_in_git_repo_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            # Copy script + dependencies so the path is resolvable.
            scripts_dir = Path(outside) / "scripts"
            scripts_dir.mkdir()
            shutil.copy(INSTALL_SCRIPT, scripts_dir / "install-git-hooks.sh")
            (scripts_dir / "install-git-hooks.sh").chmod(0o755)
            r = _run(
                [str(scripts_dir / "install-git-hooks.sh")],
                cwd=outside,
            )
            self.assertEqual(r.returncode, 1)
            self.assertIn("not inside a git repository", r.stderr)

    def test_install_creates_hooks(self) -> None:
        r = _run(["./scripts/install-git-hooks.sh"], cwd=self.repo)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        for h in ("pre-commit", "commit-msg"):
            installed = self._hooks_dir() / h
            self.assertTrue(installed.exists(), f"missing {h}")
            self.assertTrue(_is_executable(installed))

    def test_install_dry_run_makes_no_changes(self) -> None:
        before = sorted(p.name for p in self._hooks_dir().iterdir())
        r = _run(["./scripts/install-git-hooks.sh", "--dry-run"], cwd=self.repo)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("[dry-run]", r.stdout)
        after = sorted(p.name for p in self._hooks_dir().iterdir())
        # git init creates sample hooks; the set should be unchanged after dry-run.
        # Our installed hook names (pre-commit, commit-msg) should NOT appear
        # as new entries (sample hooks have .sample suffix).
        new = set(after) - set(before)
        self.assertNotIn("pre-commit", new)
        self.assertNotIn("commit-msg", new)

    def test_existing_hook_blocks_without_force(self) -> None:
        existing = self._hooks_dir() / "pre-commit"
        existing.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")
        existing.chmod(0o755)
        r = _run(["./scripts/install-git-hooks.sh"], cwd=self.repo)
        self.assertEqual(r.returncode, 2, msg=r.stderr)
        self.assertIn("--force", r.stderr)
        # The existing hook is untouched.
        self.assertIn("existing", existing.read_text(encoding="utf-8"))

    def test_force_overwrites_existing_and_creates_backup(self) -> None:
        existing = self._hooks_dir() / "pre-commit"
        existing.write_text("#!/bin/sh\necho OLD_HOOK\n", encoding="utf-8")
        existing.chmod(0o755)
        r = _run(
            ["./scripts/install-git-hooks.sh", "--force"], cwd=self.repo
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        new_body = existing.read_text(encoding="utf-8")
        self.assertNotIn("OLD_HOOK", new_body)
        self.assertIn("check-no-ai-attribution.sh", new_body)
        backup = self._hooks_dir() / "pre-commit.bak"
        self.assertTrue(backup.exists())
        self.assertIn("OLD_HOOK", backup.read_text(encoding="utf-8"))

    def test_reinstall_of_identical_hook_is_idempotent(self) -> None:
        # First install.
        r1 = _run(["./scripts/install-git-hooks.sh"], cwd=self.repo)
        self.assertEqual(r1.returncode, 0, msg=r1.stderr)
        # Second install without --force should succeed because the on-disk
        # hook is byte-identical to ours (checksum match).
        r2 = _run(["./scripts/install-git-hooks.sh"], cwd=self.repo)
        self.assertEqual(r2.returncode, 0, msg=r2.stderr)

    def test_force_dry_run_reports_backup_plan(self) -> None:
        existing = self._hooks_dir() / "pre-commit"
        existing.write_text("#!/bin/sh\necho OLD\n", encoding="utf-8")
        existing.chmod(0o755)
        r = _run(
            ["./scripts/install-git-hooks.sh", "--dry-run", "--force"],
            cwd=self.repo,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("would back up", r.stdout)
        self.assertIn("[dry-run]", r.stdout)
        # No actual backup created in dry-run.
        self.assertFalse((self._hooks_dir() / "pre-commit.bak").exists())
        # Original untouched.
        self.assertIn("OLD", existing.read_text(encoding="utf-8"))


class HookBehaviorTest(unittest.TestCase):
    """End-to-end: install hooks, then attempt good and bad commits."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "fixture-repo"
        self.repo.mkdir()
        self._populate_scripts()
        _run(["git", "init", "-q", "-b", "main"], cwd=self.repo)
        _run(["git", "config", "user.name", "fixture-user"], cwd=self.repo)
        _run(["git", "config", "user.email", "fixture@example.com"], cwd=self.repo)
        # Install the hooks.
        r = _run(["./scripts/install-git-hooks.sh"], cwd=self.repo)
        if r.returncode != 0:
            raise RuntimeError(
                f"install failed: stdout={r.stdout!r} stderr={r.stderr!r}"
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _populate_scripts(self) -> None:
        (self.repo / "scripts").mkdir()
        shutil.copy(INSTALL_SCRIPT, self.repo / "scripts" / "install-git-hooks.sh")
        shutil.copy(GUARD_SCRIPT, self.repo / "scripts" / "check-no-ai-attribution.sh")
        (self.repo / "scripts" / "git-hooks").mkdir()
        shutil.copy(PRE_COMMIT_HOOK, self.repo / "scripts" / "git-hooks" / "pre-commit")
        shutil.copy(COMMIT_MSG_HOOK, self.repo / "scripts" / "git-hooks" / "commit-msg")
        for p in (
            self.repo / "scripts" / "install-git-hooks.sh",
            self.repo / "scripts" / "check-no-ai-attribution.sh",
            self.repo / "scripts" / "git-hooks" / "pre-commit",
            self.repo / "scripts" / "git-hooks" / "commit-msg",
        ):
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _add(self, name: str, content: str) -> None:
        (self.repo / name).write_text(content, encoding="utf-8")
        _run(["git", "add", name], cwd=self.repo)

    def _commit(self, message: str, *, env=None):
        return _run(
            ["git", "commit", "-m", message],
            cwd=self.repo,
            env=env,
        )

    def test_clean_commit_passes_both_hooks(self) -> None:
        self._add("a.txt", "hello\n")
        r = self._commit("feat: add a")
        self.assertEqual(r.returncode, 0, msg=f"{r.stdout}\n{r.stderr}")

    def test_pre_commit_blocks_staged_attribution(self) -> None:
        self._add("bad.txt", "head\n" + _coauth() + "\ntail\n")
        r = self._commit("feat: add bad")
        self.assertNotEqual(r.returncode, 0, msg=r.stdout)
        # Combined output (git prints hook stderr to stderr, plus its own).
        combined = r.stdout + r.stderr
        self.assertIn("FORBIDDEN", combined)

    def test_commit_msg_blocks_attribution_in_message(self) -> None:
        self._add("ok.txt", "fine\n")
        r = self._commit("feat: x\n\n" + _coauth())
        self.assertNotEqual(r.returncode, 0, msg=r.stdout)
        combined = r.stdout + r.stderr
        self.assertIn("FORBIDDEN", combined)

    def test_commit_msg_blocks_robot_emoji_marker(self) -> None:
        self._add("ok.txt", "fine\n")
        r = self._commit("feat: x\n\n\U0001F916 Generated")
        self.assertNotEqual(r.returncode, 0)

    def test_pre_commit_blocks_claude_identity(self) -> None:
        self._add("ok.txt", "fine\n")
        env = {
            "GIT_AUTHOR_NAME": "Claude Helper",
            "GIT_AUTHOR_EMAIL": "human@example.com",
            "GIT_COMMITTER_NAME": "Claude Helper",
            "GIT_COMMITTER_EMAIL": "human@example.com",
        }
        # The identity check in the pre-commit hook reads git config, not
        # GIT_AUTHOR_*. Override via git config locally instead.
        _run(["git", "config", "user.name", "Claude Helper"], cwd=self.repo)
        r = self._commit("feat: x")
        # Restore identity for tearDown safety.
        _run(["git", "config", "user.name", "fixture-user"], cwd=self.repo)
        self.assertNotEqual(r.returncode, 0, msg=r.stdout)

    def test_pre_commit_blocks_bot_email_identity(self) -> None:
        self._add("ok.txt", "fine\n")
        _run(["git", "config", "user.email", "ci@anthropic.com"], cwd=self.repo)
        r = self._commit("feat: x")
        _run(["git", "config", "user.email", "fixture@example.com"], cwd=self.repo)
        self.assertNotEqual(r.returncode, 0, msg=r.stdout)


class NetworkIsolationTest(unittest.TestCase):
    """The hooks must not make outbound network calls during commits."""

    def test_hook_scripts_do_not_reference_curl_or_wget(self) -> None:
        for hook in (PRE_COMMIT_HOOK, COMMIT_MSG_HOOK, INSTALL_SCRIPT):
            body = hook.read_text(encoding="utf-8")
            self.assertNotIn(
                "curl",
                body,
                f"{hook.name} should not invoke curl",
            )
            self.assertNotIn(
                "wget",
                body,
                f"{hook.name} should not invoke wget",
            )
            self.assertNotIn(
                "http://",
                body,
                f"{hook.name} should not reference http://",
            )
            self.assertNotIn(
                "https://",
                body,
                f"{hook.name} should not reference https://",
            )


if __name__ == "__main__":
    unittest.main()
