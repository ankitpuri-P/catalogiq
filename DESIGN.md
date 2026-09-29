# CatalogIQ — Design

```
 browser                        server process
 ───────                        ──────────────────────────────────────────────────────────
 CSV ─parse─▶ POST /api/jobs ─▶ validate ─▶ INSERT job + job_items (1 txn) ─▶ 202
                                                   │
                                                   ▼  one task per pending item
                                   ThreadPoolExecutor (WORKER_THREADS, e.g. 20)
                                                   │
                         ┌─────────── Enricher.enrich(content) ───────────┐
                         │ 1. enrichment_cache hit?        ──yes──▶ result │
                         │ 2. under lock: in-flight future for this key?  │
                         │      yes ──▶ wait on it ──────────────▶ result │
                         │      no  ──▶ register future, become leader    │
                         │ 3. leader: up to 4 attempts                    │
                         │      ┌ BoundedSemaphore(LLM_CONCURRENCY) ┐     │
                         │      │ provider.complete() → parse/validate│    │
                         │      └────────────────────────────────────┘    │
                         │      sleep 200/400/800 ms between (no slot held)│
                         │ 4. write cache, pop future, resolve waiters    │
                         └────────────────────────────────────────────────┘
                                                   │
                        1 txn: job_item→done, UPSERT product, job counters++
                                                   │
 poll 1/s ◀─ GET /api/jobs/<id> ◀──────────── SQLite (WAL) ──▶ GET /api/products
```

## 1. Architecture

**Upload to finished product.** The browser parses the CSV and POSTs the
products. The handler validates them, inserts the job and every item into
`job_items` in one transaction, submits one task per item to a shared thread
pool, and returns 202. That takes tens of milliseconds even for thousands of
items, because no LLM work happens on the request thread. Each worker calls
`Enricher.enrich()`, which returns a cached result, waits for an identical item
that is already in flight, or calls the LLM. Then, in a **single transaction**,
the worker marks the item done, upserts the product and bumps the job counters.
The last item flips the job to `completed` with a conditional update.

**What runs in parallel.** Up to `WORKER_THREADS` items at once, across all
jobs, plus one thread per HTTP request. The pool is deliberately bigger than the
LLM limit. Some workers are only waiting: on a backoff timer, or on a duplicate
another worker is enriching. They should not hold up items that could be making
progress.

**How the limit is enforced.** One `threading.BoundedSemaphore(LLM_CONCURRENCY)`
is shared by every job. A worker acquires it for exactly one provider call,
including validating that call's output, and releases it before any backoff
sleep. So a failing item never occupies a slot while it waits. The metrics
counter for peak concurrency is updated inside the semaphore, under its own
lock.

**De-duplication.** The key is the SHA-256 of `lower(collapse_ws(title + " " +
description))`. Validated results live in the `enrichment_cache` table, so they
survive restarts. Concurrent duplicates go through a `dict[key -> Future]`
guarded by a lock. The first item becomes the leader and later ones block on
`future.result()`. The leader writes the cache **before** removing its future,
and a new leader re-checks the cache under the lock. That closes the gap where
a result is finished but not yet visible. Failures are shared with the current
waiters but not cached.

**Why threads.** LLM calls are I/O-bound, so the GIL is not a bottleneck and
processes would add IPC for no gain. asyncio would work equally well: one event
loop, `asyncio.Semaphore`, `asyncio.Future`. I chose threads because they let
the whole project use the standard library. A sync `sqlite3` connection and
`urllib` work naturally, the HTTP server is `ThreadingHTTPServer`, and the
blocking code is simple to read and test. *Rejected:* asyncio with FastAPI plus
aiosqlite/httpx, which is three dependencies and puts sync SQLite calls on the
event loop unless every one is wrapped; and a separate worker process with a
queue broker, which is overkill for one machine.

## 2. Crash recovery

**Already implemented.** Every item is a row in `job_items` with status
`pending` or `done`. An item's outcome, its product row and the job counters
are committed together, so a crash leaves each item either fully recorded or
still pending, never half-counted. `finish_item` only acts on a `pending` row,
which makes recording idempotent. On startup, `resume_unfinished()` requeues the
pending items of every job that is not `completed`. Items finished before the
crash are skipped. Items that were mid-call are redone, and if their LLM result
had already been cached they become cache hits. I tested this with `kill -9`
partway through a 300-item job: 95 were done, 205 resumed, and the restart made
exactly 205 LLM calls.

**What remains.** A call that was in flight at the crash is paid for twice,
which is unavoidable without provider-side idempotency. Metrics reset on
restart. With several server processes, two of them could resume the same job.
That would need a lease: `UPDATE job_items SET owner=?, lease_until=? WHERE
status='pending' AND (lease_until IS NULL OR lease_until < now)`. The same
lease is the basis for a multi-worker queue (see below).

## 3. Scale and cost: 1M listings a day

1M a day is about 11.6 items per second on average, with bursts far above that.
The design goal is to minimise **requests**, then spend them evenly.

1. **Cache first.** Marketplace feeds repeat a lot, because the same product
   comes from many sellers and gets re-uploaded daily. Content-hash dedup
   already removes exact repeats. A second layer could normalise more
   aggressively (strip seller noise like "BEST PRICE", canonicalise units)
   before hashing. A 50% hit rate halves the bill.
2. **Batch several products per prompt.** Put 10–20 listings in one request
   and ask for a JSON array keyed by SKU. That cuts request count about 15×,
   which is what an RPM limit counts, and amortises the prompt tokens. Validate
   each element separately. If one element is bad, retry only that product,
   alone or in the next batch, rather than the whole batch. Trade-offs: larger
   batches raise latency and the blast radius of a malformed response. 10–20
   is a reasonable range for small models.
3. **Shared rate limiter.** The per-process semaphore caps concurrency, not
   rate. With several workers or machines, use a token bucket in Redis or a
   Postgres row that every worker takes from before calling, sized to the
   provider's RPM/TPM with headroom. On a 429, honour `Retry-After` and slow the
   whole fleet down, not just the one caller.
4. **Several workers.** Move the queue out of process memory: `job_items` in
   Postgres with `SELECT … FOR UPDATE SKIP LOCKED` and leases, or a real queue
   (SQS/Redis streams). Stateless workers can then scale horizontally. The
   in-flight dedup map becomes a unique "claim" row per content key.
5. **Second provider as fallback.** Put a circuit breaker per provider. When the
   primary's error rate or 429s cross a threshold, route to the secondary
   (another free tier or a local model). Tag each product with the provider
   and prompt version so its quality can be tracked and it can be re-enriched
   later.
6. **Smooth the load.** Accept bursts into the queue immediately (202) and drain
   at the provider's rate. Low-priority backfills run off-peak.

## 4. API performance: 5M products

What makes `GET /api/products?q=…` slow today:
- `LIKE '%foo%'` cannot use a B-tree index, so every query scans all 5M rows,
  over two columns.
- `COUNT(*)` for `total` makes a second full scan.
- `OFFSET` pagination reads and throws away every earlier row, so page 10,000
  is very slow.

Fixes, in order of impact:
1. **Full-text search.** An FTS5 virtual table over `clean_title, raw_title`
   (`tokenize='unicode61 remove_diacritics 2'`, which also fixes the
   ASCII-only case folding), kept in sync by triggers, queried with
   `MATCH 'foo*'`. For typo tolerance or ranking at scale, move to
   Postgres `tsvector`/`pg_trgm`, OpenSearch or Meilisearch.
2. **Keyset (cursor) pagination.** `WHERE sku > :last_sku ORDER BY sku LIMIT
   20` makes every page O(page size) using the primary key. The API returns an
   opaque `next_cursor`. Keep `page` only for shallow pages.
3. **Indexes for filters.** `(category, sku)` already exists, so category plus
   sort order is one index range scan.
4. **Cheaper totals.** Return an estimate or cap it ("10,000+"). Otherwise
   cache counts per `(category, q)` for a short TTL.
5. **Caching.** Short-TTL cache of popular query pages in Redis or at the CDN.
   The response has no per-user fields, so it is safe to share across callers.
   Otherwise add `Vary` on identity. Invalidate on product writes, or accept a
   few seconds of staleness.
6. **Select only needed columns** (already the case), and serve reads from a
   replica so enrichment writes do not contend with search.

## 5. Quality: invented brands, wrong categories

**Catching problems automatically.** Each product gets a cheap confidence
score, and anything below a threshold goes to the review queue:
- **Brand grounding.** The brand must appear, fuzzily, in the raw title or
  description, or in a known-brands table built from approved products.
  Otherwise set it to null or flag it. The prompt already says "never guess",
  but a check is what makes that reliable.
- **Category cross-checks.** Compare the LLM's category with a keyword or
  small-classifier baseline, like the mock's rules, and with the category
  already approved for the same brand. Disagreement means review.
- **Self-consistency.** For uncertain items, ask twice or ask a second model.
  Disagreement means review.
- **Title drift.** Flag clean titles that introduce numbers or units missing
  from the raw text, such as a pack size or weight the listing never stated.
- **Validation failures and retries.** Items that needed retries, or where a
  field was repaired, get a lower score.

**Choosing what humans review.** Order the queue by `(1 − confidence) × impact`,
where impact is sales, traffic or seller tier. Also send a small random sample
of high-confidence items (e.g. 1%) to reviewers to measure the true error rate
and catch systematic drift. Reviewer edits (`PATCH` → `approved`) become
labelled data. They are used to tune the thresholds, add few-shot examples to
the prompt, and grow the known-brands table. Approved edits are preserved when
the same content is re-submitted, which is already implemented.

## Security notes

The brief's API has no authentication. The server therefore binds to the
loopback addresses (`127.0.0.1` and `::1`) only. It rejects `Host` headers that are not localhost, which blocks
DNS rebinding. It requires `Content-Type: application/json` on writes, so a
cross-origin page cannot send a "simple" POST. It serves HTML with a strict CSP
(`default-src 'self'`, no inline script), and the frontend inserts data only
through `textContent`. Deploying beyond localhost would need an authenticated
principal on every route, with job and product access scoped per
tenant/seller, plus per-principal rate limits on `POST /api/jobs`. SQL is fully
parameterised, and the only dynamic identifiers come from fixed tuples. Inputs
are bounded (body size, product count, field lengths, page size, query length).
LLM output is treated as untrusted and validated before storage. The listing is
placed in delimited "data" tags in the prompt, and it cannot trigger any action
beyond producing a validated JSON record.
