"""
Integration tests: issued-key env sync (P3, plan §5 / D7 / D21).

An issued key reaches an agent environment's ``credentials.json`` only when the
credential owner (the recipient B) == the key's subject == the environment
agent's owner. A key that is not currently usable is still synced, but
token-less, with ``unavailable_reason`` (``key_revoked | key_expired |
key_paused``) and ``key_status`` so the container can say *why*.

The synced payload is observed through the env adapter's captured
``set_credentials`` call (the same seam ``agents_agent_api_test.py`` uses).
"""
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models import AgentApiToken
from app.services.environments.environment_service import EnvironmentService
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    create_random_credential,
    link_credential_to_agent,
    update_credential,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer

API = settings.API_V1_STR


def _setup_producer(client, headers, name: str) -> dict:
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    update_agent(
        client, headers, agent["id"],
        agent_api_enabled=True, agent_api_external_access_enabled=True,
    )
    return agent


def _developer(client, superuser_headers) -> tuple[dict, dict[str, str]]:
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, credential_type="anthropic", set_default=True)
    return user, headers


def _issue(client, issuer_headers, producer_id, recipient_id) -> dict:
    r = client.post(
        f"{API}/agents/{producer_id}/agent-api/keys",
        headers=issuer_headers,
        json={"issued_to_user_id": recipient_id, "label": "env-key"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _reveal(client, headers, credential_id) -> str:
    r = client.post(f"{API}/credentials/{credential_id}/agent-api-key/reveal", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _capture(trigger: Callable[[], None]) -> list[dict]:
    """Run ``trigger`` with a persistent adapter and return the last synced
    ``credentials.json`` it received."""
    persistent = EnvironmentTestAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: persistent
    try:
        trigger()
        drain_tasks()
    finally:
        lm.get_adapter = original
    assert persistent.credentials_set, "credential sync never reached the env adapter"
    return persistent.credentials_set["credentials_json"]


def _agent_api_entries(creds: list[dict]) -> list[dict]:
    return [c for c in creds if c["type"] == "agent_api"]


def _touch(client, headers, credential_id) -> None:
    """Fire a sync by editing the credential (name only)."""
    update_credential(client, headers, credential_id, name=f"touched-{uuid.uuid4().hex[:6]}")


def _setup_recipient_with_key(client, su_headers, producer_name: str):
    producer = _setup_producer(client, su_headers, producer_name)
    b, b_headers = _developer(client, su_headers)
    b_agent = create_agent_via_api(client, b_headers, name="B Consumer")
    drain_tasks()
    key = _issue(client, su_headers, producer["id"], b["id"])
    token = _reveal(client, b_headers, key["credential_id"])
    return producer, b, b_headers, b_agent, key, token


def test_active_key_syncs_into_recipient_agent_with_internal_url_rewrite(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    producer, b, b_headers, b_agent, key, token = _setup_recipient_with_key(
        client, superuser_token_headers, "Env Sync Producer"
    )

    creds = _capture(
        lambda: link_credential_to_agent(client, b_headers, b_agent["id"], key["credential_id"])
    )
    entries = _agent_api_entries(creds)
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["id"] == key["credential_id"]
    assert "unavailable_reason" not in entry or entry["unavailable_reason"] is None
    data = entry["credential_data"]
    assert data["token"] == token
    internal = urlsplit(settings.AGENT_ENV_BACKEND_URL).netloc
    assert urlsplit(data["base_url"]).netloc == internal
    assert urlsplit(data["spec_url"]).netloc == internal
    assert producer["id"] in data["base_url"]
    assert data["spec_url"].endswith("/openapi.json")
    assert entry["service_uri"] == "env-sync-producer-api"


def test_non_active_keys_sync_token_less_with_reason_and_key_status(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """revoked -> key_revoked / revoked_by_issuer; paused -> key_paused /
    external_access_paused (and back to a full entry on resume); expired ->
    key_expired / expired."""
    su = superuser_token_headers
    producer, b, b_headers, b_agent, key, token = _setup_recipient_with_key(
        client, su, "Env Unavailable Producer"
    )
    cred_id = key["credential_id"]
    _capture(lambda: link_credential_to_agent(client, b_headers, b_agent["id"], cred_id))

    def single(creds: list[dict]) -> dict:
        entries = _agent_api_entries(creds)
        assert len(entries) == 1, entries
        return entries[0]

    # ── paused ─────────────────────────────────────────────────────────────
    entry = single(_capture(lambda: update_agent(
        client, su, producer["id"], agent_api_external_access_enabled=False)))
    assert entry["unavailable_reason"] == "key_paused"
    assert entry["key_status"] == "external_access_paused"
    assert "token" not in entry["credential_data"]
    assert set(entry["credential_data"]) >= {"base_url", "spec_url", "label", "producer_agent_id"}

    # ── resume -> usable again with the token ──────────────────────────────
    entry = single(_capture(lambda: update_agent(
        client, su, producer["id"], agent_api_external_access_enabled=True)))
    assert entry.get("unavailable_reason") is None
    assert entry["credential_data"]["token"] == token

    # ── expired (time-based: lands on the next sync) ───────────────────────
    row = db.get(AgentApiToken, uuid.UUID(key["id"]))
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    db.add(row)
    db.commit()
    entry = single(_capture(lambda: _touch(client, b_headers, cred_id)))
    assert entry["unavailable_reason"] == "key_expired"
    assert entry["key_status"] == "expired"
    assert "token" not in entry["credential_data"]

    # ── revoked (precedes expired) ─────────────────────────────────────────
    entry = single(_capture(lambda: client.post(
        f"{API}/agents/{producer['id']}/agent-api/keys/{key['id']}/revoke", headers=su)))
    assert entry["unavailable_reason"] == "key_revoked"
    assert entry["key_status"] == "revoked_by_issuer"
    assert "token" not in entry["credential_data"]
    assert token not in str(entry)


def test_key_never_syncs_to_an_agent_the_recipient_does_not_own(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The issuer cannot link the recipient's credential to the issuer's own
    agent (it is not theirs), so the key never reaches an agent B does not own."""
    su = superuser_token_headers
    producer, b, b_headers, b_agent, key, token = _setup_recipient_with_key(
        client, su, "Env Foreign Producer"
    )
    issuer_agent = create_agent_via_api(client, su, name="Issuer Consumer")
    drain_tasks()

    r = client.post(
        f"{API}/agents/{issuer_agent['id']}/credentials",
        headers=su,
        json={"credential_id": key["credential_id"]},
    )
    assert r.status_code in (400, 403, 404), r.text

    # The recipient can not link it to an agent they do not own either.
    r = client.post(
        f"{API}/agents/{issuer_agent['id']}/credentials",
        headers=b_headers,
        json={"credential_id": key["credential_id"]},
    )
    assert r.status_code in (400, 403, 404), r.text

    # The issuer's agent environment, synced for any reason, holds no key.
    other = create_random_credential(client, su, credential_type="api_token")
    persisted = _capture(
        lambda: link_credential_to_agent(client, su, issuer_agent["id"], other["id"])
    )
    assert _agent_api_entries(persisted) == []
    assert token not in str(persisted)


def test_connection_whose_producer_was_deleted_is_not_synced(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    producer = _setup_producer(client, su, "Doomed Conn Producer")
    consumer = create_agent_via_api(client, su, name="Conn Consumer")
    drain_tasks()
    r = client.post(
        f"{API}/agents/{producer['id']}/agent-api/connect",
        headers=su,
        json={"consumer_agent_id": consumer["id"], "credential_label": "conn"},
    )
    assert r.status_code == 200, r.text
    conn_cred = r.json()["credential_id"]

    creds = _capture(lambda: _touch(client, su, conn_cred))
    assert len(_agent_api_entries(creds)) == 1, "control: live connection syncs"

    r = client.delete(f"{API}/agents/{producer['id']}", headers=su)
    assert r.status_code == 200, r.text

    creds = _capture(lambda: _touch(client, su, conn_cred))
    assert _agent_api_entries(creds) == [], "a connection to a deleted producer must not sync"


def test_owner_identity_token_only_present_when_a_connection_is_synced(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An env whose only agent_api entries are issued keys gets no
    ``owner_identity_token`` block; an env with a connection still does."""
    su = superuser_token_headers
    producer, b, b_headers, b_agent, key, token = _setup_recipient_with_key(
        client, su, "Identity Block Producer"
    )
    creds = _capture(
        lambda: link_credential_to_agent(client, b_headers, b_agent["id"], key["credential_id"])
    )
    assert len(_agent_api_entries(creds)) == 1
    assert [c for c in creds if c["type"] == "owner_identity_token"] == []

    consumer = create_agent_via_api(client, su, name="Identity Block Consumer")
    drain_tasks()
    r = client.post(
        f"{API}/agents/{producer['id']}/agent-api/connect",
        headers=su,
        json={"consumer_agent_id": consumer["id"], "credential_label": "conn"},
    )
    assert r.status_code == 200, r.text
    creds = _capture(lambda: _touch(client, su, r.json()["credential_id"]))
    assert len([c for c in creds if c["type"] == "owner_identity_token"]) == 1
