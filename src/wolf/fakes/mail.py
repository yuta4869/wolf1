from __future__ import annotations

from typing import Any, Dict, List, Mapping

from ..adapters.mail import MailDraft


class FakeMail:
    def __init__(self) -> None:
        self.inbox: List[Mapping[str, Any]] = []
        self.drafts: Dict[str, MailDraft] = {}
        self.sent: List[MailDraft] = []
        self._next = 1

    def list_inbox(self, *, limit: int = 50) -> List[Mapping[str, Any]]:
        return list(self.inbox[:limit])

    def create_draft(self, draft: MailDraft) -> str:
        did = f"draft-{self._next}"
        self._next += 1
        self.drafts[did] = draft
        return did

    def send(self, draft_id: str, *, confirmation_token: str) -> None:
        if not confirmation_token:
            raise PermissionError("confirmation_token required")
        if draft_id not in self.drafts:
            raise KeyError(draft_id)
        self.sent.append(self.drafts.pop(draft_id))
