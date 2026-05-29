"""GUI settings persistence.

Settings live at `<project_root>/.wolf/config/settings.json`. The
schema is a flat dict with a fixed set of keys. Unknown keys in the
incoming payload are dropped silently; this keeps the GUI from
persisting attacker-controlled fields (especially anything that
looks like a token or credential path).

We never store secrets here. The `default_gmail_credentials_path`
field is intentionally omitted from `DEFAULT_SETTINGS`; if you want
the GUI to remember a path, you have to send it through `save_*`
and the server will reject obvious token-looking values.

If the on-disk file is malformed JSON, `load_settings` renames it
to `settings.json.bak.<ts>` and returns defaults. This is
fail-open-safe: the user keeps using the GUI without a crash, and
the bad file is preserved for inspection.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Dict


SETTINGS_FILENAME = "settings.json"


# Forbidden value patterns: any field that matches one of these is
# rejected at save time. The intent is to make accidental storage
# of a real token / secret produce an explicit error instead of a
# silent persisted credential.
_FORBIDDEN_VALUE_RE = re.compile(
    r"(?i)("
    r"bearer\s+[a-z0-9._\-]{16,}"
    r"|ya29\.[a-z0-9._\-]{16,}"
    r"|access_token"
    r"|refresh_token"
    r"|client_secret"
    r"|api_key"
    r")"
)

# Keys that are explicitly forbidden in a save payload. The server
# rejects the whole request if any of these appear.
_FORBIDDEN_KEYS = (
    "access_token",
    "refresh_token",
    "client_secret",
    "api_key",
    "bearer_token",
    "password",
    "secret",
    "credentials",  # bare key
)

DEFAULT_SETTINGS: Dict[str, Any] = {
    "default_llm_backend": "fake",
    "default_ollama_model": "",
    "default_ollama_url": "",
    "default_gmail_backend": "fake",
    "default_output": "json",
    "theme": "system",
    "avatar_enabled": False,
    "avatar_style": "placeholder",
    # Optional; intentionally not loaded as a real secret store.
    # If the user sets this, the GUI will pass it to gmail-* commands.
    # The credentials FILE itself remains outside the repo; only the
    # path string is persisted.
    "gmail_credentials_path": "",
}


# Allowed value types per key. Anything else is dropped at save time.
_TYPED_SCHEMA = {
    "default_llm_backend": (str, ("fake", "ollama")),
    "default_ollama_model": (str, None),
    "default_ollama_url": (str, None),
    "default_gmail_backend": (str, ("fake", "gmail")),
    "default_output": (str, ("json", "text")),
    "theme": (str, ("system", "light", "dark")),
    "avatar_enabled": (bool, None),
    "avatar_style": (str, ("placeholder",)),
    "gmail_credentials_path": (str, None),
}


class SettingsError(Exception):
    """Raised when a save payload contains forbidden / invalid fields."""

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.label = label


def default_settings_path(project_root: Path) -> Path:
    return Path(project_root).resolve() / ".wolf" / "config" / SETTINGS_FILENAME


def _coerce(key: str, value: Any) -> Any:
    """Return a sanitized value for `key`, or raise SettingsError."""
    spec = _TYPED_SCHEMA.get(key)
    if spec is None:
        # Unknown keys are dropped silently (see module docstring).
        raise KeyError(key)
    typ, choices = spec
    if typ is bool:
        if isinstance(value, bool):
            return value
        raise SettingsError(f"settings: {key!r} must be boolean")
    # Strings.
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SettingsError(f"settings: {key!r} must be a string")
    if choices is not None and value not in choices:
        raise SettingsError(
            f"settings: {key!r} must be one of {choices!r}"
        )
    if _FORBIDDEN_VALUE_RE.search(value):
        raise SettingsError(
            f"settings: {key!r} value matches a forbidden secret pattern"
        )
    return value


def _reject_secret_keys(payload: Dict[str, Any]) -> None:
    lowered = {k.lower() for k in payload.keys()}
    for forbidden in _FORBIDDEN_KEYS:
        if forbidden in lowered:
            raise SettingsError(
                f"settings: payload contains forbidden key {forbidden!r}"
            )


def load_settings(path: Path) -> Dict[str, Any]:
    """Load settings, fall back to defaults on missing / malformed."""
    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        raw = p.read_text(encoding="utf-8")
        decoded = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # Backup the bad file and return defaults.
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            p.rename(p.with_name(f"{p.name}.bak.{ts}"))
        except OSError:
            pass
        return dict(DEFAULT_SETTINGS)
    if not isinstance(decoded, dict):
        # Same backup-and-default behavior.
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            p.rename(p.with_name(f"{p.name}.bak.{ts}"))
        except OSError:
            pass
        return dict(DEFAULT_SETTINGS)
    out = dict(DEFAULT_SETTINGS)
    for k, v in decoded.items():
        try:
            out[k] = _coerce(k, v)
        except (KeyError, SettingsError):
            # Drop unknown keys and unsafe values silently on load.
            continue
    return out


def save_settings(path: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and persist `payload`. Returns the saved snapshot."""
    if not isinstance(payload, dict):
        raise SettingsError("settings: payload must be a JSON object")
    _reject_secret_keys(payload)
    merged = dict(DEFAULT_SETTINGS)
    for k, v in payload.items():
        try:
            merged[k] = _coerce(k, v)
        except KeyError:
            # Unknown key: drop silently to avoid persisting attacker-
            # controlled fields.
            continue
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(merged, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return merged
