"""End-to-end tests for the Phase 2 Agent Bundles & Installs flow.

Covers:
- Publish creates an ``AgentBundle`` + first ``AgentBundleRevision``,
  promotes the install to ``is_publisher_install=True``, links
  ``bundle_uuid`` and ``installed_revision_id``.
- A second user can see public+listed bundles in the catalog and install
  them via ``POST /catalog/{bundle_id}/install``; the install row carries
  the right linkage and a foreign install of the same bundle is
  idempotent.
- Republishing creates a new revision and flags ``pending_update`` on
  foreign installs (manual update mode).
- ``apply-update`` clears the flag and bumps the install's
  ``installed_revision_id``.
- Uninstall marks the per-user app-data volume orphaned and deletes the
  install row.
- Edit bundle id pre-publish works; post-publish returns 409.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user, user_authentication_headers


API = settings.API_V1_STR
APP_DATA_BASE = f"{API}/users/me/app-data"


def _make_user_and_headers(client: TestClient) -> tuple[dict, dict[str, str]]:
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    # Install needs an AI credential default to provision the env. Mirrors
    # the pattern used by the agent management tests for fresh users.
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _publish(
    client: TestClient,
    headers: dict,
    agent_id: str,
    *,
    notes: str | None = None,
    visibility: str = "public",
    is_listed: bool = True,
) -> dict:
    """Publish an install + (default) flip listing/visibility for catalog tests."""
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={"release_notes": notes} if notes else {},
    )
    assert r.status_code == 200, r.text
    revision = r.json()
    drain_tasks()

    # Make the bundle catalog-visible. The publish endpoint produces the
    # revision; bundle metadata edit is on /bundles/{uuid}.
    fresh = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    bundle_uuid = fresh["bundle_uuid"]
    assert bundle_uuid is not None
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=headers,
        json={"is_listed": is_listed, "visibility": visibility},
    )
    assert r.status_code == 200, r.text
    return revision


def test_publish_creates_bundle_and_revision(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """First publish: bundle row + revision 1 + install promoted to publisher."""
    agent = create_agent_via_api(client, superuser_token_headers, name="Publishable")
    drain_tasks()
    revision = _publish(
        client, superuser_token_headers, agent["id"], notes="initial"
    )

    assert revision["revision_number"] == 1
    assert revision["release_notes"] == "initial"
    assert revision["content_hash"]

    fresh = client.get(
        f"{API}/agents/{agent['id']}", headers=superuser_token_headers
    ).json()
    assert fresh["is_publisher_install"] is True
    assert fresh["bundle_uuid"] is not None
    assert fresh["installed_revision_id"] == revision["id"]
    assert fresh["installed_revision_number"] == 1

    # Bundle now appears in publisher's bundles list.
    bundles = client.get(f"{API}/bundles/", headers=superuser_token_headers).json()
    matched = [b for b in bundles["data"] if b["bundle_id"] == fresh["bundle_id"]]
    assert len(matched) == 1
    bundle = matched[0]
    assert bundle["latest_revision_number"] == 1
    assert bundle["install_count"] == 1  # publisher's install


def test_install_from_catalog_for_other_user(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Foreign user installs a public bundle; verify linkage + idempotence."""
    # Publisher creates and publishes.
    agent = create_agent_via_api(
        client, superuser_token_headers, name="Catalog Bundle"
    )
    drain_tasks()
    _publish(client, superuser_token_headers, agent["id"])
    fresh = client.get(
        f"{API}/agents/{agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = fresh["bundle_id"]

    # Recipient user.
    _, recipient_headers = _make_user_and_headers(client)

    # Catalog lists the public bundle.
    catalog = client.get(f"{API}/catalog/", headers=recipient_headers).json()
    seen = [e for e in catalog["data"] if e["bundle_id"] == bundle_id]
    assert len(seen) == 1
    entry = seen[0]
    assert entry["is_installed"] is False
    assert entry["latest_revision_number"] == 1

    # Install.
    r = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=recipient_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    install = r.json()
    drain_tasks()
    assert install["bundle_id"] == bundle_id
    assert install["bundle_uuid"] == fresh["bundle_uuid"]
    assert install["installed_revision_id"] == fresh["installed_revision_id"]
    assert install["is_publisher_install"] is False
    assert install["installed_revision_number"] == 1

    # Idempotent — second install returns the same row.
    r = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=recipient_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    again = r.json()
    assert again["id"] == install["id"]


def test_republish_flags_pending_update_and_apply_clears_it(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Second publish flips foreign install's ``pending_update``; apply clears it."""
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Updatable"
    )
    drain_tasks()
    _publish(client, superuser_token_headers, publisher_agent["id"])
    fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = fresh["bundle_id"]

    _, recipient_headers = _make_user_and_headers(client)
    install = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=recipient_headers,
        json={},
    ).json()
    drain_tasks()
    install_id = install["id"]
    assert install["pending_update"] is False

    # Second publish.
    r = client.post(
        f"{API}/agents/{publisher_agent['id']}/publish",
        headers=superuser_token_headers,
        json={"release_notes": "v2"},
    )
    assert r.status_code == 200, r.text
    revision_2 = r.json()
    drain_tasks()
    assert revision_2["revision_number"] == 2

    # Foreign install now flagged.
    install = client.get(f"{API}/agents/{install_id}", headers=recipient_headers).json()
    assert install["pending_update"] is True

    check = client.post(
        f"{API}/agents/{install_id}/check-updates", headers=recipient_headers
    ).json()
    assert check["pending_update"] is True
    assert check["installed_revision_number"] == 1
    assert check["latest_revision_number"] == 2

    # Apply update.
    r = client.post(
        f"{API}/agents/{install_id}/apply-update", headers=recipient_headers
    )
    assert r.status_code == 200, r.text
    install = r.json()
    drain_tasks()
    assert install["pending_update"] is False
    assert install["installed_revision_id"] == revision_2["id"]
    assert install["installed_revision_number"] == 2
    assert install["last_update_status"] == "synced"


def test_uninstall_marks_app_data_orphaned(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Uninstalling a foreign install orphans the user's app-data volume."""
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="With App Data"
    )
    drain_tasks()
    _publish(client, superuser_token_headers, publisher_agent["id"])
    pub_fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = pub_fresh["bundle_id"]

    _, recipient_headers = _make_user_and_headers(client)
    install = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=recipient_headers,
        json={},
    ).json()
    drain_tasks()

    # Recipient app-data volume present + attached.
    listing = client.get(APP_DATA_BASE, headers=recipient_headers).json()
    matched = [v for v in listing["data"] if v["bundle_id"] == bundle_id]
    assert len(matched) == 1
    volume = matched[0]
    assert volume["is_orphaned"] is False
    assert volume["current_install_id"] == install["id"]

    # Uninstall.
    r = client.post(
        f"{API}/agents/{install['id']}/uninstall", headers=recipient_headers
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    listing = client.get(APP_DATA_BASE, headers=recipient_headers).json()
    matched = [v for v in listing["data"] if v["id"] == volume["id"]]
    assert len(matched) == 1
    assert matched[0]["is_orphaned"] is True
    assert matched[0]["current_install_id"] is None


def test_edit_bundle_id_prepublish_then_locked_after_publish(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Edit allowed pre-publish; rejected with 409 once a revision exists."""
    agent = create_agent_via_api(
        client, superuser_token_headers, name="Bundle ID Editable"
    )
    drain_tasks()
    new_id = "io.test.bundle." + uuid.uuid4().hex[:8]

    # Pre-publish edit.
    r = client.patch(
        f"{API}/agents/{agent['id']}/bundle-id",
        headers=superuser_token_headers,
        json={"bundle_id": new_id},
    )
    assert r.status_code == 200, r.text
    fresh = r.json()
    assert fresh["bundle_id"] == new_id
    assert fresh["bundle_uuid"] is None  # not yet published

    # Reserved prefix rejected.
    r = client.patch(
        f"{API}/agents/{agent['id']}/bundle-id",
        headers=superuser_token_headers,
        json={"bundle_id": "io.opencinna.system.foo"},
    )
    assert r.status_code == 400

    # Publish and re-attempt.
    _publish(client, superuser_token_headers, agent["id"])
    r = client.patch(
        f"{API}/agents/{agent['id']}/bundle-id",
        headers=superuser_token_headers,
        json={"bundle_id": "io.test.different." + uuid.uuid4().hex[:8]},
    )
    assert r.status_code == 409


def test_visibility_users_requires_grant(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Bundles with visibility=users only show to grant recipients."""
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Restricted Bundle"
    )
    drain_tasks()
    _publish(
        client,
        superuser_token_headers,
        publisher_agent["id"],
        visibility="users",
    )
    pub_fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = pub_fresh["bundle_id"]
    bundle_uuid = pub_fresh["bundle_uuid"]

    user_a, headers_a = _make_user_and_headers(client)
    user_b, headers_b = _make_user_and_headers(client)

    # Neither user sees the bundle yet.
    catalog_a = client.get(f"{API}/catalog/", headers=headers_a).json()
    assert not [e for e in catalog_a["data"] if e["bundle_id"] == bundle_id]
    catalog_b = client.get(f"{API}/catalog/", headers=headers_b).json()
    assert not [e for e in catalog_b["data"] if e["bundle_id"] == bundle_id]

    # Publisher grants user A.
    r = client.post(
        f"{API}/bundles/{bundle_uuid}/grants",
        headers=superuser_token_headers,
        json={"email": user_a["email"]},
    )
    assert r.status_code == 200, r.text
    grant = r.json()
    assert grant["user_email"] == user_a["email"]

    # User A now sees + can install; user B still cannot.
    catalog_a = client.get(f"{API}/catalog/", headers=headers_a).json()
    assert any(e["bundle_id"] == bundle_id for e in catalog_a["data"])
    catalog_b = client.get(f"{API}/catalog/", headers=headers_b).json()
    assert not [e for e in catalog_b["data"] if e["bundle_id"] == bundle_id]

    r = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=headers_b,
        json={},
    )
    assert r.status_code == 403, r.text


def test_delete_bundle_blocked_with_foreign_installs(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Bundle delete is rejected while any foreign install exists."""
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Delete Blocked"
    )
    drain_tasks()
    _publish(client, superuser_token_headers, publisher_agent["id"])
    pub_fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = pub_fresh["bundle_id"]
    bundle_uuid = pub_fresh["bundle_uuid"]

    _, recipient_headers = _make_user_and_headers(client)
    install = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=recipient_headers,
        json={},
    ).json()
    drain_tasks()

    r = client.delete(
        f"{API}/bundles/{bundle_uuid}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 409, r.text
    assert "install" in r.json()["detail"].lower()

    # After uninstall the foreign install, delete should work.
    client.post(f"{API}/agents/{install['id']}/uninstall", headers=recipient_headers)
    drain_tasks()
    r = client.delete(
        f"{API}/bundles/{bundle_uuid}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text
