"""Test utilities for SSH key credential operations.

Helpers for creating, updating, and inspecting ssh_key credentials via the API.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string


# ---------------------------------------------------------------------------
# Canonical test key material
# ---------------------------------------------------------------------------

# A real Ed25519 key pair generated for test use only — NOT for production.
# Used by import-mode tests so the server can fingerprint real key material.
_TEST_ED25519_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GkZH "
    "test@example.com"
)
_TEST_ED25519_PRIVATE_KEY = """\
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQy
NTUxOQAAACDjKqp5Fc65tEnRulDqKii7Gm4B+QvaJFotHYdpfRpGRwAAAJDmHSXm5h0l
5gAAAAtzc2gtZWQyNTUxOQAAACDjKqp5Fc65tEnRulDqKii7Gm4B+QvaJFotHYdpfRpG
RwAAAEBqhCcJ3dXy/9hcJTWFcHU3kv+EAyLp0TKivFbFuSW844yqqeRXOubRJ0bpQ6io
ouxpuAfkL2iRaLR2HaX0aRkHAAAADXRlc3RAZXhhbXBsZQ==
-----END OPENSSH PRIVATE KEY-----"""


def get_test_ed25519_pair() -> tuple[str, str]:
    """Return a valid Ed25519 (public_key, private_key) test pair."""
    return _TEST_ED25519_PUBLIC_KEY, _TEST_ED25519_PRIVATE_KEY


# ---------------------------------------------------------------------------
# Create helpers
# ---------------------------------------------------------------------------

def create_ssh_key_credential_generate(
    client: TestClient,
    token_headers: dict[str, str],
    key_type: str = "rsa",
    name: str | None = None,
    host_aliases: list[str] | None = None,
    allow_sharing: bool = False,
) -> dict:
    """Create an ssh_key credential in generate mode via POST /credentials/.

    Returns the public CredentialPublic response (no credential_data).
    """
    payload: dict = {
        "name": name or f"test-ssh-{random_lower_string()[:10]}",
        "type": "ssh_key",
        "allow_sharing": allow_sharing,
        "credential_data": {
            "mode": "generate",
            "key_type": key_type,
        },
    }
    if host_aliases is not None:
        payload["credential_data"]["host_aliases"] = host_aliases

    r = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=token_headers,
        json=payload,
    )
    assert r.status_code == 200, (
        f"create_ssh_key_credential_generate failed ({r.status_code}): {r.text}"
    )
    return r.json()


def create_ssh_key_credential_import(
    client: TestClient,
    token_headers: dict[str, str],
    public_key: str | None = None,
    private_key: str | None = None,
    name: str | None = None,
    host_aliases: list[str] | None = None,
    allow_sharing: bool = False,
) -> dict:
    """Create an ssh_key credential in import mode via POST /credentials/.

    Uses the canonical Ed25519 test pair unless keys are explicitly supplied.
    Returns the public CredentialPublic response.
    """
    if public_key is None or private_key is None:
        public_key, private_key = get_test_ed25519_pair()

    payload: dict = {
        "name": name or f"test-ssh-import-{random_lower_string()[:10]}",
        "type": "ssh_key",
        "allow_sharing": allow_sharing,
        "credential_data": {
            "mode": "import",
            "public_key": public_key,
            "private_key": private_key,
        },
    }
    if host_aliases is not None:
        payload["credential_data"]["host_aliases"] = host_aliases

    r = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=token_headers,
        json=payload,
    )
    assert r.status_code == 200, (
        f"create_ssh_key_credential_import failed ({r.status_code}): {r.text}"
    )
    return r.json()


def get_ssh_key_credential_with_data(
    client: TestClient,
    token_headers: dict[str, str],
    credential_id: str,
) -> dict:
    """GET /credentials/{id}/with-data — returns decrypted credential_data."""
    r = client.get(
        f"{settings.API_V1_STR}/credentials/{credential_id}/with-data",
        headers=token_headers,
    )
    assert r.status_code == 200, (
        f"get_ssh_key_credential_with_data failed ({r.status_code}): {r.text}"
    )
    return r.json()


def patch_ssh_key_credential(
    client: TestClient,
    token_headers: dict[str, str],
    credential_id: str,
    credential_data: dict | None = None,
    name: str | None = None,
    notes: str | None = None,
) -> dict:
    """PATCH (PUT) /credentials/{id} for ssh_key credentials.

    Wraps the PUT endpoint used by all credential updates.
    """
    payload: dict = {}
    if credential_data is not None:
        payload["credential_data"] = credential_data
    if name is not None:
        payload["name"] = name
    if notes is not None:
        payload["notes"] = notes

    r = client.put(
        f"{settings.API_V1_STR}/credentials/{credential_id}",
        headers=token_headers,
        json=payload,
    )
    assert r.status_code == 200, (
        f"patch_ssh_key_credential failed ({r.status_code}): {r.text}"
    )
    return r.json()
