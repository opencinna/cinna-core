"""``X-Robots-Tag`` on every HTTP response: nothing the backend serves is indexed.

Capability URLs (``/a/v/<cap>/…``, ``/a/z/<cap>``, agent-start, OAuth and
discovery endpoints) carry the credential in the path, so a crawler that ever
sees such a link must be told not to index it. The defence is ``noindex``, not
``robots.txt Disallow``: a disallowed URL is never fetched, so its ``noindex``
is never seen and the bare URL (token included) can still be listed.

This pure-ASGI wrapper is registered last in ``app.main`` so it is the
outermost user middleware: it stamps responses written by every inner layer,
including the ``/a/`` preflights and 500s ``ArtifactContentMiddleware`` sends
itself, the upload-limit 413s, and the mounted sub-apps (``/mcp``, ``/ws``).
Any value an inner layer set is overwritten. An unhandled exception that
escapes before a response started becomes a plain 500 carrying the header
(Starlette's ``ServerErrorMiddleware`` sits outside us and would otherwise
answer without it); the exception is re-raised so it is still logged.
WebSocket and lifespan scopes pass through untouched.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Single backend source of the directive. The SPA (frontend/index.html) and
# nginx `location /` (frontend/nginx.conf, docs/infrastructure/nginx_setup.md)
# carry the same string for the responses the backend never sees.
ROBOTS_DIRECTIVE = "noindex, nofollow, noarchive, nosnippet, noimageindex"
ROBOTS_HEADER = "X-Robots-Tag"


class RobotsTagMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)[ROBOTS_HEADER] = ROBOTS_DIRECTIVE
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if not started:
                await self._internal_error(send)
            raise

    @staticmethod
    async def _internal_error(send: Send) -> None:
        body = b"Internal Server Error"
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (ROBOTS_HEADER.lower().encode(), ROBOTS_DIRECTIVE.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
