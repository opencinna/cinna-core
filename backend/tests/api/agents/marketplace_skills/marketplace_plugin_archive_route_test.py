"""``GET /llm-plugins/plugins/{id}/archive`` — any format, `/discover` visibility."""
from fastapi.testclient import TestClient

from tests.api.agents.marketplace_skills.marketplace_plugin_snapshots_test import (
    EXT_URL,
    _ext_entry,
    _ext_tree,
    _local_entry,
    _new_market,
    _row,
)
from tests.utils.llm_plugin import (
    PLUGINS_BASE,
    claude_repo,
    get_plugin_archive,
    stubbed_plugin_fetch,
)
from tests.utils.marketplace_skills import (
    create_second_superuser,
    new_skills_marketplace,
    rows_of,
    sha_of,
    skills_repo,
    tar_members,
)

UNKNOWN = "00000000-0000-4000-8000-000000000001"


def _detail(r) -> dict:
    return r.json()["detail"]


def test_archive_route_serves_plugin_and_skill_rows_with_errors(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Plugin row: bytes match the digest, header set, given digest works, unknown digest 404 snapshot_missing.
    2. Skills row: served through the same route.
    3. Pending (fetch failing) 409 snapshot_pending; content verdict 409 snapshot_failed with reason;
       no snapshot at all 404 snapshot_missing is not reachable for claude rows, unknown id 404 not_found.
    4. skill-archive on a plugin row is still 409 not_a_skill.
    """
    h = superuser_token_headers
    populate = claude_repo(
        [_local_entry("good"), _local_entry("empty"), _ext_entry("later")],
        {"plugins/good/commands/g.md": "g", "plugins/empty/README.md": "x"},
    )
    with stubbed_plugin_fetch({EXT_URL: _ext_tree()}) as fetch:
        fetch.fail_urls.add(EXT_URL)
        m = _new_market(client, h, populate)

    good, empty, later = (_row(client, h, m["id"], n) for n in ("good", "empty", "later"))

    r = get_plugin_archive(client, h, good["id"])
    assert r.status_code == 200, r.text
    assert sha_of(r.content) == good["archive_sha256"]
    assert r.headers["X-Archive-Sha256"] == good["archive_sha256"]
    assert [x.name for x in tar_members(r.content)] == ["good/commands/g.md"]
    assert get_plugin_archive(client, h, good["id"], sha256=good["archive_sha256"]).status_code == 200
    r = get_plugin_archive(client, h, good["id"], sha256="0" * 64)
    assert r.status_code == 404 and _detail(r)["code"] == "snapshot_missing"

    r = get_plugin_archive(client, h, later["id"])
    assert r.status_code == 409 and _detail(r)["code"] == "snapshot_pending"
    r = get_plugin_archive(client, h, empty["id"])
    assert r.status_code == 409
    assert _detail(r) == {"code": "snapshot_failed", "reason": "no_components"}
    r = get_plugin_archive(client, h, UNKNOWN)
    assert r.status_code == 404 and _detail(r)["code"] == "not_found"

    r = client.get(f"{PLUGINS_BASE}/plugins/{good['id']}/skill-archive", headers=h)
    assert r.status_code == 409 and _detail(r)["code"] == "not_a_skill"

    sm = new_skills_marketplace(client, h, skills_repo(("one",), broken=()))
    srow = rows_of(client, h, sm["id"])["one"]
    r = get_plugin_archive(client, h, srow["id"])
    assert r.status_code == 200
    assert sha_of(r.content) == srow["skill_archive_sha256"]
    assert r.headers["X-Archive-Sha256"] == srow["skill_archive_sha256"]


def test_archive_route_respects_marketplace_visibility(
    client: TestClient, superuser_token_headers: dict[str, str], normal_user_token_headers: dict[str, str]
) -> None:
    """A private marketplace's archive is 404 to everyone else (no superuser bypass); public is open; anon rejected."""
    h = superuser_token_headers
    _, other_super = create_second_superuser(client, h)
    files = {"plugins/p/commands/c.md": "c"}
    from tests.utils.llm_plugin import create_marketplace, sync_marketplace

    priv = create_marketplace(client, h, url="https://github.com/example/priv-arch.git", public_discovery=False)
    sync_marketplace(client, h, priv["id"], populate=claude_repo([_local_entry("p")], files))
    pub = create_marketplace(client, h, url="https://github.com/example/pub-arch.git", public_discovery=True)
    sync_marketplace(client, h, pub["id"], populate=claude_repo([_local_entry("p")], files))
    priv_id = _row(client, h, priv["id"], "p")["id"]
    pub_id = _row(client, h, pub["id"], "p")["id"]

    assert get_plugin_archive(client, h, priv_id).status_code == 200
    for who in (normal_user_token_headers, other_super):
        r = get_plugin_archive(client, who, priv_id)
        assert r.status_code == 404 and _detail(r)["code"] == "not_found"
        assert get_plugin_archive(client, who, pub_id).status_code == 200
    assert client.get(f"{PLUGINS_BASE}/plugins/{pub_id}/archive").status_code in (401, 403)
