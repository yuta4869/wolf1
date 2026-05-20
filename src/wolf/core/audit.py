from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .types import AuditEvent


SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "auth",
    "ssn",
    "private_key",
)
MASK = "***REDACTED***"


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    return any(frag in k for frag in SENSITIVE_KEY_FRAGMENTS)


def _mask(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: (MASK if _is_sensitive(k) else _mask(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(x) for x in obj]
    if isinstance(obj, tuple):
        return [_mask(x) for x in obj]
    return obj


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class AuditLogger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: AuditEvent) -> None:
        payload = _mask(asdict(event))
        line = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def tail(self, n: int = 100) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        return [json.loads(line) for line in lines[-n:] if line.strip()]
