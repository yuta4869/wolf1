# Local Ollama LLM backend

`src/wolf/adapters/ollama.py` provides `OllamaLLMAdapter`, which implements
the `LLMAdapter` Protocol against a locally-running Ollama HTTP server. The
adapter is opt-in: the default CLI backend is still `FakeLLM`, and Router
is unchanged in its handling of LLM calls.

This is the first step toward replacing the FakeLLM in everyday use without
sending any data to a cloud LLM provider.

## What this is and is not

Wolf uses Ollama because it runs models entirely on the user's local
machine. The adapter posts JSON to `/api/generate` and reads the
`response` field. It does NOT:

- install Ollama or pull models
- stream tokens (`stream` is hard-coded to `false`)
- call any cloud LLM provider
- bypass the Router's safety pipeline
- accept non-localhost URLs unless the caller passes
  `allow_non_localhost=True`

## Prerequisites (installed by the user)

1. Install Ollama from <https://ollama.com>. On macOS the `.app` runs a
   local daemon on `http://127.0.0.1:11434`. On Linux / WSL, follow the
   distribution's instructions.
2. Pull at least one model before invoking the CLI:

    ```sh
    ollama pull llama3.1
    ```

   Wolf does NOT pull models automatically.

3. Verify the daemon is reachable:

    ```sh
    curl -fsS http://127.0.0.1:11434/api/tags
    ```

   The CLI does NOT use `curl`; this is just a sanity check during setup.

## CLI usage

The `summarize-email` subcommand accepts a `--backend` flag. The default
is `fake`; pass `ollama` plus `--model` to route through the adapter.

```sh
# Fake backend (default):
PYTHONPATH=src python3 -m wolf.cli summarize-email --text "会議メモを要約して"

# Ollama backend:
PYTHONPATH=src python3 -m wolf.cli \
    summarize-email \
    --backend ollama \
    --model llama3.1 \
    --text "会議メモを要約して"

# Custom Ollama URL (must be localhost unless allow-non-localhost is set):
PYTHONPATH=src python3 -m wolf.cli \
    summarize-email \
    --backend ollama \
    --model llama3.1 \
    --ollama-url http://127.0.0.1:11434 \
    --text "..."
```

Exit codes:

- `0` — Router allowed the action and the LLM returned a summary.
- `2` — Router denied (sensitive path, prompt injection, missing model,
  external URL refused, or Ollama unreachable).
- `1` — internal error (rare; usually a bug, not a config problem).

When Ollama is unreachable, the CLI prints a JSON `RouterDecision` with
`stage=provider`, `allowed=false`, `reason="provider failed: ..."`. The
`reason` field carries a short label like `ollama:summarize: network
error`. It does NOT echo the prompt text. Verify this by setting a unique
sentinel in `--text` and grepping stdout / stderr for it.

## Docker

The default `docker-compose.yml` runs with `network_mode: none`, which
prevents the container from reaching Ollama on the host. Running
`docker compose run --rm wolf python -m wolf.cli summarize-email
--backend ollama ...` will therefore fail with a network error — this is
the expected behavior; the safety guarantee that the unit test container
does not touch external services is preserved.

If you want to call Ollama from inside a container, create a separate
compose file (e.g. `docker-compose.ollama.yml`) that:

- removes `network_mode: none`,
- adds `--add-host=host.docker.internal:host-gateway` on Linux, or uses
  `host.docker.internal` directly on macOS / Windows,
- passes `--ollama-url http://host.docker.internal:11434`.

This is **not** part of PR #13 and is deferred to a later PR that adds an
explicit network-allowed compose profile.

## macOS / WSL / Ubuntu notes

- **macOS**: Ollama's installer runs the daemon as a background process.
  `http://127.0.0.1:11434` is the default. No additional setup is
  required.
- **WSL2 (Windows)**: install Ollama inside the WSL2 distribution, not on
  the Windows host. Wolf running in WSL2 will reach the WSL-local
  daemon. The Docker Desktop CUDA setup does not affect this — Ollama
  uses CUDA directly via the WSL2 driver when available, independent of
  Docker.
- **Ubuntu (native)**: the daemon listens on `127.0.0.1` by default.
  Verify with `systemctl status ollama` (if installed as a service) or
  `pgrep -f 'ollama serve'`.

## Privacy posture

- The adapter never sends the prompt to a network host other than the
  one named in `base_url`.
- `base_url` must be a localhost URL unless `allow_non_localhost=True` is
  explicitly set. This is the same flag exposed by the CLI as
  `--allow-non-localhost-ollama`.
- The Router wraps the email body in `UntrustedText` and quotes it with
  `quote_untrusted_for_prompt` before the adapter ever sees it. The
  bilingual boundary preamble is part of what gets sent to Ollama.
- If Ollama is misconfigured to forward to a cloud relay, the adapter
  cannot tell. That is a deployment concern, not an adapter concern.
- The adapter does not log the prompt. The Router's audit log records
  the action kind, decision stage, and body length / source — never the
  body content.

## Troubleshooting

`provider failed: ollama:summarize: network error`
:   Ollama is not running, or the URL is wrong. Run
    `curl -fsS $URL/api/tags` to confirm.

`provider failed: ollama:summarize: HTTP 404`
:   The Ollama API path may have changed in a future release. The
    adapter targets `/api/generate`. Verify the local Ollama version
    matches the documented API.

`provider failed: ollama:summarize: response missing 'response' field`
:   Ollama returned a JSON object without the expected shape. Likely a
    model still loading, a wrong model name, or an Ollama version mismatch.

`provider failed: ollama:summarize: server reported done=false`
:   The adapter requested a non-streaming response but the server still
    sent a partial. Re-run; if it persists, the model may be too large
    for a single non-streaming call, in which case a future streaming
    adapter is needed.
