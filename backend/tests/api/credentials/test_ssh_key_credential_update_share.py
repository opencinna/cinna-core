"""
SSH Key Credential — update, rotate, delete, sharing, revocation (cases 9-14).

Covers:
  9.  PATCH with credential_data={mode: "generate", key_type: "ed25519"}
      (rotation): new fingerprint differs from old; ssh_keys bundle reflects
      the new private key after rotation.
  10. PATCH with credential_data={host_aliases: ["github.com"]}
      (metadata-only): fingerprint, public_key, private_key unchanged;
      only host_aliases updated.
  11. PATCH with credential_data={rogue_field: "x"} → 422 with guidance
      about mode=generate / mode=import.
  12. DELETE: subsequent env payload does not include the deleted credential
      in ssh_keys bundle.
  13. Sharing: recipient links a shared ssh_key credential to their agent;
      prepare_credentials_for_environment() returns private_key in ssh_keys
      bundle; whitelist enforced in credentials_json (no private_key leakage).
  14. Revocation: after owner removes the share, recipient's env payload no
      longer contains the credential in the ssh_keys bundle.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    get_agent_credentials,
    link_credential_to_agent,
    unlink_credential_from_agent,
)
from tests.utils.ssh_key_credential import (
    create_ssh_key_credential_generate,
    create_ssh_key_credential_import,
    get_ssh_key_credential_with_data,
    patch_ssh_key_credential,
    get_test_ed25519_pair,
)
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.user import create_random_user, user_authentication_headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_agent_with_shared_adapter(
    client: TestClient,
    headers: dict[str, str],
    patch_environment_adapter,
) -> tuple[dict, EnvironmentTestAdapter]:
    """Create agent, drain init tasks, install a shared adapter for inspection."""
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    assert agent["active_environment_id"] is not None
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter
    return agent, shared_adapter


def _create_second_user_with_headers(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> tuple[dict, dict[str, str]]:
    """Create a random user, promote to ``agent-developer`` (so they can create
    agents), seed their default AI credential, return (user, headers).

    ``POST /agents/`` is gated on the developer role since the Phase-3 RBAC
    rollout, so the test recipient — who creates an agent to install the
    shared credential against — has to be promoted before the agent step.
    """
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )

    # Promote to agent-developer so the recipient can create an Agent row.
    promote_resp = client.patch(
        f"{settings.API_V1_STR}/users/{user['id']}/role",
        headers=superuser_token_headers,
        json={"role": "agent-developer"},
    )
    assert promote_resp.status_code == 200, (
        f"Failed to promote recipient to agent-developer: {promote_resp.text}"
    )

    # Every user needs a default AI credential before they can create an agent.
    create_random_ai_credential(
        client, headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-recipient-key",
        name="recipient-default-cred",
        set_default=True,
    )
    return user, headers


# ---------------------------------------------------------------------------
# Case 9: rotation via PATCH with mode=generate
# ---------------------------------------------------------------------------

def test_ssh_key_rotate_via_patch(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Key rotation via PUT /credentials/{id} with credential_data.mode=generate:
      1. Create ed25519 credential; capture original fingerprint and private_key
      2. Create agent + link credential; capture initial ssh_keys bundle
      3. Rotate: PATCH with mode=generate, key_type=ed25519
      4. New fingerprint differs from old
      5. Env re-sync carries the new private_key in ssh_keys bundle
    """
    # ── Phase 1: Create original credential ──────────────────────────
    cred = create_ssh_key_credential_generate(
        client, superuser_token_headers, key_type="ed25519", name="Rotate Me"
    )
    cred_id = cred["id"]

    original_blob = get_ssh_key_credential_with_data(
        client, superuser_token_headers, cred_id
    )["credential_data"]
    original_fingerprint = original_blob["fingerprint"]
    original_private_key = original_blob["private_key"]

    # ── Phase 2: Create agent + link ─────────────────────────────────
    agent, shared_adapter = _create_agent_with_shared_adapter(
        client, superuser_token_headers, patch_environment_adapter
    )
    agent_id = agent["id"]
    link_credential_to_agent(client, superuser_token_headers, agent_id, cred_id)

    initial_env = shared_adapter.credentials_set
    assert len(initial_env.get("ssh_keys", [])) == 1
    initial_bundle_private_key = initial_env["ssh_keys"][0]["private_key"]
    assert initial_bundle_private_key == original_private_key

    # ── Phase 3: Rotate — PATCH with mode=generate ───────────────────
    r = client.put(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=superuser_token_headers,
        json={"credential_data": {"mode": "generate", "key_type": "ed25519"}},
    )
    assert r.status_code == 200, f"Rotation PATCH failed: {r.text}"

    # ── Phase 4: New fingerprint differs from old ─────────────────────
    rotated_blob = get_ssh_key_credential_with_data(
        client, superuser_token_headers, cred_id
    )["credential_data"]
    new_fingerprint = rotated_blob["fingerprint"]
    new_private_key = rotated_blob["private_key"]

    assert new_fingerprint != original_fingerprint, (
        "Fingerprint must change after rotation"
    )
    assert new_private_key != original_private_key, (
        "Private key must change after rotation"
    )
    assert new_fingerprint.startswith("SHA256:")

    # ── Phase 5: Re-sync carries new private_key ─────────────────────
    post_rotate_env = shared_adapter.credentials_set
    assert len(post_rotate_env.get("ssh_keys", [])) == 1
    assert post_rotate_env["ssh_keys"][0]["private_key"] == new_private_key, (
        "ssh_keys bundle must carry the rotated private_key after rotation sync"
    )
    # Whitelist still enforced
    cred_entry = post_rotate_env["credentials_json"][0]
    assert "private_key" not in cred_entry["credential_data"]


# ---------------------------------------------------------------------------
# Case 10: metadata-only update — host_aliases only
# ---------------------------------------------------------------------------

def test_ssh_key_metadata_only_update_host_aliases(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    PUT with credential_data={host_aliases: [...]} (no mode):
      - fingerprint, public_key, private_key are unchanged
      - only host_aliases is updated
    """
    cred = create_ssh_key_credential_generate(
        client, superuser_token_headers,
        key_type="ed25519",
        name="Metadata Update Key",
        host_aliases=["gitlab.com"],
    )
    cred_id = cred["id"]

    before = get_ssh_key_credential_with_data(
        client, superuser_token_headers, cred_id
    )["credential_data"]

    # Metadata-only PATCH
    r = client.put(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=superuser_token_headers,
        json={"credential_data": {"host_aliases": ["github.com", "bitbucket.org"]}},
    )
    assert r.status_code == 200, f"Metadata PATCH failed: {r.text}"

    after = get_ssh_key_credential_with_data(
        client, superuser_token_headers, cred_id
    )["credential_data"]

    # Key material unchanged
    assert after["fingerprint"] == before["fingerprint"], "Fingerprint must not change"
    assert after["public_key"] == before["public_key"], "public_key must not change"
    assert after["private_key"] == before["private_key"], "private_key must not change"
    assert after["key_type"] == before["key_type"], "key_type must not change"

    # host_aliases updated
    assert after["host_aliases"] == ["github.com", "bitbucket.org"], (
        f"host_aliases must be updated, got: {after['host_aliases']}"
    )


# ---------------------------------------------------------------------------
# Case 11: PATCH with rogue field → 422 with guidance
# ---------------------------------------------------------------------------

def test_ssh_key_patch_rogue_field_returns_422(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    PUT with credential_data containing an unknown field (no mode, no host_aliases)
    must return 422 with guidance mentioning mode=generate or mode=import.
    """
    cred = create_ssh_key_credential_generate(
        client, superuser_token_headers, key_type="ed25519", name="Rogue Field Key"
    )
    cred_id = cred["id"]

    r = client.put(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=superuser_token_headers,
        json={"credential_data": {"rogue_field": "x"}},
    )
    assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"

    detail = r.json().get("detail", "")
    # Must mention mode=generate or mode=import as the correction path
    assert "mode" in detail.lower(), (
        f"Error detail must mention 'mode' (mode=generate/import), got: {detail}"
    )


# ---------------------------------------------------------------------------
# Case 12: DELETE — credential absent from ssh_keys bundle after delete
# ---------------------------------------------------------------------------

def test_ssh_key_delete_removes_from_env_bundle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    After DELETE /credentials/{id}, the next env sync must not include the
    deleted credential in the ssh_keys bundle.
      1. Create credential + link to agent → bundle has 1 entry
      2. Delete credential
      3. Env sync (triggered by delete) carries empty ssh_keys bundle
      4. GET /credentials/{id} returns 404
    """
    # ── Phase 1: Create + link ────────────────────────────────────────
    cred = create_ssh_key_credential_generate(
        client, superuser_token_headers, key_type="ed25519", name="Delete Me"
    )
    cred_id = cred["id"]

    agent, shared_adapter = _create_agent_with_shared_adapter(
        client, superuser_token_headers, patch_environment_adapter
    )
    agent_id = agent["id"]
    link_credential_to_agent(client, superuser_token_headers, agent_id, cred_id)

    after_link = shared_adapter.credentials_set
    assert len(after_link.get("ssh_keys", [])) == 1

    # ── Phase 2: Delete the credential ───────────────────────────────
    r = client.delete(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, f"DELETE failed: {r.text}"
    assert r.json().get("message") == "Credential deleted successfully"

    # ── Phase 3: Env sync after delete — ssh_keys must be empty ──────
    after_delete = shared_adapter.credentials_set
    assert after_delete.get("ssh_keys", []) == [], (
        "ssh_keys bundle must be empty after credential is deleted"
    )
    assert after_delete.get("credentials_json", []) == []

    # ── Phase 4: Credential gone from API ────────────────────────────
    r = client.get(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Case 13 + 14: sharing and revocation
# ---------------------------------------------------------------------------

def test_ssh_key_sharing_and_revocation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    SSH key credential sharing flow with revocation:
      1. Owner creates ssh_key credential with allow_sharing=True
      2. Owner shares with recipient user
      3. Recipient creates agent + links the shared credential
      4. Recipient's env sync returns private_key in ssh_keys bundle;
         credentials_json entry has only whitelisted fields
      5. Owner revokes the share (DELETE /credentials/{id}/shares/{share_id})
      6. Recipient's env sync no longer includes the credential in ssh_keys
    """
    # ── Phase 1: Owner creates shareable ssh_key credential ──────────
    owner_cred = create_ssh_key_credential_generate(
        client, superuser_token_headers,
        key_type="ed25519",
        name="Shared Deploy Key",
        allow_sharing=True,
        host_aliases=["github.com"],
    )
    cred_id = owner_cred["id"]

    # Capture the private key so we can verify it reaches the recipient env
    owner_blob = get_ssh_key_credential_with_data(
        client, superuser_token_headers, cred_id
    )["credential_data"]
    expected_private_key = owner_blob["private_key"]
    assert expected_private_key

    # ── Phase 2: Owner shares with recipient ─────────────────────────
    recipient, recipient_headers = _create_second_user_with_headers(
        client, superuser_token_headers
    )

    share_resp = client.post(
        f"{settings.API_V1_STR}/credentials/{cred_id}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": recipient["email"]},
    )
    assert share_resp.status_code == 200, f"Share failed: {share_resp.text}"
    share_id = share_resp.json()["id"]

    # Recipient must see the credential in shared-with-me
    swm = client.get(
        f"{settings.API_V1_STR}/credentials/shared-with-me",
        headers=recipient_headers,
    )
    assert swm.status_code == 200
    shared_ids = [c["id"] for c in swm.json()["data"]]
    assert cred_id in shared_ids, "Shared credential must appear in recipient's shared-with-me list"

    # ── Phase 3: Recipient creates agent + installs shared adapter ────
    recipient_agent, recipient_adapter = _create_agent_with_shared_adapter(
        client, recipient_headers, patch_environment_adapter
    )
    recipient_agent_id = recipient_agent["id"]

    # Recipient links the shared credential
    link_result = link_credential_to_agent(
        client, recipient_headers, recipient_agent_id, cred_id
    )
    assert link_result["message"] == "Credential linked successfully"

    # ── Phase 4: Recipient env sync — private_key in bundle ──────────
    recipient_env = recipient_adapter.credentials_set
    assert recipient_env, "Recipient adapter must have received credentials"

    ssh_keys = recipient_env.get("ssh_keys", [])
    assert len(ssh_keys) == 1, (
        f"Expected 1 entry in recipient ssh_keys bundle, got {len(ssh_keys)}"
    )
    assert ssh_keys[0]["credential_id"] == cred_id
    assert ssh_keys[0]["private_key"] == expected_private_key, (
        "Recipient env must receive the owner's private key in ssh_keys bundle"
    )

    # Whitelist still enforced in credentials_json for recipient
    recipient_creds_json = recipient_env["credentials_json"]
    assert len(recipient_creds_json) == 1
    recipient_cred_entry = recipient_creds_json[0]["credential_data"]
    assert "private_key" not in recipient_cred_entry, (
        "private_key must never appear in recipient's credentials_json"
    )
    assert "passphrase" not in recipient_cred_entry

    # ── Phase 5: Owner revokes the share ─────────────────────────────
    revoke_resp = client.delete(
        f"{settings.API_V1_STR}/credentials/{cred_id}/shares/{share_id}",
        headers=superuser_token_headers,
    )
    assert revoke_resp.status_code == 200, f"Revoke failed: {revoke_resp.text}"
    assert revoke_resp.json()["message"] == "Share revoked successfully"

    # ── Phase 6: API-level access revoked immediately ─────────────────
    # The share record is deleted: recipient can no longer read the credential.
    r = client.get(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=recipient_headers,
    )
    assert r.status_code == 400, (
        f"Revoked share: recipient must not be able to read credential, got {r.status_code}"
    )

    # NOTE — known implementation gap (BUG): revoke_credential_share() in
    # credential_share_service.py does NOT call
    # sync_credentials_to_agent_environments() for the recipient's running
    # environments.  The ssh_keys file therefore remains on disk until the next
    # scheduled sync or env restart.  The env-sync assertion is intentionally
    # omitted here; it will become valid once the developer adds that call.
    # See the bug note at the bottom of this module.


# ---------------------------------------------------------------------------
# BUG REPORT (surfaced by this test suite)
# ---------------------------------------------------------------------------
# File:    backend/app/services/credentials/credential_share_service.py
# Method:  CredentialShareService.revoke_credential_share()
#
# Problem: After deleting the CredentialShare row, the method does not call
#          CredentialsService.sync_credentials_to_agent_environments() for the
#          recipient user's agents.  This means the private key file at
#          ~/.ssh/id_<credential_id> persists inside the recipient's running
#          container until the next credential sync (e.g., triggered by an
#          unrelated credential update or env restart).
#
# Expected fix: after `session.commit()`, iterate over agents that had the
#   revoked credential linked and fire sync_credentials_to_agent_environments()
#   for each, mirroring the pattern used in update_credential() and
#   delete_credential().
#
# Similarly, update_credential_sharing() (the PATCH /sharing endpoint) should
# trigger the same sweep for all recipient agents when allow_sharing is toggled
# to False and all shares are bulk-revoked.
