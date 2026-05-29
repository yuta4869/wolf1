# Local GUI shell (PR #31, v0.3 dev)

A tiny **local-only** web GUI that exposes the existing CLI surface
through `http.server` (stdlib). No Flask / FastAPI / React /
Electron. No external dependencies. No authentication. The GUI is
a convenience shell for command entry, audit inspection, and a
placeholder for future avatar / robot UI work.

## Launching

```sh
PYTHONPATH=src python3 -m wolf.cli gui
# Or equivalently:
PYTHONPATH=src python3 -m wolf.gui

# Open in the system browser
PYTHONPATH=src python3 -m wolf.cli gui --open-browser

# Pick a port
PYTHONPATH=src python3 -m wolf.cli gui --port 9000

# Expose on the LAN (explicit opt-in; there is NO auth)
PYTHONPATH=src python3 -m wolf.cli gui --host 0.0.0.0 --allow-lan
```

Default bind is **127.0.0.1:8765**. Binding a non-loopback host
requires `--allow-lan`; without it the CLI exits 2 with
`reason=...not loopback...`.

`Ctrl+C` stops the server. The `gui.launch` audit event is written
to `var/audit/audit.jsonl` on start.

## Panels

| Tab | What it does |
|---|---|
| **Command** | Pick one of the allowlisted commands, fill arguments, POST to `/api/command`. The server re-enters the existing CLI handler. |
| **Settings** | Edit `.wolf/config/settings.json` under the current project root. Token-shaped values are rejected. |
| **Files** | Quick buttons for `summarize-file` / `search-files` / `search-summarize`. |
| **Mail** | Quick buttons for `mail-search` / `mail-summarize` / `mail-thread` / `mail-search-summarize`. |
| **Gmail** | Quick buttons for `gmail-search` / `gmail-read` / `gmail-thread` / `gmail-summarize` / `gmail-search-summarize`. Default backend is `fake`. |
| **Audit** | Live `audit-tail` via `/api/audit-tail`. Filter by `action_kind`. |
| **Avatar** | **Placeholder only.** No engine, no camera, no microphone, no WebRTC, no robot. See "What this is not". |

## API surface

```
GET  /                    →  index.html
GET  /static/<name>       →  bounded; path traversal returns 404
GET  /api/health          →  {"ok": true, "host": "...", "port": ...}
GET  /api/settings        →  current settings (no secrets)
POST /api/settings        →  validate + persist; forbidden keys → 400
POST /api/command         →  allowlisted command → run via cli.main
GET  /api/audit-tail      →  audit jsonl tail; ?limit=N&action_kind=...
```

### `POST /api/command`

```json
{
  "command": "gmail-search",
  "args": {
    "query": "meeting",
    "gmail_backend": "fake"
  }
}
```

The server validates `command` against the hard-coded allowlist
(see `COMMAND_ALLOWLIST` in `src/wolf/gui/server.py`), builds a
typed argv, and re-enters `wolf.cli.main(...)`. No shell, no
subprocess.

Response:

```json
{
  "exit_code": 0,
  "command": "gmail-search",
  "result": { /* the JSON the CLI printed, if any */ },
  "stdout_text": "...",
  "stderr_text": ""
}
```

Each call writes one `action_kind=gui.command` audit event with
metadata only (command name, backend, result stage, decision).

## Settings

Persisted at `<project_root>/.wolf/config/settings.json`. The
schema:

| Key | Type | Default | Notes |
|---|---|---|---|
| `default_llm_backend` | `fake \| ollama` | `fake` | |
| `default_ollama_model` | str | `""` | e.g. `llama3.2:3b` |
| `default_ollama_url` | str | `""` | default 127.0.0.1:11434 |
| `default_gmail_backend` | `fake \| gmail` | `fake` | |
| `default_output` | `json \| text` | `json` | |
| `theme` | `system \| light \| dark` | `system` | |
| `avatar_enabled` | bool | `false` | |
| `avatar_style` | `placeholder` | `placeholder` | only choice in v0.3 dev |
| `gmail_credentials_path` | str | `""` | optional path string only; never the token itself |

### Forbidden inputs

`POST /api/settings` rejects (HTTP 400) any payload that contains
a key like `access_token`, `refresh_token`, `client_secret`,
`api_key`, `bearer_token`, `password`, `secret`, `credentials`,
or whose value matches a token-shaped regex
(`Bearer …`, `ya29.…`, etc.). The intent is to make accidental
secret persistence loud rather than silent.

A malformed `settings.json` on disk is renamed to
`settings.json.bak.<UTC-ts>` and the GUI returns defaults — the
user keeps working without a crash and the bad file is preserved
for inspection.

## Security posture

- **Default bind 127.0.0.1.** Non-loopback host requires
  `--allow-lan`. Cited in the help text at launch.
- **No auth.** This is a developer convenience for the user's own
  machine. Don't open it on a shared LAN without understanding
  that anyone on the network can drive `/api/command`.
- **Static traversal blocked.** `/static/` is sandboxed to
  `src/wolf/gui/static/`. `..` escapes resolve out and 404.
- **POST body cap.** 256 KB. Larger payloads are refused.
- **`/api/command` is allowlisted.** Anything outside the
  allowlist (including `robot-preflight`, `summarize-email`,
  `task-extract-*`, `calendar-draft-*`, `gmail-draft`,
  `index-files`) returns 400. The omission of mutating commands
  is intentional: PR #31 ships a read / inspect shell only.
- **No subprocess.** Commands re-enter `wolf.cli.main(...)` in
  the same process; there is no `subprocess` / `shell=True`
  anywhere in the GUI layer.
- **Audit coverage.** Every launch writes a `gui.launch` event;
  every `POST /api/command` writes a `gui.command` event with
  the command name, backend, and result stage. Bodies, tokens,
  full prompts, and result content beyond `stage` are never
  embedded.
- **Browser DOM safety.** All user-provided values flow into the
  DOM via `textContent`, never `innerHTML`. No HTML
  interpolation; no `eval`; no dynamic `<script>` insertion.

## Examples

```sh
# Launch and open in browser
python3 -m wolf.cli gui --open-browser

# Just confirm /api/health from another terminal
curl -s http://127.0.0.1:8765/api/health
# {"ok": true, "host": "127.0.0.1", "port": 8765, ...}

# Drive a command from curl (allowlisted only)
curl -s -X POST http://127.0.0.1:8765/api/command \
  -H "Content-Type: application/json" \
  -d '{"command": "gmail-search", "args": {"query": "meeting"}}' \
  | python3 -m json.tool

# Inspect the audit trail
curl -s "http://127.0.0.1:8765/api/audit-tail?limit=5" | python3 -m json.tool
```

## What this is not

- **Not an authenticated multi-user app.** It is a single-user
  developer shell. No login, no session, no CSRF token.
- **Not a chat / streaming UI.** No WebSocket, no SSE; every
  command is one POST → one response.
- **Not a real avatar.** The Avatar panel is a placeholder.
  No camera, no microphone, no WebRTC, no Live2D / VRM loader,
  no robot avatar bridge.
- **Not a mutating shell.** No write commands (`mail-draft`,
  `gmail-draft`, `index-files`, `task-extract-*`,
  `calendar-draft-*`, `robot-preflight`) are reachable through
  `/api/command` in PR #31. Use the CLI directly for those.
- **Not a deployment target.** No production server hardening,
  no rate limiting, no TLS. Local-only by design.

## Known limitations

- Long-running commands block the request thread (stdlib
  `HTTPServer` is single-threaded by default). Ollama-backed
  summarize calls will visibly hang the response — that is
  expected.
- The settings backup-and-reset behavior on a malformed file
  produces one `.bak.<ts>` per load. If multiple loads happen
  on the same broken file, you get multiple stale backups.
- `--allow-lan` is fire-and-forget. There is no per-request
  audit of the remote peer beyond what `gui.command` records.
- Path validation on command args is delegated to the existing
  CLI handlers (and `ProjectBoundaryGuard` /
  `SensitivePathGuard` underneath). The GUI does not
  pre-validate paths.
