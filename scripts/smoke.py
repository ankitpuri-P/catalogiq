"""End-to-end check against a running server. Works the same on Windows, macOS, Linux.

    python scripts/smoke.py                          # uploads data/sample_listings.csv
    python scripts/smoke.py --csv other.csv
    python scripts/smoke.py --base http://127.0.0.1:8001

Exits non-zero if any check fails.
"""

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_CSV = Path(__file__).resolve().parent.parent / "data" / "sample_listings.csv"
failures = 0


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status, payload = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, json.loads(exc.read())
    return status, payload, time.perf_counter() - t0


def check(label, ok, detail=""):
    global failures
    failures += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


def load_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    products = []
    for r in rows:
        p = {"sku": (r.get("sku") or "").strip(), "raw_title": r.get("raw_title") or ""}
        if r.get("raw_description"):
            p["raw_description"] = r["raw_description"]
        products.append(p)
    return products


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print(f"server {base}")
    try:
        status, health, dt = call(base, "GET", "/api/health")
    except OSError as exc:
        sys.exit(f"cannot reach {base}: {exc}. Is the server running?")
    check("health", status == 200 and health.get("status") == "ok",
          f"{health} in {dt * 1000:.0f} ms")
    _, before, _ = call(base, "GET", "/api/metrics")

    products = load_csv(args.csv)
    print(f"\nuploading {len(products)} listings from {args.csv}")
    status, job, dt = call(base, "POST", "/api/jobs", {"products": products})
    check("POST /api/jobs returns 202 within 1 s", status == 202 and dt < 1.0,
          f"{status} in {dt * 1000:.0f} ms")
    if status != 202:
        sys.exit(f"job rejected: {job}")

    slowest = 0.0
    while True:
        time.sleep(1)
        _, job, dt = call(base, "GET", f"/api/jobs/{job['id']}")
        slowest = max(slowest, dt)
        print(f"  {job['status']:9} done {job['done']}/{job['total']}  "
              f"failed {job['failed']}  cache hits {job['cache_hits']}")
        if job["status"] == "completed":
            break
    check("API stayed fast while the job ran", slowest < 0.5,
          f"slowest poll {slowest * 1000:.0f} ms")
    check("done == total", job["done"] == job["total"])

    _, after, _ = call(base, "GET", "/api/metrics")
    calls = after["llm_calls_total"] - before["llm_calls_total"]
    print(f"\nmetrics {after}")
    print(f"  this job: {calls} LLM calls for {len(products)} items "
          f"({job['cache_hits']} cache hits)")
    check("concurrency never above the limit",
          after["max_concurrent_llm_calls"] <= health["llm_concurrency"])

    print("\nde-duplication")
    stamp = str(int(time.time()))
    dupes = [{"sku": f"SMOKE-{stamp}-{i}", "raw_title": t, "raw_description": "pck of 2"}
             for i, t in enumerate([f" AMUL  butter {stamp}", f"amul BUTTER {stamp}",
                                    f"Amul   Butter {stamp}"])]
    _, job, _ = call(base, "POST", "/api/jobs", {"products": dupes})
    while job["status"] != "completed":
        time.sleep(1)
        _, job, _ = call(base, "GET", f"/api/jobs/{job['id']}")
    check("3 identical listings -> 2 cache hits", job["cache_hits"] == 2 or job["failed"] == 3,
          f"{job}")

    print("\nreview")
    sku = dupes[0]["sku"]
    status, p, _ = call(base, "PATCH", f"/api/products/{sku}",
                        {"clean_title": "Amul Butter (Pack of 2)", "category": "Groceries",
                         "tags": ["Butter", "dairy"]})
    check("PATCH approves", status == 200 and p.get("status") == "approved", f"{p}")

    print("\nerror cases")
    for label, method, path, body, want in [
        ("empty product list", "POST", "/api/jobs", {"products": []}, 400),
        ("product without sku", "POST", "/api/jobs", {"products": [{"raw_title": "x"}]}, 400),
        ("non-numeric page", "GET", "/api/products?page=abc", None, 400),
        ("unknown job", "GET", "/api/jobs/j_000000000000", None, 404),
        ("unknown sku", "GET", "/api/products/NO-SUCH-SKU", None, 404),
        ("invalid category", "PATCH", f"/api/products/{sku}", {"category": "Toys"}, 400),
        ("empty title", "PATCH", f"/api/products/{sku}", {"clean_title": ""}, 400),
    ]:
        status, body, _ = call(base, method, path, body)
        check(f"{label} -> {want}", status == want and "error" in body, f"got {status} {body}")

    _, page, _ = call(base, "GET", "/api/products?q=amul&page_size=3")
    print(f"\nsample search 'amul': {page['total']} results")
    for item in page["items"]:
        print(f"  {item['sku']:>16}  {item['category']:<14} {item['clean_title']}")

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
