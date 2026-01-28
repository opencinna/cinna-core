from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string


def generate_random_ssh_key(
    client: TestClient,
    token_headers: dict[str, str],
    name: str | None = None,
) -> dict:
    """Generate a random SSH key via the API and return the response data."""
    if name is None:
        name = f"test-key-{random_lower_string()[:12]}"

    data = {"name": name}
    r = client.post(
        f"{settings.API_V1_STR}/ssh-keys/generate",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200
    return r.json()


def import_ssh_key(
    client: TestClient,
    token_headers: dict[str, str],
    name: str | None = None,
    public_key: str | None = None,
    private_key: str | None = None,
    passphrase: str | None = None,
) -> dict:
    """Import an SSH key via the API and return the response data."""
    if name is None:
        name = f"test-key-{random_lower_string()[:12]}"

    # Use test keys if not provided
    if public_key is None or private_key is None:
        public_key, private_key = get_test_key_pair()

    data = {
        "name": name,
        "public_key": public_key,
        "private_key": private_key,
    }
    if passphrase is not None:
        data["passphrase"] = passphrase

    r = client.post(
        f"{settings.API_V1_STR}/ssh-keys/",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200
    return r.json()


def get_test_key_pair() -> tuple[str, str]:
    """Return a valid test RSA key pair (public_key, private_key)."""
    # This is a test-only RSA 2048-bit key pair, NOT for production use
    public_key = """ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDFvY7pPHWjGfvd3Aj8L+Fh8rYc0Xq8mMRkBMm9z0t5NXBK5j2L6xBvMn3FxSE6V4K7hS2ZL8vHvFqYC9L7M5MBqE1B5g8V1Q5LnXME8MjH6B8v5gLzQ8XmR4V3K9H2L5J6M8N7P1Q3R5S7T9U2V4W6X8Y1Z3A5B7C9D2E4F6G8H1J3K5L7M9N2P4Q6R8S1T3U5V7W9X2Y4Z6a8b1c3d5e7f9g2h4j6k8l1m3n5p7q9r2s4t6u8v1w3x5y7z9 test@example.com"""

    private_key = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEAxb2O6Tx1oxn73dwI/C/hYfK2HNF6vJjEZATJvc9LeTVwSuY9
i+sQbzJ9xcUhOleCu4UtmS/Lx7xamAvS+zOTAahNQeYPFdUOS51zBPDIx+gfL+YC
80PF5keFdyvR9i+SejPDez9UN0e0u0/VNld2V3hYeVh6WHxYflh+aH9of3h/iH+Y
f6h/uH/If9h/6H/4gAiAGIAogCmAOIA5gDqAO4A8gD2APoA/gECAQYBCgEOARABF
gEaARoBHgEiASYBKgEuATIBNgE6AT4BQgFGAUoBTgFSAVYBWgFeAWIBZgFqAW4Bc
gF2AXoBfgGCAYYBigGOAZIBlgGaAZ4BogGmAaoBrgGyAbYBugG+AcIBxgHKAc4B0
gHWAdoB3gHiAeYB6gHuAfIB9gH6Af4CAgIGAgoCDgISAhYCGgIeAiICJgIqAi4CM
gI2AjoCPgJCAkYCSgJOAlICVgJaAl4CYgJmAmoCbgJyAnYCegJ+AoIChgKKAo4Ck
gKWApoCngKiAqYCqgKuArICtgK6Ar4CwgLGAsoCzgLQCAwEAAQJBALQhD7CZ8OZ6
QKV1C7E6V5yLZP4P2c8FQFQvPPnXK1Z4Y9d1hYQq8gxg9YQVH1n4DqMzIYm7Nz5X
1v5P2N8Q9XECIQDzj1Q5L8m4N7P1Q3R5S7T9U2V4W6X8Y1Z3A5B7C9D2EwIhAM5P
7Q9R2S4T6U8V1W3X5Y7Z9a2b4c6d8e1f3g5h7j9kAiEAp6Q8R1S3T5U7V9W2X4Y6
Z8a1b3c5d7e9f2g4h6j8kAiAl5R7S9T2U4V6W8X1Y3Z5a7b9c2d4e6f8g1h3j5k7
AiEAmYS3T5U7V9W2X4Y6Z8a1b3c5d7e9f2g4h6j8k1l3n5
-----END RSA PRIVATE KEY-----"""

    return public_key, private_key
