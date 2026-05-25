"""Tests for src/wolf/adapters/ollama.py.

The Ollama server is NOT contacted by these tests. We patch
urllib.request.urlopen to return controlled bytes, or we let urlopen
fail naturally for negative cases. An optional integration smoke test
gated by WOLF_RUN_OLLAMA_INTEGRATION=1 lives at the bottom of this file
and only runs against a real local Ollama when explicitly opted in.
"""

from __future__ import annotations

import io
import json
import os
import socket
import unittest
import urllib.error
import urllib.request
from typing import Optional
from unittest import mock

from wolf.adapters.llm import LLMAdapter
from wolf.adapters.ollama import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SEC,
    GENERATE_PATH,
    OllamaConfig,
    OllamaLLMAdapter,
)
from wolf.core.errors import AdapterError


def _make_response(payload: dict) -> mock.MagicMock:
    body = json.dumps(payload).encode("utf-8")
    fake = mock.MagicMock()
    fake.__enter__ = mock.MagicMock(return_value=fake)
    fake.__exit__ = mock.MagicMock(return_value=False)
    fake.read = mock.MagicMock(return_value=body)
    return fake


def _make_raw_response(body: bytes) -> mock.MagicMock:
    fake = mock.MagicMock()
    fake.__enter__ = mock.MagicMock(return_value=fake)
    fake.__exit__ = mock.MagicMock(return_value=False)
    fake.read = mock.MagicMock(return_value=body)
    return fake


class ConstructorTest(unittest.TestCase):
    def test_implements_llm_adapter_protocol(self) -> None:
        a = OllamaLLMAdapter(model="m")
        self.assertIsInstance(a, LLMAdapter)

    def test_default_base_url_is_localhost(self) -> None:
        a = OllamaLLMAdapter(model="m")
        self.assertEqual(a.config.base_url, DEFAULT_BASE_URL)
        self.assertIn("127.0.0.1", a.config.base_url)

    def test_model_required(self) -> None:
        with self.assertRaises(ValueError):
            OllamaLLMAdapter(model="")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            OllamaLLMAdapter(model="   ")
        with self.assertRaises(TypeError) if False else self.assertRaises(ValueError):  # type: ignore[unreachable]
            OllamaLLMAdapter(model=None)  # type: ignore[arg-type]

    def test_external_base_url_rejected_by_default(self) -> None:
        with self.assertRaises(ValueError) as cm:
            OllamaLLMAdapter(model="m", base_url="http://example.com")
        self.assertIn("localhost", str(cm.exception).lower())

    def test_external_base_url_allowed_with_flag(self) -> None:
        a = OllamaLLMAdapter(
            model="m",
            base_url="http://example.com",
            allow_non_localhost=True,
        )
        self.assertEqual(a.config.base_url, "http://example.com")
        self.assertTrue(a.config.allow_non_localhost)

    def test_localhost_variants_accepted(self) -> None:
        for url in (
            "http://localhost:11434",
            "http://127.0.0.1:11434",
            "http://[::1]:11434",
        ):
            with self.subTest(url=url):
                OllamaLLMAdapter(model="m", base_url=url)

    def test_invalid_scheme_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OllamaLLMAdapter(model="m", base_url="ftp://127.0.0.1:11434")

    def test_url_without_host_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OllamaLLMAdapter(model="m", base_url="http://")

    def test_timeout_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            OllamaLLMAdapter(model="m", timeout_sec=0)
        with self.assertRaises(ValueError):
            OllamaLLMAdapter(model="m", timeout_sec=-1.5)

    def test_repr_does_not_include_secret_like_data(self) -> None:
        a = OllamaLLMAdapter(model="m")
        r = repr(a)
        self.assertIn("OllamaLLMAdapter", r)
        # No prompt is stored on the instance, so repr should not contain
        # the word "prompt" at all.
        self.assertNotIn("prompt", r.lower())


class GenerateRequestShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OllamaLLMAdapter(model="llama3.1")

    def _patched_urlopen(self, payload: dict):
        return mock.patch.object(
            urllib.request,
            "urlopen",
            return_value=_make_response(payload),
        )

    def test_posts_to_generate_endpoint(self) -> None:
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["timeout"] = timeout
            return _make_response(
                {"response": "ok", "done": True}
            )

        with mock.patch.object(
            urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            self.adapter.summarize("hello")

        self.assertEqual(captured["url"], DEFAULT_BASE_URL + GENERATE_PATH)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["timeout"], DEFAULT_TIMEOUT_SEC)

    def test_request_json_has_required_fields(self) -> None:
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            captured["content_type"] = req.headers.get("Content-type")
            return _make_response({"response": "ok", "done": True})

        with mock.patch.object(
            urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            self.adapter.summarize("hello world")

        body = json.loads(captured["body"].decode("utf-8"))
        self.assertEqual(body["model"], "llama3.1")
        self.assertEqual(body["prompt"], "hello world")
        self.assertEqual(body["stream"], False)
        self.assertIn("application/json", captured["content_type"])

    def test_max_tokens_maps_to_options_num_predict(self) -> None:
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            return _make_response({"response": "ok", "done": True})

        with mock.patch.object(
            urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            self.adapter.summarize("hi", max_tokens=128)

        body = json.loads(captured["body"].decode("utf-8"))
        self.assertEqual(body.get("options", {}).get("num_predict"), 128)

    def test_response_field_is_returned(self) -> None:
        with mock.patch.object(
            urllib.request,
            "urlopen",
            return_value=_make_response(
                {"response": "summary text", "done": True}
            ),
        ):
            result = self.adapter.summarize("hi")
        self.assertEqual(result, "summary text")

    def test_custom_timeout_passed_to_urlopen(self) -> None:
        adapter = OllamaLLMAdapter(model="m", timeout_sec=5.0)
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            return _make_response({"response": "x", "done": True})

        with mock.patch.object(
            urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            adapter.summarize("hi")
        self.assertEqual(captured["timeout"], 5.0)


class GenerateErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OllamaLLMAdapter(model="m")

    def test_network_url_error_becomes_adapter_error(self) -> None:
        url_error = urllib.error.URLError("Connection refused")
        with mock.patch.object(
            urllib.request, "urlopen", side_effect=url_error
        ):
            with self.assertRaises(AdapterError) as cm:
                self.adapter.summarize("hi")
        self.assertIn("network", cm.exception.label.lower())
        self.assertIsInstance(cm.exception.cause, urllib.error.URLError)

    def test_http_error_becomes_adapter_error(self) -> None:
        http_error = urllib.error.HTTPError(
            url="http://x", code=500, msg="server", hdrs=None, fp=io.BytesIO()
        )
        with mock.patch.object(
            urllib.request, "urlopen", side_effect=http_error
        ):
            with self.assertRaises(AdapterError) as cm:
                self.adapter.summarize("hi")
        self.assertIn("HTTP 500", cm.exception.label)

    def test_timeout_becomes_adapter_error(self) -> None:
        with mock.patch.object(
            urllib.request, "urlopen", side_effect=socket.timeout("slow")
        ):
            with self.assertRaises(AdapterError) as cm:
                self.adapter.summarize("hi")
        self.assertIn("timeout", cm.exception.label.lower())

    def test_invalid_json_becomes_adapter_error(self) -> None:
        with mock.patch.object(
            urllib.request,
            "urlopen",
            return_value=_make_raw_response(b"not json {"),
        ):
            with self.assertRaises(AdapterError) as cm:
                self.adapter.summarize("hi")
        self.assertIn("JSON", cm.exception.label)

    def test_missing_response_field_becomes_adapter_error(self) -> None:
        with mock.patch.object(
            urllib.request,
            "urlopen",
            return_value=_make_response({"done": True}),
        ):
            with self.assertRaises(AdapterError) as cm:
                self.adapter.summarize("hi")
        self.assertIn("response", cm.exception.label)

    def test_response_field_not_string(self) -> None:
        with mock.patch.object(
            urllib.request,
            "urlopen",
            return_value=_make_response({"response": 123, "done": True}),
        ):
            with self.assertRaises(AdapterError):
                self.adapter.summarize("hi")

    def test_done_false_becomes_adapter_error(self) -> None:
        with mock.patch.object(
            urllib.request,
            "urlopen",
            return_value=_make_response(
                {"response": "partial", "done": False}
            ),
        ):
            with self.assertRaises(AdapterError) as cm:
                self.adapter.summarize("hi")
        self.assertIn("done=false", cm.exception.label.lower())

    def test_adapter_error_str_does_not_contain_prompt(self) -> None:
        unique_marker = "SENSITIVE_PROMPT_TOKEN_42_XYZ_UNIQUE"
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            try:
                self.adapter.summarize(
                    f"please summarize {unique_marker} for me"
                )
            except AdapterError as exc:
                rendered = repr(exc) + "|" + str(exc) + "|" + exc.label
                self.assertNotIn(unique_marker, rendered)


class NoExternalImportsTest(unittest.TestCase):
    def test_module_does_not_import_requests_or_httpx(self) -> None:
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "wolf" / "adapters" / "ollama.py"
        body = src.read_text(encoding="utf-8")
        self.assertNotIn("import requests", body)
        self.assertNotIn("from requests", body)
        self.assertNotIn("import httpx", body)
        self.assertNotIn("from httpx", body)
        self.assertIn("urllib", body)


class IntegrationSmokeTest(unittest.TestCase):
    """Optional smoke test against a real local Ollama server.

    Skipped unless WOLF_RUN_OLLAMA_INTEGRATION=1 and WOLF_OLLAMA_MODEL is
    set. The CI container runs with network_mode: none so this test is
    always skipped there.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("WOLF_RUN_OLLAMA_INTEGRATION") != "1":
            raise unittest.SkipTest("WOLF_RUN_OLLAMA_INTEGRATION not set")
        if not os.environ.get("WOLF_OLLAMA_MODEL"):
            raise unittest.SkipTest("WOLF_OLLAMA_MODEL not set")

    def test_local_summarize(self) -> None:
        model = os.environ["WOLF_OLLAMA_MODEL"]
        adapter = OllamaLLMAdapter(model=model)
        result = adapter.summarize(
            "One sentence: meetings move tomorrow to 3pm."
        )
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)


if __name__ == "__main__":
    unittest.main()
