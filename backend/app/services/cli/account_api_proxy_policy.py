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

Besides segment prefixes, ``EXCLUDED_PATTERNS`` (any method) and
``EXCLUDED_WRITE_PATTERNS`` (one method) deny single routes by full regex match
on the API-relative path: routes whose response carries a secret value
(issued API keys, minted bearer tokens, raw workspace files).

Matching runs on the percent-DECODED path, because that is what the router
sees (httpx decodes ``%63redentials`` to ``credentials`` before dispatch).
"""
from __future__ import annotations

import re
from urllib.parse import unquote

# ``app.core.config.settings`` is imported lazily (``_api_base``) so the pure
# ``route_refusal`` can be imported by the repo-root platform-knowledge sync
# script, which builds the API-reference catalogue without backend Settings.


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

# Single routes denied by FULL regex match on the API-relative path (no
# ``/api/v1/`` prefix, no trailing slash), any method. Each is a route whose
# response carries a secret value; see the deny sweep in
# docs/plans/agent_api_issued_keys_cli_plan.md §2a.
AGENT_API_KEYS_PATTERN = r"agents/[^/]+/agent-api/keys(?:/.*)?"
EXCLUDED_PATTERNS: tuple[str, ...] = (
    # Issued API keys: issue returns a self-issued value; rotate starts a
    # countdown on a value the CLI cannot deliver. Use `cinna agent-api access`.
    AGENT_API_KEYS_PATTERN,
    # Raw workspace files: credentials/credentials.json holds every linked
    # credential's decrypted value, and a folder download zips it.
    r"environments/[^/]+/workspace/(?:download|view-file)(?:/.*)?",
    # Same files through a dashboard block (the file path rides the query).
    r"dashboards/[^/]+/blocks/[^/]+/env-file",
)

# Share links: every response carries the share URL (the share token) and the
# security code, so the whole surface is web-only (team-lead decision, C18).
SHARE_PATTERNS: tuple[str, ...] = (
    r"agents/[^/]+/guest-shares(?:/.*)?",
    r"agents/[^/]+/webapp-shares(?:/.*)?",
    # Token-keyed guest logins: answer a guest session JWT.
    r"guest-share/[^/]+/auth",
    r"webapp-share/[^/]+/auth",
)

# (method, pattern) pairs: routes that mint a bearer secret and return it once.
# Their listings and updates return prefixes only and stay reachable.
EXCLUDED_WRITE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("POST", r"agents/[^/]+/access-tokens"),  # A2A access token
    ("POST", r"agents/[^/]+/mcp-connectors/[^/]+/tokens"),  # MCP direct token
    ("POST", r"agents/[^/]+/acp-connectors/[^/]+/tokens"),  # ACP token
    ("POST", r"agents/[^/]+/webhooks/(?:git-source|script|session)"),
    ("POST", r"agents/[^/]+/webhooks/[^/]+/regenerate-token"),
    ("POST", r"tasks/[^/]+/triggers/webhook"),
    ("POST", r"tasks/[^/]+/triggers/[^/]+/regenerate-token"),
)

# The dedicated CLI verb that replaces a denied route, by pattern.
DEDICATED_VERBS: dict[str, str] = {
    AGENT_API_KEYS_PATTERN: "cinna agent-api access",
}

_AGENT_API_KEYS_MESSAGE = (
    "Issued API keys are managed with 'cinna agent-api access'; the escape "
    "hatch never carries key values."
)
_SECRET_ROUTE_MESSAGE = (
    "The escape hatch never returns secret values (tokens, keys, raw workspace "
    "files). Use the web app for this action."
)
_SHARE_ROUTE_MESSAGE = (
    "Share links and security codes are managed in the web app; the escape "
    "hatch never returns them."
)
_PREFIX_MESSAGE = (
    "The escape hatch may not call '{path}' (credential/user-management/admin/"
    "CLI/auth surfaces are excluded). Use a dedicated command if available."
)
_STREAMING_MESSAGE = (
    "Streaming/long-running routes (create-flow, SSE, exec) are not supported "
    "via the escape hatch."
)

# Allowed HTTP methods for the buffered escape hatch.
ALLOWED_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE"}
)

# A normalized path must look like ``/api/v1/<segment>...`` with safe characters
# only (no ``..``, no scheme, no query — query is carried out-of-band).
_SAFE_PATH_RE = re.compile(r"^/api/v1(?:/[A-Za-z0-9._~%{}-]+)*/?$")

# Control characters after decoding. Starlette routes a trailing ``%0A``
# (``$`` matches before a final newline) while ``re.fullmatch`` misses it,
# which would reopen every excluded pattern. A plain space is allowed: route
# templates never end a literal segment in a space, so ``keys%20`` reaches no
# key route (checked against the app's route table).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _api_base() -> str:
    """``settings.API_V1_STR`` without a trailing slash (lazy import)."""
    from app.core.config import settings

    return settings.API_V1_STR.rstrip("/")


def _segment_prefix_match(path: str, prefix: str) -> bool:
    """True if ``path`` equals ``prefix`` or is a child segment of it.

    ``/api/v1/users`` and ``/api/v1/users/me`` match ``/api/v1/users``;
    ``/api/v1/users-public`` does NOT (segment boundary respected).
    """
    return path == prefix or path.startswith(prefix + "/")


def route_refusal(method: str, relative_path: str) -> ApiProxyDenied | None:
    """The refusal for a well-formed ``(method, path)``, or None when allowed.

    ``relative_path`` is API-relative (``agents/{id}/...``: no ``/api/v1/``, no
    trailing slash, already percent-decoded). Pure and settings-free, so the
    API-reference catalogue marks exactly the routes this policy refuses.
    """
    method = (method or "").upper()
    path = relative_path.strip("/")

    # Streaming / exec / create-flow routes the buffered proxy can't represent.
    if any(_segment_prefix_match(path, p) for p in STREAMING_DENY):
        return ApiProxyDenied("excluded_method", _STREAMING_MESSAGE)

    # Prefix denylist, after the exact user-path carve-outs.
    if (method, path) in USER_PATH_ALLOW_EXACT:
        return None
    if any(_segment_prefix_match(path, p) for p in EXCLUDED_PREFIXES):
        return ApiProxyDenied(
            "excluded_path", _PREFIX_MESSAGE.format(path=f"/{path}")
        )

    # Single routes whose response carries a secret value.
    if re.fullmatch(AGENT_API_KEYS_PATTERN, path):
        return ApiProxyDenied("excluded_path", _AGENT_API_KEYS_MESSAGE)
    if any(re.fullmatch(pattern, path) for pattern in EXCLUDED_PATTERNS):
        return ApiProxyDenied("excluded_path", _SECRET_ROUTE_MESSAGE)
    if any(re.fullmatch(pattern, path) for pattern in SHARE_PATTERNS):
        return ApiProxyDenied("excluded_path", _SHARE_ROUTE_MESSAGE)
    for denied_method, pattern in EXCLUDED_WRITE_PATTERNS:
        if method == denied_method and re.fullmatch(pattern, path):
            return ApiProxyDenied("excluded_path", _SECRET_ROUTE_MESSAGE)
    return None


def dedicated_verb(relative_path: str) -> str | None:
    """The CLI verb that replaces a denied route, when one exists."""
    path = relative_path.strip("/")
    for pattern, verb in DEDICATED_VERBS.items():
        if re.fullmatch(pattern, path):
            return verb
    return None


def assert_api_proxy_allowed(method: str, normalized_path: str) -> None:
    """The ONE gate deciding whether the account-CLI escape hatch may call a route.

    Raises :class:`ApiProxyDenied` otherwise.

    ``normalized_path`` must already be prefixed with ``settings.API_V1_STR`` and
    carry a single leading slash, no ``..`` segments, and no query string (the
    caller guarantees this; we re-assert defensively here).
    """
    method = (method or "").upper()
    # Match on the decoded path: the inner dispatch decodes percent-escapes
    # before routing, so an encoded segment must not slip past the denylist.
    decoded_path = unquote(normalized_path)

    # 1. Shape / safety — re-assert the caller's contract defensively.
    if (
        ".." in decoded_path
        # A ``.`` segment is refused, never collapsed.
        or "." in decoded_path.split("/")
        or not _SAFE_PATH_RE.match(normalized_path)
        or _CONTROL_CHARS_RE.search(decoded_path)
        # A '%' left after one decode is double encoding.
        or "%" in decoded_path
        or "//" in decoded_path
    ):
        raise ApiProxyDenied(
            "malformed_path",
            f"Malformed proxy path '{normalized_path}'. Paths must be relative to "
            "the API root, target /api/v1, and contain no control characters, "
            "'.'/'..' segments, '//' or double encoding.",
        )

    # 2. Method allowlist (the proxy is buffered HTTP only).
    if method not in ALLOWED_METHODS:
        raise ApiProxyDenied(
            "excluded_method",
            f"HTTP method '{method}' is not supported by the escape hatch "
            f"(allowed: {', '.join(sorted(ALLOWED_METHODS))}).",
        )

    # 3. Streaming routes, the prefix denylist and the single secret routes.
    base = _api_base() + "/"
    relative = decoded_path[len(base):] if decoded_path.startswith(base) else ""
    refusal = route_refusal(method, relative)
    if refusal is not None:
        raise refusal

    # Default-allow: every other control-plane route is reachable, still subject
    # to the inner route's own per-route authorization.
