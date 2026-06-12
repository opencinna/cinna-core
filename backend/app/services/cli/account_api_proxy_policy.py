"""
Account-CLI API-proxy exclusion policy — the single chokepoint.

The ``cinna api <METHOD> <path>`` escape hatch (Phase 3) re-dispatches an
authenticated call into (most of) the platform API on behalf of the account
token's owning user. ``assert_api_proxy_allowed`` is the **one** place that
decides whether a target ``(method, path)`` may be reached through that hatch.

Mirrors the egress-guard pattern (``assert_url_allowed`` is the single chokepoint
for outbound MCP/OAuth traffic): pure, dependency-free, and exhaustively
unit-tested — one assertion per excluded prefix.

Policy = **denylist** (default-allow). The escape hatch's whole purpose is to call
anything in the generated API reference the local agent might need, so we allow
the broad control plane and **subtract** the sensitive surfaces (credential
values, user management, admin, CLI recursion, MFA, other clients' auth, the
zero-knowledge sync store, audit log, auth/session issuance, and
streaming/exec routes the buffered proxy can't represent).

The denylist is *defense in depth*, not a substitute for per-route authorization:
the inner call runs as the **real user** with a normal JWT, so every downstream
ownership / ``require_developer`` / superuser check still applies. The denylist
exists to remove **categories** the account token must never touch even if the
user could touch them in the UI — i.e. it is strictly *more* restrictive than the
user's own rights, never less.
"""
from __future__ import annotations

import re

from app.core.config import settings


class ApiProxyDenied(Exception):
    """Raised when a target ``(method, path)`` is excluded from the escape hatch."""

    def __init__(self, reason: str, message: str):
        # reason ∈ {"excluded_path", "excluded_method", "malformed_path"}
        self.reason = reason
        self.message = message
        super().__init__(message)


# ── Denylist constants (one test per entry) ──────────────────────────────────
#
# Matched by **path-segment prefix** on the normalized, post-``API_V1_STR`` path
# (e.g. ``/api/v1/credentials/...``). A bare ``credentials`` here matches the
# whole credentials surface regardless of trailing segments. Entries are written
# without the ``/api/v1`` prefix; ``_excluded_prefixes()`` prepends it once.

# Sensitive capability categories removed from the generic hatch entirely.
EXCLUDED_PREFIXES: tuple[str, ...] = (
    # Credential VALUES — Decision 6 forbids credential reads. The connect verbs
    # use their dedicated /account/connect/* endpoints, not this proxy.
    "credentials",
    "ai-credentials",
    "oauth-credentials",
    "credential-shares",
    # User management — only the two exact GETs below are allowed (see
    # USER_PATH_ALLOW_EXACT). Everything else under /users is denied.
    "users",
    # Admin surfaces.
    "admin",
    "admin-environments",
    "private",
    # The entire CLI router (incl. /cli/account/*): no recursion, no token
    # self-management, no calling api-proxy again.
    "cli",
    # Other clients' auth + the zero-knowledge sync store.
    "desktop-auth",
    "app-auth",
    "app-sync",
    # 2FA management — never reachable from a machine credential.
    "mfa",
    # Audit log read (already SKIP_TAGS in the API reference).
    "security-events",
    # Auth / session issuance (already SKIP_TAGS; deny defensively).
    "login",
    "oauth",
    "auth",
    "token",
)

# Exact (method, path) pairs carved BACK IN from an excluded prefix. The only
# user-management reads the hatch may perform: the caller's own profile and the
# minimal user-search projection (needed to wire shares).
USER_PATH_ALLOW_EXACT: tuple[tuple[str, str], ...] = (
    ("GET", "users/me"),
    ("GET", "users/search"),
)

# Streaming / SSE / exec / create-flow routes the buffered proxy cannot
# represent. Most are already unreachable as non-/account CLI routes; listed for
# completeness and matched by path-segment prefix (method-agnostic).
STREAMING_DENY: tuple[str, ...] = (
    "agents/create-flow-stream",
    "agents/create-flow",
)

# Allowed HTTP methods for the buffered escape hatch.
ALLOWED_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE"}
)

# A normalized path must look like ``/api/v1/<segment>...`` with safe characters
# only (no ``..``, no scheme, no query — query is carried out-of-band).
_SAFE_PATH_RE = re.compile(r"^/api/v1(?:/[A-Za-z0-9._~%{}-]+)*/?$")


def _excluded_prefixes() -> tuple[str, ...]:
    """Excluded prefixes with the ``API_V1_STR`` prefix applied once."""
    base = settings.API_V1_STR.rstrip("/")
    return tuple(f"{base}/{p}" for p in EXCLUDED_PREFIXES)


def _streaming_prefixes() -> tuple[str, ...]:
    base = settings.API_V1_STR.rstrip("/")
    return tuple(f"{base}/{p}" for p in STREAMING_DENY)


def _user_allow_exact() -> frozenset[tuple[str, str]]:
    base = settings.API_V1_STR.rstrip("/")
    return frozenset(
        (method, f"{base}/{path}") for method, path in USER_PATH_ALLOW_EXACT
    )


def _segment_prefix_match(path: str, prefix: str) -> bool:
    """True if ``path`` equals ``prefix`` or is a child segment of it.

    ``/api/v1/users`` and ``/api/v1/users/me`` match ``/api/v1/users``;
    ``/api/v1/users-public`` does NOT (segment boundary respected).
    """
    return path == prefix or path.startswith(prefix + "/")


def assert_api_proxy_allowed(method: str, normalized_path: str) -> None:
    """The ONE gate deciding whether the account-CLI escape hatch may call a route.

    Raises :class:`ApiProxyDenied` otherwise.

    ``normalized_path`` must already be prefixed with ``settings.API_V1_STR`` and
    carry a single leading slash, no ``..`` segments, and no query string (the
    caller guarantees this; we re-assert defensively here).
    """
    method = (method or "").upper()
    path = normalized_path.rstrip("/") or normalized_path

    # 1. Shape / safety — re-assert the caller's contract defensively.
    if ".." in normalized_path or not _SAFE_PATH_RE.match(normalized_path):
        raise ApiProxyDenied(
            "malformed_path",
            f"Malformed proxy path '{normalized_path}'. Paths must be relative to "
            "the API root, contain no '..' segments, and target /api/v1.",
        )

    # 2. Method allowlist (the proxy is buffered HTTP only).
    if method not in ALLOWED_METHODS:
        raise ApiProxyDenied(
            "excluded_method",
            f"HTTP method '{method}' is not supported by the escape hatch "
            f"(allowed: {', '.join(sorted(ALLOWED_METHODS))}).",
        )

    # 3. Streaming / exec / create-flow routes the buffered proxy can't represent.
    for prefix in _streaming_prefixes():
        if _segment_prefix_match(path, prefix):
            raise ApiProxyDenied(
                "excluded_method",
                "Streaming/long-running routes (create-flow, SSE, exec) are not "
                "supported via the escape hatch.",
            )

    # 4. Denylist — but allow the exact user-path carve-outs first.
    if (method, path) in _user_allow_exact():
        return

    for prefix in _excluded_prefixes():
        if _segment_prefix_match(path, prefix):
            raise ApiProxyDenied(
                "excluded_path",
                f"The escape hatch may not call '{normalized_path}' "
                "(credential/user-management/admin/CLI/auth surfaces are excluded). "
                "Use a dedicated command if available.",
            )

    # Default-allow: every other control-plane route is reachable, still subject
    # to the inner route's own per-route authorization.
