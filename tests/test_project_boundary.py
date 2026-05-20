from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from wolf.safety.project_boundary import (
    ProjectBoundaryConfig,
    ProjectBoundaryDecision,
    ProjectBoundaryGuard,
    is_relative_to_path,
)
from wolf.safety.sensitive_paths import SensitivePathGuard


class RelativeToPathHelperTest(unittest.TestCase):
    def test_child_under_parent_is_relative(self) -> None:
        self.assertTrue(
            is_relative_to_path(
                Path("/a/b/c/file.txt"), Path("/a/b/c"), case_sensitive=True
            )
        )

    def test_sibling_is_not_relative(self) -> None:
        self.assertFalse(
            is_relative_to_path(
                Path("/a/b/d/file.txt"), Path("/a/b/c"), case_sensitive=True
            )
        )

    def test_shorter_path_is_not_relative(self) -> None:
        self.assertFalse(
            is_relative_to_path(
                Path("/a/b"), Path("/a/b/c"), case_sensitive=True
            )
        )

    def test_case_sensitive_rejects_case_mismatch(self) -> None:
        self.assertFalse(
            is_relative_to_path(
                Path("/A/B/C/file"), Path("/a/b/c"), case_sensitive=True
            )
        )

    def test_case_insensitive_accepts_case_mismatch(self) -> None:
        self.assertTrue(
            is_relative_to_path(
                Path("/A/B/C/file"), Path("/a/b/c"), case_sensitive=False
            )
        )


class BasicCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "src" / "wolf" / "core").mkdir(parents=True)
        (self.root / "src" / "wolf" / "core" / "types.py").write_text(
            "x = 1\n", encoding="utf-8"
        )
        self.guard = ProjectBoundaryGuard(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_project_root_itself_is_allowed(self) -> None:
        d = self.guard.check(self.root)
        self.assertTrue(d.allowed)
        self.assertFalse(d.escaped)

    def test_file_under_project_root_is_allowed(self) -> None:
        d = self.guard.check(self.root / "src" / "wolf" / "core" / "types.py")
        self.assertTrue(d.allowed)
        self.assertFalse(d.escaped)

    def test_relative_path_resolves_under_project_root(self) -> None:
        d = self.guard.check("src/wolf/core/types.py")
        self.assertTrue(d.allowed)

    def test_absolute_under_project_root_is_allowed(self) -> None:
        d = self.guard.check(str(self.root / "src" / "wolf" / "core"))
        self.assertTrue(d.allowed)

    def test_dotdot_resolving_inside_is_allowed(self) -> None:
        d = self.guard.check("src/../src/wolf/core/types.py")
        self.assertTrue(d.allowed)

    def test_dotdot_escaping_outside_is_denied(self) -> None:
        d = self.guard.check("../sibling/file.txt")
        self.assertFalse(d.allowed)
        self.assertTrue(d.escaped)

    def test_etc_passwd_is_denied(self) -> None:
        d = self.guard.check("/etc/passwd")
        self.assertFalse(d.allowed)
        self.assertTrue(d.escaped)

    def test_home_ssh_is_denied(self) -> None:
        d = self.guard.check("~/.ssh/config")
        self.assertFalse(d.allowed)
        self.assertTrue(d.escaped)

    def test_decision_carries_project_root_string(self) -> None:
        d = self.guard.check("src/wolf/core/types.py")
        self.assertEqual(d.project_root, str(self.root))

    def test_normalized_path_is_normalized_not_raw(self) -> None:
        d = self.guard.check("src/../src/wolf/core/types.py")
        self.assertNotIn("..", d.normalized_path)
        self.assertIn("src/wolf/core/types.py", d.normalized_path)

    def test_reason_mentions_outside_when_denied(self) -> None:
        d = self.guard.check("/etc/passwd")
        self.assertIn("outside", d.reason.lower())

    def test_reason_mentions_inside_when_allowed(self) -> None:
        d = self.guard.check("src/wolf/core/types.py")
        self.assertIn("inside", d.reason.lower())


class InvalidInputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.guard = ProjectBoundaryGuard(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_none_is_denied(self) -> None:
        d = self.guard.check(None)
        self.assertFalse(d.allowed)
        self.assertFalse(d.escaped)
        self.assertIn("none", d.reason.lower())

    def test_empty_string_is_denied(self) -> None:
        d = self.guard.check("")
        self.assertFalse(d.allowed)

    def test_whitespace_only_is_denied(self) -> None:
        d = self.guard.check("   \n\t")
        self.assertFalse(d.allowed)

    def test_windows_drive_path_on_posix_is_denied(self) -> None:
        if os.sep != "/":
            self.skipTest("POSIX-specific defensive rejection")
        d = self.guard.check("C:\\Windows\\System32")
        self.assertFalse(d.allowed)
        self.assertIn("windows", d.reason.lower())


class NonexistentPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.guard = ProjectBoundaryGuard(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_future_file_under_root_is_allowed(self) -> None:
        d = self.guard.check(self.root / "will_be_created" / "later.txt")
        self.assertTrue(d.allowed)

    def test_future_file_escaping_via_dotdot_is_denied(self) -> None:
        d = self.guard.check("../nonexistent/file.txt")
        self.assertFalse(d.allowed)
        self.assertTrue(d.escaped)


class ProjectRootExistenceTest(unittest.TestCase):
    def test_missing_root_raises_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does_not_exist"
            with self.assertRaises(ValueError):
                ProjectBoundaryGuard(missing)

    def test_missing_root_can_be_allowed_via_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does_not_exist"
            guard = ProjectBoundaryGuard(
                missing,
                ProjectBoundaryConfig(require_project_root_exists=False),
            )
            self.assertIsInstance(guard, ProjectBoundaryGuard)

    def test_none_root_raises(self) -> None:
        with self.assertRaises(ValueError):
            ProjectBoundaryGuard(None)  # type: ignore[arg-type]

    def test_empty_root_raises(self) -> None:
        with self.assertRaises(ValueError):
            ProjectBoundaryGuard("")


class SymlinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.outside_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.outside = Path(self.outside_tmp.name).resolve()
        (self.outside / "secret_outside.txt").write_text("x", encoding="utf-8")
        (self.root / "inside_dir").mkdir()
        (self.root / "inside_dir" / "file.txt").write_text(
            "y", encoding="utf-8"
        )
        self.guard = ProjectBoundaryGuard(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.outside_tmp.cleanup()

    def test_symlink_to_outside_is_denied(self) -> None:
        link = self.root / "escape_link"
        os.symlink(self.outside, link)
        d = self.guard.check(link / "secret_outside.txt")
        self.assertFalse(d.allowed)
        self.assertTrue(d.escaped)
        self.assertTrue(d.used_realpath)
        self.assertIn("symlink", d.reason.lower())

    def test_symlink_to_inside_is_allowed(self) -> None:
        link = self.root / "alias_link"
        os.symlink(self.root / "inside_dir", link)
        d = self.guard.check(link / "file.txt")
        self.assertTrue(d.allowed)
        self.assertFalse(d.escaped)
        self.assertTrue(d.used_realpath)

    def test_direct_symlink_to_outside_file_is_denied(self) -> None:
        link = self.root / "outside_file_link"
        os.symlink(self.outside / "secret_outside.txt", link)
        d = self.guard.check(link)
        self.assertFalse(d.allowed)
        self.assertTrue(d.escaped)
        self.assertTrue(d.used_realpath)


class CaseSensitivityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        parent = Path(self.tmp.name).resolve()
        self.fake_root = parent / "wolfproj_for_case_test"
        self.strict = ProjectBoundaryGuard(
            self.fake_root,
            ProjectBoundaryConfig(
                case_sensitive=True,
                require_project_root_exists=False,
            ),
        )
        self.loose = ProjectBoundaryGuard(
            self.fake_root,
            ProjectBoundaryConfig(
                case_sensitive=False,
                require_project_root_exists=False,
            ),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_strict_rejects_case_mismatch(self) -> None:
        upper = str(self.fake_root.parent / "WOLFPROJ_FOR_CASE_TEST" / "x.txt")
        d = self.strict.check(upper)
        self.assertFalse(d.allowed)

    def test_loose_accepts_case_mismatch(self) -> None:
        upper = str(self.fake_root.parent / "WOLFPROJ_FOR_CASE_TEST" / "x.txt")
        d = self.loose.check(upper)
        self.assertTrue(d.allowed)

    def test_strict_accepts_exact_case(self) -> None:
        exact = str(self.fake_root / "x.txt")
        d = self.strict.check(exact)
        self.assertTrue(d.allowed)


class IndependenceFromSensitivePathGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.guard = ProjectBoundaryGuard(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_secrets_under_root_is_allowed_by_boundary(self) -> None:
        # ProjectBoundaryGuard does NOT care that it is "secrets/"
        d = self.guard.check(self.root / "secrets" / "key.pem")
        self.assertTrue(d.allowed)
        self.assertFalse(d.escaped)

    def test_boundary_does_not_import_sensitive_paths(self) -> None:
        # Sanity: module is self-contained (no cross-import surprise)
        import wolf.safety.project_boundary as pb

        src = Path(pb.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sensitive_paths", src)


class OrderedGuardIntegrationTest(unittest.TestCase):
    """Demonstrates ProjectBoundaryGuard -> SensitivePathGuard layering."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = Path(self.home_tmp.name).resolve()
        (self.root / "src" / "wolf").mkdir(parents=True)
        (self.root / "src" / "wolf" / "file.py").write_text(
            "x", encoding="utf-8"
        )
        self.boundary = ProjectBoundaryGuard(self.root)
        self.sensitive = SensitivePathGuard(
            project_root=self.root, home=self.home
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.home_tmp.cleanup()

    def _check_ordered(self, path: str):
        b = self.boundary.check(path)
        if not b.allowed:
            return ("boundary", b, None)
        s = self.sensitive.check(path)
        return ("sensitive", b, s)

    def test_etc_passwd_blocked_at_boundary_layer(self) -> None:
        layer, b, s = self._check_ordered("/etc/passwd")
        self.assertEqual(layer, "boundary")
        self.assertFalse(b.allowed)
        self.assertIsNone(s)

    def test_secrets_in_project_blocked_at_sensitive_layer(self) -> None:
        path = str(self.root / "secrets" / "key.pem")
        layer, b, s = self._check_ordered(path)
        self.assertEqual(layer, "sensitive")
        self.assertTrue(b.allowed)
        self.assertIsNotNone(s)
        self.assertFalse(s.allowed)

    def test_normal_source_passes_both_layers(self) -> None:
        path = str(self.root / "src" / "wolf" / "file.py")
        layer, b, s = self._check_ordered(path)
        self.assertEqual(layer, "sensitive")
        self.assertTrue(b.allowed)
        self.assertIsNotNone(s)
        self.assertTrue(s.allowed)


if __name__ == "__main__":
    unittest.main()
