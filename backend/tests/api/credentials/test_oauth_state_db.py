"""Credential OAuth state lives in the database (``credential_oauth_state``).

Plan: docs/plans/desktop_google_oauth_credentials_plan.md §1. Authorize and
callback are separate requests sharing nothing but the DB row; the state is
single-use and expires.
"""
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.credential import create_random_credential
from tests.utils.oauth_credential import (
    EXCHANGE_CODE,
    complete_oauth_callback,
    google_oauth_configured,
    state_from_url,
)
from tests.utils.user import create_random_user_with_headers
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
TTL = "app.services.credentials.oauth_credentials_service.OAUTH_STATE_TTL_SECONDS"


def _new_credential(client: TestClient, headers: dict[str, str]) -> dict:
    return create_random_credential(
        client, headers, credential_type="gmail_oauth", credential_data={}
    )


def _authorize(client: TestClient, headers: dict[str, str], cid: str):
    with google_oauth_configured():
        return client.post(f"{API}/credentials/{cid}/oauth/authorize", headers=headers)


def test_state_started_on_one_request_completes_on_another_and_is_single_use(
    client: TestClient,
) -> None:
    """
    1. Authorize returns a bare (web) state
    2. A separate callback request completes it: origin web, metadata shows the email
    3. Replaying the state → 400; a random state → 400
    """
    _, headers = create_random_user_with_headers(client)
    cred = _new_credential(client, headers)

    r = _authorize(client, headers, cred["id"])
    assert r.status_code == 200, r.text
    state = r.json()["state"]
    assert state_from_url(r.json()["authorization_url"]) == state
    assert not state.startswith("desktop.")

    ok = complete_oauth_callback(client, state, email="web-user@example.com")
    assert ok.status_code == 200, ok.text
    assert ok.json()["origin"] == "web"
    assert ok.json()["credential_id"] == cred["id"]
    meta = client.get(f"{API}/credentials/{cred['id']}/oauth/metadata", headers=headers)
    assert meta.status_code == 200, meta.text
    assert meta.json()["user_email"] == "web-user@example.com"

    assert complete_oauth_callback(client, state).status_code == 400
    assert complete_oauth_callback(client, random_lower_string()).status_code == 400


def test_expired_state_is_rejected(client: TestClient) -> None:
    _, headers = create_random_user_with_headers(client)
    cred = _new_credential(client, headers)
    with patch(TTL, 0):
        state = _authorize(client, headers, cred["id"]).json()["state"]
    r = complete_oauth_callback(client, state)
    assert r.status_code == 400, r.text
    assert "state" in r.json()["detail"].lower()


def test_failed_code_exchange_still_consumes_the_state(client: TestClient) -> None:
    _, headers = create_random_user_with_headers(client)
    cred = _new_credential(client, headers)
    state = _authorize(client, headers, cred["id"]).json()["state"]

    with google_oauth_configured(), patch(
        EXCHANGE_CODE, new=AsyncMock(side_effect=RuntimeError("google down"))
    ):
        r = client.post(
            f"{API}/credentials/oauth/callback", json={"code": "c", "state": state}
        )
    assert r.status_code == 400, r.text
    assert complete_oauth_callback(client, state).status_code == 400


def test_credential_deleted_between_authorize_and_callback_is_400(
    client: TestClient,
) -> None:
    _, headers = create_random_user_with_headers(client)
    cred = _new_credential(client, headers)
    state = _authorize(client, headers, cred["id"]).json()["state"]

    d = client.delete(f"{API}/credentials/{cred['id']}", headers=headers)
    assert d.status_code == 200, d.text
    assert complete_oauth_callback(client, state).status_code == 400


def test_authorize_stays_owner_only_and_callback_needs_a_state(client: TestClient) -> None:
    _, owner_h = create_random_user_with_headers(client)
    _, other_h = create_random_user_with_headers(client)
    cred = _new_credential(client, owner_h)
    assert _authorize(client, other_h, cred["id"]).status_code == 403
    r = complete_oauth_callback(client, "")
    assert r.status_code == 400, r.text


def test_web_callback_with_owner_session_names_the_masked_account(
    client: TestClient,
) -> None:
    """
    1. The owner's browser (signed in) completes its own web authorization → 200
    2. The response names the owning account by masked email
    """
    owner, headers = create_random_user_with_headers(client)
    cred = _new_credential(client, headers)
    state = _authorize(client, headers, cred["id"]).json()["state"]

    ok = complete_oauth_callback(client, state, headers=headers)
    assert ok.status_code == 200, ok.text
    local, domain = owner["email"].split("@")
    assert ok.json()["owner_email_masked"] == f"{local[:2]}***@{domain}"
    assert owner["email"] not in ok.text
