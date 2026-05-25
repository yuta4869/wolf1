# Search and summarize in one command

`wolf search-summarize` chains the PR #17 keyword index with the PR #16
chunked summarize pipeline so you can answer "what does the project say
about X?" in a single invocation.

## What it does

1. Loads the JSON file index (default
   `.wolf/index/files.json`). If `--build-index` is set, it builds the
   index first.
2. Runs the keyword search and takes up to `--limit` hits.
3. For each hit (up to `--max-files`), re-reads the file, chunks it if
   it exceeds `--chunk-size`, summarizes each chunk via the Router,
   then summarizes the per-file chunk summaries.
4. Concatenates the per-file summaries and routes them through one
   final `LLM_SUMMARIZE` for a single aggregate summary.

Every step goes through the Router, so the `ProjectBoundaryGuard`,
`SensitivePathGuard`, and prompt-injection scan run on every piece of
content before it reaches the LLM. The audit log records one event per
sub-action.

## Examples

```sh
# Index first, then ask.
PYTHONPATH=src python3 -m wolf.cli index-files --path ./docs
PYTHONPATH=src python3 -m wolf.cli search-summarize \
    --query "summarize" --output text

# Build the index implicitly.
PYTHONPATH=src python3 -m wolf.cli search-summarize \
    --query "summarize" --build-index --path ./docs

# Use a real local Ollama model.
PYTHONPATH=src python3 -m wolf.cli search-summarize \
    --query "summarize" --backend ollama --model llama3.1
```

## Output

`--output json` (default) returns:

```json
{
  "allowed": true,
  "stage": "complete",
  "warnings": ["..."],
  "result": {
    "query": "summarize",
    "hit_count": 3,
    "summarized_count": 3,
    "skipped_count": 0,
    "files": [
      {"path": "docs/a.md", "match_count": 2, "line_number": 5, "summary_length": 412},
      ...
    ],
    "summary": "<aggregate summary text>"
  }
}
```

Pass `--include-per-file-summary` to attach each hit's individual LLM
summary under `result.files[].summary`:

```json
{
  "result": {
    "files": [
      {
        "path": "docs/a.md",
        "match_count": 2,
        "line_number": 5,
        "summary_length": 412,
        "summary": "<per-file summary text>"
      }
    ],
    "summary": "<aggregate summary text>"
  }
}
```

The flag is JSON-only. `--output text` ignores it and still writes only
the aggregate summary to stdout. Per-file summaries are LLM output (not
raw file bytes), so they are safe to include in an automation pipeline;
the raw body remains absent from every other field.

`--output text` writes only the aggregate summary to stdout and prints
a single line on stderr with the warning count if any.

## Exit codes

- `0` — at least one matching file was summarized and the aggregate
  succeeded.
- `2` — no hits, all hits were skipped (file_read / boundary /
  sensitive / injection), or the aggregate step failed.
- `1` — internal error.

## Privacy and safety

- Raw file content does not appear in stdout or stderr. The `files`
  records carry only path, match count, line number, and summary
  length.
- Files matching `secrets/`, `.env`, `credentials/`, `tokens/`,
  `private/`, or `~/.ssh|.aws|.config/gcloud` are skipped at the
  per-hit gate even if they were in the index.
- Critical prompt-injection markers in a file body still block that
  file's summary; the other hits continue to be processed.
- Warnings (skip reasons, chunking truncation, per-file injection
  notices) are surfaced in the JSON `warnings` field and as a stderr
  count in text mode.

## Limitations

- Keyword search is exact substring (case-insensitive). Semantic /
  embedding-based search is out of scope for this PR.
- The index is not auto-refreshed. Re-run `index-files` or use
  `--build-index` when files change.
- `--limit` and `--max-files` are independent; `--limit` caps the
  search, `--max-files` caps the summarize step. They default to the
  same value (5).
- Each hit's chunking is the same as `summarize-file` (see PR #16);
  the dir walker logic from `summarize-dir` is not reused here.
