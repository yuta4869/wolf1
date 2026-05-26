# Local mail pack (PR #22)

`wolf` v0.2 adds read-only handling of local mail files (`.eml`,
`.mbox`). There is **no** Gmail / IMAP / SMTP integration, no actual
mail send, no Gmail draft creation. The mail subcommands read from
the user's filesystem and route through the same Router safety
pipeline used by the file-summarize commands.

## What's covered

- `mail-summarize` — summarize one `.eml` or up to `--limit`
  messages from a `.mbox`.
- `mail-search` — substring search across subject / from / body of
  local mail.
- `mail-draft` — generate a reply draft using an explicit
  `--instruction` (trusted) plus the mail body (untrusted).
  Nothing is sent; the draft body is returned in the JSON
  envelope (or printed to stdout in text mode).

The mail body is always wrapped as `UntrustedText(source=email)` so
the prompt-injection scan runs on it. Mail subcommands run in
**strict** prompt-injection mode by default — warning-level markers
also block, not just critical markers.

## File support

- `.eml` — single message, parsed with Python's `email` package
  (default policy). Headers extracted: from / to / cc / subject /
  date / message-id. Text/plain part is preferred; text/html is
  converted to plain text by a stdlib HTMLParser. Attachments are
  recorded as a `has_attachments` flag but their bytes are never
  read into the body.
- `.mbox` — multiple messages, parsed with `mailbox.mbox`.
  `--limit` caps how many are read (default 10). `--filter-subject`,
  `--filter-from`, `--filter-body-contains` work at the read-local
  layer if you call the module directly; the CLI exposes
  `--query` on `mail-search` and the basic limit on
  `mail-summarize` / `mail-draft`.

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
