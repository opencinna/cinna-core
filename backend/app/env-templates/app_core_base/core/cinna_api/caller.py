"""
Request-scoped caller identity for the agent REST API SDK.

When a consumer agent calls this producer's API, the platform proxy verifies the
caller's identity token, resolves the cinna-core user who owns the calling
install, and injects trusted ``X-Cinna-Caller-*`` headers into the forwarded
request. This accessor reads those headers so a handler can tell **who** is
calling and apply per-user authorization.

The headers are authoritative: the proxy strips any client-supplied copies and
sets them only after verifying the identity token, so a handler can trust them.
When the caller is anonymous (no identity token, expired token, or a legacy
shared-token connection) the headers are absent and ``Caller.is_anonymous`` is
``True`` — the producer decides what an anonymous caller may do.

Usage in ``agent_api/orders.py``::

    from cinna_api import api, caller, Caller, error

    @api.get("/orders")
    def list_orders(me: Caller = caller):
        if me.is_anonymous:
            raise error(401, "Sign-in required")
        # me.user_id / me.email / me.username identify the calling user
        return {"orders": orders_for(me.user_id)}

``caller`` is a FastAPI dependency, so it is resolved fresh per request and shows
up correctly in the harvested OpenAPI spec without leaking into the public schema
(the header params are marked ``include_in_schema=False``).
"""
from dataclasses import dataclass, field

from fastapi import Header

# Header names the proxy injects. Kept in sync with
# ``app.services.agent_api.agent_api_identity_service`` on the backend.
_USER_ID_HEADER = "X-Cinna-Caller-User-Id"
_EMAIL_HEADER = "X-Cinna-Caller-Email"
_USERNAME_HEADER = "X-Cinna-Caller-Username"
_SCOPES_HEADER = "X-Cinna-Caller-Scopes"


@dataclass(frozen=True)
class Caller:
    """The platform-resolved identity of the user making this request.

    All fields are ``None``/empty for an anonymous caller. ``scopes`` are the
    per-user capability scopes the producer's owner granted this caller from the
    "Access & Scopes" UI; the platform resolves them live on every call. An
    attributed caller with no grant (or a producer that has not enabled per-user
    scopes) has an empty ``scopes`` list — the producer decides what such a
    caller may do.
    """

    user_id: str | None = None
    email: str | None = None
    username: str | None = None
    scopes: list[str] = field(default_factory=list)

    @property
    def is_anonymous(self) -> bool:
        """True when the platform could not attribute this call to a user."""
        return self.user_id is None

    def has_scope(self, scope: str) -> bool:
        """True when the caller's resolved scopes include ``scope``."""
        return scope in self.scopes


def _parse_scopes(raw: str | None) -> list[str]:
    """Parse the space-separated ``X-Cinna-Caller-Scopes`` header into a list.

    The proxy encodes scopes OAuth-style (space-separated). Empty / missing
    header ⇒ empty list. Resilient to extra whitespace.
    """
    if not raw:
        return []
    return [s for s in raw.split() if s]


def _resolve_caller(
    x_cinna_caller_user_id: str | None = Header(default=None, include_in_schema=False),
    x_cinna_caller_email: str | None = Header(default=None, include_in_schema=False),
    x_cinna_caller_username: str | None = Header(default=None, include_in_schema=False),
    x_cinna_caller_scopes: str | None = Header(default=None, include_in_schema=False),
) -> Caller:
    """FastAPI dependency: build a ``Caller`` from the trusted proxy headers."""
    return Caller(
        user_id=x_cinna_caller_user_id or None,
        email=x_cinna_caller_email or None,
        username=x_cinna_caller_username or None,
        scopes=_parse_scopes(x_cinna_caller_scopes),
    )


# Ready-to-use dependency marker. Authors annotate a handler param with it:
#   def handler(me: Caller = caller): ...
# Importing ``Depends`` lazily keeps this module import-light and mirrors the
# rest of the SDK's "import from one place" ergonomics.
from fastapi import Depends  # noqa: E402

caller = Depends(_resolve_caller)
