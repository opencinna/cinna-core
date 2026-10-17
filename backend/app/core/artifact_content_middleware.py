"""CORS and baseline security headers for artifact content (``/a/``), D2.

The global ``CORSMiddleware`` reflects the request origin and adds
``Access-Control-Allow-Credentials: true``. Artifact content runs in an opaque
(sandboxed) origin and must be readable cross-origin **without** credentials:
``Access-Control-Allow-Origin: *`` and nothing else. This pure-ASGI wrapper is
registered after ``CORSMiddleware`` so it is the outer layer and sees every
``/a/`` response last:

- ``OPTIONS`` preflights are answered here and never reach the app;
- on ``http.response.start`` the CORS headers are rewritten (credentials and
  reflected origin removed, ``Vary: Origin`` dropped, ``*`` set, the range
  headers exposed);
- any ``/a/`` response that lacks a CSP (an unmatched route, a framework
  error) gets the error security headers, and every error status gets
  ``Cache-Control: no-store``;
- an unhandled exception becomes a 500 that carries the same headers.
"""
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_PREFIX = "/a/"

_STRIPPED = (
    "access-control-allow-credentials",
    "access-control-allow-origin",
    "access-control-expose-headers",
    "access-control-allow-methods",
    "access-control-allow-headers",
)


def _error_headers() -> dict[str, str]:
    # Imported lazily: the header policy lives with the content service.
    from app.services.artifacts.artifact_content import error_headers

    return error_headers()


def _cors_headers() -> dict[str, str]:
    from app.services.artifacts.artifact_content import EXPOSED_HEADERS

    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": EXPOSED_HEADERS,
    }


def _rewrite(message: Message) -> None:
    headers = MutableHeaders(scope=message)
    for name in _STRIPPED:
        del headers[name]
    vary = [v.strip() for v in headers.get("vary", "").split(",") if v.strip()]
    remaining = [v for v in vary if v.lower() != "origin"]
    del headers["vary"]
    if remaining:
        headers["Vary"] = ", ".join(remaining)
    for name, value in _cors_headers().items():
        headers[name] = value
    status = int(message.get("status", 200))
    if "content-security-policy" not in headers:
        for name, value in _error_headers().items():
            if name.lower() not in headers:
                headers[name] = value
    if status >= 400:
        headers["Cache-Control"] = "no-store"


class ArtifactContentMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(CONTENT_PREFIX):
            await self.app(scope, receive, send)
            return

        if scope.get("method") == "OPTIONS":
            await self._preflight(send)
            return

        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                _rewrite(message)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if not started:
                await self._plain(send, 500, b"Internal Server Error")
            raise

    @staticmethod
    async def _preflight(send: Send) -> None:
        headers = {
            **_error_headers(),
            **_cors_headers(),
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "Range, If-None-Match, If-Range",
            "Access-Control-Max-Age": "600",
        }
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _plain(send: Send, status: int, body: bytes) -> None:
        headers = {
            **_error_headers(),
            **_cors_headers(),
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Length": str(len(body)),
        }
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            }
        )
        await send({"type": "http.response.body", "body": body})
