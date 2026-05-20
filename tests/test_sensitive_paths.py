from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from wolf.safety.sensitive_paths import (
    DEFAULT_RULES,
    PathDecision,
    SensitivePathGuard,
)


SETTINGS_PATH = (
    Path(__file__).resolve().parents[1] / ".claude" / "settings.json"
)


def _make_guard(
    *, project_root: Path, home: Path = None, case_sensitive: bool = False
) -> SensitivePathGuard:
    return SensitivePathGuard(
        project_root=project_root,
        home=home if home is not None else Path.home(),
        case_sensitive=case_sensitive,
    )


class BasicDenialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = Path(self.home_tmp.name).resolve()
        self.guard = _make_guard(project_root=self.root, home=self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.home_tmp.cleanup()

    def test_env_at_project_root_denied(self) -> None:
        d = self.guard.check(self.root / ".env")
        self.assertFalse(d.allowed)
        self.assertEqual(d.matched_rule, "./.env")

    def test_env_dot_variant_denied(self) -> None:
        d = self.guard.check(self.root / ".env.local")
        self.assertFalse(d.allowed)
        self.assertEqual(d.matched_rule, "./.env.*")

    def test_env_production_variant_denied(self) -> None:
        d = self.guard.check(self.root / ".env.production")
        self.assertFalse(d.allowed)
        self.assertEqual(d.matched_rule, "./.env.*")

    def test_secrets_subtree_denied(self) -> None:
        d = self.guard.check(self.root / "secrets" / "key.pem")
        self.assertFalse(d.allowed)
        self.assertEqual(d.matched_rule, "./secrets")

    def test_secrets_dir_itself_denied(self) -> None:
        d = self.guard.check(self.root / "secrets")
        self.assertFalse(d.allowed)
        self.assertEqual(d.matched_rule, "./secrets")

    def test_secrets_deeply_nested_denied(self) -> None:
        d = self.guard.check(self.root / "secrets" / "a" / "b" / "c.pem")
        self.assertFalse(d.allowed)

    def test_credentials_subtree_denied(self) -> None:
        d = self.guard.check(self.root / "credentials" / "oauth.json")
        self.assertFalse(d.allowed)

    def test_tokens_subtree_denied(self) -> None:
        d = self.guard.check(self.root / "tokens" / "access.txt")
        self.assertFalse(d.allowed)

    def test_private_subtree_denied(self) -> None:
        d = self.guard.check(self.root / "private" / "notes.md")
        self.assertFalse(d.allowed)

    def test_home_ssh_denied(self) -> None:
        d = self.guard.check(self.home / ".ssh" / "id_rsa")
        self.assertFalse(d.allowed)
        self.assertEqual(d.matched_rule, "~/.ssh")

    def test_home_aws_denied(self) -> None:
        d = self.guard.check(self.home / ".aws" / "credentials")
        self.assertFalse(d.allowed)
        self.assertEqual(d.matched_rule, "~/.aws")

    def test_home_gcloud_denied(self) -> None:
        d = self.guard.check(self.home / ".config" / "gcloud" / "active_config")
        self.assertFalse(d.allowed)
        self.assertEqual(d.matched_rule, "~/.config/gcloud")

    def test_home_config_other_app_allowed(self) -> None:
        d = self.guard.check(self.home / ".config" / "other_app" / "x")
        self.assertTrue(d.allowed)

    def test_allowed_path_in_project(self) -> None:
        d = self.guard.check(self.root / "src" / "wolf" / "__init__.py")
        self.assertTrue(d.allowed)
        self.assertIsNone(d.matched_rule)

    def test_root_rule_does_not_match_nested_env(self) -> None:
        d = self.guard.check(self.root / "src" / ".env")
        self.assertTrue(d.allowed)

    def test_envrc_not_denied_by_env_rules(self) -> None:
        d = self.guard.check(self.root / ".envrc")
        self.assertTrue(d.allowed)


class NormalizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.guard = _make_guard(project_root=self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_relative_path_resolves_against_project_root(self) -> None:
        d = self.guard.check(".env")
        self.assertFalse(d.allowed)
        self.assertEqual(d.matched_rule, "./.env")

    def test_relative_subtree_path_denied(self) -> None:
        d = self.guard.check("secrets/key.pem")
        self.assertFalse(d.allowed)

    def test_dotdot_escape_into_secrets_denied(self) -> None:
        d = self.guard.check("src/../secrets/key.pem")
        self.assertFalse(d.allowed)
        self.assertEqual(d.matched_rule, "./secrets")

    def test_dotdot_out_of_secrets_into_safe_dir_allowed(self) -> None:
        d = self.guard.check("secrets/../src/main.py")
        self.assertTrue(d.allowed)

    def test_redundant_dot_segments_collapsed(self) -> None:
        d = self.guard.check("./secrets/./key.pem")
        self.assertFalse(d.allowed)

    def test_tilde_expansion_uses_configured_home(self) -> None:
        home_tmp = tempfile.TemporaryDirectory()
        try:
            home = Path(home_tmp.name).resolve()
            guard = _make_guard(project_root=self.root, home=home)
            d = guard.check("~/.ssh/id_rsa")
            self.assertFalse(d.allowed)
            self.assertEqual(d.matched_rule, "~/.ssh")
        finally:
            home_tmp.cleanup()

    def test_empty_string_fails_closed(self) -> None:
        d = self.guard.check("")
        self.assertFalse(d.allowed)
        self.assertIn("empty", d.reason.lower())
        self.assertIsNone(d.normalized_path)

    def test_whitespace_only_fails_closed(self) -> None:
        d = self.guard.check("   ")
        self.assertFalse(d.allowed)

    def test_none_fails_closed(self) -> None:
        d = self.guard.check(None)
        self.assertFalse(d.allowed)
        self.assertIsNone(d.normalized_path)

    def test_unrelated_system_path_not_denied(self) -> None:
        d = self.guard.check("/etc/passwd")
        self.assertTrue(d.allowed)


class CaseSensitivityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_case_insensitive_denies_uppercase_env(self) -> None:
        guard = _make_guard(project_root=self.root)
        d = guard.check(self.root / ".ENV")
        self.assertFalse(d.allowed)

    def test_default_case_insensitive_denies_uppercase_subtree(self) -> None:
        guard = _make_guard(project_root=self.root)
        d = guard.check(self.root / "SECRETS" / "x")
        self.assertFalse(d.allowed)

    def test_default_case_insensitive_denies_mixed_case_env_variant(
        self,
    ) -> None:
        guard = _make_guard(project_root=self.root)
        d = guard.check(self.root / ".Env.Local")
        self.assertFalse(d.allowed)

    def test_case_sensitive_mode_allows_uppercase_env(self) -> None:
        guard = _make_guard(project_root=self.root, case_sensitive=True)
        d = guard.check(self.root / ".ENV")
        self.assertTrue(d.allowed)

    def test_case_sensitive_mode_still_denies_exact_lowercase(self) -> None:
        guard = _make_guard(project_root=self.root, case_sensitive=True)
        d = guard.check(self.root / ".env")
        self.assertFalse(d.allowed)


class SymlinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "key.pem").write_text("k", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "main.py").write_text("# ok", encoding="utf-8")
        self.guard = _make_guard(project_root=self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_symlink_to_secrets_dir_denied_via_realpath(self) -> None:
        link = self.root / "innocent_link"
        os.symlink(self.root / "secrets", link)
        d = self.guard.check(link / "key.pem")
        self.assertFalse(d.allowed)
        self.assertIn("symlink", d.reason)
        self.assertEqual(d.matched_rule, "./secrets")

    def test_symlink_to_safe_file_allowed(self) -> None:
        link = self.root / "safe_link"
        os.symlink(self.root / "src" / "main.py", link)
        d = self.guard.check(link)
        self.assertTrue(d.allowed)

    def test_direct_symlink_to_env_file_denied(self) -> None:
        (self.root / ".env").write_text("X=1", encoding="utf-8")
        link = self.root / "innocent_env_link"
        os.symlink(self.root / ".env", link)
        d = self.guard.check(link)
        self.assertFalse(d.allowed)


EXPECTED_READ_DENIES = {
    "Read(./.env)": ".env",
    "Read(./.env.*)": ".env.local",
    "Read(./secrets/**)": "secrets/key.pem",
    "Read(./credentials/**)": "credentials/oauth.json",
    "Read(./tokens/**)": "tokens/access.txt",
    "Read(./private/**)": "private/notes.md",
    "Read(~/.ssh/**)": "~/.ssh/id_rsa",
    "Read(~/.aws/**)": "~/.aws/credentials",
    "Read(~/.config/gcloud/**)": "~/.config/gcloud/active_config",
}


class SettingsAlignmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        self.deny = self.settings["permissions"]["deny"]
        self.read_denies = [e for e in self.deny if e.startswith("Read(")]

    def test_no_unknown_read_denies_in_settings(self) -> None:
        for entry in self.read_denies:
            self.assertIn(
                entry,
                EXPECTED_READ_DENIES,
                f"settings.json has unrecognized Read deny rule {entry!r} - "
                f"update SensitivePathGuard or this test mapping",
            )

    def test_all_expected_rules_present_in_settings(self) -> None:
        for entry in EXPECTED_READ_DENIES:
            self.assertIn(
                entry,
                self.read_denies,
                f"settings.json is missing expected Read deny rule {entry!r}",
            )

    def test_each_settings_pattern_is_blocked_by_guard(self) -> None:
        project_root = SETTINGS_PATH.resolve().parents[1]
        guard = SensitivePathGuard(
            project_root=project_root, home=Path.home()
        )
        for entry, example in EXPECTED_READ_DENIES.items():
            with self.subTest(entry=entry, example=example):
                decision = guard.check(example)
                self.assertFalse(
                    decision.allowed,
                    f"settings rule {entry!r} -> {example!r} "
                    f"should be denied but guard allowed it",
                )

    def test_default_rules_cover_every_expected_settings_rule(self) -> None:
        descriptions = {r.describe() for r in DEFAULT_RULES}
        for entry in EXPECTED_READ_DENIES:
            stem = entry[len("Read(") : -1].replace("/**", "").rstrip("/")
            self.assertIn(
                stem,
                descriptions,
                f"DEFAULT_RULES missing a rule corresponding to {entry!r} "
                f"(expected {stem!r})",
            )


if __name__ == "__main__":
    unittest.main()
