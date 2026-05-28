# CLI command reference

`wolf` exposes one CLI binary (`python -m wolf.cli`) with a handful of
subcommands. All commands accept a global `--project-root PATH`
(default: `cwd`); every other flag belongs to a specific subcommand.

## Common conventions

- **Exit codes**: `0` on success, `2` on safety denial / no hits /
  file-read failure / adapter failure, `1` on internal error.
- **`--output {json,text}`** is supported by every command that
  produces user-visible output. `json` (default) emits a stable safe
  envelope that includes at least `allowed`, `stage`, `reason`,
  `executed`, `requires_confirmation`. `text` emits a human-readable
  one-liner per result and uses stderr for warnings.
- **Backends**: where an LLM is involved, `--backend {fake,ollama}`
  selects which adapter to use. `fake` is in-process and is the
  default. `ollama` requires a local Ollama daemon and a
  `--model <name>` argument.
- **Embedding backends**: where embeddings are involved,
  `--embedding-backend {fake,ollama}` and `--embedding-model <name>`
  apply the same way.
- Every command routes through the same Router, which always runs
  `ProjectBoundaryGuard` → `SensitivePathGuard` → `PolicyEngine` →
  prompt-injection scan → provider → audit log. You cannot bypass
  any of these from the CLI.

## Subcommand list

### `summarize-file`

Summarize one file under `--project-root`.

```sh
python -m wolf.cli summarize-file --path PATH [options]
```

Key flags:

- `--path PATH` (required) — relative or absolute path inside the
  project.
- `--backend {fake,ollama}` — LLM backend.
- `--model NAME` — required when `--backend ollama`.
- `--max-bytes N` — per-file size cap (default 1 MiB).
- `--chunk-size N` — bytes per chunk when chunking (default 32 KiB).
- `--max-chunks N` — chunk cap (default 32; truncation surfaces as
  a warning).
- `--no-chunk` — disable chunking.
- `--strict-prompt-injection` — block on warning-level markers in
  addition to critical markers (default: warnings allowed for
  local documents).
- `--output {json,text}`.

### `summarize-dir`

Walk a directory and summarize each eligible text file, then
summarize the per-file summaries.

```sh
python -m wolf.cli summarize-dir --path DIR [options]
```

Key flags:

- `--path DIR` (required).
- `--include PATTERN` / `--exclude PATTERN` — fnmatch globs,
  repeatable. Default include: `*.txt *.md *.rst *.py`.
- `--no-recursive` — top-level only.
- `--max-files N` (default 50), `--max-total-bytes N` (default
  2 MiB), `--max-bytes N` (per file, default 1 MiB).
- `--backend`, `--model`, `--strict-prompt-injection`, `--output`.

### `index-files`

Build the JSON file index (and optionally the embedding index)
under `.wolf/index/`.

```sh
python -m wolf.cli index-files --path DIR [options]
```

Key flags:

- `--path DIR` (required).
- `--no-recursive`, `--include`, `--exclude`, `--max-files`,
  `--max-bytes`.
- `--index-output PATH` — override the default index location
  (`.wolf/index/files.json`).
- `--embed` — also build the embedding index.
- `--embedding-backend {fake,ollama}`, `--embedding-model NAME`,
  `--embedding-ollama-url`, `--embedding-input-bytes N` (default
  4096), `--embedding-index-path PATH`.
- `--output {json,text}`.

### `search-files`

Search the index. Default is substring; `--semantic` switches to
embedding cosine search.

```sh
python -m wolf.cli search-files --query TEXT [options]
```

Key flags:

- `--query TEXT` (required).
- `--index-path PATH` — substring index location.
- `--max-hits N` (default 50).
- `--semantic` — use the embedding index.
- `--embedding-index-path PATH`,
  `--embedding-backend {fake,ollama}`,
  `--embedding-model NAME`, `--embedding-ollama-url`,
  `--allow-non-localhost-ollama`.
- `--output {json,text}`.

Substring hits include `match_count` and `line_number`; semantic
hits include `score`.

### `search-summarize`

Search and summarize in one shot. Substring by default;
`--semantic` switches to embedding retrieval.

```sh
python -m wolf.cli search-summarize --query TEXT [options]
```

Key flags:

- `--query TEXT` (required).
- `--index-path PATH`, `--build-index`, `--path DIR` — substring
  index handling.
- `--limit N` (search cap, default 5), `--max-files N` (summarize
  cap, default 5), `--max-bytes-per-file N`.
- `--chunk-size`, `--max-chunks`, `--no-chunk`,
  `--strict-prompt-injection`.
- `--include-per-file-summary` (JSON only) — attach each hit's
  summary under `result.files[].summary`.
- `--semantic`, `--embedding-index-path`,
  `--embedding-backend {fake,ollama}`, `--embedding-model NAME`,
  `--embedding-ollama-url`.
- `--backend`, `--model`, `--ollama-url`,
  `--allow-non-localhost-ollama`, `--output`.

### `mail-summarize`

Read a local `.eml` or `.mbox` and summarize each message via the
Router pipeline.

```sh
python -m wolf.cli mail-summarize --path PATH [options]
```

Key flags: `--path PATH` (required, `.eml`, `.mbox`, or Maildir
directory), `--limit N` (default 10), `--max-bytes N` (default 1
MiB), `--filter-subject SUBSTR`, `--filter-from SUBSTR`,
`--filter-body-contains SUBSTR` (pre-filter messages; repeatable
to OR-combine within a kind; different kinds AND-combine),
`--filter-since YYYY-MM-DD`, `--filter-until YYYY-MM-DD` (UTC
date window; inclusive end-of-day for until; see `mail.md`),
`--backend {fake,ollama}`, `--model NAME`, `--ollama-url`,
`--allow-non-localhost-ollama`, `--output {json,text}`. JSON output
under `result.summaries[]` carries `has_attachments`,
`attachments_count`, and a per-attachment `attachments` array with
`filename`, `content_type`, `size_bytes`.

### `mail-search`

Substring search across subject / from / body of local mail.

```sh
python -m wolf.cli mail-search --path PATH --query TEXT [options]
```

Key flags: `--path PATH` (required, `.eml`, `.mbox`, or Maildir
directory), `--query TEXT` (required), `--limit N`, `--max-hits N`
(default 10), `--max-bytes N`, `--filter-subject SUBSTR`,
`--filter-from SUBSTR`, `--filter-body-contains SUBSTR` (repeatable
to OR-combine within a kind; AND across kinds; orthogonal to
`--query`), `--filter-since YYYY-MM-DD`,
`--filter-until YYYY-MM-DD` (UTC date window; see `mail.md`),
`--output {json,text}`. Each `result.hits[]` entry
carries `has_attachments` and `attachments_count`.

### `mail-draft`

Generate a reply draft. The user instruction is trusted; the mail
body is treated as `UntrustedText`. No mail is sent.

```sh
python -m wolf.cli mail-draft --path PATH --instruction TEXT [options]
```

Key flags: `--path PATH` (required, `.eml`, `.mbox`, or Maildir
directory), `--instruction TEXT` (required), `--message-index N`
(default 0; applied to filtered list), `--limit N`, `--max-bytes N`,
`--filter-subject SUBSTR`, `--filter-from SUBSTR`,
`--filter-body-contains SUBSTR` (repeatable; same semantics as
mail-search), `--filter-since YYYY-MM-DD`,
`--filter-until YYYY-MM-DD` (UTC date window; see `mail.md`),
`--backend`, `--model`, `--ollama-url`,
`--allow-non-localhost-ollama`, `--output {json,text}`. JSON `result`
carries `source_has_attachments` and `source_attachments_count`.

### `mail-thread`

Group the messages in a local mailbox into conversation threads
using `Message-ID` / `In-Reply-To` / `References` headers plus a
normalized-subject fallback. No LLM call; pure stdlib clustering.

```sh
python -m wolf.cli mail-thread --path PATH [options]
```

Key flags: `--path PATH` (required, `.eml`, `.mbox`, or Maildir
directory), `--limit N` (default 100), `--max-bytes N`,
`--filter-subject SUBSTR`, `--filter-from SUBSTR`,
`--filter-body-contains SUBSTR` (filter messages before threading;
repeatable as in mail-search), `--filter-since YYYY-MM-DD`,
`--filter-until YYYY-MM-DD` (UTC date window; see [mail.md](mail.md)),
`--output {json,text}`. JSON `result.threads[]` carries
`thread_id`, `subject`, `message_count`, `participants`,
`first_date`, `last_date`, and a body-less `messages[]` array
(see `docs/usage/mail.md`).

### `mail-search-summarize`

Combine `mail-search` and `mail-summarize` in one Router-mediated
call. Default mode summarizes each hit individually, then produces
an aggregate; `--threaded` groups the hits into conversations via
`mail-thread` first and summarizes per-thread.

```sh
python -m wolf.cli mail-search-summarize \
    --path PATH --query TEXT [options]
```

Key flags: as `mail-search`, plus `--threaded`,
`--per-message-summary/--no-per-message-summary` (default on),
`--filter-since YYYY-MM-DD`, `--filter-until YYYY-MM-DD`, and the
usual `--backend` / `--model` / `--ollama-url` /
`--allow-non-localhost-ollama` / `--output` flags. The aggregate
step runs under a Router configured with
`allow_warning_injection_findings=True` because its input is
LLM-generated text; critical injection markers still block.

### `summarize-email`

Wrap a string as `UntrustedText(source=email)` and route through
the LLM. Used to exercise the email-source code path; not a real
mail integration.

```sh
python -m wolf.cli summarize-email --text "..." [options]
```

Key flags: `--text TEXT` (required), `--backend`, `--model`,
`--ollama-url`, `--allow-non-localhost-ollama`.

### `gmail-search` (PR #26)

Search Gmail (or the in-memory fake) and return enriched
metadata. No body bytes in the output beyond `snippet`.

```sh
python -m wolf.cli gmail-search --gmail-backend fake --query TEXT
```

Key flags: `--query TEXT` (required), `--limit N` (default 10),
`--gmail-backend {fake,gmail}` (default `fake`),
`--credentials-path PATH` (required for `gmail`),
`--gmail-base-url URL`, `--allow-non-https-gmail`,
`--no-enrich` (skip per-message header lookup),
`--output {json,text}`.

### `gmail-read` (PR #26)

Read one Gmail message and return metadata + a bounded
`body_preview`. The full body is never returned.

```sh
python -m wolf.cli gmail-read --gmail-backend fake --message-id ID
```

Key flags: `--message-id ID` (required), `--gmail-backend`,
`--credentials-path`, `--gmail-base-url`, `--allow-non-https-gmail`,
`--body-preview-bytes N` (default 500), `--output`.

### `gmail-summarize` (PR #26)

Search and/or read Gmail messages and summarize each via the
Router pipeline (mail-strict). No send.

```sh
python -m wolf.cli gmail-summarize \
    --gmail-backend fake --query TEXT --llm-backend fake
```

Key flags: `--query TEXT` OR `--message-id ID`, `--limit N`,
Gmail flags as above, plus the LLM flags
`--llm-backend {fake,ollama}` (default `fake`), `--model NAME`
(required for `ollama`), `--ollama-url`,
`--allow-non-localhost-ollama`, `--output`.

### `gmail-draft` (PR #26)

Read one Gmail message, draft a reply via the LLM, and create
the draft on Gmail (or the fake). NEVER sends.

```sh
python -m wolf.cli gmail-draft \
    --gmail-backend fake --message-id ID \
    --instruction "..." --llm-backend fake
```

Key flags: `--message-id ID` (required), `--instruction TEXT`
(required, treated as trusted), Gmail flags as above, LLM flags
as above, `--body-preview-bytes N` (default 500), `--output`.
JSON `result` carries `draft_id`, `message_id`, `thread_id`,
`subject_suggestion`, `body_preview`, `body_truncated`. See
`docs/usage/gmail.md` for the full shape.

### `check-path`

Run `ProjectBoundaryGuard` + `SensitivePathGuard` on a path. Does
not read the file.

```sh
python -m wolf.cli check-path --path PATH
```

### `robot-preflight`

Run `RobotPreflight` against a `FakeRobotTransport` healthy state.
Dry-run only; never invokes `execute_motion`.

```sh
python -m wolf.cli robot-preflight
```

## JSON output schema (common fields)

Every JSON response carries:

- `allowed` (bool)
- `executed` (bool)
- `requires_confirmation` (bool)
- `stage` (one of `project_boundary`, `sensitive_path`, `policy`,
  `robot_preflight`, `prompt_injection`, `provider`, `file_read`,
  `search`, `audit`, `complete`)
- `reason` (string)
- `provider_called` (bool)
- `audit_event_id` (string or null)
- `failed_checks` (list of strings)
- `warnings` (list of strings)
- `result` (command-specific shape; only on success)

## `text` output mode

Each command's `text` mode is documented in the per-command usage
docs (`docs/usage/summarize_file.md`,
`docs/usage/summarize_dir.md`, `docs/usage/search_files.md`,
`docs/usage/search_summarize.md`,
`docs/usage/semantic_search.md`). In every case:

- stdout carries only the human-readable result (summary text, hit
  table, or index summary).
- stderr carries a single-line warning count and any safety / file
  read messages.
- Raw file bodies never appear in stdout or stderr.
