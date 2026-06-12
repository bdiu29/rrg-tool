"""
Home module — hub homepage linking every module, with live status badges.

Routes registered:
  GET / → index.html
"""

from pathlib import Path

from modules import Response

_MODULE_DIR = Path(__file__).resolve().parent


def _handle_index(req):
    with open(_MODULE_DIR / "index.html") as f:
        return Response.html(f.read())


def register_routes(router):
    router.get("/", _handle_index)
