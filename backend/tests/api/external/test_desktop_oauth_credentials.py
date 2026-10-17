"""Desktop Google OAuth credentials — create / reauthorize / list / materialize / token.

Plan: docs/plans/desktop_google_oauth_credentials_plan.md §3, §4, §6.1.

Core brokers the grant: the refresh token never leaves Core. Everything is set
up through the API (Desktop session via PKCE consent, authorization via the
public ``/credentials/oauth/callback``). Google is never contacted: the code
exchange and refresh exchange are patched at their single binding sites.
"""
import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.credentials.oauth_credentials_service import OAuthRefreshError
from tests.stubs.socketio_stub import StubSocketIOConnector
from tests.utils.cli import (
    account_cli_headers,
    bootstrap_account_token,
    exchange_for_desktop_token,
)
from tests.utils.credential import (
    create_random_credential,
    set_credential_sharing,
    share_credential_via_api,
    update_credential,
)
from tests.utils.desktop_auth import revoke_desktop_client
from tests.utils.desktop_credentials import (
    DESKTOP_CREDENTIALS_URL,
    desktop_session,
    list_desktop_credentials,
)
from tests.utils.oauth_credential import (
    EXCHANGE_REFRESH,
    complete_oauth_callback,
    google_oauth_configured,
    google_oauth_unconfigured,
    query_of,
    state_from_url,
)
from tests.utils.security_event import events_of_type
from tests.utils.user import create_random_user_with_headers, promote_to_developer

API = settings.API_V1_STR
OAUTH_URL = f"{DESKTOP_CREDENTIALS_URL}/oauth"
MATERIALIZE = f"{DESKTOP_CREDENTIALS_URL}/materialize"
ACCESS = "ya29.callback-access"
REFRESH = "1//callback-refresh"
NEW_ACCESS = "ya29.refreshed-access"
EMAIL = "granted@example.com"


@pytest.fixture(autouse=True)
def patch_external_services():
    """Override the conftest fixture: keep the REAL credential refresh."""
    with patch("app.services.events.event_service.socketio_connector", StubSocketIOConnector()):
        yield


@pytest.fixture
def google():
    """Google configured + a refresh mock that returns a fresh token."""
    mock = AsyncMock(return_value={"access_token": NEW_ACCESS, "expires_in": 3600})
    with patch(EXCHANGE_REFRESH, new=mock), google_oauth_configured():
        yield mock


def _create(client, dh, type_="gmail_oauth", name=None):
    body = {"type": type_}
    if name:
        body["name"] = name
    with google_oauth_configured():
        return client.post(OAUTH_URL, headers=dh, json=body)


def _ready(client, dh, *, type_="gmail_oauth", expires_in=3600, email=EMAIL) -> str:
    """Create via Desktop and complete the callback; returns the credential id."""
    r = _create(client, dh, type_)
    assert r.status_code == 201, r.text
    cb = complete_oauth_callback(
        client, state_from_url(r.json()["authorization_url"]),
        email=email, expires_in=expires_in,
    )
    assert cb.status_code == 200, cb.text
    return r.json()["credential_id"]


def _item(client, headers, cid) -> dict:
    r = list_desktop_credentials(client, headers)
    assert r.status_code == 200, r.text
    return next(i for i in r.json()["items"] if i["id"] == cid)


def _token(client, headers, cid, **body):
    return client.post(
        f"{DESKTOP_CREDENTIALS_URL}/{cid}/access-token", headers=headers, json=body
    )


def _materialize(client, headers, ids):
    return client.post(MATERIALIZE, headers=headers, json={"credential_ids": ids})


def _refused(body) -> dict[str, str]:
    return {r["id"]: r["reason"] for r in body["refused"]}


def _share_with_consent(client, owner_h, cid, recipient, *, consent=True):
    set_credential_sharing(client, owner_h, cid, True)
    update_credential(client, owner_h, cid, allow_local_use=consent)
    share_credential_via_api(client, owner_h, cid, recipient["email"])


# ── C1: create / reconnect ───────────────────────────────────────────────────


def test_create_returns_authorization_url_default_name_and_local_use(
    client: TestClient,
) -> None:
    """
    1. Create gdrive_oauth_readonly → 201 with the offline/consent URL, the type's
       scopes and a desktop.-prefixed state; expires_at is in the future
    2. Default name "Google Drive (read-only)", allow_local_use, Default workspace
    3. The audit event is recorded with created=true; no tokens in the response
    """
    _, uh = create_random_user_with_headers(client)
    dh, client_id = desktop_session(client, uh)

    r = _create(client, dh, "gdrive_oauth_readonly")
    assert r.status_code == 201, r.text
    body = r.json()
    q = query_of(body["authorization_url"])
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["state"][0].startswith("desktop.")
    assert q["scope"][0].split() == [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid",
    ]
    assert body["expires_at"] > time.time()

    listed = client.get(f"{API}/credentials/", headers=uh).json()["data"]
    cred = next(c for c in listed if c["id"] == body["credential_id"])
    assert cred["name"] == "Google Drive (read-only)"
    assert cred["type"] == "gdrive_oauth_readonly"
    assert cred["allow_local_use"] is True
    assert cred["user_workspace_id"] is None

    events = events_of_type(client, uh, "CREDENTIAL_OAUTH_AUTHORIZE_LOCAL")
    assert len(events) == 1
    d = events[0]["details"]
    assert d["credential_id"] == body["credential_id"]
    assert d["credential_type"] == "gdrive_oauth_readonly"
    assert d["created"] is True
    assert "state" not in str(d)


def test_create_with_explicit_name_and_default_names_per_type(client: TestClient) -> None:
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    r = _create(client, dh, "gmail_oauth", name="Work mail")
    assert r.status_code == 201, r.text
    r2 = _create(client, dh, "gcalendar_oauth")
    names = {
        c["id"]: c["name"] for c in client.get(f"{API}/credentials/", headers=uh).json()["data"]
    }
    assert names[r.json()["credential_id"]] == "Work mail"
    assert names[r2.json()["credential_id"]] == "Google Calendar"


def test_create_whitespace_name_falls_back_to_default_and_name_is_stripped(
    client: TestClient,
) -> None:
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    blank = _create(client, dh, "gdrive_oauth", name="   ")
    assert blank.status_code == 201, blank.text
    padded = _create(client, dh, "gmeet_oauth", name="  Team meet  ")
    assert padded.status_code == 201, padded.text
    names = {
        c["id"]: c["name"] for c in client.get(f"{API}/credentials/", headers=uh).json()["data"]
    }
    assert names[blank.json()["credential_id"]] == "Google Drive"
    assert names[padded.json()["credential_id"]] == "Team meet"


def test_create_reuses_the_pending_credential_of_the_same_type(
    client: TestClient,
) -> None:
    """
    1. A second create of the same type while the first is still pending
       returns the same credential (renamed when a name is given), audited
       with created=false; only one credential exists
    2. A different type gets its own credential
    3. Once the pending one is authorized, the next create makes a new one
    """
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)

    first = _create(client, dh, "gmail_oauth")
    assert first.status_code == 201, first.text
    cid = first.json()["credential_id"]
    again = _create(client, dh, "gmail_oauth", name="Personal mail")
    assert again.status_code == 201, again.text
    assert again.json()["credential_id"] == cid
    assert state_from_url(again.json()["authorization_url"]) != state_from_url(
        first.json()["authorization_url"]
    )
    listed = client.get(f"{API}/credentials/", headers=uh).json()["data"]
    assert [c["id"] for c in listed if c["type"] == "gmail_oauth"] == [cid]
    assert next(c for c in listed if c["id"] == cid)["name"] == "Personal mail"
    events = events_of_type(client, uh, "CREDENTIAL_OAUTH_AUTHORIZE_LOCAL")
    assert sorted(e["details"]["created"] for e in events) == [False, True]

    other = _create(client, dh, "gmail_oauth_readonly")
    assert other.json()["credential_id"] != cid

    cb = complete_oauth_callback(client, state_from_url(again.json()["authorization_url"]))
    assert cb.status_code == 200, cb.text
    fresh = _create(client, dh, "gmail_oauth")
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["credential_id"] != cid


def test_create_refusals_non_google_type_and_unconfigured_google(client: TestClient) -> None:
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)

    r = _create(client, dh, "api_token")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "unsupported_type"

    with google_oauth_unconfigured():
        r = client.post(OAUTH_URL, headers=dh, json={"type": "gmail_oauth"})
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "oauth_not_configured"
    assert client.get(f"{API}/credentials/", headers=uh).json()["data"] == []


def test_reconnect_is_owner_only(client: TestClient) -> None:
    owner, oh = create_random_user_with_headers(client)
    recipient, rh = create_random_user_with_headers(client)
    _, sh = create_random_user_with_headers(client)
    odh, _ = desktop_session(client, oh)
    rdh, _ = desktop_session(client, rh)
    sdh, _ = desktop_session(client, sh)
    cid = _ready(client, odh)
    _share_with_consent(client, oh, cid, recipient)

    url = f"{DESKTOP_CREDENTIALS_URL}/{cid}/oauth/authorize"
    with google_oauth_configured():
        r = client.post(url, headers=odh)
        assert r.status_code == 200, r.text
        assert r.json()["credential_id"] == cid
        assert state_from_url(r.json()["authorization_url"]).startswith("desktop.")
        for h in (rdh, sdh):
            denied = client.post(url, headers=h)
            assert denied.status_code == 404, denied.text
            assert denied.json()["detail"]["code"] == "credential_not_found"
        missing = client.post(
            f"{DESKTOP_CREDENTIALS_URL}/{uuid.uuid4()}/oauth/authorize", headers=odh
        )
        assert missing.status_code == 404

    events = events_of_type(client, oh, "CREDENTIAL_OAUTH_AUTHORIZE_LOCAL")
    assert [e["details"]["created"] for e in events].count(False) == 1


def test_reconnect_non_google_type_and_unconfigured(client: TestClient) -> None:
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    plain = create_random_credential(client, uh, credential_type="api_token")
    with google_oauth_configured():
        r = client.post(f"{DESKTOP_CREDENTIALS_URL}/{plain['id']}/oauth/authorize", headers=dh)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "unsupported_type"

    cid = _ready(client, dh)
    with google_oauth_unconfigured():
        r = client.post(f"{DESKTOP_CREDENTIALS_URL}/{cid}/oauth/authorize", headers=dh)
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "oauth_not_configured"


def test_oauth_routes_are_desktop_only_and_rate_limited(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Browser JWT → 403; CLI-exchanged Desktop session → 403
    2. Revoked client → 401
    3. The 11th create within a minute → 429 with Retry-After
    """
    user, uh = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    with google_oauth_configured():
        assert client.post(OAUTH_URL, headers=uh, json={"type": "gmail_oauth"}).status_code == 403
        account_jwt, _ = bootstrap_account_token(client, uh, machine_name="oauth-gate")
        issued = exchange_for_desktop_token(
            client, account_cli_headers(account_jwt), device_name="oauth-exchanged"
        )
        xh = {"Authorization": f"Bearer {issued['access_token']}"}
        assert client.post(OAUTH_URL, headers=xh, json={"type": "gmail_oauth"}).status_code == 403

    rdh, rid = desktop_session(client, uh)
    revoke_desktop_client(client, uh, rid)
    assert _create(client, rdh).status_code == 401

    dh, _ = desktop_session(client, uh)
    for _i in range(10):
        assert _create(client, dh).status_code == 201
    r = _create(client, dh)
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) >= 1


# ── C2/C3 backend: callback ──────────────────────────────────────────────────


def test_desktop_started_state_completes_via_callback_with_desktop_origin(
    client: TestClient,
) -> None:
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    r = _create(client, dh)
    state = state_from_url(r.json()["authorization_url"])
    cb = complete_oauth_callback(client, state, email="desk@example.com")
    assert cb.status_code == 200, cb.text
    assert cb.json()["origin"] == "desktop"
    assert cb.json()["credential_id"] == r.json()["credential_id"]
    assert complete_oauth_callback(client, state).status_code == 400


def test_sessionless_callback_succeeds_and_names_the_owner_account(
    client: TestClient,
) -> None:
    """The normal desktop case: the system browser has no Cinna session (or a
    stale token, treated the same). The response names the owning account by
    masked email so a user who completed someone else's link notices."""
    owner, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    local, domain = owner["email"].split("@")

    r = _create(client, dh)
    cb = complete_oauth_callback(client, state_from_url(r.json()["authorization_url"]))
    assert cb.status_code == 200, cb.text
    assert cb.json()["owner_email_masked"] == f"{local[:2]}***@{domain}"
    assert owner["email"] not in cb.text

    r = _create(client, dh, "gdrive_oauth")
    cb = complete_oauth_callback(
        client,
        state_from_url(r.json()["authorization_url"]),
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert cb.status_code == 200, cb.text


def test_callback_with_the_owners_session_succeeds(client: TestClient) -> None:
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    r = _create(client, dh)
    cb = complete_oauth_callback(
        client, state_from_url(r.json()["authorization_url"]), headers=uh
    )
    assert cb.status_code == 200, cb.text
    assert cb.json()["credential_id"] == r.json()["credential_id"]


def test_callback_with_another_accounts_session_is_rejected_and_burns_the_state(
    client: TestClient,
) -> None:
    """
    Consent phishing: the attacker sends their desktop authorization link to a
    signed-in victim.
    1. The victim's browser completes it → 403 that names no account; the
       attacker's credential stays unauthorized
    2. The state is consumed: replaying it, even sessionless, → 400
    """
    attacker, ah = create_random_user_with_headers(client)
    _, vh = create_random_user_with_headers(client)
    adh, _ = desktop_session(client, ah)
    r = _create(client, adh)
    cid = r.json()["credential_id"]
    state = state_from_url(r.json()["authorization_url"])

    cb = complete_oauth_callback(client, state, email="victim@example.com", headers=vh)
    assert cb.status_code == 403, cb.text
    assert attacker["email"] not in cb.text
    assert attacker["email"].split("@")[0][:2] + "***" not in cb.text
    assert _item(client, adh, cid)["oauth_status"] == "pending_authorization"

    assert complete_oauth_callback(client, state).status_code == 400
    assert _item(client, adh, cid)["oauth_status"] == "pending_authorization"


# ── C4: list ─────────────────────────────────────────────────────────────────


def test_list_oauth_status_granted_email_and_etag_transitions(
    client: TestClient, google
) -> None:
    """
    1. After create: pending_authorization, no granted email
    2. After callback: ready + email; ETag changed; If-None-Match with it → 304
    3. A refresh rejected with invalid_grant → reauth_required; ETag changed
    4. Reconnect + callback → ready again
    5. No secret fields anywhere in the list
    """
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    r = _create(client, dh)
    cid = r.json()["credential_id"]

    first = list_desktop_credentials(client, dh)
    item = next(i for i in first.json()["items"] if i["id"] == cid)
    assert item["oauth_status"] == "pending_authorization"
    assert item["granted_user_email"] is None
    etag_pending = first.headers["etag"]

    complete_oauth_callback(
        client, state_from_url(r.json()["authorization_url"]), email=EMAIL, expires_in=60
    )
    second = list_desktop_credentials(client, dh)
    item = next(i for i in second.json()["items"] if i["id"] == cid)
    assert item["oauth_status"] == "ready"
    assert item["granted_user_email"] == EMAIL
    etag_ready = second.headers["etag"]
    assert etag_ready != etag_pending
    assert list_desktop_credentials(client, dh, etag=etag_ready).status_code == 304

    boom = AsyncMock(side_effect=OAuthRefreshError("reauth_required", "rejected"))
    with patch(EXCHANGE_REFRESH, new=boom):
        assert _token(client, dh, cid).status_code == 409
    third = list_desktop_credentials(client, dh)
    assert next(i for i in third.json()["items"] if i["id"] == cid)["oauth_status"] == "reauth_required"
    etag_reauth = third.headers["etag"]
    assert etag_reauth != etag_ready
    assert list_desktop_credentials(client, dh, etag=etag_ready).status_code == 200

    with google_oauth_configured():
        re = client.post(f"{DESKTOP_CREDENTIALS_URL}/{cid}/oauth/authorize", headers=dh)
    complete_oauth_callback(client, state_from_url(re.json()["authorization_url"]), email=EMAIL)
    assert _item(client, dh, cid)["oauth_status"] == "ready"

    for text in (first.text, second.text, third.text):
        assert ACCESS not in text and REFRESH not in text
        assert "refresh_token" not in text and "access_token" not in text


def test_non_oauth_items_carry_no_oauth_fields(client: TestClient) -> None:
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    plain = create_random_credential(client, uh, credential_type="api_token", credential_data={
        "api_token_type": "bearer", "api_token": "plain-secret",
    })
    item = _item(client, dh, plain["id"])
    assert item["oauth_status"] is None and item["granted_user_email"] is None


# ── C4: materialize ──────────────────────────────────────────────────────────


def test_materialize_refreshes_and_whitelists_fields(client: TestClient, google) -> None:
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    cid = _ready(client, dh, expires_in=60)  # inside the 600 s materialize window

    r = _materialize(client, dh, [cid])
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["refused"] == []
    google.assert_awaited_once_with(REFRESH)
    entry = next(i for i in body["items"] if i["id"] == cid)
    allowed = {
        "access_token", "token_type", "expires_at", "scope",
        "granted_user_email", "granted_user_name",
    }
    data = entry["entry"]["credential_data"]
    assert data["access_token"] == NEW_ACCESS
    assert set(data) <= allowed
    for forbidden in ("refresh_token", "refresh_error", "granted_at", REFRESH):
        assert forbidden not in r.text


def test_materialize_typed_refusals(client: TestClient) -> None:
    """
    1. Created, never authorized → pending_authorization
    2. Rejected grant → reauthorization_required
    3. Expired token + provider error → token_unavailable
    """
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    pending = _create(client, dh).json()["credential_id"]
    reauth = _ready(client, dh, expires_in=60)
    unavailable = _ready(client, dh, expires_in=-30)

    with google_oauth_configured():
        with patch(EXCHANGE_REFRESH, new=AsyncMock(side_effect=OAuthRefreshError("reauth_required", "no"))):
            assert _token(client, dh, reauth).status_code == 409
        with patch(EXCHANGE_REFRESH, new=AsyncMock(side_effect=OAuthRefreshError("provider_error", "503"))):
            r = _materialize(client, dh, [pending, reauth, unavailable])
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []
    assert _refused(r.json()) == {
        pending: "pending_authorization",
        reauth: "reauthorization_required",
        unavailable: "token_unavailable",
    }
    assert ACCESS not in r.text and REFRESH not in r.text


def test_shared_oauth_credential_needs_owner_consent(client: TestClient, google) -> None:
    owner, oh = create_random_user_with_headers(client)
    recipient, rh = create_random_user_with_headers(client)
    odh, _ = desktop_session(client, oh)
    rdh, _ = desktop_session(client, rh)
    cid = _ready(client, odh)
    _share_with_consent(client, oh, cid, recipient, consent=False)

    item = _item(client, rdh, cid)
    assert item["relation"] == "shared"
    assert item["local_use_allowed"] is False
    assert item["granted_user_email"] is None
    assert _refused(_materialize(client, rdh, [cid]).json()) == {cid: "local_use_not_allowed"}

    update_credential(client, oh, cid, allow_local_use=True)
    item = _item(client, rdh, cid)
    assert item["local_use_allowed"] is True
    assert item["granted_user_email"] == EMAIL
    r = _materialize(client, rdh, [cid])
    assert r.json()["refused"] == []
    assert [i["id"] for i in r.json()["items"]] == [cid]


# ── C5: access-token ─────────────────────────────────────────────────────────


def test_token_valid_then_near_expiry_refreshes(client: TestClient, google) -> None:
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    valid = _ready(client, dh, expires_in=10_000)
    r = _token(client, dh, valid)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["access_token"] == ACCESS
    assert r.json()["refreshed"] is False and r.json()["token_type"] == "Bearer"
    google.assert_not_called()
    assert REFRESH not in r.text

    near = _ready(client, dh, expires_in=60)
    r = _token(client, dh, near)
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] == NEW_ACCESS and r.json()["refreshed"] is True
    google.assert_awaited_once_with(REFRESH)
    assert _token(client, dh, near).json()["refreshed"] is False
    assert google.await_count == 1


def test_token_failure_mapping(client: TestClient, google) -> None:
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)

    # invalid_grant → 409, second call skips Google
    cid = _ready(client, dh, expires_in=60)
    boom = AsyncMock(side_effect=OAuthRefreshError("reauth_required", "rejected"))
    with patch(EXCHANGE_REFRESH, new=boom):
        r1, r2 = _token(client, dh, cid), _token(client, dh, cid)
    assert r1.status_code == r2.status_code == 409
    assert r1.json()["detail"]["code"] == "reauthorization_required"
    assert boom.await_count == 1
    assert REFRESH not in r1.text

    # provider error + expired token → 502 + Retry-After
    expired = _ready(client, dh, expires_in=-30)
    boom = AsyncMock(side_effect=OAuthRefreshError("provider_error", "503"))
    with patch(EXCHANGE_REFRESH, new=boom):
        r = _token(client, dh, expired)
    assert r.status_code == 502, r.text
    assert r.json()["detail"]["code"] == "provider_error"
    assert int(r.headers["Retry-After"]) >= 1

    # busy advisory lock + expired token → 502 refresh_in_progress
    from app.services.credentials import oauth_refresh_service as ors

    busy = _ready(client, dh, expires_in=-30)
    with (
        patch.object(ors.OAuthRefreshService, "_try_advisory_lock", return_value=False),
        patch.object(ors, "LOCK_WAIT_TIMEOUT_SECONDS", 0.2),
        patch.object(ors, "LOCK_POLL_INTERVAL_SECONDS", 0.05),
    ):
        r = _token(client, dh, busy)
    assert r.status_code == 502, r.text
    assert r.json()["detail"]["code"] == "refresh_in_progress"
    assert r.headers["Retry-After"] == "1"

    # Google not configured + expired token → 503
    nocfg = _ready(client, dh, expires_in=-30)
    with google_oauth_unconfigured():
        r = _token(client, dh, nocfg)
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "oauth_not_configured"

    # non-OAuth type → 422
    plain = create_random_credential(client, uh, credential_type="api_token", credential_data={
        "api_token_type": "bearer", "api_token": "plain-secret",
    })
    r = _token(client, dh, plain["id"])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "not_refreshable"


def test_token_access_rules(
    client: TestClient, google, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Stranger, revoked share and random id → 404 credential_not_found
    2. Browser JWT → 403; CLI-exchanged Desktop session → 403
    3. A shared user without consent → 404; with allow_local_use → 200
    """
    owner, oh = create_random_user_with_headers(client)
    recipient, rh = create_random_user_with_headers(client)
    _, sh = create_random_user_with_headers(client)
    odh, _ = desktop_session(client, oh)
    rdh, _ = desktop_session(client, rh)
    sdh, _ = desktop_session(client, sh)
    promote_to_developer(client, superuser_token_headers, owner["id"])
    # Near expiry: the recipient's first token is a refresh, which is audited.
    cid = _ready(client, odh, expires_in=60)

    for h in (sdh, rdh):
        r = _token(client, h, cid)
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "credential_not_found"
    assert _token(client, odh, str(uuid.uuid4())).status_code == 404

    assert _token(client, oh, cid).status_code == 403
    account_jwt, _ = bootstrap_account_token(client, oh, machine_name="tok-gate")
    issued = exchange_for_desktop_token(
        client, account_cli_headers(account_jwt), device_name="tok-exchanged"
    )
    xh = {"Authorization": f"Bearer {issued['access_token']}"}
    assert _token(client, xh, cid).status_code == 403

    _share_with_consent(client, oh, cid, recipient, consent=False)
    assert _token(client, rdh, cid).status_code == 404
    update_credential(client, oh, cid, allow_local_use=True)
    r = _token(client, rdh, cid)
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] == NEW_ACCESS and r.json()["refreshed"] is True

    # Owner-side copy of the audit event for the recipient's refresh.
    owner_events = events_of_type(client, oh, "CREDENTIAL_TOKEN_ISSUED_LOCAL")
    assert any(e["details"].get("recipient_email") == recipient["email"] for e in owner_events)
    assert NEW_ACCESS not in str(owner_events) and REFRESH not in str(owner_events)


def test_token_revoked_client_is_401_refreshes_are_audited_and_rate_limited(
    client: TestClient, google
) -> None:
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    cid = _ready(client, dh, expires_in=10_000)

    # A still-valid token is handed back without a refresh: no audit event.
    r = _token(client, dh, cid)
    assert r.status_code == 200 and r.json()["refreshed"] is False
    assert events_of_type(client, uh, "CREDENTIAL_TOKEN_ISSUED_LOCAL") == []

    # A refresh is audited once; the cached token after it is not.
    near = _ready(client, dh, expires_in=60)
    assert _token(client, dh, near).json()["refreshed"] is True
    assert _token(client, dh, near).json()["refreshed"] is False
    events = events_of_type(client, uh, "CREDENTIAL_TOKEN_ISSUED_LOCAL")
    assert len(events) == 1
    assert events[0]["details"]["credential_id"] == near
    assert events[0]["details"]["refreshed"] is True
    assert NEW_ACCESS not in str(events) and REFRESH not in str(events)

    for _i in range(117):
        assert _token(client, dh, cid).status_code == 200
    r = _token(client, dh, cid)
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) >= 1

    rdh, rid = desktop_session(client, uh)
    revoke_desktop_client(client, uh, rid)
    assert _token(client, rdh, cid).status_code == 401
