"""Tests for scripts/check-no-ai-attribution.sh.

The script enforces the "No AI attribution policy" in CLAUDE.md. These tests
spawn the script as a subprocess to verify exit codes and detection behavior.

The forbidden marker fixtures in this file are split across literal Python
string concatenation so that the script's own --file mode (which DOES scan
test files unless allowlisted) does not report this file as polluted. The
file is on the allowlist, so it will not be scanned via --file in normal
use, but the split-string convention also keeps the fixtures from
accidentally tripping other static scanners.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check-no-ai-attribution.sh"


# Forbidden phrase fixtures, assembled at runtime so they do not appear as
# single literals if anyone greps this file for the attribution patterns.
def _coauth() -> str:
    return "Co-" + "Authored-By: " + "Claude"


def _generated_with() -> str:
    return "Generated " + "with " + "Claude"


def _robot_generated() -> str:
    # U+1F916 (robot emoji) followed by " Generated"
    return "\U0001F916" + " Generated"


def _run(args, *, env_overrides=None, cwd=None, timeout=15):
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


class ScriptPresenceTest(unittest.TestCase):
    def test_script_exists_and_is_executable(self) -> None:
        self.assertTrue(SCRIPT.exists())
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_help_exits_zero(self) -> None:
        r = _run(["--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage", r.stderr.lower())

    def test_no_args_is_error(self) -> None:
        r = _run([])
        self.assertEqual(r.returncode, 1)

    def test_unknown_mode_is_error(self) -> None:
        r = _run(["--bogus"])
        self.assertEqual(r.returncode, 1)


class TextModeTest(unittest.TestCase):
    def test_clean_text_allowed(self) -> None:
        r = _run(["--text", "This is a normal commit message."])
        self.assertEqual(r.returncode, 0)

    def test_co_authored_by_claude_rejected(self) -> None:
        r = _run(["--text", "feat: x\n\n" + _coauth()])
        self.assertEqual(r.returncode, 2)
        self.assertIn("FORBIDDEN", r.stderr)

    def test_generated_with_claude_rejected(self) -> None:
        r = _run(["--text", _generated_with()])
        self.assertEqual(r.returncode, 2)

    def test_robot_emoji_generated_rejected(self) -> None:
        r = _run(["--text", _robot_generated()])
        self.assertEqual(r.returncode, 2)

    def test_text_mode_does_not_allowlist_anything(self) -> None:
        # --text input is never allowlisted, even if the same string would be
        # OK inside CLAUDE.md.
        r = _run(["--text", _coauth()])
        self.assertEqual(r.returncode, 2)

    def test_empty_text_is_ok(self) -> None:
        r = _run(["--text", ""])
        self.assertEqual(r.returncode, 0)


class FileModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, content: str) -> Path:
        p = self.dir / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_clean_file_allowed(self) -> None:
        p = self._write("clean.txt", "normal content\nno markers here\n")
        r = _run(["--file", str(p)])
        self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_file_with_attribution_rejected(self) -> None:
        p = self._write("bad.txt", "header\n" + _coauth() + "\nfooter\n")
        r = _run(["--file", str(p)])
        self.assertEqual(r.returncode, 2, msg=r.stderr)

    def test_nonexistent_file_is_error(self) -> None:
        r = _run(["--file", str(self.dir / "missing.txt")])
        self.assertEqual(r.returncode, 1)

    def test_empty_file_allowed(self) -> None:
        p = self._write("empty.txt", "")
        r = _run(["--file", str(p)])
        self.assertEqual(r.returncode, 0)

    def test_allowlisted_path_skipped(self) -> None:
        # CLAUDE.md in the repo root must be allowlisted even though it
        # contains the literal forbidden markers (to describe the policy).
        r = _run(["--file", "CLAUDE.md"], cwd=REPO_ROOT)
        self.assertEqual(r.returncode, 0)
        self.assertIn("allowlisted", r.stderr.lower())

    def test_script_itself_is_allowlisted(self) -> None:
        r = _run(
            ["--file", "scripts/check-no-ai-attribution.sh"], cwd=REPO_ROOT
        )
        self.assertEqual(r.returncode, 0)

    def test_test_file_is_allowlisted(self) -> None:
        r = _run(
            ["--file", "tests/test_no_ai_attribution.py"], cwd=REPO_ROOT
        )
        self.assertEqual(r.returncode, 0)


class CommitModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "fixture-repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "fixture-user")
        self._git("config", "user.email", "fixture@example.com")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=True,
        )

    def _commit(self, message: str) -> str:
        (self.repo / "x.txt").write_text("x\n", encoding="utf-8")
        self._git("add", "x.txt")
        # Use --allow-empty-message? No, message has content.
        proc = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git commit failed: {proc.stderr}"
            )
        sha = self._git("rev-parse", "HEAD").stdout.strip()
        return sha

    def _run_in_repo(self, args):
        return _run(args, cwd=self.repo)

    def test_clean_commit_allowed(self) -> None:
        self._commit("feat: add x")
        r = self._run_in_repo(["--commit", "HEAD"])
        self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_commit_with_coauthored_by_claude_rejected(self) -> None:
        self._commit("feat: x\n\n" + _coauth())
        r = self._run_in_repo(["--commit", "HEAD"])
        self.assertEqual(r.returncode, 2, msg=r.stderr)

    def test_commit_with_generated_with_claude_rejected(self) -> None:
        self._commit("feat: x\n\n" + _generated_with())
        r = self._run_in_repo(["--commit", "HEAD"])
        self.assertEqual(r.returncode, 2)

    def test_commit_with_robot_emoji_rejected(self) -> None:
        self._commit("feat: x\n\n" + _robot_generated())
        r = self._run_in_repo(["--commit", "HEAD"])
        self.assertEqual(r.returncode, 2)

    def test_invalid_ref_is_error(self) -> None:
        self._commit("feat: add x")
        r = self._run_in_repo(["--commit", "no_such_ref"])
        self.assertEqual(r.returncode, 1)

    def test_commit_with_claude_author_rejected(self) -> None:
        # Override commit-time identity via env so the fixture's persistent
        # repo config is not modified.
        env = {
            "GIT_AUTHOR_NAME": "Claude Bot",
            "GIT_AUTHOR_EMAIL": "bot@example.com",
            "GIT_COMMITTER_NAME": "Claude Bot",
            "GIT_COMMITTER_EMAIL": "bot@example.com",
        }
        (self.repo / "y.txt").write_text("y\n", encoding="utf-8")
        self._git("add", "y.txt")
        env_full = os.environ.copy()
        env_full.update(env)
        proc = subprocess.run(
            ["git", "commit", "-m", "feat: y"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=env_full,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        r = self._run_in_repo(["--commit", "HEAD"])
        self.assertEqual(r.returncode, 2, msg=r.stderr)

    def test_commit_with_anthropic_committer_rejected(self) -> None:
        env = {
            "GIT_AUTHOR_NAME": "Normal Human",
            "GIT_AUTHOR_EMAIL": "human@example.com",
            "GIT_COMMITTER_NAME": "anthropic-bot",
            "GIT_COMMITTER_EMAIL": "ci@anthropic.com",
        }
        (self.repo / "z.txt").write_text("z\n", encoding="utf-8")
        self._git("add", "z.txt")
        env_full = os.environ.copy()
        env_full.update(env)
        subprocess.run(
            ["git", "commit", "-m", "feat: z"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=env_full,
            check=True,
        )
        r = self._run_in_repo(["--commit", "HEAD"])
        self.assertEqual(r.returncode, 2, msg=r.stderr)


class StagedModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "fixture-repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=str(self.repo), check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "fixture-user"],
            cwd=str(self.repo), check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.com"],
            cwd=str(self.repo), check=True,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _add(self, name: str, content: str) -> None:
        (self.repo / name).write_text(content, encoding="utf-8")
        subprocess.run(
            ["git", "add", name],
            cwd=str(self.repo),
            check=True,
            capture_output=True,
        )

    def test_clean_staged_files_allowed(self) -> None:
        self._add("a.txt", "hello\n")
        self._add("b.txt", "world\n")
        r = _run(["--staged"], cwd=self.repo)
        self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_staged_file_with_attribution_rejected(self) -> None:
        self._add("bad.txt", "head\n" + _coauth() + "\nbody\n")
        r = _run(["--staged"], cwd=self.repo)
        self.assertEqual(r.returncode, 2, msg=r.stderr)

    def test_no_staged_files_is_ok(self) -> None:
        r = _run(["--staged"], cwd=self.repo)
        self.assertEqual(r.returncode, 0)


class IdentityModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "fixture-repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=str(self.repo), check=True,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _set_identity(self, name: str, email: str) -> None:
        subprocess.run(
            ["git", "config", "user.name", name],
            cwd=str(self.repo), check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", email],
            cwd=str(self.repo), check=True,
        )

    def test_clean_identity_allowed(self) -> None:
        self._set_identity("Alice", "alice@example.com")
        r = _run(["--identity"], cwd=self.repo)
        self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_claude_in_name_rejected(self) -> None:
        self._set_identity("Claude Helper", "ok@example.com")
        r = _run(["--identity"], cwd=self.repo)
        self.assertEqual(r.returncode, 2)

    def test_anthropic_in_email_rejected(self) -> None:
        self._set_identity("Alice", "ci@anthropic.com")
        r = _run(["--identity"], cwd=self.repo)
        self.assertEqual(r.returncode, 2)

    def test_unset_identity_is_rejected(self) -> None:
        # Isolate from any global git config that would otherwise satisfy
        # user.name / user.email via fallback.
        r = _run(
            ["--identity"],
            cwd=self.repo,
            env_overrides={
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "HOME": str(self.repo),
            },
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("unset", r.stderr.lower())


class IntegrationWithRealRepoTest(unittest.TestCase):
    """Sanity-check that the real wolf repo passes its own guard.

    Skipped when not running inside a git checkout (e.g. inside the Docker
    test container which mounts source files but not the .git directory).
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not (REPO_ROOT / ".git").exists():
            raise unittest.SkipTest("not running inside a git checkout")

    def test_current_repo_HEAD_is_clean(self) -> None:
        r = _run(["--commit", "HEAD"], cwd=REPO_ROOT)
        self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_current_repo_identity_is_clean(self) -> None:
        r = _run(["--identity"], cwd=REPO_ROOT)
        self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
