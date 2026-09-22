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
listed or delivered. Only ``LOCALLY_COMPATIBLE_TYPES`` — types that work on a
device out of the box — are listed or delivered at all.
"""

import hashlib
import json
import logging
import uuid

from sqlmodel import Session, select

from app.models.credentials.credential import Credential, CredentialType
from app.models.credentials.credential_share import CredentialShare
from app.models.credentials.desktop_credential import (
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
from app.services.common.rate_limiter import RateLimiter
from app.services.credentials.credentials_service import CredentialsService
from app.services.desktop_auth.desktop_auth_service import DesktopAuthService
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)

MATERIALIZE_LIMIT_PER_MINUTE = 60

# Credential types that work on a Desktop device out of the box: the delivered
# entry (plus, for a service account, its full key JSON) is all a local agent
# needs. Everything else is never listed nor delivered:
# - OAuth (gmail/gdrive/gcalendar, incl. readonly): refreshing the access token
#   needs the OAuth client secret, which is private to the backend server.
# - ssh_key: the private key never leaves Core, so the entry is useless locally.
# - agent_api: bound to the platform's anonymous connection model.
# - mcp_provider: an SDK MCP-server manifest entry, not a credential file.
LOCALLY_COMPATIBLE_TYPES: frozenset[CredentialType] = frozenset(
    {
        CredentialType.EMAIL_IMAP,
        CredentialType.EMAIL_SMTP,
        CredentialType.ODOO,
        CredentialType.API_TOKEN,
        CredentialType.GOOGLE_SERVICE_ACCOUNT,
    }
)

_limiter = RateLimiter()


class DesktopCredentialError(Exception):
    """Request-level failure; the route maps it to an HTTP response."""

    def __init__(
        self, status_code: int, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.headers = headers


class DesktopCredentialService:
    @staticmethod
    def revision(credential: Credential) -> str:
        """Opaque per-credential revision; ``updated_at`` is bumped by DB triggers
        on every credential write and every share insert/update/delete."""
        return hashlib.sha256(str(credential.updated_at).encode()).hexdigest()[:32]

    @staticmethod
    def local_access_refusal(
        credential: Credential | None,
        user_id: uuid.UUID,
        shared_sources: dict[uuid.UUID, str | None],
        categories: dict[uuid.UUID, str],
    ) -> DesktopCredentialRefusalReason | None:
        """Why ``user_id`` may not copy ``credential`` to a device, or None.

        Owners may, for their "mine" credentials. A recipient needs a live share
        *and* the owner's ``allow_sharing``, a share that makes it "mine" (not a
        bundle or skill install), plus the owner's local-use consent. Superuser
        status grants nothing here. ``categories`` comes from ``_categories``.
        """
        if credential is None:
            return "not_found"
        is_owned = credential.owner_id == user_id
        if not is_owned and (
            credential.id not in shared_sources or not credential.allow_sharing
        ):
            return "no_access"
        if categories.get(credential.id) != "mine":
            return "not_local_category"
        if not is_owned and not credential.allow_local_use:
            return "local_use_not_allowed"
        return None

    @staticmethod
    def _shared_sources(
        session: Session, user_id: uuid.UUID
    ) -> dict[uuid.UUID, str | None]:
        """``{credential_id: share.source}`` for every share held by ``user_id``."""
        return dict(
            session.exec(
                select(CredentialShare.credential_id, CredentialShare.source).where(
                    CredentialShare.shared_with_user_id == user_id
                )
            ).all()
        )

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
        session: Session, user: User, if_none_match: str | None
    ) -> tuple[str, DesktopCredentialList | None]:
        """Return ``(etag, list)``; ``list`` is None when ``if_none_match`` matches.

        The ETag covers every metadata field plus the revision. ``status`` and
        ``expires_at`` derive from the encrypted payload, whose every change
        bumps the revision, so the common unchanged poll costs no decryption.
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
        # A shared entry without local-use consent is still listed
        # (local_use_allowed=false) so Desktop can explain it; every other
        # refusal (including automatic/bundle credentials) omits the entry.
        visible = [
            c
            for c in rows
            if DesktopCredentialService.local_access_refusal(
                c, user.id, shared, categories
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
            {
                "id": str(c.id),
                "name": c.name,
                "type": c.type.value,
                "notes": c.notes,
                "service_uri": c.service_uri,
                "is_placeholder": bool(c.is_placeholder),
                "relation": "owned" if c.owner_id == user.id else "shared",
                "category": categories[c.id],
                "owner_email": owner_emails.get(c.owner_id),
                "local_use_allowed": c.owner_id == user.id or c.allow_local_use,
                "revision": DesktopCredentialService.revision(c),
                "user_workspace_id": str(c.user_workspace_id)
                if c.user_workspace_id
                else None,
            }
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
            items.append(
                DesktopCredentialListItem(
                    **meta,
                    status=CredentialsService.check_credential_completeness(
                        c.type.value, data
                    ),
                    expires_at=data.get("expires_at"),
                )
            )
        return etag, DesktopCredentialList(items=items)

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
    def _check_rate_limit(user_id: uuid.UUID, client_id: str) -> None:
        # Per-process sliding window, not a distributed quota.
        retry = _limiter.check(f"{user_id}:{client_id}", MATERIALIZE_LIMIT_PER_MINUTE)
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
        selected: list[dict] = []
        revisions: dict[str, str] = {}
        owners: dict[str, uuid.UUID] = {}
        for cid in requested:
            credential = loaded.get(cid)
            # Access is checked before type, so a caller without access learns
            # nothing about the credential.
            reason = DesktopCredentialService.local_access_refusal(
                credential, user.id, shared, categories
            )
            if reason is None and credential.type not in LOCALLY_COMPATIBLE_TYPES:
                reason = "unsupported_type"
            if reason:
                refused.append(DesktopCredentialRefusal(id=cid, reason=reason))
                continue
            selected.append(
                CredentialsService.credential_to_env_dict(
                    credential,
                    CredentialsService.decrypt_credential_data(session, credential),
                )
            )
            revisions[str(cid)] = DesktopCredentialService.revision(credential)
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
