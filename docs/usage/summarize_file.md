# Summarize a local file

`wolf summarize-file` reads a text file under `--project-root` and
summarizes it via the selected LLM backend. The command is read-only:
no file deletion, no move, no overwrite. The full safety pipeline runs:

1. `ProjectBoundaryGuard` rejects anything outside `--project-root`,
   including paths reached via `..` or symlinks.
2. `SensitivePathGuard` rejects `.env`, `.env.*`, `secrets/**`,
   `credentials/**`, `tokens/**`, `private/**`, and the home-anchored
   `~/.ssh`, `~/.aws`, `~/.config/gcloud`.
3. The file content is read with `read_text_file`, which enforces a
   default 1 MiB size limit, rejects files with NUL bytes or a high
   non-text byte ratio, and requires valid UTF-8 (configurable).
4. The decoded text is wrapped in `UntrustedText` (source kind
   `local_document`) and routed through `PromptInjectionShield`. For
   `summarize-file` the default is to surface warning-level markers
   (e.g., the words "robot" or "send email" in a project doc) as
   warnings without blocking, since most project documents legitimately
   mention these words. Critical markers (e.g., "ignore previous
   instructions") still block. Pass `--strict-prompt-injection` to
   block on warnings too. `summarize-email` retains the stricter
   default — emails are far more likely to be actual injection vectors.
5. The Router calls the selected LLM backend (`fake` by default,
   `ollama` opt-in) and returns a `RouterDecision` JSON to stdout, or a
   plain-text summary if `--output text` is set.

The raw file content never appears in stderr or in the
`RouterDecision` JSON fields `failed_checks` / `warnings` / `stage` /
`reason`. (The `result` field carries the LLM's summary, which is a
function of the input but is not a verbatim echo of the file body.)

## Examples

```sh
# Fake backend (in-process, no network):
PYTHONPATH=src python3 -m wolf.cli summarize-file \
    --path ./notes/meeting.txt \
    --backend fake

# Ollama backend (requires `ollama serve` on 127.0.0.1:11434 and a
# pre-pulled model):
PYTHONPATH=src python3 -m wolf.cli summarize-file \
    --path ./notes/meeting.txt \
    --backend ollama \
    --model llama3.1

# Tighter size limit for a fast smoke:
PYTHONPATH=src python3 -m wolf.cli summarize-file \
    --path ./notes/meeting.txt \
    --max-bytes 65536

# Plain-text output: just the summary on stdout, errors on stderr.
PYTHONPATH=src python3 -m wolf.cli summarize-file \
    --path ./notes/meeting.txt \
    --output text

# Strict prompt-injection mode: warning markers block in addition to
# critical markers. Use for files of unknown provenance (e.g., a doc a
# colleague handed you).
PYTHONPATH=src python3 -m wolf.cli summarize-file \
    --path ./untrusted/notes.txt \
    --strict-prompt-injection
```

## Flags

| flag | default | description |
| --- | --- | --- |
| `--path PATH` | required | file to read |
| `--backend fake|ollama` | `fake` | LLM backend |
| `--model NAME` | required for ollama | Ollama model name |
| `--ollama-url URL` | `http://127.0.0.1:11434` | Ollama server |
| `--allow-non-localhost-ollama` | off | opt-in for non-localhost URLs |
| `--max-bytes N` | `1048576` (1 MiB) | refuse to read files above this |
| `--no-chunk` | off | disable automatic chunking |
| `--chunk-size N` | `32768` | bytes per chunk when chunking |
| `--max-chunks N` | `32` | drop remaining chunks past this count (warning surfaces) |
| `--strict-prompt-injection` | off | block on warning markers too |
| `--output json|text` | `json` | output format on stdout |

## Chunking

When the file exceeds `--chunk-size`, `summarize-file` splits the body
on paragraph / line / hard-byte boundaries, summarizes each chunk
through the Router (so each chunk goes through the prompt-injection
scan), then summarizes the concatenated per-chunk summaries to produce
the final output. `--no-chunk` skips this and sends the whole file in
one LLM call (which may fail on large inputs). Truncation past
`--max-chunks` is reported in the `warnings` field.

## Exit codes

- `0` — file read OK, prompt-injection scan passed, LLM returned a
  summary.
- `2` — denied at one of the safety stages, or the file could not be
  read safely, or the LLM backend failed. Stdout JSON identifies which
  stage via the `stage` field.
- `1` — internal error (unexpected exception). Rare.

## Stages you may see in the JSON

- `project_boundary` — path is outside `--project-root`.
- `sensitive_path` — path matches a sensitive pattern (e.g., `.env`,
  `secrets/`).
- `policy` — unsupported action / risk level.
- `file_read` — file does not exist, is not a regular file, exceeds
  `--max-bytes`, looks binary, or cannot be decoded.
- `prompt_injection` — file content contained an injection marker.
- `provider` — the LLM backend failed (e.g., Ollama unreachable).
- `complete` — all stages passed; the `result` field has the summary.

## Privacy and safety notes

- The file is opened in binary mode and never logged. Audit records
  carry the byte size, encoding, and source path — never the bytes.
- A file containing **critical** prompt-injection markers (e.g.,
  `"ignore previous instructions"`) is blocked at the `prompt_injection`
  stage and the LLM is NOT called. This is unchanged by
  `--strict-prompt-injection`.
- **Warning-level** markers (e.g., literal mentions of `robot`,
  `send email`, `sudo`) are surfaced in the `warnings` field by default
  but do NOT block. Pass `--strict-prompt-injection` to convert them
  back to blocking, matching the PR #14 behavior and the
  `summarize-email` default.
- `--output text` is convenient for piping, but failure messages still
  go to stderr and never include the file body. A warning count line
  may appear on stderr when warnings are present; the actual marker
  list lives in the `--output json` payload.
- The Ollama backend refuses non-localhost URLs unless
  `--allow-non-localhost-ollama` is set explicitly. The Fake backend
  has no network surface.
- Symlinks under the project root that point outside are caught by
  `ProjectBoundaryGuard.check` via `os.path.realpath`.

## When `summarize-file` is the wrong tool

- The file is a PDF, image, audio clip, or other non-text format. This
  command requires plain text and rejects binaries.
- The file is larger than 1 MiB and you do not want to truncate. Raise
  `--max-bytes` or split the file first.
- You need to summarize across multiple files. Run the command per
  file; an aggregated mode is not implemented in this PR.

## Docker

Inside the default `docker compose run --rm wolf` container, the
`summarize-file` subcommand works with `--backend fake` (no network
required). The `--backend ollama` path will fail because the container
runs with `network_mode: none`; this is the intended posture for the
default test image.
