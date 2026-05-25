"""Tests for src/wolf/adapters/ollama_embeddings.py.

No real Ollama is required; urllib.request.urlopen is patched.
"""

from __future__ import annotations

import json
import os
import socket
import unittest
import urllib.error
import urllib.request
from typing import Optional
from unittest import mock

from wolf.adapters.embedding import EmbeddingAdapter
from wolf.adapters.ollama_embeddings import (
    DEFAULT_BASE_URL,
    EMBEDDINGS_PATH,
    OllamaEmbeddingAdapter,
)
from wolf.core.errors import AdapterError


def _resp(payload: dict) -> mock.MagicMock:
    body = json.dumps(payload).encode("utf-8")
    fake = mock.MagicMock()
    fake.__enter__ = mock.MagicMock(return_value=fake)
    fake.__exit__ = mock.MagicMock(return_value=False)
    fake.read = mock.MagicMock(return_value=body)
    return fake


def _raw_resp(body: bytes) -> mock.MagicMock:
    fake = mock.MagicMock()
    fake.__enter__ = mock.MagicMock(return_value=fake)
    fake.__exit__ = mock.MagicMock(return_value=False)
    fake.read = mock.MagicMock(return_value=body)
    return fake


class ConstructorTest(unittest.TestCase):
    def test_implements_embedding_adapter(self) -> None:
        a = OllamaEmbeddingAdapter(model="m")
        self.assertIsInstance(a, EmbeddingAdapter)

    def test_default_base_url_is_localhost(self) -> None:
        a = OllamaEmbeddingAdapter(model="m")
        self.assertIn("127.0.0.1", a.config.base_url)

    def test_model_required(self) -> None:
        with self.assertRaises(ValueError):
            OllamaEmbeddingAdapter(model="")
        with self.assertRaises(ValueError):
            OllamaEmbeddingAdapter(model="   ")

    def test_external_url_rejected_by_default(self) -> None:
        with self.assertRaises(ValueError):
            OllamaEmbeddingAdapter(model="m", base_url="http://example.com")

    def test_external_url_allowed_with_flag(self) -> None:
        a = OllamaEmbeddingAdapter(
            model="m",
            base_url="http://example.com",
            allow_non_localhost=True,
        )
        self.assertEqual(a.config.base_url, "http://example.com")

    def test_invalid_scheme(self) -> None:
        with self.assertRaises(ValueError):
            OllamaEmbeddingAdapter(model="m", base_url="ftp://127.0.0.1")

    def test_positive_timeout(self) -> None:
        with self.assertRaises(ValueError):
            OllamaEmbeddingAdapter(model="m", timeout_sec=0)
        with self.assertRaises(ValueError):
            OllamaEmbeddingAdapter(model="m", timeout_sec=-1)


class EmbedRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OllamaEmbeddingAdapter(model="nomic-embed-text")

    def test_request_shape(self) -> None:
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = req.data
            return _resp({"embedding": [0.1, 0.2, 0.3]})

        with mock.patch.object(
            urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            vec = self.adapter.embed("hello")

        self.assertEqual(captured["url"], DEFAULT_BASE_URL + EMBEDDINGS_PATH)
        self.assertEqual(captured["method"], "POST")
        body = json.loads(captured["body"].decode("utf-8"))
        self.assertEqual(body["model"], "nomic-embed-text")
        self.assertEqual(body["prompt"], "hello")
        self.assertEqual(vec, [0.1, 0.2, 0.3])

    def test_int_values_are_coerced_to_float(self) -> None:
        with mock.patch.object(
            urllib.request,
            "urlopen",
            return_value=_resp({"embedding": [1, 2, 3]}),
        ):
            vec = self.adapter.embed("hi")
        self.assertEqual(vec, [1.0, 2.0, 3.0])
        for v in vec:
            self.assertIsInstance(v, float)


class EmbedErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OllamaEmbeddingAdapter(model="m")

    def test_network_error(self) -> None:
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            with self.assertRaises(AdapterError) as cm:
                self.adapter.embed("hi")
        self.assertIn("network", cm.exception.label.lower())

    def test_http_error(self) -> None:
        import io

        http_error = urllib.error.HTTPError(
            url="x", code=500, msg="srv", hdrs=None, fp=io.BytesIO()
        )
        with mock.patch.object(
            urllib.request, "urlopen", side_effect=http_error
        ):
            with self.assertRaises(AdapterError) as cm:
                self.adapter.embed("hi")
        self.assertIn("HTTP 500", cm.exception.label)

    def test_invalid_json(self) -> None:
        with mock.patch.object(
            urllib.request, "urlopen", return_value=_raw_resp(b"not json")
        ):
            with self.assertRaises(AdapterError) as cm:
                self.adapter.embed("hi")
        self.assertIn("JSON", cm.exception.label)

    def test_missing_embedding_field(self) -> None:
        with mock.patch.object(
            urllib.request, "urlopen", return_value=_resp({"foo": "bar"})
        ):
            with self.assertRaises(AdapterError) as cm:
                self.adapter.embed("hi")
        self.assertIn("embedding", cm.exception.label)

    def test_embedding_not_a_list(self) -> None:
        with mock.patch.object(
            urllib.request,
            "urlopen",
            return_value=_resp({"embedding": "not-a-list"}),
        ):
            with self.assertRaises(AdapterError):
                self.adapter.embed("hi")

    def test_empty_embedding(self) -> None:
        with mock.patch.object(
            urllib.request,
            "urlopen",
            return_value=_resp({"embedding": []}),
        ):
            with self.assertRaises(AdapterError):
                self.adapter.embed("hi")

    def test_non_numeric_embedding_value(self) -> None:
        with mock.patch.object(
            urllib.request,
            "urlopen",
            return_value=_resp({"embedding": [0.1, "bad", 0.3]}),
        ):
            with self.assertRaises(AdapterError):
                self.adapter.embed("hi")

    def test_error_label_does_not_leak_text(self) -> None:
        marker = "EMBED_TEXT_LEAK_PROBE_QQ_42"
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            try:
                self.adapter.embed(f"please embed {marker}")
            except AdapterError as exc:
                rendered = repr(exc) + "|" + str(exc) + "|" + exc.label
                self.assertNotIn(marker, rendered)


class IntegrationSmokeTest(unittest.TestCase):
    """Optional real-Ollama smoke; skipped unless explicitly enabled."""

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("WOLF_RUN_OLLAMA_INTEGRATION") != "1":
            raise unittest.SkipTest("WOLF_RUN_OLLAMA_INTEGRATION not set")
        if not os.environ.get("WOLF_OLLAMA_EMBED_MODEL"):
            raise unittest.SkipTest("WOLF_OLLAMA_EMBED_MODEL not set")

    def test_local_embed(self) -> None:
        model = os.environ["WOLF_OLLAMA_EMBED_MODEL"]
        a = OllamaEmbeddingAdapter(model=model)
        vec = a.embed("Hello world.")
        self.assertGreater(len(vec), 8)
        for v in vec:
            self.assertIsInstance(v, float)


if __name__ == "__main__":
    unittest.main()
