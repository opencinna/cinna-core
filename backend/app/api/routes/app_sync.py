"""App Sync routes — zero-knowledge native-client data sync (§5.1, §12.5).

Prefix ``/app-sync`` (mounted under ``/api/v1``), tag ``App Sync``. All
endpoints require ``CurrentUser`` and operate strictly on the caller's own
data. The ``external_client_id`` JWT claim (desktop tokens) is used for
``last_writer_client_id`` attribution and device linking.
"""
import uuid
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentClientClaims, CurrentUser, SessionDep
from app.models import (
    AppSyncDevicePublic,
    AppSyncKeyEnvelopePublic,
    DeviceInput,
    EncryptionInitRequest,
    EncryptionStatePublic,
    KeyEnvelopeInput,
    Message,
    PairingCompleteRequest,
    PairingInboxDetail,
    PairingInboxItem,
    PairingRevealRequest,
    PairingSealerNonceRequest,
    PairingStartRequest,
    PairingStartResponse,
    PairingStatusPublic,
    PullRequest,
    PushRequest,
    SyncRequest,
    SyncResponse,
    SyncStatePublic,
    WipeRequest,
)
from app.services.app_sync.app_sync_service import AppSyncError, AppSyncService

router = APIRouter(prefix="/app-sync", tags=["App Sync"])


def _handle_service_error(e: AppSyncError) -> NoReturn:
    """Convert service exceptions to HTTP exceptions (never returns)."""
    detail = e.detail if e.detail is not None else e.message
    raise HTTPException(status_code=e.status_code, detail=detail)


# ── Sync verbs ─────────────────────────────────────────────────────────────


@router.post("/", response_model=SyncResponse)
def sync(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    claims: CurrentClientClaims,
    body: SyncRequest,
) -> SyncResponse:
    """Primary bidirectional sync: push ``changes`` then pull seq > cursor."""
    _client_kind, external_client_id = claims
    try:
        return AppSyncService.sync(
            session,
            current_user,
            cursor=body.cursor,
            changes=body.changes,
            collections=body.collections,
            limit=body.limit,
            writer_client_id=external_client_id,
        )
    except AppSyncError as e:
        _handle_service_error(e)


@router.post("/pull", response_model=SyncResponse)
def pull(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    body: PullRequest,
) -> SyncResponse:
    """Pull-only (download). Used for the fresh-login bootstrap loop."""
    try:
        return AppSyncService.sync(
            session,
            current_user,
            cursor=body.cursor,
            changes=[],
            collections=body.collections,
            limit=body.limit,
            writer_client_id=None,
        )
    except AppSyncError as e:
        _handle_service_error(e)


@router.post("/push", response_model=SyncResponse)
def push(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    claims: CurrentClientClaims,
    body: PushRequest,
) -> SyncResponse:
    """Push-only (upload). Returns ``applied`` + the new ``next_cursor``."""
    _client_kind, external_client_id = claims
    try:
        return AppSyncService.push_only(
            session,
            current_user,
            changes=body.changes,
            writer_client_id=external_client_id,
        )
    except AppSyncError as e:
        _handle_service_error(e)


@router.get("/state", response_model=SyncStatePublic)
def get_state(
    *, session: SessionDep, current_user: CurrentUser
) -> SyncStatePublic:
    """Lightweight bootstrap: cursor, quota usage, per-collection counts."""
    return AppSyncService.get_state(session, current_user)


@router.delete("/", response_model=Message)
def wipe(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    body: WipeRequest | None = None,
) -> Message:
    """Wipe the caller's sync data (optionally scoped to collections)."""
    collections = body.collections if body else None
    count = AppSyncService.wipe(session, current_user, collections=collections)
    return Message(message=f"Deleted {count} synced record(s)")


# ── Encryption / key management (§12.5) ─────────────────────────────────────


@router.get("/encryption", response_model=EncryptionStatePublic)
def get_encryption(
    *, session: SessionDep, current_user: CurrentUser
) -> EncryptionStatePublic:
    """Tell a client how it can unlock: init state, methods, devices."""
    return AppSyncService.get_encryption_state(session, current_user)


@router.post("/encryption/init", response_model=EncryptionStatePublic)
def init_encryption(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    body: EncryptionInitRequest,
) -> EncryptionStatePublic:
    """First device only: register device + initial envelopes, set v1."""
    try:
        return AppSyncService.init_encryption(session, current_user, data=body)
    except AppSyncError as e:
        _handle_service_error(e)


@router.delete("/encryption", response_model=EncryptionStatePublic)
def reset_encryption(
    *, session: SessionDep, current_user: CurrentUser
) -> EncryptionStatePublic:
    """Tear E2E back down (delete envelopes/devices, set v0) so the account can
    be set up fresh. Pairs with ``DELETE /`` (wipe records) for a full reset."""
    try:
        return AppSyncService.reset_encryption(session, current_user)
    except AppSyncError as e:
        _handle_service_error(e)


@router.get("/keys", response_model=list[AppSyncKeyEnvelopePublic])
def list_keys(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    umk_version: int | None = Query(default=None),
) -> list[AppSyncKeyEnvelopePublic]:
    """List wrapped envelopes (optionally for a UMK generation)."""
    return AppSyncService.list_envelopes(
        session, current_user, umk_version=umk_version
    )


@router.post("/keys", response_model=AppSyncKeyEnvelopePublic)
def add_key(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    body: KeyEnvelopeInput,
) -> AppSyncKeyEnvelopePublic:
    """Add or replace a wrapped UMK envelope."""
    try:
        return AppSyncService.add_envelope(session, current_user, data=body)
    except AppSyncError as e:
        _handle_service_error(e)


@router.delete("/keys/{envelope_id}", response_model=Message)
def delete_key(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    envelope_id: uuid.UUID,
) -> Message:
    """Remove a wrapped UMK envelope."""
    try:
        AppSyncService.delete_envelope(session, current_user, envelope_id=envelope_id)
    except AppSyncError as e:
        _handle_service_error(e)
    return Message(message="Key envelope deleted")


# ── Devices ──────────────────────────────────────────────────────────────


@router.post("/devices", response_model=AppSyncDevicePublic)
def register_device(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    body: DeviceInput,
) -> AppSyncDevicePublic:
    """Register a device public key."""
    try:
        return AppSyncService.register_device(session, current_user, data=body)
    except AppSyncError as e:
        _handle_service_error(e)


@router.get("/devices", response_model=list[AppSyncDevicePublic])
def list_devices(
    *, session: SessionDep, current_user: CurrentUser
) -> list[AppSyncDevicePublic]:
    """List the caller's registered devices (trusted-devices UI)."""
    return AppSyncService.list_devices(session, current_user)


@router.delete("/devices/{device_id}", response_model=Message)
def revoke_device(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    device_id: uuid.UUID,
) -> Message:
    """Revoke a device: delete its device envelopes, mark revoked."""
    try:
        AppSyncService.revoke_device(session, current_user, device_id=device_id)
    except AppSyncError as e:
        _handle_service_error(e)
    return Message(message="Device revoked")


# ── Pairing relay (§12.6) ───────────────────────────────────────────────


@router.post("/pairing/start", response_model=PairingStartResponse)
def pairing_start(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    body: PairingStartRequest,
) -> PairingStartResponse:
    """Joining device: create a short-lived pairing relay row → pairing code."""
    try:
        return AppSyncService.pairing_start(
            session,
            current_user,
            new_device_pubkey=body.new_device_pubkey,
            commitment=body.commitment,
            device_label=body.device_label,
        )
    except AppSyncError as e:
        _handle_service_error(e)


# ── Sealer-facing inbox (keyed by row id) ────────────────────────────────
# IMPORTANT: these static `/pairing/inbox...` routes MUST be declared BEFORE
# the parametrized `/pairing/{code}` routes below — FastAPI matches in
# declaration order, so otherwise "inbox" would be captured as a {code}.


@router.get("/pairing/inbox", response_model=list[PairingInboxItem])
def pairing_inbox(
    *,
    session: SessionDep,
    current_user: CurrentUser,
) -> list[PairingInboxItem]:
    """Trusted device discovers its own pending pairing requests (metadata only)."""
    try:
        return AppSyncService.pairing_inbox(session, current_user)
    except AppSyncError as e:
        _handle_service_error(e)


@router.get("/pairing/inbox/{pairing_id}", response_model=PairingInboxDetail)
def pairing_inbox_get(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    pairing_id: uuid.UUID,
) -> PairingInboxDetail:
    """Sealer reads pubkey/commitment/nonces for one of its own rows."""
    try:
        return AppSyncService.pairing_inbox_get(
            session, current_user, pairing_id=pairing_id
        )
    except AppSyncError as e:
        _handle_service_error(e)


@router.post("/pairing/inbox/{pairing_id}/sealer-nonce", response_model=Message)
def pairing_set_sealer_nonce(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    pairing_id: uuid.UUID,
    body: PairingSealerNonceRequest,
) -> Message:
    """Sealer posts its nonce (pending → sealer_nonce_set)."""
    try:
        AppSyncService.pairing_set_sealer_nonce(
            session, current_user, pairing_id=pairing_id, sealer_nonce=body.sealer_nonce
        )
    except AppSyncError as e:
        _handle_service_error(e)
    return Message(message="Sealer nonce set")


@router.post("/pairing/inbox/{pairing_id}/complete", response_model=Message)
def pairing_complete_by_id(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    pairing_id: uuid.UUID,
    body: PairingCompleteRequest,
) -> Message:
    """Sealer posts the UMK sealed to the joining device (revealed → completed)."""
    try:
        AppSyncService.pairing_complete_by_id(
            session, current_user, pairing_id=pairing_id, sealed_umk=body.sealed_umk
        )
    except AppSyncError as e:
        _handle_service_error(e)
    return Message(message="Pairing completed")


# ── Joiner-facing (keyed by the secret code) ─────────────────────────────


@router.get("/pairing/{code}", response_model=PairingStatusPublic)
def pairing_get(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    code: str,
) -> PairingStatusPublic:
    """Joining device polls for the sealer nonce, then the sealed UMK."""
    try:
        return AppSyncService.pairing_get(session, current_user, code=code)
    except AppSyncError as e:
        _handle_service_error(e)


@router.post("/pairing/{code}/reveal", response_model=Message)
def pairing_reveal(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    code: str,
    body: PairingRevealRequest,
) -> Message:
    """Joiner reveals its nonce last (sealer_nonce_set → revealed)."""
    try:
        AppSyncService.pairing_reveal(
            session, current_user, code=code, joiner_nonce=body.joiner_nonce
        )
    except AppSyncError as e:
        _handle_service_error(e)
    return Message(message="Joiner nonce revealed")
