"""
Integration tests for native-client bundle version surfacing + in-app updates.

Covers the External Agent Access additions that let Cinna Desktop / Mobile show
"installed vs latest" bundle version and apply an update without the web SPA:

  - GET    /external/agents                              → bundle_version snapshot
  - POST   /external/agents/{id}/check-updates           → reconcile pending_update
  - POST   /external/agents/{id}/apply-update            → apply latest revision

Scenarios:
  1. Full version/update lifecycle from a consumer install's point of view:
     install v1.0 → discovery shows up-to-date → publisher ships v1.1 →
     discovery shows update_available → check-updates → apply-update →
     discovery shows up-to-date again.
  2. bundle_version is None for the publisher's own working copy and for plain
     (never-installed-from-a-bundle) agents; ownership + auth guards on the
     update endpoints.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import install_bundle, make_bundle_public, make_user_and_headers
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR
_EXT_BASE = f"{API}/external"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_external_agents(client: TestClient, headers: dict) -> list[dict]:
    r = client.get(f"{_EXT_BASE}/agents", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["targets"]


def _agent_target(targets: list[dict], agent_id: str) -> dict:
    return next(
        t
        for t in targets
        if t["target_type"] == "agent" and t["target_id"] == agent_id
    )


def _publish(
    client: TestClient,
    headers: dict,
    agent_id: str,
    *,
    version: str,
    notes: str | None = None,
) -> dict:
    """Publish an agent with an explicit version label, then drain tasks."""
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={"version": version, "release_notes": notes},
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    return r.json()


# ---------------------------------------------------------------------------
# Scenario 1: full version + update lifecycle
# ---------------------------------------------------------------------------


def test_external_install_version_and_update_lifecycle(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    Consumer install version + update flow over the native surface:
      1. Publisher publishes v1.0 and makes the bundle public.
      2. Consumer installs it.
      3. Discovery → bundle_version present, installed==latest==1.0, no update.
      4. Publisher publishes v1.1.
      5. Discovery → update_available True, installed 1.0 / latest 1.1
         (read-only, derived from revision numbers).
      6. check-updates → pending_update True with both version labels.
      7. apply-update → returns post-update snapshot at 1.1, no update.
      8. Discovery → up to date again at 1.1.
    """
    # ── Phase 1: Publisher publishes v1.0 + makes public ──────────────────
    pub_agent = create_agent_via_api(
        client, superuser_token_headers, name="Versioned Bundle"
    )
    drain_tasks()
    _publish(client, superuser_token_headers, pub_agent["id"], version="1.0", notes="v1")
    pub_fresh = client.get(
        f"{API}/agents/{pub_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = pub_fresh["bundle_id"]
    make_bundle_public(client, superuser_token_headers, pub_fresh["bundle_uuid"])

    # ── Phase 2: Consumer installs ────────────────────────────────────────
    _, consumer_headers = make_user_and_headers(client)
    install = install_bundle(client, consumer_headers, bundle_id)
    install_id = install["id"]

    # ── Phase 3: Discovery → up to date at 1.0 ────────────────────────────
    target = _agent_target(
        _list_external_agents(client, consumer_headers), install_id
    )
    bv = target["bundle_version"]
    assert bv is not None, "consumer install must carry a bundle_version snapshot"
    assert bv["installed_version"] == "1.0"
    assert bv["latest_version"] == "1.0"
    assert bv["installed_revision_number"] == bv["latest_revision_number"]
    assert bv["update_available"] is False

    # ── Phase 4: Publisher ships v1.1 ─────────────────────────────────────
    _publish(client, superuser_token_headers, pub_agent["id"], version="1.1", notes="v2")

    # ── Phase 5: Discovery → update available (read-only derivation) ───────
    target = _agent_target(
        _list_external_agents(client, consumer_headers), install_id
    )
    bv = target["bundle_version"]
    assert bv["installed_version"] == "1.0"
    assert bv["latest_version"] == "1.1"
    assert bv["latest_revision_number"] > bv["installed_revision_number"]
    assert bv["update_available"] is True

    # ── Phase 6: check-updates reconciles pending_update ──────────────────
    r = client.post(
        f"{_EXT_BASE}/agents/{install_id}/check-updates", headers=consumer_headers
    )
    assert r.status_code == 200, r.text
    cu = r.json()
    assert cu["pending_update"] is True
    assert cu["installed_version"] == "1.0"
    assert cu["latest_version"] == "1.1"

    # ── Phase 7: apply-update → post-update snapshot at 1.1 ───────────────
    r = client.post(
        f"{_EXT_BASE}/agents/{install_id}/apply-update", headers=consumer_headers
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    applied = r.json()
    assert applied["installed_version"] == "1.1"
    assert applied["latest_version"] == "1.1"
    assert applied["update_available"] is False

    # ── Phase 8: Discovery → up to date at 1.1 ────────────────────────────
    target = _agent_target(
        _list_external_agents(client, consumer_headers), install_id
    )
    bv = target["bundle_version"]
    assert bv["installed_version"] == "1.1"
    assert bv["update_available"] is False


# ---------------------------------------------------------------------------
# Scenario 2: bundle_version is None where there is nothing to update + guards
# ---------------------------------------------------------------------------


def test_bundle_version_absent_for_non_installs_and_update_guards(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    bundle_version is populated only for the caller's own consumer installs:
      1. The publisher's own working copy carries bundle_version=None
         (it is the source of the bundle, not an install of it).
      2. A plain agent never published / installed from a bundle carries None.
      3. apply-update / check-updates are owner-gated (401 / 403) and 404 on
         an unknown agent id.
    """
    # ── Phase 1: Publisher working copy → bundle_version None ─────────────
    pub_agent = create_agent_via_api(
        client, superuser_token_headers, name="Publisher Working Copy"
    )
    drain_tasks()
    _publish(client, superuser_token_headers, pub_agent["id"], version="1.0")

    targets = _list_external_agents(client, superuser_token_headers)
    pub_target = _agent_target(targets, pub_agent["id"])
    assert pub_target["bundle_version"] is None, (
        "the publisher's own install is the bundle source, not an install of it"
    )

    # ── Phase 2: Plain (non-bundle) agent → bundle_version None ───────────
    plain = create_agent_via_api(
        client, superuser_token_headers, name="Plain Agent"
    )
    drain_tasks()
    plain_target = _agent_target(
        _list_external_agents(client, superuser_token_headers), plain["id"]
    )
    assert plain_target["bundle_version"] is None

    # ── Phase 3: Auth + ownership guards on the update endpoints ──────────
    # Unauthenticated → 401
    assert (
        client.post(f"{_EXT_BASE}/agents/{plain['id']}/apply-update").status_code
        == 401
    )
    assert (
        client.post(f"{_EXT_BASE}/agents/{plain['id']}/check-updates").status_code
        == 401
    )

    # A different (non-owner, non-superuser) user → 403
    _, other_headers = create_random_user_with_headers(client)
    assert (
        client.post(
            f"{_EXT_BASE}/agents/{plain['id']}/apply-update", headers=other_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{_EXT_BASE}/agents/{plain['id']}/check-updates", headers=other_headers
        ).status_code
        == 403
    )

    # Unknown agent id → 404
    ghost = str(uuid.uuid4())
    assert (
        client.post(
            f"{_EXT_BASE}/agents/{ghost}/apply-update",
            headers=superuser_token_headers,
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{_EXT_BASE}/agents/{ghost}/check-updates",
            headers=superuser_token_headers,
        ).status_code
        == 404
    )
