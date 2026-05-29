"""Local-only HTTP server for the wolf GUI.

Built on `http.server.BaseHTTPRequestHandler` (stdlib). Routes:

  GET  /                       → index.html
  GET  /static/<name>          → bounded static (path-traversal safe)
  GET  /api/health             → {"ok": true, ...}
  GET  /api/settings           → current settings (no secrets)
  POST /api/settings           → save (forbidden secrets rejected)
  POST /api/command            → run an allowlisted command, return JSON
  GET  /api/audit-tail         → recent audit events
  *                            → 404

Security posture:

- Default bind is 127.0.0.1. Binding 0.0.0.0 requires an explicit
  `--allow-lan` flag at the CLI layer.
- No authentication, no session, no CSRF token. The server is for
  the user themselves on their own machine. If you opened it up
  with `--allow-lan`, you accepted that posture.
- Static files are restricted to `src/wolf/gui/static/`. Any
  attempt to escape via `..` or symlinks resolves out and we 404.
- `/api/command` only accepts commands on a hard-coded allowlist.
  Each command goes through the existing CLI handlers (no shell,
  no subprocess); arguments are converted to argparse-style
  arrays before re-entering `wolf.cli.main`.
- The whole stdout of the inner CLI call is captured and returned
  as text. If the command emits JSON, the GUI displays it
  formatted client-side; otherwise it shows raw text. No bodies
  or tokens leak out of the audit / CLI layer beyond what those
  layers already permit.
"""

from __future__ import annotations

import io
import json
import os
import socketserver
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from .settings import (
    DEFAULT_SETTINGS,
    SettingsError,
    default_settings_path,
    load_settings,
    save_settings,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATIC_DIRNAME = "static"
STATIC_ROOT = Path(__file__).resolve().parent / STATIC_DIRNAME

# Cap on POST body size to keep a single client from exhausting
# memory by streaming a giant payload.
_MAX_POST_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# Command allowlist
# ---------------------------------------------------------------------------


def _command_to_argv(command: str, args: Mapping[str, Any]) -> Optional[List[str]]:
    """Build the argv we'd hand to `wolf.cli.main` for `command`.

    Returns None if `command` is unknown. Caller checks the
    allowlist via this function only — anything not listed here is
    rejected before we touch CLI logic.
    """

    def _str(v: Any) -> str:
        return "" if v is None else str(v)

    def _bool(v: Any) -> bool:
        return bool(v) if isinstance(v, bool) else False

    def _append_repeating(out: List[str], flag: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                if item:
                    out.extend([flag, _str(item)])
        elif _str(value):
            out.extend([flag, _str(value)])

    if command == "search-files":
        out = ["search-files"]
        out.extend(["--query", _str(args.get("query"))])
        if args.get("path"):
            out.extend(["--path", _str(args.get("path"))])
        if args.get("max_hits"):
            out.extend(["--max-hits", _str(args.get("max_hits"))])
        return out

    if command == "summarize-file":
        out = ["summarize-file", "--path", _str(args.get("path"))]
        if args.get("backend"):
            out.extend(["--backend", _str(args.get("backend"))])
        if args.get("model"):
            out.extend(["--model", _str(args.get("model"))])
        return out

    if command == "search-summarize":
        out = ["search-summarize", "--query", _str(args.get("query"))]
        if args.get("path"):
            out.extend(["--path", _str(args.get("path"))])
        if args.get("backend"):
            out.extend(["--backend", _str(args.get("backend"))])
        if args.get("model"):
            out.extend(["--model", _str(args.get("model"))])
        return out

    if command == "mail-search":
        out = ["mail-search", "--path", _str(args.get("path"))]
        out.extend(["--query", _str(args.get("query"))])
        return out

    if command == "mail-summarize":
        out = ["mail-summarize", "--path", _str(args.get("path"))]
        if args.get("backend"):
            out.extend(["--backend", _str(args.get("backend"))])
        if args.get("model"):
            out.extend(["--model", _str(args.get("model"))])
        return out

    if command == "mail-thread":
        out = ["mail-thread", "--path", _str(args.get("path"))]
        return out

    if command == "mail-search-summarize":
        out = ["mail-search-summarize", "--path", _str(args.get("path"))]
        out.extend(["--query", _str(args.get("query"))])
        if args.get("backend"):
            out.extend(["--backend", _str(args.get("backend"))])
        if args.get("model"):
            out.extend(["--model", _str(args.get("model"))])
        return out

    if command == "gmail-search":
        out = ["gmail-search", "--query", _str(args.get("query"))]
        out.extend(["--gmail-backend", _str(args.get("gmail_backend") or "fake")])
        if args.get("credentials_path"):
            out.extend(["--credentials-path", _str(args.get("credentials_path"))])
        return out

    if command == "gmail-read":
        out = ["gmail-read", "--message-id", _str(args.get("message_id"))]
        out.extend(["--gmail-backend", _str(args.get("gmail_backend") or "fake")])
        if args.get("credentials_path"):
            out.extend(["--credentials-path", _str(args.get("credentials_path"))])
        return out

    if command == "gmail-thread":
        out = ["gmail-thread"]
        out.extend(["--gmail-backend", _str(args.get("gmail_backend") or "fake")])
        if args.get("credentials_path"):
            out.extend(["--credentials-path", _str(args.get("credentials_path"))])
        if args.get("query"):
            out.extend(["--query", _str(args.get("query"))])
        if args.get("message_id"):
            out.extend(["--message-id", _str(args.get("message_id"))])
        if args.get("thread_id"):
            out.extend(["--thread-id", _str(args.get("thread_id"))])
        return out

    if command == "gmail-summarize":
        out = ["gmail-summarize"]
        out.extend(["--gmail-backend", _str(args.get("gmail_backend") or "fake")])
        out.extend(["--llm-backend", _str(args.get("llm_backend") or "fake")])
        if args.get("credentials_path"):
            out.extend(["--credentials-path", _str(args.get("credentials_path"))])
        if args.get("model"):
            out.extend(["--model", _str(args.get("model"))])
        if args.get("query"):
            out.extend(["--query", _str(args.get("query"))])
        if args.get("message_id"):
            out.extend(["--message-id", _str(args.get("message_id"))])
        return out

    if command == "gmail-search-summarize":
        out = ["gmail-search-summarize"]
        out.extend(["--gmail-backend", _str(args.get("gmail_backend") or "fake")])
        out.extend(["--llm-backend", _str(args.get("llm_backend") or "fake")])
        if args.get("credentials_path"):
            out.extend(["--credentials-path", _str(args.get("credentials_path"))])
        if args.get("model"):
            out.extend(["--model", _str(args.get("model"))])
        if args.get("query"):
            out.extend(["--query", _str(args.get("query"))])
        if args.get("thread_id"):
            out.extend(["--thread-id", _str(args.get("thread_id"))])
        if _bool(args.get("threaded")):
            out.append("--threaded")
        return out

    if command == "audit-tail":
        out = ["audit-tail"]
        if args.get("limit"):
            out.extend(["--limit", _str(args.get("limit"))])
        if args.get("action_kind"):
            out.extend(["--action-kind", _str(args.get("action_kind"))])
        return out

    return None


# Exposed for tests.
COMMAND_ALLOWLIST = (
    "search-files",
    "summarize-file",
    "search-summarize",
    "mail-search",
    "mail-summarize",
    "mail-thread",
    "mail-search-summarize",
    "gmail-search",
    "gmail-read",
    "gmail-thread",
    "gmail-summarize",
    "gmail-search-summarize",
    "audit-tail",
)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _audit_gui_command(
    project_root: Path,
    *,
    command: str,
    backend: str,
    result_stage: str,
    outcome: str,
    decision: str,
    extra: Optional[Mapping[str, Any]] = None,
) -> None:
    """Write one gui.command audit event. Metadata only."""
    from ..cli import AuditLogger, _default_audit_path
    from ..core.audit import utc_now_iso
    from ..core.types import AuditEvent

    audit = AuditLogger(_default_audit_path(project_root.resolve()))
    detail: Dict[str, Any] = {
        "command": command,
        "backend": backend,
        "result_stage": result_stage,
    }
    if extra:
        for k, v in extra.items():
            detail[k] = v
    event = AuditEvent(
        ts=utc_now_iso(),
        actor="gui",
        action_kind="gui.command",
        decision=decision,
        target=f"gui:{command}",
        outcome=outcome,
        detail=detail,
    )
    audit.log(event)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class WolfGuiHandler(BaseHTTPRequestHandler):
    """Stdlib HTTPRequestHandler for the GUI.

    Per-request flow goes through `_route_get` / `_route_post`,
    which dispatch to method-shaped handlers. Each handler returns
    `(status, content_type, body_bytes)`; the framework writes the
    response.
    """

    server_version = "wolf-gui/0.3"
    # Squelch default access logging — `serve_forever` is run in
    # the foreground; the user already sees their own actions.
    silence_log = True

    # Project root is attached to the server instance by
    # `build_server`. We can't put it on the handler class because
    # the handler is per-request.
    @property
    def project_root(self) -> Path:
        return getattr(self.server, "project_root", Path.cwd())

    # ---------------- Standard log silencing ----------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if getattr(self.server, "log_to_stderr", False):
            super().log_message(format, *args)

    # ---------------- Helpers ----------------

    def _write(
        self,
        status: int,
        content_type: str,
        body: bytes,
        *,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _write_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._write(status, "application/json; charset=utf-8", body)

    def _write_404(self) -> None:
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _read_post_body(self) -> Optional[bytes]:
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError:
            return None
        if length < 0 or length > _MAX_POST_BYTES:
            return None
        if length == 0:
            return b""
        try:
            return self.rfile.read(length)
        except OSError:
            return None

    # ---------------- Dispatch ----------------

    def do_GET(self) -> None:  # noqa: N802  (BaseHTTPRequestHandler API)
        url = urlsplit(self.path)
        path = url.path
        query = parse_qs(url.query)
        if path == "/":
            self._serve_index()
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return
        if path == "/api/health":
            self._api_health()
            return
        if path == "/api/settings":
            self._api_settings_get()
            return
        if path == "/api/audit-tail":
            self._api_audit_tail(query)
            return
        self._write_404()

    def do_POST(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        path = url.path
        if path == "/api/settings":
            self._api_settings_post()
            return
        if path == "/api/command":
            self._api_command()
            return
        self._write_404()

    # ---------------- Route handlers ----------------

    def _serve_index(self) -> None:
        index = STATIC_ROOT / "index.html"
        try:
            body = index.read_bytes()
        except OSError:
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "index.html not found"},
            )
            return
        self._write(HTTPStatus.OK, "text/html; charset=utf-8", body)

    def _serve_static(self, raw_name: str) -> None:
        # Reject obvious traversal attempts up front.
        if not raw_name or "\x00" in raw_name:
            self._write_404()
            return
        candidate = (STATIC_ROOT / raw_name).resolve()
        try:
            candidate.relative_to(STATIC_ROOT)
        except ValueError:
            self._write_404()
            return
        if not candidate.is_file():
            self._write_404()
            return
        suffix = candidate.suffix.lower()
        if suffix == ".html":
            ctype = "text/html; charset=utf-8"
        elif suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif suffix == ".svg":
            ctype = "image/svg+xml"
        elif suffix in (".png", ".jpg", ".jpeg", ".gif"):
            ctype = "application/octet-stream"
        else:
            ctype = "application/octet-stream"
        try:
            body = candidate.read_bytes()
        except OSError:
            self._write_404()
            return
        self._write(HTTPStatus.OK, ctype, body)

    def _api_health(self) -> None:
        self._write_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "host": self.server.server_address[0],
                "port": self.server.server_address[1],
                "project_root": str(self.project_root),
            },
        )

    def _api_settings_get(self) -> None:
        path = default_settings_path(self.project_root)
        try:
            settings = load_settings(path)
        except Exception:  # noqa: BLE001
            settings = dict(DEFAULT_SETTINGS)
        self._write_json(HTTPStatus.OK, {"settings": settings})

    def _api_settings_post(self) -> None:
        body = self._read_post_body()
        if body is None:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid body"},
            )
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid JSON"},
            )
            return
        if not isinstance(payload, dict):
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "JSON object required"},
            )
            return
        try:
            saved = save_settings(
                default_settings_path(self.project_root), payload
            )
        except SettingsError as exc:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": exc.label},
            )
            return
        self._write_json(HTTPStatus.OK, {"settings": saved})

    def _api_command(self) -> None:
        body = self._read_post_body()
        if body is None:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid body"},
            )
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid JSON"},
            )
            return
        if not isinstance(payload, dict):
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "JSON object required"},
            )
            return
        command = payload.get("command")
        args = payload.get("args") or {}
        if not isinstance(command, str) or command not in COMMAND_ALLOWLIST:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"command not allowed: {command!r}"},
            )
            return
        if not isinstance(args, dict):
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "args must be an object"},
            )
            return
        argv = _command_to_argv(command, args)
        if argv is None:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"command not allowed: {command!r}"},
            )
            return
        # Force --project-root to our server's project root and a
        # consistent --output mode (always JSON; the UI re-renders).
        full_argv = ["--project-root", str(self.project_root)] + argv
        if "--output" not in full_argv:
            full_argv.extend(["--output", "json"])

        from ..cli import main as cli_main

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        code = 1
        try:
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                code = cli_main(full_argv)
        except SystemExit as exc:
            # argparse-on-bad-input does sys.exit; treat as bad request.
            code = int(exc.code) if isinstance(exc.code, int) else 2
        except Exception as exc:  # noqa: BLE001
            code = 1
            err_buf.write(f"internal: {type(exc).__name__}\n")
        stdout = out_buf.getvalue()
        stderr = err_buf.getvalue()

        # If the CLI emitted JSON, parse it for the UI; otherwise
        # the UI gets the raw text.
        result_obj: Any = None
        try:
            result_obj = json.loads(stdout) if stdout.strip() else None
        except json.JSONDecodeError:
            result_obj = None

        result_stage = ""
        if isinstance(result_obj, dict):
            result_stage = str(result_obj.get("stage", ""))

        # Audit (metadata only).
        try:
            _audit_gui_command(
                self.project_root,
                command=command,
                backend=str(
                    args.get("backend")
                    or args.get("llm_backend")
                    or args.get("gmail_backend")
                    or "fake"
                ),
                result_stage=result_stage,
                outcome="allow" if code == 0 else "deny",
                decision="allow" if code == 0 else "deny",
            )
        except OSError:
            pass

        self._write_json(
            HTTPStatus.OK,
            {
                "exit_code": code,
                "command": command,
                "result": result_obj,
                "stdout_text": stdout if result_obj is None else "",
                "stderr_text": stderr,
            },
        )

    def _api_audit_tail(self, query: Mapping[str, List[str]]) -> None:
        limit = 20
        action_kind: Optional[str] = None
        if "limit" in query and query["limit"]:
            try:
                limit = int(query["limit"][0])
            except ValueError:
                limit = 20
        if "action_kind" in query and query["action_kind"]:
            action_kind = query["action_kind"][0]
        argv = ["--project-root", str(self.project_root), "audit-tail"]
        argv.extend(["--limit", str(limit)])
        if action_kind:
            argv.extend(["--action-kind", action_kind])
        argv.extend(["--output", "json"])

        from ..cli import main as cli_main

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        try:
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                cli_main(argv)
        except SystemExit:
            pass
        except Exception as exc:  # noqa: BLE001
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"internal: {type(exc).__name__}"},
            )
            return
        out = out_buf.getvalue()
        try:
            decoded = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError:
            decoded = {}
        self._write_json(HTTPStatus.OK, decoded)


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def build_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    project_root: Optional[Path] = None,
    allow_lan: bool = False,
    log_to_stderr: bool = False,
) -> HTTPServer:
    """Construct an HTTPServer with safe defaults.

    Binding 0.0.0.0 (or any non-loopback) requires `allow_lan=True`.
    """
    bind_host = host or DEFAULT_HOST
    if not allow_lan and bind_host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError(
            f"host {bind_host!r} is not loopback; pass allow_lan=True "
            "to bind to a non-loopback address"
        )
    server = HTTPServer((bind_host, int(port)), WolfGuiHandler)
    server.project_root = Path(project_root or Path.cwd()).resolve()
    server.log_to_stderr = bool(log_to_stderr)
    return server


def serve_forever(server: HTTPServer) -> None:
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
