"""EmbeddingAdapter Protocol.

Adapters that implement this protocol turn text into a list of floats
(an embedding). The Protocol is intentionally minimal:

- one method: `embed(text) -> List[float]`
- no batch interface (yet),
- no async,
- no token-budget negotiation,
- no model selection at call time (adapters are constructed for a
  specific model).

Concrete implementations live next to other adapters
(`ollama_embeddings.py`) and tests can use a fake.
"""

from __future__ import annotations

from typing import List, Protocol, runtime_checkable


@runtime_checkable
class EmbeddingAdapter(Protocol):
    def embed(self, text: str) -> List[float]: ...
