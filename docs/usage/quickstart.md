# Quickstart

This walks through the seven representative flows for v0.1. Each step
is independent; you can stop at any point.

The examples assume:

- you are in the cloned repository root (`/Users/you/wolf` or similar),
- `python3` is on PATH,
- you have run `pip install -e ".[dev]"` (or you prefix every command
  with `PYTHONPATH=src`; the examples below use that form for clarity).

## 1. Fake LLM summarize

No Ollama required. Verifies the safety pipeline + Router + Fake LLM
all the way through.

```sh
PYTHONPATH=src python3 -m wolf.cli summarize-file \
    --path ./README.md \
    --backend fake \
    --output text
```

Expected: a `SUMMARY(<n>ch): ...` line on stdout, exit code 0.

## 2. Ollama LLM summarize

Requires `ollama serve` running locally and `llama3.1` (or any other
chat model) pulled.

```sh
PYTHONPATH=src python3 -m wolf.cli summarize-file \
    --path ./README.md \
    --backend ollama \
    --model llama3.1 \
    --output text
```

If Ollama is not running, the CLI exits 2 with
`stage=provider reason="provider failed: ollama:summarize: network error"`
and never echoes the file body.

## 3. Build a substring index

```sh
PYTHONPATH=src python3 -m wolf.cli index-files --path ./docs --output text
```

Writes `.wolf/index/files.json` with one entry per file. The full
body is not stored; each entry carries path, size, mtime, extension,
encoding, and a bounded snippet.

## 4. Substring search

```sh
PYTHONPATH=src python3 -m wolf.cli search-files \
    --query "summarize" \
    --output text
```

Case-insensitive substring match. Re-reads each candidate file at
query time to produce a per-match snippet. Exit 2 when there are
no hits.

## 5. Search and summarize

```sh
PYTHONPATH=src python3 -m wolf.cli search-summarize \
    --query "summarize" \
    --output text
```

Finds matching files, summarizes each via the Router pipeline, then
summarizes the per-file summaries into one aggregate. Pass
`--include-per-file-summary` to attach the per-file summaries to the
JSON output.

## 6. Fake embedding semantic smoke

No Ollama required; uses the deterministic `FakeEmbeddingAdapter`. Not
a real semantic search — but it exercises the full embedding /
vector-index / cosine-search code path.

```sh
PYTHONPATH=src python3 -m wolf.cli index-files \
    --path ./docs \
    --embed --embedding-backend fake --embedding-model fake-embed \
    --output text

PYTHONPATH=src python3 -m wolf.cli search-files \
    --query "file summarization" \
    --semantic --embedding-backend fake --embedding-model fake-embed \
    --output text
```

Expected: ranked `<path> (score=0.xxxx): <snippet>` lines.

## 7. Ollama embedding semantic search

Requires Ollama running with `nomic-embed-text` (or another embedding
model) pulled.

```sh
PYTHONPATH=src python3 -m wolf.cli index-files \
    --path ./docs \
    --embed --embedding-backend ollama --embedding-model nomic-embed-text \
    --output text

PYTHONPATH=src python3 -m wolf.cli search-summarize \
    --query "file summarization" \
    --semantic --embedding-backend ollama --embedding-model nomic-embed-text \
    --backend ollama --model llama3.1 \
    --output text
```

This combines a real local embedding model for retrieval with a real
local LLM for summarization. Wall-clock latency depends on how many
hits the search returns and how large each file is.

## Where things land on disk

- `.wolf/index/files.json` — substring metadata index
- `.wolf/index/embeddings.json` — embedding index
- `var/audit/audit.jsonl` — append-only audit log (one event per
  Router action; never includes raw file bodies)

Both `.wolf/` and `var/` are excluded by `.gitignore`.
