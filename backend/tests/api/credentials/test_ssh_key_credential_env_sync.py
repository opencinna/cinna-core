"""
SSH Key Credential — env-sync whitelist & bundle shape tests (case 8).

Covers:
  a) prepare_credentials_for_environment() returns ssh_keys list of length 1
     with full private_key, public_key, host_aliases, credential_id
  b) credentials_json entry for ssh_key contains ONLY whitelisted fields —
     private_key and passphrase are NOT present
  c) ssh_keys bundle is empty when no ssh_key credentials are linked
  d) Multiple ssh_key credentials all appear in ssh_keys bundle
  e) host_aliases=None serializes safely (defaults to ["*"]) in both bundle
     and whitelisted credentials_json
  f) Prompt README redaction: private_key body is absent (or shown as
     REDACTED) in the generated credentials_readme text
"""
from fastapi.testclient import TestClient

from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    get_agent_credentials,
    link_credential_to_agent,
    real_credentials_json,
    unlink_credential_from_agent,
)
from tests.utils.ssh_key_credential import (
    create_ssh_key_credential_generate,
    create_ssh_key_credential_import,
    get_ssh_key_credential_with_data,
    get_test_ed25519_pair,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_agent_with_shared_adapter(
    client: TestClient,
    headers: dict[str, str],
    patch_environment_adapter,
) -> tuple[dict, EnvironmentTestAdapter]:
    """Create agent, drain env-init tasks, then install a shared adapter for inspection."""
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    assert agent["active_environment_id"] is not None

    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter
    return agent, shared_adapter


# ---------------------------------------------------------------------------
# Case 8a + 8b + 8f  (core whitelist scenario, single credential)
# ---------------------------------------------------------------------------

def test_ssh_key_env_sync_whitelist_and_bundle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Single ssh_key credential linked to an agent:
      1. Create credential (generate mode, ed25519)
      2. Create agent + install shared adapter
      3. Link credential → adapter.credentials_set is populated
      4. ssh_keys bundle: length 1, contains private_key, public_key,
         host_aliases, credential_id
      5. credentials_json entry: private_key and passphrase are NOT present;
         only whitelisted fields (public_key, fingerprint, key_type,
         host_aliases) are in credential_data
      6. credentials_readme: does not contain private_key body text
    """
    # ── Phase 1: Create ssh_key credential ───────────────────────────
    cred = create_ssh_key_credential_generate(
        client, superuser_token_headers,
        key_type="ed25519",
        name="Test SSH Deploy Key",
        host_aliases=["github.com"],
    )
    cred_id = cred["id"]
    assert cred["type"] == "ssh_key"
    assert cred["status"] == "complete"

    # Capture private_key from the with-data response so we can assert
    # it appears in the ssh_keys bundle but NOT in credentials_json.
    with_data = get_ssh_key_credential_with_data(
        client, superuser_token_headers, cred_id
    )
    stored_private_key = with_data["credential_data"]["private_key"]
    assert stored_private_key, "private_key must be present in owner blob"

    # ── Phase 2: Create agent + shared adapter ────────────────────────
    agent, shared_adapter = _create_agent_with_shared_adapter(
        client, superuser_token_headers, patch_environment_adapter
    )
    agent_id = agent["id"]

    # ── Phase 3: Link credential → env sync fires ─────────────────────
    result = link_credential_to_agent(
        client, superuser_token_headers, agent_id, cred_id
    )
    assert result["message"] == "Credential linked successfully"

    # Verify credential appears in agent list
    agent_creds = get_agent_credentials(
        client, superuser_token_headers, agent_id
    )
    assert agent_creds["count"] == 1
    assert agent_creds["data"][0]["id"] == cred_id

    # ── Phase 4: Inspect adapter — ssh_keys bundle ────────────────────
    env_data = shared_adapter.credentials_set
    assert env_data, "Adapter must have received credentials"

    ssh_keys = env_data.get("ssh_keys", [])
    assert len(ssh_keys) == 1, f"Expected 1 ssh_key bundle entry, got {len(ssh_keys)}"

    bundle_entry = ssh_keys[0]
    assert bundle_entry["credential_id"] == cred_id
    assert bundle_entry["private_key"] == stored_private_key
    assert bundle_entry["public_key"].startswith("ssh-ed25519 ")
    assert bundle_entry["host_aliases"] == ["github.com"]
    # passphrase is None for a generated key
    assert bundle_entry.get("passphrase") is None

    # ── Phase 5: credentials_json whitelist enforcement ───────────────
    creds_json = real_credentials_json(env_data)
    assert len(creds_json) == 1
    entry = creds_json[0]
    assert entry["id"] == cred_id
    assert entry["type"] == "ssh_key"

    cred_data_in_json = entry["credential_data"]
    # Whitelisted fields must be present
    assert "public_key" in cred_data_in_json
    assert "fingerprint" in cred_data_in_json
    assert "key_type" in cred_data_in_json
    assert "host_aliases" in cred_data_in_json
    # Sensitive fields must NOT be present
    assert "private_key" not in cred_data_in_json, (
        "private_key must never appear in credentials_json"
    )
    assert "passphrase" not in cred_data_in_json, (
        "passphrase must never appear in credentials_json"
    )

    # ── Phase 6: README redaction ─────────────────────────────────────
    readme = env_data.get("credentials_readme", "")
    assert readme, "credentials_readme must be non-empty"
    assert "ssh_key" in readme, "README must mention the ssh_key credential type"
    # The private key body must not appear verbatim in the README
    assert stored_private_key not in readme, (
        "Raw private_key body must not appear in credentials_readme"
    )


# ---------------------------------------------------------------------------
# Case 8c  — ssh_keys bundle is empty when no ssh_key credentials linked
# ---------------------------------------------------------------------------

def test_ssh_key_env_sync_empty_bundle_when_no_ssh_keys(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    When an agent has only non-ssh_key credentials linked (or none at all),
    the ssh_keys key in the env payload must be an empty list.
    """
    agent, shared_adapter = _create_agent_with_shared_adapter(
        client, superuser_token_headers, patch_environment_adapter
    )
    agent_id = agent["id"]

    # Trigger a sync by forcing a link of a non-ssh_key credential, then unlink
    # so we can test the empty state.  Simpler: link then immediately unlink an
    # email_imap credential — the unlink sync should carry ssh_keys=[].
    from tests.utils.credential import create_random_credential
    imap_cred = create_random_credential(client, superuser_token_headers, credential_type="email_imap")
    link_credential_to_agent(client, superuser_token_headers, agent_id, imap_cred["id"])

    env_data = shared_adapter.credentials_set
    assert env_data, "Adapter must have received credentials after link"

    ssh_keys = env_data.get("ssh_keys", [])
    assert ssh_keys == [], (
        f"ssh_keys must be empty list when no ssh_key credentials linked, got: {ssh_keys}"
    )


# ---------------------------------------------------------------------------
# Case 8d  — multiple ssh_key credentials all appear in bundle
# ---------------------------------------------------------------------------

def test_ssh_key_env_sync_multiple_credentials_in_bundle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    When two ssh_key credentials are linked to the same agent, both must
    appear in the ssh_keys bundle with their respective credential_ids.
    """
    # Create two ssh_key credentials
    cred_a = create_ssh_key_credential_generate(
        client, superuser_token_headers, key_type="ed25519", name="Key A"
    )
    cred_b = create_ssh_key_credential_generate(
        client, superuser_token_headers, key_type="rsa", name="Key B"
    )

    agent, shared_adapter = _create_agent_with_shared_adapter(
        client, superuser_token_headers, patch_environment_adapter
    )
    agent_id = agent["id"]

    # Link both
    link_credential_to_agent(client, superuser_token_headers, agent_id, cred_a["id"])
    link_credential_to_agent(client, superuser_token_headers, agent_id, cred_b["id"])

    env_data = shared_adapter.credentials_set
    assert env_data, "Adapter must have received credentials"

    ssh_keys = env_data.get("ssh_keys", [])
    assert len(ssh_keys) == 2, (
        f"Expected 2 ssh_key bundle entries, got {len(ssh_keys)}"
    )

    bundle_ids = {e["credential_id"] for e in ssh_keys}
    assert cred_a["id"] in bundle_ids, "Key A must appear in ssh_keys bundle"
    assert cred_b["id"] in bundle_ids, "Key B must appear in ssh_keys bundle"

    # Each entry must have private_key present
    for entry in ssh_keys:
        assert entry["private_key"], f"private_key must be non-empty for {entry['credential_id']}"

    # credentials_json must also have 2 entries, neither with private_key
    creds_json = real_credentials_json(env_data)
    assert len(creds_json) == 2
    for entry in creds_json:
        assert "private_key" not in entry["credential_data"]
        assert "passphrase" not in entry["credential_data"]


# ---------------------------------------------------------------------------
# Case 8e  — host_aliases=None defaults to ["*"] in bundle and json
# ---------------------------------------------------------------------------

def test_ssh_key_env_sync_null_host_aliases_defaults_to_wildcard(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    When host_aliases is None (not specified at create time), the env sync
    must supply ["*"] in both the ssh_keys bundle and credentials_json so
    the agent-env config generation always has a valid Host pattern.
    """
    # Create without host_aliases (defaults to None)
    cred = create_ssh_key_credential_generate(
        client, superuser_token_headers,
        key_type="ed25519",
        name="Wildcard Key",
        # host_aliases intentionally omitted
    )
    cred_id = cred["id"]

    # Confirm the stored blob has host_aliases=None
    with_data = get_ssh_key_credential_with_data(
        client, superuser_token_headers, cred_id
    )
    assert with_data["credential_data"]["host_aliases"] is None

    agent, shared_adapter = _create_agent_with_shared_adapter(
        client, superuser_token_headers, patch_environment_adapter
    )
    agent_id = agent["id"]
    link_credential_to_agent(client, superuser_token_headers, agent_id, cred_id)

    env_data = shared_adapter.credentials_set
    assert env_data

    # ssh_keys bundle: host_aliases must be ["*"]
    ssh_keys = env_data.get("ssh_keys", [])
    assert len(ssh_keys) == 1
    assert ssh_keys[0]["host_aliases"] == ["*"], (
        f"Expected host_aliases=[\"*\"] for null aliases, got: {ssh_keys[0]['host_aliases']}"
    )

    # credentials_json: host_aliases must also be ["*"] (via _process_ssh_key_for_env)
    creds_json = real_credentials_json(env_data)
    assert len(creds_json) == 1
    cred_data = creds_json[0]["credential_data"]
    assert cred_data["host_aliases"] == ["*"], (
        f"Expected host_aliases=[\"*\"] in credentials_json, got: {cred_data['host_aliases']}"
    )
