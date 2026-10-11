"""Deleting a cloud bundle removes its storage folder.

Bug 6 (bundle half) of the catalog-unification follow-up: ``data/bundles/<bundle
id>/`` stayed on disk after the bundle row was deleted. Every revision snapshot
lives under ``<BUNDLE_STORAGE_DIR>/<bundle_id>/<revision_number>/``; the folder
now goes with the row on every path that deletes the bundle.

``BUNDLE_STORAGE_DIR`` is a tmp tree here (``patch_storage_dirs`` autouse in
this domain's conftest), so the folders are real and can be inspected.

Scenarios:
  1. ``DELETE /bundles/{uuid}``: the folder goes; a 409 refusal (a foreign
     install exists) keeps it; another bundle's folder is untouched.
  2. ``DELETE /catalog/items/{uuid}`` on a cloud bundle (204): the folder goes.
  3. Revision deletes: removing one of two revisions keeps the bundle folder
     and the other snapshot; removing the last revision auto-deletes the
     bundle and its folder.

Notes:
  The name guard (``remove_data_subdir``) is unit-tested in
  ``tests/unit/test_data_dirs_remove_subdir.py``.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import make_person, publish_agent_bundle
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_bundle_public, make_user_and_headers, publish_bundle
from tests.utils.catalog import delete_item

API = settings.API_V1_STR


def _bundle_dir(bundle_id: str) -> Path:
    return Path(settings.BUNDLE_STORAGE_DIR) / bundle_id


def _revisions(client: TestClient, headers: dict[str, str], bundle_uuid: str) -> list[dict]:
    r = client.get(f"{API}/bundles/{bundle_uuid}/revisions", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_delete_bundle_removes_its_storage_folder_only_when_the_delete_happens(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Publisher publishes two bundles; both storage folders exist.
    2. Another user installs the first one; DELETE answers 409 and the
       folder stays.
    3. After the uninstall, DELETE answers 200 and the folder is gone.
    4. The second bundle's folder is untouched.
    """
    publisher = make_person(client, superuser_token_headers)
    doomed = publish_agent_bundle(client, publisher.headers, "storage-doomed")
    kept = publish_agent_bundle(client, publisher.headers, "storage-kept")
    assert _bundle_dir(doomed["bundle_id"]).is_dir()
    assert _bundle_dir(kept["bundle_id"]).is_dir()

    # ── Phase 2: a refused delete keeps the folder ────────────────────────
    make_bundle_public(client, publisher.headers, doomed["bundle_uuid"])
    _, installer = make_user_and_headers(client)
    r = client.post(f"{API}/catalog/{doomed['bundle_id']}/install", headers=installer, json={})
    assert r.status_code == 200, r.text
    install_id = r.json()["id"]
    drain_tasks()

    r = client.delete(f"{API}/bundles/{doomed['bundle_uuid']}", headers=publisher.headers)
    assert r.status_code == 409, r.text
    assert _bundle_dir(doomed["bundle_id"]).is_dir()

    # ── Phase 3: the delete that happens removes it ───────────────────────
    r = client.post(f"{API}/agents/{install_id}/uninstall", headers=installer)
    assert r.status_code == 200, r.text
    drain_tasks()
    r = client.delete(f"{API}/bundles/{doomed['bundle_uuid']}", headers=publisher.headers)
    assert r.status_code == 200, r.text

    assert not _bundle_dir(doomed["bundle_id"]).exists()
    assert client.get(
        f"{API}/bundles/{doomed['bundle_uuid']}", headers=publisher.headers
    ).status_code == 404

    # ── Phase 4: the other bundle is untouched ────────────────────────────
    assert _bundle_dir(kept["bundle_id"]).is_dir()
    assert any(_bundle_dir(kept["bundle_id"]).iterdir())


def test_delete_catalog_item_of_a_cloud_bundle_removes_its_storage_folder(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """DELETE /catalog/items/{uuid} (cloud branch) answers 204 and the bundle's
    folder is gone."""
    publisher = make_person(client, superuser_token_headers)
    bundle = publish_agent_bundle(client, publisher.headers, "storage-item")
    assert _bundle_dir(bundle["bundle_id"]).is_dir()

    r = delete_item(client, publisher.headers, bundle["bundle_uuid"])
    assert r.status_code == 204, r.text

    assert not _bundle_dir(bundle["bundle_id"]).exists()
    assert client.get(
        f"{API}/catalog/items/{bundle['bundle_uuid']}", headers=publisher.headers
    ).status_code == 404


def test_deleting_the_last_revision_removes_the_bundle_folder(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Publish twice: two revision snapshots under the bundle folder.
    2. Delete revision 1: the bundle and its folder stay, revision 2's
       snapshot stays.
    3. Delete revision 2 (the last one): the bundle is auto-deleted and its
       whole folder is gone.
    """
    publisher = make_person(client, superuser_token_headers)
    bundle = publish_agent_bundle(client, publisher.headers, "storage-revs")
    publish_bundle(client, publisher.headers, bundle["agent_id"], notes="second")
    folder = _bundle_dir(bundle["bundle_id"])

    revisions = {r["revision_number"]: r for r in _revisions(
        client, publisher.headers, bundle["bundle_uuid"]
    )}
    assert set(revisions) == {1, 2}
    assert folder.is_dir()

    # ── Phase 2: one of two revisions ─────────────────────────────────────
    r = client.delete(
        f"{API}/bundles/{bundle['bundle_uuid']}/revisions/{revisions[1]['id']}",
        headers=publisher.headers,
    )
    assert r.status_code == 200, r.text
    assert folder.is_dir()
    assert [rev["revision_number"] for rev in _revisions(
        client, publisher.headers, bundle["bundle_uuid"]
    )] == [2]

    # ── Phase 3: the last revision takes the bundle and folder with it ───
    r = client.delete(
        f"{API}/bundles/{bundle['bundle_uuid']}/revisions/{revisions[2]['id']}",
        headers=publisher.headers,
    )
    assert r.status_code == 200, r.text
    assert client.get(
        f"{API}/bundles/{bundle['bundle_uuid']}", headers=publisher.headers
    ).status_code == 404
    assert not folder.exists()
