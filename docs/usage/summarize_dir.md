# Summarize a local directory

`wolf summarize-dir` walks a directory under `--project-root`,
summarizes each eligible text file, and produces one consolidated
summary across the per-file summaries.

## What it walks

- Default include patterns: `*.txt`, `*.md`, `*.rst`, `*.py`.
- `--include PATTERN` (repeatable) overrides the default.
- `--exclude PATTERN` (repeatable) filters out matches.
- `--no-recursive` limits the walk to the immediate directory.

Per-file behavior:

- The path is gated through `ProjectBoundaryGuard` and
  `SensitivePathGuard`. `secrets/`, `.env`, `credentials/`, `tokens/`,
  `private/` etc. are skipped with a warning.
- Binary files (NUL bytes, high non-text ratio) are skipped with a
  warning.
- Files larger than `--max-bytes` (default 1 MiB) are skipped with a
  warning.
- Once `--max-files` (default 50) eligible files are accepted, the
  remaining candidates are skipped.
- Once cumulative reads exceed `--max-total-bytes` (default 2 MiB),
  further files are skipped.
- Files that hit a critical prompt-injection marker are skipped.
- Files with only warning-level markers are summarized (the
  `--strict-prompt-injection` flag flips this to skip too).

The final aggregated summary is produced by routing the concatenated
per-file summaries through the selected LLM backend (Fake by default;
Ollama opt-in). Exit code 0 on success, 2 if no eligible files were
found or if the final aggregation failed.

## Examples

```sh
# Default walk of ./docs with the fake LLM.
PYTHONPATH=src python3 -m wolf.cli summarize-dir --path ./docs --backend fake

# Plain-text final summary on stdout, warnings on stderr.
PYTHONPATH=src python3 -m wolf.cli summarize-dir --path ./docs --backend fake --output text

# Only top-level markdown, no recursion.
PYTHONPATH=src python3 -m wolf.cli summarize-dir \
    --path ./docs --include "*.md" --no-recursive --backend fake

# Use a real local Ollama model.
PYTHONPATH=src python3 -m wolf.cli summarize-dir \
    --path ./docs --backend ollama --model llama3.1
```

## Privacy and safety notes

- Per-file body bytes are never echoed to stdout / stderr. The
  warnings carry only relative paths and skip reasons (e.g.,
  `"dir: skipped secrets/key.pem (stage=sensitive_path)"`).
- The aggregated summary may contain content derived from the file
  bodies (that is the point of a summary), but never verbatim file
  contents because each file is itself summarized first.
- Default `network_mode: none` Docker container reaches no network.
  The Ollama backend requires either a host Ollama or an opt-in
  compose override.

## Limitations

- No streaming.
- No embeddings or vector search.
- No watch mode.
- No PDF / OCR.
- For per-file chunking of very large files, use `summarize-file`
  directly with `--chunk-size`; the dir walker does not currently
  chunk individual files.
