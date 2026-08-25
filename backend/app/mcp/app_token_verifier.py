"""App MCP Token Verifier — bearer-token auth for the app-level MCP server.

Two questions, in order, and both must be yes:

1. **Is this token real?** Hashed lookup against ``app_mcp_token``, plus expiry
   and revocation. Mirrors ``MCPTokenVerifier`` but with no ``connector_id``.
2. **Is App MCP available to this user right now?** App MCP is a
   ``ServerChannel`` (``channel_type="app_mcp"``, the platform's one
   ``authenticated`` transport), so the admin kill switch, the channel's
   ``visibility`` + grant allowlist, and the user's own per-channel toggle all
   apply — resolved through ``ChannelPolicyService`` like every other channel.

Question 2 is checked **at use, not at issue**. ``app_mcp_token`` rows are
untouched by any of it: a token minted while the channel was open keeps
working only for as long as the channel stays open for its owner, which is the
entire point of an admin having a switch.

Both failures return ``None``, which the MCP layer renders as the same 401. A
distinguishable "your token is fine but you are not allowed" would be an
oracle for a server's channel configuration, and there is nothing the caller
could do with the distinction anyway.

WHY THERE IS A CACHE, AND WHAT IT COSTS
---------------------------------------
``verify_token`` runs once per HTTP request to ``/mcp/app/mcp`` — every
``tools/call``, every ``tools/list``, every ``prompts/list``, every SSE
reconnect. Question 2 is two more database reads on that path, so the resolved
answer is cached per user id, in this process's memory, for
``settings.APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS``.

Three properties, none negotiable:

* **The miss path fails closed.** A lookup that *raises* denies. A kill switch
  that becomes advisory the moment the database is unhappy is not a kill
  switch. The exception is never cached either — caching a failure as an
  answer would extend a transient outage into a TTL-long denial, and caching
  it as ``True`` would be the security bug.
* **Revocation actually lands.** Within the TTL, per backend process. The TTL
  is therefore the documented revocation delay and the reason to keep it
  short.
* **"Not materialized yet" is not an error.** The channel row is created
  lazily by ``ServerChannelService.get_or_create_singleton``, the single
  accessor every caller shares. That is what lets this verifier fail closed on
  a real fault without denying every user on a deployment whose row simply had
  not been touched yet.

The cache is process-local. A multi-worker deployment has one per worker, each
converging within the TTL; nothing here assumes a single process.
"""
import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from mcp.server.auth.provider import AccessToken, TokenVerifier
from sqlmodel import select

from app.core.config import settings
from app.core.db import create_session
from app.mcp.context_vars import mcp_authenticated_user_id_var
from app.models.app_mcp.app_mcp_token import AppMCPToken
from app.services.server_channels.adapters.app_mcp import AppMCPChannelAdapter
from app.services.server_channels.channel_policy_service import ChannelPolicyService
from app.services.server_channels.server_channel_service import ServerChannelService

logger = logging.getLogger(__name__)


def _hash_token(token: str) -> str:
    """SHA256 hash of a token string."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class _AvailabilityEntry:
    """One cached answer and the monotonic instant it stops being usable."""

    available: bool
    expires_at: float


# Guarded by ``_availability_lock``. Keyed by user id; both answers are cached,
# because a *denied* user hitting an MCP client's reconnect loop is exactly the
# traffic pattern that would otherwise hammer the database.
_availability_cache: dict[uuid.UUID, _AvailabilityEntry] = {}
_availability_lock = threading.Lock()


def _cached_availability(user_id: uuid.UUID, now: float) -> bool | None:
    """A fresh cached answer, or ``None`` when there is none to use."""
    with _availability_lock:
        entry = _availability_cache.get(user_id)
        if entry is None or entry.expires_at <= now:
            return None
        return entry.available


def _store_availability(
    user_id: uuid.UUID, available: bool, now: float, ttl: int
) -> None:
    """Cache one resolved answer, evicting if the bound has been reached.

    ``ttl`` is passed in rather than read here: the caller has already read it
    to decide whether the cache is in play at all, and a second independent
    read could see a different value — the freshness window an entry is
    written with would then not be the one the decision to write it was made
    under.

    Eviction drops expired entries first and clears the rest only if that was
    not enough. Clearing costs one extra lookup per active user; it can never
    produce a wrong answer, because every entry is re-derivable from the
    database and absence means "go and ask".

    Only a *resolved* answer reaches here. A lookup that failed is not an
    answer and is never stored — see :func:`is_app_mcp_available`.
    """
    with _availability_lock:
        if len(_availability_cache) >= settings.APP_MCP_AVAILABILITY_CACHE_MAX_ENTRIES:
            for stale in [
                key
                for key, entry in _availability_cache.items()
                if entry.expires_at <= now
            ]:
                _availability_cache.pop(stale, None)
            if (
                len(_availability_cache)
                >= settings.APP_MCP_AVAILABILITY_CACHE_MAX_ENTRIES
            ):
                _availability_cache.clear()
        _availability_cache[user_id] = _AvailabilityEntry(
            available=available, expires_at=now + ttl
        )


def reset_availability_cache() -> None:
    """Drop every cached answer. For process-lifecycle callers and tests.

    Not part of the revocation mechanism — revocation is the TTL. This exists
    so a caller that knows the cache is meaningless (a test, a reload) can say
    so rather than sleeping.
    """
    with _availability_lock:
        _availability_cache.clear()


def _resolve_availability(user_id: uuid.UUID) -> bool | None:
    """Is the App MCP channel available to ``user_id``? ``None`` if unknown.

    Three-valued on purpose. ``True``/``False`` are *answers* — the database
    was asked and it said so. ``None`` means the question could not be
    answered at all, and the two must not collapse into one ``False``: the
    caller denies on both, but only an answer may be cached. Returning a plain
    ``False`` here is how a single connection blip becomes a TTL-long denial
    for a user the database is perfectly willing to allow.

    ``ResolvedChannelPolicy.is_available`` is already the full conjunction —
    ``channel.enabled`` AND access AND the user's own toggle — so the kill
    switch is *not* re-checked here. Reading ``channel.enabled`` separately
    would be a second copy of a rule ``ChannelPolicyService`` exists to own,
    and the module docstring there forbids exactly that.
    """
    try:
        with create_session() as db:
            channel = ServerChannelService.get_or_create_singleton(
                db, AppMCPChannelAdapter.channel_type
            )
            return ChannelPolicyService.resolve(db, channel, user_id).is_available
    except Exception:
        logger.exception(
            "[AppMCP] Channel availability lookup failed for user %s — denying",
            user_id,
        )
        return None


def is_app_mcp_available(user_id: uuid.UUID) -> bool:
    """Cached, fail-closed answer to "may this user use App MCP right now?".

    A failed lookup denies and is *not* stored, in either direction: caching
    it as ``True`` would be the security bug, and caching it as ``False``
    would keep denying a user for the rest of the TTL after the database had
    already recovered — every retry answered from memory without ever asking
    the healthy database again. So the deny is returned, and the very next
    call resolves afresh.

    A TTL of ``<= 0`` bypasses the cache entirely, in both directions. That is
    the deterministic path an API-level test uses to observe a revocation
    without sleeping, and a legitimate (costly) production setting for an
    operator who wants the switch to bite immediately.
    """
    ttl = settings.APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS
    if ttl <= 0:
        # ``is True`` rather than a bare truthiness check: the bypass path has
        # to turn an unresolved ``None`` into a denial too.
        return _resolve_availability(user_id) is True

    now = time.monotonic()
    cached = _cached_availability(user_id, now)
    if cached is not None:
        return cached

    available = _resolve_availability(user_id)
    if available is None:
        return False
    _store_availability(user_id, available, now, ttl)
    return available


class AppMCPTokenVerifier(TokenVerifier):
    """Verifies App MCP bearer tokens, then the channel's availability."""

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a bearer token and return an ``AccessToken`` if usable.

        Token validity first, availability second, and the order matters twice
        over: an invalid token must not cost a policy resolution, and an
        unknown token must not be able to populate a cache entry keyed on a
        user id it never proved it owns.
        """
        token_hash = _hash_token(token)

        with create_session() as db:
            token_record = db.exec(
                select(AppMCPToken).where(
                    AppMCPToken.token_hash == token_hash,
                    AppMCPToken.token_type == "access",
                )
            ).first()

            if not token_record:
                logger.debug("[AppMCP] Token not found in database")
                return None

            # Check expiry (DB stores naive UTC datetimes)
            if token_record.expires_at < datetime.now(UTC).replace(tzinfo=None):
                logger.debug("[AppMCP] Token expired")
                return None

            # Check revocation
            if token_record.is_revoked:
                logger.debug("[AppMCP] Token revoked")
                return None

            user_id = token_record.user_id
            scopes = (
                [s for s in token_record.scope.split(" ") if s]
                if token_record.scope
                else []
            )
            expires_at_ts = (
                int(token_record.expires_at.timestamp())
                if token_record.expires_at
                else None
            )
            client_id = token_record.client_id
            resource = token_record.resource or None

        # Outside the token session on purpose: the availability lookup opens
        # its own short-lived one, and holding two nested sessions across a
        # call that may write (the lazy channel materialization) is how a
        # verifier ends up sharing a transaction with a row it created.
        if not is_app_mcp_available(user_id):
            # Same ``None`` an invalid token gets. The caller must not be able
            # to tell "revoked token" from "channel switched off for you".
            logger.debug(
                "[AppMCP] App MCP is not available to user %s — token refused",
                user_id,
            )
            return None

        # Propagate authenticated user identity to tool handlers via ContextVar.
        mcp_authenticated_user_id_var.set(str(user_id))

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at_ts,
            resource=resource,
        )
