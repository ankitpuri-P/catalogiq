"""The enrichment pipeline: concurrency limit, retries, de-duplication, metrics.

Threading model
---------------
Items run on a shared ThreadPoolExecutor. The pool is larger than the LLM limit
on purpose, because some threads are just waiting (on a retry backoff, or on a
duplicate that is already in flight). The LLM limit itself is one
threading.BoundedSemaphore shared by every job; a thread holds it only for the
duration of a single provider call, never while sleeping between retries.
"""

import logging
import random
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from .enrichment import ValidationError, content_key, parse_enrichment
from .llm import LLMError

log = logging.getLogger("catalogiq.pipeline")

MAX_ATTEMPTS = 4  # 1 call + 3 retries


class EnrichmentFailed(Exception):
    """All attempts for one piece of content failed."""


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.llm_calls_total = 0
        self.llm_errors_total = 0
        self.max_concurrent_llm_calls = 0
        self._in_progress = 0

    def call_started(self):
        with self._lock:
            self.llm_calls_total += 1
            self._in_progress += 1
            if self._in_progress > self.max_concurrent_llm_calls:
                self.max_concurrent_llm_calls = self._in_progress

    def call_finished(self, ok):
        with self._lock:
            self._in_progress -= 1
            if not ok:
                self.llm_errors_total += 1

    def snapshot(self):
        with self._lock:
            return {
                "llm_calls_total": self.llm_calls_total,
                "llm_errors_total": self.llm_errors_total,
                "max_concurrent_llm_calls": self.max_concurrent_llm_calls,
            }


class Enricher:
    """Turns (raw_title, raw_description) into a validated enrichment dict.

    enrich() returns (result, cache_hit). It raises EnrichmentFailed after
    MAX_ATTEMPTS failed attempts.
    """

    def __init__(self, provider, db, concurrency, retry_base_delay_s=0.2, metrics=None,
                 sleep=time.sleep):
        self.provider = provider
        self.db = db
        self.metrics = metrics or Metrics()
        self.retry_base_delay_s = retry_base_delay_s
        self._slots = threading.BoundedSemaphore(concurrency)
        self._sleep = sleep
        # content_key -> Future for content currently being enriched by some thread.
        self._inflight = {}
        self._inflight_lock = threading.Lock()

    def enrich(self, raw_title, raw_description):
        key = content_key(raw_title, raw_description)

        cached = self.db.cache_get(key)
        if cached is not None:
            return cached, True

        with self._inflight_lock:
            future = self._inflight.get(key)
            if future is None:
                # Re-check under the lock: a leader may have finished between the
                # first cache lookup and here. Leaders write the cache *before*
                # removing themselves from _inflight, so if the key is not in
                # _inflight now, any finished result is already in the cache.
                cached = self.db.cache_get(key)
                if cached is not None:
                    return cached, True
                future = Future()
                self._inflight[key] = future
                leader = True
            else:
                leader = False

        if not leader:
            # Same content is being enriched right now: wait for that result.
            # Raises EnrichmentFailed if the leader failed; we do not retry it
            # again, it already had MAX_ATTEMPTS.
            return future.result(), True

        try:
            result = self._call_with_retries(raw_title, raw_description)
            self.db.cache_put(key, result)
        except BaseException as exc:
            with self._inflight_lock:
                del self._inflight[key]
            future.set_exception(exc)
            raise
        with self._inflight_lock:
            del self._inflight[key]
        future.set_result(result)
        return result, False

    def _call_with_retries(self, raw_title, raw_description):
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                # 200 ms, 400 ms, 800 ms, with a little jitter so retries from many
                # items do not all land at the same instant.
                delay = self.retry_base_delay_s * (2 ** (attempt - 1))
                self._sleep(delay * random.uniform(0.9, 1.1))
            try:
                return self._one_call(raw_title, raw_description)
            except (LLMError, ValidationError) as exc:
                last_error = exc
        raise EnrichmentFailed(f"failed after {MAX_ATTEMPTS} attempts: {last_error}")

    def _one_call(self, raw_title, raw_description):
        with self._slots:
            self.metrics.call_started()
            ok = False
            try:
                text = self.provider.complete(raw_title, raw_description)
                result = parse_enrichment(text)  # bad JSON/category -> failed attempt
                ok = True
                return result
            except (LLMError, ValidationError):
                raise
            except Exception as exc:
                # Anything unexpected from a provider is treated as a failed call.
                log.exception("unexpected provider error")
                raise LLMError(f"provider error: {exc.__class__.__name__}") from None
            finally:
                self.metrics.call_finished(ok)


def new_job_id():
    return "j_" + secrets.token_hex(6)


class JobRunner:
    """Owns the worker pool; schedules job items and records their outcome."""

    def __init__(self, db, enricher, worker_threads):
        self.db = db
        self.enricher = enricher
        self._pool = ThreadPoolExecutor(max_workers=worker_threads,
                                        thread_name_prefix="catalogiq-worker")

    def submit(self, products):
        job_id = new_job_id()
        job = self.db.create_job(job_id, products)
        self._schedule(job_id)
        return job

    def resume_unfinished(self):
        """Re-queue items of jobs that were interrupted by a restart."""
        resumed = 0
        for job_id in self.db.unfinished_jobs():
            resumed += self._schedule(job_id)
        return resumed

    def _schedule(self, job_id):
        items = self.db.pending_items(job_id)
        for item in items:
            self._pool.submit(self._process, item)
        return len(items)

    def _process(self, item):
        try:
            self.db.mark_job_started(item["job_id"])
            key = content_key(item["raw_title"], item["raw_description"])
            try:
                result, cache_hit = self.enricher.enrich(item["raw_title"],
                                                         item["raw_description"])
                self.db.finish_item(item, key, result, None, cache_hit)
            except EnrichmentFailed as exc:
                self.db.finish_item(item, key, None, str(exc), False)
        except Exception:
            # Leave the item pending; it is retried on the next restart.
            log.exception("could not record item %s of job %s", item["idx"], item["job_id"])

    def shutdown(self, wait=True):
        self._pool.shutdown(wait=wait, cancel_futures=not wait)
