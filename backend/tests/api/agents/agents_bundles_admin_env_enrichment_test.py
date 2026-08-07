"""Admin agent-environments list — bundle enrichment (Phase 3 of the
``bundle_auto_update_and_install_ux`` plan).

``AdminAgentEnvironmentPublic`` gains ``bundle_id``, ``is_publisher_install``,
``update_mode``, ``installed_revision_number/version``,
``latest_revision_number/version``, and a computed ``update_available`` flag;
``GET /admin/agent-environments/`` gains an ``update_available`` query filter.

Placed alongside the other bundle tests (per the plan's test section) rather
than in ``tests/api/agent_environments/`` — this directory's conftest.py
patches ``app.services.bundles.install_service.create_session`` and collects
``app.utils.create_task_with_error_logging`` (``CREATE_SESSION_TARGETS_AGENT`` /
``BACKGROUND_TASK_TARGETS_FULL``), which the bundle-publish flow used to set
up these fixtures depends on for its publish-time auto-update fast path
background task to drain cleanly.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle as _install,
    make_user_and_headers as _make_user_and_headers,
    publish_bundle_and_make_public as _publish_and_make_public,
    publish_bundle_revision as _publish,
)
from tests.utils.environment import list_environments

API = settings.API_V1_STR
_ADMIN_BASE = f"{API}/admin/agent-environments"


def _admin_list(client: TestClient, headers: dict[str, str], **params) -> dict:
    r = client.get(_ADMIN_BASE + "/", headers=headers, params=params)
    assert r.status_code == 200, f"Admin list failed: {r.text}"
    return r.json()


def _env_id_for_agent(client: TestClient, headers: dict[str, str], agent_id: str) -> str:
    result = list_environments(client, headers, agent_id)
    return result["data"][0]["id"]


def _row_by_env_id(body: dict, env_id: str) -> dict:
    matches = [r for r in body["data"] if r["id"] == env_id]
    assert len(matches) == 1, f"Expected exactly one row for env {env_id}, got {matches}"
    return matches[0]


def test_admin_env_list_bundle_enrichment_and_update_available_filter(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Scenario 11 (plan §7): bundle enrichment fields + update_available filter.

      1. Non-bundle agent (never published/installed): bundle_id=None,
         is_publisher_install=False, update_available=False.
      2. Publisher install (rev 1, then republished to rev 2): bundle_id set,
         is_publisher_install=True, installed==latest always,
         update_available=False even though it carries a real bundle.
      3. Foreign consumer install caught up on rev 1: update_available=False.
      4. After the publisher republishes to rev 2, the SAME consumer install
         (still on rev 1) becomes update_available=True with correct
         installed/latest revision numbers + versions.
      5. The ``update_available`` filter includes exactly the behind row and
         excludes the rest.
    """
    # ── Phase 1: non-bundle agent ──────────────────────────────────────────
    standalone = create_agent_via_api(
        client, superuser_token_headers, name="Standalone Non-Bundle Agent"
    )
    drain_tasks()
    standalone_env_id = _env_id_for_agent(client, superuser_token_headers, standalone["id"])

    # ── Phase 2: publisher publishes rev 1, flips bundle public ────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Admin Enrichment Bundle",
    )
    drain_tasks()
    revision1 = _publish_and_make_public(
        client, superuser_token_headers, publisher_agent["id"],
        notes="v1", visibility="public", is_listed=True,
    )
    fresh_publisher = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = fresh_publisher["bundle_id"]
    publisher_env_id = _env_id_for_agent(
        client, superuser_token_headers, publisher_agent["id"]
    )

    # ── Phase 3: foreign consumer installs rev 1 (caught up) ───────────────
    _, consumer_headers = _make_user_and_headers(client)
    consumer_install = _install(client, consumer_headers, bundle_id)
    consumer_env_id = _env_id_for_agent(client, consumer_headers, consumer_install["id"])

    body = _admin_list(client, superuser_token_headers)

    # -- Phase 1 assertions: non-bundle row --
    standalone_row = _row_by_env_id(body, standalone_env_id)
    assert standalone_row["bundle_id"] is None
    assert standalone_row["is_publisher_install"] is False
    assert standalone_row["update_mode"] == "manual"  # Agent.update_mode default
    assert standalone_row["installed_revision_number"] is None
    assert standalone_row["latest_revision_number"] is None
    assert standalone_row["update_available"] is False

    # -- Phase 2 assertions: publisher row (rev 1, up to date) --
    publisher_row = _row_by_env_id(body, publisher_env_id)
    assert publisher_row["bundle_id"] == bundle_id
    assert publisher_row["is_publisher_install"] is True
    assert publisher_row["installed_revision_number"] == 1
    assert publisher_row["latest_revision_number"] == 1
    assert publisher_row["update_available"] is False

    # -- Phase 3 assertions: consumer row (rev 1, up to date) --
    consumer_row = _row_by_env_id(body, consumer_env_id)
    assert consumer_row["bundle_id"] == bundle_id
    assert consumer_row["is_publisher_install"] is False
    assert consumer_row["installed_revision_number"] == 1
    assert consumer_row["latest_revision_number"] == 1
    assert consumer_row["update_available"] is False

    # ── Phase 4: publisher republishes → consumer falls behind ─────────────
    revision2 = _publish(
        client, superuser_token_headers, publisher_agent["id"], notes="v2"
    )
    assert revision2["revision_number"] == 2

    body2 = _admin_list(client, superuser_token_headers)

    publisher_row2 = _row_by_env_id(body2, publisher_env_id)
    assert publisher_row2["installed_revision_number"] == 2
    assert publisher_row2["latest_revision_number"] == 2
    assert publisher_row2["update_available"] is False

    consumer_row2 = _row_by_env_id(body2, consumer_env_id)
    assert consumer_row2["installed_revision_number"] == 1
    assert consumer_row2["latest_revision_number"] == 2
    assert consumer_row2["installed_revision_version"] == consumer_row["installed_revision_version"]
    assert consumer_row2["update_available"] is True

    standalone_row2 = _row_by_env_id(body2, standalone_env_id)
    assert standalone_row2["update_available"] is False

    # ── Phase 5: update_available filter narrows correctly ─────────────────
    behind_body = _admin_list(client, superuser_token_headers, update_available=True)
    behind_ids = {r["id"] for r in behind_body["data"]}
    assert consumer_env_id in behind_ids
    assert publisher_env_id not in behind_ids
    assert standalone_env_id not in behind_ids
    assert all(r["update_available"] is True for r in behind_body["data"])

    not_behind_body = _admin_list(client, superuser_token_headers, update_available=False)
    not_behind_ids = {r["id"] for r in not_behind_body["data"]}
    assert consumer_env_id not in not_behind_ids
    assert publisher_env_id in not_behind_ids
    assert standalone_env_id in not_behind_ids
    assert all(r["update_available"] is False for r in not_behind_body["data"])
