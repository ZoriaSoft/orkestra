"""A tiny OpenAI-compatible mock server for integration tests.

Runs a real :class:`http.server.ThreadingHTTPServer` on an ephemeral
localhost port, so probes exercise the full httpx + socket path instead of
mocking library internals. Behaviour is controlled per test via attributes:

- ``models``: ids returned by ``GET */models``
- ``require_key``: expected bearer token; ``None`` disables the auth check
- ``mode``: ``ok`` | ``bad_json`` | ``wrong_shape`` | ``error`` | ``slow``
- ``delay_seconds``: extra latency injected before answering
- ``requests``: captured ``(path, headers)`` pairs for assertions
- ``chat_handler``: ``fn(request_body) -> str | dict`` serving
  ``POST */chat/completions``; return a plain string for assistant content or
  a dict ``{"content": str, "usage": {...}}``. ``None`` -> every POST fails
  loudly with a 500.
- ``chat_requests``: captured POST bodies for assertions
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockOpenAI/0.1"

    def log_message(self, *_args: Any) -> None:  # keep test output clean
        return

    @property
    def mock(self) -> "MockOpenAIServer":
        return self.server.mock  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        mock = self.mock
        mock.requests.append((self.command, self.path, dict(self.headers)))

        if mock.delay_seconds:
            time.sleep(mock.delay_seconds)

        if not self.path.rstrip("/").endswith("/models"):
            self._json(404, {"error": {"message": f"no route {self.path}"}})
            return

        if mock.require_key is not None:
            expected = f"Bearer {mock.require_key}"
            if self.headers.get("Authorization") != expected:
                self._json(401, {"error": {"message": "invalid api key"}})
                return

        if mock.mode == "error":
            self._json(500, {"error": {"message": "mock exploded"}})
            return
        if mock.mode == "bad_json":
            body = b"this is not json{"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if mock.mode == "wrong_shape":
            self._json(200, {"unexpected": True})
            return

        self._json(
            200,
            {
                "object": "list",
                "data": [
                    {"id": name, "object": "model", "created": 0, "owned_by": "mock"}
                    for name in mock.models
                ],
            },
        )

    def do_POST(self) -> None:
        mock = self.mock
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw_body) if raw_body else {}
        except ValueError:
            body = {"_unparseable": raw_body.decode(errors="replace")}
        mock.requests.append((self.command, self.path, dict(self.headers)))
        mock.chat_requests.append(body)

        if mock.delay_seconds:
            time.sleep(mock.delay_seconds)

        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": f"no route {self.path}"}})
            return

        if mock.require_key is not None:
            expected = f"Bearer {mock.require_key}"
            if self.headers.get("Authorization") != expected:
                self._json(401, {"error": {"message": "invalid api key"}})
                return

        if mock.mode == "error":
            self._json(500, {"error": {"message": "mock exploded"}})
            return

        if mock.chat_handler is None:
            self._json(
                500,
                {"error": {"message": "chat_handler not configured on mock"}},
            )
            return

        try:
            reply = mock.chat_handler(body)
        except Exception as exc:  # surface test-side failures as 500s
            self._json(500, {"error": {"message": f"chat_handler: {exc!r}"}})
            return

        if isinstance(reply, str):
            reply = {"content": reply}
        content = reply.get("content", "")
        usage = reply.get(
            "usage", {"prompt_tokens": 42, "completion_tokens": 17}
        )
        self._json(
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": body.get("model", "mock"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            },
        )

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MockOpenAIServer:
    """Lifecycle wrapper around the mock HTTP server."""

    def __init__(self) -> None:
        self.models: list[str] = ["mock-strong-1", "mock-cheap-1", "mock-cheap-2"]
        self.require_key: str | None = None
        self.mode: str = "ok"
        self.delay_seconds: float = 0.0
        self.chat_handler: Callable[[dict[str, Any]], str | dict[str, Any]] | None = None
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.chat_requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.mock = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        """Ephemeral port the server listens on."""
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        """Base URL without a ``/v1`` suffix (client must append it)."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def v1_url(self) -> str:
        """Base URL with a ``/v1`` suffix."""
        return f"{self.base_url}/v1"

    def start(self) -> "MockOpenAIServer":
        """Start serving in a daemon thread."""
        self._thread.start()
        return self

    def stop(self) -> None:
        """Shut the server down and wait briefly for the thread."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def last_headers(self) -> dict[str, str]:
        """Headers of the most recent captured request."""
        return self.requests[-1][2] if self.requests else {}
