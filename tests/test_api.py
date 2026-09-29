"""API contract tests against a real server on a free port, using the mock LLM."""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from catalogiq.__main__ import build
from catalogiq.config import Config
from http.server import BaseHTTPRequestHandler

from catalogiq.server import Server, make_servers


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = Config(db_path=os.path.join(cls.tmp.name, "api.db"), host="localhost", port=0,
                        mock_latency_ms=20, mock_failure_rate=0.0, retry_base_delay_ms=1)
        cls.app, cls.runner = build(config)
        cls.servers = make_servers(cls.app)
        cls.port = cls.servers[0].server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        for s in cls.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.shutdown()
            s.server_close()
        cls.runner.shutdown(wait=True)
        cls.app.db.close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        hdrs = {"Content-Type": "application/json"} if data is not None else {}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def wait_for_job(self, job_id, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, job = self.request("GET", f"/api/jobs/{job_id}")
            if job["status"] == "completed":
                return job
            time.sleep(0.05)
        self.fail("job did not complete")

    # ---------------------------------------------------------------- tests

    def test_health(self):
        status, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "llm_provider": "mock", "llm_concurrency": 5})

    def test_metrics_are_integers(self):
        status, body = self.request("GET", "/api/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"llm_calls_total", "llm_errors_total",
                                     "max_concurrent_llm_calls"})
        self.assertTrue(all(isinstance(v, int) for v in body.values()))

    def test_job_lifecycle_and_products(self):
        products = [{"sku": f"API{i:03d}", "raw_title": f"Tata salt {i} kg"} for i in range(30)]
        products.append({"sku": "API999", "raw_title": " AMUL   butter 500G",
                         "raw_description": "pck of 2"})
        t0 = time.time()
        status, job = self.request("POST", "/api/jobs", {"products": products})
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual(status, 202)
        self.assertEqual(job["total"], 31)
        self.assertIn(job["status"], ("queued", "running"))
        for key in ("id", "done", "failed", "cache_hits", "created_at", "started_at",
                    "finished_at"):
            self.assertIn(key, job)

        # API stays fast while the job runs.
        t0 = time.time()
        self.assertEqual(self.request("GET", "/api/health")[0], 200)
        self.assertLess(time.time() - t0, 0.5)

        job = self.wait_for_job(job["id"])
        self.assertEqual(job["done"], 31)
        self.assertIsNotNone(job["finished_at"])

        status, p = self.request("GET", "/api/products/API999")
        self.assertEqual(status, 200)
        self.assertEqual(p["category"], "Groceries")
        self.assertEqual(p["brand"], "Amul")
        self.assertEqual(p["status"], "enriched")
        self.assertIsNone(p["error"])
        self.assertEqual(set(p), {"sku", "raw_title", "raw_description", "clean_title",
                                  "category", "brand", "tags", "status", "error"})

        status, page = self.request("GET", "/api/products?page=1&page_size=10&q=TATA")
        self.assertEqual(status, 200)
        self.assertEqual((page["page"], page["page_size"], page["total"]), (1, 10, 30))
        skus = [i["sku"] for i in page["items"]]
        self.assertEqual(skus, sorted(skus))
        self.assertEqual(skus[0], "API000")

        _, page2 = self.request("GET", "/api/products?page=2&page_size=10&q=tata")
        self.assertEqual(page2["items"][0]["sku"], "API010")

        _, cat = self.request("GET", "/api/products?category=Groceries&page_size=500")
        self.assertEqual(cat["page_size"], 100)
        self.assertTrue(all(i["category"] == "Groceries" for i in cat["items"]))

        # PATCH: edit + approve
        status, p = self.request("PATCH", "/api/products/API999",
                                 {"clean_title": "Amul Butter 500 g (Pack of 2)",
                                  "tags": ["Butter", "dairy"]})
        self.assertEqual(status, 200)
        self.assertEqual(p["status"], "approved")
        self.assertEqual(p["tags"], ["butter", "dairy"])
        self.assertEqual(p["clean_title"], "Amul Butter 500 g (Pack of 2)")

    def test_resubmitting_sku_updates_it(self):
        _, job = self.request("POST", "/api/jobs", {"products": [{"sku": "UPD", "raw_title": "Dove soap"}]})
        self.wait_for_job(job["id"])
        _, job = self.request("POST", "/api/jobs", {"products": [{"sku": "UPD", "raw_title": "Boat earbuds"}]})
        self.wait_for_job(job["id"])
        _, p = self.request("GET", "/api/products/UPD")
        self.assertEqual((p["raw_title"], p["category"]), ("Boat earbuds", "Electronics"))

    def test_job_validation_errors(self):
        cases = [
            {"products": []},
            {"products": [{"raw_title": "no sku"}]},
            {"products": [{"sku": "X"}]},
            {"products": [{"sku": "X", "raw_title": "   "}]},
            {"products": [{"sku": "", "raw_title": "t"}]},
            {"products": [{"sku": "X", "raw_title": "t", "price": 1}]},
            {"products": "nope"},
            {},
        ]
        for body in cases:
            with self.subTest(body=body):
                status, err = self.request("POST", "/api/jobs", body)
                self.assertEqual(status, 400)
                self.assertIsInstance(err["error"], str)

    def test_invalid_json_body(self):
        status, err = self.request("POST", "/api/jobs", raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("error", err)

    def test_non_json_content_type_rejected(self):
        status, _ = self.request("POST", "/api/jobs", raw=b'{"products": []}',
                                 headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_unknown_job_and_product(self):
        self.assertEqual(self.request("GET", "/api/jobs/j_doesnotexist")[0], 404)
        self.assertEqual(self.request("GET", "/api/products/NOPE")[0], 404)
        self.assertEqual(self.request("PATCH", "/api/products/NOPE", {"clean_title": "x"})[0], 404)

    def test_bad_pagination(self):
        for qs in ("page=abc", "page_size=x", "page=0", "page=-1", "page_size=0", "page=1.5"):
            with self.subTest(qs=qs):
                status, err = self.request("GET", f"/api/products?{qs}")
                self.assertEqual(status, 400)
                self.assertIn("error", err)

    def test_patch_validation(self):
        _, job = self.request("POST", "/api/jobs", {"products": [{"sku": "PV", "raw_title": "Lux soap"}]})
        self.wait_for_job(job["id"])
        for body in ({"category": "Toys"}, {"clean_title": ""}, {"clean_title": "   "},
                     {"tags": "a,b"}, {"tags": ["a", "b", "c", "d", "e", "f"]},
                     {"brand": "Other"}, {"status": "enriched"}):
            with self.subTest(body=body):
                status, err = self.request("PATCH", "/api/products/PV", body)
                self.assertEqual(status, 400)
                self.assertIn("error", err)
        self.assertEqual(self.request("GET", "/api/products/PV")[1]["status"], "enriched")

    def test_foreign_host_header_rejected(self):
        status, _ = self.request("GET", "/api/health", headers={"Host": "evil.example:8000"})
        self.assertEqual(status, 403)

    def test_localhost_resolves_quickly(self):
        # On Windows "localhost" tries ::1 first; if only IPv4 were served, each
        # request would stall ~2 s. Both loopbacks are served, so this is fast.
        t0 = time.time()
        with urllib.request.urlopen(f"http://localhost:{self.port}/api/health", timeout=5) as r:
            self.assertEqual(r.status, 200)
        self.assertLess(time.time() - t0, 0.5)

    def test_second_server_cannot_take_the_port(self):
        # Windows would allow this with SO_REUSEADDR; SO_EXCLUSIVEADDRUSE forbids it.
        with self.assertRaises(OSError):
            Server(("127.0.0.1", self.port), BaseHTTPRequestHandler).server_close()

    def test_unknown_route_and_method(self):
        self.assertEqual(self.request("GET", "/api/nope")[0], 404)
        self.assertEqual(self.request("DELETE", "/api/products/X")[0], 405)

    def test_frontend_served_with_security_headers(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("default-src 'self'", resp.headers["Content-Security-Policy"])
            self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn(b"CatalogIQ", resp.read())


if __name__ == "__main__":
    unittest.main()
