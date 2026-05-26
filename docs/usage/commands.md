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

Key flags: `--path PATH` (required, `.eml` or `.mbox`), `--limit N`
(default 10), `--max-bytes N` (default 1 MiB),
`--backend {fake,ollama}`, `--model NAME`, `--ollama-url`,
`--allow-non-localhost-ollama`, `--output {json,text}`.

### `mail-search`

Substring search across subject / from / body of local mail.

```sh
python -m wolf.cli mail-search --path PATH --query TEXT [options]
```

Key flags: `--path PATH` (required), `--query TEXT` (required),
`--limit N`, `--max-hits N` (default 10), `--max-bytes N`,
`--output {json,text}`.

### `mail-draft`

Generate a reply draft. The user instruction is trusted; the mail
body is treated as `UntrustedText`. No mail is sent.

```sh
python -m wolf.cli mail-draft --path PATH --instruction TEXT [options]
```

Key flags: `--path PATH` (required), `--instruction TEXT` (required),
`--message-index N` (default 0, only used for `.mbox`),
`--limit N`, `--max-bytes N`, `--backend`, `--model`, `--ollama-url`,
`--allow-non-localhost-ollama`, `--output {json,text}`.

### `summarize-email`

Wrap a string as `UntrustedText(source=email)` and route through
the LLM. Used to exercise the email-source code path; not a real
mail integration.

```sh
python -m wolf.cli summarize-email --text "..." [options]
```

Key flags: `--text TEXT` (required), `--backend`, `--model`,
`--ollama-url`, `--allow-non-localhost-ollama`.

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
