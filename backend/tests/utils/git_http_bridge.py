"""Loopback HTTP bridge that lets a real ``git`` binary talk to the test app.

The app under test runs inside the ``TestClient`` (in-process ASGI, test DB
session, savepoint rollback). A ``git`` subprocess needs a TCP port, so this
module serves a tiny ``http.server`` on ``127.0.0.1:<ephemeral>`` and forwards
every request -- method, path, headers, the raw (possibly chunked) body -- to
the ``TestClient`` and relays status, headers and body back. Nothing about the
app is faked: git smart HTTP, auth, the grants check, the ``pre-receive`` hook
and the post-push bookkeeping all run for real.

Requests are serialised with a lock: the test DB session is a single
connection and must not see two requests at once.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

_RELAYED_REQUEST_HEADERS = {
    "authorization",
    "content-type",
    "content-encoding",
    "accept",
    "git-protocol",
    "user-agent",
}
_RELAYED_RESPONSE_HEADERS = ("Content-Type", "Cache-Control", "WWW-Authenticate")


class GitHttpBridge:
    def __init__(self, client: TestClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # silence
                pass

            def _read_body(self) -> bytes:
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    body = b""
                    while True:
                        size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                        if size == 0:
                            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                                pass  # trailers
                            return body
                        body += self.rfile.read(size)
                        self.rfile.readline()
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _forward(self) -> None:
                body = self._read_body()
                headers = {
                    k: v for k, v in self.headers.items() if k.lower() in _RELAYED_REQUEST_HEADERS
                }
                with bridge._lock:
                    bridge.requests.append((self.command, self.path, dict(headers)))
                    response = bridge._client.request(
                        self.command, self.path, headers=headers, content=body
                    )
                payload = response.content
                self.send_response(response.status_code)
                for name in _RELAYED_RESPONSE_HEADERS:
                    if name in response.headers:
                        self.send_header(name, response.headers[name])
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

            do_GET = _forward
            do_POST = _forward

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def repo_url(self, repo_id: str) -> str:
        return f"{self.base_url}/git/{repo_id}.git"

    def start(self) -> "GitHttpBridge":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
