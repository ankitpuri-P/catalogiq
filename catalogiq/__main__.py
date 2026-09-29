"""Entry point: python -m catalogiq"""

import logging
import signal
import sys
import threading

from .config import ConfigError, load_config
from .db import Database
from .llm import make_provider
from .pipeline import Enricher, JobRunner, Metrics
from .server import App, make_servers


def build(config):
    """Wire the components together. Returns (app, runner)."""
    db = Database(config.db_path)
    metrics = Metrics()
    enricher = Enricher(make_provider(config), db, config.llm_concurrency,
                        retry_base_delay_s=config.retry_base_delay_ms / 1000.0,
                        metrics=metrics)
    runner = JobRunner(db, enricher, config.worker_threads)
    return App(config, db, runner, metrics), runner


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("catalogiq")
    try:
        config = load_config()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        sys.exit(2)

    app, runner = build(config)
    try:
        servers = make_servers(app)
    except OSError as exc:
        log.error("cannot listen on %s:%d: %s (is another copy already running?)",
                  config.host, config.port, exc)
        sys.exit(1)
    provider = app.runner.enricher.provider
    if hasattr(provider, "check"):
        problem = provider.check()
        if problem:
            # Not fatal: Ollama may be started after us. Items fail (and say why)
            # until it is available.
            log.warning("LLM provider not ready: %s", problem)
        else:
            log.info("loading %s into memory...", provider.model)
            def warm():
                try:
                    provider.warm_up()
                    log.info("%s loaded", provider.model)
                except Exception as exc:
                    log.warning("could not preload %s: %s", provider.model, exc)
            threading.Thread(target=warm, daemon=True).start()

    resumed = runner.resume_unfinished()
    if resumed:
        log.info("resumed %d pending items from unfinished jobs", resumed)

    def stop(signum, frame):
        # shutdown() blocks until serve_forever returns, so call it off-thread.
        for s in servers:
            threading.Thread(target=s.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)       # Ctrl+C
    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGBREAK"):          # Ctrl+Break on Windows
        signal.signal(signal.SIGBREAK, stop)

    # Extra listeners (e.g. [::1]) on background threads, the first on this one.
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    log.info("CatalogIQ on http://localhost:%d (provider=%s, concurrency=%d)",
             servers[0].server_address[1], config.llm_provider, config.llm_concurrency)
    try:
        servers[0].serve_forever()
    finally:
        for s in servers:
            s.server_close()
        # Queued items stay 'pending' in the DB and are resumed on next start.
        runner.shutdown(wait=False)
        log.info("stopped")


if __name__ == "__main__":
    main()
