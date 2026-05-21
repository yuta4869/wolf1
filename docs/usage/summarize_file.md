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
   `local_document`) and routed through `PromptInjectionShield`.
5. The Router calls the selected LLM backend (`fake` by default,
   `ollama` opt-in) and returns a `RouterDecision` JSON to stdout.

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
```

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
- A file containing prompt-injection markers (e.g., `"ignore previous
  instructions"`) is blocked at the `prompt_injection` stage and the
  LLM is NOT called.
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
