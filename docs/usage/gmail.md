# Gmail read / draft (PR #26)

Local-first Gmail integration. Implements the minimum needed to
search, read, summarize, and create drafts. Does **NOT** implement:

- mail send (no `users.messages.send`, no SMTP, no IMAP).
- OAuth browser login.
- refresh-token flow.
- attachment content download.
- modify / labels / push notifications / background sync.

The default backend is the in-memory `FakeGmailClient`, which lets
the CLI and tests run with `network_mode: none`. The real Gmail
backend is opt-in via `--gmail-backend gmail --credentials-path
/path/to/gmail_token.json`.

## What's covered

- `gmail-search` — Gmail query (`q=`) → list of matching message ids
  enriched with subject / from / date / snippet by default.
- `gmail-read` — fetch one message and return metadata plus a
  **bounded** `body_preview` (default 500 bytes). The full body is
  never returned; large bodies set `body_truncated=true`.
- `gmail-summarize` — search and/or read, then summarize each
  message via the Router pipeline (mail-strict; warning markers
  block).
- `gmail-draft` — read one message, generate a reply via the LLM,
  and create the draft on Gmail (or the fake). The draft is NEVER
  sent.

## Backends

| Flag | Meaning | Default |
|---|---|---|
| `--gmail-backend fake` | `FakeGmailClient` (in-memory). | default |
| `--gmail-backend gmail` | Real Gmail REST API via stdlib `urllib`. Requires `--credentials-path`. | opt-in |
| `--credentials-path PATH` | Path to a JSON file `{"access_token": "..."}` produced out-of-band. Not refreshed by this CLI. | required for `gmail` |
| `--gmail-base-url URL` | Override `https://gmail.googleapis.com` (for localhost test stubs only). | unused |
| `--allow-non-https-gmail` | Permit a non-https base URL (localhost only). | off |

LLM backend for `gmail-summarize` / `gmail-draft`:

| Flag | Meaning | Default |
|---|---|---|
| `--llm-backend fake` | In-process `FakeLLM`. | default |
| `--llm-backend ollama` | Local Ollama via `urllib`. Requires `--model`. | opt-in |
| `--model NAME` | Ollama model name (e.g. `llama3.1`). | unused |
| `--ollama-url URL` | Ollama server URL. | `http://127.0.0.1:11434` |
| `--allow-non-localhost-ollama` | Permit a non-localhost Ollama URL. | off |

## Credentials file

The file is a plain JSON object. Minimum shape:

```json
{"access_token": "ya29.A0..."}
```

A `refresh_token`, `expiry`, or `scopes` field may be present; this
CLI ignores them and does NOT refresh the access token.

**Do not commit this file.** Put it under `secrets/` (which is
denied by `SensitivePathGuard`) or somewhere outside the repo, and
pass the absolute path with `--credentials-path`.

How you obtain the token is out of scope for this CLI. Use the
Google OAuth Playground, `gcloud auth print-access-token`, or a
separate OAuth client. Whichever path you take, the token must
include at least `gmail.readonly` (for search / read /
summarize) and `gmail.compose` (for draft creation).

## Examples

```sh
# Fake backend (no network).
PYTHONPATH=src python3 -m wolf.cli gmail-search \
    --gmail-backend fake --query "meeting"

# Real Gmail, opt-in.
PYTHONPATH=src python3 -m wolf.cli gmail-search \
    --gmail-backend gmail \
    --credentials-path /Users/me/secrets/gmail_token.json \
    --query "from:alice meeting" --limit 5

# Read a single message with a bounded body preview.
PYTHONPATH=src python3 -m wolf.cli gmail-read \
    --gmail-backend fake --message-id msg_1 \
    --body-preview-bytes 1000

# Summarize matching messages via FakeLLM.
PYTHONPATH=src python3 -m wolf.cli gmail-summarize \
    --gmail-backend fake --query "meeting" \
    --llm-backend fake

# Draft a reply (creates a Gmail draft; never sends).
PYTHONPATH=src python3 -m wolf.cli gmail-draft \
    --gmail-backend fake --message-id msg_1 \
    --instruction "丁寧に返信して" \
    --llm-backend fake --output text
```

## JSON output shape

### `gmail-search`

```json
{
  "stage": "complete",
  "result": {
    "query": "meeting",
    "hit_count": 2,
    "hits": [
      {"message_id": "msg_1", "thread_id": "thread_1"}
    ],
    "messages": [
      {
        "message_id": "msg_1",
        "thread_id": "thread_1",
        "subject": "Quarterly planning meeting",
        "from": "Alice Example <alice@example.invalid>",
        "date": "Tue, 21 May 2026 09:00:00 +0900",
        "snippet": "Kicking off Q3 planning...",
        "has_attachments": false,
        "attachments_count": 0
      }
    ]
  }
}
```

`messages[]` is empty if `--no-enrich` is passed.

### `gmail-read`

```json
{
  "stage": "complete",
  "result": {
    "message_id": "msg_1",
    "thread_id": "thread_1",
    "subject": "...",
    "from": "...",
    "to": "...",
    "cc": "",
    "date": "...",
    "rfc822_message_id": "<...>",
    "snippet": "...",
    "label_ids": ["INBOX"],
    "has_attachments": false,
    "attachments_count": 0,
    "attachments": [],
    "body_preview": "First N bytes of body...",
    "body_preview_bytes": 78,
    "body_total_bytes": 78,
    "body_truncated": false
  }
}
```

### `gmail-summarize`

```json
{
  "stage": "complete",
  "result": {
    "message_count": 2,
    "summarized_count": 2,
    "summaries": [
      {
        "message_id": "msg_1",
        "thread_id": "thread_1",
        "subject": "...",
        "from": "...",
        "date": "...",
        "summary": "Router-mediated summary text...",
        "summary_length": 421
      }
    ]
  }
}
```

### `gmail-draft`

```json
{
  "stage": "complete",
  "reason": "gmail draft created (not sent)",
  "result": {
    "draft_id": "fake_draft_1",
    "message_id": "fake_drafted_msg_1",
    "thread_id": "thread_1",
    "source_message_id": "msg_1",
    "source_subject": "Quarterly planning meeting",
    "source_from": "Alice Example <...>",
    "subject_suggestion": "Re: Quarterly planning meeting",
    "body_preview": "First N bytes of the draft...",
    "body_preview_bytes": 500,
    "body_total_bytes": 946,
    "body_truncated": true
  }
}
```

## Safety / privacy posture

- Default backend is `fake`. The real backend is **opt-in** and
  requires an explicit file path; the CLI never searches the
  filesystem for tokens.
- The access token is held inside a `GmailCredentials` dataclass
  whose `repr` / `str` redact the value. The token never appears in
  CLI stdout, stderr, error labels, or log lines.
- Gmail message bodies are wrapped as `UntrustedText(SourceKind.
  EMAIL)` before reaching the LLM via `Router.route(...)`. The
  Router runs the prompt-injection scan in mail-strict mode
  (warning markers block).
- `gmail-read` returns a bounded `body_preview` (default 500
  bytes); the full body is not embedded in JSON. To process the
  full body, use `gmail-summarize` or `gmail-draft`, which feed it
  through the Router.
- `FakeGmailClient` deliberately exposes no `send` method. Trying
  to call `.send(...)` raises `AttributeError`. The real
  `GmailClient` likewise implements no send.
- Network errors, HTTP errors, timeouts, and invalid JSON all
  surface as `GmailClientError` with a short label; response
  bodies are not embedded in errors.
- The real backend refuses non-https base URLs unless
  `--allow-non-https-gmail` is passed AND the host is localhost.

## What this is not

- No `gmail.send` / SMTP / IMAP / forward / archive / label
  mutation.
- No OAuth browser login or refresh-token flow.
- No attachment content download (metadata only).
- No background sync / push notifications / watch.
- No contacts / calendar awareness.
- No bulk operations.
