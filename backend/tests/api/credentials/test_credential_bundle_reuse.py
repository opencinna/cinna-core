"""Owner switch ``allow_bundle_reuse`` and the re-sharer listing (P1, plan §2.2, §2.5).

Scenarios:
  A. Toggle validation: re-use needs sharing on, an ``agent_api`` connection
     (not an api_token), not an issued key, and the owner (a recipient gets
     404/403 and the flag is unchanged).
  B. Turning sharing off clears the flag (D2) through both ``PUT /credentials``
     and ``PATCH /credentials/{id}/sharing``; turning sharing back on does not
     restore it.
  C. ``GET /credentials/{id}/reshares``: strangers get 404, the owner and a
     recipient with no re-shares get an empty list.
  D. The shared-with-me projection exposes ``allow_bundle_reuse_effective``.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent_api_keys import make_developer, setup_producer
from tests.utils.credential import (
    create_random_credential,
    get_credential,
    set_credential_sharing,
    share_credential_via_api,
    update_credential,
)

API = settings.API_V1_STR


def _connection(client: TestClient, headers: dict[str, str], name: str) -> str:
    producer = setup_producer(client, headers, name=name)
    r = client.post(f"{API}/agents/{producer['id']}/agent-api/connect", headers=headers, json={})
    assert r.status_code == 200, r.text
    return r.json()["credential_id"]


def _put(client, headers, cid, **body):
    return client.put(f"{API}/credentials/{cid}", headers=headers, json=body)


def test_allow_bundle_reuse_toggle_validation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Default is off; re-use without sharing is rejected (400)
    2. Sharing + re-use in one update works on an agent_api connection
    3. An api_token credential cannot enable re-use (400)
    4. An issued key (identity-bound) cannot enable re-use
    5. A recipient cannot flip the owner's switch; the flag is unchanged
    """
    su = superuser_token_headers
    owner, oh = make_developer(client, su)
    cid = _connection(client, oh, "Reuse-Toggle-Producer")

    # ── Phase 1: default off, needs sharing ──────────────────────────
    assert get_credential(client, oh, cid)["allow_bundle_reuse"] is False
    r = _put(client, oh, cid, allow_bundle_reuse=True)
    assert r.status_code == 400, r.text
    assert get_credential(client, oh, cid)["allow_bundle_reuse"] is False

    # ── Phase 2: sharing + re-use together ───────────────────────────
    r = _put(client, oh, cid, allow_sharing=True, allow_bundle_reuse=True)
    assert r.status_code == 200, r.text
    assert r.json()["allow_bundle_reuse"] is True
    assert get_credential(client, oh, cid)["allow_bundle_reuse"] is True

    # ── Phase 3: wrong type ──────────────────────────────────────────
    tok = create_random_credential(client, oh, credential_type="api_token")
    update_credential(client, oh, tok["id"], allow_sharing=True)
    r = _put(client, oh, tok["id"], allow_bundle_reuse=True)
    assert r.status_code == 400, r.text
    assert get_credential(client, oh, tok["id"])["allow_bundle_reuse"] is False

    # ── Phase 4: issued key ──────────────────────────────────────────
    holder, hh = make_developer(client, su)
    producer = setup_producer(client, oh, name="Reuse-Issued-Producer")
    r = client.post(
        f"{API}/agents/{producer['id']}/agent-api/keys",
        headers=oh,
        json={"issued_to_user_id": holder["id"]},
    )
    assert r.status_code == 200, r.text
    key_cid = r.json()["credential_id"]
    r = _put(client, hh, key_cid, allow_sharing=True, allow_bundle_reuse=True)
    assert r.status_code == 400, r.text
    assert get_credential(client, hh, key_cid)["allow_bundle_reuse"] is False

    # ── Phase 5: a recipient is not the owner ────────────────────────
    other, other_h = make_developer(client, su)
    share_credential_via_api(client, oh, cid, other["email"])
    r = _put(client, other_h, cid, allow_bundle_reuse=False)
    assert r.status_code in (400, 403, 404), r.text
    assert get_credential(client, oh, cid)["allow_bundle_reuse"] is True


def test_sharing_off_clears_bundle_reuse_and_back_on_does_not_restore_it(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Re-use on; PUT allow_sharing=false clears it
    2. Sharing on again leaves re-use off (no silent restore)
    3. Re-use on again; PATCH /sharing off clears it as well
    """
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    cid = _connection(client, oh, "Reuse-Clear-Producer")

    # ── Phase 1: PUT path ────────────────────────────────────────────
    assert _put(client, oh, cid, allow_sharing=True, allow_bundle_reuse=True).status_code == 200
    r = _put(client, oh, cid, allow_sharing=False)
    assert r.status_code == 200, r.text
    assert r.json()["allow_bundle_reuse"] is False

    # ── Phase 2: back on does not restore ────────────────────────────
    r = _put(client, oh, cid, allow_sharing=True)
    assert r.status_code == 200, r.text
    assert get_credential(client, oh, cid)["allow_bundle_reuse"] is False

    # ── Phase 3: PATCH /sharing path ─────────────────────────────────
    assert _put(client, oh, cid, allow_bundle_reuse=True).status_code == 200
    set_credential_sharing(client, oh, cid, False)
    assert get_credential(client, oh, cid)["allow_bundle_reuse"] is False


def test_reshares_listing_and_shared_with_me_effective_flag(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Stranger: GET /reshares -> 404 (existence not leaked)
    2. Owner and a recipient with no re-shares get an empty list
    3. shared-with-me shows allow_bundle_reuse_effective False, then True
       once the owner enables re-use, False again when sharing goes off
    """
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    recipient, rh = make_developer(client, su)
    _, stranger_h = make_developer(client, su)
    cid = _connection(client, oh, "Reuse-Reshares-Producer")
    assert _put(client, oh, cid, allow_sharing=True).status_code == 200
    share_credential_via_api(client, oh, cid, recipient["email"])

    # ── Phase 1: stranger ────────────────────────────────────────────
    r = client.get(f"{API}/credentials/{cid}/reshares", headers=stranger_h)
    assert r.status_code == 404, r.text
    r = client.get(f"{API}/credentials/{cid}/reshares", headers=oh)
    assert r.status_code == 200 and r.json() == {"data": [], "count": 0}, r.text
    r = client.get(f"{API}/credentials/{cid}/reshares", headers=rh)
    assert r.status_code == 200 and r.json() == {"data": [], "count": 0}, r.text

    # ── Phase 2: effective flag ──────────────────────────────────────
    def effective() -> bool:
        r = client.get(f"{API}/credentials/shared-with-me", headers=rh)
        assert r.status_code == 200, r.text
        row = next(c for c in r.json()["data"] if c["id"] == cid)
        return row["allow_bundle_reuse_effective"]

    assert effective() is False
    assert _put(client, oh, cid, allow_bundle_reuse=True).status_code == 200
    assert effective() is True
    set_credential_sharing(client, oh, cid, False)
    r = client.get(f"{API}/credentials/shared-with-me", headers=rh)
    assert all(c["id"] != cid for c in r.json()["data"]) or effective() is False
