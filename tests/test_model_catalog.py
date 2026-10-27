from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from model_client import (
    MAX_MODEL_RESPONSE_BYTES,
    ModelClientError,
    _models_endpoint,
    list_models,
)


class CatalogHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.calls.append((self.path, self.headers.get("Authorization")))
        if self.path == "/redirect/models":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/stolen")
            self.end_headers()
            return
        if self.path == "/unauthorized/models":
            self._reply(401, b"echo sk-test-secret")
            return
        if self.path == "/forbidden/models":
            self._reply(403, b"echo sk-test-secret")
            return
        if self.path == "/missing/models":
            self._reply(404, b"echo sk-test-secret")
            return
        if self.path == "/large/models":
            self._reply(200, b"x" * (MAX_MODEL_RESPONSE_BYTES + 1))
            return
        if self.path == "/many/models":
            self._json({"data": [{"id": f"m{i}"} for i in range(2501)]})
            return
        if self.path == "/bad/models":
            self._json({"items": []})
            return
        if self.path == "/empty/models":
            self._json({"data": []})
            return
        self._json({"data": [
            {"id": "model-a", "name": "Model A"},
            {"id": "model-a", "name": "Duplicate"},
            {"id": "model-b"},
            {"id": "sk-test-secret"},
            {"id": "model-c", "name": "echo sk-test-secret"},
            {"id": "model-d\nline"},
            {"id": "x" * 201},
            {"id": "model-e", "name": "bad\nname"},
            {"id": 123},
        ]})

    def _json(self, payload):
        self._reply(200, json.dumps(payload).encode("utf-8"))

    def _reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ModelCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler)
        cls.server.calls = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.server.calls.clear()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.secret = "sk-test-secret"

    def config(self, path=""):
        return {"base_url": self.base + path, "api_key": self.secret}

    def test_get_auth_prefix_normalization_dedup_and_no_key_echo(self):
        for suffix in ("/v1", "/v1/chat/completions", "/v1/models"):
            with self.subTest(suffix=suffix):
                models = list_models(self.config(suffix))
                self.assertEqual(self.server.calls[-1], ("/v1/models", f"Bearer {self.secret}"))
                self.assertEqual(models, [
                    {"id": "model-a", "name": "Model A"},
                    {"id": "model-b", "name": "model-b"},
                    {"id": "model-c", "name": "model-c"},
                    {"id": "model-e", "name": "model-e"},
                ])
                self.assertNotIn(self.secret, json.dumps(models))

    def test_redirect_is_not_followed(self):
        with self.assertRaisesRegex(ModelClientError, "HTTP 302"):
            list_models(self.config("/redirect"))
        self.assertEqual([path for path, _ in self.server.calls], ["/redirect/models"])

    def test_http_failures_do_not_echo_key_or_body(self):
        for route, status in (("/unauthorized", 401), ("/forbidden", 403), ("/missing", 404)):
            with self.subTest(route=route):
                with self.assertRaises(ModelClientError) as caught:
                    list_models(self.config(route))
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn(self.secret, str(caught.exception))
                if status in (401, 403):
                    self.assertIn("检查 API Key", str(caught.exception))

    def test_empty_malformed_large_and_row_limit(self):
        self.assertEqual(list_models(self.config("/empty")), [])
        for route in ("/bad", "/large", "/many"):
            with self.subTest(route=route), self.assertRaises(ModelClientError):
                list_models(self.config(route))

    def test_address_validation_and_siliconflow_chat_filter(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            list_models({"base_url": "http://example.com/v1", "api_key": self.secret})
        with self.assertRaisesRegex(ValueError, "查询参数"):
            list_models({"base_url": "https://example.com/v1?key=secret", "api_key": self.secret})
        self.assertEqual(
            _models_endpoint("https://api.siliconflow.cn/v1/chat/completions"),
            "https://api.siliconflow.cn/v1/models?sub_type=chat",
        )


if __name__ == "__main__":
    unittest.main()
