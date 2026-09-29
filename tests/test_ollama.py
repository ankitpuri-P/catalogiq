"""OllamaProvider against a fake Ollama server (no GPU or Ollama install needed)."""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from catalogiq.config import ConfigError, load_config
from catalogiq.enrichment import CATEGORIES, parse_enrichment
from catalogiq.llm import LLMError, OllamaProvider

GOOD = {"clean_title": "Amul Butter 500 g", "category": "Groceries", "brand": "Amul",
        "tags": ["butter"]}


class FakeOllama(BaseHTTPRequestHandler):
    requests = []
    reply = {}
    status = 200
    models = ["qwen2.5:7b"]

    def log_message(self, *args):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._json(200, {"models": [{"name": n} for n in self.models]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.requests.append((self.path, body))
        if FakeOllama.status != 200:
            return self._json(FakeOllama.status, {"error": "model not found"})
        self._json(200, FakeOllama.reply)


class TestOllamaProvider(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeOllama.requests = []
        FakeOllama.status = 200
        FakeOllama.reply = {"message": {"content": json.dumps(GOOD)}, "done_reason": "stop"}
        self.provider = OllamaProvider(self.url, "qwen2.5:7b", timeout_s=5)

    def test_request_shape(self):
        out = parse_enrichment(self.provider.complete("AMUL butter 500G", "pck of 2"))
        self.assertEqual(out["brand"], "Amul")
        path, body = FakeOllama.requests[0]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(body["model"], "qwen2.5:7b")
        self.assertFalse(body["stream"])
        self.assertEqual(body["keep_alive"], "30m")
        self.assertEqual(body["options"], {"temperature": 0, "num_ctx": 1024, "num_predict": 256})
        # Category is constrained by the schema, not just by the prompt.
        self.assertEqual(body["format"]["properties"]["category"]["enum"], list(CATEGORIES))
        self.assertIn("AMUL butter 500G", body["messages"][0]["content"])

    def test_long_listing_is_clipped_in_prompt(self):
        self.provider.complete("t" * 5000, "d" * 5000)
        prompt = FakeOllama.requests[0][1]["messages"][0]["content"]
        self.assertLess(len(prompt), 2500)  # stays well inside a 1024-token context

    def test_missing_model_error_says_how_to_fix(self):
        FakeOllama.status = 404
        with self.assertRaises(LLMError) as ctx:
            self.provider.complete("x", None)
        self.assertIn("ollama pull qwen2.5:7b", str(ctx.exception))

    def test_truncated_output_is_a_failed_attempt(self):
        FakeOllama.reply = {"message": {"content": '{"clean_title": "Am'}, "done_reason": "length"}
        with self.assertRaises(LLMError):
            self.provider.complete("x", None)

    def test_unreachable_server(self):
        dead = OllamaProvider("http://127.0.0.1:9", "qwen2.5:7b", timeout_s=2)
        with self.assertRaises(LLMError):
            dead.complete("x", None)
        self.assertIn("is Ollama running", dead.check())

    def test_check_reports_missing_model(self):
        self.assertIsNone(self.provider.check())
        other = OllamaProvider(self.url, "llama3.2:3b", timeout_s=5)
        self.assertIn("ollama pull llama3.2:3b", other.check())

    def test_warm_up_loads_model(self):
        FakeOllama.reply = {"done": True}
        self.provider.warm_up()
        path, body = FakeOllama.requests[0]
        self.assertEqual((path, body["model"], body["keep_alive"]), ("/api/generate", "qwen2.5:7b", "30m"))


class TestOllamaConfig(unittest.TestCase):
    def load(self, **env):
        with mock.patch.dict(os.environ, env, clear=True):
            return load_config()

    def test_defaults_suit_a_6gb_gpu(self):
        c = self.load()
        self.assertEqual((c.ollama_model, c.ollama_num_ctx, c.ollama_keep_alive),
                         ("qwen2.5:7b", 1024, "30m"))
        self.assertEqual(c.ollama_url, "http://127.0.0.1:11434")

    def test_rejects_bad_values(self):
        for env in ({"OLLAMA_NUM_CTX": "abc"}, {"OLLAMA_NUM_CTX": "100"},
                    {"OLLAMA_KEEP_ALIVE": "forever"}, {"OLLAMA_MODEL": "a b"},
                    {"OLLAMA_URL": "file:///etc/passwd"}, {"LLM_PROVIDER": "gpt"}):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                self.load(**env)


if __name__ == "__main__":
    unittest.main()
