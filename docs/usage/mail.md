# Local mail pack (PR #22)

`wolf` v0.2 adds read-only handling of local mail files (`.eml`,
`.mbox`). There is **no** Gmail / IMAP / SMTP integration, no actual
mail send, no Gmail draft creation. The mail subcommands read from
the user's filesystem and route through the same Router safety
pipeline used by the file-summarize commands.

## What's covered

- `mail-summarize` — summarize one `.eml`, up to `--limit` messages
  from a `.mbox`, or messages from a Maildir directory.
- `mail-search` — substring search across subject / from / body of
  local mail.
- `mail-draft` — generate a reply draft using an explicit
  `--instruction` (trusted) plus the mail body (untrusted).
  Nothing is sent; the draft body is returned in the JSON
  envelope (or printed to stdout in text mode).
- `mail-thread` (PR #25) — group messages into conversation threads
  via `Message-ID` / `In-Reply-To` / `References` with a
  normalized-subject fallback.
- `mail-search-summarize` (PR #25) — run a search and summarize each
  matching message (or thread with `--threaded`), then summarize the
  per-message / per-thread summaries into one aggregate.

The mail body is always wrapped as `UntrustedText(source=email)` so
the prompt-injection scan runs on it. Mail subcommands run in
**strict** prompt-injection mode by default — warning-level markers
also block, not just critical markers.

## Filters

All five mail subcommands (`mail-summarize`, `mail-search`,
`mail-draft`, `mail-thread`, `mail-search-summarize`) accept the same
pre-filter flags:

- `--filter-subject SUBSTR`
- `--filter-from SUBSTR`
- `--filter-body-contains SUBSTR`

All three are case-insensitive substring matches.

**OR within a kind**: pass the same flag multiple times to OR-combine.
For example `--filter-from alice --filter-from bob` keeps messages
whose From header contains "alice" *or* "bob".

**AND across kinds**: different filter flags are AND-combined.
`--filter-from alice --filter-subject meeting` keeps messages from
alice *and* whose subject contains "meeting".

For `mail-search` filters are orthogonal to `--query`: the filter
runs first to drop messages, then `--query` runs against whatever
survives.

For `mail-summarize` filters cut the set of messages that get
summarized.

For `mail-draft` filters narrow the candidate list **before**
`--message-index` is applied. `--message-index 0` (default) selects
the first filtered message.

Exit-2 semantics when filters drop everything:
- `mail-search` / `mail-search-summarize` with empty candidate set →
  `stage=search` `result.message_count=0`.
- `mail-summarize` / `mail-thread` with empty candidate set →
  `stage=mail_read` `reason="no messages"`.
- `mail-draft` with empty candidate set → `stage=mail_read`
  `reason="no messages"`.
- `mail-draft` with non-empty candidate set but `--message-index`
  out of range → `stage=mail_read` `reason="message_index out of range"`.

## Datetime filters (PR #25)

In addition to the substring filters above, all mail subcommands
accept `--filter-since YYYY-MM-DD` and `--filter-until YYYY-MM-DD`:

- `--filter-since` is the **start** of the named UTC day (inclusive).
- `--filter-until` is the **end** of the named UTC day (inclusive),
  i.e. `YYYY-MM-DD 23:59:59.999999 UTC`.
- The mail's `Date:` header is parsed with
  `email.utils.parsedate_to_datetime`. Timezone-naive timestamps are
  treated as UTC.
- A message whose Date header is missing or unparseable is **skipped**
  (with a warning recorded in `result.skipped`) when either flag is
  set; without the flag those messages pass through unchanged.
- An invalid CLI date (e.g., `--filter-since not-a-date`) exits 2
  before any mail is read; the stderr message names the offending
  argument.

## Threading (PR #25)

`mail-thread` groups the messages it reads into conversation threads
using:

1. `Message-ID`, `In-Reply-To`, and `References` headers (union-find
   over the lineage graph).
2. Normalized subject as a fallback: strip leading `Re:` / `Fwd:` /
   `FW:` / `AW:` prefixes, lowercase, and collapse whitespace.
   Messages with the same normalized subject share a root.

Each thread JSON record carries `thread_id`, `subject`,
`message_count`, `participants`, `first_date`, `last_date`, and a
`messages[]` array with `index` (into the source mailbox), `subject`,
`from`, `date`, `message_id`. Bodies are NOT included; callers who
need a body re-read the source mail at the given index.

`mail-search-summarize --threaded` reuses the same threader: hits are
matched to threads, each thread's messages are concatenated, and the
Router summarizes them per-thread. The aggregate then summarizes the
per-thread summaries.

For the aggregate step the Router is configured with
`allow_warning_injection_findings=True` because the input is the
LLM's own per-message / per-thread output (which often contains
benign warning markers like the word "command"). Critical markers
still block.

## File support

- `.eml` — single message, parsed with Python's `email` package
  (default policy). Headers extracted: from / to / cc / subject /
  date / message-id. Text/plain part is preferred; text/html is
  converted to plain text by a stdlib HTMLParser.
- `.mbox` — multiple messages, parsed with `mailbox.mbox`.
  `--limit` caps how many are read (default 10).
- Maildir directory — a directory containing `cur/`, `new/`, and
  `tmp/` subdirectories. Parsed with `mailbox.Maildir`. The CLI
  detects Maildir automatically when `--path` is a directory; if
  the directory does not have the Maildir layout, the command
  exits 2 with a `stage=mail_read` denial.

All three formats flow through the same three subcommands
(`mail-summarize`, `mail-search`, `mail-draft`) and accept the same
filter / output flags.

## Attachment metadata

Attachment bytes are never read into the body. For each attachment,
wolf records:

- `filename` (empty string if absent)
- `content_type`
- `size_bytes` (size of the encoded payload as it appeared in the
  mail; base64 is reported at the encoded size)

`mail-summarize` JSON `result.summaries[]` carries `has_attachments`,
`attachments_count`, and an `attachments` array.

`mail-search` JSON `result.hits[]` carries `has_attachments` and
`attachments_count` (no per-attachment array, to keep hit payloads
small).

`mail-draft` JSON `result` carries `source_has_attachments` and
`source_attachments_count` for the message that produced the draft.

## Examples

```sh
# Summarize one .eml with the in-process Fake LLM (no network).
PYTHONPATH=src python3 -m wolf.cli mail-summarize \
    --path ./tests/fixtures/mail/sample.eml --backend fake

# Summarize the first 5 messages of an .mbox via local Ollama.
PYTHONPATH=src python3 -m wolf.cli mail-summarize \
    --path ./mail/inbox.mbox \
    --limit 5 \
    --backend ollama --model llama3.1

# Substring search across an .mbox.
PYTHONPATH=src python3 -m wolf.cli mail-search \
    --path ./tests/fixtures/mail/sample.mbox --query "meeting"

# Reply draft for an .eml (no send).
PYTHONPATH=src python3 -m wolf.cli mail-draft \
    --path ./tests/fixtures/mail/sample.eml \
    --instruction "丁寧に断る返信を作って" \
    --backend fake --output text

# Reply draft pointing at message index 2 inside an .mbox.
PYTHONPATH=src python3 -m wolf.cli mail-draft \
    --path ./mail/inbox.mbox \
    --message-index 2 \
    --instruction "短く確認の返事をして" \
    --backend ollama --model llama3.1

# Group an .mbox into conversation threads (no summarization).
PYTHONPATH=src python3 -m wolf.cli mail-thread \
    --path ./tests/fixtures/mail/thread.mbox

# Search + per-message summary in one shot.
PYTHONPATH=src python3 -m wolf.cli mail-search-summarize \
    --path ./tests/fixtures/mail/sample.mbox \
    --query meeting --backend fake

# Search + per-thread summary (groups hits into threads first).
PYTHONPATH=src python3 -m wolf.cli mail-search-summarize \
    --path ./tests/fixtures/mail/thread.mbox \
    --query planning --threaded --backend fake

# Restrict to a UTC date window before summarizing.
PYTHONPATH=src python3 -m wolf.cli mail-summarize \
    --path ./mail/inbox.mbox \
    --filter-since 2026-05-01 --filter-until 2026-05-31 \
    --backend fake
```

## JSON output shape

### `mail-summarize`

```json
{
  "stage": "complete",
  "result": {
    "message_count": 3,
    "summarized_count": 3,
    "summaries": [
      {
        "message_id": "<...>",
        "subject": "Quarterly planning meeting",
        "from": "Alice Example <...>",
        "date": "Tue, 21 May 2026 09:00:00 +0900",
        "summary": "...",
        "summary_length": 412
      }
    ]
  }
}
```

### `mail-search`

```json
{
  "stage": "complete",
  "result": {
    "query": "meeting",
    "message_count": 3,
    "hits": [
      {
        "subject": "Quarterly planning meeting",
        "from": "Alice Example <...>",
        "date": "...",
        "message_id": "<...>",
        "snippet": "Quarterly planning meeting",
        "match_field": "subject",
        "match_count": 1
      }
    ]
  }
}
```

### `mail-draft`

```json
{
  "stage": "complete",
  "result": {
    "source_subject": "Quarterly planning meeting",
    "source_from": "Alice Example <...>",
    "source_message_id": "<...>",
    "subject_suggestion": "Re: Quarterly planning meeting",
    "body": "<draft body>",
    "body_length": 412
  }
}
```

### `mail-thread`

```json
{
  "stage": "complete",
  "result": {
    "thread_count": 3,
    "threads": [
      {
        "thread_id": "<thread-001@example.invalid>",
        "subject": "Q3 planning kickoff",
        "message_count": 3,
        "participants": ["Alice Example <...>", "Bob Example <...>", "Carol Example <...>"],
        "first_date": "Tue, 21 May 2026 09:00:00 +0900",
        "last_date": "Tue, 21 May 2026 11:00:00 +0900",
        "messages": [
          {
            "index": 0,
            "subject": "Q3 planning kickoff",
            "from": "Alice Example <...>",
            "date": "Tue, 21 May 2026 09:00:00 +0900",
            "message_id": "<thread-001@example.invalid>"
          }
        ]
      }
    ]
  }
}
```

### `mail-search-summarize`

Per-message mode (default):

```json
{
  "stage": "complete",
  "result": {
    "mode": "message",
    "query": "meeting",
    "hit_count": 2,
    "summarized_count": 2,
    "skipped_count": 0,
    "summary": "Aggregate summary across hits...",
    "messages": [
      {
        "message_id": "<...>",
        "subject": "...",
        "from": "...",
        "date": "...",
        "match_field": "subject",
        "match_count": 1,
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
    "query": "planning",
    "hit_count": 3,
    "summarized_count": 1,
    "skipped_count": 0,
    "summary": "Aggregate summary across threads...",
    "threads": [
      {
        "thread_id": "<thread-001@example.invalid>",
        "subject": "Q3 planning kickoff",
        "message_count": 3,
        "participants": ["Alice Example <...>", "Bob Example <...>", "Carol Example <...>"],
        "summary_length": 272
      }
    ]
  }
}
```

Per-message and per-thread summary *bodies* are not returned to keep
the JSON envelope small; their lengths appear as `summary_length`.
The aggregate `summary` field carries the full Router-mediated text.

## Privacy posture

- The .eml / .mbox file path passes through `ProjectBoundaryGuard`
  and `SensitivePathGuard` before any bytes are read. Pointing
  mail commands at `./secrets/foo.eml` is rejected at
  `stage=sensitive_path`.
- Attachment bytes are never read into the body. Base64 / binary
  parts are recorded as `has_attachments=True` and skipped.
- The HTML-to-text fallback drops `<script>` and `<style>` blocks
  before serializing.
- `MailReadError` labels contain no body bytes; only short reason
  strings (e.g., `"body exceeds max_bytes (1048576)"`,
  `"parse failed (HeaderParseError)"`).
- Snippets in `mail-search` results are anchored at the match
  position and bounded by `--snippet-context-bytes` (default 80).
- `mail-draft` separates instruction from mail body with explicit
  `<INSTRUCTION>` / `<EMAIL_METADATA>` boundary text in the prompt.
  The mail body itself remains wrapped as `UntrustedText` and is
  scanned by `PromptInjectionShield` before reaching the LLM.

## What this is not

- No `gmail` / `imap` / `smtp` / `pop3` integration.
- No send / forward / archive / label.
- No `.msg` (Outlook) parsing.
- No PGP / S/MIME decryption.
- No attachment content extraction (PDF / docx / images).
- No contacts / calendar awareness.
