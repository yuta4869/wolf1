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
  sent. Each create_draft call writes an `AuditEvent`
  (`action_kind=gmail.create_draft`) to `var/audit/audit.jsonl`;
  the event records the draft id, source message id, subject
  suggestion, and `draft_body_length` — never the body text,
  never the access token (PR #27).
- `gmail-thread` (PR #27, extended PR #28) — read messages and
  group them by Gmail's `threadId`, with a normalized-subject
  fallback. PR #28 adds `--thread-id` for direct fetch via
  `GET /threads/{id}?format=full`.
- `gmail-search-summarize` (PR #27, extended PR #28) — search →
  read → per-message (default) or per-thread (`--threaded`)
  summary, then a Router-mediated aggregate. PR #28 adds
  `--thread-id` (implies threaded mode) and a `result.trace`
  block with `input_mode` / `gmail_backend` / `llm_backend` /
  per-stage counts.
- **Audit coverage (PR #28)** — every `gmail-*` command writes
  one `AuditEvent` to `var/audit/audit.jsonl` with
  `action_kind` ∈ {`gmail.search`, `gmail.read`,
  `gmail.thread`, `gmail.search_summarize`,
  `gmail.create_draft`}. Details are metadata only (provider,
  query length, counts, ids); the access token, raw mail body,
  and the search query *content* are never recorded.

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

# Group messages into Gmail threads.
PYTHONPATH=src python3 -m wolf.cli gmail-thread \
    --gmail-backend fake --query "meeting"

# Search → per-message summary → aggregate.
PYTHONPATH=src python3 -m wolf.cli gmail-search-summarize \
    --gmail-backend fake --query "meeting" \
    --llm-backend fake

# Search → per-thread summary → aggregate.
PYTHONPATH=src python3 -m wolf.cli gmail-search-summarize \
    --gmail-backend fake --query "meeting" --threaded \
    --llm-backend fake --output text

# Direct thread-id fetch (PR #28) — skip search entirely.
PYTHONPATH=src python3 -m wolf.cli gmail-thread \
    --gmail-backend fake --thread-id thread_1 --output text

# Summarize one Gmail thread by id, no search step (PR #28).
PYTHONPATH=src python3 -m wolf.cli gmail-search-summarize \
    --gmail-backend fake --thread-id thread_1 \
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
    "body_truncated": true,
    "provider": "fake"
  }
}
```

### `gmail-thread` (PR #27)

```json
{
  "stage": "complete",
  "result": {
    "message_count": 4,
    "thread_count": 2,
    "threads": [
      {
        "thread_id": "thread_1",
        "subject": "Quarterly planning meeting",
        "message_count": 2,
        "participants": ["Alice Example <...>", "Bob Example <...>"],
        "first_date": "Tue, 21 May 2026 09:00:00 +0900",
        "last_date": "Tue, 21 May 2026 17:00:00 +0900",
        "messages": [
          {
            "index": 0,
            "gmail_message_id": "msg_1",
            "rfc822_message_id": "<fake-001@example.invalid>",
            "subject": "Quarterly planning meeting",
            "from": "Alice Example <...>",
            "date": "Tue, 21 May 2026 09:00:00 +0900"
          }
        ]
      }
    ]
  }
}
```

`messages[]` carries no body bytes. To render a thread, re-read
each `gmail_message_id` via `gmail-read`.

### `gmail-search-summarize` (PR #27)

Per-message mode (default):

```json
{
  "stage": "complete",
  "result": {
    "mode": "message",
    "query": "meeting",
    "message_count": 2,
    "summarized_count": 2,
    "summary": "Aggregate Router-mediated summary...",
    "summary_length": 272,
    "messages": [
      {
        "message_id": "msg_1",
        "thread_id": "thread_1",
        "subject": "...",
        "from": "...",
        "date": "...",
        "summary_length": 272
      }
    ]
  }
}
```

Per-thread mode (`--threaded`):

```json
{
  "stage": "complete",
  "result": {
    "mode": "threaded",
    "query": "meeting",
    "message_count": 2,
    "summarized_count": 2,
    "thread_count": 1,
    "summary": "Aggregate Router-mediated summary...",
    "summary_length": 272,
    "threads": [
      {
        "thread_id": "thread_1",
        "subject": "Quarterly planning meeting",
        "message_count": 2,
        "participants": ["Alice Example <...>", "Bob Example <...>"],
        "summary_length": 272
      }
    ]
  }
}
```

Per-message / per-thread *summary text* is omitted by default to
keep the JSON envelope small. Pass `--include-per-message-summary`
or `--include-per-thread-summary` to attach the strings under
`result.messages[].summary` / `result.threads[].summary`. The
aggregate `result.summary` is always included.

The aggregate step runs under a Router with
`allow_warning_injection_findings=True` because its input is
LLM-generated text (which often contains benign warning markers
like the word "command"). Critical markers still block.

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
- `gmail-draft` audit (PR #27): every `create_draft` call writes
  one event to `var/audit/audit.jsonl` with
  `action_kind=gmail.create_draft`. The event carries the draft
  id, source message id, subject suggestion, and
  `draft_body_length` — never the draft body text, never the
  source body, never the access token. Failed attempts are
  recorded with `outcome=draft_failed` and an `error_label` from
  `GmailClientError`. If the audit write itself fails (disk
  full, permission), the CLI fails closed with `stage=audit_log`
  and exit 2, even though the draft was already created on
  Gmail's side.
- See [`docs/setup/gmail.md`](../setup/gmail.md) for how to
  obtain a real access token, where to store it, and what
  scopes are needed.
- **Full Gmail API audit (PR #28)** — every `gmail-*` command
  records one `AuditEvent` to `var/audit/audit.jsonl`:
  - `gmail.search` (actor `cli:gmail-search`): `provider`,
    `query_length`, `max_results`, `hit_count`,
    `enriched_count`, `skipped_count`.
  - `gmail.read` (actor `cli:gmail-read`): `provider`,
    `message_id`, `thread_id`, `subject`, `has_attachments`,
    `attachments_count`, `body_total_bytes`.
  - `gmail.thread` (actor `cli:gmail-thread`): `provider`,
    `input_mode` (`query` | `message_id` | `thread_id`),
    `query_length`, `message_count`, `thread_count`,
    `skipped_count`.
  - `gmail.search_summarize` (actor
    `cli:gmail-search-summarize`): `provider`, `llm_backend`,
    `input_mode`, `query_length`, `searched_count`,
    `read_count`, `summarized_count`, `threaded`,
    `thread_count`, `aggregate_summary_length`.
  - `gmail.create_draft` is unchanged from PR #27.
  - Raw mail body, draft body, access token, and the search
    query string itself are NEVER recorded. Only metadata
    counts and ids. If an audit write fails (`OSError`), the
    CLI returns `stage=audit_log` with exit 2.

## Traceability (PR #28)

`gmail-search-summarize` JSON adds a `result.trace` block:

```json
"trace": {
  "input_mode": "thread_id",
  "gmail_backend": "fake",
  "llm_backend": "fake",
  "searched_count": 0,
  "read_count": 2,
  "summarized_count": 2,
  "audit_event_count": 1
}
```

Use it to correlate a run with the corresponding entries in
`var/audit/audit.jsonl` without parsing the rest of the JSON.

## What this is not

- No `gmail.send` / SMTP / IMAP / forward / archive / label
  mutation.
- No OAuth browser login or refresh-token flow.
- No attachment content download (metadata only).
- No background sync / push notifications / watch.
- No contacts / calendar awareness.
- No bulk operations.
