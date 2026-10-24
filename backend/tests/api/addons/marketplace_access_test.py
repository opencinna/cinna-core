"""K5 — marketplace access mode, per-entry allow / ban.

Scenarios:
  1. ``access_mode`` is required on create (422), echoed on the row.
  2. Allowlist: synced entries wait (absent from discover, batch, directory,
     both archive routes, the plain GET for a normal user), show in the entries
     route with ``waiting=true`` and a ready snapshot; allowing one makes it
     visible everywhere, and the superuser has no bypass on the user routes.
  3. allow_all: ban hides everywhere, ``default`` brings it back; user rows
     show ``default|allowed`` with the audit fields blanked.
  4. State is keyed by entry name: it survives a resync and an entry removed
     then re-added (new row id).
  5. Mode switch: a dry run reports counts and changes nothing, an applied
     switch reports the same counts and keeps the verdicts.
  6. Bulk: counts, ``foreign_ids`` (all-or-nothing), unknown marketplace /
     plugin, validation.
  7. Superuser-only on all three write routes.
  8. A5: a cloud link to a banned plugin still lists under the agent.
"""
import uuid

from fastapi.testclient import TestClient

from tests.utils.addons import directory_names, get_directory
from tests.utils.llm_plugin import (
    PLUGINS_BASE,
    claude_repo,
    create_marketplace,
    discover_plugins,
    get_plugin_archive,
    install_agent_plugin,
    list_agent_plugins,
    list_marketplace_entries,
    plugins_by_name,
    sync_marketplace,
)
from tests.utils.skill_catalog import make_agent_with_env


def _entry(name: str) -> dict:
    return {"name": name, "description": f"{name} plugin", "source": f"./plugins/{name}"}


def _files(*names: str) -> dict:
    return {f"plugins/{n}/commands/run.md": f"run {n}" for n in names}


def _market(client, h, names, *, mode="allowlist", tag=None) -> dict:
    m = create_marketplace(
        client,
        h,
        url=f"https://github.com/example/access-{tag or uuid.uuid4().hex[:8]}.git",
        access_mode=mode,
    )
    sync_marketplace(
        client, h, m["id"], populate=claude_repo([_entry(n) for n in names], _files(*names))
    )
    return m


def _resync(client, h, m, names) -> None:
    sync_marketplace(
        client, h, m["id"], populate=claude_repo([_entry(n) for n in names], _files(*names))
    )


def _entries(client, h, m, **params) -> dict[str, dict]:
    return {r["name"]: r for r in list_marketplace_entries(client, h, m["id"], **params)["data"]}


def _set_access(client, h, plugin_id, access, *, reason=None, expected=200) -> dict:
    body: dict = {"access": access}
    if reason is not None:
        body["reason"] = reason
    r = client.put(f"{PLUGINS_BASE}/plugins/{plugin_id}/access", headers=h, json=body)
    assert r.status_code == expected, r.text
    return r.json()


def _bulk(client, h, mid, ids, access, *, expected=200, reason=None) -> dict:
    body: dict = {"ids": ids, "access": access}
    if reason is not None:
        body["reason"] = reason
    r = client.put(f"{PLUGINS_BASE}/marketplaces/{mid}/entries/access", headers=h, json=body)
    assert r.status_code == expected, r.text
    return r.json()


def _mode(client, h, mid, mode, *, dry_run=False, expected=200) -> dict:
    r = client.put(
        f"{PLUGINS_BASE}/marketplaces/{mid}/access-mode",
        headers=h,
        json={"access_mode": mode, "dry_run": dry_run},
    )
    assert r.status_code == expected, r.text
    return r.json()


def _batch(client, h, ids) -> list[str]:
    r = client.get(f"{PLUGINS_BASE}/plugins", headers=h, params={"ids": ",".join(ids)})
    assert r.status_code == 200, r.text
    return [p["name"] for p in r.json()["data"]]


def _visible_everywhere(client, h, plugin) -> dict[str, bool]:
    """Where a plugin shows up for the caller ``h``."""
    return {
        "discover": plugin["name"] in plugins_by_name(discover_plugins(client, h)),
        "batch": plugin["name"] in _batch(client, h, [plugin["id"]]),
        "directory": plugin["name"]
        in directory_names(get_directory(client, h, search=plugin["name"], limit=100)),
        "archive": get_plugin_archive(client, h, plugin["id"]).status_code == 200,
    }


def _plugin_count(client, h, m) -> int:
    rows = client.get(f"{PLUGINS_BASE}/marketplaces", headers=h).json()["data"]
    listed = next(r for r in rows if r["id"] == m["id"])
    single = client.get(f"{PLUGINS_BASE}/marketplaces/{m['id']}", headers=h).json()
    assert listed["plugin_count"] == single["plugin_count"]
    return listed["plugin_count"]


def test_create_requires_access_mode_and_echoes_it(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    create_marketplace(client, h, access_mode=None, expected_status=422)
    create_marketplace(client, h, access_mode="everything", expected_status=422)
    m = create_marketplace(client, h, url="https://github.com/example/m1.git", access_mode="allowlist")
    assert m["access_mode"] == "allowlist"
    got = client.get(f"{PLUGINS_BASE}/marketplaces/{m['id']}", headers=h).json()
    assert got["access_mode"] == "allowlist"


def test_allowlist_hides_new_entries_until_allowed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    """
    1. Two entries synced into an allowlist marketplace wait: hidden from every
       user route (superuser included), listed by the entries route as waiting
       with a ready snapshot.
    2. Allowing one shows it everywhere; the other stays hidden.
    3. A normal user gets 404 on the plain GET of the hidden one.
    """
    h, u = superuser_token_headers, normal_user_token_headers
    m = _market(client, h, ["alpha", "beta"])

    assert plugins_by_name(discover_plugins(client, h)) == {}
    assert plugins_by_name(discover_plugins(client, u)) == {}
    assert _plugin_count(client, h, m) == 2, "superuser sees the total"
    assert _plugin_count(client, u, m) == 0, "nothing allowed yet"

    rows = _entries(client, h, m)
    assert set(rows) == {"alpha", "beta"}
    assert all(r["access"] == "default" for r in rows.values())
    assert all(r["snapshot_status"] == "ready" for r in rows.values()), (
        "snapshots run for hidden entries"
    )
    waiting = list_marketplace_entries(client, h, m["id"], waiting="true")
    assert {r["name"] for r in waiting["data"]} == {"alpha", "beta"}
    counts = waiting["counts"]
    assert (counts["waiting"], counts["visible"], counts["default"]) == (2, 0, 2)
    assert (counts["allowed"], counts["banned"]) == (0, 0)

    alpha, beta = rows["alpha"], rows["beta"]
    for who in (h, u):
        assert not any(_visible_everywhere(client, who, alpha).values())
    assert get_plugin_archive(client, h, alpha["id"]).status_code == 404
    r = client.get(f"{PLUGINS_BASE}/plugins/{alpha['id']}/skill-archive", headers=h)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
    r = client.get(f"{PLUGINS_BASE}/plugins/{alpha['id']}/archive", headers=u)
    assert r.json()["detail"]["code"] == "not_found"
    assert client.get(f"{PLUGINS_BASE}/plugins/{alpha['id']}", headers=u).status_code == 404

    # ── allow one ─────────────────────────────────────────────────────
    updated = _set_access(client, h, alpha["id"], "allowed", reason="reviewed")
    assert updated["access"] == "allowed"
    assert updated["access_reason"] == "reviewed"
    assert updated["access_changed_at"]
    for who in (h, u):
        assert all(_visible_everywhere(client, who, alpha).values())
        assert not any(_visible_everywhere(client, who, beta).values())
    assert client.get(f"{PLUGINS_BASE}/plugins/{alpha['id']}", headers=u).status_code == 200
    assert _plugin_count(client, u, m) == 1 and _plugin_count(client, h, m) == 2

    user_row = plugins_by_name(discover_plugins(client, u))["alpha"]
    assert user_row["access"] == "allowed"
    assert user_row["access_reason"] is None and user_row["access_changed_at"] is None
    super_row = _entries(client, h, m)["alpha"]
    assert super_row["access"] == "allowed" and super_row["access_reason"] == "reviewed"

    still_waiting = list_marketplace_entries(client, h, m["id"], waiting="true")
    assert [r["name"] for r in still_waiting["data"]] == ["beta"]
    only_allowed = list_marketplace_entries(client, h, m["id"], access="allowed")
    assert [r["name"] for r in only_allowed["data"]] == ["alpha"]
    c = only_allowed["counts"]
    assert (c["allowed"], c["waiting"], c["visible"], c["default"]) == (1, 1, 1, 1)


def test_allow_all_ban_and_unban(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    h, u = superuser_token_headers, normal_user_token_headers
    m = _market(client, h, ["alpha", "beta"], mode="allow_all")
    rows = _entries(client, h, m)
    alpha = rows["alpha"]
    assert all(_visible_everywhere(client, u, alpha).values())
    assert plugins_by_name(discover_plugins(client, u))["alpha"]["access"] == "default"

    banned = _set_access(client, h, alpha["id"], "banned", reason="malware")
    assert banned["access"] == "banned" and banned["access_reason"] == "malware"
    for who in (h, u):
        assert not any(_visible_everywhere(client, who, alpha).values())
        assert "beta" in plugins_by_name(discover_plugins(client, who))
    assert client.get(f"{PLUGINS_BASE}/plugins/{alpha['id']}", headers=u).status_code == 404
    assert client.get(f"{PLUGINS_BASE}/plugins/{alpha['id']}", headers=h).status_code == 200
    assert _entries(client, h, m, access="banned").keys() == {"alpha"}
    counts = list_marketplace_entries(client, h, m["id"])["counts"]
    assert (counts["banned"], counts["visible"], counts["waiting"]) == (1, 1, 0)
    assert _plugin_count(client, u, m) == 1 and _plugin_count(client, h, m) == 2

    back = _set_access(client, h, alpha["id"], "default")
    assert back["access"] == "default" and back["access_reason"] is None
    assert all(_visible_everywhere(client, u, alpha).values())

    # reason is capped
    _set_access(client, h, alpha["id"], "banned", reason="x" * 1001, expected=422)
    _set_access(client, h, alpha["id"], "nonsense", expected=422)


def test_verdict_survives_resync_and_remove_then_readd(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    m = _market(client, h, ["alpha", "beta"], mode="allow_all")
    rows = _entries(client, h, m)
    _set_access(client, h, rows["alpha"]["id"], "banned", reason="r1")
    _set_access(client, h, rows["beta"]["id"], "allowed")

    _resync(client, h, m, ["alpha", "beta"])
    again = _entries(client, h, m)
    assert again["alpha"]["access"] == "banned" and again["beta"]["access"] == "allowed"

    _resync(client, h, m, ["beta"])
    assert set(_entries(client, h, m)) == {"beta"}
    _resync(client, h, m, ["alpha", "beta"])
    readded = _entries(client, h, m)
    assert readded["alpha"]["id"] != rows["alpha"]["id"], "re-added entry is a new row"
    assert readded["alpha"]["access"] == "banned"
    assert readded["alpha"]["access_reason"] == "r1"
    assert readded["beta"]["access"] == "allowed"
    assert "alpha" not in plugins_by_name(discover_plugins(client, h))


def test_mode_switch_dry_run_then_apply_keeps_verdicts(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    h, u = superuser_token_headers, normal_user_token_headers
    m = _market(client, h, ["a", "b", "c"], mode="allow_all")
    rows = _entries(client, h, m)
    _set_access(client, h, rows["a"]["id"], "allowed")
    _set_access(client, h, rows["b"]["id"], "banned")

    dry = _mode(client, h, m["id"], "allowlist", dry_run=True)
    assert dry == {
        "access_mode": "allowlist",
        "previous_mode": "allow_all",
        "visible_before": 2,
        "visible_after": 1,
        "applied": False,
    }
    assert client.get(f"{PLUGINS_BASE}/marketplaces/{m['id']}", headers=h).json()["access_mode"] == "allow_all"
    assert set(plugins_by_name(discover_plugins(client, u))) == {"a", "c"}

    applied = _mode(client, h, m["id"], "allowlist")
    assert applied["applied"] is True
    assert (applied["visible_before"], applied["visible_after"]) == (2, 1)
    assert applied["previous_mode"] == "allow_all"
    assert set(plugins_by_name(discover_plugins(client, u))) == {"a"}
    assert client.get(f"{PLUGINS_BASE}/marketplaces/{m['id']}", headers=h).json()["access_mode"] == "allowlist"
    assert _entries(client, h, m)["b"]["access"] == "banned", "verdicts are kept"

    back = _mode(client, h, m["id"], "allow_all")
    assert (back["visible_before"], back["visible_after"]) == (1, 2)
    assert set(plugins_by_name(discover_plugins(client, u))) == {"a", "c"}

    _mode(client, h, m["id"], "bogus", expected=422)
    r = client.put(
        f"{PLUGINS_BASE}/marketplaces/{uuid.uuid4()}/access-mode",
        headers=h,
        json={"access_mode": "allowlist"},
    )
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"


def test_bulk_access_all_or_nothing_and_guards(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    h, u = superuser_token_headers, normal_user_token_headers
    m = _market(client, h, ["a", "b", "c"])
    other = _market(client, h, ["z"])
    rows = _entries(client, h, m)
    foreign = _entries(client, h, other)["z"]["id"]

    res = _bulk(client, h, m["id"], [rows["a"]["id"], rows["b"]["id"]], "allowed")
    assert res == {"updated": 2, "visible": 2}
    assert set(plugins_by_name(discover_plugins(client, u))) == {"a", "b"}

    # duplicates count once
    res = _bulk(client, h, m["id"], [rows["c"]["id"], rows["c"]["id"]], "allowed", reason="dup")
    assert res["updated"] == 1 and res["visible"] == 3

    # foreign id: 422, nothing written
    ghost = str(uuid.uuid4())
    err = _bulk(client, h, m["id"], [rows["a"]["id"], foreign, ghost], "banned", expected=422)
    assert err["detail"]["code"] == "foreign_ids"
    assert set(err["detail"]["ids"]) == {foreign, ghost}
    assert _entries(client, h, m)["a"]["access"] == "allowed"
    assert _entries(client, h, other)["z"]["access"] == "default"

    # validation + 404s
    _bulk(client, h, m["id"], [], "allowed", expected=422)
    _bulk(client, h, m["id"], [str(uuid.uuid4()) for _ in range(501)], "allowed", expected=422)
    r = client.put(
        f"{PLUGINS_BASE}/marketplaces/{uuid.uuid4()}/entries/access",
        headers=h,
        json={"ids": [rows["a"]["id"]], "access": "allowed"},
    )
    assert r.status_code == 404
    _set_access(client, h, str(uuid.uuid4()), "allowed", expected=404)


def test_access_routes_are_superuser_only(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    h, u = superuser_token_headers, normal_user_token_headers
    m = _market(client, h, ["a"])
    pid = _entries(client, h, m)["a"]["id"]

    r = client.put(f"{PLUGINS_BASE}/plugins/{pid}/access", headers=u, json={"access": "allowed"})
    assert r.status_code == 403
    r = client.put(
        f"{PLUGINS_BASE}/marketplaces/{m['id']}/entries/access",
        headers=u,
        json={"ids": [pid], "access": "allowed"},
    )
    assert r.status_code == 403
    r = client.put(
        f"{PLUGINS_BASE}/marketplaces/{m['id']}/access-mode",
        headers=u,
        json={"access_mode": "allow_all"},
    )
    assert r.status_code == 403
    assert client.put(f"{PLUGINS_BASE}/plugins/{pid}/access", json={"access": "allowed"}).status_code in (401, 403)
    assert _entries(client, h, m)["a"]["access"] == "default"
    assert plugins_by_name(discover_plugins(client, h)) == {}


def test_existing_cloud_link_to_banned_plugin_still_lists(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A5: a ban hides the entry from discovery, not from agents already using it."""
    h = superuser_token_headers
    m = _market(client, h, ["alpha"], mode="allow_all")
    plugin = _entries(client, h, m)["alpha"]
    agent_id, _ = make_agent_with_env(client, h, "access-a5")
    install_agent_plugin(client, h, agent_id, plugin["id"])

    _set_access(client, h, plugin["id"], "banned")
    assert not any(_visible_everywhere(client, h, plugin).values())
    links = list_agent_plugins(client, h, agent_id)
    assert [link["plugin_id"] for link in links] == [plugin["id"]]
