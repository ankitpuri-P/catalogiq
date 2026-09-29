"""HTTP API and static frontend, on the standard library's ThreadingHTTPServer.

Each request runs on its own thread and only does short SQLite reads/writes,
so the API stays responsive while workers are busy with LLM calls.
"""

import json
import logging
import os
import re
import secrets
import socket
import socketserver
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .enrichment import CATEGORIES, MAX_TITLE_LEN, ValidationError, normalize_tags

log = logging.getLogger("catalogiq.server")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# Fixed map: request paths never touch the filesystem directly.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}

MAX_BODY_BYTES = 20 * 1024 * 1024
MAX_PRODUCTS_PER_JOB = 50_000
MAX_SKU_LEN = 128
MAX_RAW_TITLE_LEN = 1000
MAX_RAW_DESCRIPTION_LEN = 5000
MAX_QUERY_LEN = 200
MAX_PAGE_SIZE = 100
PRODUCT_FIELDS = {"sku", "raw_title", "raw_description"}
PATCH_FIELDS = {"clean_title", "category", "tags"}

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}

_JOB_ID_RE = re.compile(r"^j_[0-9a-f]{12}$")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ------------------------------------------------------------ input validation

def validate_job_body(body):
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object")
    unknown = set(body) - {"products"}
    if unknown:
        raise ApiError(400, f"unknown field: {sorted(unknown)[0]}")
    products = body.get("products")
    if not isinstance(products, list) or not products:
        raise ApiError(400, "products must be a non-empty list")
    if len(products) > MAX_PRODUCTS_PER_JOB:
        raise ApiError(400, f"at most {MAX_PRODUCTS_PER_JOB} products per job")
    clean = []
    for i, p in enumerate(products):
        if not isinstance(p, dict):
            raise ApiError(400, f"products[{i}] must be an object")
        unknown = set(p) - PRODUCT_FIELDS
        if unknown:
            raise ApiError(400, f"products[{i}] has unknown field: {sorted(unknown)[0]}")
        sku, title, desc = p.get("sku"), p.get("raw_title"), p.get("raw_description")
        if not isinstance(sku, str) or not sku.strip():
            raise ApiError(400, f"products[{i}] is missing sku")
        if not isinstance(title, str) or not title.strip():
            raise ApiError(400, f"products[{i}] is missing raw_title")
        if desc is not None and not isinstance(desc, str):
            raise ApiError(400, f"products[{i}].raw_description must be a string")
        sku = sku.strip()
        if len(sku) > MAX_SKU_LEN:
            raise ApiError(400, f"products[{i}].sku longer than {MAX_SKU_LEN} characters")
        if len(title) > MAX_RAW_TITLE_LEN:
            raise ApiError(400, f"products[{i}].raw_title longer than {MAX_RAW_TITLE_LEN} characters")
        if desc is not None and len(desc) > MAX_RAW_DESCRIPTION_LEN:
            raise ApiError(400, f"products[{i}].raw_description longer than "
                                f"{MAX_RAW_DESCRIPTION_LEN} characters")
        clean.append({"sku": sku, "raw_title": title, "raw_description": desc})
    return clean


def validate_patch_body(body):
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object")
    unknown = set(body) - PATCH_FIELDS
    if unknown:
        raise ApiError(400, f"unknown or read-only field: {sorted(unknown)[0]}")
    changes = {}
    if "clean_title" in body:
        title = body["clean_title"]
        if not isinstance(title, str) or not title.strip():
            raise ApiError(400, "clean_title must be a non-empty string")
        title = " ".join(title.split())
        if len(title) > MAX_TITLE_LEN:
            raise ApiError(400, f"clean_title longer than {MAX_TITLE_LEN} characters")
        changes["clean_title"] = title
    if "category" in body:
        if body["category"] not in CATEGORIES:
            raise ApiError(400, f"category must be one of: {', '.join(CATEGORIES)}")
        changes["category"] = body["category"]
    if "tags" in body:
        try:
            tags = normalize_tags(body["tags"])
        except ValidationError as exc:
            raise ApiError(400, str(exc)) from None
        if len(tags) > 5:
            raise ApiError(400, "at most 5 tags")
        changes["tags"] = tags
    return changes


def parse_positive_int(params, name, default):
    values = params.get(name)
    if not values:
        return default
    raw = values[0].strip()
    if not raw.isdigit() or len(raw) > 9:
        raise ApiError(400, f"{name} must be a positive integer")
    value = int(raw)
    if value < 1:
        raise ApiError(400, f"{name} must be a positive integer")
    return value


# ------------------------------------------------------------ app

class App:
    def __init__(self, config, db, runner, metrics):
        self.config = config
        self.db = db
        self.runner = runner
        self.metrics = metrics
        self.set_port(config.port)

    def set_port(self, port):
        self.allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"}
        if port == 80:
            self.allowed_hosts |= {"localhost", "127.0.0.1", "[::1]"}

    def health(self):
        return 200, {"status": "ok", "llm_provider": self.config.llm_provider,
                     "llm_concurrency": self.config.llm_concurrency}

    def get_metrics(self):
        return 200, self.metrics.snapshot()

    def create_job(self, body):
        products = validate_job_body(body)
        return 202, self.runner.submit(products)

    def get_job(self, job_id):
        job = self.db.get_job(job_id) if _JOB_ID_RE.match(job_id) else None
        if job is None:
            raise ApiError(404, "job not found")
        return 200, job

    def list_products(self, params):
        page = parse_positive_int(params, "page", 1)
        page_size = min(parse_positive_int(params, "page_size", 20), MAX_PAGE_SIZE)
        category = (params.get("category") or [""])[0]
        if category and category not in CATEGORIES:
            raise ApiError(400, "unknown category")
        q = (params.get("q") or [""])[0].strip()
        if len(q) > MAX_QUERY_LEN:
            raise ApiError(400, f"q longer than {MAX_QUERY_LEN} characters")
        items, total = self.db.list_products(page, page_size, category or None, q or None)
        return 200, {"items": items, "page": page, "page_size": page_size, "total": total}

    def get_product(self, sku):
        product = self.db.get_product(sku)
        if product is None:
            raise ApiError(404, "product not found")
        return 200, product

    def patch_product(self, sku, body):
        changes = validate_patch_body(body)
        try:
            product = self.db.update_product(sku, changes)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        if product is None:
            raise ApiError(404, "product not found")
        return 200, product


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CatalogIQ"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # ---- plumbing

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status, body, content_type="application/json", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _read_json(self):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                # Also blocks cross-site "simple" form/text posts from other origins.
                raise ApiError(415, "Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                raise ApiError(400, "invalid Content-Length") from None
            if length <= 0:
                raise ApiError(400, "request body is required")
            if length > MAX_BODY_BYTES:
                raise ApiError(413, "request body too large")
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ApiError(400, "body is not valid JSON") from None

        def _dispatch(self, method):
            try:
                # Defends the localhost-only API against DNS-rebinding pages.
                if self.headers.get("Host", "") not in app.allowed_hosts:
                    raise ApiError(403, "host not allowed")
                url = urlparse(self.path)
                path = url.path
                if method in ("GET", "HEAD") and path in STATIC_FILES:
                    return self._static(path)
                if not path.startswith("/api/"):
                    raise ApiError(404, "not found")
                status, body = self._route(method, path, parse_qs(url.query))
                self._send(status, body)
            except ApiError as exc:
                if method not in ("GET", "HEAD"):
                    self.close_connection = True  # body may be unread; do not reuse
                self._send(exc.status, {"error": exc.message})
            except Exception:
                self.close_connection = True
                ref = secrets.token_hex(4)
                log.exception("unhandled error ref=%s %s %s", ref, method,
                              urlparse(self.path).path)
                self._send(500, {"error": f"internal error (ref {ref})"})

        def _route(self, method, path, params):
            parts = [unquote(p) for p in path[len("/api/"):].split("/")]
            match method, parts:
                case "GET", ["health"]:
                    return app.health()
                case "GET", ["metrics"]:
                    return app.get_metrics()
                case "POST", ["jobs"]:
                    return app.create_job(self._read_json())
                case "GET", ["jobs", job_id]:
                    return app.get_job(job_id)
                case "GET", ["products"]:
                    return app.list_products(params)
                case "GET", ["products", sku] if sku:
                    return app.get_product(sku)
                case "PATCH", ["products", sku] if sku:
                    return app.patch_product(sku, self._read_json())
                case _, (["health"] | ["metrics"] | ["jobs"] | ["jobs", _] |
                         ["products"] | ["products", _]):
                    raise ApiError(405, "method not allowed")
            raise ApiError(404, "not found")

        def _static(self, path):
            name, ctype = STATIC_FILES[path]
            data = (STATIC_DIR / name).read_bytes()
            self._send(200, data, ctype, SECURITY_HEADERS)

        # ---- verbs

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("HEAD")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process silently bind the same port,
    # so use SO_EXCLUSIVEADDRUSE there instead. On POSIX it only skips TIME_WAIT.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Skip HTTPServer.server_bind: its socket.getfqdn() reverse DNS lookup can
        # stall startup for seconds on Windows.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class Server6(Server):
    address_family = socket.AF_INET6


def make_servers(app):
    """Bind the listening socket(s). Returns a list of servers sharing one port.

    HOST=localhost (the default) listens on both 127.0.0.1 and ::1. Windows
    resolves "localhost" to ::1 first, and a refused connection there costs
    about 2 s before clients fall back to IPv4, so both must be served.
    """
    host = app.config.host
    hosts = ["127.0.0.1", "::1"] if host == "localhost" else [host]
    handler = make_handler(app)
    port = app.config.port
    servers = []
    for h in hosts:
        cls = Server6 if ":" in h else Server
        try:
            server = cls((h, port), handler)
        except OSError as exc:
            if servers and h == "::1":  # IPv6 disabled on this machine; IPv4 still works
                log.warning("not listening on [::1]:%d (%s)", port, exc)
                continue
            raise
        port = server.server_address[1]  # when port 0 picked a free port, reuse it
        servers.append(server)
    app.set_port(port)
    return servers
