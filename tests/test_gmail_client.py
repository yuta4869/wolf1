"""Unit tests for the real GmailClient (urllib-based).

The tests mock urllib.request.urlopen with a stub so no real network
call happens. They verify request shape, header propagation, error
mapping, response parsing, and token redaction.
"""

from __future__ import annotations

import base64
import io
import json
import socket
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from wolf.gmail.client import (
    DEFAULT_BASE_URL,
    GmailClient,
    GmailClientError,
    GmailCredentials,
    _parse_message,
)
from wolf.gmail.draft import build_reply_draft_raw


def _write_creds(dir_: Path, token: str = "fake-access-token") -> Path:
    p = dir_ / "gmail_token.json"
    p.write_text(json.dumps({"access_token": token}), encoding="utf-8")
    return p


def _fake_response(payload: dict, status: int = 200):
    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._body = body
            self.status = status

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    return _Resp(json.dumps(payload).encode("utf-8"))


class CredentialsTest(unittest.TestCase):
    def test_from_path_loads_access_token(self) -> None:
        with TemporaryDirectory() as td:
            p = _write_creds(Path(td), token="abc")
            c = GmailCredentials.from_path(p)
            self.assertIsInstance(c, GmailCredentials)

    def test_missing_file_raises(self) -> None:
        with TemporaryDirectory() as td:
            with self.assertRaises(GmailClientError):
                GmailCredentials.from_path(Path(td) / "nope.json")

    def test_non_json_raises(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "creds.json"
            p.write_text("not json", encoding="utf-8")
            with self.assertRaises(GmailClientError):
                GmailCredentials.from_path(p)

    def test_missing_access_token_raises(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "creds.json"
            p.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
            with self.assertRaises(GmailClientError):
                GmailCredentials.from_path(p)

    def test_repr_redacts_token(self) -> None:
        with TemporaryDirectory() as td:
            p = _write_creds(Path(td), token="super-secret-token")
            c = GmailCredentials.from_path(p)
            self.assertNotIn("super-secret-token", repr(c))
            self.assertNotIn("super-secret-token", str(c))


class ClientConstructionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.creds = GmailCredentials.from_path(_write_creds(Path(self._td.name)))

    def test_default_base_url_is_https(self) -> None:
        c = GmailClient(self.creds)
        self.assertEqual(repr(c).count("https://"), 1)

    def test_non_https_rejected(self) -> None:
        with self.assertRaises(GmailClientError):
            GmailClient(
                self.creds,
                base_url="http://example.invalid",
            )

    def test_http_localhost_allowed_only_when_flag_set(self) -> None:
        with self.assertRaises(GmailClientError):
            GmailClient(self.creds, base_url="http://127.0.0.1:8080")
        c = GmailClient(
            self.creds,
            base_url="http://127.0.0.1:8080",
            allow_non_https=True,
        )
        self.assertIn("127.0.0.1:8080", repr(c))

    def test_zero_timeout_rejected(self) -> None:
        with self.assertRaises(GmailClientError):
            GmailClient(self.creds, timeout_sec=0)


class SearchRequestShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.creds = GmailCredentials.from_path(_write_creds(Path(self._td.name)))
        self.client = GmailClient(self.creds)

    def test_search_builds_get_request_with_q_and_max_results(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout):  # noqa: ARG001
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["headers"] = dict(req.header_items())
            return _fake_response(
                {
                    "messages": [
                        {"id": "abc", "threadId": "t1"},
                        {"id": "def", "threadId": "t2"},
                    ]
                }
            )

        with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            hits = self.client.search(query="from:alice meeting", max_results=5)

        self.assertEqual(captured["method"], "GET")
        self.assertIn("/gmail/v1/users/me/messages", captured["url"])
        self.assertIn("q=from%3Aalice+meeting", captured["url"])
        self.assertIn("maxResults=5", captured["url"])
        # Authorization header carries the bearer token.
        auth = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertIn("authorization", auth)
        self.assertTrue(auth["authorization"].startswith("Bearer "))
        self.assertEqual(len(hits), 2)

    def test_search_empty_messages_returns_empty(self) -> None:
        def fake_urlopen(req, timeout):  # noqa: ARG001
            return _fake_response({})

        with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            hits = self.client.search(query="zzz", max_results=10)
        self.assertEqual(hits, ())

    def test_search_invalid_messages_field_raises(self) -> None:
        def fake_urlopen(req, timeout):  # noqa: ARG001
            return _fake_response({"messages": "not-a-list"})

        with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(GmailClientError):
                self.client.search(query="x", max_results=10)


class ReadRequestShapeAndParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.creds = GmailCredentials.from_path(_write_creds(Path(self._td.name)))
        self.client = GmailClient(self.creds)

    def test_read_uses_format_full_and_escapes_id(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout):  # noqa: ARG001
            captured["url"] = req.full_url
            return _fake_response(
                {
                    "id": "abc/123",
                    "threadId": "t",
                    "snippet": "snip",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Hi"},
                            {"name": "From", "value": "x@y"},
                            {"name": "To", "value": "me"},
                            {"name": "Date", "value": "Wed, 1 Jan 2026"},
                        ],
                        "mimeType": "text/plain",
                        "body": {
                            "data": base64.urlsafe_b64encode(
                                b"hello body"
                            ).decode("ascii"),
                        },
                    },
                }
            )

        with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            m = self.client.read(message_id="abc/123")

        self.assertIn("abc%2F123", captured["url"])
        self.assertIn("format=full", captured["url"])
        self.assertEqual(m.subject, "Hi")
        self.assertEqual(m.body_text, "hello body")

    def test_parse_message_prefers_plain_part(self) -> None:
        plain = base64.urlsafe_b64encode(b"PLAIN").decode("ascii")
        html = base64.urlsafe_b64encode(b"<html>HTML</html>").decode("ascii")
        decoded = {
            "id": "x",
            "threadId": "t",
            "payload": {
                "headers": [{"name": "Subject", "value": "s"}],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": plain}},
                    {"mimeType": "text/html", "body": {"data": html}},
                ],
            },
        }
        m = _parse_message(decoded)
        self.assertEqual(m.body_text, "PLAIN")

    def test_parse_message_html_fallback_strips_tags(self) -> None:
        html = base64.urlsafe_b64encode(
            b"<html><body><p>Hello</p>"
            b"<script>alert(1)</script>"
            b"<style>p{}</style>"
            b"<p>World</p></body></html>"
        ).decode("ascii")
        decoded = {
            "id": "x",
            "threadId": "t",
            "payload": {
                "headers": [{"name": "Subject", "value": "s"}],
                "parts": [
                    {"mimeType": "text/html", "body": {"data": html}},
                ],
            },
        }
        m = _parse_message(decoded)
        self.assertIn("Hello", m.body_text)
        self.assertIn("World", m.body_text)
        self.assertNotIn("<script>", m.body_text)
        self.assertNotIn("<style>", m.body_text)
        self.assertNotIn("alert", m.body_text)

    def test_parse_message_collects_attachment_metadata_only(self) -> None:
        decoded = {
            "id": "x",
            "threadId": "t",
            "payload": {
                "headers": [{"name": "Subject", "value": "s"}],
                "parts": [
                    {
                        "filename": "report.pdf",
                        "mimeType": "application/pdf",
                        "body": {
                            "attachmentId": "a1",
                            "size": 9999,
                        },
                    },
                ],
            },
        }
        m = _parse_message(decoded)
        self.assertTrue(m.has_attachments)
        self.assertEqual(m.attachments[0].filename, "report.pdf")
        self.assertEqual(m.attachments[0].mime_type, "application/pdf")
        self.assertEqual(m.attachments[0].size_bytes, 9999)
        # Body of the attachment is never embedded in body_text.
        self.assertEqual(m.body_text, "")


class CreateDraftRequestShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.creds = GmailCredentials.from_path(_write_creds(Path(self._td.name)))
        self.client = GmailClient(self.creds)

    def test_create_draft_posts_base64url_raw(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout):  # noqa: ARG001
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _fake_response(
                {
                    "id": "draft_99",
                    "message": {"id": "msg_99", "threadId": "thr_99"},
                }
            )

        with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            draft = self.client.create_draft(
                to="alice@example.invalid",
                source_subject="Hello",
                body="Reply body",
                in_reply_to="<src@example.invalid>",
                references="<src@example.invalid>",
                thread_id="thr_99",
            )

        self.assertEqual(captured["method"], "POST")
        self.assertIn("/gmail/v1/users/me/drafts", captured["url"])
        headers_lc = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers_lc.get("content-type"), "application/json")
        msg = captured["body"]["message"]
        self.assertIn("raw", msg)
        # The raw payload base64url-decodes to RFC2822 text containing
        # the To, Subject, and body.
        raw_bytes = base64.urlsafe_b64decode(
            msg["raw"].encode("ascii") + b"=="
        )
        decoded_text = raw_bytes.decode("utf-8", errors="replace")
        self.assertIn("To: alice@example.invalid", decoded_text)
        self.assertIn("Subject: Re: Hello", decoded_text)
        self.assertIn("In-Reply-To: <src@example.invalid>", decoded_text)
        self.assertIn("Reply body", decoded_text)
        self.assertEqual(msg["threadId"], "thr_99")
        self.assertEqual(draft.draft_id, "draft_99")
        self.assertEqual(draft.message_id, "msg_99")


class ErrorMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.creds = GmailCredentials.from_path(_write_creds(Path(self._td.name)))
        self.client = GmailClient(self.creds)

    def test_http_error_maps_to_gmail_client_error(self) -> None:
        def fake_urlopen(req, timeout):  # noqa: ARG001
            raise urllib.error.HTTPError(
                url=req.full_url,
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=io.BytesIO(b"super-secret-token-leaked-by-server"),
            )

        with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(GmailClientError) as ctx:
                self.client.search(query="x", max_results=1)
        self.assertIn("HTTP 401", ctx.exception.label)
        # Error label must not embed response body content.
        self.assertNotIn("super-secret-token-leaked", ctx.exception.label)

    def test_url_error_maps_safely(self) -> None:
        def fake_urlopen(req, timeout):  # noqa: ARG001
            raise urllib.error.URLError("nope")

        with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(GmailClientError) as ctx:
                self.client.search(query="x", max_results=1)
        self.assertIn("network error", ctx.exception.label)

    def test_timeout_maps_safely(self) -> None:
        def fake_urlopen(req, timeout):  # noqa: ARG001
            raise socket.timeout("slow")

        with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(GmailClientError) as ctx:
                self.client.search(query="x", max_results=1)
        self.assertIn("timeout", ctx.exception.label)

    def test_invalid_json_response_maps_safely(self) -> None:
        class _Resp:
            def read(self) -> bytes:
                return b"<<<not json>>>"

            def __enter__(self):
                return self

            def __exit__(self, *_a) -> None:
                return None

        with patch.object(
            urllib.request, "urlopen", return_value=_Resp()
        ):
            with self.assertRaises(GmailClientError) as ctx:
                self.client.search(query="x", max_results=1)
        self.assertIn("invalid JSON", ctx.exception.label)


class TokenDoesNotLeakTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.creds = GmailCredentials.from_path(
            _write_creds(Path(self._td.name), token="super-secret-token")
        )

    def test_client_repr_does_not_contain_token(self) -> None:
        c = GmailClient(self.creds)
        self.assertNotIn("super-secret-token", repr(c))

    def test_token_not_in_error_label(self) -> None:
        c = GmailClient(self.creds)

        def fake_urlopen(req, timeout):  # noqa: ARG001
            raise urllib.error.HTTPError(
                url=req.full_url,
                code=403,
                msg="Forbidden",
                hdrs=None,
                fp=None,
            )

        with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            try:
                c.search(query="x", max_results=1)
            except GmailClientError as exc:
                self.assertNotIn("super-secret-token", exc.label)
                self.assertNotIn("super-secret-token", repr(exc))


class DraftBuilderTest(unittest.TestCase):
    """Lightweight checks for build_reply_draft_raw."""

    def test_subject_re_prefix_added(self) -> None:
        raw = build_reply_draft_raw(
            to="a@b", source_subject="Hi", body="x"
        )
        decoded = base64.urlsafe_b64decode(
            raw.encode("ascii") + b"=="
        ).decode("utf-8", errors="replace")
        self.assertIn("Subject: Re: Hi", decoded)

    def test_existing_re_prefix_not_doubled(self) -> None:
        raw = build_reply_draft_raw(
            to="a@b", source_subject="Re: Hi", body="x"
        )
        decoded = base64.urlsafe_b64decode(
            raw.encode("ascii") + b"=="
        ).decode("utf-8", errors="replace")
        self.assertIn("Subject: Re: Hi", decoded)
        self.assertNotIn("Subject: Re: Re:", decoded)

    def test_empty_subject_becomes_re(self) -> None:
        raw = build_reply_draft_raw(
            to="a@b", source_subject="", body="x"
        )
        decoded = base64.urlsafe_b64decode(
            raw.encode("ascii") + b"=="
        ).decode("utf-8", errors="replace")
        self.assertIn("Subject: Re:", decoded)


if __name__ == "__main__":
    unittest.main()
