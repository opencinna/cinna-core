"""Account-scoped, audited delivery of credentials to authenticated Desktop devices.

Two operations back ``/external/credentials``:

- ``list_credentials`` — metadata of everything the caller may attach locally
  (owned, or shared with them under a live share). Secrets are decrypted only
  when the caller's cached ETag no longer matches.
- ``materialize`` — the delivered values for an explicit selection, shaped by
  the same env builder the cloud sync uses, so type transformations and the
  ``AGENT_ENV_ALLOWED_FIELDS`` whitelist stay shared.

Authorization (``local_access_refusal``) is one rule, applied in a single
pass. Only the caller's "mine" credentials qualify: "automatic" and "bundle"
ones (per ``CredentialsService.classify_credential_category``) are never
listed or delivered — except a share stamped ``skill_local_install``, which
``provision-local`` creates precisely so a Desktop can copy it. Only ``LOCALLY_COMPATIBLE_TYPES`` — types that work on a
device out of the box — are listed or delivered at all.

Google OAuth types are core-brokered: the refresh token never leaves Core.
Materialize delivers a fresh access token (refreshing first when needed) and
refuses ``pending_authorization`` / ``reauthorization_required`` /
``token_unavailable``; the list carries ``oauth_status`` and
``granted_user_email``. Desktop gets later tokens from
``DesktopOAuthService.access_token`` (``/external/credentials/{id}/access-token``).

Issued ``agent_api`` keys (plan §6/§7) are opt-in: only with
``include=agent_api_keys``, only the caller's own key issued to the caller, and
only delivered while active (other statuses are listed, and refused on
materialize with ``key_revoked`` / ``key_expired`` / ``key_paused``).
"""

import hashlib
import json
import logging
import uuid
from datetime import UTC

from sqlmodel import Session, select

from app.models.credentials.credential import Credential, CredentialType
from app.models.credentials.credential_share import (
    SHARE_SOURCE_SKILL_LOCAL_INSTALL,
    CredentialShare,
)
from app.models.credentials.desktop_credential import (
    DesktopAgentApiKeyFacts,
    DesktopCredentialList,
    DesktopCredentialListItem,
    DesktopCredentialMaterializeRequest,
    DesktopCredentialMaterializeResponse,
    DesktopCredentialRefusal,
    DesktopCredentialRefusalReason,
    DesktopMaterializedCredential,
)
from app.models.desktop_auth.desktop_oauth_client import DesktopOAuthClient
from app.models.events.security_event import (
    CREDENTIAL_MATERIALIZED_LOCAL,
    SecurityEventCreate,
)
from app.models.users.user import User
from app.services.agent_api.agent_api_key_status import (
    KeyState,
    key_states_by_credential,
    unavailable_reason_for,
)
from app.services.agent_api.agent_api_token_service import AgentApiTokenService
from app.services.common.rate_limiter import RateLimiter
from app.services.credentials.credentials_service import CredentialsService
from app.services.credentials.oauth_refresh_service import (
    GOOGLE_OAUTH_TYPES,
    AgentEnvTokenError,
    OAuthRefreshService,
    oauth_status,
)
from app.services.desktop_auth.desktop_auth_service import DesktopAuthService
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)

MATERIALIZE_LIMIT_PER_MINUTE = 60
# Minimum lifetime of the access token a materialize delivers for a Google
# OAuth credential (the Desktop asks /access-token for later ones).
MATERIALIZE_OAUTH_MIN_TTL_SECONDS = 600

# Credential types that work on a Desktop device: the delivered entry (plus,
# for a service account, its full key JSON) is all a local agent needs.
# - Google OAuth (gmail/gdrive/gcalendar/gmeet, incl. readonly): core-brokered.
#   The entry carries a short-lived access token only; the refresh token and
#   the OAuth client secret stay in Core, and the Desktop gets later tokens
#   from the access-token endpoint.
# Everything else is never listed nor delivered:
# - ssh_key: the private key never leaves Core, so the entry is useless locally.
# - agent_api: only an issued key owned by and issued to the caller, and only
#   when the client opts in (include=agent_api_keys); connections never.
# - mcp_provider: an SDK MCP-server manifest entry, not a credential file.
LOCALLY_COMPATIBLE_TYPES: frozenset[CredentialType] = frozenset(
    {
        CredentialType.EMAIL_IMAP,
        CredentialType.EMAIL_SMTP,
        CredentialType.ODOO,
        CredentialType.API_TOKEN,
        CredentialType.GOOGLE_SERVICE_ACCOUNT,
        CredentialType.AGENT_API,
        CredentialType.GMAIL_OAUTH,
        CredentialType.GMAIL_OAUTH_READONLY,
        CredentialType.GDRIVE_OAUTH,
        CredentialType.GDRIVE_OAUTH_READONLY,
        CredentialType.GCALENDAR_OAUTH,
        CredentialType.GCALENDAR_OAUTH_READONLY,
        CredentialType.GMEET_OAUTH,
        CredentialType.GMEET_OAUTH_READONLY,
    }
)

_limiter = RateLimiter()


class DesktopCredentialError(Exception):
    """Request-level failure; the route maps it to an HTTP response.

    ``code`` (optional) is a stable machine code; with it the route answers
    ``detail={"code", "message"}``, without it the plain message string.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        headers: dict[str, str] | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.headers = headers
        self.code = code


class DesktopCredentialService:
    @staticmethod
    def revision(credential: Credential, key_state: KeyState | None = None) -> str:
        """Opaque per-credential revision.

        Non-keys: ``updated_at`` (bumped by DB triggers on every credential
        write and every share insert/update/delete). Issued keys (D25): also the
        read-time ``key_status``, the expiry and the public ``base_url``, so
        revoke, pause, producer/issuer removal, expiry and an origin change all
        move it without a write.
        """
        if key_state is None:
            return hashlib.sha256(str(credential.updated_at).encode()).hexdigest()[:32]
        facts = DesktopCredentialService.key_facts(key_state)
        raw = (
            f"{credential.updated_at}|{key_state.status.value}|"
            f"{facts.expires_at}|{facts.base_url}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    @staticmethod
    def key_facts(key_state: KeyState) -> DesktopAgentApiKeyFacts:
        """The non-secret facts of an issued key, URLs on the public origin."""
        token = key_state.token
        agent = key_state.agent if token.agent_id is not None else None
        expires_at = token.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return DesktopAgentApiKeyFacts(
            producer_agent_id=agent.id if agent is not None else None,
            producer_name=agent.name if agent is not None else None,
            issuer_email=key_state.issuer_email if token.owner_id else None,
            base_url=(
                AgentApiTokenService.build_base_url(agent.id) if agent else None
            ),
            spec_url=(
                AgentApiTokenService.build_spec_url(agent.id) if agent else None
            ),
            expires_at=(
                expires_at.astimezone(UTC).isoformat() if expires_at else None
            ),
        )

    @staticmethod
    def local_access_refusal(
        credential: Credential | None,
        user_id: uuid.UUID,
        shared_sources: dict[uuid.UUID, str | None],
        categories: dict[uuid.UUID, str],
        include_agent_api_keys: bool = False,
        key_states: dict[uuid.UUID, KeyState] | None = None,
    ) -> DesktopCredentialRefusalReason | None:
        """Why ``user_id`` may not copy ``credential`` to a device, or None.

        Owners may, for their "mine" credentials. A recipient needs a live share
        *and* the owner's ``allow_sharing``, a share that makes it "mine" (not a
        bundle or cloud skill install; a ``skill_local_install`` share counts),
        plus the owner's local-use consent. Superuser
        status grants nothing here. ``categories`` comes from ``_categories``.

        ``agent_api``: only when ``include_agent_api_keys`` and the credential is
        the caller's own issued key, issued to the caller (``key_states`` from
        ``key_states_by_credential``); every other ``agent_api`` credential,
        connections included, is ``unsupported_type``. Checked after access so
        a caller without access learns nothing. Status is NOT checked here
        (keys are listed whatever their status; materialize gates it).
        """
        if credential is None:
            return "not_found"
        is_owned = credential.owner_id == user_id
        if not is_owned and (
            credential.id not in shared_sources or not credential.allow_sharing
        ):
            return "no_access"
        if credential.type == CredentialType.AGENT_API:
            state = (key_states or {}).get(credential.id)
            if (
                not include_agent_api_keys
                or not is_owned
                or state is None
                or state.token.subject_user_id != user_id
            ):
                return "unsupported_type"
        if categories.get(credential.id) != "mine" and not (
            not is_owned
            and shared_sources.get(credential.id) == SHARE_SOURCE_SKILL_LOCAL_INSTALL
        ):
            return "not_local_category"
        if not is_owned and not credential.allow_local_use:
            return "local_use_not_allowed"
        return None

    @staticmethod
    def _key_states(
        session: Session, credentials: list[Credential], include_agent_api_keys: bool
    ) -> dict[uuid.UUID, KeyState]:
        """Issued-key states for the ``agent_api`` rows (one query), or {}."""
        if not include_agent_api_keys:
            return {}
        return key_states_by_credential(
            session,
            [c.id for c in credentials if c.type == CredentialType.AGENT_API],
        )

    @staticmethod
    def _shared_sources(
        session: Session, user_id: uuid.UUID
    ) -> dict[uuid.UUID, str | None]:
        """``{credential_id: share.source}`` for every share held by ``user_id``.

        A credential held only through a user group maps to ``None`` (a direct,
        "mine" share for local copy, D13); a direct row's source wins.
        """
        from app.services.credentials.credential_share_service import (
            CredentialShareService,
        )

        sources: dict[uuid.UUID, str | None] = {
            credential_id: None
            for credential_id in session.exec(
                CredentialShareService.group_shared_credential_ids_subquery(user_id)
            ).all()
        }
        sources.update(
            session.exec(
                select(CredentialShare.credential_id, CredentialShare.source).where(
                    CredentialShare.shared_with_user_id == user_id
                )
            ).all()
        )
        return sources

    @staticmethod
    def _categories(
        session: Session,
        user_id: uuid.UUID,
        credentials: list[Credential],
        shared_sources: dict[uuid.UUID, str | None],
    ) -> dict[uuid.UUID, str]:
        """The caller's category for each credential, batched: owned rows via
        ``classify_owned_credentials``, shared rows from the share's source.
        Rows that are neither get no entry (and are refused as no_access)."""
        categories = CredentialsService.classify_owned_credentials(
            session, [c for c in credentials if c.owner_id == user_id]
        )
        for c in credentials:
            if c.owner_id != user_id and c.id in shared_sources:
                categories[c.id] = CredentialsService.classify_credential_category(
                    is_owned=False,
                    credential_type=c.type,
                    share_source=shared_sources[c.id],
                )
        return categories

    @staticmethod
    def _load(
        session: Session, credential_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, Credential]:
        if not credential_ids:
            return {}
        return {
            c.id: c
            for c in session.exec(
                select(Credential).where(Credential.id.in_(credential_ids))
            ).all()
        }

    # ------------------------------------------------------------------ #
    # List                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def list_credentials(
        session: Session,
        user: User,
        if_none_match: str | None,
        include_agent_api_keys: bool = False,
    ) -> tuple[str, DesktopCredentialList | None]:
        """Return ``(etag, list)``; ``list`` is None when ``if_none_match`` matches.

        The ETag covers every metadata field plus the revision. ``status``,
        ``expires_at`` and, for Google OAuth types, ``oauth_status`` and
        ``granted_user_email`` derive from the encrypted payload, whose every
        change bumps the revision, so the common unchanged poll costs no
        decryption.
        Issued keys (opt-in) put ``key_status``, ``expires_at`` and their
        ``agent_api`` facts in the metadata, so status flips, producer renames
        and issuer email changes move the ETag too.
        """
        shared = DesktopCredentialService._shared_sources(session, user.id)
        rows = session.exec(
            select(Credential)
            .where(
                (Credential.owner_id == user.id) | Credential.id.in_(list(shared)),
                Credential.type.in_(LOCALLY_COMPATIBLE_TYPES),
            )
            .order_by(Credential.id)
        ).all()
        categories = DesktopCredentialService._categories(
            session, user.id, list(rows), shared
        )
        key_states = DesktopCredentialService._key_states(
            session, list(rows), include_agent_api_keys
        )
        # A shared entry without local-use consent is still listed
        # (local_use_allowed=false) so Desktop can explain it; every other
        # refusal (including automatic/bundle credentials) omits the entry.
        visible = [
            c
            for c in rows
            if DesktopCredentialService.local_access_refusal(
                c,
                user.id,
                shared,
                categories,
                include_agent_api_keys=include_agent_api_keys,
                key_states=key_states,
            )
            in (None, "local_use_not_allowed")
        ]
        owner_ids = {c.owner_id for c in visible}
        owner_emails = (
            dict(
                session.exec(
                    select(User.id, User.email).where(User.id.in_(owner_ids))
                ).all()
            )
            if owner_ids
            else {}
        )

        metadata = [
            DesktopCredentialService._list_metadata(
                c, user.id, categories, owner_emails, key_states.get(c.id)
            )
            for c in visible
        ]
        etag = (
            '"'
            + hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
            + '"'
        )
        if if_none_match == etag:
            return etag, None

        items = []
        for c, meta in zip(visible, metadata, strict=True):
            data = CredentialsService.decrypt_credential_data(session, c)
            if c.id in key_states:
                # Keys: expiry is the token's (already in meta); status is
                # "complete" — availability is key_status, not the payload.
                items.append(DesktopCredentialListItem(**meta, status="complete"))
                continue
            is_oauth = c.type.value in GOOGLE_OAUTH_TYPES
            items.append(
                DesktopCredentialListItem(
                    **meta,
                    status=CredentialsService.check_credential_completeness(
                        c.type.value, data
                    ),
                    expires_at=data.get("expires_at"),
                    oauth_status=oauth_status(data) if is_oauth else None,
                    # A recipient without local-use consent sees no Google account.
                    granted_user_email=(
                        data.get("granted_user_email")
                        if is_oauth and meta["local_use_allowed"]
                        else None
                    ),
                )
            )
        return etag, DesktopCredentialList(items=items)

    @staticmethod
    def _list_metadata(
        credential: Credential,
        user_id: uuid.UUID,
        categories: dict[uuid.UUID, str],
        owner_emails: dict[uuid.UUID, str],
        key_state: KeyState | None,
    ) -> dict:
        """The ETag-covered metadata of one listed credential."""
        meta = {
            "id": str(credential.id),
            "name": credential.name,
            "type": credential.type.value,
            "notes": credential.notes,
            "service_uri": credential.service_uri,
            "is_placeholder": bool(credential.is_placeholder),
            "relation": "owned" if credential.owner_id == user_id else "shared",
            "category": categories[credential.id],
            "owner_email": owner_emails.get(credential.owner_id),
            "local_use_allowed": credential.owner_id == user_id
            or credential.allow_local_use,
            "revision": DesktopCredentialService.revision(credential, key_state),
            "user_workspace_id": str(credential.user_workspace_id)
            if credential.user_workspace_id
            else None,
        }
        if key_state is not None:
            facts = DesktopCredentialService.key_facts(key_state)
            meta["key_status"] = key_state.status.value
            meta["expires_at"] = facts.expires_at
            meta["agent_api"] = facts.model_dump(mode="json")
        return meta

    # ------------------------------------------------------------------ #
    # Materialize                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _require_live_client(session: Session, client_id: str) -> None:
        """401 unless the Desktop client still exists, is not revoked, and was
        not bought with a CLI account token (shared predicate with the
        credential-minting gate in ``api/deps.py``)."""
        try:
            client_uuid = uuid.UUID(str(client_id))
        except (TypeError, ValueError):
            raise DesktopCredentialError(401, "Desktop authentication revoked") from None
        client = session.get(DesktopOAuthClient, client_uuid)
        if (
            client is None
            or client.is_revoked
            or DesktopAuthService.is_cli_exchanged_session(session, client_id)
        ):
            raise DesktopCredentialError(401, "Desktop authentication revoked")

    @staticmethod
    def _check_rate_limit(
        user_id: uuid.UUID,
        client_id: str,
        *,
        scope: str | None = None,
        limit_per_minute: int = MATERIALIZE_LIMIT_PER_MINUTE,
    ) -> None:
        """429 past ``limit_per_minute`` for ``(scope, user, client)``; no
        ``scope`` is the materialize bucket."""
        # Per-process sliding window, not a distributed quota.
        key = f"{user_id}:{client_id}"
        if scope:
            key = f"{scope}:{key}"
        retry = _limiter.check(key, limit_per_minute)
        if retry:
            raise DesktopCredentialError(
                429,
                "Too many credential requests",
                headers={"Retry-After": str(int(retry) + 1)},
            )

    @staticmethod
    async def materialize(
        session: Session,
        user: User,
        client_id: str,
        request: DesktopCredentialMaterializeRequest,
        include_agent_api_keys: bool = False,
    ) -> DesktopCredentialMaterializeResponse:
        """Deliver the selected credentials to the caller's Desktop client.

        Refusals are per item. Request-level failures (revoked device, rate
        limit) raise ``DesktopCredentialError``. The caller must already have
        established that this is an interactive (non-CLI-exchanged) Desktop JWT.
        """
        DesktopCredentialService._require_live_client(session, client_id)
        DesktopCredentialService._check_rate_limit(user.id, client_id)

        refused: list[DesktopCredentialRefusal] = []
        requested = list(dict.fromkeys(request.credential_ids))
        shared = DesktopCredentialService._shared_sources(session, user.id)
        loaded = DesktopCredentialService._load(session, requested)
        categories = DesktopCredentialService._categories(
            session, user.id, list(loaded.values()), shared
        )
        key_states = DesktopCredentialService._key_states(
            session, list(loaded.values()), include_agent_api_keys
        )
        selected: list[dict] = []
        revisions: dict[str, str] = {}
        owners: dict[str, uuid.UUID] = {}
        for cid in requested:
            credential = loaded.get(cid)
            # Access is checked before type, so a caller without access learns
            # nothing about the credential.
            reason = DesktopCredentialService.local_access_refusal(
                credential,
                user.id,
                shared,
                categories,
                include_agent_api_keys=include_agent_api_keys,
                key_states=key_states,
            )
            if reason is None and credential.type not in LOCALLY_COMPATIBLE_TYPES:
                reason = "unsupported_type"
            if reason is None and credential.type.value in GOOGLE_OAUTH_TYPES:
                # May refresh (and commit) — nothing in this loop writes before it.
                reason = await DesktopCredentialService._ensure_oauth_token(
                    session, credential
                )
            key_state = key_states.get(cid) if reason is None else None
            if key_state is not None:
                # Status gate: only an active key is delivered.
                reason = unavailable_reason_for(key_state.status)
            if reason:
                refused.append(
                    DesktopCredentialRefusal(
                        id=cid,
                        reason=reason,
                        key_status=(
                            key_state.status.value if key_state is not None else None
                        ),
                    )
                )
                continue
            selected.append(
                CredentialsService.credential_to_env_dict(
                    credential,
                    CredentialsService.decrypt_credential_data(session, credential),
                )
            )
            revisions[str(cid)] = DesktopCredentialService.revision(
                credential, key_state
            )
            owners[str(cid)] = credential.owner_id

        bundle = CredentialsService.prepare_credentials_for_environment(
            session, None, selected_credentials=selected, desktop_owner=user
        )
        files = {
            f["credential_id"]: f["json_content"]
            for f in bundle["service_account_files"]
        }
        entries = {
            e["id"]: e for e in bundle["credentials_json"] if e["id"] in revisions
        }
        # Defensive: refuse anything the builder dropped rather than omit it.
        for entry in selected:
            if entry["id"] not in entries:
                refused.append(
                    DesktopCredentialRefusal(id=entry["id"], reason="unsupported_type")
                )
        # Delivered entries are active keys only: a key whose status changed
        # between the gate above and the builder is refused, not delivered.
        for cid in [c for c, e in entries.items() if e.get("unavailable_reason")]:
            entry = entries.pop(cid)
            refused.append(
                DesktopCredentialRefusal(
                    id=cid,
                    reason=entry["unavailable_reason"],
                    key_status=entry.get("key_status"),
                )
            )

        items = []
        for cid, entry in entries.items():
            items.append(
                DesktopMaterializedCredential(
                    id=cid,
                    revision=revisions[cid],
                    entry=entry,
                    service_account_file=files.get(cid),
                )
            )
            await DesktopCredentialService._audit_delivery(
                session, user, client_id, cid, owners[cid]
            )

        def block(block_type: str) -> dict | None:
            return next(
                (e for e in bundle["credentials_json"] if e["type"] == block_type),
                None,
            )

        return DesktopCredentialMaterializeResponse(
            items=items,
            refused=refused,
            current_user=block("current_user") if request.include_current_user else None,
        )

    @staticmethod
    async def _ensure_oauth_token(
        session: Session, credential: Credential
    ) -> DesktopCredentialRefusalReason | None:
        """Make sure a Google OAuth credential holds an access token valid for
        ``MATERIALIZE_OAUTH_MIN_TTL_SECONDS``; the refusal reason, or None.

        Refreshes through ``OAuthRefreshService.issue_access_token`` (the one
        outcome-to-error mapping). On success the credential is re-read, so the
        caller's decrypt and revision see what a refresh just stored.
        """
        status = oauth_status(
            CredentialsService.decrypt_credential_data(session, credential)
        )
        if status == "pending_authorization":
            return "pending_authorization"
        if status == "reauth_required":
            return "reauthorization_required"
        try:
            await OAuthRefreshService.issue_access_token(
                session, credential, min_ttl=MATERIALIZE_OAUTH_MIN_TTL_SECONDS
            )
        except AgentEnvTokenError as e:
            if e.code == "reauthorization_required":
                return "reauthorization_required"
            if e.code == "not_refreshable":
                return "pending_authorization"
            return "token_unavailable"
        session.refresh(credential)
        return None

    @staticmethod
    async def _audit_delivery(
        session: Session,
        user: User,
        client_id: str,
        credential_id: str,
        owner_id: uuid.UUID,
    ) -> None:
        """Audit one delivered credential for the requester and, when a recipient
        copied a shared credential, for its owner too. Never records values."""
        await SecurityEventService.create_event(
            session=session,
            user_id=user.id,
            data=SecurityEventCreate(
                event_type=CREDENTIAL_MATERIALIZED_LOCAL,
                severity="high",
                details={"credential_id": credential_id, "desktop_client_id": client_id},
            ),
        )
        if owner_id != user.id:
            await SecurityEventService.create_event(
                session=session,
                user_id=owner_id,
                data=SecurityEventCreate(
                    event_type=CREDENTIAL_MATERIALIZED_LOCAL,
                    severity="high",
                    details={
                        "credential_id": credential_id,
                        "recipient_user_id": str(user.id),
                        "recipient_email": user.email,
                    },
                ),
            )
