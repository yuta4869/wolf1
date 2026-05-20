from __future__ import annotations

from typing import List, Tuple


class FakeLLM:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, int, int]] = []

    def summarize(self, text: str, *, max_tokens: int = 256) -> str:
        self.calls.append(("summarize", len(text), max_tokens))
        snippet = text.strip().replace("\n", " ")[:max_tokens]
        return f"SUMMARY({len(text)}ch): {snippet}"

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        self.calls.append(("generate", len(prompt), max_tokens))
        return f"FAKE[{prompt[:80]}]"
