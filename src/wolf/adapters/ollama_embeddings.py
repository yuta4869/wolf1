"""Local Ollama embeddings adapter.

POSTs to `/api/embeddings` on a local Ollama HTTP server. Mirrors the
shape and safety properties of `OllamaLLMAdapter`:

- stdlib `urllib` only,
- localhost default with explicit opt-in for non-localhost,
- AdapterError on network / JSON / shape failures with no prompt echo
  in the error label,
- model is required at construction time.

Ollama's current `/api/embeddings` body is `{"model": ..., "prompt": "..."}`
and the response is `{"embedding": [floats...]}`.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

from ..core.errors import AdapterError
from .embedding import EmbeddingAdapter  # noqa: F401  (typing helper)
from .ollama import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SEC,
    _is_localhost_url,
)


EMBEDDINGS_PATH = "/api/embeddings"


def _validate_url(url: str, *, allow_non_localhost: bool) -> None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise ValueError(f"invalid base_url: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"base_url must use http or https scheme, got {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise ValueError("base_url must include a host")
    if not _is_localhost_url(url) and not allow_non_localhost:
        raise ValueError(
            "base_url is not localhost; set allow_non_localhost=True to "
            "permit external endpoints (this opens a non-local network path)"
        )


@dataclass(frozen=True)
class OllamaEmbeddingConfig:
    base_url: str = DEFAULT_BASE_URL
    model: Optional[str] = None
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    allow_non_localhost: bool = False


class OllamaEmbeddingAdapter:
    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        allow_non_localhost: bool = False,
    ) -> None:
        if not model or not isinstance(model, str) or not model.strip():
            raise ValueError(
                "OllamaEmbeddingAdapter: model is required (non-empty string)"
            )
        _validate_url(base_url, allow_non_localhost=allow_non_localhost)
        if timeout_sec <= 0:
            raise ValueError(
                f"OllamaEmbeddingAdapter: timeout_sec must be positive, "
                f"got {timeout_sec!r}"
            )
        self.config = OllamaEmbeddingConfig(
            base_url=base_url.rstrip("/"),
            model=model,
            timeout_sec=float(timeout_sec),
            allow_non_localhost=bool(allow_non_localhost),
        )

    def __repr__(self) -> str:
        return (
            f"OllamaEmbeddingAdapter(model={self.config.model!r}, "
            f"base_url={self.config.base_url!r}, "
            f"timeout_sec={self.config.timeout_sec})"
        )

    def embed(self, text: str) -> List[float]:
        url = self.config.base_url + EMBEDDINGS_PATH
        payload = {"model": self.config.model, "prompt": text}
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body_bytes,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.config.timeout_sec
            ) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise AdapterError(
                f"ollama:embed: HTTP {exc.code}", cause=exc
            ) from exc
        except urllib.error.URLError as exc:
            raise AdapterError(
                "ollama:embed: network error", cause=exc
            ) from exc
        except socket.timeout as exc:
            raise AdapterError(
                f"ollama:embed: timeout after {self.config.timeout_sec}s",
                cause=exc,
            ) from exc
        except OSError as exc:
            raise AdapterError(
                "ollama:embed: socket error", cause=exc
            ) from exc

        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError(
                "ollama:embed: invalid JSON response", cause=exc
            ) from exc

        if not isinstance(decoded, dict):
            raise AdapterError("ollama:embed: response is not a JSON object")
        if "embedding" not in decoded:
            raise AdapterError(
                "ollama:embed: response missing 'embedding' field"
            )
        vec = decoded["embedding"]
        if not isinstance(vec, list):
            raise AdapterError("ollama:embed: 'embedding' is not a list")
        if not vec:
            raise AdapterError("ollama:embed: 'embedding' is empty")
        out: List[float] = []
        for x in vec:
            if not isinstance(x, (int, float)) or isinstance(x, bool):
                raise AdapterError(
                    "ollama:embed: 'embedding' contains non-numeric value"
                )
            out.append(float(x))
        return out
