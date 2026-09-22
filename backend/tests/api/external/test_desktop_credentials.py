"""Desktop credential delivery — ``/external/credentials`` (list + materialize).

Feature doc: docs/application/desktop_credentials/README.md.

All state is set up through the API: credentials via ``/credentials``, shares
via ``/credentials/{id}/shares``, consent via ``PUT /credentials/{id}``
(``allow_local_use``), Desktop sessions via the real PKCE consent flow, and
CLI-exchanged Desktop sessions via ``/cli/account/desktop-token``. Audit rows
are read back through the self-scoped ``/security-events/`` feed.

Only locally compatible types (email IMAP/SMTP, Odoo, API token, Google
service account) are ever listed or delivered; the share/consent lifecycle is
exercised with those.
"""
import json
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent, update_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    create_bundle_credential,
    install_bundle,
    link_bundle_credential_to_agent,
    make_bundle_public,
    make_user_and_headers,
    publish_bundle,
)
from tests.utils.cli import (
    account_cli_headers,
    bootstrap_account_token,
    exchange_for_desktop_token,
)
from tests.utils.credential import (
    get_credential,
    get_credential_with_data,
    link_credential_to_agent,
    real_credentials_json,
    share_credential_via_api,
    update_credential,
)
from tests.utils.desktop_auth import revoke_desktop_client
from tests.utils.desktop_credentials import (
    DESKTOP_CREDENTIALS_URL,
    MATERIALIZE_URL,
    create_credential_via_api,
    desktop_session,
    list_desktop_credentials,
    materialize_desktop_credentials,
    revoke_credential_share,
)
from tests.utils.security_event import events_of_type
from tests.utils.skill_catalog import (
    install_skill,
    make_agent_with_env,
    make_developer,
    patched_skill_storage,
    publish_skill,
    write_skill_with_credentials,
)
from tests.utils.ssh_key_credential import create_ssh_key_credential_generate
from tests.utils.user import create_random_user_with_headers, promote_to_developer

API = settings.API_V1_STR
EVENT = "CREDENTIAL_MATERIALIZED_LOCAL"


def _api_token_data(token: str) -> dict:
    return {"api_token_type": "bearer", "api_token": token}


def _oauth_data(access: str, refresh: str, expires_at: int) -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_at": expires_at,
        "scope": "https://www.googleapis.com/auth/gmail.modify",
    }


def _refused(result: dict) -> dict[str, str]:
    return {r["id"]: r["reason"] for r in result["refused"]}


def _items(result: dict) -> dict[str, dict]:
    return {i["id"]: i for i in result["items"]}


def _post_materialize(client: TestClient, headers: dict[str, str], ids: list[str]):
    return client.post(MATERIALIZE_URL, headers=headers, json={"credential_ids": ids})


# ── Scenario 1: access rules across the share lifecycle ──────────────────────


def test_owner_recipient_stranger_and_superuser_access_across_share_lifecycle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Owner creates a shareable api_token credential and shares it (consent off)
    2. A browser JWT cannot materialize (403)
    3. Owner delivers; include_current_user toggles the current_user block
    4. Recipient is refused local_use_not_allowed; stranger and superuser no_access
    5. Owner grants consent → recipient delivers; superuser still no_access
    6. Owner rotates the secret → recipient receives the new value, new revision
    7. Owner withdraws consent → local_use_not_allowed again; re-grant
    8. Owner revokes the share → no_access; re-share → delivered again
    9. Owner turns allow_sharing off (revokes every share) → no_access
   10. Owner deletes the credential → not_found for everyone
    """
    owner, oh = create_random_user_with_headers(client)
    recipient, rh = create_random_user_with_headers(client)
    _, sh = create_random_user_with_headers(client)
    od, _ = desktop_session(client, oh)
    rd, _ = desktop_session(client, rh)
    sd, _ = desktop_session(client, sh)
    ad, _ = desktop_session(client, superuser_token_headers)

    # ── Phase 1: owned + shared credential, consent off by default ────────
    cred = create_credential_via_api(
        client,
        oh,
        "api_token",
        _api_token_data("desktop-fixture-secret-123"),
        allow_sharing=True,
        service_uri="fixture-slot",
    )
    cid = cred["id"]
    assert cred["allow_local_use"] is False
    share = share_credential_via_api(client, oh, cid, recipient["email"])

    # ── Phase 2: browser JWT is not a Desktop session ─────────────────────
    r = _post_materialize(client, oh, [cid])
    assert r.status_code == 403, r.text

    # ── Phase 3: owner delivers; current_user only on request ─────────────
    delivered = materialize_desktop_credentials(client, od, [cid], include_current_user=True)
    assert delivered["refused"] == []
    item = delivered["items"][0]
    assert item["id"] == cid
    assert item["entry"]["credential_data"] == {
        "http_header_name": "Authorization",
        "http_header_value": "Bearer desktop-fixture-secret-123",
        "service_uri": "fixture-slot",
    }
    assert item["ssh_key"] is None
    assert item["service_account_file"] is None
    assert delivered["current_user"]["type"] == "current_user"
    assert delivered["owner_identity"] is None
    first_revision = item["revision"]

    without_user = materialize_desktop_credentials(client, od, [cid])
    assert without_user["current_user"] is None
    assert len(without_user["items"]) == 1

    # ── Phase 4: recipient without consent, stranger, superuser ───────────
    assert _refused(materialize_desktop_credentials(client, rd, [cid])) == {
        cid: "local_use_not_allowed"
    }
    assert _refused(materialize_desktop_credentials(client, sd, [cid])) == {cid: "no_access"}
    assert _refused(materialize_desktop_credentials(client, ad, [cid])) == {cid: "no_access"}

    # ── Phase 5: consent on → recipient delivers; superuser still refused ─
    updated = update_credential(client, oh, cid, allow_local_use=True)
    assert updated["allow_local_use"] is True
    assert get_credential_with_data(client, oh, cid)["allow_local_use"] is True
    allowed = materialize_desktop_credentials(client, rd, [cid])
    assert allowed["refused"] == []
    assert (
        allowed["items"][0]["entry"]["credential_data"]["http_header_value"]
        == "Bearer desktop-fixture-secret-123"
    )
    assert allowed["items"][0]["revision"] != first_revision
    assert _refused(materialize_desktop_credentials(client, ad, [cid])) == {cid: "no_access"}
    assert _refused(materialize_desktop_credentials(client, sd, [cid])) == {cid: "no_access"}

    # ── Phase 6: rotation reaches the recipient with a new revision ───────
    update_credential(
        client, oh, cid, credential_data=_api_token_data("desktop-rotated-secret-456")
    )
    rotated = materialize_desktop_credentials(client, rd, [cid])
    assert (
        rotated["items"][0]["entry"]["credential_data"]["http_header_value"]
        == "Bearer desktop-rotated-secret-456"
    )
    assert rotated["items"][0]["revision"] != allowed["items"][0]["revision"]

    # ── Phase 7: consent withdrawn → refused; re-grant ────────────────────
    update_credential(client, oh, cid, allow_local_use=False)
    assert _refused(materialize_desktop_credentials(client, rd, [cid])) == {
        cid: "local_use_not_allowed"
    }
    # The owner is never subject to their own consent flag.
    assert materialize_desktop_credentials(client, od, [cid])["refused"] == []
    update_credential(client, oh, cid, allow_local_use=True)

    # ── Phase 8: share revoked → no_access; re-share restores delivery ────
    revoke_credential_share(client, oh, cid, share["id"])
    assert _refused(materialize_desktop_credentials(client, rd, [cid])) == {cid: "no_access"}
    share_credential_via_api(client, oh, cid, recipient["email"])
    assert materialize_desktop_credentials(client, rd, [cid])["refused"] == []

    # ── Phase 9: allow_sharing off revokes every share ────────────────────
    update_credential(client, oh, cid, allow_sharing=False)
    assert _refused(materialize_desktop_credentials(client, rd, [cid])) == {cid: "no_access"}
    assert materialize_desktop_credentials(client, od, [cid])["refused"] == []

    # ── Phase 10: deleted credential → not_found; unknown id → not_found ──
    r = client.delete(f"{API}/credentials/{cid}", headers=oh)
    assert r.status_code == 200, r.text
    assert _refused(materialize_desktop_credentials(client, od, [cid])) == {cid: "not_found"}
    assert _refused(materialize_desktop_credentials(client, rd, [cid])) == {cid: "not_found"}
    ghost = str(uuid.uuid4())
    assert _refused(materialize_desktop_credentials(client, od, [ghost])) == {ghost: "not_found"}


# ── Scenario 2: list metadata, visibility and ETag ───────────────────────────


def test_list_metadata_visibility_and_etag_changes(client: TestClient) -> None:
    """
    1. Owner has an api_token (shared, consent off) and an mcp_provider
    2. Owner list: api_token owned/local_use_allowed; mcp_provider excluded; no secrets
    3. Recipient list: shared entry with owner_email and local_use_allowed=false
    4. Matching If-None-Match → 304 with the same ETag; unauthenticated → 401
    5. Rotation changes the ETag (owner and recipient)
    6. Consent toggle changes the ETag and local_use_allowed
    7. Share removal changes the recipient ETag and hides the entry
    """
    owner, oh = create_random_user_with_headers(client)
    recipient, rh = create_random_user_with_headers(client)

    # ── Phase 1: fixtures ─────────────────────────────────────────────────
    cred = create_credential_via_api(
        client,
        oh,
        "api_token",
        _api_token_data("list-secret-never-listed"),
        allow_sharing=True,
        service_uri="list-slot",
        notes="list notes",
    )
    cid = cred["id"]
    mcp = create_credential_via_api(
        client,
        oh,
        "mcp_provider",
        {"endpoint_url": "https://mcp.example.com/mcp", "auth_mode": "fixed_token"},
        mcp_auth_mode="fixed_token",
    )
    share = share_credential_via_api(client, oh, cid, recipient["email"])

    # ── Phase 2: owner list ───────────────────────────────────────────────
    r = list_desktop_credentials(client, oh)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "private, no-cache"
    assert "list-secret-never-listed" not in r.text
    owner_items = {i["id"]: i for i in r.json()["items"]}
    assert mcp["id"] not in owner_items
    mine = owner_items[cid]
    assert mine["relation"] == "owned"
    assert mine["local_use_allowed"] is True
    assert mine["owner_email"] == owner["email"]
    assert mine["service_uri"] == "list-slot"
    assert mine["notes"] == "list notes"
    assert mine["status"] == "complete"
    assert mine["is_placeholder"] is False
    assert mine["type"] == "api_token"
    owner_etag = r.headers["etag"]

    # ── Phase 3: recipient sees a shared entry without consent ────────────
    r = list_desktop_credentials(client, rh)
    assert r.status_code == 200
    assert "list-secret-never-listed" not in r.text
    shared = {i["id"]: i for i in r.json()["items"]}
    assert set(shared) == {cid}
    assert shared[cid]["relation"] == "shared"
    assert shared[cid]["owner_email"] == owner["email"]
    assert shared[cid]["local_use_allowed"] is False
    assert shared[cid]["revision"] == mine["revision"]
    recipient_etag = r.headers["etag"]

    # ── Phase 4: conditional GET and auth guard ───────────────────────────
    r = list_desktop_credentials(client, rh, etag=recipient_etag)
    assert r.status_code == 304
    assert r.headers["etag"] == recipient_etag
    assert r.content == b""
    assert list_desktop_credentials(client, oh, etag='"stale"').status_code == 200
    assert client.get(DESKTOP_CREDENTIALS_URL).status_code == 401

    # ── Phase 5: rotation changes both ETags ──────────────────────────────
    update_credential(client, oh, cid, credential_data=_api_token_data("list-rotated"))
    r = list_desktop_credentials(client, oh, etag=owner_etag)
    assert r.status_code == 200
    assert r.headers["etag"] != owner_etag
    r = list_desktop_credentials(client, rh, etag=recipient_etag)
    assert r.status_code == 200
    assert r.headers["etag"] != recipient_etag
    after_rotation = r.headers["etag"]

    # ── Phase 6: consent toggle ───────────────────────────────────────────
    update_credential(client, oh, cid, allow_local_use=True)
    r = list_desktop_credentials(client, rh, etag=after_rotation)
    assert r.status_code == 200
    assert r.headers["etag"] != after_rotation
    assert r.json()["items"][0]["local_use_allowed"] is True
    after_consent = r.headers["etag"]
    assert list_desktop_credentials(client, rh, etag=after_consent).status_code == 304

    # ── Phase 7: share removal ────────────────────────────────────────────
    revoke_credential_share(client, oh, cid, share["id"])
    r = list_desktop_credentials(client, rh, etag=after_consent)
    assert r.status_code == 200
    assert r.headers["etag"] != after_consent
    assert r.json()["items"] == []


# ── Scenario 3: which tokens may export secrets ──────────────────────────────


def test_materialize_token_gating(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Non-desktop (browser) JWT → 403
    2. Account CLI token → refused
    3. Desktop session exchanged from an account CLI token → 403 on materialize,
       while the metadata list stays readable
    4. An interactive Desktop session delivers; once its client is revoked → 401
    """
    user, uh = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    cred = create_credential_via_api(client, uh, "api_token", _api_token_data("gate-secret"))
    cid = cred["id"]

    # ── Phase 1: browser JWT ──────────────────────────────────────────────
    r = _post_materialize(client, uh, [cid])
    assert r.status_code == 403
    assert "gate-secret" not in r.text

    # ── Phase 2: account CLI token ────────────────────────────────────────
    account_jwt, _ = bootstrap_account_token(client, uh, machine_name="gate-machine")
    acc_h = account_cli_headers(account_jwt)
    # The account JWT is not a user session at all: user resolution rejects it
    # before the Desktop gate is reached.
    r = _post_materialize(client, acc_h, [cid])
    assert r.status_code == 404, r.text
    assert "gate-secret" not in r.text

    # ── Phase 3: CLI-exchanged Desktop session ────────────────────────────
    issued = exchange_for_desktop_token(client, acc_h, device_name="exchanged-desktop")
    exchanged_h = {"Authorization": f"Bearer {issued['access_token']}"}
    r = _post_materialize(client, exchanged_h, [cid])
    assert r.status_code == 403, r.text
    assert "gate-secret" not in r.text
    listed = list_desktop_credentials(client, exchanged_h)
    assert listed.status_code == 200
    assert [i["id"] for i in listed.json()["items"]] == [cid]

    # ── Phase 4: interactive Desktop delivers until revoked ───────────────
    dh, client_id = desktop_session(client, uh)
    assert materialize_desktop_credentials(client, dh, [cid])["refused"] == []
    revoke_desktop_client(client, uh, client_id)
    r = _post_materialize(client, dh, [cid])
    assert r.status_code == 401, r.text


# ── Scenario 4: only locally compatible types reach a Desktop ───────────────

OAUTH_TYPES = (
    "gmail_oauth",
    "gmail_oauth_readonly",
    "gdrive_oauth",
    "gdrive_oauth_readonly",
    "gcalendar_oauth",
    "gcalendar_oauth_readonly",
)


def test_incompatible_types_are_neither_listed_nor_delivered(client: TestClient) -> None:
    """
    1. Owner has one credential of every locally incompatible type (all six
       OAuth types, ssh_key, mcp_provider) plus one api_token
    2. The Desktop list contains only the api_token
    3. Materialize delivers the api_token and refuses every other item
       unsupported_type; no token or key material leaves Core
    4. A recipient holding a direct share + local-use consent of an OAuth
       credential gets the same: not listed, refused unsupported_type
    """
    owner, oh = create_random_user_with_headers(client)
    recipient, rh = create_random_user_with_headers(client)
    od, _ = desktop_session(client, oh)
    rd, _ = desktop_session(client, rh)
    expires = int(time.time()) + 3600

    # ── Phase 1: one credential per incompatible type + one compatible ────
    incompatible = [
        create_credential_via_api(
            client,
            oh,
            t,
            _oauth_data(f"access-{t}", f"refresh-{t}", expires),
            allow_sharing=True,
            allow_local_use=True,
        )["id"]
        for t in OAUTH_TYPES
    ]
    incompatible.append(create_ssh_key_credential_generate(client, oh, key_type="ed25519")["id"])
    incompatible.append(
        create_credential_via_api(
            client,
            oh,
            "mcp_provider",
            {"endpoint_url": "https://mcp.example.com/mcp", "auth_mode": "fixed_token"},
            mcp_auth_mode="fixed_token",
        )["id"]
    )
    good = create_credential_via_api(client, oh, "api_token", _api_token_data("compatible-1"))
    gid = good["id"]

    # ── Phase 2: the list carries only the compatible credential ──────────
    r = list_desktop_credentials(client, od)
    assert r.status_code == 200, r.text
    assert [i["id"] for i in r.json()["items"]] == [gid]

    # ── Phase 3: materialize delivers it and refuses the rest ─────────────
    r = _post_materialize(client, od, [*incompatible, gid])
    assert r.status_code == 200, r.text
    body = r.json()
    assert list(_items(body)) == [gid]
    assert _refused(body) == dict.fromkeys(incompatible, "unsupported_type")
    assert "access-" not in r.text and "refresh-" not in r.text
    assert "PRIVATE KEY" not in r.text
    assert body["owner_identity"] is None
    assert all(i["ssh_key"] is None for i in body["items"])

    # ── Phase 4: a consenting direct share of OAuth changes nothing ───────
    oauth_id = incompatible[0]
    share_credential_via_api(client, oh, oauth_id, recipient["email"])
    r = list_desktop_credentials(client, rd)
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []
    r = _post_materialize(client, rd, [oauth_id])
    assert r.status_code == 200, r.text
    assert _refused(r.json()) == {oauth_id: "unsupported_type"}
    assert r.json()["items"] == []
    assert "access-" not in r.text


def test_service_account_side_file_and_request_size_bound(client: TestClient) -> None:
    """
    1. Service account: full JSON as side file, private key not in the entry
    2. More than 50 ids → 422
    """
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)

    # ── Phase 1: service account side file ────────────────────────────────
    sa_data = {
        "type": "service_account",
        "project_id": "fixture",
        "client_email": "fixture@fixture.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nsa-private-fixture\n-----END PRIVATE KEY-----\n",
        "private_key_id": "id",
    }
    sa = create_credential_via_api(client, uh, "google_service_account", sa_data)
    assert sa["id"] in {i["id"] for i in list_desktop_credentials(client, dh).json()["items"]}
    result = materialize_desktop_credentials(client, dh, [sa["id"]])
    sa_item = _items(result)[sa["id"]]
    assert sa_item["service_account_file"] == sa_data
    assert "private_key" not in sa_item["entry"]["credential_data"]
    assert "sa-private-fixture" not in json.dumps(sa_item["entry"])

    # ── Phase 2: request size bound ───────────────────────────────────────
    r = client.post(
        MATERIALIZE_URL,
        headers=dh,
        json={"credential_ids": [str(uuid.uuid4()) for _ in range(51)]},
    )
    assert r.status_code == 422


# ── Scenario 5: OAuth edits keep server-managed tokens ───────────────────────


def test_editing_oauth_credential_keeps_server_refreshed_tokens(client: TestClient) -> None:
    """
    1. Owner has an OAuth credential holding the current (server-refreshed) tokens
    2. Owner saves from a stale edit dialog: old token values + a changed field
    3. The stored tokens are kept; the non-token field is updated
    """
    _, uh = create_random_user_with_headers(client)
    now = int(time.time())
    cred = create_credential_via_api(
        client, uh, "gmail_oauth", _oauth_data("current-access", "current-refresh", now + 3600)
    )

    stale = _oauth_data("stale-access", "stale-refresh", now - 600)
    stale["granted_user_email"] = "renamed@example.com"
    update_credential(client, uh, cred["id"], name="Renamed", credential_data=stale)

    stored = get_credential_with_data(client, uh, cred["id"])
    assert stored["name"] == "Renamed"
    data = stored["credential_data"]
    assert data["access_token"] == "current-access"
    assert data["refresh_token"] == "current-refresh"
    assert data["expires_at"] == now + 3600
    assert data["granted_user_email"] == "renamed@example.com"


# ── Scenario 6: audit trail ──────────────────────────────────────────────────


def test_materialization_audit_events_for_requester_and_owner(client: TestClient) -> None:
    """
    1. Recipient materializes a shared credential → one event in the recipient's
       feed (credential + desktop client) and one in the owner's feed
       (credential + recipient id + email)
    2. Owner materializes their own credential → exactly one more owner event,
       carrying no recipient fields
    3. A refused item is not audited
    4. No secret value appears in any event
    """
    owner, oh = create_random_user_with_headers(client)
    recipient, rh = create_random_user_with_headers(client)
    od, _ = desktop_session(client, oh)
    rd, _ = desktop_session(client, rh)
    secret = "audit-secret-value-789"
    cred = create_credential_via_api(
        client,
        oh,
        "api_token",
        _api_token_data(secret),
        allow_sharing=True,
        allow_local_use=True,
    )
    cid = cred["id"]
    share_credential_via_api(client, oh, cid, recipient["email"])
    assert events_of_type(client, oh, EVENT) == []

    # ── Phase 1: recipient copies → both feeds ────────────────────────────
    assert materialize_desktop_credentials(client, rd, [cid])["refused"] == []
    recipient_events = events_of_type(client, rh, EVENT)
    assert len(recipient_events) == 1
    # desktop_client_id is the internal client row id (not the public OAuth
    # client_id), so it is checked for shape and distinctness only.
    recipient_details = recipient_events[0]["details"]
    assert set(recipient_details) == {"credential_id", "desktop_client_id"}
    assert recipient_details["credential_id"] == cid
    uuid.UUID(recipient_details["desktop_client_id"])
    assert recipient_events[0]["severity"] == "high"
    owner_events = events_of_type(client, oh, EVENT)
    assert len(owner_events) == 1
    assert owner_events[0]["details"] == {
        "credential_id": cid,
        "recipient_user_id": recipient["id"],
        "recipient_email": recipient["email"],
    }

    # ── Phase 2: owner copies their own → one plain event ─────────────────
    assert materialize_desktop_credentials(client, od, [cid])["refused"] == []
    owner_events = events_of_type(client, oh, EVENT)
    assert len(owner_events) == 2
    own = [e for e in owner_events if "desktop_client_id" in e["details"]]
    assert len(own) == 1
    assert set(own[0]["details"]) == {"credential_id", "desktop_client_id"}
    assert own[0]["details"]["credential_id"] == cid
    assert own[0]["details"]["desktop_client_id"] != recipient_details["desktop_client_id"]
    assert len(events_of_type(client, rh, EVENT)) == 1

    # ── Phase 3: refused items are not audited ────────────────────────────
    update_credential(client, oh, cid, allow_local_use=False)
    assert _refused(materialize_desktop_credentials(client, rd, [cid])) == {
        cid: "local_use_not_allowed"
    }
    assert len(events_of_type(client, rh, EVENT)) == 1
    assert len(events_of_type(client, oh, EVENT)) == 2

    # ── Phase 4: no values in any event ───────────────────────────────────
    dumped = json.dumps(events_of_type(client, oh, EVENT) + events_of_type(client, rh, EVENT))
    assert secret not in dumped
    assert "Bearer" not in dumped


# ── Scenario 7: rate limit ───────────────────────────────────────────────────


def test_materialize_rate_limit_is_60_per_minute(client: TestClient) -> None:
    """60 calls from one user/client succeed; the 61st → 429 with Retry-After.
    A second Desktop client of the same user has its own budget."""
    _, uh = create_random_user_with_headers(client)
    dh, _ = desktop_session(client, uh)
    for _ in range(60):
        r = _post_materialize(client, dh, [])
        assert r.status_code == 200, r.text
    r = _post_materialize(client, dh, [])
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1

    other, _ = desktop_session(client, uh)
    assert _post_materialize(client, other, []).status_code == 200


# ── Scenario 8: cloud env sync and Desktop share one entry shape ────────────


def test_cloud_env_sync_and_desktop_delivery_share_api_token_shape(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    An api_token credential with a service_uri, linked to a cloud agent, reaches
    the environment as header + service_uri; the Desktop delivery returns the
    same entry.
    """
    cred = create_credential_via_api(
        client,
        superuser_token_headers,
        "api_token",
        _api_token_data("parity-token-321"),
        service_uri="parity-slot",
    )
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    assert get_agent(client, superuser_token_headers, agent["id"])["active_environment_id"]
    adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: adapter
    link_credential_to_agent(client, superuser_token_headers, agent["id"], cred["id"])

    cloud_entries = {e["id"]: e for e in real_credentials_json(adapter.credentials_set)}
    cloud = cloud_entries[cred["id"]]
    assert cloud["credential_data"] == {
        "http_header_name": "Authorization",
        "http_header_value": "Bearer parity-token-321",
        "service_uri": "parity-slot",
    }
    assert cloud["service_uri"] == "parity-slot"

    dh, _ = desktop_session(client, superuser_token_headers)
    desktop_entry = materialize_desktop_credentials(client, dh, [cred["id"]])["items"][0]["entry"]
    assert desktop_entry == cloud


# ── Scenario 9: SMTP completeness ───────────────────────────────────────────


def test_email_smtp_is_complete_without_from_email(client: TestClient) -> None:
    """An email_smtp credential with no from_email is complete (CRUD and Desktop list)."""
    _, uh = create_random_user_with_headers(client)
    cred = create_credential_via_api(
        client,
        uh,
        "email_smtp",
        {
            "host": "smtp.example.com",
            "port": 587,
            "username": "user@example.com",
            "password": "smtp-secret",
            "use_tls": True,
            "use_ssl": False,
        },
    )
    assert cred["status"] == "complete"
    assert get_credential(client, uh, cred["id"])["status"] == "complete"
    listed = {i["id"]: i for i in list_desktop_credentials(client, uh).json()["items"]}
    assert listed[cred["id"]]["status"] == "complete"


# ── Scenario 10: OpenAPI ─────────────────────────────────────────────────────


def test_openapi_names_desktop_credential_response_models(client: TestClient) -> None:
    spec = client.get(f"{API}/openapi.json").json()
    list_schema = spec["paths"][f"{API}/external/credentials"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    mat = spec["paths"][f"{API}/external/credentials/materialize"]["post"]
    mat_schema = mat["responses"]["200"]["content"]["application/json"]["schema"]
    assert list_schema == {"$ref": "#/components/schemas/DesktopCredentialList"}
    assert mat_schema == {"$ref": "#/components/schemas/DesktopCredentialMaterializeResponse"}
    assert mat["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DesktopCredentialMaterializeRequest"
    }
    reasons = spec["components"]["schemas"]["DesktopCredentialRefusal"]["properties"]["reason"]
    assert set(reasons["enum"]) == {
        "not_found",
        "no_access",
        "not_local_category",
        "local_use_not_allowed",
        "unsupported_type",
    }


# ── Scenario 11: only "mine" credentials reach a Desktop ─────────────────────


def _listed(client: TestClient, headers: dict[str, str]) -> dict[str, dict]:
    r = list_desktop_credentials(client, headers)
    assert r.status_code == 200, r.text
    return {i["id"]: i for i in r.json()["items"]}


def test_owned_automatic_agent_api_connection_is_neither_listed_nor_delivered(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. "Connect Agent API" creates an owned agent_api connection → "automatic":
       absent from the list, refused not_local_category on materialize.
    2. An external agent_api key the same owner issued is "mine" but not a
       locally compatible type: not listed, refused unsupported_type, and its
       token never appears in the response.
    """
    su = client.get(f"{API}/users/me", headers=superuser_token_headers).json()
    dh, _ = desktop_session(client, superuser_token_headers)
    agent = create_agent_via_api(client, superuser_token_headers, name="Desktop Category Producer")
    drain_tasks()
    update_agent(
        client,
        superuser_token_headers,
        agent["id"],
        agent_api_enabled=True,
        agent_api_external_access_enabled=True,
    )

    # ── Phase 1: automatic connection ─────────────────────────────────────
    r = client.post(
        f"{API}/agents/{agent['id']}/agent-api/connect",
        headers=superuser_token_headers,
        json={"read_only_override": False},
    )
    assert r.status_code == 200, r.text
    conn_id = str(r.json()["credential_id"])
    assert conn_id not in _listed(client, dh)
    result = materialize_desktop_credentials(client, dh, [conn_id])
    assert _refused(result) == {conn_id: "not_local_category"}
    assert result["items"] == []

    # ── Phase 2: external key is "mine" but never delivered ───────────────
    r = client.post(
        f"{API}/agents/{agent['id']}/agent-api/keys",
        headers=superuser_token_headers,
        json={"subject_user_id": su["id"], "read_only_override": False},
    )
    assert r.status_code == 200, r.text
    key = r.json()
    key_cred_id = str(key["credential_id"])
    listed = _listed(client, dh)
    assert key_cred_id not in listed
    assert conn_id not in listed
    result = materialize_desktop_credentials(client, dh, [key_cred_id])
    assert _refused(result) == {key_cred_id: "unsupported_type"}
    assert result["items"] == []
    assert result["owner_identity"] is None
    assert key["token"] not in json.dumps(result)


def test_bundle_install_share_is_neither_listed_nor_delivered(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A publisher's credential reaches an installer through a bundle install
       (share source "bundle_install"), with local use allowed by the owner.
    2. It is absent from the installer's Desktop list and refused
       not_local_category on materialize.
    3. The same credential shared directly with another user is listed as
       category "mine" and delivered.
    """
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Desktop Category Bundle"
    )
    drain_tasks()
    cred = create_bundle_credential(
        client, superuser_token_headers, name="desk-bundle-cred", allow_sharing=True
    )
    update_credential(client, superuser_token_headers, cred["id"], allow_local_use=True)
    link_bundle_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], cred["id"]
    )
    fresh = publish_bundle(client, superuser_token_headers, publisher_agent["id"])
    make_bundle_public(client, superuser_token_headers, fresh["bundle_uuid"])

    _, installer_headers = make_user_and_headers(client)
    install_bundle(client, installer_headers, fresh["bundle_id"])
    shared = client.get(f"{API}/credentials/shared-with-me", headers=installer_headers)
    assert next(c for c in shared.json()["data"] if c["id"] == cred["id"])[
        "source"
    ] == "bundle_install"

    idh, _ = desktop_session(client, installer_headers)
    assert cred["id"] not in _listed(client, idh)
    assert _refused(materialize_desktop_credentials(client, idh, [cred["id"]])) == {
        cred["id"]: "not_local_category"
    }

    # ── Direct share of the same credential is "mine" ─────────────────────
    direct_user, direct_headers = create_random_user_with_headers(client)
    share_credential_via_api(client, superuser_token_headers, cred["id"], direct_user["email"])
    ddh, _ = desktop_session(client, direct_headers)
    item = _listed(client, ddh)[cred["id"]]
    assert item["category"] == "mine"
    assert item["relation"] == "shared"
    assert item["local_use_allowed"] is True
    result = materialize_desktop_credentials(client, ddh, [cred["id"]])
    assert result["refused"] == []
    assert [i["id"] for i in result["items"]] == [cred["id"]]


def test_skill_install_share_is_not_listed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    tmp_path: Path,
) -> None:
    """A credential reaching the installer through a catalog skill install
    (share source "skill_install", category "automatic") is not listed for
    the installer's Desktop, and is refused not_local_category."""
    with patched_skill_storage(tmp_path / "skill-storage"):
        _, pub_headers = make_developer(client, superuser_token_headers)
        pub_agent, pub_env = make_agent_with_env(client, pub_headers, "DeskSkill-Publisher")
        cred = create_credential_via_api(
            client,
            pub_headers,
            "api_token",
            _api_token_data("skill-install-secret"),
            service_uri="slot-desk",
            allow_sharing=True,
        )
        update_credential(client, pub_headers, cred["id"], allow_local_use=True)
        link_credential_to_agent(client, pub_headers, pub_agent, cred["id"])
        write_skill_with_credentials(
            pub_env, "desk-skill", [{"slot": "slot-desk", "type": "api_token"}]
        )
        revision = publish_skill(
            client, pub_headers, pub_agent, "desk-skill", visibility="public"
        )

        _, con_headers = make_developer(client, superuser_token_headers)
        con_agent, _ = make_agent_with_env(client, con_headers, "DeskSkill-Consumer")
        adapter = EnvironmentTestAdapter()
        adapter.skills_index = {"hash": "hash-empty", "skills": [], "errors": []}
        adapter.workspace_files = {}
        patch_environment_adapter.get_adapter = lambda env: adapter
        install_skill(client, con_headers, con_agent, revision["package_id"])

    shared = client.get(f"{API}/credentials/shared-with-me", headers=con_headers)
    assert next(c for c in shared.json()["data"] if c["id"] == cred["id"])[
        "source"
    ] == "skill_install"
    cdh, _ = desktop_session(client, con_headers)
    assert cred["id"] not in _listed(client, cdh)
    assert _refused(materialize_desktop_credentials(client, cdh, [cred["id"]])) == {
        cred["id"]: "not_local_category"
    }
