from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMAdapter(Protocol):
    def summarize(self, text: str, *, max_tokens: int = 256) -> str: ...

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str: ...
