"""Unit tests for the WireLog client. Uses only stdlib (no pytest)."""

from __future__ import annotations

import json
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

from wirelog import WireLog


class MockHandler(BaseHTTPRequestHandler):
    """Simple mock WireLog API server."""

    last_request: dict[str, Any] = {}
    response_body: dict[str, Any] = {"accepted": 1}
    response_status: int = 200

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        MockHandler.last_request = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(body) if body else {},
        }
        self.send_response(MockHandler.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(MockHandler.response_body).encode())

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress request logging


class TestWireLogClient(unittest.TestCase):
    server: HTTPServer
    thread: Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), MockHandler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def _client(self) -> WireLog:
        port = self.server.server_address[1]
        return WireLog(api_key="sk_test_key", host=f"http://127.0.0.1:{port}")

    def test_track_sends_event(self) -> None:
        MockHandler.response_body = {"accepted": 1}
        MockHandler.response_status = 200
        client = self._client()

        result = client.track("signup", user_id="u_123", event_properties={"plan": "free"})

        self.assertEqual(result, {"accepted": 1})
        self.assertEqual(MockHandler.last_request["path"], "/track")
        self.assertEqual(MockHandler.last_request["body"]["event_type"], "signup")
        self.assertEqual(MockHandler.last_request["body"]["user_id"], "u_123")
        self.assertEqual(
            MockHandler.last_request["body"]["event_properties"], {"plan": "free"}
        )
        self.assertIn("insert_id", MockHandler.last_request["body"])
        self.assertIn("time", MockHandler.last_request["body"])

    def test_track_batch(self) -> None:
        MockHandler.response_body = {"accepted": 2}
        MockHandler.response_status = 200
        client = self._client()

        events = [
            {"event_type": "page_view", "user_id": "u_1"},
            {"event_type": "click", "user_id": "u_2"},
        ]
        result = client.track_batch(
            events,
            origin="client",
            client_originated=True,
        )

        self.assertEqual(result, {"accepted": 2})
        self.assertEqual(MockHandler.last_request["body"]["events"], events)
        self.assertEqual(MockHandler.last_request["body"]["origin"], "client")
        self.assertEqual(
            MockHandler.last_request["body"]["clientOriginated"], True
        )

    def test_track_with_origin_hints(self) -> None:
        MockHandler.response_body = {"accepted": 1}
        MockHandler.response_status = 200
        client = self._client()

        result = client.track(
            "ai_usage_charged",
            user_id="u_123",
            origin="server",
            client_originated=False,
        )

        self.assertEqual(result, {"accepted": 1})
        self.assertEqual(MockHandler.last_request["body"]["origin"], "server")
        self.assertEqual(
            MockHandler.last_request["body"]["clientOriginated"], False
        )

    def test_query(self) -> None:
        MockHandler.response_body = {"rows": [{"count": 42}]}
        MockHandler.response_status = 200
        client = self._client()

        result = client.query("* | last 7d | count")

        self.assertEqual(result, {"rows": [{"count": 42}]})
        self.assertEqual(MockHandler.last_request["path"], "/query")
        self.assertEqual(
            MockHandler.last_request["body"]["q"], "* | last 7d | count"
        )
        self.assertEqual(MockHandler.last_request["body"]["format"], "llm")

    def test_identify(self) -> None:
        MockHandler.response_body = {"ok": True}
        MockHandler.response_status = 200
        client = self._client()

        result = client.identify(
            "alice@acme.org",
            device_id="dev_123",
            user_properties={"email": "alice@acme.org", "plan": "pro"},
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(MockHandler.last_request["path"], "/identify")
        self.assertEqual(
            MockHandler.last_request["body"]["user_id"], "alice@acme.org"
        )
        self.assertEqual(
            MockHandler.last_request["body"]["device_id"], "dev_123"
        )

    def test_api_key_header(self) -> None:
        MockHandler.response_body = {"accepted": 1}
        MockHandler.response_status = 200
        client = self._client()

        client.track("test")

        self.assertEqual(
            MockHandler.last_request["headers"]["X-Api-Key"], "sk_test_key"
        )

    def test_constructor_defaults(self) -> None:
        client = WireLog()
        self.assertEqual(client.host, "https://api.wirelog.ai")
        self.assertEqual(client.api_key, "")

    def test_host_trailing_slash_stripped(self) -> None:
        client = WireLog(api_key="sk_test", host="https://api.wirelog.ai/")
        self.assertEqual(client.host, "https://api.wirelog.ai")


if __name__ == "__main__":
    unittest.main()
