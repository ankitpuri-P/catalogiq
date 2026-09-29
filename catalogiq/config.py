"""Configuration read from environment variables.

A malformed value is a startup error, never a silent fallback: a typo in
LLM_CONCURRENCY should not quietly turn into "unlimited".
"""

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

PROVIDERS = ("mock", "ollama")


class ConfigError(ValueError):
    pass


def _int(name, default, lo, hi):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _float(name, default, lo, hi):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _model_name(raw):
    name = raw.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,100}(:[A-Za-z0-9._-]{1,60})?", name):
        raise ConfigError(f"OLLAMA_MODEL is not a valid model name: {raw!r}")
    return name


def _keep_alive(raw):
    value = raw.strip()
    if not re.fullmatch(r"-1|\d{1,6}[smh]?", value):
        raise ConfigError(f"OLLAMA_KEEP_ALIVE must look like 30m, 1h, 600 or -1, got {raw!r}")
    return value


@dataclass(frozen=True)
class Config:
    llm_provider: str = "mock"
    llm_concurrency: int = 5
    mock_latency_ms: int = 200
    mock_failure_rate: float = 0.1
    retry_base_delay_ms: int = 200
    worker_threads: int = 20
    db_path: str = "catalogiq.db"
    host: str = "localhost"
    port: int = 8000
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_timeout_s: float = 60.0
    ollama_num_ctx: int = 1024
    ollama_keep_alive: str = "30m"


def load_config():
    provider = os.environ.get("LLM_PROVIDER", "mock").strip().lower() or "mock"
    if provider not in PROVIDERS:
        raise ConfigError(f"LLM_PROVIDER must be one of {PROVIDERS}, got {provider!r}")

    concurrency = _int("LLM_CONCURRENCY", 5, 1, 1000)

    # 127.0.0.1 rather than localhost: Ollama listens on IPv4, and on Windows
    # trying ::1 first adds ~2 s to every call.
    ollama_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    parsed = urlparse(ollama_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError("OLLAMA_URL must be an http(s) URL with a host")

    return Config(
        llm_provider=provider,
        llm_concurrency=concurrency,
        mock_latency_ms=_int("MOCK_LATENCY_MS", 200, 0, 600_000),
        mock_failure_rate=_float("MOCK_FAILURE_RATE", 0.1, 0.0, 1.0),
        retry_base_delay_ms=_int("RETRY_BASE_DELAY_MS", 200, 0, 60_000),
        # Worker threads may be parked waiting on a backoff or on a duplicate
        # that is in flight, so keep more threads than LLM slots.
        worker_threads=_int("WORKER_THREADS", max(8, concurrency * 4), 1, 4000),
        db_path=os.environ.get("CATALOGIQ_DB", "catalogiq.db"),
        host=os.environ.get("HOST", "localhost").strip() or "localhost",
        port=_int("PORT", 8000, 1, 65535),
        ollama_url=ollama_url,
        ollama_model=_model_name(os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")),
        ollama_timeout_s=_float("OLLAMA_TIMEOUT_S", 60.0, 1.0, 600.0),
        # 1024 tokens fits the prompt + answer (~500) and keeps the KV cache small
        # enough that a 7B model and 2 parallel requests stay inside 6 GB of VRAM.
        ollama_num_ctx=_int("OLLAMA_NUM_CTX", 1024, 512, 32768),
        ollama_keep_alive=_keep_alive(os.environ.get("OLLAMA_KEEP_ALIVE", "30m")),
    )
