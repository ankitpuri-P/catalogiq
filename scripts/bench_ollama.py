"""Measure which Ollama model and concurrency work best on this machine.

Sends the same N unique listings from the sample CSV straight to Ollama (the
CatalogIQ server does not need to be running) and reports speed and answer
quality for each model x concurrency combination.

    python scripts/bench_ollama.py
    python scripts/bench_ollama.py --models qwen2.5:7b llama3.2:3b --concurrency 1 2 --n 20

For concurrency > 1 to help, Ollama itself must allow parallel requests:
set OLLAMA_NUM_PARALLEL=2 for the Ollama app and restart it (see USER_GUIDE.md).
"""

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalogiq.enrichment import ValidationError, normalize_content, parse_enrichment
from catalogiq.llm import LLMError, OllamaProvider

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_listings.csv"


def load_unique(n):
    seen, out = set(), []
    with open(SAMPLE, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = normalize_content(row["raw_title"], row.get("raw_description"))
            if key not in seen:
                seen.add(key)
                out.append((row["raw_title"], row.get("raw_description") or None))
    return out[:n]


def run_one(provider, title, desc):
    t0 = time.perf_counter()
    try:
        result = parse_enrichment(provider.complete(title, desc))
        err = None
    except (LLMError, ValidationError) as exc:
        result, err = None, str(exc)
    return time.perf_counter() - t0, result, err, title, desc


def bench(url, model, concurrency, items, ctx):
    provider = OllamaProvider(url, model, timeout_s=180, num_ctx=ctx)
    problem = provider.check()
    if problem:
        print(f"  skipped: {problem}")
        return None
    t0 = time.perf_counter()
    provider.warm_up()
    load_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    with ThreadPoolExecutor(concurrency) as pool:
        results = list(pool.map(lambda it: run_one(provider, *it), items))
    wall = time.perf_counter() - t0

    ok = [r for r in results if r[1]]
    # An invented brand is one that does not appear anywhere in the listing text.
    invented = [r for r in ok if r[1]["brand"] and
                r[1]["brand"].lower().replace("'", "") not in
                f"{r[3]} {r[4] or ''}".lower().replace("'", "")]
    return {"model": model, "conc": concurrency, "load_s": load_s,
            "per_item_s": wall / len(items), "items_per_min": 60 * len(items) / wall,
            "valid": len(ok), "n": len(items), "invented": len(invented),
            "errors": [r[2] for r in results if r[2]][:2],
            "examples": [(r[3], r[1]) for r in ok[:3]]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("--models", nargs="+", default=["qwen2.5:7b", "llama3.2:3b"])
    ap.add_argument("--concurrency", nargs="+", type=int, default=[1, 2])
    ap.add_argument("--n", type=int, default=20, help="listings per run")
    ap.add_argument("--ctx", type=int, default=1024, help="num_ctx")
    args = ap.parse_args()

    items = load_unique(args.n)
    rows = []
    for model in args.models:
        for conc in args.concurrency:
            print(f"\n== {model}  concurrency {conc}  ({len(items)} listings)")
            r = bench(args.url, model, conc, items, args.ctx)
            if not r:
                continue
            rows.append(r)
            print(f"  load {r['load_s']:.1f}s | {r['per_item_s']:.2f} s/item | "
                  f"{r['items_per_min']:.0f} items/min | valid {r['valid']}/{r['n']} | "
                  f"invented brands {r['invented']}")
            for e in r["errors"]:
                print(f"  error: {e}")
            for title, res in r["examples"]:
                print(f"    {title.strip()[:40]:<40} -> {res['clean_title']} | "
                      f"{res['category']} | {res['brand']}")

    if not rows:
        sys.exit("\nNo runs completed. Is Ollama running and are the models pulled?")
    print("\nsummary (fastest first)")
    print(f"  {'model':<16}{'conc':>5}{'s/item':>9}{'items/min':>11}{'valid':>8}{'invented':>10}")
    for r in sorted(rows, key=lambda r: r["per_item_s"]):
        print(f"  {r['model']:<16}{r['conc']:>5}{r['per_item_s']:>9.2f}{r['items_per_min']:>11.0f}"
              f"{r['valid']:>5}/{r['n']:<3}{r['invented']:>8}")
    good = [r for r in rows if r["valid"] == r["n"]] or rows
    best = min(good, key=lambda r: (r["invented"], r["per_item_s"]))
    print(f"\nsuggested: OLLAMA_MODEL={best['model']}  LLM_CONCURRENCY={best['conc']}")
    print(json.dumps({"OLLAMA_MODEL": best["model"], "LLM_CONCURRENCY": best["conc"]}))


if __name__ == "__main__":
    main()
