"""Local-only GUI shell for wolf.

A minimal stdlib-only web GUI that exposes the existing CLI surface
through a small HTTP server bound to 127.0.0.1 by default. No
external dependencies; no Flask / FastAPI / Electron / React; no
authentication; no remote network calls. The GUI is a developer
convenience for command entry, audit inspection, and a placeholder
panel for future avatar / robot UI work.
"""

from .server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    WolfGuiHandler,
    build_server,
    serve_forever,
)
from .settings import (
    DEFAULT_SETTINGS,
    SETTINGS_FILENAME,
    SettingsError,
    default_settings_path,
    load_settings,
    save_settings,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_SETTINGS",
    "SETTINGS_FILENAME",
    "SettingsError",
    "WolfGuiHandler",
    "build_server",
    "default_settings_path",
    "load_settings",
    "save_settings",
    "serve_forever",
]
