#!/usr/bin/env python3
"""
Market Intelligence Harness — main entry point.

Each module lives in modules/<name>/ and exposes a single register_routes(router)
function. To add a new module, create the folder and call register_routes here.
"""

import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from modules import Response
from modules.rrg import register_routes as register_rrg
from modules.schwab import register_routes as register_schwab


# ---------------------------------------------------------------------------
# Request / Router
# ---------------------------------------------------------------------------

class Request:
    """Wraps an incoming HTTP request for route handlers."""

    __slots__ = ("method", "path", "qs", "headers", "_rfile", "_body")

    def __init__(self, method, path, qs, headers, rfile):
        self.method  = method
        self.path    = path
        self.qs      = qs
        self.headers = headers
        self._rfile  = rfile
        self._body   = None

    def json_body(self):
        """Lazily parse and cache the JSON request body."""
        if self._body is None:
            length = int(self.headers.get("Content-Length", 0))
            raw    = self._rfile.read(length) if length else b"{}"
            self._body = json.loads(raw) if raw.strip() else {}
        return self._body


class Router:
    """Maps (method, path) → handler function."""

    def __init__(self):
        self._routes = {}

    def get(self, path, fn):
        self._routes[("GET", path)] = fn

    def post(self, path, fn):
        self._routes[("POST", path)] = fn

    def dispatch(self, req):
        return self._routes.get((req.method, req.path))


# ---------------------------------------------------------------------------
# Route registration — add new modules here
# ---------------------------------------------------------------------------

router = Router()
register_rrg(router)
register_schwab(router)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _respond(self, resp):
        body = resp.body.encode("utf-8") if isinstance(resp.body, str) else resp.body
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # quiet

    def _handle(self, method):
        parsed  = urlparse(self.path)
        req     = Request(
            method  = method,
            path    = parsed.path,
            qs      = parse_qs(parsed.query),
            headers = self.headers,
            rfile   = self.rfile,
        )
        handler = router.dispatch(req)
        if handler is None:
            self._respond(Response.error("not found", 404))
        else:
            try:
                self._respond(handler(req))
            except Exception as e:
                self._respond(Response.error(str(e), 500))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def main():
    port   = int(os.environ.get("PORT", "8000"))
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"\n  Market Intelligence Harness →  http://localhost:{port}")
    print("  RRG: /   ·   Schwab: /schwab.html\n")
    print("  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()
