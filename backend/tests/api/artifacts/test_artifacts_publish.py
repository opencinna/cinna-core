"""Publishing and republishing artifacts (P3).

Scenarios:
  1. Republish keeps the id and page URL, adds revision 2, makes it current,
     reports its own ``excluded``; the start page survives when still present
     and the kind follows the new files otherwise; old caps keep serving their
     own revision.
  2. Only the owner can republish (another user — even one who can view —
     gets 404); title/description travel with a republish.
  3. Quota counts every revision; the backend body cap refuses an oversize
     request before parsing; capabilities report the limits.
  4. Workspaces: a foreign workspace is refused; the list filters by
     workspace (``""`` = default) and by ``source_key``.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.artifacts import (
    build_zip,
    entry_url,
    get_artifact,
    list_artifacts,
    publish,
    republish,
    set_grants,
    update_artifact,
    view,
)
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR


def test_republish_keeps_identity_and_adds_a_revision(client: TestClient) -> None:
    _, headers = create_random_user_with_headers(client)
    v1_zip = build_zip({"index.html": "<p>v1</p>", "about.html": "<p>about</p>"})
    created = publish(client, headers, archive=v1_zip, title="Report", entry_path="about.html")
    v1 = view(client, headers, created["id"])

    # ── Phase 1: republish with a .env — same id/url, revision 2, excluded ──
    v2_zip = build_zip({"index.html": "<p>v2</p>", "about.html": "<p>about 2</p>", ".env": "S=1"})
    again = republish(client, headers, created["id"], archive=v2_zip, note="second")
    assert again["id"] == created["id"]
    assert again["url"] == created["url"]
    assert again["revision_number"] == 2
    assert again["current_revision_number"] == 2
    assert again["revision_count"] == 2
    assert again["excluded"] == [".env"]
    # the explicit start page survives a republish that still contains it
    assert (again["kind"], again["entry_path"]) == ("site", "about.html")

    detail = get_artifact(client, headers, created["id"])
    assert [r["number"] for r in detail["revisions"]] == [2, 1]
    assert detail["revisions"][0]["is_current"] and detail["revisions"][0]["note"] == "second"
    assert detail["revisions"][0]["excluded"] == [".env"]

    # ── Phase 2: the old cap still serves revision 1, a new view serves 2 ──
    assert client.get(entry_url(v1)).content == b"<p>about</p>"
    v2 = view(client, headers, created["id"])
    assert v2["revision_number"] == 2
    assert client.get(entry_url(v2)).content == b"<p>about 2</p>"
    assert view(client, headers, created["id"], rev=1)["revision_number"] == 1

    # ── Phase 3: files without the start page ⇒ the kind follows the files ──
    third = republish(client, headers, created["id"], files={"data.csv": b"a,b"}, title="Renamed")
    assert (third["kind"], third["entry_path"]) == ("files", None)
    assert third["title"] == "Renamed"
    assert third["revision_number"] == 3


def test_only_the_owner_republishes(client: TestClient) -> None:
    _, owner = create_random_user_with_headers(client)
    viewer, viewer_headers = create_random_user_with_headers(client)
    created = publish(client, owner, files={"a.txt": b"a"})
    set_grants(client, owner, created["id"], [viewer["id"]])
    assert get_artifact(client, viewer_headers, created["id"])["is_owner"] is False

    republish(client, viewer_headers, created["id"], files={"a.txt": b"b"}, expect=404)
    update_artifact(client, viewer_headers, created["id"], title="Mine now", expect=404)
    assert get_artifact(client, owner, created["id"])["revision_count"] == 1


def test_quota_counts_every_revision_and_body_cap(client: TestClient) -> None:
    _, headers = create_random_user_with_headers(client)

    caps = client.get(f"{API}/artifacts/capabilities", headers=headers).json()
    assert caps["version"] == 1
    assert caps["limits"]["max_revision_files"] == settings.ARTIFACT_MAX_REVISION_FILES
    assert caps["limits"]["user_quota_bytes"] == settings.ARTIFACT_USER_QUOTA_BYTES

    with patch.object(settings, "ARTIFACT_USER_QUOTA_BYTES", 2500):
        created = publish(client, headers, files={"a.bin": b"x" * 1000})
        republish(client, headers, created["id"], files={"a.bin": b"y" * 1000})
        # 2000 used; another 1000 crosses 2500
        republish(client, headers, created["id"], files={"a.bin": b"z" * 1000}, expect=413)
        publish(client, headers, files={"b.bin": b"z" * 1000}, expect=413)
    assert get_artifact(client, headers, created["id"])["revision_count"] == 2

    # the backend body cap answers before the multipart body is parsed
    with (
        patch.object(settings, "ARTIFACT_MAX_REVISION_BYTES", 1000),
        patch.object(settings, "ARTIFACT_UPLOAD_OVERHEAD_BYTES", 1000),
    ):
        r = client.post(
            f"{API}/artifacts",
            headers=headers,
            data={"title": "Big"},
            files=[("files", ("big.bin", b"x" * 5000, "application/octet-stream"))],
        )
        assert r.status_code == 413
        assert "Upload body is larger" in r.json()["detail"]


def test_workspaces_and_filters(client: TestClient) -> None:
    _, headers = create_random_user_with_headers(client)
    _, other_headers = create_random_user_with_headers(client)
    r = client.post(f"{API}/user-workspaces/", headers=headers, json={"name": "Reports"})
    assert r.status_code == 200, r.text
    workspace_id = r.json()["id"]
    r = client.post(f"{API}/user-workspaces/", headers=other_headers, json={"name": "Theirs"})
    foreign_workspace = r.json()["id"]

    in_ws = publish(client, headers, files={"a.txt": b"a"}, title="In workspace", user_workspace_id=workspace_id)
    assert in_ws["user_workspace_id"] == workspace_id
    default = publish(client, headers, files={"b.txt": b"b"}, title="Default")
    publish(client, headers, files={"c.txt": b"c"}, user_workspace_id=foreign_workspace, expect=400)

    ids = lambda page: [a["id"] for a in page["data"]]  # noqa: E731
    assert ids(list_artifacts(client, headers, user_workspace_id=workspace_id)) == [in_ws["id"]]
    assert ids(list_artifacts(client, headers, user_workspace_id="")) == [default["id"]]
    assert set(ids(list_artifacts(client, headers))) == {in_ws["id"], default["id"]}
    # source_key is only set by agent publishes (P6): an upload never matches
    assert list_artifacts(client, headers, source_key="env:/workspace/out")["count"] == 0
    r = client.get(f"{API}/artifacts", headers=headers, params={"user_workspace_id": "nope"})
    assert r.status_code == 400
