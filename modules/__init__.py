import json


class Response:
    """HTTP response returned by every route handler."""

    __slots__ = ("body", "status", "content_type")

    def __init__(self, body, status=200, content_type="application/json"):
        self.body         = body
        self.status       = status
        self.content_type = content_type

    @classmethod
    def json(cls, data, status=200):
        return cls(json.dumps(data), status, "application/json")

    @classmethod
    def html(cls, content):
        return cls(content, 200, "text/html; charset=utf-8")

    @classmethod
    def error(cls, message, status=500):
        return cls(json.dumps({"error": message}), status, "application/json")
