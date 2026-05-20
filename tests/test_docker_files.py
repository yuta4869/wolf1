from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


class DockerfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _read("Dockerfile")

    def test_uses_python_311_slim_base(self) -> None:
        self.assertIn("FROM python:3.11-slim", self.content)

    def test_default_cmd_runs_unittest(self) -> None:
        self.assertIn("unittest", self.content)

    def test_does_not_copy_sensitive_paths(self) -> None:
        forbidden = (".env", "secrets", "credentials", "tokens", "private")
        for line in self.content.splitlines():
            stripped = line.strip()
            if not stripped.upper().startswith("COPY"):
                continue
            tokens = stripped.split()
            sources = tokens[1:-1]
            for src in sources:
                for needle in forbidden:
                    self.assertNotIn(
                        needle,
                        src,
                        f"Dockerfile COPY references {needle!r} in {src!r}",
                    )

    def test_sets_python_unbuffered(self) -> None:
        self.assertIn("PYTHONUNBUFFERED=1", self.content)


class DockerIgnoreTest(unittest.TestCase):
    REQUIRED = (
        ".env",
        ".env.*",
        "secrets",
        "credentials",
        "tokens",
        "private",
        "data",
        "models",
        "vector_db",
    )

    def setUp(self) -> None:
        self.lines = {
            line.strip() for line in _read(".dockerignore").splitlines()
        }

    def test_contains_all_required_entries(self) -> None:
        for entry in self.REQUIRED:
            self.assertIn(
                entry,
                self.lines,
                f".dockerignore missing required entry {entry!r}",
            )


class DefaultComposeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _read("docker-compose.yml")

    def test_has_network_mode_none(self) -> None:
        self.assertRegex(self.content, r"network_mode:\s*none")

    def test_no_privileged_true(self) -> None:
        self.assertNotRegex(self.content, r"privileged:\s*true")

    def test_no_network_mode_host(self) -> None:
        self.assertNotRegex(self.content, r"network_mode:\s*host")

    def test_no_pid_host(self) -> None:
        self.assertNotRegex(self.content, r"\bpid:\s*host\b")

    def test_no_docker_socket_mount(self) -> None:
        self.assertNotIn("/var/run/docker.sock", self.content)

    def test_no_dollar_home_mount(self) -> None:
        self.assertNotRegex(self.content, r"\$HOME")

    def test_no_tilde_home_volume(self) -> None:
        self.assertNotRegex(self.content, r"-\s*~/")

    def test_mounts_src_read_only(self) -> None:
        self.assertRegex(self.content, r"\./src:/app/src:ro")

    def test_mounts_tests_read_only(self) -> None:
        self.assertRegex(self.content, r"\./tests:/app/tests:ro")

    def test_mounts_pyproject_read_only(self) -> None:
        self.assertRegex(
            self.content, r"\./pyproject\.toml:/app/pyproject\.toml:ro"
        )

    def test_mounts_docker_artifacts_read_only_for_in_container_tests(
        self,
    ) -> None:
        required = (
            r"\./Dockerfile:/app/Dockerfile:ro",
            r"\./docker-compose\.yml:/app/docker-compose\.yml:ro",
            r"\./docker-compose\.gpu\.yml:/app/docker-compose\.gpu\.yml:ro",
            r"\./docker-compose\.mac\.yml:/app/docker-compose\.mac\.yml:ro",
            r"\./\.dockerignore:/app/\.dockerignore:ro",
            r"\./\.claude/settings\.json:/app/\.claude/settings\.json:ro",
        )
        for pattern in required:
            self.assertRegex(self.content, pattern)

    def test_default_backend_is_cpu(self) -> None:
        self.assertRegex(self.content, r"WOLF_BACKEND:\s*cpu")

    def test_service_name_is_wolf(self) -> None:
        self.assertRegex(self.content, r"(?m)^\s*wolf:")


class GpuComposeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _read("docker-compose.gpu.yml")

    def test_declares_gpu_capability(self) -> None:
        self.assertIn("capabilities: [gpu]", self.content)

    def test_uses_nvidia_driver(self) -> None:
        self.assertRegex(self.content, r"driver:\s*nvidia")

    def test_sets_nvidia_visible_devices(self) -> None:
        self.assertIn("NVIDIA_VISIBLE_DEVICES", self.content)

    def test_sets_nvidia_driver_capabilities(self) -> None:
        self.assertIn("NVIDIA_DRIVER_CAPABILITIES", self.content)

    def test_backend_is_cuda(self) -> None:
        self.assertRegex(self.content, r"WOLF_BACKEND:\s*cuda")

    def test_no_privileged_true(self) -> None:
        self.assertNotRegex(self.content, r"privileged:\s*true")

    def test_no_docker_socket_mount(self) -> None:
        self.assertNotIn("/var/run/docker.sock", self.content)


class MacComposeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _read("docker-compose.mac.yml")

    def test_no_nvidia_visible_devices(self) -> None:
        self.assertNotIn("NVIDIA_VISIBLE_DEVICES", self.content)

    def test_no_nvidia_driver_capabilities(self) -> None:
        self.assertNotIn("NVIDIA_DRIVER_CAPABILITIES", self.content)

    def test_no_gpu_capability(self) -> None:
        self.assertNotIn("[gpu]", self.content)

    def test_no_nvidia_driver(self) -> None:
        self.assertNotRegex(self.content, r"driver:\s*nvidia")

    def test_backend_is_cpu(self) -> None:
        self.assertRegex(self.content, r"WOLF_BACKEND:\s*cpu")


if __name__ == "__main__":
    unittest.main()
