"""Baseline security headers on backend HTTP responses (C2, backend part).

Sets, only where a response does not already carry them:

* ``X-Content-Type-Options: nosniff`` — except on agent-served content
  (webapps, shared workspace views), whose files arrive with whatever type
  the agent gave them and must keep rendering;
* ``Referrer-Policy: strict-origin-when-cross-origin``;
* ``Strict-Transport-Security`` — only for https requests (the scheme
  itself, or ``X-Forwarded-Proto: https`` from our own private-network
  proxy) and never when ``SECURITY_HSTS_MAX_AGE_SECONDS`` is 0.

Deliberately never ``X-Frame-Options`` or a CSP: the SPA iframes backend
content (webapps, ``/a/``, ``/d/``, file viewers). Those live on nginx and in
the SPA's build-time CSP. Pure ASGI, registered just inside
``RobotsTagMiddleware``; WebSocket and lifespan scopes pass through.
"""
import re

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.services.common.rate_limiter import is_private_peer

_API = re.escape(settings.API_V1_STR)
# Agent-served content: no nosniff (see module docstring).
_AGENT_CONTENT_PATH = re.compile(
    rf"^{_API}/(?:webapp|agents/[^/]+/webapp|shared/workspace)(?:/|$)"
)

_NOSNIFF = ("X-Content-Type-Options", "nosniff")
_REFERRER = ("Referrer-Policy", "strict-origin-when-cross-origin")
_HSTS = "Strict-Transport-Security"


def _is_https(scope: Scope) -> bool:
    if scope.get("scheme") == "https":
        return True
    client = scope.get("client")
    peer = client[0] if client else ""
    if not is_private_peer(peer):
        return False
    proto = Headers(scope=scope).get("x-forwarded-proto", "")
    return proto.split(",", 1)[0].strip().lower() == "https"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        nosniff = not _AGENT_CONTENT_PATH.match(scope.get("path", ""))
        hsts_max_age = settings.SECURITY_HSTS_MAX_AGE_SECONDS
        hsts = hsts_max_age > 0 and _is_https(scope)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if nosniff:
                    headers.setdefault(*_NOSNIFF)
                headers.setdefault(*_REFERRER)
                if hsts:
                    headers.setdefault(_HSTS, f"max-age={hsts_max_age}")
            await send(message)

        await self.app(scope, receive, send_wrapper)
