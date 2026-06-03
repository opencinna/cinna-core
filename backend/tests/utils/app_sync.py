"""Utility helpers for App Sync API tests.

All helpers call only the API — no direct DB access. They encapsulate the
HTTP call + status assertion and return parsed JSON so test scenarios can
focus on the assertions that matter.
"""
from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.core.config import settings

_BASE = f"{settings.API_V1_STR}/app-sync"

# ---------------------------------------------------------------------------
# Ciphertext / fingerprint factories
# ---------------------------------------------------------------------------


def make_ciphertext(content: str = "hello-world") -> str:
    """Return a realistic opaque base64 ciphertext string (simulates AEAD output)."""
    # Mimic the §12.3 envelope format: valid JSON wrapped in base64 so it
    # passes the "non-empty string" check; the server never parses it.
    envelope = f'{{"v":1,"alg":"xchacha20poly1305","umk":1,"n":"AAAA","ct":"{base64.b64encode(content.encode()).decode()}"}}'
    return base64.b64encode(envelope.encode()).decode()


def make_fingerprint(content: str = "hello-world") -> str:
    """Return a stable opaque fingerprint string (simulates client HMAC)."""
    import hashlib

    return base64.b64encode(
        hashlib.sha256(content.encode()).digest()
    ).decode()


def make_record_upsert(
    *,
    collection: str = "note",
    client_entity_id: str | None = None,
    content: str = "test-content",
    deleted: bool = False,
    client_updated_at: datetime | None = None,
    enc_umk_version: int = 1,
) -> dict:
    """Build a SyncRecordUpsert payload dict ready for JSON serialisation."""
    if client_entity_id is None:
        client_entity_id = str(uuid.uuid4())
    if client_updated_at is None:
        client_updated_at = datetime.now(UTC)

    record: dict = {
        "collection": collection,
        "client_entity_id": client_entity_id,
        "deleted": deleted,
        "client_updated_at": client_updated_at.isoformat(),
        "enc_umk_version": enc_umk_version,
    }
    if not deleted:
        record["payload_ciphertext"] = make_ciphertext(content)
        record["content_fingerprint"] = make_fingerprint(content)
    return record


# ---------------------------------------------------------------------------
# Encryption / init helpers
# ---------------------------------------------------------------------------


def make_device_input(label: str = "Test Device") -> dict:
    """Build a DeviceInput payload."""
    return {
        "device_label": label,
        "public_key": base64.b64encode(b"x" * 32).decode(),  # fake X25519 pubkey
    }


def make_envelope_input(
    wrap_method: str = "recovery",
    umk_version: int = 1,
    device_id: str | None = None,
) -> dict:
    """Build a KeyEnvelopeInput payload."""
    env: dict = {
        "wrap_method": wrap_method,
        "umk_version": umk_version,
        "wrapped_key": base64.b64encode(b"wrapped-umk-bytes-here").decode(),
    }
    if wrap_method == "recovery":
        env["kdf"] = "hkdf"
    elif wrap_method == "passphrase":
        env["kdf"] = "argon2id"
        env["kdf_params"] = {"salt": base64.b64encode(b"salt" * 4).decode()}
    if device_id is not None:
        env["device_id"] = device_id
    return env


def init_encryption(
    client: TestClient,
    headers: dict[str, str],
    *,
    device_label: str = "Test Device",
    extra_envelopes: list[dict] | None = None,
    expect_status: int = 200,
) -> dict:
    """POST /app-sync/encryption/init and return the parsed response."""
    device = make_device_input(device_label)
    envelopes = [
        make_envelope_input("device"),  # device_id will be auto-resolved
        make_envelope_input("recovery"),
    ]
    if extra_envelopes:
        envelopes.extend(extra_envelopes)

    r = client.post(
        f"{_BASE}/encryption/init",
        headers=headers,
        json={"device": device, "envelopes": envelopes},
    )
    assert r.status_code == expect_status, (
        f"init_encryption expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------


def sync(
    client: TestClient,
    headers: dict[str, str],
    *,
    changes: list[dict] | None = None,
    cursor: int = 0,
    collections: list[str] | None = None,
    limit: int = 500,
    expect_status: int = 200,
) -> dict:
    """POST /app-sync/ (combined push-then-pull)."""
    body: dict = {"cursor": cursor, "limit": limit}
    if changes is not None:
        body["changes"] = changes
    if collections is not None:
        body["collections"] = collections
    r = client.post(f"{_BASE}/", headers=headers, json=body)
    assert r.status_code == expect_status, (
        f"sync expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def push(
    client: TestClient,
    headers: dict[str, str],
    *,
    changes: list[dict],
    expect_status: int = 200,
) -> dict:
    """POST /app-sync/push."""
    r = client.post(f"{_BASE}/push", headers=headers, json={"changes": changes})
    assert r.status_code == expect_status, (
        f"push expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def pull(
    client: TestClient,
    headers: dict[str, str],
    *,
    cursor: int = 0,
    collections: list[str] | None = None,
    limit: int = 500,
    expect_status: int = 200,
) -> dict:
    """POST /app-sync/pull."""
    body: dict = {"cursor": cursor, "limit": limit}
    if collections is not None:
        body["collections"] = collections
    r = client.post(f"{_BASE}/pull", headers=headers, json=body)
    assert r.status_code == expect_status, (
        f"pull expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def get_state(
    client: TestClient,
    headers: dict[str, str],
    expect_status: int = 200,
) -> dict:
    """GET /app-sync/state."""
    r = client.get(f"{_BASE}/state", headers=headers)
    assert r.status_code == expect_status, (
        f"get_state expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def wipe(
    client: TestClient,
    headers: dict[str, str],
    *,
    collections: list[str] | None = None,
    expect_status: int = 200,
) -> dict:
    """DELETE /app-sync/."""
    body = {}
    if collections is not None:
        body["collections"] = collections
    r = client.request("DELETE", f"{_BASE}/", headers=headers, json=body)
    assert r.status_code == expect_status, (
        f"wipe expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def get_encryption(
    client: TestClient,
    headers: dict[str, str],
    expect_status: int = 200,
) -> dict:
    """GET /app-sync/encryption."""
    r = client.get(f"{_BASE}/encryption", headers=headers)
    assert r.status_code == expect_status, (
        f"get_encryption expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def reset_encryption(
    client: TestClient,
    headers: dict[str, str],
    expect_status: int = 200,
) -> dict:
    """DELETE /app-sync/encryption."""
    r = client.request("DELETE", f"{_BASE}/encryption", headers=headers)
    assert r.status_code == expect_status, (
        f"reset_encryption expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def list_keys(
    client: TestClient,
    headers: dict[str, str],
    *,
    umk_version: int | None = None,
    expect_status: int = 200,
) -> list:
    """GET /app-sync/keys."""
    params = {}
    if umk_version is not None:
        params["umk_version"] = umk_version
    r = client.get(f"{_BASE}/keys", headers=headers, params=params)
    assert r.status_code == expect_status, (
        f"list_keys expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def add_key(
    client: TestClient,
    headers: dict[str, str],
    *,
    envelope: dict,
    expect_status: int = 200,
) -> dict:
    """POST /app-sync/keys."""
    r = client.post(f"{_BASE}/keys", headers=headers, json=envelope)
    assert r.status_code == expect_status, (
        f"add_key expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def delete_key(
    client: TestClient,
    headers: dict[str, str],
    *,
    envelope_id: str,
    expect_status: int = 200,
) -> dict:
    """DELETE /app-sync/keys/{envelope_id}."""
    r = client.delete(f"{_BASE}/keys/{envelope_id}", headers=headers)
    assert r.status_code == expect_status, (
        f"delete_key expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def register_device(
    client: TestClient,
    headers: dict[str, str],
    *,
    label: str = "Test Device",
    expect_status: int = 200,
) -> dict:
    """POST /app-sync/devices."""
    r = client.post(
        f"{_BASE}/devices",
        headers=headers,
        json=make_device_input(label),
    )
    assert r.status_code == expect_status, (
        f"register_device expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def list_devices(
    client: TestClient,
    headers: dict[str, str],
    expect_status: int = 200,
) -> list:
    """GET /app-sync/devices."""
    r = client.get(f"{_BASE}/devices", headers=headers)
    assert r.status_code == expect_status, (
        f"list_devices expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def revoke_device(
    client: TestClient,
    headers: dict[str, str],
    *,
    device_id: str,
    expect_status: int = 200,
) -> dict:
    """DELETE /app-sync/devices/{device_id}."""
    r = client.delete(f"{_BASE}/devices/{device_id}", headers=headers)
    assert r.status_code == expect_status, (
        f"revoke_device expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def pairing_start(
    client: TestClient,
    headers: dict[str, str],
    *,
    new_device_pubkey: str | None = None,
    device_label: str = "New Device",
    expect_status: int = 200,
) -> dict:
    """POST /app-sync/pairing/start."""
    if new_device_pubkey is None:
        new_device_pubkey = base64.b64encode(b"ephemeral-pubkey" + b"x" * 16).decode()
    r = client.post(
        f"{_BASE}/pairing/start",
        headers=headers,
        json={"new_device_pubkey": new_device_pubkey, "device_label": device_label},
    )
    assert r.status_code == expect_status, (
        f"pairing_start expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def pairing_get(
    client: TestClient,
    headers: dict[str, str],
    *,
    code: str,
    expect_status: int = 200,
) -> dict:
    """GET /app-sync/pairing/{code}."""
    r = client.get(f"{_BASE}/pairing/{code}", headers=headers)
    assert r.status_code == expect_status, (
        f"pairing_get expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def pairing_complete(
    client: TestClient,
    headers: dict[str, str],
    *,
    code: str,
    sealed_umk: str | None = None,
    expect_status: int = 200,
) -> dict:
    """POST /app-sync/pairing/{code}/complete."""
    if sealed_umk is None:
        sealed_umk = base64.b64encode(b"sealed-umk-data-for-new-device").decode()
    r = client.post(
        f"{_BASE}/pairing/{code}/complete",
        headers=headers,
        json={"sealed_umk": sealed_umk},
    )
    assert r.status_code == expect_status, (
        f"pairing_complete expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json()
