"""Local Ollama LLM adapter.

Implements the LLMAdapter Protocol against a locally-running Ollama HTTP
server (default http://127.0.0.1:11434). Uses only urllib from the stdlib;
no third-party HTTP client is imported. The adapter is intentionally
narrow:

- It does no safety filtering — Router is responsible for that.
- It does not stream — `stream` is hard-coded to false.
- It does not call tools, embed, or chat in multi-turn mode.
- It refuses non-localhost base_url by default to prevent accidental
  cloud-LLM connections.

Any HTTP / JSON / protocol failure surfaces as an AdapterError with a
message that does NOT echo the prompt content. Callers must rely on the
audit log and the Router's own bookkeeping to correlate failures with
inputs.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from ..core.errors import AdapterError
from .llm import LLMAdapter  # noqa: F401  (re-exported for typing imports)


DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT_SEC = 30.0
GENERATE_PATH = "/api/generate"

_LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


__all__ = [
    "AdapterError",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SEC",
    "GENERATE_PATH",
    "OllamaConfig",
    "OllamaLLMAdapter",
]


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str = DEFAULT_BASE_URL
    model: Optional[str] = None
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    allow_non_localhost: bool = False


def _is_localhost_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host in _LOCALHOST_HOSTS


def _validate_url(url: str, *, allow_non_localhost: bool) -> None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise ValueError(f"invalid base_url: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"base_url must use http or https scheme, got "
            f"{parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise ValueError("base_url must include a host")
    if not _is_localhost_url(url) and not allow_non_localhost:
        raise ValueError(
            "base_url is not localhost; set allow_non_localhost=True to "
            "permit external endpoints (this opens a non-local network "
            "path)"
        )


class OllamaLLMAdapter:
    """LLMAdapter that posts to /api/generate on a local Ollama server.

    Construction validates that the model is provided and that the URL is
    a localhost URL unless allow_non_localhost is set. The adapter does
    not contact the server at construction time; the first network call
    happens in summarize() / generate().
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        allow_non_localhost: bool = False,
    ) -> None:
        if not model or not isinstance(model, str) or not model.strip():
            raise ValueError("OllamaLLMAdapter: model is required (non-empty string)")
        _validate_url(base_url, allow_non_localhost=allow_non_localhost)
        if timeout_sec <= 0:
            raise ValueError(
                f"OllamaLLMAdapter: timeout_sec must be positive, got {timeout_sec!r}"
            )
        self.config = OllamaConfig(
            base_url=base_url.rstrip("/"),
            model=model,
            timeout_sec=float(timeout_sec),
            allow_non_localhost=bool(allow_non_localhost),
        )

    def __repr__(self) -> str:
        return (
            f"OllamaLLMAdapter(model={self.config.model!r}, "
            f"base_url={self.config.base_url!r}, "
            f"timeout_sec={self.config.timeout_sec})"
        )

    def summarize(self, text: str, *, max_tokens: int = 256) -> str:
        return self._generate_impl(text, max_tokens=max_tokens, label="summarize")

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        return self._generate_impl(prompt, max_tokens=max_tokens, label="generate")

    def _generate_impl(self, prompt: str, *, max_tokens: int, label: str) -> str:
        url = self.config.base_url + GENERATE_PATH
        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": False,
        }
        if max_tokens and max_tokens > 0:
            payload["options"] = {"num_predict": int(max_tokens)}
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
                f"ollama:{label}: HTTP {exc.code}",
                cause=exc,
            ) from exc
        except urllib.error.URLError as exc:
            raise AdapterError(
                f"ollama:{label}: network error",
                cause=exc,
            ) from exc
        except socket.timeout as exc:
            raise AdapterError(
                f"ollama:{label}: timeout after {self.config.timeout_sec}s",
                cause=exc,
            ) from exc
        except OSError as exc:
            raise AdapterError(
                f"ollama:{label}: socket error",
                cause=exc,
            ) from exc

        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError(
                f"ollama:{label}: invalid JSON response",
                cause=exc,
            ) from exc

        if not isinstance(decoded, dict):
            raise AdapterError(
                f"ollama:{label}: response is not a JSON object"
            )
        if "response" not in decoded:
            raise AdapterError(
                f"ollama:{label}: response missing 'response' field"
            )
        if decoded.get("done") is False:
            raise AdapterError(
                f"ollama:{label}: server reported done=false (partial / streaming response)"
            )
        result = decoded.get("response")
        if not isinstance(result, str):
            raise AdapterError(
                f"ollama:{label}: 'response' field is not a string"
            )
        return result
