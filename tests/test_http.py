"""End-to-end tests over a real socket: the wire format itself is under test.

A mocked transport would not catch a wrong URL path, a missing header, or a
response shape the extractor cannot read.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from served_collapse_guard.core import probe_model, probe_once

SEEN = []


class Handler(BaseHTTPRequestHandler):
    served_model = "gemini-2-5-flash"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        SEEN.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "model": body.get("model"),
                "max_tokens": body.get("max_tokens"),
            }
        )
        payload = {"model": self.served_model, "metadata": {"served_model": self.served_model}}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class LiveServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.endpoint = f"127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        SEEN.clear()
        Handler.served_model = "gemini-2-5-flash"

    def test_probe_reads_served_model_off_the_wire(self):
        result = probe_once(self.endpoint, "claude-sonnet-4-6", timeout=5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["served"], "gemini-2-5-flash")
        self.assertEqual(SEEN[0]["path"], "/v1/chat/completions")
        self.assertEqual(SEEN[0]["max_tokens"], 10)

    def test_collapse_detected_end_to_end(self):
        result = probe_model(self.endpoint, "claude-sonnet-4-6", samples=1, timeout=5)
        self.assertEqual(result.collapsed_kind, "cross-vendor")

    def test_no_key_means_no_authorization_header(self):
        probe_once(self.endpoint, "gpt-4o", timeout=5)
        self.assertIsNone(SEEN[0]["auth"])

    def test_api_key_is_sent_as_bearer(self):
        probe_once(self.endpoint, "gpt-4o", api_key="sk-test-123", timeout=5)
        self.assertEqual(SEEN[0]["auth"], "Bearer sk-test-123")

    def test_extra_header_reaches_the_server(self):
        probe_once(self.endpoint, "gpt-4o", headers={"X-Pool": "blue"}, timeout=5)
        self.assertEqual(SEEN[0]["path"], "/v1/chat/completions")

    def test_stability_over_repeated_samples(self):
        result = probe_model(self.endpoint, "gemini-2-5-flash", samples=3, timeout=5)
        self.assertTrue(result.stable)
        self.assertFalse(result.collapsed)
        self.assertEqual(len(SEEN), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
