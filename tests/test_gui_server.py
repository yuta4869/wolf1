"""Tests for src/wolf/gui/server.py.

We spin up a real `http.server.HTTPServer` on an ephemeral port for
each test and drive it with `urllib.request`. This validates the
actual HTTP path including content-type and header behavior.
"""

from __future__ import annotations

import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, Optional, Tuple

from wolf.gui.server import (
    COMMAND_ALLOWLIST,
    DEFAULT_HOST,
    build_server,
    serve_forever,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class _ServerHarness:
    """Bring up a server in a background thread; tear it down on exit."""

    def __init__(self, project_root: Optional[Path] = None) -> None:
        self.project_root = project_root or Path.cwd()
        self.server = build_server(
            host=DEFAULT_HOST,
            port=0,
            project_root=self.project_root,
        )
        self.host, self.port = self.server.server_address[:2]
        self._thread = threading.Thread(
            target=serve_forever, args=(self.server,), daemon=True
        )
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def get(self, path: str) -> Tuple[int, Dict[str, str], bytes]:
        req = urllib.request.Request(self.base + path, method="GET")
        return self._do(req)

    def post_json(self, path: str, payload: Any) -> Tuple[int, Dict[str, str], bytes]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        return self._do(req)

    def post_raw(self, path: str, data: bytes, ctype: str = "application/json") -> Tuple[int, Dict[str, str], bytes]:
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": ctype},
        )
        return self._do(req)

    def _do(self, req: urllib.request.Request) -> Tuple[int, Dict[str, str], bytes]:
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers.items()), resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp is not None else b""
            return exc.code, dict(exc.headers.items() if exc.headers else []), body
        except urllib.error.URLError as exc:
            # The server may close the socket mid-upload when the
            # body exceeds the cap; surface that as a synthetic 400
            # so the test reads "the server refused the upload".
            if isinstance(exc.reason, (BrokenPipeError, ConnectionResetError)):
                return 400, {}, b""
            raise
        except (BrokenPipeError, ConnectionResetError):
            return 400, {}, b""

    def close(self) -> None:
        self.server.shutdown()
        self._thread.join(timeout=3)


class _ServerTestCase(unittest.TestCase):
    """Convenience base that sets up a project root + server."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        # Create a minimal source tree so the boundary guard accepts
        # paths under root.
        (self.root / "src" / "wolf" / "core").mkdir(parents=True)
        (self.root / "src" / "wolf" / "core" / "types.py").write_text(
            "x = 1\n", encoding="utf-8"
        )
        # Copy the Gmail / mail fixtures used by smoke command tests.
        (self.root / "mail").mkdir()
        shutil.copy(
            REPO_ROOT / "tests" / "fixtures" / "mail" / "sample.mbox",
            self.root / "mail" / "sample.mbox",
        )
        self.harness = _ServerHarness(self.root)

    def tearDown(self) -> None:
        self.harness.close()
        self.tmp.cleanup()


class BasicRoutingTest(_ServerTestCase):
    def test_index_is_html(self) -> None:
        status, headers, body = self.harness.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"wolf", body)

    def test_health_ok(self) -> None:
        status, _, body = self.harness.get("/api/health")
        self.assertEqual(status, 200)
        decoded = json.loads(body.decode("utf-8"))
        self.assertTrue(decoded["ok"])
        self.assertEqual(decoded["host"], "127.0.0.1")

    def test_unknown_route_404(self) -> None:
        status, _, _ = self.harness.get("/bogus")
        self.assertEqual(status, 404)

    def test_static_app_js(self) -> None:
        status, headers, body = self.harness.get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertIn(b"wolf local GUI", body)

    def test_static_style_css(self) -> None:
        status, headers, _ = self.harness.get("/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get("Content-Type", ""))


class StaticTraversalTest(_ServerTestCase):
    def test_dotdot_escape_blocked(self) -> None:
        status, _, _ = self.harness.get("/static/../server.py")
        self.assertEqual(status, 404)

    def test_absolute_path_blocked(self) -> None:
        status, _, _ = self.harness.get("/static//etc/hosts")
        self.assertEqual(status, 404)

    def test_unknown_static_file(self) -> None:
        status, _, _ = self.harness.get("/static/does_not_exist.txt")
        self.assertEqual(status, 404)


class SettingsApiTest(_ServerTestCase):
    def test_default_settings_returned(self) -> None:
        status, _, body = self.harness.get("/api/settings")
        self.assertEqual(status, 200)
        decoded = json.loads(body.decode("utf-8"))
        self.assertIn("settings", decoded)
        self.assertEqual(
            decoded["settings"]["default_llm_backend"], "fake"
        )

    def test_post_persists_changes(self) -> None:
        status, _, body = self.harness.post_json(
            "/api/settings",
            {"theme": "dark", "default_ollama_model": "llama3.2:3b"},
        )
        self.assertEqual(status, 200)
        decoded = json.loads(body.decode("utf-8"))
        self.assertEqual(decoded["settings"]["theme"], "dark")
        # Subsequent GET reflects it.
        status, _, body = self.harness.get("/api/settings")
        decoded = json.loads(body.decode("utf-8"))
        self.assertEqual(decoded["settings"]["theme"], "dark")

    def test_post_rejects_access_token(self) -> None:
        status, _, body = self.harness.post_json(
            "/api/settings",
            {"access_token": "ya29.A0xxxxxxxxxxxxxxxx"},
        )
        self.assertEqual(status, 400)
        self.assertIn(b"forbidden", body)

    def test_post_rejects_invalid_json(self) -> None:
        status, _, _ = self.harness.post_raw(
            "/api/settings", b"not json"
        )
        self.assertEqual(status, 400)

    def test_post_rejects_non_object(self) -> None:
        status, _, _ = self.harness.post_raw(
            "/api/settings", b"[1, 2, 3]"
        )
        self.assertEqual(status, 400)


class CommandApiTest(_ServerTestCase):
    def test_allowlist_is_nonempty(self) -> None:
        self.assertGreater(len(COMMAND_ALLOWLIST), 5)

    def test_disallowed_command_400(self) -> None:
        status, _, body = self.harness.post_json(
            "/api/command",
            {"command": "robot-preflight", "args": {}},
        )
        self.assertEqual(status, 400)
        self.assertIn(b"not allowed", body)

    def test_unknown_command_400(self) -> None:
        status, _, _ = self.harness.post_json(
            "/api/command",
            {"command": "rm-rf-slash", "args": {}},
        )
        self.assertEqual(status, 400)

    def test_args_must_be_object(self) -> None:
        status, _, _ = self.harness.post_json(
            "/api/command",
            {"command": "audit-tail", "args": ["nope"]},
        )
        self.assertEqual(status, 400)

    def test_mail_search_allowlisted_runs(self) -> None:
        status, _, body = self.harness.post_json(
            "/api/command",
            {
                "command": "mail-search",
                "args": {
                    "path": "mail/sample.mbox",
                    "query": "meeting",
                },
            },
        )
        self.assertEqual(status, 200)
        decoded = json.loads(body.decode("utf-8"))
        self.assertEqual(decoded["command"], "mail-search")
        # exit_code may be 0 or 2 depending on whether the fixture
        # matched; the important thing is no error and result is JSON.
        self.assertIn("exit_code", decoded)

    def test_gmail_search_fake_runs(self) -> None:
        status, _, body = self.harness.post_json(
            "/api/command",
            {
                "command": "gmail-search",
                "args": {
                    "query": "meeting",
                    "gmail_backend": "fake",
                },
            },
        )
        self.assertEqual(status, 200)
        decoded = json.loads(body.decode("utf-8"))
        self.assertEqual(decoded["command"], "gmail-search")
        self.assertEqual(decoded["exit_code"], 0)
        self.assertIsNotNone(decoded["result"])

    def test_audit_tail_via_command(self) -> None:
        # First produce one event.
        self.harness.post_json(
            "/api/command",
            {"command": "gmail-search", "args": {"query": "meeting"}},
        )
        status, _, body = self.harness.post_json(
            "/api/command",
            {"command": "audit-tail", "args": {"limit": 5}},
        )
        self.assertEqual(status, 200)
        decoded = json.loads(body.decode("utf-8"))
        self.assertEqual(decoded["exit_code"], 0)
        self.assertGreaterEqual(
            decoded["result"]["result"]["event_count"], 1
        )

    def test_audit_get_endpoint(self) -> None:
        self.harness.post_json(
            "/api/command",
            {"command": "gmail-search", "args": {"query": "meeting"}},
        )
        status, _, body = self.harness.get(
            "/api/audit-tail?limit=10&action_kind=gmail.search"
        )
        self.assertEqual(status, 200)
        decoded = json.loads(body.decode("utf-8"))
        self.assertIn("result", decoded)

    def test_command_audit_event_recorded(self) -> None:
        self.harness.post_json(
            "/api/command",
            {"command": "gmail-search", "args": {"query": "meeting"}},
        )
        # Read the audit jsonl directly.
        audit_path = self.root / "var" / "audit" / "audit.jsonl"
        text = audit_path.read_text(encoding="utf-8")
        # gui.command was written.
        self.assertIn('"action_kind":"gui.command"', text)


class HostBindingTest(unittest.TestCase):
    def test_non_loopback_requires_allow_lan(self) -> None:
        with self.assertRaises(ValueError):
            build_server(host="0.0.0.0", port=0, project_root=Path.cwd())

    def test_allow_lan_lets_through(self) -> None:
        # Bind to 127.0.0.1 with allow_lan=True (still safe) just to
        # confirm the flag path doesn't raise.
        srv = build_server(
            host="127.0.0.1", port=0, project_root=Path.cwd(),
            allow_lan=True,
        )
        try:
            self.assertEqual(srv.server_address[0], "127.0.0.1")
        finally:
            srv.server_close()


class SecurityHeadersTest(_ServerTestCase):
    def test_nosniff_present_on_index(self) -> None:
        status, headers, _ = self.harness.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(
            headers.get("X-Content-Type-Options", ""), "nosniff"
        )

    def test_post_body_size_cap(self) -> None:
        # Send a 1 MB body — exceeds the 256 KB cap.
        big = b"x" * (1024 * 1024)
        status, _, _ = self.harness.post_raw(
            "/api/settings", big
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
