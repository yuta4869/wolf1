from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class MailDraft:
    to: str
    subject: str
    body: str


@runtime_checkable
class MailAdapter(Protocol):
    def list_inbox(self, *, limit: int = 50) -> List[Mapping[str, Any]]: ...

    def create_draft(self, draft: MailDraft) -> str: ...

    def send(self, draft_id: str, *, confirmation_token: str) -> None: ...
