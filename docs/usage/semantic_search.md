# Semantic search (PR #20)

PR #20 adds local embedding-based search alongside the substring search
introduced in PR #17. There is no external vector DB, no numpy
dependency, no cloud embeddings. The vector index is a JSON file under
`.wolf/index/embeddings.json`, and cosine similarity is computed in
pure Python.

## Building the embedding index

The flag `--embed` on `index-files` opts in to embedding generation
alongside the regular metadata index. The embedding backend defaults
to `ollama`; pass `fake` for tests / smoke without a live model.

```sh
# Real local Ollama (recommended).
PYTHONPATH=src python3 -m wolf.cli index-files \
    --path ./docs \
    --embed --embedding-backend ollama --embedding-model nomic-embed-text

# Test / smoke without Ollama.
PYTHONPATH=src python3 -m wolf.cli index-files \
    --path ./docs \
    --embed --embedding-backend fake --embedding-model fake-embed
```

What gets embedded: the first `--embedding-input-bytes` bytes (default
4 KiB) of each file. We deliberately do NOT embed full bodies — this
keeps the index small and stays well within typical embedding-model
context windows.

The resulting index includes path, size, mtime, extension, snippet,
encoding, and the embedding vector. The full file body is not stored.

## Searching

`search-files --semantic` switches retrieval from substring to vector
cosine. `--embedding-model` must match the one used during indexing.

```sh
PYTHONPATH=src python3 -m wolf.cli search-files \
    --query "file summarization" \
    --semantic --embedding-backend ollama --embedding-model nomic-embed-text
```

JSON shape:

```json
{
  "result": {
    "query": "file summarization",
    "mode": "semantic",
    "hits": [
      {"path": "docs/usage/summarize_file.md", "score": 0.78, "snippet": "..."}
    ]
  }
}
```

Substring search remains the default (no `--semantic` flag) and is
unchanged.

## Search and summarize, semantic mode

`search-summarize --semantic` runs the embedding search and then
summarizes the top hits via the same chunked Router pipeline used by
`summarize-file`.

```sh
PYTHONPATH=src python3 -m wolf.cli search-summarize \
    --query "file summarization" \
    --semantic --embedding-backend ollama --embedding-model nomic-embed-text \
    --backend ollama --model llama3.1 \
    --output text
```

`result.files[]` entries carry `path`, `score`, `summary_length`, and
(with `--include-per-file-summary`) `summary`. `result.summary` is the
aggregate.

## Privacy / safety

- The embedding index never stores full file bodies — only metadata
  plus the embedding vector and a bounded snippet.
- Each candidate path is re-validated through `ProjectBoundaryGuard`
  and `SensitivePathGuard` at search time, so a file moved into
  `secrets/` after indexing is silently dropped.
- The Ollama embedding adapter refuses non-localhost URLs unless
  `--allow-non-localhost-ollama` is set explicitly.
- `AdapterError` from the embedder is presented as a safe provider
  failure on stderr (text mode) or as a `stage=provider` JSON payload;
  the query text is not echoed.

## Limitations

- One model per index (the index stores the model name). Re-run
  `index-files --embed --embedding-model ...` to switch models.
- No incremental refresh; the index is rebuilt on every run.
- No metadata filters yet (file type, mtime range). Combine with
  `--include` / `--exclude` at index time instead.
- Cosine similarity is computed in pure Python; for repositories with
  many thousands of files this becomes the bottleneck before
  embedding generation does. A numpy backend can replace it later
  without changing the API.
- The fake embedding adapter is for tests only; it does not produce
  semantically meaningful vectors and should not be used for real
  retrieval.
