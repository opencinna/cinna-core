"""Revisions: delete rules (D17) and downloads (P3).

Scenarios:
  1. Deleting a non-current revision removes it (row, folder and cached zip);
     the current one is 409; a revision pinned by a live link is 409 until the
     link is revoked, then it deletes (the dead link is unpinned).
  2. Downloads: the owner can download any revision, the zip is built once
     and cached; unknown revisions 404.
"""
import io
import zipfile

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.artifacts import (
    build_zip,
    create_link,
    get_artifact,
    publish,
    republish,
    revoke_link,
)
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR


def _zip(text: str) -> bytes:
    return build_zip({"index.html": f"<p>{text}</p>"})


def test_revision_delete_rules(client: TestClient, artifact_storage) -> None:
    _, owner = create_random_user_with_headers(client)
    _, other = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_zip("1"))
    for text in ("2", "3"):
        republish(client, owner, a["id"], archive=_zip(text))
    revisions = {r["number"]: r["id"] for r in get_artifact(client, owner, a["id"])["revisions"]}
    base = f"{API}/artifacts/{a['id']}/revisions"

    # ── current ⇒ 409; non-owner ⇒ 404; unknown ⇒ 404 ──
    assert client.delete(f"{base}/3", headers=owner).status_code == 409
    assert client.delete(f"{base}/1", headers=other).status_code == 404
    assert client.delete(f"{base}/9", headers=owner).status_code == 404

    # ── pinned by a live link ⇒ 409, after revoke ⇒ deletes ──
    link = create_link(client, owner, a["id"], pinned_revision_id=revisions[1])
    assert client.delete(f"{base}/1", headers=owner).status_code == 409
    revoke_link(client, owner, a["id"], link["id"])

    # cache the zip of revision 1 so its removal is observable
    assert client.get(f"{API}/artifacts/{a['id']}/download?rev=1", headers=owner).status_code == 200
    assert (artifact_storage / a["id"] / f"{revisions[1]}.zip").is_file()
    r = client.delete(f"{base}/1", headers=owner)
    assert r.status_code == 200, r.text
    assert not (artifact_storage / a["id"] / revisions[1]).exists()
    assert not (artifact_storage / a["id"] / f"{revisions[1]}.zip").exists()

    # ── plain non-current delete ──
    assert client.delete(f"{base}/2", headers=owner).status_code == 200
    detail = get_artifact(client, owner, a["id"])
    assert [r["number"] for r in detail["revisions"]] == [3]
    assert detail["revision_count"] == 1
    # the revoked link survives, unpinned
    assert detail["links"][0]["pinned_revision_id"] is None


def test_download_any_revision_as_owner(client: TestClient, artifact_storage) -> None:
    _, owner = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_zip("1"), title="My/Report")
    republish(client, owner, a["id"], archive=_zip("2"))

    for rev, text in ((None, b"<p>2</p>"), (1, b"<p>1</p>")):
        url = f"{API}/artifacts/{a['id']}/download" + (f"?rev={rev}" if rev else "")
        r = client.get(url, headers=owner)
        assert r.status_code == 200
        assert r.headers["content-disposition"].startswith("attachment")
        assert "My_Report" in r.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert zf.read("index.html") == text

    zips = sorted(p.name for p in (artifact_storage / a["id"]).glob("*.zip"))
    assert len(zips) == 2
    assert client.get(f"{API}/artifacts/{a['id']}/download?rev=7", headers=owner).status_code == 404
