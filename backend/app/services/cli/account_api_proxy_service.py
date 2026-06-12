"""
Account CLI generic API escape hatch — ``cinna api <METHOD> <path>``.

The escape hatch lets the account token's *owning user* call (most of) the
platform API for anything not yet wrapped in a dedicated verb. It does so without
weakening the Phase 1 structural guarantee (account tokens only authenticate
``/account/*``):

  1. The account token authenticates the OUTER ``/account/api-proxy`` route only,
     via ``AccountCLIContextDep`` — unchanged.
  2. The target ``(method, path)`` passes through the SINGLE exclusion chokepoint
     (``assert_api_proxy_allowed``) BEFORE any dispatch.
  3. The inner call is made with a **freshly-minted, request-scoped, short-lived
     (8 s) normal user JWT** for the owning user, re-dispatched against the
     in-process ASGI app. Downstream routes see an ordinary authenticated user
     via the unchanged ``get_current_user`` — zero per-route changes.

The minted JWT never leaves the backend; the CLI only ever holds the account
token. The buffered proxy targets JSON control-plane calls — streaming, binary,
and multipart are out of scope (denied by the chokepoint / response-type guard).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from fastapi import Request, Response
from fastapi import status as http_status
from sqlmodel import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.models.cli.account_convenience import AccountApiProxyRequest
from app.models.cli.cli_token import CLIToken
from app.models.events.security_event import (
    CLI_ACCOUNT_API_PROXY_CALL,
    SecurityEventCreate,
)
from app.models.users.user import User
from app.services.cli.account_api_proxy_policy import (
    ApiProxyDenied,
    assert_api_proxy_allowed,
)
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)

# Internal re-dispatch identity TTL. Long enough to cover one inner round-trip,
# short enough that a leaked token (it never leaves the backend anyway) is inert.
_INNER_JWT_TTL_SECONDS = 8

# Inner-dispatch timeout. The escape hatch targets JSON control-plane calls, not
# long-running work; a tight bound also limits how long the inner call holds a
# SECOND pooled DB connection (its own SessionDep) concurrently with the outer
# request's, bounding pool pressure under concurrent escape-hatch use.
_INNER_DISPATCH_TIMEOUT_SECONDS = 30.0

# Response headers forwarded back to the CLI (allowlist). Hop-by-hop and
# auth-bearing headers are dropped.
_FORWARDED_RESPONSE_HEADERS = ("content-type", "content-disposition")

# Marker set ONLY on the buffered passthrough response (mirrored inner-API
# result). Lets clients deterministically tell a mirrored inner response from a
# hatch-own refusal without string-matching error detail wording.
_PROXIED_MARKER_HEADER = "X-Cinna-Proxied"

# Internal ASGI base URL — never resolved over the network (ASGITransport).
_INTERNAL_BASE_URL = "http://internal"


def _client_ip(request: Request | None) -> str | None:
    """Best-effort source IP for audit. Prefers the first X-Forwarded-For hop."""
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return None


class _RateLimiter:
    """In-memory sliding-window throttle keyed by account-token id.

    Mirrors the in-process throttle pattern used by event handlers / MCP rate
    limiting. Process-local (one window per worker); a backstop against a runaway
    local agent loop, not a billing control.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, limit_per_min: int) -> float | None:
        """Record a hit. Return ``None`` if allowed, else seconds until retry."""
        now = time.monotonic()
        window_start = now - 60.0
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if len(bucket) >= limit_per_min:
                retry_after = max(1.0, 60.0 - (now - bucket[0]))
                return retry_after
            bucket.append(now)
            return None


class AccountApiProxyService:
    """Server-side escape hatch — in-process ASGI re-dispatch behind the chokepoint."""

    _rate_limiter = _RateLimiter()

    # ── Path normalization ───────────────────────────────────────────────
    @staticmethod
    def _normalize_path(raw_path: str) -> str:
        """Normalize a caller-supplied path to ``/api/v1/<...>``.

        The caller supplies a path relative to the API root (``agents``,
        ``/agents``, ``agents/{id}/credentials``). We strip any whitespace,
        collapse a leading ``/api/v1`` the caller may have included, ensure a
        single leading slash, and prefix ``settings.API_V1_STR``.

        Raises ``ApiProxyDenied(malformed_path)`` for ``..`` segments or a query
        string smuggled into the path (query is carried out-of-band).
        """
        base = settings.API_V1_STR.rstrip("/")
        path = (raw_path or "").strip()

        if "?" in path or "#" in path or "\\" in path:
            raise ApiProxyDenied(
                "malformed_path",
                "Proxy path must not contain a query string, fragment, or "
                "backslash. Use the 'query' field for query parameters.",
            )
        if ".." in path:
            raise ApiProxyDenied(
                "malformed_path",
                "Proxy path must not contain '..' segments.",
            )

        # Single leading slash.
        path = "/" + path.lstrip("/")
        # Tolerate a caller that already included the /api/v1 prefix.
        if path == base or path.startswith(base + "/"):
            normalized = path
        else:
            normalized = base + path
        # Collapse accidental double slashes (but keep the single leading one).
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        return normalized

    # ── Main entry point ─────────────────────────────────────────────────
    @staticmethod
    async def proxy(
        db: Session,
        account_token: CLIToken,
        user: User,
        req: AccountApiProxyRequest,
        request: Request,
    ) -> Response:
        """Run policy + re-dispatch and return a buffered passthrough response.

        Raises ``ApiProxyDenied`` (route maps to 403/400) for excluded targets;
        raises ``HTTPException`` for rate limit (429), oversize (413/502), and
        streaming responses (502).
        """
        from fastapi import HTTPException

        token_key = str(account_token.id)

        # 1. Rate limit (per account token).
        retry_after = AccountApiProxyService._rate_limiter.check(
            token_key, settings.ACCOUNT_API_PROXY_RATE_LIMIT_PER_MIN
        )
        if retry_after is not None:
            raise HTTPException(
                status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Escape-hatch rate limit exceeded for this account token.",
                headers={"Retry-After": str(int(retry_after))},
            )

        # 2. Normalize + validate path, then run the SINGLE chokepoint.
        #    Both raise ApiProxyDenied; emit the audit event for exclusion hits.
        try:
            normalized_path = AccountApiProxyService._normalize_path(req.path)
            assert_api_proxy_allowed(req.method, normalized_path)
        except ApiProxyDenied as denied:
            await AccountApiProxyService._audit_denied(
                db, user, account_token, req, denied, request
            )
            raise

        # 3. Enforce request body size (cheap pre-dispatch guard).
        if req.json_body is not None:
            try:
                import json as _json

                encoded = _json.dumps(req.json_body).encode("utf-8")
            except (TypeError, ValueError) as e:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail=f"Request body is not JSON-serializable: {e}",
                )
            if len(encoded) > settings.ACCOUNT_API_PROXY_MAX_BODY_BYTES:
                raise HTTPException(
                    status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Request body exceeds the escape-hatch size limit.",
                )

        # 4. Mint a request-scoped normal user JWT (8 s TTL). Never returned to
        #    the client; presents as an ordinary user JWT to get_current_user.
        inner_jwt = create_access_token(
            user.id, expires_delta=timedelta(seconds=_INNER_JWT_TTL_SECONDS)
        )

        # 5. In-process ASGI re-dispatch against the REAL middleware + route
        #    stack. The app is imported lazily to avoid an import cycle.
        from app.main import app as fastapi_app

        headers = {
            "Authorization": f"Bearer {inner_jwt}",
            "Accept": "application/json",
        }
        request_kwargs: dict = {"headers": headers}
        if req.query:
            request_kwargs["params"] = req.query
        if req.json_body is not None:
            request_kwargs["json"] = req.json_body

        # We do NOT blanket-follow redirects (a downstream RedirectResponse to an
        # off-policy path would bypass the chokepoint). Instead we transparently
        # follow ONLY FastAPI's trailing-slash normalization (``/agents`` → 307
        # ``/agents/``): same path + a trailing slash, re-checked through the
        # chokepoint. Anything else is returned to the caller as-is.
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=_INTERNAL_BASE_URL,
            timeout=_INNER_DISPATCH_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            inner = await client.request(
                req.method, normalized_path, **request_kwargs
            )
            if inner.status_code in (307, 308):
                location = inner.headers.get("location", "")
                # Location may be absolute (http://internal/api/v1/agents/) or a
                # bare path; compare only the path component.
                location_path = urlsplit(location).path
                if location_path == normalized_path + "/":
                    assert_api_proxy_allowed(req.method, location_path)
                    inner = await client.request(
                        req.method, location_path, **request_kwargs
                    )

        # 6. Response guards: reject streaming content type and oversize bodies.
        content_type = inner.headers.get("content-type", "")
        if content_type.startswith("text/event-stream"):
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail="Streaming responses are not supported via the escape hatch.",
            )
        body = inner.content
        if len(body) > settings.ACCOUNT_API_PROXY_MAX_RESPONSE_BYTES:
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail="Inner response exceeds the escape-hatch size limit.",
            )

        logger.info(
            "account api-proxy %s %s → %s (token=%s)",
            req.method,
            normalized_path,
            inner.status_code,
            account_token.id,
        )

        # 7. Buffered passthrough: status 1:1, forwarded header allowlist.
        #    The ``X-Cinna-Proxied`` marker is the deterministic classifier for
        #    clients: present = this is a mirrored INNER-API response (status/body
        #    come from the target route); absent = the hatch ITSELF refused the
        #    call (policy denial, malformed path, rate limit, size guard, SSE
        #    502), since those all raise ``HTTPException`` and never reach here.
        forwarded = {
            k: v
            for k, v in inner.headers.items()
            if k.lower() in _FORWARDED_RESPONSE_HEADERS
        }
        forwarded[_PROXIED_MARKER_HEADER] = "1"
        return Response(
            content=body,
            status_code=inner.status_code,
            media_type=inner.headers.get("content-type"),
            headers=forwarded,
        )

    # ── Audit ────────────────────────────────────────────────────────────
    @staticmethod
    async def _audit_denied(
        db: Session,
        user: User,
        account_token: CLIToken,
        req: AccountApiProxyRequest,
        denied: ApiProxyDenied,
        request: Request,
    ) -> None:
        """Write a SecurityEvent on exclusion hits (``excluded_*``) only.

        ``malformed_path`` is a caller mistake, not a probe — not audited.
        """
        if not denied.reason.startswith("excluded"):
            return
        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                event_type=CLI_ACCOUNT_API_PROXY_CALL,
                severity="medium",
                agent_id=None,
                details={
                    "method": req.method,
                    "path": req.path,
                    "reason": denied.reason,
                    "account_token_id": str(account_token.id),
                    "ip": _client_ip(request),
                },
            ),
        )
