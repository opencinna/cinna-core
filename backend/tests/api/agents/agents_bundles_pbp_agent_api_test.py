"""Phase 6 tests — Group 2: PBP agent_api one-shared-token via bundle (integration).

Verifies that a bundle publisher can mark an ``agent_api`` credential
``allow_sharing=True`` + ``provided_by="publisher"`` and that the install-time
pipeline correctly:

  1. Emits a ``provided_by="publisher"`` spec for the ``agent_api`` credential.
  2. Creates a ``CredentialShare`` (publisher → installer) at install time.
  3. Creates an ``AgentCredentialLink`` on the installer's install.
  4. Exposes the credential to the installer via GET /credentials/{id}.
  5. Syncs ``{base_url, token}`` into the installer's container env on
     ``prepare_credentials_for_environment`` (mirrors the URL-rewrite path that
     ``agents_agent_api_test.py`` already covers for the owner agent).
  6. A consumer proxy call authenticates on the shared token (mirrors the
     existing consumer-call test path in ``agents_agent_api_test.py``).

The test uses the ``EnvironmentTestAdapter`` stub exactly as in
``agents_agent_api_test.py`` for proxy calls.

Direct DB access via the ``db`` fixture is used only to verify
``CredentialShare`` and ``AgentCredentialLink`` rows — there are no
listing API endpoints that expose per-install linked-credential ids together
with their owner in the cross-user install scenario.
"""
import uuid
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.credentials.credential import Credential
from app.models.credentials.credential_share import CredentialShare
from app.models.credentials.link_models import AgentCredentialLink
from app.services.credentials.credentials_service import CredentialsService
from app.services.environments.environment_service import EnvironmentService
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import get_credential_with_data
from tests.utils.user import create_random_user, user_authentication_headers

API = settings.API_V1_STR


# ── Module-level helpers ──────────────────────────────────────────────────────


def _make_user_and_headers(client: TestClient) -> tuple[dict, dict[str, str]]:
    """Create a fresh user with a default AI credential; return (user, headers)."""
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _setup_api_agent(
    client: TestClient,
    headers: dict[str, str],
    name: str,
) -> dict:
    """Create an agent with agent_api_enabled=True."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    update_agent(client, headers, agent["id"], agent_api_enabled=True)
    return agent


def _mint_token(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    label: str | None = None,
) -> dict:
    """Mint a proxy token via the connect helper; return token dict."""
    body: dict = {}
    if label is not None:
        body["credential_label"] = label
    r = client.post(
        f"{API}/agents/{agent_id}/agent-api/connect",
        headers=headers,
        json=body,
    )
    assert r.status_code == 200, f"Connect failed: {r.text}"
    conn = r.json()

    cred = get_credential_with_data(client, headers, conn["credential_id"])
    token_value = cred["credential_data"]["token"]
    return {
        "id": conn["token_id"],
        "credential_id": conn["credential_id"],
        "token": token_value,
        "base_url": conn["base_url"],
        "spec_url": conn["spec_url"],
        "agent_id": agent_id,
    }


def _link_credential_to_agent(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    credential_id: str,
) -> None:
    r = client.post(
        f"{API}/agents/{agent_id}/credentials",
        headers=headers,
        json={"credential_id": credential_id},
    )
    assert r.status_code in (200, 201), r.text


def _publish(client: TestClient, headers: dict[str, str], agent_id: str) -> dict:
    """Publish agent, drain tasks, return fresh agent row."""
    r = client.post(f"{API}/agents/{agent_id}/publish", headers=headers, json={})
    assert r.status_code == 200, r.text
    drain_tasks()
    return client.get(f"{API}/agents/{agent_id}", headers=headers).json()


def _make_public(client: TestClient, headers: dict[str, str], bundle_uuid: str) -> None:
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=headers,
        json={"is_listed": True, "visibility": "public"},
    )
    assert r.status_code == 200, r.text


def _install(
    client: TestClient,
    headers: dict[str, str],
    bundle_id: str,
    *,
    request_body: dict | None = None,
) -> dict:
    r = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=headers,
        json=request_body or {},
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    return r.json()


def _consumer_proxy_url(agent_id: str, path: str) -> str:
    return f"{API}/agent-api/{agent_id}/{path}"


def _bearer_headers(token_value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_value}"}


# ── The scenario ──────────────────────────────────────────────────────────────


def test_pbp_agent_api_bundle_install_share_link_sync_proxy(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """PBP one-shared-token flow for an agent_api credential bundled as publisher-provided.

    End-to-end scenario:
      1.  Publisher creates a producer agent with agent_api_enabled=True.
      2.  Publisher mints a connection token via the connect helper.
      3.  The resulting agent_api credential is marked allow_sharing=True.
      4.  Publisher creates a consumer agent B, links the agent_api credential.
      5.  Publisher publishes agent B's bundle.
          Install-context shows the spec as provided_by="publisher".
      6.  Installer installs agent B's bundle (empty body → quick install).
          Assert: CredentialShare (publisher → installer) created.
          Assert: AgentCredentialLink on the install points at the agent_api cred.
      7.  The agent_api credential is accessible to the installer via API.
      8.  prepare_credentials_for_environment on the installer's install
          includes the agent_api credential with rewritten internal URL.
      9.  Consumer proxy call from the installer's session authenticates on
          the shared token → 200.
    """
    pub_headers = superuser_token_headers
    pub_user_id = uuid.UUID(
        client.get(f"{API}/users/me", headers=pub_headers).json()["id"]
    )

    # ── Phase 1-2: Producer agent + minted token ──────────────────────────────
    producer = _setup_api_agent(
        client, pub_headers, name=f"PBP-AgentApi-Producer-{uuid.uuid4().hex[:4]}"
    )
    producer_id = producer["id"]

    minted = _mint_token(
        client, pub_headers, producer_id, label="pbp-bundle-token"
    )
    agent_api_cred_id = minted["credential_id"]
    token_value = minted["token"]
    base_url = minted["base_url"]

    # ── Phase 3: Enable allow_sharing on the agent_api credential ─────────────
    r = client.put(
        f"{API}/credentials/{agent_api_cred_id}",
        headers=pub_headers,
        json={"allow_sharing": True},
    )
    assert r.status_code == 200, r.text
    updated_cred = r.json()
    assert updated_cred["allow_sharing"] is True, (
        "allow_sharing must be True after PUT update"
    )

    # ── Phase 4: Consumer agent B → link the agent_api credential ─────────────
    consumer_b = create_agent_via_api(
        client, pub_headers, name=f"PBP-AgentApi-ConsumerB-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    consumer_b_id = consumer_b["id"]
    _link_credential_to_agent(client, pub_headers, consumer_b_id, agent_api_cred_id)

    # ── Phase 5: Publish consumer B's bundle ──────────────────────────────────
    fresh_b = _publish(client, pub_headers, consumer_b_id)
    bundle_id = fresh_b["bundle_id"]
    bundle_uuid = fresh_b["bundle_uuid"]
    _make_public(client, pub_headers, bundle_uuid)

    # Verify install-context shows provided_by="publisher" for the agent_api spec
    # (use a fresh installer who will later actually install)
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])

    ctx_r = client.get(
        f"{API}/catalog/{bundle_id}/install-context",
        headers=installer_headers,
    )
    assert ctx_r.status_code == 200, ctx_r.text
    ctx = ctx_r.json()
    pbp_specs = [s for s in ctx["service_specs"] if s.get("provided_by") == "publisher"]
    assert len(pbp_specs) == 1, (
        f"Expected 1 publisher-provided spec for the agent_api credential; "
        f"got {ctx['service_specs']}"
    )
    pbp_spec = pbp_specs[0]
    assert pbp_spec["publisher_summary"] is not None, (
        "PBP spec must carry a publisher_summary"
    )
    # The agent_api credential type must surface in the publisher summary
    assert pbp_spec["publisher_summary"]["type"] == "agent_api", (
        f"publisher_summary.type must be 'agent_api'; "
        f"got {pbp_spec['publisher_summary']['type']}"
    )

    # ── Phase 6: Install → assert share + link ────────────────────────────────
    install = _install(client, installer_headers, bundle_id)
    install_id = uuid.UUID(install["id"])
    db.expire_all()

    # CredentialShare (publisher → installer)
    share = db.exec(
        select(CredentialShare).where(
            CredentialShare.credential_id == uuid.UUID(agent_api_cred_id),
            CredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert share is not None, (
        f"Expected CredentialShare (publisher → installer) for agent_api cred "
        f"{agent_api_cred_id} after PBP bundle install; found none"
    )

    # AgentCredentialLink on the install pointing at the publisher's agent_api cred
    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id,
            AgentCredentialLink.credential_id == uuid.UUID(agent_api_cred_id),
        )
    ).all()
    assert len(links) == 1, (
        f"Expected exactly 1 AgentCredentialLink on install {install_id} "
        f"for agent_api cred {agent_api_cred_id}; got {len(links)}"
    )

    # ── Phase 7: Installer can read the shared credential via API ─────────────
    r = client.get(
        f"{API}/credentials/{agent_api_cred_id}",
        headers=installer_headers,
    )
    assert r.status_code == 200, (
        f"Installer must be able to access PBP agent_api credential; got {r.text}"
    )
    cred_resp = r.json()
    assert cred_resp["type"] == "agent_api"

    # ── Phase 8: prepare_credentials_for_environment syncs {base_url, token} ──
    # This mirrors the agent_api URL-rewrite test in agents_agent_api_test.py.
    prepared = CredentialsService.prepare_credentials_for_environment(
        db, install_id
    )
    api_creds = [
        c for c in prepared["credentials_json"] if c["type"] == "agent_api"
    ]
    assert len(api_creds) == 1, (
        f"Expected 1 agent_api credential in prepared env creds; got {api_creds}"
    )
    synced = api_creds[0]["credential_data"]
    assert "base_url" in synced, "synced agent_api cred must have base_url"
    assert "token" in synced, "synced agent_api cred must have token"
    # Token value is preserved (URL rewriting does not touch the token)
    assert synced["token"] == token_value, (
        f"Synced token must equal the minted token; "
        f"got {synced['token'][:8]}... expected {token_value[:8]}..."
    )
    # URL is rewritten to the internal backend origin for container reach
    internal_netloc = urlsplit(settings.AGENT_ENV_BACKEND_URL).netloc
    assert urlsplit(synced["base_url"]).netloc == internal_netloc, (
        f"Synced base_url must point at the internal backend origin "
        f"({internal_netloc}); got {synced['base_url']}"
    )
    # Path still targets the producer agent (same as stored)
    assert producer_id in synced["base_url"], (
        f"Synced base_url must contain the producer agent id ({producer_id}); "
        f"got {synced['base_url']}"
    )

    # ── Phase 9: Consumer proxy call authenticates on the shared token → 200 ──
    # We use the EnvironmentTestAdapter stub, same as agents_agent_api_test.py.
    persistent = EnvironmentTestAdapter()
    lm = EnvironmentService._lifecycle_manager
    original_get_adapter = lm.get_adapter
    lm.get_adapter = lambda env: persistent

    try:
        r = client.get(
            _consumer_proxy_url(producer_id, "ping"),
            headers=_bearer_headers(token_value),
        )
        assert r.status_code == 200, (
            f"Consumer proxy call with shared publisher token must return 200; "
            f"got {r.status_code}: {r.text}"
        )
        body = r.json()
        assert body.get("ok") is True
    finally:
        lm.get_adapter = original_get_adapter
