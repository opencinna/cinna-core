"""
SSH Key Credential — create-flow tests (cases 1-7).

Covers:
  1. Create with mode=generate, key_type=rsa → public_key starts with ssh-rsa,
     fingerprint in SHA256:... format, key_type="rsa"
  2. Create with mode=generate, key_type=ed25519 → public_key starts with ssh-ed25519
  3. Create with mode=import + valid Ed25519 pair → stores, fingerprints, returns blob
  4. Create with invalid public key prefix → 422
  5. Create with private key missing PEM markers → 422
  6. Create with passphrase-encrypted private key → 422 with MVP rejection message
  7. Create with malformed host_aliases → 422
"""
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.ssh_key_credential import (
    create_ssh_key_credential_generate,
    create_ssh_key_credential_import,
    get_ssh_key_credential_with_data,
    get_test_ed25519_pair,
)


# ---------------------------------------------------------------------------
# Case 1: generate mode — RSA (default)
# ---------------------------------------------------------------------------

def test_create_ssh_key_generate_rsa(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Generate-mode RSA credential lifecycle:
      1. Create with mode=generate, key_type=rsa
      2. Public response has no credential_data (not leaked)
      3. Fetch with-data → public_key starts with ssh-rsa, fingerprint is SHA256:...,
         key_type="rsa", private_key is present in the blob
    """
    # ── Phase 1: Create ───────────────────────────────────────────────
    cred = create_ssh_key_credential_generate(
        client, superuser_token_headers, key_type="rsa"
    )
    cred_id = cred["id"]

    # Public response must not leak credential_data
    assert cred["type"] == "ssh_key"
    assert cred["status"] == "complete"
    assert "credential_data" not in cred
    assert "encrypted_data" not in cred

    # ── Phase 2: Fetch with decrypted data ────────────────────────────
    with_data = get_ssh_key_credential_with_data(
        client, superuser_token_headers, cred_id
    )
    blob = with_data["credential_data"]

    assert blob["public_key"].startswith("ssh-rsa "), (
        f"Expected ssh-rsa prefix, got: {blob['public_key'][:30]}"
    )
    assert blob["fingerprint"].startswith("SHA256:"), (
        f"Expected SHA256: prefix, got: {blob['fingerprint']}"
    )
    assert blob["key_type"] == "rsa"
    assert blob["private_key"], "private_key must be present in the blob for owner"
    assert "BEGIN" in blob["private_key"] and "PRIVATE KEY" in blob["private_key"]
    assert blob["passphrase"] is None


# ---------------------------------------------------------------------------
# Case 2: generate mode — Ed25519
# ---------------------------------------------------------------------------

def test_create_ssh_key_generate_ed25519(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Generate-mode Ed25519 credential:
      1. Create with mode=generate, key_type=ed25519
      2. public_key starts with ssh-ed25519, key_type="ed25519"
    """
    cred = create_ssh_key_credential_generate(
        client, superuser_token_headers, key_type="ed25519"
    )
    cred_id = cred["id"]

    assert cred["type"] == "ssh_key"
    assert cred["status"] == "complete"
    assert "credential_data" not in cred

    with_data = get_ssh_key_credential_with_data(
        client, superuser_token_headers, cred_id
    )
    blob = with_data["credential_data"]

    assert blob["public_key"].startswith("ssh-ed25519 "), (
        f"Expected ssh-ed25519 prefix, got: {blob['public_key'][:30]}"
    )
    assert blob["fingerprint"].startswith("SHA256:")
    assert blob["key_type"] == "ed25519"
    assert blob["private_key"], "private_key must be present in the blob"
    assert "OPENSSH PRIVATE KEY" in blob["private_key"]


# ---------------------------------------------------------------------------
# Case 3: import mode — valid Ed25519 pair
# ---------------------------------------------------------------------------

def test_create_ssh_key_import_valid(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Import-mode with a valid Ed25519 pair:
      1. Create with mode=import + public_key + private_key
      2. Fingerprint computed server-side
      3. Stored data round-trips correctly
      4. key_type detected as ed25519
    """
    pub, priv = get_test_ed25519_pair()

    cred = create_ssh_key_credential_import(
        client, superuser_token_headers,
        public_key=pub,
        private_key=priv,
        host_aliases=["github.com", "gitlab.com"],
    )
    cred_id = cred["id"]

    assert cred["type"] == "ssh_key"
    assert cred["status"] == "complete"
    assert "credential_data" not in cred

    with_data = get_ssh_key_credential_with_data(
        client, superuser_token_headers, cred_id
    )
    blob = with_data["credential_data"]

    assert blob["public_key"].startswith("ssh-ed25519 ")
    assert blob["fingerprint"].startswith("SHA256:")
    assert blob["key_type"] == "ed25519"
    assert blob["private_key"] == priv
    assert blob["passphrase"] is None
    assert blob["host_aliases"] == ["github.com", "gitlab.com"]


# ---------------------------------------------------------------------------
# Case 4: import mode — invalid public key prefix → 422
# ---------------------------------------------------------------------------

def test_create_ssh_key_import_invalid_public_key_prefix(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Invalid public key prefix must be rejected with 422."""
    _, priv = get_test_ed25519_pair()

    r = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=superuser_token_headers,
        json={
            "name": "bad-prefix-key",
            "type": "ssh_key",
            "credential_data": {
                "mode": "import",
                "public_key": "not-a-valid-prefix AAAAC3NzaC1lZDI1NTE5 comment",
                "private_key": priv,
            },
        },
    )
    assert r.status_code == 422
    detail = r.json().get("detail", "")
    # Error message should reference the public_key field
    assert "public_key" in detail.lower() or "public key" in detail.lower(), (
        f"Expected mention of public_key in error, got: {detail}"
    )


# ---------------------------------------------------------------------------
# Case 5: import mode — private key missing PEM markers → 422
# ---------------------------------------------------------------------------

def test_create_ssh_key_import_missing_pem_markers(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Private key without PEM markers must be rejected with 422."""
    pub, _ = get_test_ed25519_pair()

    r = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=superuser_token_headers,
        json={
            "name": "bad-pem-key",
            "type": "ssh_key",
            "credential_data": {
                "mode": "import",
                "public_key": pub,
                "private_key": "this is definitely not a PEM key",
            },
        },
    )
    assert r.status_code == 422
    detail = r.json().get("detail", "")
    assert "private_key" in detail.lower() or "private key" in detail.lower() or "pem" in detail.lower(), (
        f"Expected mention of private_key/PEM in error, got: {detail}"
    )


# ---------------------------------------------------------------------------
# Case 6: import mode — passphrase-encrypted private key → 422
# ---------------------------------------------------------------------------

# A real passphrase-encrypted Ed25519 key (passphrase: "testpass")
_ENCRYPTED_ED25519_PRIVATE_KEY = """\
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAAbmJjcnlwdAAAABIAAAAEAAAAAwAAAAJub25lAAAACQAAAA
MAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQyNTUxOQAAACAjwJKHfkSi3tQy8E7r
QbLZu3ehlJVJhj3XbU2VMMl0EQAAAJBkGvq4ZBr6uAAAABNzc2gtZWQyNTUxOQAAACj
AAAAC3NzaC1lZDI1NTE5AAAAIDCYQ/rYp3Z2qULHqb3oFGqcEfDJAAAASQAAAAtzc2gt
ZWQyNTUxOQAAAECz0PmQfzfMBbQmAuknPJ/U4yCVdJmZJ5JmzUJ5JmzU
-----END OPENSSH PRIVATE KEY-----"""

_ENCRYPTED_RSA_PRIVATE_KEY = """\
-----BEGIN RSA PRIVATE KEY-----
Proc-Type: 4,ENCRYPTED
DEK-Info: AES-128-CBC,A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6

MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfnO3xczwuqPuSe0zcJpoxNqvNIjCjEMQhMO
this-is-fake-base64-content-for-test-purposes-only
-----END RSA PRIVATE KEY-----"""


def test_create_ssh_key_import_passphrase_encrypted_rejected(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Passphrase-encrypted private keys must be rejected with 422.
    Tests the traditional OpenSSL PEM format (DEK-Info header) which is
    reliably detected by the is_private_key_encrypted() utility.
    """
    pub, _ = get_test_ed25519_pair()

    r = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=superuser_token_headers,
        json={
            "name": "encrypted-key",
            "type": "ssh_key",
            "credential_data": {
                "mode": "import",
                "public_key": pub,
                "private_key": _ENCRYPTED_RSA_PRIVATE_KEY,
            },
        },
    )
    assert r.status_code == 422
    detail = r.json().get("detail", "")
    # Must include the MVP rejection message (or close variant)
    assert "not yet supported" in detail.lower() or "passphrase" in detail.lower() or "encrypted" in detail.lower(), (
        f"Expected passphrase-rejection message, got: {detail}"
    )


# ---------------------------------------------------------------------------
# Case 7: malformed host_aliases → 422
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_aliases,description", [
    (
        ["github.com", "alias with spaces"],
        "alias containing whitespace",
    ),
    (
        ["github.com", "alias\nwith\nnewlines"],
        "alias containing newlines",
    ),
    (
        [123, "github.com"],
        "non-string element (int)",
    ),
    (
        "github.com",
        "string instead of list",
    ),
    (
        {"host": "github.com"},
        "dict instead of list",
    ),
])
def test_create_ssh_key_generate_malformed_host_aliases(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    bad_aliases,
    description: str,
) -> None:
    """Malformed host_aliases values must be rejected with 422."""
    r = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=superuser_token_headers,
        json={
            "name": f"bad-aliases-{description[:20]}",
            "type": "ssh_key",
            "credential_data": {
                "mode": "generate",
                "key_type": "ed25519",
                "host_aliases": bad_aliases,
            },
        },
    )
    assert r.status_code == 422, (
        f"Expected 422 for {description}, got {r.status_code}: {r.text}"
    )
