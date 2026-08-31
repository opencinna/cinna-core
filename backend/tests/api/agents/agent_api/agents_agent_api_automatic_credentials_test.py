"""
Automatic Credentials / agent_api drift feature tests.

Covers four scenarios from the implementation plan:

  1. Connect-time workspace stamping
     - Connecting with a consumer agent stamps the *consumer's* workspace on
       the auto-created agent_api credential.
     - Connecting without a consumer agent (global picker) stamps the
       *producer's* workspace.
     - Both cases are confirmed via GET /credentials?user_workspace_id=<id>.

  2. Fresh-publish provided_by detection (Problem-2 regression guard)
     - Publisher links an agent_api credential with allow_sharing=True,
       publishes → the revision spec reports provided_by="publisher".
     - The install-context for a foreign user also reports "publisher" for
       that spec.
     - Confirms that a fresh publish with sharing enabled is NOT mis-detected
       as "user" (the core Problem-2 bug this feature fixes).

  3. Drift detection (end-to-end)
     - Publish while allow_sharing=False (snapshot "user").
     - Enable allow_sharing=True afterwards WITHOUT republishing.
     - GET /agents/{id}/bundle-credential-drift → stale=True, drift entry
       with snapshot_provided_by="user", live_provided_by="publisher",
       drifted=True.
     - Also asserts the non-drift case: republish after enabling sharing →
       stale=False, no drifted entries.
     - Also asserts the never-published case: publisher install without any
       revision → stale=False, empty drift.

  4. Drift endpoint authorization
     - Non-owner gets 404 (leak-safe, not 403).
     - Non-existent agent ID gets 404.
     - Non-publisher-install owner gets 404 (endpoint is publisher-only).
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import get_credential_with_data
from tests.utils.user import (
    create_random_user,
    promote_to_developer,
    user_authentication_headers,
)
from tests.utils.workspace import create_random_workspace

API = settings.API_V1_STR


# ── Module-level helpers ───────────────────────────────────────────────────────


def _make_user_and_headers(client: TestClient) -> tuple[dict, dict[str, str]]:
    """Create a random user with a default AI credential; return (user, headers)."""
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _setup_api_agent(
    client: TestClient,
    headers: dict[str, str],
    name: str = "API Agent",
    user_workspace_id: str | None = None,
) -> dict:
    """Create an agent with agent_api_enabled=True, optionally in a workspace."""
    body: dict = {"name": name}
    if user_workspace_id is not None:
        body["user_workspace_id"] = user_workspace_id
    r = client.post(f"{API}/agents/", headers=headers, json=body)
    assert r.status_code == 200, f"Agent creation failed: {r.text}"
    agent = r.json()
    drain_tasks()
    update_agent(client, headers, agent["id"], agent_api_enabled=True)
    return agent


def _create_agent_in_workspace(
    client: TestClient,
    headers: dict[str, str],
    name: str,
    user_workspace_id: str,
) -> dict:
    """Create a plain agent in a specific workspace."""
    r = client.post(
        f"{API}/agents/",
        headers=headers,
        json={"name": name, "user_workspace_id": user_workspace_id},
    )
    assert r.status_code == 200, f"Agent creation failed: {r.text}"
    agent = r.json()
    drain_tasks()
    return agent


def _connect(
    client: TestClient,
    headers: dict[str, str],
    producer_agent_id: str,
    *,
    label: str | None = None,
    consumer_agent_id: str | None = None,
) -> dict:
    """Call the connect helper and return the response JSON."""
    body: dict = {}
    if label is not None:
        body["credential_label"] = label
    if consumer_agent_id is not None:
        body["consumer_agent_id"] = consumer_agent_id
    r = client.post(
        f"{API}/agents/{producer_agent_id}/agent-api/connect",
        headers=headers,
        json=body,
    )
    assert r.status_code == 200, f"Connect failed: {r.text}"
    return r.json()


def _publish(client: TestClient, headers: dict[str, str], agent_id: str) -> dict:
    """Publish the agent bundle and return the fresh agent row."""
    r = client.post(f"{API}/agents/{agent_id}/publish", headers=headers, json={})
    assert r.status_code == 200, r.text
    drain_tasks()
    fresh = client.get(f"{API}/agents/{agent_id}", headers=headers)
    assert fresh.status_code == 200, fresh.text
    return fresh.json()


def _make_public(
    client: TestClient, headers: dict[str, str], bundle_uuid: str
) -> None:
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=headers,
        json={"is_listed": True, "visibility": "public"},
    )
    assert r.status_code == 200, r.text


def _drift_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/bundle-credential-drift"


# ── Scenario 1: Connect-time workspace stamping ────────────────────────────────


def test_connect_time_workspace_stamping(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Connect-time workspace stamping:
      1. Create a workspace W.
      2. Create a producer agent with agent_api_enabled=True (no workspace).
      3. Create a consumer agent IN workspace W.
      4. Connect producer to consumer via connect helper with consumer_agent_id.
      5. Assert the created agent_api credential has user_workspace_id == W.
      6. Confirm via workspace-filtered credential list: credential appears
         under W's filter but NOT under the default-workspace filter.

      7. Create a second producer agent IN workspace W2.
      8. Connect without a consumer_agent_id (global picker).
      9. Assert the resulting credential has user_workspace_id == W2 (producer's workspace).
         Confirmed via workspace-filtered list.

      10. Authorization guard: non-owner cannot see these credentials.
    """
    # ── Phase 1: workspace W ─────────────────────────────────────────────────
    ws = create_random_workspace(client, superuser_token_headers)
    ws_id = ws["id"]

    # ── Phase 2: producer (no workspace) ─────────────────────────────────────
    producer = _setup_api_agent(
        client, superuser_token_headers, name="Stamp-Producer-NoWS"
    )
    producer_id = producer["id"]

    # ── Phase 3: consumer agent IN workspace W ────────────────────────────────
    consumer = _create_agent_in_workspace(
        client,
        superuser_token_headers,
        name=f"Stamp-Consumer-{uuid.uuid4().hex[:4]}",
        user_workspace_id=ws_id,
    )
    consumer_id = consumer["id"]

    # ── Phase 4: connect with consumer_agent_id ───────────────────────────────
    conn = _connect(
        client,
        superuser_token_headers,
        producer_id,
        label="stamp-consumer-test",
        consumer_agent_id=consumer_id,
    )
    cred_id = conn["credential_id"]

    # ── Phase 5: credential carries consumer's workspace ─────────────────────
    # GET /credentials/{id} returns user_workspace_id on the public response
    r = client.get(f"{API}/credentials/{cred_id}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    cred_public = r.json()
    assert str(cred_public["user_workspace_id"]) == ws_id, (
        f"Expected user_workspace_id={ws_id} (consumer's workspace); "
        f"got {cred_public['user_workspace_id']}"
    )
    assert cred_public["type"] == "agent_api"

    # ── Phase 6: workspace-filtered list includes the credential ─────────────
    r = client.get(
        f"{API}/credentials/",
        headers=superuser_token_headers,
        params={"user_workspace_id": ws_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    creds_in_ws = body.get("data", [])
    assert any(c["id"] == cred_id for c in creds_in_ws), (
        f"agent_api credential {cred_id} must appear under workspace {ws_id} filter; "
        f"found: {[c['id'] for c in creds_in_ws]}"
    )

    # Credential does NOT appear under the default-workspace filter
    # (default workspace = empty string → NULL filter)
    r = client.get(
        f"{API}/credentials/",
        headers=superuser_token_headers,
        params={"user_workspace_id": ""},
    )
    assert r.status_code == 200, r.text
    default_body = r.json()
    default_creds = default_body.get("data", [])
    assert not any(c["id"] == cred_id for c in default_creds), (
        f"agent_api credential {cred_id} must NOT appear under the default workspace "
        f"filter when it belongs to workspace {ws_id}"
    )

    # ── Phase 7-9: producer in workspace W2 → no consumer (global picker) ────
    ws2 = create_random_workspace(
        client,
        superuser_token_headers,
        name=f"ws2-{uuid.uuid4().hex[:6]}",
    )
    ws2_id = ws2["id"]

    producer2 = _setup_api_agent(
        client,
        superuser_token_headers,
        name=f"Stamp-Producer-W2-{uuid.uuid4().hex[:4]}",
        user_workspace_id=ws2_id,
    )
    producer2_id = producer2["id"]

    conn2 = _connect(
        client,
        superuser_token_headers,
        producer2_id,
        label="stamp-producer-ws-test",
        # No consumer_agent_id → falls back to producer's workspace
    )
    cred2_id = conn2["credential_id"]

    r = client.get(f"{API}/credentials/{cred2_id}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    cred2_public = r.json()
    assert str(cred2_public["user_workspace_id"]) == ws2_id, (
        f"Expected user_workspace_id={ws2_id} (producer's workspace, no consumer); "
        f"got {cred2_public['user_workspace_id']}"
    )

    r = client.get(
        f"{API}/credentials/",
        headers=superuser_token_headers,
        params={"user_workspace_id": ws2_id},
    )
    assert r.status_code == 200, r.text
    body2 = r.json()
    creds_in_ws2 = body2.get("data", [])
    assert any(c["id"] == cred2_id for c in creds_in_ws2), (
        f"Credential {cred2_id} must appear under workspace {ws2_id} filter"
    )

    # ── Phase 10: non-owner sees neither credential ───────────────────────────
    _, other_headers = _make_user_and_headers(client)
    r_other = client.get(
        f"{API}/credentials/{cred_id}", headers=other_headers
    )
    # The credential is not shared with the other user → they cannot access it.
    # The credentials route returns 400 "Not enough permissions" for unshared
    # credentials (not 403/404) — that is the established route contract.
    assert r_other.status_code in (400, 403, 404), (
        f"Non-owner must not access credential {cred_id}; got {r_other.status_code}"
    )


def test_connect_rejects_non_owned_consumer_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A caller may not stamp/link a consumer agent they do not own.

    The connect helper validates consumer-agent ownership up front (same
    authority as the link step). A non-owned ``consumer_agent_id`` must be
    rejected BEFORE any token/credential is minted — so it cannot leave an
    orphaned credential stamped with a foreign agent's workspace.

      1. User A owns a producer agent with the API enabled.
      2. User B owns a consumer agent.
      3. User A connects with B's consumer_agent_id → 403 (not owned).
      4. Assert no orphaned agent_api credential was created for A.
    """
    # User A — producer owner. Promote so they can create agents.
    user_a, headers_a = _make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, user_a["id"])
    producer = _setup_api_agent(client, headers_a, name="Reject-Producer")
    producer_id = producer["id"]

    # User B — consumer owner.
    user_b, headers_b = _make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, user_b["id"])
    consumer_b = client.post(
        f"{API}/agents/",
        headers=headers_b,
        json={"name": "Reject-Consumer-B"},
    ).json()
    drain_tasks()
    consumer_b_id = consumer_b["id"]

    # User A's credential count before the attempt.
    before = client.get(
        f"{API}/credentials", headers=headers_a, params={"limit": 100}
    ).json()
    before_agent_api = [c for c in before["data"] if c["type"] == "agent_api"]

    # User A connects with B's consumer → rejected up front.
    r = client.post(
        f"{API}/agents/{producer_id}/agent-api/connect",
        headers=headers_a,
        json={"consumer_agent_id": consumer_b_id},
    )
    assert r.status_code == 403, (
        f"Connecting with a non-owned consumer agent must be rejected with "
        f"403; got {r.status_code}: {r.text}"
    )

    # No orphaned agent_api credential was created for A.
    after = client.get(
        f"{API}/credentials", headers=headers_a, params={"limit": 100}
    ).json()
    after_agent_api = [c for c in after["data"] if c["type"] == "agent_api"]
    assert len(after_agent_api) == len(before_agent_api), (
        "Rejected connect must not leave an orphaned agent_api credential; "
        f"before={len(before_agent_api)} after={len(after_agent_api)}"
    )


# ── Scenario 2: Fresh-publish → agent_api provided_by="publisher" ─────────────


def test_fresh_publish_agent_api_allow_sharing_snapshots_as_publisher(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Fresh-publish provided_by detection (Problem-2 regression guard):
      1. Create a producer agent with agent_api_enabled=True.
      2. Mint an agent_api credential via the connect helper.
      3. Enable allow_sharing=True on the credential.
      4. Create a consumer agent and link the credential to it.
      5. Publish the consumer agent's bundle.
      6. Assert the latest revision's required_credential_specs entry for the
         agent_api credential has provided_by="publisher".
      7. Make bundle public; assert foreign install-context also shows
         provided_by="publisher" for the agent_api spec.
    """
    headers = superuser_token_headers

    # ── Phase 1-2: Producer + minted agent_api credential ────────────────────
    producer = _setup_api_agent(
        client, headers, name=f"PBP-Producer-{uuid.uuid4().hex[:4]}"
    )
    producer_id = producer["id"]

    conn = _connect(client, headers, producer_id, label="fresh-pub-sharing-test")
    agent_api_cred_id = conn["credential_id"]

    # ── Phase 3: Enable allow_sharing on the agent_api credential ────────────
    r = client.put(
        f"{API}/credentials/{agent_api_cred_id}",
        headers=headers,
        json={"allow_sharing": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["allow_sharing"] is True

    # ── Phase 4: Consumer agent B → link the agent_api credential ────────────
    r = client.post(
        f"{API}/agents/",
        headers=headers,
        json={"name": f"PBP-Consumer-{uuid.uuid4().hex[:4]}"},
    )
    assert r.status_code == 200, r.text
    consumer = r.json()
    drain_tasks()
    consumer_id = consumer["id"]

    r = client.post(
        f"{API}/agents/{consumer_id}/credentials",
        headers=headers,
        json={"credential_id": agent_api_cred_id},
    )
    assert r.status_code in (200, 201), r.text

    # ── Phase 5: Publish consumer's bundle ───────────────────────────────────
    fresh_consumer = _publish(client, headers, consumer_id)
    bundle_uuid = fresh_consumer["bundle_uuid"]

    # ── Phase 6: Revision spec shows provided_by="publisher" ─────────────────
    revs_r = client.get(
        f"{API}/bundles/{bundle_uuid}/revisions", headers=headers
    )
    assert revs_r.status_code == 200, revs_r.text
    revs_data = revs_r.json()["data"]
    assert len(revs_data) >= 1, "Expected at least one revision after publish"
    latest_specs = revs_data[0]["required_credential_specs"]

    agent_api_specs = [s for s in latest_specs if s.get("type") == "agent_api"]
    assert len(agent_api_specs) == 1, (
        f"Expected exactly 1 agent_api spec in revision; got {latest_specs}"
    )
    spec = agent_api_specs[0]
    assert spec["provided_by"] == "publisher", (
        f"agent_api credential with allow_sharing=True published fresh must yield "
        f"provided_by='publisher'; got '{spec['provided_by']}'. "
        f"This is the Problem-2 regression guard: a fresh publish must not "
        f"mis-detect a shared agent_api credential as 'user'."
    )
    assert spec["publisher_credential_id"] == agent_api_cred_id, (
        f"Expected publisher_credential_id={agent_api_cred_id}; got {spec}"
    )
    assert spec["allow_sharing"] is True

    # ── Phase 7: Foreign install-context also reports "publisher" ────────────
    bundle_id = fresh_consumer["bundle_id"]
    _make_public(client, headers, bundle_uuid)

    _, installer_headers = _make_user_and_headers(client)
    ctx_r = client.get(
        f"{API}/catalog/{bundle_id}/install-context",
        headers=installer_headers,
    )
    assert ctx_r.status_code == 200, ctx_r.text
    ctx = ctx_r.json()

    pbp_specs = [s for s in ctx["service_specs"] if s.get("provided_by") == "publisher"]
    assert any(
        s.get("publisher_summary", {}).get("type") == "agent_api"
        for s in pbp_specs
    ), (
        f"Foreign install-context must surface the agent_api spec as provided_by='publisher'; "
        f"got service_specs={ctx['service_specs']}"
    )


# ── Scenario 3: Drift detection ───────────────────────────────────────────────


def test_drift_detection_stale_after_sharing_enabled_post_publish(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Drift detection end-to-end:
      A. Never-published: drift endpoint returns stale=False, empty drift.
      B. Publish while allow_sharing=False (snapshot "user"), then enable
         allow_sharing=True WITHOUT republishing → stale=True, drift entry
         with snapshot_provided_by="user", live_provided_by="publisher".
         Foreign install-context still shows "user" (snapshot is immutable).
      C. Republish after enabling sharing → stale=False, no drifted entries.
    """
    headers = superuser_token_headers

    # ── Setup: producer + consumer agent with linked agent_api credential ─────
    producer = _setup_api_agent(
        client, headers, name=f"Drift-Producer-{uuid.uuid4().hex[:4]}"
    )
    conn = _connect(
        client, headers, producer["id"], label="drift-test-cred"
    )
    agent_api_cred_id = conn["credential_id"]
    cred_name = get_credential_with_data(client, headers, agent_api_cred_id)["name"]

    r = client.post(
        f"{API}/agents/",
        headers=headers,
        json={"name": f"Drift-Consumer-{uuid.uuid4().hex[:4]}"},
    )
    assert r.status_code == 200, r.text
    consumer = r.json()
    drain_tasks()
    consumer_id = consumer["id"]

    r = client.post(
        f"{API}/agents/{consumer_id}/credentials",
        headers=headers,
        json={"credential_id": agent_api_cred_id},
    )
    assert r.status_code in (200, 201), r.text

    # ── Phase A: Never published → stale=False, empty drift ──────────────────
    # The consumer is a publisher install after the first publish; before
    # any publish it is a regular agent install with no bundle_uuid.
    # The drift endpoint requires is_publisher_install=True; the install gains
    # that flag on first publish. Before the first publish there is no publisher
    # install for this agent, so the endpoint simply 404s (publisher-only guard).
    # Confirm the endpoint returns 404 (not a 500) before publish:
    r_before_pub = client.get(_drift_url(consumer_id), headers=headers)
    assert r_before_pub.status_code == 404, (
        f"Drift endpoint must return 404 for an agent that is not yet a "
        f"publisher install; got {r_before_pub.status_code}: {r_before_pub.text}"
    )

    # ── Phase B: Publish with allow_sharing=False (snapshot "user") ───────────
    # The credential was created with allow_sharing=False by the connect helper
    # (that is the default — the very pattern that triggers the bug).
    # Confirm sharing is still False before publishing:
    r_cred = client.get(f"{API}/credentials/{agent_api_cred_id}", headers=headers)
    assert r_cred.status_code == 200, r_cred.text
    assert r_cred.json()["allow_sharing"] is False, (
        "Credential must start with allow_sharing=False (connect helper default)"
    )

    fresh_after_first_pub = _publish(client, headers, consumer_id)
    bundle_uuid = fresh_after_first_pub["bundle_uuid"]
    bundle_id = fresh_after_first_pub["bundle_id"]

    # Verify snapshot says "user" for the agent_api spec
    revs_r = client.get(
        f"{API}/bundles/{bundle_uuid}/revisions", headers=headers
    )
    assert revs_r.status_code == 200, revs_r.text
    revs_data = revs_r.json()["data"]
    first_rev_specs = revs_data[0]["required_credential_specs"]
    agent_api_spec_v1 = next(
        (s for s in first_rev_specs if s.get("name") == cred_name), None
    )
    assert agent_api_spec_v1 is not None, (
        f"Expected spec named '{cred_name}' in first revision; got {first_rev_specs}"
    )
    assert agent_api_spec_v1["provided_by"] == "user", (
        f"First publish (allow_sharing=False) must snapshot as 'user'; "
        f"got '{agent_api_spec_v1['provided_by']}'"
    )

    # Drift endpoint: no drift yet (nothing changed after publish)
    r_drift = client.get(_drift_url(consumer_id), headers=headers)
    assert r_drift.status_code == 200, r_drift.text
    no_drift = r_drift.json()
    assert no_drift["stale"] is False, (
        f"Drift must be stale=False right after publish (nothing changed); "
        f"got {no_drift}"
    )
    # All entries report drifted=False
    assert all(not d["drifted"] for d in no_drift["drift"]), (
        f"No entry should be drifted immediately after publish; got {no_drift['drift']}"
    )

    # Now enable allow_sharing=True WITHOUT republishing
    r = client.put(
        f"{API}/credentials/{agent_api_cred_id}",
        headers=headers,
        json={"allow_sharing": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["allow_sharing"] is True

    # Drift endpoint now reports stale=True
    r_drift2 = client.get(_drift_url(consumer_id), headers=headers)
    assert r_drift2.status_code == 200, r_drift2.text
    drift_body = r_drift2.json()

    assert drift_body["stale"] is True, (
        f"After enabling allow_sharing post-publish, drift must be stale=True; "
        f"got {drift_body}"
    )

    drifted_entries = [d for d in drift_body["drift"] if d["drifted"]]
    assert len(drifted_entries) >= 1, (
        f"Expected at least one drifted entry; got drift={drift_body['drift']}"
    )

    agent_api_drift_entry = next(
        (d for d in drifted_entries if d["name"] == cred_name), None
    )
    assert agent_api_drift_entry is not None, (
        f"Expected drift entry for credential '{cred_name}'; "
        f"got drifted_entries={drifted_entries}"
    )
    assert agent_api_drift_entry["snapshot_provided_by"] == "user", (
        f"Snapshot must still be 'user' (immutable revision); "
        f"got {agent_api_drift_entry}"
    )
    assert agent_api_drift_entry["live_provided_by"] == "publisher", (
        f"Live must be 'publisher' (allow_sharing=True now); "
        f"got {agent_api_drift_entry}"
    )
    assert agent_api_drift_entry["type"] == "agent_api", (
        f"Drift entry type must be 'agent_api'; got {agent_api_drift_entry}"
    )

    # Foreign install-context still shows "user" (immutable snapshot until republish)
    _make_public(client, headers, bundle_uuid)
    _, installer_headers = _make_user_and_headers(client)
    ctx_r = client.get(
        f"{API}/catalog/{bundle_id}/install-context",
        headers=installer_headers,
    )
    assert ctx_r.status_code == 200, ctx_r.text
    ctx = ctx_r.json()
    agent_api_install_spec = next(
        (s for s in ctx["service_specs"] if s.get("name") == cred_name),
        None,
    )
    assert agent_api_install_spec is not None, (
        f"Expected install-context spec named '{cred_name}'; got {ctx['service_specs']}"
    )
    assert agent_api_install_spec["provided_by"] == "user", (
        f"Install-context must still show 'user' (snapshot is immutable) until "
        f"publisher republishes; got '{agent_api_install_spec['provided_by']}'"
    )

    # ── Phase C: Republish → drift clears ─────────────────────────────────────
    fresh_after_second_pub = _publish(client, headers, consumer_id)
    assert fresh_after_second_pub["bundle_uuid"] == bundle_uuid, (
        "Bundle UUID must remain the same after republish"
    )

    # Second revision snapshot now says "publisher"
    revs_r2 = client.get(
        f"{API}/bundles/{bundle_uuid}/revisions", headers=headers
    )
    assert revs_r2.status_code == 200, revs_r2.text
    revs_data2 = revs_r2.json()["data"]
    # newest-first; revs_data2[0] is the latest revision
    second_rev_specs = revs_data2[0]["required_credential_specs"]
    agent_api_spec_v2 = next(
        (s for s in second_rev_specs if s.get("name") == cred_name), None
    )
    assert agent_api_spec_v2 is not None, (
        f"Expected spec '{cred_name}' in second revision; got {second_rev_specs}"
    )
    assert agent_api_spec_v2["provided_by"] == "publisher", (
        f"Second revision (republished with allow_sharing=True) must snapshot "
        f"as 'publisher'; got '{agent_api_spec_v2['provided_by']}'"
    )

    # Drift endpoint now reports stale=False
    r_drift3 = client.get(_drift_url(consumer_id), headers=headers)
    assert r_drift3.status_code == 200, r_drift3.text
    after_republish = r_drift3.json()
    assert after_republish["stale"] is False, (
        f"After republish, drift must be stale=False; got {after_republish}"
    )
    assert all(not d["drifted"] for d in after_republish["drift"]), (
        f"No entry should be drifted after republish; got {after_republish['drift']}"
    )


# ── Scenario 4: Drift endpoint authorization ─────────────────────────────────


def test_drift_endpoint_authorization(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Drift endpoint authorization:
      1. Owner can access the endpoint after first publish.
      2. A non-owner *developer* gets 404 (no existence leak — not 403). The
         endpoint is ``require_developer``-gated like its publisher siblings,
         so the non-owner is promoted first to isolate the ownership/leak-safe
         path from the role gate.
      3. Non-existent agent ID gets 404.
      4. An agent that is a foreign install (not a publisher install) returns
         404 even for its owner, because the endpoint is publisher-install-only.
    """
    headers = superuser_token_headers

    # ── Setup: create a minimal publisher install ─────────────────────────────
    producer = _setup_api_agent(
        client, headers, name=f"Auth-Drift-Producer-{uuid.uuid4().hex[:4]}"
    )
    conn = _connect(client, headers, producer["id"], label="auth-drift-cred")
    agent_api_cred_id = conn["credential_id"]

    r = client.post(
        f"{API}/agents/",
        headers=headers,
        json={"name": f"Auth-Drift-Consumer-{uuid.uuid4().hex[:4]}"},
    )
    assert r.status_code == 200, r.text
    consumer = r.json()
    drain_tasks()
    consumer_id = consumer["id"]

    r = client.post(
        f"{API}/agents/{consumer_id}/credentials",
        headers=headers,
        json={"credential_id": agent_api_cred_id},
    )
    assert r.status_code in (200, 201), r.text

    # Publish to make this a publisher install
    _publish(client, headers, consumer_id)

    # ── Phase 1: Owner can access the drift endpoint ──────────────────────────
    r_owner = client.get(_drift_url(consumer_id), headers=headers)
    assert r_owner.status_code == 200, (
        f"Owner must be able to access drift endpoint after publish; "
        f"got {r_owner.status_code}: {r_owner.text}"
    )
    assert "stale" in r_owner.json(), "Response must contain 'stale' field"
    assert "drift" in r_owner.json(), "Response must contain 'drift' field"

    # ── Phase 2: Non-owner developer gets 404 (no existence leak) ─────────────
    # The endpoint is require_developer-gated, so promote the non-owner to
    # developer first — otherwise they'd hit the generic role-gate 403 before
    # the ownership check. With the role gate cleared, a non-owner must get the
    # leak-safe 404 (not 403) so bundle existence isn't revealed.
    other_user, other_headers = _make_user_and_headers(client)
    promote_to_developer(client, headers, other_user["id"])
    r_other = client.get(_drift_url(consumer_id), headers=other_headers)
    assert r_other.status_code == 404, (
        f"Non-owner developer must get 404 (leak-safe, not 403); "
        f"got {r_other.status_code}: {r_other.text}"
    )

    # ── Phase 3: Non-existent agent ID → 404 ──────────────────────────────────
    ghost_id = str(uuid.uuid4())
    r_ghost = client.get(_drift_url(ghost_id), headers=headers)
    assert r_ghost.status_code == 404, (
        f"Non-existent agent ID must return 404; got {r_ghost.status_code}"
    )

    # ── Phase 4: Foreign install (non-publisher) → 404 ────────────────────────
    # Create a second user who installs the bundle; their install is a foreign
    # install (is_publisher_install=False) — endpoint must 404 even for them.
    fresh_consumer = client.get(
        f"{API}/agents/{consumer_id}", headers=headers
    ).json()
    bundle_id = fresh_consumer["bundle_id"]
    bundle_uuid = fresh_consumer["bundle_uuid"]
    _make_public(client, headers, bundle_uuid)

    installer_user, installer_headers = _make_user_and_headers(client)
    # Promote so the role gate is cleared and we exercise the publisher-only
    # 404 path (not the generic developer-role 403).
    promote_to_developer(client, headers, installer_user["id"])
    r_install = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=installer_headers,
        json={},
    )
    assert r_install.status_code == 200, r_install.text
    drain_tasks()
    install = r_install.json()
    install_id = install["id"]

    # The installer owns this agent row but it is a foreign install
    assert install["is_publisher_install"] is False, (
        "Installed agent must not be a publisher install"
    )

    r_foreign_drift = client.get(_drift_url(install_id), headers=installer_headers)
    assert r_foreign_drift.status_code == 404, (
        f"Foreign install owner (developer) must get 404 (publisher-only endpoint); "
        f"got {r_foreign_drift.status_code}: {r_foreign_drift.text}"
    )
