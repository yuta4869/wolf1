"""Tests for src/wolf/gui/settings.py."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wolf.gui.settings import (
    DEFAULT_SETTINGS,
    SETTINGS_FILENAME,
    SettingsError,
    default_settings_path,
    load_settings,
    save_settings,
)


class DefaultsTest(unittest.TestCase):
    def test_default_path_under_project_root(self) -> None:
        with TemporaryDirectory() as td:
            p = default_settings_path(Path(td))
            self.assertEqual(p.name, SETTINGS_FILENAME)
            self.assertIn(".wolf", str(p))
            self.assertIn("config", str(p))

    def test_load_missing_returns_defaults(self) -> None:
        with TemporaryDirectory() as td:
            s = load_settings(Path(td) / "nope.json")
        self.assertEqual(s, DEFAULT_SETTINGS)

    def test_default_settings_have_no_secrets(self) -> None:
        for k, v in DEFAULT_SETTINGS.items():
            self.assertNotIn("token", k.lower())
            self.assertNotIn("secret", k.lower())
            if isinstance(v, str):
                self.assertNotIn("Bearer ", v)


class SaveAndRoundtripTest(unittest.TestCase):
    def test_save_then_load(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            saved = save_settings(
                p,
                {
                    "default_llm_backend": "ollama",
                    "default_ollama_model": "llama3.2:3b",
                    "theme": "dark",
                    "avatar_enabled": True,
                },
            )
        self.assertEqual(saved["default_llm_backend"], "ollama")
        self.assertEqual(saved["default_ollama_model"], "llama3.2:3b")
        self.assertEqual(saved["theme"], "dark")
        self.assertTrue(saved["avatar_enabled"])

    def test_roundtrip_via_disk(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            save_settings(p, {"theme": "light"})
            loaded = load_settings(p)
            self.assertEqual(loaded["theme"], "light")

    def test_unknown_key_dropped_silently(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            saved = save_settings(p, {"unknown_field": "x"})
            self.assertNotIn("unknown_field", saved)


class ForbiddenSecretsTest(unittest.TestCase):
    def test_access_token_key_rejected(self) -> None:
        with TemporaryDirectory() as td:
            with self.assertRaises(SettingsError):
                save_settings(
                    Path(td) / "s.json",
                    {"access_token": "ya29.A0xxxxxxxxxxxxxxxx"},
                )

    def test_refresh_token_key_rejected(self) -> None:
        with TemporaryDirectory() as td:
            with self.assertRaises(SettingsError):
                save_settings(
                    Path(td) / "s.json",
                    {"refresh_token": "1//abcxxxxxxxxxxxx"},
                )

    def test_bearer_value_rejected(self) -> None:
        with TemporaryDirectory() as td:
            with self.assertRaises(SettingsError):
                save_settings(
                    Path(td) / "s.json",
                    {"gmail_credentials_path": "Bearer abcdef1234567890_zz"},
                )

    def test_invalid_choice_rejected(self) -> None:
        with TemporaryDirectory() as td:
            with self.assertRaises(SettingsError):
                save_settings(
                    Path(td) / "s.json",
                    {"default_llm_backend": "openai"},
                )

    def test_non_bool_for_bool_rejected(self) -> None:
        with TemporaryDirectory() as td:
            with self.assertRaises(SettingsError):
                save_settings(
                    Path(td) / "s.json",
                    {"avatar_enabled": "yes"},
                )


class MalformedFileTest(unittest.TestCase):
    def test_malformed_json_backed_up_and_returns_defaults(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            p.write_text("not json", encoding="utf-8")
            loaded = load_settings(p)
            self.assertEqual(loaded, DEFAULT_SETTINGS)
            # Backup file lives next to the original.
            backups = list(Path(td).glob("s.json.bak.*"))
            self.assertGreaterEqual(len(backups), 1)

    def test_non_object_json_backed_up(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            p.write_text("[1, 2, 3]", encoding="utf-8")
            loaded = load_settings(p)
        self.assertEqual(loaded, DEFAULT_SETTINGS)


if __name__ == "__main__":
    unittest.main()
