# File index and keyword search

PR #17 adds two CLI subcommands that work together:

1. `wolf index-files --path <dir>` walks a project-local directory and
   writes a small JSON file index to `.wolf/index/files.json`
   (configurable via `--index-output`).
2. `wolf search-files --query <text>` loads that index, runs a
   case-insensitive substring search, and returns matching files with
   snippets anchored around the match.

This is a deliberately small first step. No embeddings, no vector DB,
no semantic search — just metadata + keyword. Embeddings live in a
later PR.

## What the index stores per file

- `path` (relative to `--project-root`)
- `size` (full byte size)
- `mtime` (Unix timestamp)
- `extension`
- `snippet` (first ~160 bytes of decoded text; bounded preview)
- `encoding`

The index does **not** store the full body. A leaked index file is
metadata, not a corpus. Search results re-open the file at query time
to produce the per-match snippet.

## Examples

```sh
# Index the docs/ directory with defaults (*.txt, *.md, *.rst, *.py).
PYTHONPATH=src python3 -m wolf.cli index-files --path ./docs

# Limit to markdown only, non-recursive.
PYTHONPATH=src python3 -m wolf.cli index-files \
    --path ./docs --include "*.md" --no-recursive

# Search the index for a substring.
PYTHONPATH=src python3 -m wolf.cli search-files --query "summarize"

# Human-readable output.
PYTHONPATH=src python3 -m wolf.cli search-files \
    --query "summarize" --output text
```

## Safety

- The directory path is gated through `ProjectBoundaryGuard` and
  `SensitivePathGuard` before the walk begins. Pointing
  `index-files` at `secrets/` fails at `stage=sensitive_path`.
- Each file in the walk is re-checked against both guards. `.env`,
  `secrets/*`, etc. are skipped with a warning recorded in the
  index's `skipped` field.
- Binary files (NUL bytes / high non-text ratio) are skipped.
- The default per-file size limit is 1 MiB.
- The default index location is `.wolf/index/files.json`. The CLI
  refuses an `--index-output` outside `--project-root`.
- `.wolf/` is gitignored so an index file does not accidentally land
  in version control.

## Flags

### `index-files`

| flag | default | description |
| --- | --- | --- |
| `--path PATH` | required | directory to index |
| `--no-recursive` | off | only top-level files |
| `--include PATTERN` | `*.txt *.md *.rst *.py` | fnmatch, repeatable |
| `--exclude PATTERN` | — | fnmatch, repeatable |
| `--max-files N` | `500` | walk cap |
| `--max-bytes N` | `1048576` | per-file size cap |
| `--index-output PATH` | `.wolf/index/files.json` | where to write |
| `--output json\|text` | `json` | output format |

### `search-files`

| flag | default | description |
| --- | --- | --- |
| `--query TEXT` | required | substring to find (case-insensitive) |
| `--index-path PATH` | `.wolf/index/files.json` | which index to load |
| `--max-hits N` | `50` | hit cap |
| `--output json\|text` | `json` | output format |

## Exit codes

- `0` — index built, or search returned at least one hit.
- `2` — boundary / sensitive denial, missing directory, no hits, or
  missing / malformed index file.
- `1` — internal error.

## Limitations

- Substring match is exact (case-insensitive). Stemming / fuzzy /
  semantic matching is out of scope for this PR.
- The index does not auto-refresh. Re-run `index-files` when files
  change. `mtime` is recorded so a future incremental mode can use it.
- Search re-reads each candidate file from disk; for very large
  indexes this is O(N) over the hit candidates. Embedding-based
  retrieval will replace this in a future PR.
- No PDF, OCR, image, or audio content is indexed.
