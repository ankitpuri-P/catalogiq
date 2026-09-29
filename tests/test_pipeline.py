"""Pipeline tests: concurrency limit, retries, validation, de-duplication.

All run against the mock provider (or a scripted fake with the same interface),
with short latencies so the suite finishes in a few seconds.
"""

import json
import os
import tempfile
import threading
import time
import unittest

from catalogiq.db import Database
from catalogiq.enrichment import CATEGORIES, content_key, normalize_content, parse_enrichment
from catalogiq.llm import LLMError, MockProvider
from catalogiq.pipeline import MAX_ATTEMPTS, EnrichmentFailed, Enricher, JobRunner

VALID = json.dumps({"clean_title": "Amul Butter 500 g", "category": "Groceries",
                    "brand": "Amul", "tags": ["butter"]})


class ScriptedProvider:
    """Returns (or raises) the scripted responses in order; counts concurrency."""

    def __init__(self, responses, latency_s=0.0):
        self.responses = list(responses)
        self.latency_s = latency_s
        self.calls = 0
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def complete(self, raw_title, raw_description):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            response = self.responses.pop(0) if self.responses else VALID
        try:
            time.sleep(self.latency_s)
            if isinstance(response, Exception):
                raise response
            return response
        finally:
            with self._lock:
                self.active -= 1


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def enricher(self, provider, concurrency=5, sleep=None):
        kwargs = {"sleep": sleep} if sleep else {}
        return Enricher(provider, self.db, concurrency, retry_base_delay_s=0.001, **kwargs)

    def run_job(self, enricher, products, workers=40, timeout=20):
        runner = JobRunner(self.db, enricher, workers)
        job = runner.submit(products)
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.db.get_job(job["id"])
            if job["status"] == "completed":
                break
            time.sleep(0.01)
        runner.shutdown()
        self.assertEqual(job["status"], "completed")
        return job


class TestConcurrencyLimit(PipelineTestCase):
    def test_never_exceeds_limit_and_reaches_it(self):
        provider = ScriptedProvider([], latency_s=0.03)
        enricher = self.enricher(provider, concurrency=3)
        products = [{"sku": f"S{i}", "raw_title": f"product {i}"} for i in range(30)]
        job = self.run_job(enricher, products, workers=30)
        self.assertEqual(job["done"], 30)
        self.assertLessEqual(provider.peak, 3)
        self.assertEqual(enricher.metrics.max_concurrent_llm_calls, 3)
        self.assertEqual(enricher.metrics.llm_calls_total, 30)

    def test_limit_is_shared_across_jobs(self):
        provider = ScriptedProvider([], latency_s=0.03)
        enricher = self.enricher(provider, concurrency=2)
        runner = JobRunner(self.db, enricher, 40)
        jobs = [runner.submit([{"sku": f"J{j}-{i}", "raw_title": f"job {j} item {i}"}
                               for i in range(10)]) for j in range(3)]
        runner.shutdown(wait=True)
        for job in jobs:
            self.assertEqual(self.db.get_job(job["id"])["status"], "completed")
        self.assertLessEqual(provider.peak, 2)
        self.assertEqual(enricher.metrics.max_concurrent_llm_calls, 2)

    def test_backoff_does_not_hold_a_slot(self):
        # One item keeps failing and backing off; with concurrency 1 the other
        # items must still make progress while it sleeps.
        slept = []
        provider = ScriptedProvider([LLMError("x")] * 3)
        def sleep(s):
            slept.append(s)
            time.sleep(0.05)
        enricher = self.enricher(provider, concurrency=1, sleep=sleep)
        products = [{"sku": f"S{i}", "raw_title": f"item {i}"} for i in range(5)]
        job = self.run_job(enricher, products, workers=5)
        self.assertEqual(job["failed"], 0)
        self.assertEqual(enricher.metrics.max_concurrent_llm_calls, 1)


class TestRetries(PipelineTestCase):
    def test_succeeds_after_transient_failures(self):
        provider = ScriptedProvider([LLMError("boom"), LLMError("boom"), VALID])
        enricher = self.enricher(provider)
        result, hit = enricher.enrich("amul butter", None)
        self.assertEqual(result["category"], "Groceries")
        self.assertFalse(hit)
        self.assertEqual(provider.calls, 3)
        self.assertEqual(enricher.metrics.llm_calls_total, 3)
        self.assertEqual(enricher.metrics.llm_errors_total, 2)

    def test_gives_up_after_four_attempts(self):
        provider = ScriptedProvider([LLMError("down")] * 10)
        enricher = self.enricher(provider)
        with self.assertRaises(EnrichmentFailed) as ctx:
            enricher.enrich("amul butter", None)
        self.assertIn("down", str(ctx.exception))
        self.assertEqual(provider.calls, MAX_ATTEMPTS)
        self.assertEqual(enricher.metrics.llm_calls_total, 4)
        self.assertEqual(enricher.metrics.llm_errors_total, 4)

    def test_exponential_backoff_delays(self):
        delays = []
        provider = ScriptedProvider([LLMError("x")] * 4)
        enricher = Enricher(provider, self.db, 5, retry_base_delay_s=0.2, sleep=delays.append)
        with self.assertRaises(EnrichmentFailed):
            enricher.enrich("t", None)
        self.assertEqual(len(delays), 3)
        for got, want in zip(delays, [0.2, 0.4, 0.8]):
            self.assertAlmostEqual(got, want, delta=want * 0.1 + 1e-9)

    def test_failed_item_does_not_stop_job(self):
        provider = ScriptedProvider([LLMError("x")] * 4)  # first item's 4 attempts fail
        enricher = self.enricher(provider, concurrency=1)
        products = [{"sku": f"S{i}", "raw_title": f"item {i}"} for i in range(5)]
        job = self.run_job(enricher, products, workers=1)
        self.assertEqual(job["done"], 5)
        self.assertEqual(job["failed"], 1)
        failed = self.db.get_product("S0")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("4 attempts", failed["error"])
        self.assertEqual(self.db.get_product("S1")["status"], "enriched")

    def test_mock_failure_rate_one_marks_all_failed(self):
        enricher = self.enricher(MockProvider(latency_ms=0, failure_rate=1.0))
        products = [{"sku": f"S{i}", "raw_title": f"item {i}"} for i in range(4)]
        job = self.run_job(enricher, products)
        self.assertEqual(job["failed"], 4)
        self.assertEqual(enricher.metrics.llm_calls_total, 16)
        self.assertEqual(enricher.metrics.llm_errors_total, 16)


class TestValidation(PipelineTestCase):
    def test_invalid_json_is_a_failed_attempt(self):
        provider = ScriptedProvider(["not json", VALID])
        enricher = self.enricher(provider)
        result, _ = enricher.enrich("x", None)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(enricher.metrics.llm_errors_total, 1)
        self.assertEqual(result["brand"], "Amul")

    def test_unknown_category_is_a_failed_attempt(self):
        bad = json.dumps({"clean_title": "X", "category": "Toys", "brand": None, "tags": []})
        provider = ScriptedProvider([bad, bad, bad, bad])
        with self.assertRaises(EnrichmentFailed):
            self.enricher(provider).enrich("x", None)
        self.assertEqual(provider.calls, 4)

    def test_parse_normalizes_tags(self):
        r = parse_enrichment(json.dumps({"clean_title": " A  b ", "category": "Other",
                                         "brand": "", "tags": ["A", "a", "B", "c", "d", "e", "f"]}))
        self.assertEqual(r, {"clean_title": "A b", "category": "Other", "brand": None,
                             "tags": ["a", "b", "c", "d", "e"]})

    def test_mock_output_is_always_valid(self):
        mock = MockProvider(latency_ms=0, failure_rate=0.0)
        r = parse_enrichment(mock.complete(" AMUL   butter 500G", "pck of 2"))
        self.assertIn(r["category"], CATEGORIES)
        self.assertEqual(r["brand"], "Amul")
        self.assertEqual(r["category"], "Groceries")
        self.assertLessEqual(len(r["tags"]), 5)


class TestDeduplication(PipelineTestCase):
    def test_content_normalization(self):
        self.assertEqual(normalize_content("  AMUL   Butter ", "Pck\tof 2"), "amul butter pck of 2")
        self.assertEqual(content_key("AMUL butter", "pck of 2"),
                         content_key("amul   BUTTER ", " pck of 2"))
        self.assertNotEqual(content_key("amul butter", None), content_key("amul butter", "x"))

    def test_concurrent_duplicates_make_one_call(self):
        provider = ScriptedProvider([], latency_s=0.2)
        enricher = self.enricher(provider, concurrency=5)
        products = [{"sku": f"D{i}", "raw_title": ("AMUL butter" if i % 2 else "  amul   BUTTER"),
                     "raw_description": "pck of 2"} for i in range(10)]
        job = self.run_job(enricher, products, workers=10)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(job["done"], 10)
        self.assertEqual(job["cache_hits"], 9)
        self.assertEqual(enricher.metrics.llm_calls_total, 1)
        for i in range(10):
            self.assertEqual(self.db.get_product(f"D{i}")["clean_title"], "Amul Butter 500 g")

    def test_waiters_share_the_leader_failure(self):
        provider = ScriptedProvider([LLMError("x")] * 4, latency_s=0.05)
        enricher = self.enricher(provider)
        products = [{"sku": f"D{i}", "raw_title": "same"} for i in range(5)]
        job = self.run_job(enricher, products, workers=5)
        self.assertEqual(provider.calls, 4)  # one leader x 4 attempts, not 5 x 4
        self.assertEqual(job["failed"], 5)
        self.assertEqual(job["cache_hits"], 0)

    def test_cache_survives_across_jobs_and_restart(self):
        provider = ScriptedProvider([])
        self.run_job(self.enricher(provider), [{"sku": "A", "raw_title": "Tata salt 1kg"}])
        self.assertEqual(provider.calls, 1)
        # A fresh Database + Enricher on the same file simulates a restart.
        self.db.close()
        self.db = Database(self.db.path)
        provider2 = ScriptedProvider([])
        job = self.run_job(self.enricher(provider2), [{"sku": "B", "raw_title": "TATA  SALT 1KG"}])
        self.assertEqual(provider2.calls, 0)
        self.assertEqual(job["cache_hits"], 1)

    def test_failures_are_not_cached(self):
        provider = ScriptedProvider([LLMError("x")] * 4)
        enricher = self.enricher(provider)
        with self.assertRaises(EnrichmentFailed):
            enricher.enrich("retry me", None)
        result, hit = enricher.enrich("retry me", None)
        self.assertFalse(hit)
        self.assertEqual(provider.calls, 5)


class TestJobs(PipelineTestCase):
    def test_resume_after_restart_skips_finished_items(self):
        products = [{"sku": f"S{i}", "raw_title": f"item {i}"} for i in range(6)]
        job = self.db.create_job("j_000000000001", products)
        # Pretend the server died after two items were recorded.
        for item in self.db.pending_items(job["id"])[:2]:
            self.db.finish_item(item, content_key(item["raw_title"], None),
                                parse_enrichment(VALID), None, False)
        provider = ScriptedProvider([])
        runner = JobRunner(self.db, self.enricher(provider), 4)
        self.assertEqual(runner.resume_unfinished(), 4)
        runner.shutdown(wait=True)
        job = self.db.get_job(job["id"])
        self.assertEqual((job["status"], job["done"]), ("completed", 6))
        self.assertEqual(provider.calls, 4)

    def test_finish_item_is_idempotent(self):
        job = self.db.create_job("j_000000000002", [{"sku": "S", "raw_title": "t"}])
        item = self.db.pending_items(job["id"])[0]
        for _ in range(2):
            self.db.finish_item(item, "k", parse_enrichment(VALID), None, False)
        self.assertEqual(self.db.get_job(job["id"])["done"], 1)

    def test_reprocessing_keeps_approved_edits_for_same_content(self):
        provider = ScriptedProvider([])
        self.run_job(self.enricher(provider), [{"sku": "A", "raw_title": "amul butter"}])
        self.db.update_product("A", {"clean_title": "Human Title"})
        self.run_job(self.enricher(provider), [{"sku": "A", "raw_title": "AMUL butter"}])
        p = self.db.get_product("A")
        self.assertEqual((p["clean_title"], p["status"]), ("Human Title", "approved"))
        self.run_job(self.enricher(provider), [{"sku": "A", "raw_title": "amul cheese"}])
        self.assertEqual(self.db.get_product("A")["status"], "enriched")


if __name__ == "__main__":
    unittest.main()
