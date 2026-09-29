"""SQLite storage. One connection per thread, WAL mode, short transactions.

Tables
  jobs              one row per upload, with live counters
  job_items         every item of every job; status pending -> done. This is what
                    lets a restarted server resume a job without redoing work.
  products          the catalogue, keyed by SKU
  enrichment_cache  validated LLM output keyed by content hash
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    total       INTEGER NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    cache_hits  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS job_items (
    job_id          TEXT NOT NULL REFERENCES jobs(id),
    idx             INTEGER NOT NULL,
    sku             TEXT NOT NULL,
    raw_title       TEXT NOT NULL,
    raw_description TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (job_id, idx)
);
CREATE INDEX IF NOT EXISTS job_items_pending ON job_items(job_id, status);

CREATE TABLE IF NOT EXISTS products (
    sku             TEXT PRIMARY KEY,
    raw_title       TEXT NOT NULL,
    raw_description TEXT,
    content_key     TEXT NOT NULL,
    clean_title     TEXT,
    category        TEXT,
    brand           TEXT,
    tags            TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL,
    error           TEXT,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS products_category ON products(category, sku);

CREATE TABLE IF NOT EXISTS enrichment_cache (
    content_key TEXT PRIMARY KEY,
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

PRODUCT_COLUMNS = ("sku, raw_title, raw_description, clean_title, category, brand, "
                   "tags, status, error")
JOB_COLUMNS = "id, status, total, done, failed, cache_hits, created_at, started_at, finished_at"


def now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _like_escape(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _product_row(row):
    d = dict(row)
    d["tags"] = json.loads(d["tags"] or "[]")
    return d


class Database:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        self._all = []  # every per-thread connection, so close() can release them
        self._all_lock = threading.Lock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            with self._all_lock:
                self._all.append(conn)
        return _Tx(conn)

    def close(self):
        with self._all_lock:
            for conn in self._all:
                conn.close()
            self._all.clear()
        self._local = threading.local()

    # ------------------------------------------------------------ jobs

    def create_job(self, job_id, products):
        """Insert the job and all its items in one transaction."""
        with self.connect() as conn:
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO jobs (id, status, total, created_at) VALUES (?, 'queued', ?, ?)",
                (job_id, len(products), now_iso()))
            conn.executemany(
                "INSERT INTO job_items (job_id, idx, sku, raw_title, raw_description) "
                "VALUES (?, ?, ?, ?, ?)",
                [(job_id, i, p["sku"], p["raw_title"], p.get("raw_description"))
                 for i, p in enumerate(products)])
        return self.get_job(job_id)

    def get_job(self, job_id):
        with self.connect() as conn:
            row = conn.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = ?",
                               (job_id,)).fetchone()
        return dict(row) if row else None

    def mark_job_started(self, job_id):
        # Conditional update: only the first item to start flips the status.
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ? "
                "WHERE id = ? AND status = 'queued'", (now_iso(), job_id))

    def unfinished_jobs(self):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status != 'completed' ORDER BY created_at").fetchall()
        return [r["id"] for r in rows]

    def pending_items(self, job_id):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT job_id, idx, sku, raw_title, raw_description FROM job_items "
                "WHERE job_id = ? AND status = 'pending' ORDER BY idx", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def finish_item(self, item, ckey, result, error, cache_hit):
        """Record one item's outcome atomically: product row, item status, job counters.

        Because all three change in one transaction, a crash leaves the item either
        fully done or still pending, never counted twice.
        """
        failed = result is None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE job_items SET status = 'done' "
                "WHERE job_id = ? AND idx = ? AND status = 'pending'",
                (item["job_id"], item["idx"]))
            if cur.rowcount == 0:
                return  # already recorded (e.g. resumed twice); do not double count
            conn.execute(
                # A human-approved product keeps its edits if its content did not change.
                "INSERT INTO products (sku, raw_title, raw_description, content_key, "
                "  clean_title, category, brand, tags, status, error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sku) DO UPDATE SET "
                "  raw_title = excluded.raw_title, raw_description = excluded.raw_description, "
                "  content_key = excluded.content_key, clean_title = excluded.clean_title, "
                "  category = excluded.category, brand = excluded.brand, tags = excluded.tags, "
                "  status = excluded.status, error = excluded.error, "
                "  updated_at = excluded.updated_at "
                "WHERE NOT (products.status = 'approved' "
                "           AND products.content_key = excluded.content_key)",
                (item["sku"], item["raw_title"], item["raw_description"], ckey,
                 None if failed else result["clean_title"],
                 None if failed else result["category"],
                 None if failed else result["brand"],
                 json.dumps([] if failed else result["tags"]),
                 "failed" if failed else "enriched",
                 error if failed else None,
                 now_iso()))
            conn.execute(
                "UPDATE jobs SET done = done + 1, failed = failed + ?, "
                "  cache_hits = cache_hits + ? WHERE id = ?",
                (int(failed), int(cache_hit), item["job_id"]))
            conn.execute(
                "UPDATE jobs SET status = 'completed', finished_at = ? "
                "WHERE id = ? AND done >= total AND status != 'completed'",
                (now_iso(), item["job_id"]))

    # ------------------------------------------------------------ cache

    def cache_get(self, ckey):
        with self.connect() as conn:
            row = conn.execute("SELECT result FROM enrichment_cache WHERE content_key = ?",
                               (ckey,)).fetchone()
        return json.loads(row["result"]) if row else None

    def cache_put(self, ckey, result):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO enrichment_cache (content_key, result, created_at) "
                "VALUES (?, ?, ?)", (ckey, json.dumps(result), now_iso()))

    # ------------------------------------------------------------ products

    def get_product(self, sku):
        with self.connect() as conn:
            row = conn.execute(f"SELECT {PRODUCT_COLUMNS} FROM products WHERE sku = ?",
                               (sku,)).fetchone()
        return _product_row(row) if row else None

    def list_products(self, page, page_size, category=None, q=None):
        where, args = [], []
        if category:
            where.append("category = ?")
            args.append(category)
        if q:
            # LIKE is case-insensitive for ASCII in SQLite. See DESIGN.md for FTS at scale.
            pattern = f"%{_like_escape(q)}%"
            where.append("(clean_title LIKE ? ESCAPE '\\' OR raw_title LIKE ? ESCAPE '\\')")
            args += [pattern, pattern]
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM products {clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT {PRODUCT_COLUMNS} FROM products {clause} ORDER BY sku LIMIT ? OFFSET ?",
                args + [page_size, (page - 1) * page_size]).fetchall()
        return [_product_row(r) for r in rows], total

    def update_product(self, sku, changes):
        """Apply a reviewer's edits and mark the product approved.

        Returns the updated product, None if the SKU does not exist, or raises
        ValueError if the result would be an approved product without a title
        or category (possible when approving a failed product).
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT clean_title, category FROM products WHERE sku = ?",
                               (sku,)).fetchone()
            if row is None:
                return None
            title = changes.get("clean_title", row["clean_title"])
            category = changes.get("category", row["category"])
            if not title or not category:
                raise ValueError("a failed product needs clean_title and category to be approved")
            sets = ["status = 'approved'", "error = NULL", "updated_at = ?"]
            args = [now_iso()]
            for col in ("clean_title", "category"):
                if col in changes:
                    sets.append(f"{col} = ?")  # col comes from a fixed tuple, not the caller
                    args.append(changes[col])
            if "tags" in changes:
                sets.append("tags = ?")
                args.append(json.dumps(changes["tags"]))
            conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE sku = ?", args + [sku])
        return self.get_product(sku)


class _Tx:
    """Context manager: commit on success, roll back on error. Reuses the thread's connection."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if self.conn.in_transaction:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        return False
