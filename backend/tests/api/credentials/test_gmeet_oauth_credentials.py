"""
Google Meet OAuth credential types (gmeet_oauth, gmeet_oauth_readonly).

Scenarios:
  1. Create + read round trip for both types (status complete, tokens not leaked
     in the public response)
  2. Authorize URL carries exactly the Meet scopes for each type
"""
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.credential import create_random_credential, get_credential

_BASE = settings.API_V1_STR
_G = "https://www.googleapis.com/auth/"
_COMMON = [_G + "userinfo.email", _G + "userinfo.profile", "openid"]
EXPECTED_SCOPES = {
    "gmeet_oauth": [
        _G + "meetings.space.created",
        _G + "meetings.space.settings",
        _G + "meetings.space.readonly",
        *_COMMON,
    ],
    "gmeet_oauth_readonly": [_G + "meetings.space.readonly", _G + "drive.meet.readonly", *_COMMON],
}


@contextmanager
def google_enabled():
    with (
        patch.object(settings, "GOOGLE_CLIENT_ID", "test-client-id"),
        patch.object(settings, "GOOGLE_CLIENT_SECRET", "test-secret"),
    ):
        yield


@pytest.mark.parametrize("cred_type", list(EXPECTED_SCOPES))
def test_create_and_read_gmeet_credential(
    client: TestClient, superuser_token_headers: dict[str, str], cred_type: str
) -> None:
    cred = create_random_credential(client, superuser_token_headers, credential_type=cred_type)
    assert cred["type"] == cred_type
    assert cred["status"] == "complete"

    read = get_credential(client, superuser_token_headers, cred["id"])
    assert read["id"] == cred["id"]
    assert read["type"] == cred_type
    assert read["status"] == "complete"
    assert read.get("credential_data") is None


@pytest.mark.parametrize("cred_type", list(EXPECTED_SCOPES))
def test_authorize_url_contains_exact_meet_scopes(
    client: TestClient, superuser_token_headers: dict[str, str], cred_type: str
) -> None:
    cred = create_random_credential(client, superuser_token_headers, credential_type=cred_type)
    with google_enabled():
        r = client.post(
            f"{_BASE}/credentials/{cred['id']}/oauth/authorize",
            headers=superuser_token_headers,
        )
    assert r.status_code == 200, r.text
    url = urlparse(r.json()["authorization_url"])
    assert url.netloc == "accounts.google.com"
    qs = parse_qs(url.query)
    assert qs["scope"][0].split(" ") == EXPECTED_SCOPES[cred_type]
    assert qs["access_type"] == ["offline"]
    assert qs["state"] == [r.json()["state"]]

