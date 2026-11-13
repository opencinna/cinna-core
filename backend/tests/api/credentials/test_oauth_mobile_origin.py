"""Mobile origin for the credential OAuth authorize flow.

Plan: docs/plans/mobile_oauth_credential_authorize_plan.md §4.B. Authorize
takes an optional ``{"return_scheme": ...}`` body; a valid Cinna Mobile scheme
gives a ``mobile.<scheme>.<token>`` state and the callback reports origin
"mobile". Google is mocked at the helper's patch sites; everything is driven
through the API.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.credential import create_random_credential
from tests.utils.oauth_credential import (
    complete_oauth_callback,
    google_oauth_configured,
    google_oauth_unconfigured,
    state_from_url,
)
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR


def _new_credential(client: TestClient, headers: dict[str, str]) -> dict:
    return create_random_credential(
        client, headers, credential_type="gmail_oauth", credential_data={}
    )


def _authorize(
    client: TestClient, headers: dict[str, str], cid: str, body: dict | None = None
):
    with google_oauth_configured():
        if body is None:
            return client.post(f"{API}/credentials/{cid}/oauth/authorize", headers=headers)
        return client.post(
            f"{API}/credentials/{cid}/oauth/authorize", headers=headers, json=body
        )


def test_authorize_without_body_keeps_web_origin_and_bare_state(
    client: TestClient,
) -> None:
    """No body: bare state, expires_at present, callback origin is web."""
    _, headers = create_random_user_with_headers(client)
    cred = _new_credential(client, headers)

    r = _authorize(client, headers, cred["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    state = body["state"]
    assert not state.startswith("desktop.")
    assert not state.startswith("mobile.")
    assert state_from_url(body["authorization_url"]) == state
    assert isinstance(body["expires_at"], int)

    ok = complete_oauth_callback(client, state)
    assert ok.status_code == 200, ok.text
    assert ok.json()["origin"] == "web"


@pytest.mark.parametrize("scheme", ["cinna-mobile-dev", "cinna-mobile"])
def test_authorize_with_return_scheme_stores_mobile_origin(
    client: TestClient, scheme: str
) -> None:
    """A valid scheme prefixes the state and the callback returns origin mobile."""
    _, headers = create_random_user_with_headers(client)
    cred = _new_credential(client, headers)

    r = _authorize(client, headers, cred["id"], {"return_scheme": scheme})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"].startswith(f"mobile.{scheme}.")
    assert state_from_url(body["authorization_url"]) == body["state"]
    assert isinstance(body["expires_at"], int)

    ok = complete_oauth_callback(client, body["state"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["origin"] == "mobile"
    assert ok.json()["credential_id"] == cred["id"]


@pytest.mark.parametrize(
    "scheme",
    ["https", "cinna", "cinna-mobile://x", "evil-cinna-mobile", "cinna-mobile\n", ""],
)
def test_authorize_rejects_invalid_return_scheme_and_creates_no_state(
    client: TestClient, scheme: str
) -> None:
    """Invalid scheme -> 422; a follow-up no-body authorize still works."""
    _, headers = create_random_user_with_headers(client)
    cred = _new_credential(client, headers)

    r = _authorize(client, headers, cred["id"], {"return_scheme": scheme})
    assert r.status_code == 422, r.text

    again = _authorize(client, headers, cred["id"])
    assert again.status_code == 200, again.text
    assert complete_oauth_callback(client, again.json()["state"]).status_code == 200


def test_authorize_with_body_keeps_owner_and_config_rules(client: TestClient) -> None:
    """Another user gets 403, unknown id 404, unconfigured Google 501 — with a body."""
    _, owner_headers = create_random_user_with_headers(client)
    _, other_headers = create_random_user_with_headers(client)
    cred = _new_credential(client, owner_headers)
    body = {"return_scheme": "cinna-mobile-dev"}

    assert _authorize(client, other_headers, cred["id"], body).status_code == 403
    assert _authorize(client, owner_headers, str(uuid.uuid4()), body).status_code == 404
    with google_oauth_unconfigured():
        r = client.post(
            f"{API}/credentials/{cred['id']}/oauth/authorize",
            headers=owner_headers,
            json=body,
        )
    assert r.status_code == 501, r.text


def test_callback_leaves_non_placeholder_credential_unaffected(
    client: TestClient,
) -> None:
    """A regular (non-placeholder) credential stays is_placeholder == False."""
    _, headers = create_random_user_with_headers(client)
    cred = _new_credential(client, headers)
    assert cred["is_placeholder"] is False

    state = _authorize(client, headers, cred["id"]).json()["state"]
    assert complete_oauth_callback(client, state).status_code == 200

    r = client.get(f"{API}/credentials/{cred['id']}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["is_placeholder"] is False
