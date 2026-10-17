"""
OAuth Refresh Service - the single refresh primitive for OAuth credentials.

Every Google OAuth access-token refresh (credential sync, environment start,
stream start, owner's manual refresh) goes through
``OAuthRefreshService.refresh_if_needed``. It serialises refreshes of one
credential across coroutines (in-process ``asyncio.Lock``) and across workers
(``pg_try_advisory_xact_lock`` on the caller's session), and re-reads the
credential after acquiring so a token another worker just refreshed is not
refreshed again.

The try-lock is non-blocking on purpose: a blocking row lock taken by a sync
``Session`` inside an async route would block the event loop while the holder
awaits Google. A busy lock is polled with ``asyncio.sleep`` for a bounded time,
after which the caller proceeds with the current token. The xact lock is
released by the commit that stores the result.

Refresh bookkeeping lives in the encrypted credential blob
(``refresh_attempted_at``, ``refresh_error``, ``refresh_error_kind``,
``refresh_error_at``). ``reauth_required`` stops automatic attempts until the
owner re-authorizes or refreshes manually (``force=True``); ``provider_error``
is retried after ``OAUTH_REFRESH_COOLDOWN_SECONDS``.

Invariants: ``refresh_if_needed`` never emits events, never syncs
environments, never logs or returns the refresh token. ``access_token_for_env``
(the agent-env endpoint's logic) is the one caller here that pushes a refreshed
payload to the agent's running environments.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Literal

from sqlalchemy import text
from sqlmodel import Session, select

from app.core import security
from app.core.config import settings
from app.models import AgentCredentialAccessTokenResponse, AgentCredentialLink, Credential
from app.services.credentials.oauth_credentials_service import (
    OAUTH_SCOPES,
    REFRESH_ERROR_NOT_CONFIGURED,
    REFRESH_ERROR_PROVIDER,
    REFRESH_ERROR_REAUTH_REQUIRED,
    OAuthCredentialsService,
    OAuthRefreshError,
)

logger = logging.getLogger(__name__)

RefreshStatus = Literal["fresh", "refreshed", "busy", "cooldown", "failed", "skipped"]
LockState = Literal["acquired", "not_needed", "busy"]

# Google OAuth credential types handled by refresh_if_needed.
GOOGLE_OAUTH_TYPES = frozenset(OAUTH_SCOPES.keys())

# Advisory-lock namespace ("OAUT") — the first int of the two-int lock key.
ADVISORY_LOCK_NAMESPACE = 0x4F415554
LOCK_WAIT_TIMEOUT_SECONDS = 5.0
LOCK_POLL_INTERVAL_SECONDS = 0.25

# In-process locks, one map per event loop. An ``asyncio.Lock`` is bound to the
# loop it is first used on, and some code paths run coroutines on throwaway
# loops in worker threads (``asyncio.run`` inside a scheduler thread). Sharing
# one map would raise "bound to a different event loop" on contention, or wake
# a waiter from the wrong thread. Cross-loop (and cross-worker) exclusion is
# the advisory xact lock's job; this map only dedupes within one loop.
_credential_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[uuid.UUID, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)
_credential_locks_guard = threading.Lock()
_MAX_CREDENTIAL_LOCKS = 1000


def _credential_lock(credential_id: uuid.UUID) -> asyncio.Lock:
    """Return (or create) the running loop's in-process lock for a credential.

    Bounded per loop: evicts unlocked entries once a loop's map reaches
    ``_MAX_CREDENTIAL_LOCKS`` (same policy as ``get_session_lock``).
    """
    loop = asyncio.get_running_loop()
    with _credential_locks_guard:
        locks = _credential_locks.get(loop)
        if locks is None:
            locks = {}
            _credential_locks[loop] = locks
        if credential_id not in locks:
            if len(locks) >= _MAX_CREDENTIAL_LOCKS:
                for cid in [c for c, lock in locks.items() if not lock.locked()]:
                    del locks[cid]
            locks[credential_id] = asyncio.Lock()
        return locks[credential_id]


def _advisory_key(credential_id: uuid.UUID) -> int:
    """32-bit signed advisory-lock key derived from the credential id."""
    return int.from_bytes(credential_id.bytes[:4], "big", signed=True)


@dataclass
class RefreshOutcome:
    status: RefreshStatus
    credential: Credential
    error_kind: str | None = None
    error: str | None = None
    #: True when this call actually called Google (False for pre-check results).
    attempted: bool = False


class OAuthRefreshService:
    """Locked, idempotent refresh of Google OAuth credentials."""

    @staticmethod
    def _decrypt(credential: Credential) -> dict[str, Any] | None:
        if not credential.encrypted_data:
            return None
        try:
            data = json.loads(security.decrypt_field(credential.encrypted_data))
        except Exception as e:
            logger.error(
                "Could not decrypt OAuth credential %s: %s", credential.id, type(e).__name__
            )
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _precheck(
        data: dict[str, Any], min_valid_seconds: int, now: float
    ) -> RefreshStatus | None:
        """Return a terminal status when no refresh should run, else None.

        ``skipped``: no expiry recorded (not authorized yet / legacy row).
        ``fresh``: more than ``min_valid_seconds`` left.
        ``failed``: Google rejected the refresh token earlier (reauth required).
        ``cooldown``: the last attempt failed within the cooldown window.
        """
        expires_at = data.get("expires_at")
        if not isinstance(expires_at, (int, float)):
            return "skipped"
        if expires_at - now > min_valid_seconds:
            return "fresh"
        kind = data.get("refresh_error_kind")
        if kind == REFRESH_ERROR_REAUTH_REQUIRED:
            return "failed"
        attempted_at = data.get("refresh_attempted_at")
        if (
            kind == REFRESH_ERROR_PROVIDER
            and isinstance(attempted_at, (int, float))
            and now - attempted_at < settings.OAUTH_REFRESH_COOLDOWN_SECONDS
        ):
            return "cooldown"
        return None

    @staticmethod
    def _outcome_from_data(
        status: RefreshStatus, credential: Credential, data: dict[str, Any]
    ) -> RefreshOutcome:
        if status in ("failed", "cooldown"):
            return RefreshOutcome(
                status=status,
                credential=credential,
                error_kind=data.get("refresh_error_kind"),
                error=data.get("refresh_error"),
            )
        return RefreshOutcome(status=status, credential=credential)

    @staticmethod
    def _try_advisory_lock(session: Session, key: int) -> bool:
        """Non-blocking transaction-scoped advisory lock (test seam)."""
        return bool(
            session.execute(
                text("SELECT pg_try_advisory_xact_lock(:ns, :key)"),
                {"ns": ADVISORY_LOCK_NAMESPACE, "key": key},
            ).scalar_one()
        )

    @staticmethod
    @asynccontextmanager
    async def locked(
        session: Session,
        credential: Credential,
        needs_refresh: Callable[[Credential], bool] | None = None,
    ) -> AsyncIterator[LockState]:
        """Serialise a refresh of ``credential`` across coroutines and workers.

        Yields:
            ``acquired`` — the caller holds both locks and must commit its
                result (the commit releases the advisory xact lock);
            ``not_needed`` — ``needs_refresh`` returned False after a re-read
                (another worker refreshed meanwhile);
            ``busy`` — another worker held the lock for the whole wait.

        ``needs_refresh`` is evaluated on the re-read credential while waiting
        and right after acquiring. ``None`` means "always needed" (forced).
        On exit an acquired lock is released by ``session.commit()``; an
        exception inside the block rolls back instead.

        Caller contract: ``session`` must have no pending (unflushed or
        uncommitted) changes of its own. The lock re-reads ``credential``
        (discarding unflushed edits to it) and ends the session's transaction
        on exit, which commits — or on error rolls back — anything else the
        caller left pending. The block's own writes are committed here, not by
        the code inside the block.
        """
        if session.new or session.dirty or session.deleted:
            logger.warning(
                "OAuth refresh lock for credential %s taken on a session with pending "
                "changes; they will be committed or rolled back with the refresh",
                credential.id,
            )
        check = needs_refresh or (lambda _c: True)
        key = _advisory_key(credential.id)
        async with _credential_lock(credential.id):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + LOCK_WAIT_TIMEOUT_SECONDS
            state: LockState
            while True:
                acquired = OAuthRefreshService._try_advisory_lock(session, key)
                session.refresh(credential)
                if not check(credential):
                    state = "not_needed"
                    break
                if acquired:
                    state = "acquired"
                    break
                if loop.time() >= deadline:
                    state = "busy"
                    break
                await asyncio.sleep(LOCK_POLL_INTERVAL_SECONDS)

            if state == "busy":
                logger.info(
                    "OAuth refresh lock for credential %s busy; using current token",
                    credential.id,
                )
                yield state
                return

            try:
                yield state
            except BaseException:
                session.rollback()
                raise
            # Ends the transaction: releases the xact lock when we hold it.
            session.commit()

    @staticmethod
    async def refresh_if_needed(
        session: Session,
        credential: Credential,
        *,
        min_valid_seconds: int,
        force: bool = False,
        raise_on_error: bool = False,
    ) -> RefreshOutcome:
        """Refresh a Google OAuth credential if it expires within ``min_valid_seconds``.

        Args:
            session: Caller's database session (the advisory lock is taken on it).
            credential: Credential to refresh; non-Google types are ``skipped``.
            min_valid_seconds: Refresh when the token has this little time left.
            force: Skip the freshness / reauth / cooldown pre-checks (manual refresh).
            raise_on_error: Raise ``OAuthRefreshError`` instead of returning ``failed``.

        Returns:
            ``RefreshOutcome`` — never contains token values.
        """
        if credential.type.value not in GOOGLE_OAUTH_TYPES:
            return RefreshOutcome(status="skipped", credential=credential)

        data = OAuthRefreshService._decrypt(credential)
        if data is None:
            return RefreshOutcome(status="skipped", credential=credential)

        if not force:
            status = OAuthRefreshService._precheck(data, min_valid_seconds, time.time())
            if status is not None:
                outcome = OAuthRefreshService._outcome_from_data(status, credential, data)
                if status == "failed" and raise_on_error:
                    raise OAuthRefreshError(
                        outcome.error_kind or REFRESH_ERROR_REAUTH_REQUIRED,
                        outcome.error or "Re-authorization required",
                    )
                return outcome

        if not settings.google_oauth_enabled:
            if raise_on_error:
                raise OAuthRefreshError(REFRESH_ERROR_NOT_CONFIGURED, "Google OAuth is not configured")
            logger.debug("Google OAuth not configured; cannot refresh credential %s", credential.id)
            return RefreshOutcome(
                status="failed",
                credential=credential,
                error_kind=REFRESH_ERROR_NOT_CONFIGURED,
                error="Google OAuth is not configured",
            )

        def needs_refresh(c: Credential) -> bool:
            current = OAuthRefreshService._decrypt(c)
            if current is None:
                return False
            return OAuthRefreshService._precheck(current, min_valid_seconds, time.time()) is None

        async with OAuthRefreshService.locked(
            session, credential, needs_refresh=None if force else needs_refresh
        ) as state:
            if state == "busy":
                return RefreshOutcome(status="busy", credential=credential)

            data = OAuthRefreshService._decrypt(credential)
            if data is None:
                return RefreshOutcome(status="skipped", credential=credential)

            if state == "not_needed":
                status = (
                    OAuthRefreshService._precheck(data, min_valid_seconds, time.time())
                    or "fresh"
                )
                return OAuthRefreshService._outcome_from_data(status, credential, data)

            outcome = await OAuthRefreshService._refresh_locked(session, credential, data)

        # Raised only after ``locked`` committed, so the recorded error survives.
        if raise_on_error and outcome.status == "failed":
            raise OAuthRefreshError(
                outcome.error_kind or REFRESH_ERROR_PROVIDER,
                outcome.error or "Token refresh failed",
            )
        return outcome

    @staticmethod
    async def _refresh_locked(
        session: Session,
        credential: Credential,
        data: dict[str, Any],
    ) -> RefreshOutcome:
        """Call Google and stage the result. Caller holds the lock; ``locked`` commits.

        The OAuth callback writes the credential without this lock, so the
        blob read under the lock is snapshotted and re-checked before writing:
        if it changed meanwhile (the owner re-authorized), the stale result is
        dropped and the credential is reported ``fresh``.
        """
        snapshot = credential.encrypted_data
        now = time.time()
        data["refresh_attempted_at"] = int(now)
        try:
            token_data = await OAuthCredentialsService._exchange_refresh_token(
                data.get("refresh_token") or ""
            )
        except Exception as e:
            error = (
                e
                if isinstance(e, OAuthRefreshError)
                else OAuthRefreshError(
                    REFRESH_ERROR_PROVIDER, f"Token refresh failed ({type(e).__name__})"
                )
            )
            if OAuthRefreshService._changed_since(session, credential, snapshot):
                return RefreshOutcome(status="fresh", credential=credential, attempted=True)
            data["refresh_error"] = error.message
            data["refresh_error_kind"] = error.kind
            data["refresh_error_at"] = int(now)
            OAuthRefreshService._store(session, credential, data)
            if error.kind == REFRESH_ERROR_PROVIDER:
                logger.error(
                    "OAuth refresh failed for credential %s: %s", credential.id, error.message
                )
            else:
                logger.warning(
                    "OAuth refresh failed for credential %s (%s): %s",
                    credential.id, error.kind, error.message,
                )
            return RefreshOutcome(
                status="failed",
                credential=credential,
                error_kind=error.kind,
                error=error.message,
                attempted=True,
            )

        if OAuthRefreshService._changed_since(session, credential, snapshot):
            return RefreshOutcome(status="fresh", credential=credential, attempted=True)
        OAuthCredentialsService.apply_token_response(data, token_data)
        OAuthRefreshService._store(session, credential, data)
        logger.info("Refreshed OAuth access token for credential %s", credential.id)
        return RefreshOutcome(status="refreshed", credential=credential, attempted=True)

    @staticmethod
    async def issue_access_token(
        session: Session,
        credential: Credential,
        *,
        min_ttl: int,
    ) -> AgentCredentialAccessTokenResponse:
        """Return the credential's Google access token, valid for ``min_ttl`` seconds
        when it can be (refreshing through ``refresh_if_needed`` when needed).

        The one place that maps a ``RefreshOutcome`` to an error code. Callers
        resolve access first (agent link, Desktop share rules) and wrap this.
        ``min_ttl`` is clamped to ``[0, OAUTH_ON_DEMAND_MAX_MIN_TTL_SECONDS]``.

        Raises:
            AgentEnvTokenError: ``not_refreshable`` (422),
                ``reauthorization_required`` (409), ``refresh_in_progress`` /
                ``provider_error`` (502, ``retry_after``),
                ``oauth_not_configured`` (503).
        """
        min_ttl = max(0, min(min_ttl, settings.OAUTH_ON_DEMAND_MAX_MIN_TTL_SECONDS))
        credential_id = credential.id
        if credential.is_placeholder or credential.type.value not in GOOGLE_OAUTH_TYPES:
            raise AgentEnvTokenError(
                "not_refreshable", 422,
                f"Credential {credential_id} is not an authorized Google OAuth credential; "
                f"it has no access token to refresh.",
            )

        outcome = await OAuthRefreshService.refresh_if_needed(
            session, credential, min_valid_seconds=min_ttl
        )
        logger.info("Access token for credential %s: %s", credential_id, outcome.status)
        data = OAuthRefreshService._decrypt(outcome.credential) or {}
        now = time.time()
        access_token = data.get("access_token")
        expires_at = data.get("expires_at")
        token_valid = (
            bool(access_token)
            and isinstance(expires_at, (int, float))
            and expires_at > now
        )

        if outcome.status == "skipped" or not access_token:
            raise AgentEnvTokenError(
                "reauthorization_required", 409,
                f"Credential {credential_id} has not been authorized yet. Ask the user to "
                f"authorize it in the credential settings.",
            )
        if outcome.status == "failed":
            OAuthRefreshService._raise_for_failure(outcome, data, now)
        if outcome.status == "busy" and not token_valid:
            raise AgentEnvTokenError(
                "refresh_in_progress", 502,
                "Another refresh of this credential is in progress; try again in a moment.",
                retry_after=1,
            )
        if outcome.status == "cooldown" and not token_valid:
            raise AgentEnvTokenError(
                "provider_error", 502,
                "Google did not issue a new access token yet; try again shortly.",
                retry_after=_cooldown_left(data, now),
            )

        return AgentCredentialAccessTokenResponse(
            access_token=access_token,
            token_type=data.get("token_type") or "Bearer",
            expires_at=int(expires_at),
            refreshed=outcome.status == "refreshed",
        )

    @staticmethod
    async def access_token_for_env(
        session: Session,
        agent_id: uuid.UUID,
        credential_id: uuid.UUID,
        min_ttl: int,
        known_expires_at: int | None = None,
    ) -> AgentCredentialAccessTokenResponse:
        """Return a Google OAuth access token valid for at least ``min_ttl`` seconds.

        Scope: only credentials linked to ``agent_id`` (the calling env's agent);
        an unlinked or nonexistent id is the same 404 (no existence leak).
        ``known_expires_at`` is the expiry of the token the caller's env already
        holds (from its credentials.json). Whenever the returned token's expiry
        differs from it — or it is not given — the payload is pushed to the
        agent's running environments before returning, so the env's output
        redaction knows the returned value even when another path (sweep, a
        sibling agent, another worker) refreshed it in the database.

        The token itself comes from ``issue_access_token``.

        Raises:
            AgentEnvTokenError: ``credential_not_linked`` (404), plus every
                error of ``issue_access_token``.
        """
        from app.services.credentials.credentials_service import CredentialsService

        link = session.exec(
            select(AgentCredentialLink).where(
                AgentCredentialLink.agent_id == agent_id,
                AgentCredentialLink.credential_id == credential_id,
            )
        ).first()
        credential = session.get(Credential, credential_id) if link else None
        if credential is None:
            raise AgentEnvTokenError(
                "credential_not_linked", 404,
                f"Credential {credential_id} is not linked to this agent.",
            )

        resp = await OAuthRefreshService.issue_access_token(
            session, credential, min_ttl=min_ttl
        )

        if resp.refreshed or known_expires_at != resp.expires_at:
            try:
                await CredentialsService.sync_credentials_to_agent_environments(
                    session=session, agent_id=agent_id
                )
            except Exception as e:
                logger.error(
                    "Pushing refreshed credential %s to agent %s environments failed: %s",
                    credential_id, agent_id, type(e).__name__,
                )

        return resp

    @staticmethod
    def _raise_for_failure(outcome: RefreshOutcome, data: dict[str, Any], now: float) -> None:
        if outcome.error_kind == REFRESH_ERROR_NOT_CONFIGURED:
            raise AgentEnvTokenError(
                "oauth_not_configured", 503,
                "Google OAuth is not configured on this platform, so tokens cannot be "
                "refreshed. Ask an administrator.",
            )
        if outcome.error_kind == REFRESH_ERROR_REAUTH_REQUIRED:
            raise AgentEnvTokenError(
                "reauthorization_required", 409,
                f"Google no longer accepts the authorization for credential "
                f"{outcome.credential.id}. Ask the user to re-authorize it in the "
                f"credential settings.",
            )
        raise AgentEnvTokenError(
            "provider_error", 502,
            "Google could not refresh the access token right now; try again shortly.",
            retry_after=_cooldown_left(data, now),
        )

    @staticmethod
    def _changed_since(session: Session, credential: Credential, snapshot: str | None) -> bool:
        """Re-read the credential; True (and log) if its blob changed since ``snapshot``."""
        session.refresh(credential)
        if credential.encrypted_data == snapshot:
            return False
        logger.info(
            "Credential %s changed during its OAuth refresh (re-authorized?); "
            "discarding the refresh result",
            credential.id,
        )
        return True

    @staticmethod
    def _store(session: Session, credential: Credential, data: dict[str, Any]) -> None:
        """Encrypt ``data`` onto the credential; ``locked`` commits it on exit
        (that commit also releases the advisory xact lock)."""
        credential.encrypted_data = security.encrypt_field(json.dumps(data))
        session.add(credential)


class AgentEnvTokenError(Exception):
    """An agent-env access-token request cannot be served.

    ``code`` is the stable machine code the container SDK maps to
    ``CredentialRefreshError``; ``message`` is written for a person and never
    contains token values. ``retry_after`` (seconds) is set for transient
    provider failures.
    """

    def __init__(
        self, code: str, status: int, message: str, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.retry_after = retry_after


def _cooldown_left(data: dict[str, Any], now: float) -> int:
    attempted_at = data.get("refresh_attempted_at")
    if not isinstance(attempted_at, (int, float)):
        return 1
    return max(1, int(settings.OAUTH_REFRESH_COOLDOWN_SECONDS - (now - attempted_at)))


OAuthStatus = Literal["pending_authorization", "ready", "reauth_required"]


def oauth_status(data: dict[str, Any] | None) -> OAuthStatus:
    """Authorization state of a Google OAuth credential, from its decrypted blob.

    Pure function of the blob (no clock), so it only changes when the blob
    does, which bumps the credential revision: ``pending_authorization`` until
    a token is stored, ``reauth_required`` once Google rejected the refresh
    token, else ``ready`` (an expired token is still ``ready``: it refreshes).
    """
    data = data or {}
    expires_at = data.get("expires_at")
    has_expiry = isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool)
    if not data.get("access_token") or not has_expiry:
        return "pending_authorization"
    if data.get("refresh_error_kind") == REFRESH_ERROR_REAUTH_REQUIRED:
        return "reauth_required"
    return "ready"
