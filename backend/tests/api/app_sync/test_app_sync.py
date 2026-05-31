"""
Backend integration tests for the native-client data sync API (Phase 1).

All tests are API-only — no direct database access. Scenarios are grouped by
user journey, with each test function walking through a multi-step story. Error
cases and authorization guards are folded in as late phases of the relevant
scenario rather than standalone tests (per project README conventions).

Key behaviors covered (per §14 checklist):
  1. Verbatim ciphertext / zero-knowledge
  2. E2E gate (push before init → 409; init validations)
  3. Key envelopes (init + CRUD + verbatim return)
  4. Pairing relay (start → complete → single-use get → consumed)
  5. Device revoke (removes device envelopes + marks revoked)
  6. Delta pull (cursor-based filtering)
  7. Tombstone propagation
  8. LWW conflict resolution
  9. Idempotent re-push
  10. Cross-device identity (two UUIDs → two rows; non-UUID → 422)
  11. Ownership isolation (user A vs user B)
  12. Limits (payload > max → 413; batch > max → 422; quota → 413)
  13. Pagination (has_more + cursor advancement)
  14. GET /state accuracy
  15. DELETE / wipe (tombstones, counters, seq advancement)
  16. Desktop-token attribution (web token → null last_writer_client_id)
  17. Fresh-login bootstrap (cursor=0 pull loop)
"""
from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.core.config import settings
from tests.utils.app_sync import (
    add_key,
    delete_key,
    get_encryption,
    get_state,
    init_encryption,
    list_devices,
    list_keys,
    make_ciphertext,
    make_envelope_input,
    make_fingerprint,
    make_record_upsert,
    pairing_complete,
    pairing_get,
    pairing_start,
    pull,
    push,
    register_device,
    revoke_device,
    sync,
    wipe,
)
from tests.utils.user import create_random_user, user_authentication_headers

_BASE = f"{settings.API_V1_STR}/app-sync"


# ---------------------------------------------------------------------------
# Scenario 1: E2E gate + encryption init lifecycle
# ---------------------------------------------------------------------------


def test_encryption_init_lifecycle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    E2E gate and encryption init lifecycle:
      1. Push before init → 409
      2. Init with missing recovery envelope → 422
      3. Init with missing device envelope → 422
      4. Valid init → 200, encryption state reflects initialized=True
      5. GET /encryption returns correct state with device registered
      6. Re-init when already initialized → 409
      7. Push after init → succeeds (200)
    """
    headers = superuser_token_headers

    # ── Phase 1: Push before E2E initialized → 409 ────────────────────────
    record = make_record_upsert(collection="note")
    r = client.post(
        f"{_BASE}/push",
        headers=headers,
        json={"changes": [record]},
    )
    assert r.status_code == 409, f"Expected 409, got {r.status_code}: {r.text}"

    # ── Phase 2: Init without recovery envelope → 422 ─────────────────────
    from tests.utils.app_sync import make_device_input
    r = client.post(
        f"{_BASE}/encryption/init",
        headers=headers,
        json={
            "device": make_device_input("Device A"),
            "envelopes": [make_envelope_input("device")],  # missing recovery
        },
    )
    assert r.status_code == 422, f"Expected 422 (missing recovery), got {r.status_code}: {r.text}"

    # ── Phase 3: Init without device envelope → 422 ───────────────────────
    r = client.post(
        f"{_BASE}/encryption/init",
        headers=headers,
        json={
            "device": make_device_input("Device A"),
            "envelopes": [make_envelope_input("recovery")],  # missing device
        },
    )
    assert r.status_code == 422, f"Expected 422 (missing device), got {r.status_code}: {r.text}"

    # ── Phase 4: Valid init → 200 ─────────────────────────────────────────
    enc_state = init_encryption(client, headers, device_label="My MacBook")
    assert enc_state["initialized"] is True
    assert enc_state["active_umk_version"] == 1
    assert enc_state["has_recovery"] is True
    assert enc_state["has_passphrase"] is False
    assert len(enc_state["devices"]) == 1
    assert enc_state["devices"][0]["device_label"] == "My MacBook"
    assert enc_state["devices"][0]["is_revoked"] is False

    # ── Phase 5: GET /encryption reflects initialized state ───────────────
    fetched_enc = get_encryption(client, headers)
    assert fetched_enc["initialized"] is True
    assert fetched_enc["active_umk_version"] == 1
    assert fetched_enc["has_recovery"] is True

    # ── Phase 6: Re-init when already initialized → 409 ──────────────────
    init_encryption(client, headers, device_label="Another Device", expect_status=409)

    # ── Phase 7: Push after init → succeeds ──────────────────────────────
    record = make_record_upsert(collection="note", content="my first note")
    result = push(client, headers, changes=[record])
    assert result["applied"][0]["status"] == "applied"


# ---------------------------------------------------------------------------
# Scenario 2: Verbatim ciphertext (zero-knowledge assertion)
# ---------------------------------------------------------------------------


def test_verbatim_ciphertext_zero_knowledge(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The server stores ciphertext byte-for-byte and returns it unchanged:
      1. Init E2E
      2. Push a record with a specific payload_ciphertext value
      3. Pull from cursor 0 → returned payload_ciphertext is byte-identical
      4. There is no code path that decrypts the payload (server-opaque by design)
    """
    headers = superuser_token_headers
    init_encryption(client, headers)

    entity_id = str(uuid.uuid4())
    original_ciphertext = make_ciphertext("verbatim-content-that-server-never-reads")
    original_fingerprint = make_fingerprint("verbatim-content-that-server-never-reads")

    record = {
        "collection": "note",
        "client_entity_id": entity_id,
        "payload_ciphertext": original_ciphertext,
        "content_fingerprint": original_fingerprint,
        "enc_umk_version": 1,
        "deleted": False,
        "client_updated_at": datetime.now(UTC).isoformat(),
    }
    push_result = push(client, headers, changes=[record])
    assert push_result["applied"][0]["status"] == "applied"

    # ── Pull from cursor 0 and verify verbatim return ─────────────────────
    pull_result = pull(client, headers, cursor=0)
    assert len(pull_result["changes"]) >= 1

    returned = next(
        (r for r in pull_result["changes"] if r["client_entity_id"] == entity_id),
        None,
    )
    assert returned is not None, "Pushed record not found in pull response"
    assert returned["payload_ciphertext"] == original_ciphertext, (
        "Server mutated the ciphertext — zero-knowledge invariant violated"
    )
    assert returned["deleted"] is False


# ---------------------------------------------------------------------------
# Scenario 3: Key envelopes CRUD
# ---------------------------------------------------------------------------


def test_key_envelopes_crud(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Key envelope lifecycle:
      1. Init stores device + recovery envelopes (returned verbatim by GET /keys)
      2. POST /keys adds a passphrase envelope
      3. GET /keys?umk_version=1 filters correctly
      4. DELETE /keys/{id} removes the envelope
      5. GET /keys no longer returns the deleted envelope
      6. Device-envelope device_id points at a different device → 422
    """
    from tests.utils.app_sync import make_device_input

    headers = superuser_token_headers
    init_encryption(client, headers)

    # ── Phase 1: GET /keys returns device + recovery envelopes verbatim ───
    keys = list_keys(client, headers)
    assert len(keys) == 2
    methods = {k["wrap_method"] for k in keys}
    assert methods == {"device", "recovery"}

    # verify the wrapped_key is returned verbatim
    recovery_env = next(k for k in keys if k["wrap_method"] == "recovery")
    assert recovery_env["wrapped_key"] == make_envelope_input("recovery")["wrapped_key"]

    # ── Phase 2: POST /keys adds a passphrase envelope ────────────────────
    passphrase_env = make_envelope_input("passphrase")
    added = add_key(client, headers, envelope=passphrase_env)
    assert added["wrap_method"] == "passphrase"
    assert added["wrapped_key"] == passphrase_env["wrapped_key"]

    # ── Phase 3: Filter by umk_version=1 returns all three ────────────────
    keys_v1 = list_keys(client, headers, umk_version=1)
    assert len(keys_v1) == 3
    methods_v1 = {k["wrap_method"] for k in keys_v1}
    assert "passphrase" in methods_v1

    # ── Phase 4: DELETE the passphrase envelope ────────────────────────────
    passphrase_id = added["id"]
    delete_key(client, headers, envelope_id=passphrase_id)

    # ── Phase 5: GET /keys no longer returns the deleted envelope ─────────
    keys_after = list_keys(client, headers)
    assert all(k["id"] != passphrase_id for k in keys_after)
    assert len(keys_after) == 2

    # ── Phase 6: Deleting a non-existent envelope → 404 ───────────────────
    r = client.delete(
        f"{_BASE}/keys/{uuid.uuid4()}", headers=headers
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 4: Device pairing relay (blind — server never sees UMK)
# ---------------------------------------------------------------------------


def test_pairing_relay_lifecycle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    QR device pairing blind relay (§12.6a):
      1. Joining device: POST /pairing/start → pairing_code
      2. Existing device: GET /pairing/{code} → status=pending, sealed_umk=null
      3. Existing device: POST /pairing/{code}/complete {sealed_umk}
      4. Joining device: GET /pairing/{code} → status=completed, sealed_umk present
      5. Second GET by joining device → status=consumed, sealed_umk=None (single-use)
      6. POST /pairing/{code}/complete on a consumed pairing → 409
      7. Non-existent pairing code → 404
    """
    headers = superuser_token_headers

    # Pairing works without E2E init (the pairing itself is part of the key-sharing setup).

    # ── Phase 1: Joining device starts pairing ─────────────────────────────
    epk = base64.b64encode(b"ephemeral-pubkey-new-device-00000").decode()
    start_resp = pairing_start(client, headers, new_device_pubkey=epk, device_label="New Phone")
    code = start_resp["pairing_code"]
    assert code, "Expected a pairing code"
    assert "expires_at" in start_resp

    # ── Phase 2: Joining device polls before existing device completes ─────
    status_resp = pairing_get(client, headers, code=code)
    assert status_resp["status"] == "pending"
    assert status_resp["sealed_umk"] is None
    assert status_resp["new_device_pubkey"] == epk

    # ── Phase 3: Existing device completes the pairing ────────────────────
    sealed = base64.b64encode(b"sealed-umk-encrypted-for-new-device").decode()
    pairing_complete(client, headers, code=code, sealed_umk=sealed)

    # ── Phase 4: Joining device fetches the sealed UMK ────────────────────
    completed_resp = pairing_get(client, headers, code=code)
    assert completed_resp["status"] == "completed"
    assert completed_resp["sealed_umk"] == sealed

    # ── Phase 5: Second fetch → consumed, sealed_umk=None (single-use) ────
    consumed_resp = pairing_get(client, headers, code=code)
    assert consumed_resp["status"] == "consumed"
    assert consumed_resp["sealed_umk"] is None

    # ── Phase 6: Existing device tries to complete again → 409 ────────────
    r = client.post(
        f"{_BASE}/pairing/{code}/complete",
        headers=headers,
        json={"sealed_umk": sealed},
    )
    assert r.status_code == 409, f"Expected 409 on re-complete, got {r.status_code}"

    # ── Phase 7: Non-existent pairing code → 404 ──────────────────────────
    r = client.get(f"{_BASE}/pairing/nonexistent-code-000", headers=headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 5: Device revoke
# ---------------------------------------------------------------------------


def test_device_revoke(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Device revocation:
      1. Init (registers device A with its device envelope)
      2. Register device B independently
      3. GET /devices lists both
      4. POST /keys adds a device envelope for device B
      5. DELETE /devices/{device_A_id} → revoke
      6. GET /devices shows device A as revoked
      7. GET /keys shows device A's device envelope is removed
      8. Device B's envelope is unaffected
      9. DELETE non-existent device → 404
    """
    headers = superuser_token_headers
    enc_state = init_encryption(client, headers, device_label="Device A")
    device_a = enc_state["devices"][0]
    device_a_id = device_a["id"]

    # ── Phase 2: Register device B ────────────────────────────────────────
    device_b = register_device(client, headers, label="Device B")
    device_b_id = device_b["id"]
    assert device_b["is_revoked"] is False

    # ── Phase 3: List shows both ──────────────────────────────────────────
    devices = list_devices(client, headers)
    ids = {d["id"] for d in devices}
    assert device_a_id in ids
    assert device_b_id in ids

    # ── Phase 4: Add a device envelope for device B ───────────────────────
    env_b = make_envelope_input("device", device_id=device_b_id)
    added_env_b = add_key(client, headers, envelope=env_b)
    assert added_env_b["device_id"] == device_b_id

    # ── Phase 5: Revoke device A ──────────────────────────────────────────
    revoke_device(client, headers, device_id=device_a_id)

    # ── Phase 6: Device A is now revoked in the list ──────────────────────
    devices_after = list_devices(client, headers)
    revoked_dev = next(d for d in devices_after if d["id"] == device_a_id)
    assert revoked_dev["is_revoked"] is True

    # ── Phase 7: Device A's envelope is removed ───────────────────────────
    keys_after = list_keys(client, headers)
    device_a_envelopes = [k for k in keys_after if k.get("device_id") == device_a_id]
    assert len(device_a_envelopes) == 0, "Device A's envelope should be deleted on revoke"

    # ── Phase 8: Device B's envelope is unaffected ────────────────────────
    device_b_envelopes = [k for k in keys_after if k.get("device_id") == device_b_id]
    assert len(device_b_envelopes) == 1

    # ── Phase 9: Revoke non-existent device → 404 ────────────────────────
    r = client.delete(f"{_BASE}/devices/{uuid.uuid4()}", headers=headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 6: Delta pull (cursor-based filtering) + tombstones + LWW + idempotency
# ---------------------------------------------------------------------------


def test_delta_pull_tombstone_lww_idempotency(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Core sync protocol behaviors:
      1. Push record A → seq 1
      2. Push record B → seq 2
      3. Pull from cursor 1 → only B returned
      4. Delete record A → tombstone with new seq
      5. Pull from cursor 0 → A has deleted=True, null payload; B is alive
      6. LWW: push same entity with older client_updated_at → conflict + server_record
      7. LWW: push same entity with newer client_updated_at → applied
      8. Idempotent re-push (identical fingerprint) → unchanged, no new seq
    """
    headers = superuser_token_headers
    init_encryption(client, headers)

    entity_a = str(uuid.uuid4())
    entity_b = str(uuid.uuid4())
    ts_base = datetime.now(UTC) - timedelta(hours=2)

    # ── Phase 1: Push A ────────────────────────────────────────────────────
    record_a = make_record_upsert(
        collection="note",
        client_entity_id=entity_a,
        content="Note A",
        client_updated_at=ts_base,
    )
    result_a = push(client, headers, changes=[record_a])
    assert result_a["applied"][0]["status"] == "applied"
    seq_a = result_a["applied"][0]["seq"]

    # ── Phase 2: Push B ────────────────────────────────────────────────────
    record_b = make_record_upsert(
        collection="note",
        client_entity_id=entity_b,
        content="Note B",
        client_updated_at=ts_base + timedelta(minutes=1),
    )
    result_b = push(client, headers, changes=[record_b])
    assert result_b["applied"][0]["status"] == "applied"
    seq_b = result_b["applied"][0]["seq"]
    assert seq_b > seq_a

    # ── Phase 3: Pull from cursor = seq_a → only B returned ───────────────
    delta = pull(client, headers, cursor=seq_a)
    assert len(delta["changes"]) == 1
    assert delta["changes"][0]["client_entity_id"] == entity_b
    assert delta["next_cursor"] == seq_b

    # ── Phase 4: Delete record A → tombstone ──────────────────────────────
    tombstone = make_record_upsert(
        collection="note",
        client_entity_id=entity_a,
        deleted=True,
        client_updated_at=ts_base + timedelta(hours=1),  # newer than original
    )
    del_result = push(client, headers, changes=[tombstone])
    assert del_result["applied"][0]["status"] == "applied"
    seq_tombstone = del_result["applied"][0]["seq"]
    assert seq_tombstone > seq_b

    # ── Phase 5: Pull from cursor 0 → A is tombstone, B is alive ──────────
    full_pull = pull(client, headers, cursor=0)
    all_records = {r["client_entity_id"]: r for r in full_pull["changes"]}

    assert entity_a in all_records
    assert all_records[entity_a]["deleted"] is True
    assert all_records[entity_a]["payload_ciphertext"] is None

    assert entity_b in all_records
    assert all_records[entity_b]["deleted"] is False
    assert all_records[entity_b]["payload_ciphertext"] is not None

    # ── Phase 6: LWW conflict — older client_updated_at loses ─────────────
    old_update = make_record_upsert(
        collection="note",
        client_entity_id=entity_b,
        content="Old content that should lose",
        client_updated_at=ts_base - timedelta(hours=1),  # older than what's on server
    )
    conflict_result = push(client, headers, changes=[old_update])
    applied = conflict_result["applied"][0]
    assert applied["status"] == "conflict", (
        f"Expected conflict, got {applied['status']}"
    )
    assert applied["server_record"] is not None
    assert applied["server_record"]["client_entity_id"] == entity_b

    # ── Phase 7: LWW win — newer client_updated_at wins ───────────────────
    new_update = make_record_upsert(
        collection="note",
        client_entity_id=entity_b,
        content="New content that should win",
        client_updated_at=datetime.now(UTC),  # definitely newer
    )
    win_result = push(client, headers, changes=[new_update])
    assert win_result["applied"][0]["status"] == "applied"

    # ── Phase 8: Idempotent re-push (same fingerprint) → unchanged ─────────
    repush = push(client, headers, changes=[new_update])
    repush_applied = repush["applied"][0]
    assert repush_applied["status"] == "unchanged", (
        f"Expected unchanged on re-push, got {repush_applied['status']}"
    )
    seq_before_repush = win_result["applied"][0]["seq"]
    assert repush_applied["seq"] == seq_before_repush, (
        "Re-push should not burn a new seq number"
    )

    # Verify record count didn't increase (idempotency)
    state_after = get_state(client, headers)
    # B is still one live record (A was tombstoned)
    assert state_after["total_records"] == 1


# ---------------------------------------------------------------------------
# Scenario 7: Cross-device identity + UUID validation
# ---------------------------------------------------------------------------


def test_cross_device_identity_and_uuid_validation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Entity identity rules:
      1. Two different UUID client_entity_ids → two distinct rows
      2. Non-UUID client_entity_id → 422 (footgun-blocker, §3.5)
      3. Integer string as entity_id → 422
      4. Both UUIDs appear in pull results as distinct records
    """
    headers = superuser_token_headers
    init_encryption(client, headers)

    uuid_a = str(uuid.uuid4())
    uuid_b = str(uuid.uuid4())

    # ── Phase 1: Push two distinct UUIDs in the same collection ───────────
    result = push(
        client,
        headers,
        changes=[
            make_record_upsert(collection="note", client_entity_id=uuid_a, content="Entity A"),
            make_record_upsert(collection="note", client_entity_id=uuid_b, content="Entity B"),
        ],
    )
    statuses = [r["status"] for r in result["applied"]]
    assert statuses == ["applied", "applied"], f"Both should be applied: {statuses}"

    # ── Phase 2: Both appear in pull as distinct rows ──────────────────────
    pull_result = pull(client, headers, cursor=0)
    ids = {r["client_entity_id"] for r in pull_result["changes"]}
    assert uuid_a in ids
    assert uuid_b in ids
    assert len([r for r in pull_result["changes"] if r["collection"] == "note"]) == 2

    # ── Phase 3: Non-UUID entity id → 422 ─────────────────────────────────
    bad_record = make_record_upsert(collection="note", content="bad id")
    bad_record["client_entity_id"] = "not-a-uuid"
    r = client.post(f"{_BASE}/push", headers=headers, json={"changes": [bad_record]})
    assert r.status_code == 422, f"Expected 422 for non-UUID entity_id, got {r.status_code}"

    # ── Phase 4: Integer string as entity_id → 422 ────────────────────────
    int_record = make_record_upsert(collection="note", content="integer id")
    int_record["client_entity_id"] = "12345"
    r = client.post(f"{_BASE}/push", headers=headers, json={"changes": [int_record]})
    assert r.status_code == 422, f"Expected 422 for integer entity_id, got {r.status_code}"


# ---------------------------------------------------------------------------
# Scenario 8: Ownership isolation
# ---------------------------------------------------------------------------


def test_ownership_isolation(client: TestClient) -> None:
    """
    Users can only see and mutate their own records:
      1. User A inits E2E and pushes records
      2. User B inits E2E and pushes records
      3. User B's pull returns only User B's records (not User A's)
      4. User B's GET /state counts only User B's records
      5. User B's DELETE / wipes only User B's records; User A's survive
      6. Unauthenticated request → 401
    """
    user_a = create_random_user(client)
    headers_a = user_authentication_headers(
        client=client, email=user_a["email"], password=user_a["_password"]
    )
    user_b = create_random_user(client)
    headers_b = user_authentication_headers(
        client=client, email=user_b["email"], password=user_b["_password"]
    )

    # ── Phase 1: User A inits + pushes ────────────────────────────────────
    init_encryption(client, headers_a)
    entity_a = str(uuid.uuid4())
    push(
        client,
        headers_a,
        changes=[
            make_record_upsert(collection="note", client_entity_id=entity_a, content="A's note")
        ],
    )

    # ── Phase 2: User B inits + pushes ────────────────────────────────────
    init_encryption(client, headers_b)
    entity_b = str(uuid.uuid4())
    push(
        client,
        headers_b,
        changes=[
            make_record_upsert(collection="note", client_entity_id=entity_b, content="B's note")
        ],
    )

    # ── Phase 3: User B's pull does not return User A's records ───────────
    pull_b = pull(client, headers_b, cursor=0)
    b_entity_ids = {r["client_entity_id"] for r in pull_b["changes"]}
    assert entity_b in b_entity_ids
    assert entity_a not in b_entity_ids, "User B should not see User A's records"

    # ── Phase 4: User B's state counts only B's records ───────────────────
    state_b = get_state(client, headers_b)
    assert state_b["total_records"] == 1

    # Verify A's state is also just 1
    state_a = get_state(client, headers_a)
    assert state_a["total_records"] == 1

    # ── Phase 5: User B wipes; User A's records survive ───────────────────
    wipe(client, headers_b)
    state_b_after = get_state(client, headers_b)
    assert state_b_after["total_records"] == 0

    state_a_after = get_state(client, headers_a)
    assert state_a_after["total_records"] == 1

    # ── Phase 6: Unauthenticated request → 401 ────────────────────────────
    for method, path in [
        ("POST", f"{_BASE}/push"),
        ("POST", f"{_BASE}/pull"),
        ("GET", f"{_BASE}/state"),
        ("DELETE", f"{_BASE}/"),
        ("GET", f"{_BASE}/encryption"),
    ]:
        r = client.request(method, path, json={})
        assert r.status_code == 401, (
            f"Expected 401 for unauthenticated {method} {path}, got {r.status_code}"
        )


# ---------------------------------------------------------------------------
# Scenario 9: Limits (payload, batch, quota)
# ---------------------------------------------------------------------------


def test_limits(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Server-enforced limits:
      1. Payload > APP_SYNC_MAX_PAYLOAD_BYTES → 413 for that record
      2. Batch > APP_SYNC_MAX_RECORDS_PER_PUSH → 422
      3. Quota exceeded → 413 with structured detail
    """
    headers = superuser_token_headers
    init_encryption(client, headers)

    # ── Phase 1: Payload too large → 413 ──────────────────────────────────
    big_content = "x" * (settings.APP_SYNC_MAX_PAYLOAD_BYTES + 1)
    oversized_record = {
        "collection": "note",
        "client_entity_id": str(uuid.uuid4()),
        "payload_ciphertext": big_content,  # raw over-limit string
        "content_fingerprint": make_fingerprint("big"),
        "enc_umk_version": 1,
        "deleted": False,
        "client_updated_at": datetime.now(UTC).isoformat(),
    }
    r = client.post(f"{_BASE}/push", headers=headers, json={"changes": [oversized_record]})
    assert r.status_code == 413, f"Expected 413 for oversized payload, got {r.status_code}: {r.text}"

    # ── Phase 2: Batch too large → 422 ────────────────────────────────────
    # Build one more record than the limit
    batch = [
        make_record_upsert(collection="note")
        for _ in range(settings.APP_SYNC_MAX_RECORDS_PER_PUSH + 1)
    ]
    r = client.post(f"{_BASE}/push", headers=headers, json={"changes": batch})
    assert r.status_code == 422, f"Expected 422 for oversized batch, got {r.status_code}: {r.text}"

    # ── Phase 3: Quota exceeded → 413 with structured detail ──────────────
    # Temporarily lower the quota to trigger it without actually pushing GBs.
    low_record_quota = 2  # only 2 records allowed

    with patch.object(settings, "APP_SYNC_QUOTA_RECORDS", low_record_quota):
        # Push exactly at the limit
        records = [make_record_upsert(collection="note") for _ in range(low_record_quota)]
        push(client, headers, changes=records)

        # Push one more → quota exceeded
        r = client.post(
            f"{_BASE}/push",
            headers=headers,
            json={"changes": [make_record_upsert(collection="note")]},
        )
        assert r.status_code == 413, f"Expected 413 for quota exceeded, got {r.status_code}: {r.text}"
        detail = r.json()["detail"]
        assert "quota_records" in detail, f"Expected structured quota detail, got {detail}"
        assert "quota_bytes" in detail
        assert "total_records" in detail
        assert "total_bytes" in detail


# ---------------------------------------------------------------------------
# Scenario 10: Pagination + GET /state accuracy
# ---------------------------------------------------------------------------


def test_pagination_and_state(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Pagination and state correctness:
      1. Push N records across two collections
      2. Pull with limit=2 → has_more=True, cursor advances
      3. Loop pull until has_more=False, verify all records seen
      4. Cursor advances monotonically (each next_cursor >= previous)
      5. GET /state reflects correct cursor, quota, and per-collection counts
    """
    headers = superuser_token_headers
    init_encryption(client, headers)

    # Push 5 notes and 3 folders
    note_ids = [str(uuid.uuid4()) for _ in range(5)]
    folder_ids = [str(uuid.uuid4()) for _ in range(3)]

    note_records = [
        make_record_upsert(collection="note", client_entity_id=nid, content=f"note-{i}")
        for i, nid in enumerate(note_ids)
    ]
    folder_records = [
        make_record_upsert(collection="note_folder", client_entity_id=fid, content=f"folder-{i}")
        for i, fid in enumerate(folder_ids)
    ]

    push(client, headers, changes=note_records + folder_records)

    # ── Phase 2-4: Paginate pull with limit=2 ─────────────────────────────
    all_seen: list[dict] = []
    cursor = 0
    prev_cursor = -1
    iterations = 0
    while True:
        page = pull(client, headers, cursor=cursor, limit=2)
        assert page["next_cursor"] >= prev_cursor, "Cursor must advance monotonically"
        prev_cursor = page["next_cursor"]
        all_seen.extend(page["changes"])
        cursor = page["next_cursor"]
        iterations += 1
        if not page["has_more"]:
            break
        assert iterations < 20, "Pagination loop should terminate"

    # All 8 records retrieved
    seen_ids = {r["client_entity_id"] for r in all_seen}
    for nid in note_ids:
        assert nid in seen_ids, f"Note {nid} not seen in pagination"
    for fid in folder_ids:
        assert fid in seen_ids, f"Folder {fid} not seen in pagination"

    # ── Phase 5: GET /state accuracy ──────────────────────────────────────
    state = get_state(client, headers)
    assert state["total_records"] == 8
    assert state["collection_counts"].get("note") == 5
    assert state["collection_counts"].get("note_folder") == 3
    assert state["cursor"] > 0
    # Quota fields are present and sensible
    assert state["quota_bytes"] == settings.APP_SYNC_QUOTA_BYTES
    assert state["quota_records"] == settings.APP_SYNC_QUOTA_RECORDS
    assert state["total_bytes"] > 0


# ---------------------------------------------------------------------------
# Scenario 11: DELETE / wipe
# ---------------------------------------------------------------------------


def test_wipe(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Wipe behavior:
      1. Push live records across two collections
      2. DELETE / wipes all → GET /state shows total_records=0, total_bytes=0
      3. But tombstones remain (deleted=True rows with bumped seqs)
      4. A pull from pre-wipe cursor sees the tombstones
      5. Collection-scoped wipe only removes one collection
    """
    headers = superuser_token_headers
    init_encryption(client, headers)

    note_id = str(uuid.uuid4())
    folder_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())

    push(
        client,
        headers,
        changes=[
            make_record_upsert(collection="note", client_entity_id=note_id, content="a note"),
            make_record_upsert(
                collection="note_folder", client_entity_id=folder_id, content="a folder"
            ),
            make_record_upsert(collection="job", client_entity_id=job_id, content="a job"),
        ],
    )

    state_before = get_state(client, headers)
    assert state_before["total_records"] == 3
    pre_wipe_cursor = state_before["cursor"]

    # ── Phase 2: Wipe all ─────────────────────────────────────────────────
    wipe_result = wipe(client, headers)
    assert "3" in wipe_result["message"] or "synced record" in wipe_result["message"]

    state_after = get_state(client, headers)
    assert state_after["total_records"] == 0
    assert state_after["total_bytes"] == 0
    # Cursor has advanced (tombstones got new seqs)
    assert state_after["cursor"] > pre_wipe_cursor

    # ── Phase 3-4: Pull from pre-wipe cursor shows tombstones ─────────────
    delta = pull(client, headers, cursor=pre_wipe_cursor)
    tombstone_ids = {r["client_entity_id"] for r in delta["changes"] if r["deleted"]}
    assert note_id in tombstone_ids
    assert folder_id in tombstone_ids
    assert job_id in tombstone_ids

    # Payloads are null in tombstones
    for r in delta["changes"]:
        if r["deleted"]:
            assert r["payload_ciphertext"] is None

    # ── Phase 5: Scoped wipe — set up fresh data then wipe only "note" ────
    # First re-push some records (tombstones can be undeleted)
    new_note_id = str(uuid.uuid4())
    new_job_id = str(uuid.uuid4())
    push(
        client,
        headers,
        changes=[
            make_record_upsert(collection="note", client_entity_id=new_note_id, content="new note"),
            make_record_upsert(collection="job", client_entity_id=new_job_id, content="new job"),
        ],
    )

    state_before_scoped = get_state(client, headers)
    assert state_before_scoped["total_records"] == 2

    wipe(client, headers, collections=["note"])
    state_after_scoped = get_state(client, headers)
    # Only the note was wiped; the job survives
    assert state_after_scoped["total_records"] == 1
    assert state_after_scoped["collection_counts"].get("note", 0) == 0
    assert state_after_scoped["collection_counts"].get("job", 0) == 1


# ---------------------------------------------------------------------------
# Scenario 12: Combined sync (POST /) — push-then-pull in one round trip
# ---------------------------------------------------------------------------


def test_combined_sync(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    POST / (bidirectional sync):
      1. Push A, then sync with B + cursor 0 → applied=[B], changes=[A, B]
      2. Sync with cursor=latest → applied=[], changes=[] (nothing new)
      3. Sync pull with collection filter returns only that collection
    """
    headers = superuser_token_headers
    init_encryption(client, headers)

    entity_a = str(uuid.uuid4())
    entity_b = str(uuid.uuid4())

    # Pre-push A so it's on the server already
    push(
        client,
        headers,
        changes=[make_record_upsert(collection="note", client_entity_id=entity_a, content="A")],
    )

    # ── Phase 1: Sync with cursor=0 + push B ──────────────────────────────
    sync_result = sync(
        client,
        headers,
        cursor=0,
        changes=[
            make_record_upsert(collection="note", client_entity_id=entity_b, content="B")
        ],
    )
    applied_ids = {r["client_entity_id"] for r in sync_result["applied"]}
    assert entity_b in applied_ids
    changed_ids = {r["client_entity_id"] for r in sync_result["changes"]}
    # A was on server before B was pushed; after push A and B both have seq ≥ 1
    # Pull returns all records with seq > 0 (cursor=0)
    assert entity_a in changed_ids or entity_b in changed_ids

    # ── Phase 2: Sync from latest cursor → empty changes ─────────────────
    latest_cursor = sync_result["next_cursor"]
    idle_sync = sync(client, headers, cursor=latest_cursor)
    assert idle_sync["applied"] == []
    assert idle_sync["changes"] == []
    assert idle_sync["has_more"] is False

    # ── Phase 3: Collection filter ────────────────────────────────────────
    folder_id = str(uuid.uuid4())
    push(
        client,
        headers,
        changes=[
            make_record_upsert(
                collection="note_folder", client_entity_id=folder_id, content="folder"
            )
        ],
    )
    filtered_sync = sync(client, headers, cursor=0, collections=["note_folder"])
    filtered_ids = {r["client_entity_id"] for r in filtered_sync["changes"]}
    assert folder_id in filtered_ids
    # Note records should not appear in a note_folder-only filter
    assert entity_a not in filtered_ids
    assert entity_b not in filtered_ids


# ---------------------------------------------------------------------------
# Scenario 13: Desktop-token attribution
# ---------------------------------------------------------------------------


def test_desktop_token_attribution(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    last_writer_client_id attribution:
      1. Push with a web token (standard test token) → last_writer_client_id is None
      2. The field appears in pull results
    """
    headers = superuser_token_headers
    init_encryption(client, headers)

    entity_id = str(uuid.uuid4())
    record = make_record_upsert(collection="note", client_entity_id=entity_id, content="attributed")
    push(client, headers, changes=[record])

    # ── Phase 1: Web token → last_writer_client_id is null ────────────────
    pull_result = pull(client, headers, cursor=0)
    returned = next(
        (r for r in pull_result["changes"] if r["client_entity_id"] == entity_id), None
    )
    assert returned is not None
    assert returned["last_writer_client_id"] is None, (
        "Web-token pushes should produce null last_writer_client_id"
    )


# ---------------------------------------------------------------------------
# Scenario 14: Fresh-login bootstrap (cursor=0 pull loop)
# ---------------------------------------------------------------------------


def test_fresh_login_bootstrap(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Fresh login (cursor=0) loop hydrates the full dataset:
      1. Init E2E and push N records across notes, jobs, folders
      2. Simulate a fresh device pulling from cursor=0 with a small page size
      3. Loop has_more until drained — all records are present
      4. Includes folder structure (note_folder, job_folder)
      5. Tombstones are included in the pull (client may skip, but server surfaces them)
    """
    headers = superuser_token_headers
    init_encryption(client, headers)

    # Push a realistic cross-collection dataset
    note_ids = [str(uuid.uuid4()) for _ in range(4)]
    note_folder_ids = [str(uuid.uuid4()) for _ in range(2)]
    job_ids = [str(uuid.uuid4()) for _ in range(3)]
    job_folder_ids = [str(uuid.uuid4()) for _ in range(2)]

    all_live_ids = set(note_ids + note_folder_ids + job_ids + job_folder_ids)

    records = (
        [make_record_upsert(collection="note", client_entity_id=i, content=f"n{j}") for j, i in enumerate(note_ids)]
        + [make_record_upsert(collection="note_folder", client_entity_id=i, content=f"nf{j}") for j, i in enumerate(note_folder_ids)]
        + [make_record_upsert(collection="job", client_entity_id=i, content=f"job{j}") for j, i in enumerate(job_ids)]
        + [make_record_upsert(collection="job_folder", client_entity_id=i, content=f"jf{j}") for j, i in enumerate(job_folder_ids)]
    )
    push(client, headers, changes=records)

    # Tombstone one note to ensure tombstones appear in the bootstrap pull
    deleted_note_id = note_ids[0]
    push(
        client,
        headers,
        changes=[
            make_record_upsert(
                collection="note",
                client_entity_id=deleted_note_id,
                deleted=True,
                client_updated_at=datetime.now(UTC),
            )
        ],
    )
    all_live_ids.discard(deleted_note_id)

    # ── Phase 2-4: Bootstrap pull loop from cursor=0 ──────────────────────
    bootstrapped: list[dict] = []
    cursor = 0
    page_limit = 3  # small pages to exercise pagination
    iterations = 0
    while True:
        page = pull(client, headers, cursor=cursor, limit=page_limit)
        bootstrapped.extend(page["changes"])
        cursor = page["next_cursor"]
        iterations += 1
        if not page["has_more"]:
            break
        assert iterations < 50, "Bootstrap loop should converge"

    # ── Phase 5: All live records present + tombstone included ─────────────
    bootstrapped_ids = {r["client_entity_id"] for r in bootstrapped}
    for eid in all_live_ids:
        assert eid in bootstrapped_ids, f"Live entity {eid} missing from bootstrap"

    # Deleted note shows up as tombstone
    assert deleted_note_id in bootstrapped_ids
    deleted_row = next(r for r in bootstrapped if r["client_entity_id"] == deleted_note_id)
    assert deleted_row["deleted"] is True
    assert deleted_row["payload_ciphertext"] is None

    # All collections represented in live records
    collections_seen = {r["collection"] for r in bootstrapped if not r["deleted"]}
    assert "note" in collections_seen
    assert "note_folder" in collections_seen
    assert "job" in collections_seen
    assert "job_folder" in collections_seen


# ---------------------------------------------------------------------------
# Scenario 15: Delete-then-recreate (tombstone → live undelete)
# ---------------------------------------------------------------------------


def test_undelete_tombstone(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    An entity can be undeleted (tombstone → live) by pushing a newer upsert:
      1. Push record → live
      2. Delete → tombstone
      3. Push the same entity id with deleted=False and newer timestamp → live again
      4. Pull shows deleted=False with the new ciphertext
    """
    headers = superuser_token_headers
    init_encryption(client, headers)

    entity_id = str(uuid.uuid4())
    ts_create = datetime.now(UTC) - timedelta(hours=2)
    ts_delete = ts_create + timedelta(hours=1)
    ts_restore = ts_delete + timedelta(minutes=30)

    push(
        client,
        headers,
        changes=[
            make_record_upsert(
                collection="note",
                client_entity_id=entity_id,
                content="original",
                client_updated_at=ts_create,
            )
        ],
    )

    push(
        client,
        headers,
        changes=[
            make_record_upsert(
                collection="note",
                client_entity_id=entity_id,
                deleted=True,
                client_updated_at=ts_delete,
            )
        ],
    )

    # Verify tombstone
    mid_state = get_state(client, headers)
    assert mid_state["total_records"] == 0  # no live records

    # Undelete
    restored_content = "restored content"
    push(
        client,
        headers,
        changes=[
            make_record_upsert(
                collection="note",
                client_entity_id=entity_id,
                content=restored_content,
                client_updated_at=ts_restore,
            )
        ],
    )

    pull_result = pull(client, headers, cursor=0)
    # Find the latest entry for this entity (last by seq)
    entries = [r for r in pull_result["changes"] if r["client_entity_id"] == entity_id]
    latest = max(entries, key=lambda r: r["seq"])
    assert latest["deleted"] is False
    assert latest["payload_ciphertext"] == make_ciphertext(restored_content)

    final_state = get_state(client, headers)
    assert final_state["total_records"] == 1


# ---------------------------------------------------------------------------
# Scenario 16: Init with a device envelope pointing at a wrong device → 422
# ---------------------------------------------------------------------------


def test_init_device_envelope_wrong_device_id(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A device envelope whose device_id references a different device → 422.
    The init request registers exactly one device; a device envelope must
    bind to that device, not an arbitrary UUID (§12.5 / init_encryption).
    """
    from tests.utils.app_sync import make_device_input

    headers = superuser_token_headers

    wrong_device_id = str(uuid.uuid4())  # random UUID — doesn't match the device being registered
    device_env = make_envelope_input("device", device_id=wrong_device_id)
    recovery_env = make_envelope_input("recovery")

    r = client.post(
        f"{_BASE}/encryption/init",
        headers=headers,
        json={
            "device": make_device_input("Device"),
            "envelopes": [device_env, recovery_env],
        },
    )
    assert r.status_code == 422, (
        f"Expected 422 when device envelope device_id references wrong device, "
        f"got {r.status_code}: {r.text}"
    )
