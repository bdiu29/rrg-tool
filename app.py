#!/usr/bin/env python3
"""
Market Intelligence Harness — main entry point.

Each module lives in modules/<name>/ and exposes a single register_routes(router)
function. To add a new module, create the folder and call register_routes here.
"""

import json
import os
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from modules import Response
from modules.home import register_routes as register_home
from modules.rrg import register_routes as register_rrg
from modules.schwab import register_routes as register_schwab
from modules.breadth import register_routes as register_breadth
from modules.screener import register_routes as register_screener
from modules.rankings import register_routes as register_rankings
from modules.themes import register_routes as register_themes
from modules.flow import register_routes as register_flow
from modules.canslim import register_routes as register_canslim
from modules.news import register_routes as register_news
from modules.macro import register_routes as register_macro
from modules.harness import register_routes as register_harness
from modules.research import register_routes as register_research


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
register_home(router)
register_rrg(router)
register_schwab(router)
register_breadth(router)
register_screener(router)
register_rankings(router)
register_themes(router)
register_flow(router)
register_canslim(router)
register_news(router)
register_macro(router)
register_harness(router)
register_research(router)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _respond(self, resp):
        body = resp.body.encode("utf-8") if isinstance(resp.body, str) else resp.body
        try:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            # Client hung up before we finished replying (refresh, navigation, or a
            # canceled in-flight fetch). There's no socket left to answer — drop it
            # quietly instead of letting the error path double-fault on a dead pipe.
            pass

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
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"\n  Market Intelligence Harness →  http://localhost:{port}")
    print("  Hub: /   ·   RRG: /rrg.html   ·   Breadth: /breadth.html   ·   Schwab: /schwab.html\n")
    print("  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()
