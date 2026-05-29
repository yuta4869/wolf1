# Task / calendar extraction (PR #30, v0.3 dev)

`v0.3` opens with task and calendar-event extraction from local
mail and Gmail. The pipeline produces **candidate** tasks and
**draft** iCalendar (`.ics`) files. Nothing is sent, nothing is
registered with a real calendar.

## What's covered

- `task-extract-mail` — extract tasks + events from a local
  `.eml` / `.mbox` / Maildir.
- `task-extract-gmail` — same but from Gmail (default fake
  backend; real Gmail is opt-in).
- `calendar-draft-mail` — extract events from local mail and
  emit a minimal `.ics` (`VCALENDAR` + one `VEVENT` per
  candidate). Optional `--output-file` writes the `.ics` to
  disk.
- `calendar-draft-gmail` — same but from Gmail.

## Extraction pipeline

```
mail body
  → wrap_untrusted(SourceKind.EMAIL)
  → Router.route(...)             # mail-strict prompt-injection scan
  → LLM.generate(prompt)
  → JSON parse                    # expected shape: {"tasks": [...], "events": [...]}
  → coerce to TaskCandidate / CalendarEventCandidate
  → on JSON failure: deterministic regex fallback
```

The fallback (`_heuristic_tasks` / `_heuristic_events` in
`src/wolf/tasks/extract.py`) picks up:

- `Action item:` / `TODO:` / `Task:` lines
- `Due:` / `Deadline:` / `期限:` lines with an ISO date
- `Meeting:` / `Mtg:` / `Event:` / `予定:` / `打ち合わせ:`
  lines with an ISO date
- ISO 8601 dates (`YYYY-MM-DD`) and times (`HH:MM[:SS]`)
  inside the line

The fallback is intentionally narrow — it exists so the
in-process `FakeLLM` (which never emits JSON) still produces
useful candidates in tests and `network_mode: none` runs. A
real model on real mail should produce JSON and let the
fallback stay idle.

## Candidate shape

`TaskCandidate`:

```
title, description, due_date (YYYY-MM-DD or ""), due_time (HH:MM:SS or ""),
timezone (default "UTC"), source_kind, source_id, source_subject,
source_from, confidence (0..1), evidence_snippet (bounded)
```

`CalendarEventCandidate`:

```
title, description, start_date, start_time (or "" for all-day),
end_date, end_time, timezone, location, attendees,
source_kind, source_id, source_subject, confidence,
evidence_snippet
```

`evidence_snippet` is capped at ~240 UTF-8 bytes. The full mail
body is NOT carried inside the candidate.

## .ics output

`build_ics(events)` emits a single `VCALENDAR` block with one
`VEVENT` per candidate. The output is intentionally minimal:

- `UID:` is `SHA-256(source_id|title|start_date|start_time)`
  truncated to 16 hex; identical inputs produce identical UIDs.
- All-day events use
  `DTSTART;VALUE=DATE:YYYYMMDD` / `DTEND;VALUE=DATE:YYYYMMDD`.
- UTC times use `DTSTART:YYYYMMDDTHHMMSSZ`.
- Other timezones emit `DTSTART;TZID=<tzid>:YYYYMMDDTHHMMSS`
  with no trailing `Z`.
- `SUMMARY`, `DESCRIPTION`, `LOCATION`, `ATTENDEE` are
  RFC 5545-escaped and folded at 75 octets.
- Malformed `start_date` skips the event silently.

## Backends

| Flag | Meaning | Default |
|---|---|---|
| `--backend fake` | `FakeLLM` (local mail commands) | default |
| `--backend ollama` | Local Ollama; requires `--model` | opt-in |
| `--gmail-backend fake` | `FakeGmailClient` (in-memory) | default |
| `--gmail-backend gmail` | Real Gmail; requires `--credentials-path` | opt-in |
| `--llm-backend fake` | `FakeLLM` (Gmail commands) | default |
| `--llm-backend ollama` | Local Ollama; requires `--model` | opt-in |

## Examples

```sh
# Local .eml → JSON
PYTHONPATH=src python3 -m wolf.cli task-extract-mail \
    --path ./tests/fixtures/mail/tasks_sample.eml \
    --backend fake

# Local .mbox → text
PYTHONPATH=src python3 -m wolf.cli task-extract-mail \
    --path ./tests/fixtures/mail/tasks_sample.mbox \
    --backend fake --output text

# Gmail (fake) by query → JSON
PYTHONPATH=src python3 -m wolf.cli task-extract-gmail \
    --gmail-backend fake --query "meeting" --llm-backend fake

# Gmail (fake) one message → text
PYTHONPATH=src python3 -m wolf.cli task-extract-gmail \
    --gmail-backend fake --message-id msg_5 --llm-backend fake \
    --output text

# Local mail → .ics on stdout
PYTHONPATH=src python3 -m wolf.cli calendar-draft-mail \
    --path ./tests/fixtures/mail/tasks_sample.eml \
    --backend fake --output ics

# Local mail → .ics file
PYTHONPATH=src python3 -m wolf.cli calendar-draft-mail \
    --path ./tests/fixtures/mail/tasks_sample.eml \
    --backend fake --output text \
    --output-file ./var/out/events.ics

# Gmail (fake) → .ics on stdout
PYTHONPATH=src python3 -m wolf.cli calendar-draft-gmail \
    --gmail-backend fake --query "meeting" --llm-backend fake \
    --output ics

# Real Gmail (opt-in) → JSON
PYTHONPATH=src python3 -m wolf.cli task-extract-gmail \
    --gmail-backend gmail \
    --credentials-path ~/secrets/gmail_token.json \
    --query "from:alice meeting" --llm-backend fake
```

## Audit log

Every extraction run writes one `AuditEvent` to
`var/audit/audit.jsonl`:

| `action_kind` | actor | details (metadata only) |
|---|---|---|
| `task.extract_mail` | `cli:task-extract-mail` | `source_kind`, `provider`, `output_mode`, `message_count`, `task_count`, `event_count` |
| `task.extract_gmail` | `cli:task-extract-gmail` | + `llm_backend`, `query_length`, `query_fingerprint` |
| `calendar.draft_mail` | `cli:calendar-draft-mail` | as above + `wrote_file` |
| `calendar.draft_gmail` | `cli:calendar-draft-gmail` | + `query_fingerprint`, `wrote_file` |

The raw mail body, full prompt, LLM output, and access token
are NEVER recorded. Fail-closed on `OSError`
(`stage=audit_log`, exit 2). Use `audit-tail
--action-kind task.extract_mail` (etc.) to inspect.

## Safety / privacy posture

- Mail bodies are wrapped as
  `UntrustedText(SourceKind.EMAIL)` before reaching the LLM.
  Router runs the mail-strict prompt-injection scan; warning
  markers block.
- `evidence_snippet` is bounded (~240 UTF-8 bytes). The
  candidate dataclasses do not carry the full body.
- The .ics writer does not embed body bytes either. The
  `DESCRIPTION` field is built from `source_subject`,
  `description`, and `evidence_snippet` only; callers
  who pass an oversize `evidence_snippet` get what they
  passed (the bound is enforced earlier in the pipeline).
- No Google Calendar API integration. No `gmail.send`. No
  SMTP / IMAP. Candidates and `.ics` files are the only
  outputs; the user moves them to a real calendar by hand.
- `--output-file` resolves relative paths under
  `--project-root`; absolute paths go where you point them.
  The CLI does not currently re-check the path with
  `SensitivePathGuard` for `.ics` writes, so don't aim it at
  `./secrets/` (it would be denied at the boundary anyway,
  but this is documented for clarity).

## What this is not

- Not a real calendar adapter. No event creation, no
  invitation, no notification.
- Not a planner. The CLI extracts candidates from text; it
  does not deduplicate across runs or detect conflicts.
- Not an email sender. `gmail.send` does not exist in this
  codebase and PR #30 does not introduce one.
- Not a recurring-event handler. `RRULE` is not emitted.
- Not a high-fidelity NLP system. The fallback is regex;
  the Ollama path depends entirely on the model.

## Known limitations

- The fallback only triggers on ISO dates. Free-form
  "next Thursday at 3pm" is not picked up by the fake path;
  a real LLM should handle it.
- `Meeting:` title lines often retain the trailing date /
  time fragment (e.g. `"planning sync on  14:00"`). Strip
  them by hand for now.
- `task.extract_*` and `calendar.draft_*` each run one
  Router-mediated LLM call per fetched message. Large
  `--limit` values are expensive.
- The .ics writer uses CRLF line endings and folds at 75
  octets. It does not validate that the output is RFC 5545
  conformant; it just follows the basic rules.
