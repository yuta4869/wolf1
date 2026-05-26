# wolf

`wolf` is a fully local, privacy-first command-line companion for
project-local text files. It reads files from a project directory,
runs them through a small safety pipeline, and uses a local LLM
(`fake` by default, `ollama` opt-in) to summarize and search them.

This README documents the **v0.1** release scope. It is what is
shipped after PR #1 through PR #21.

## v0.2 in progress

- `mail-summarize` / `mail-search` / `mail-draft` for local `.eml`
  and `.mbox` files (PR #22). Read-only. No Gmail / IMAP / SMTP /
  send. See [`docs/usage/mail.md`](docs/usage/mail.md).

## What v0.1 covers

- Read text files under `--project-root` with `ProjectBoundaryGuard`
  and `SensitivePathGuard` keeping you inside the project and out of
  `secrets/`, `.env`, `credentials/`, `tokens/`, `private/`,
  `~/.ssh`, `~/.aws`, `~/.config/gcloud`.
- Summarize a single file (`summarize-file`), an entire directory
  (`summarize-dir`), or a keyword query (`search-summarize`).
- Build a JSON file index (`index-files`) and search it with either
  a substring matcher (`search-files`) or local Ollama embeddings
  (`search-files --semantic` / `search-summarize --semantic`).
- Use either the in-process `fake` LLM backend (for smoke / tests)
  or a locally-running Ollama daemon (`--backend ollama
  --model <name>`).
- Run inside Docker with `network_mode: none` by default. The
  default `docker compose run --rm wolf` invocation never reaches
  the network.

## What v0.1 deliberately does not do

- No Gmail, IMAP, or calendar integration.
- No PDF, OCR, image, audio, or video ingest.
- No real robot motion control. The `robot-preflight` command is a
  dry-run only and never invokes `RobotTransport.execute_motion`.
- No cloud LLM (OpenAI / Anthropic / Gemini etc.).
- No GUI / TUI / web UI.
- No vector database integration (Qdrant / Chroma / FAISS). The
  embedding index is a JSON file with pure-Python cosine.
- No background daemon, no file watcher, no incremental index
  refresh.

## Install

`wolf` ships with no third-party runtime dependencies; the optional
`[dev]` extras are pytest / ruff / mypy and are not required to run
the CLI itself.

```sh
git clone https://github.com/yuta4869/wolf1.git
cd wolf1
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Run the test suite

Host:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
```

Docker (the canonical verification environment):

```sh
docker compose run --rm wolf
```

Both paths should report ~562 passing tests with the optional Ollama
integration smokes skipped.

## Quick commands

Replace `./docs` with whatever directory in your project you want to
process. All paths must resolve inside `--project-root` (default:
current working directory).

```sh
# Summarize one file (fake LLM, no network).
python -m wolf.cli summarize-file --path ./notes/meeting.txt

# Summarize an entire directory.
python -m wolf.cli summarize-dir --path ./docs

# Build a metadata index for keyword search.
python -m wolf.cli index-files --path ./docs

# Substring search the index.
python -m wolf.cli search-files --query "summarize"

# Search and summarize in one shot.
python -m wolf.cli search-summarize --query "summarize"

# Build both the metadata and embedding indexes.
python -m wolf.cli index-files --path ./docs \
    --embed --embedding-backend ollama --embedding-model nomic-embed-text

# Semantic search via the embedding index.
python -m wolf.cli search-files --query "file summarization" \
    --semantic --embedding-backend ollama --embedding-model nomic-embed-text

# Semantic search-and-summarize end to end.
python -m wolf.cli search-summarize --query "file summarization" \
    --semantic --embedding-backend ollama --embedding-model nomic-embed-text \
    --backend ollama --model llama3.1
```

For full flag references see [`docs/usage/commands.md`](docs/usage/commands.md).
For a step-by-step first run see [`docs/usage/quickstart.md`](docs/usage/quickstart.md).
For semantic search specifically see [`docs/usage/semantic_search.md`](docs/usage/semantic_search.md).

## Ollama setup

`wolf` does not bundle, install, or auto-pull any model. You bring
the Ollama daemon and the models.

1. Install Ollama from <https://ollama.com>. The macOS app starts a
   daemon on `http://127.0.0.1:11434`.
2. Pull the LLM and (optionally) the embedding model you want to use:

    ```sh
    ollama pull llama3.1
    ollama pull nomic-embed-text
    ```

    The LLM model is for `--backend ollama --model <llm>`; the
    embedding model is for `--embed` /
    `--embedding-model <embed>`. They are different model artefacts
    and should be passed independently.

3. Verify Ollama is reachable:

    ```sh
    curl -fsS http://127.0.0.1:11434/api/tags
    ```

For more on the Ollama backend see [`docs/setup/ollama.md`](docs/setup/ollama.md).

## Privacy posture

- Files are opened in binary mode and read once per command. Bodies
  are never logged. Audit records carry byte size, encoding, source
  path, and an action stage — never body bytes.
- The Ollama LLM and embedding adapters refuse non-localhost URLs
  unless `--allow-non-localhost-ollama` is set explicitly.
- The default Docker container runs with `network_mode: none`. The
  unit test image cannot reach the network.
- The embedding index is bounded: the first 4 KiB of each file is
  embedded by default (configurable via
  `--embedding-input-bytes`). Full bodies are not stored.
- The runtime caches under `.wolf/` (file index, embedding index,
  audit log) are excluded by `.gitignore`.
- Every file path passes through `ProjectBoundaryGuard` (must
  resolve inside `--project-root`, symlinks included) and
  `SensitivePathGuard` (must not match the secrets / credentials /
  ssh / aws / gcloud patterns).

## v0.1 known limitations

See [`docs/usage/known_limitations.md`](docs/usage/known_limitations.md)
for the full list. Key items:

- Substring search is exact substring (case-insensitive). No
  stemming, fuzzy, or regex matching.
- Semantic search requires a manually built embedding index; there
  is no automatic refresh and no detection of embedding-model
  mismatch.
- For very large repositories the pure-Python cosine becomes the
  bottleneck before the embedder does.
- No chunked embeddings; the first 4 KiB of each file is what
  gets vectorized.
- PDF / OCR / images / audio are not ingested. Markdown, plain
  text, RST, and Python source are the supported file kinds out of
  the box; `--include` / `--exclude` lets you broaden or narrow
  this.
- The robot subsystem is intentionally dry-run only. There is no
  `execute_motion` invocation anywhere in the CLI code path.

## Repository layout

```
src/wolf/
  adapters/         LLM, embedding, robot transport protocols + Ollama
  core/             audit, policy, errors, types
  fakes/            in-process test doubles
  files/            read_text, chunking, index, search, semantic_search,
                    vector_index
  orchestrator/     Router (single entry point for every action)
  safety/           ProjectBoundaryGuard, SensitivePathGuard,
                    RobotPreflight, PromptInjectionShield
  cli.py            argparse front-end (summarize-file, summarize-dir,
                    search-files, search-summarize, index-files,
                    check-path, robot-preflight, summarize-email)
tests/              ~562 unit and integration tests
docs/setup/         Docker, Ollama, macOS, WSL2 setup notes
docs/usage/         per-command usage docs (this PR adds quickstart /
                    commands / known_limitations)
docs/dev/           PR workflow, CI first-run, attribution policy
scripts/            attribution guard + opt-in git hooks
.github/workflows/  attribution CI guard
docker-compose*.yml default (CPU, network none), GPU override, macOS override
```

## Project policies

- Branches and PRs: see [`docs/dev/pr_workflow.md`](docs/dev/pr_workflow.md).
- No AI attribution in commits or PR bodies (enforced by
  `scripts/check-no-ai-attribution.sh` and the GitHub Actions
  workflow): see [`docs/dev/pr_workflow.md`](docs/dev/pr_workflow.md).
- Safety architecture and product specification:
  [`CLAUDE.md`](CLAUDE.md).
