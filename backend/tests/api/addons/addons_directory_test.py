"""K3 — ``GET /addons/directory``: one index over every addon source.

Scenarios:
  1. Union: a catalog skill, a skills-format marketplace skill, a claude plugin
     and an MCP entry come back with the right ``kind`` / ``ref.source`` /
     ``install``; a pending plugin has no ``install``; policy is all true; the
     ETag header and ``Cache-Control`` are set.
  2. Visibility: a private catalog skill is absent for a stranger and present
     for the publisher and a grantee; a marketplace that is not
     ``public_discovery`` is absent for a non-owner superuser (no bypass);
     user A's cached index is never served to user B.
  3. Ranking on a crafted set: exact > prefix > title > tag/category >
     description, an unsupported entry after a ready one in its tier.
  4. Filters: kind, source, category, skip/limit (count is the total),
     ``runs_on=desktop`` drops a skill needing a cloud-only credential type.
  5. ETag: 304 on ``If-None-Match``; the ETag is stable on a repeat and changes
     at once after an MCP write, an MCP import, a ban and a marketplace sync.
"""
import uuid

from fastapi.testclient import TestClient

from tests.api.catalog.catalog_helpers import make_person, publish_skill_package
from tests.utils.addons import (
    DIRECTORY_URL,
    create_mcp_entry,
    directory_entry,
    directory_names,
    get_directory,
    import_mcp_index,
    mcp_http_body,
    opencinna_server,
    set_mcp_access,
)
from tests.utils.llm_plugin import claude_repo, create_marketplace, stubbed_plugin_fetch, sync_marketplace
from tests.utils.marketplace_skills import create_second_superuser, new_skills_marketplace, skills_repo
from tests.utils.skill_catalog import (
    add_skill_package_grant,
    make_agent_with_env,
    publish_skill,
    write_skill_with_credentials,
)
from tests.utils.user import create_random_user_with_headers

EXT_URL = "https://github.com/acme/dir-ext.git"


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _local_entry(name: str, **extra) -> dict:
    return {"name": name, "description": f"{name} plugin", "source": f"./plugins/{name}", **extra}


def _plugin_market(client, headers, entries, files, *, public_discovery=True, tag=None) -> dict:
    market = create_marketplace(
        client,
        headers,
        url=f"https://github.com/example/dir-{tag or uuid.uuid4().hex[:8]}.git",
        public_discovery=public_discovery,
    )
    sync_marketplace(client, headers, market["id"], populate=claude_repo(entries, files))
    return market


def _etag(client, headers, **params) -> str:
    r = client.get(DIRECTORY_URL, headers=headers, params=params)
    assert r.status_code == 200, r.text
    assert r.headers["ETag"] == r.json()["etag"]
    return r.json()["etag"]


def test_directory_unions_every_source_with_kind_ref_and_install(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Publish a public catalog skill; sync a skills marketplace, a claude
       marketplace (ready plugin + an external plugin whose fetch keeps failing
       = pending), add an MCP entry.
    2. The directory lists all of them with the right kind / source / install.
    """
    h = superuser_token_headers
    dev = make_person(client, h)
    viewer_headers = create_random_user_with_headers(client)[1]

    cat_name = _slug("cat-skill")
    publish_skill_package(client, dev.headers, cat_name, visibility="public")
    mkt = new_skills_marketplace(client, h, skills_repo(("mkt-alpha",), broken=()))
    plug_name, pend_name = _slug("plug"), _slug("pend")
    ext_entry = {
        "name": pend_name,
        "description": "external",
        "source": {"source": "url", "url": EXT_URL, "sha": "1" * 40},
    }
    with stubbed_plugin_fetch({}) as fetch:
        fetch.fail_urls.add(EXT_URL)
        _plugin_market(
            client,
            h,
            [_local_entry(plug_name), ext_entry],
            {f"plugins/{plug_name}/commands/go.md": "# go"},
        )
    mcp_name = _slug("mcp")
    create_mcp_entry(client, h, mcp_http_body(mcp_name))

    # A plain user sees every source.
    payload = get_directory(client, viewer_headers)
    assert payload["count"] == len(payload["data"])
    assert payload["policy"] == {
        "external_directories": True,
        "custom_mcp": True,
        "plugin_hooks": True,
        "stdio_mcp": True,
    }

    cat = directory_entry(payload, cat_name)
    assert cat["kind"] == "skill" and cat["ref"]["source"] == "core-catalog"
    assert cat["status"] == "ready"
    assert cat["install"]["archive_path"].endswith("/download")

    mkt_skill = directory_entry(payload, "mkt-alpha")
    assert mkt_skill["kind"] == "skill" and mkt_skill["ref"]["source"] == "core-marketplace"
    assert mkt_skill["status"] == "ready"
    assert "/llm-plugins/plugins/" in mkt_skill["install"]["archive_path"]
    assert mkt_skill["install"]["archive_path"].endswith(
        f"sha256={mkt_skill['digest'].removeprefix('sha256:')}"
    )

    plug = directory_entry(payload, plug_name)
    assert plug["kind"] == "plugin" and plug["ref"]["source"] == "core-marketplace"
    assert plug["status"] == "ready" and plug["components"]["commands"] == ["go"]
    assert plug["install"]["archive_path"]

    pend = directory_entry(payload, pend_name)
    assert pend["kind"] == "plugin"
    assert pend["status"] == "pending"
    assert pend["install"] is None

    mcp = directory_entry(payload, mcp_name)
    assert mcp["kind"] == "mcp" and mcp["ref"]["source"] == "core-mcp-directory"
    assert mcp["install"]["mcp"]["transport"] == "http"
    assert mcp["install"]["mcp"]["url"] == f"https://{mcp_name}.example.com/mcp"

    r = client.get(DIRECTORY_URL, headers=viewer_headers)
    assert r.headers["ETag"] == payload["etag"]
    assert "private" in r.headers["Cache-Control"] and "no-cache" in r.headers["Cache-Control"]

    # Unauthenticated callers are refused.
    assert client.get(DIRECTORY_URL).status_code in (401, 403)
    assert mkt["id"]


def test_directory_visibility_is_per_caller_and_never_cross_served(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A private catalog skill is the publisher's alone; a ``users`` skill is
       hidden from a stranger and from the grantee until the grant lands.
    2. A non-public_discovery marketplace: its owner sees it, a second
       superuser does not.
    3. The cache keyed per user never leaks one user's index to another.
    """
    h = superuser_token_headers
    owner = make_person(client, h)
    stranger = make_person(client, h)
    grantee = make_person(client, h)

    priv = _slug("priv-skill")
    publish_skill_package(client, owner.headers, priv, visibility="private")
    shared = _slug("shared-skill")
    package = publish_skill_package(client, owner.headers, shared, visibility="users")

    hidden_plug = _slug("hidden-plug")
    _plugin_market(
        client, h, [_local_entry(hidden_plug)],
        {f"plugins/{hidden_plug}/commands/x.md": "#"}, public_discovery=False,
    )
    _, other_super_headers = create_second_superuser(client, h)

    # Warm the owner's cache first, then ask as others: a keyed cache must not leak.
    assert priv in directory_names(get_directory(client, owner.headers))
    assert hidden_plug in directory_names(get_directory(client, h))

    assert priv not in directory_names(get_directory(client, stranger.headers))
    assert priv not in directory_names(get_directory(client, grantee.headers))
    assert shared in directory_names(get_directory(client, owner.headers))
    assert shared not in directory_names(get_directory(client, grantee.headers))
    assert hidden_plug not in directory_names(get_directory(client, other_super_headers))
    assert hidden_plug not in directory_names(get_directory(client, owner.headers))

    add_skill_package_grant(client, owner.headers, package["package_uuid"], grantee.email)
    assert shared in directory_names(get_directory(client, grantee.headers))
    assert priv not in directory_names(get_directory(client, grantee.headers))
    assert shared not in directory_names(get_directory(client, stranger.headers))


def test_directory_ranking_and_filters(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Crafted set around one token: exact name, name prefix (ready MCP before an
    unsupported plugin), title, category tag, description. Then filters.
    """
    h = superuser_token_headers
    tok = "zq" + uuid.uuid4().hex[:6]
    n_title, n_tag, n_desc = _slug("aa"), _slug("bb"), _slug("cc")
    create_mcp_entry(client, h, mcp_http_body(tok))
    create_mcp_entry(client, h, mcp_http_body(f"{tok}-extra"))
    create_mcp_entry(client, h, mcp_http_body(n_title, title=f"The {tok} thing"))
    create_mcp_entry(client, h, mcp_http_body(n_tag, categories=[tok]))
    create_mcp_entry(client, h, mcp_http_body(n_desc, description=f"mentions {tok} here"))
    create_mcp_entry(client, h, mcp_http_body(_slug("nomatch")))

    bad_name = f"{tok}-plug"
    with stubbed_plugin_fetch({"https://github.com/acme/dir-empty.git": {"README.md": "nothing"}}):
        _plugin_market(
            client, h,
            [{"name": bad_name, "description": "d",
              "source": {"source": "url", "url": "https://github.com/acme/dir-empty.git", "sha": "2" * 40}}],
            {},
        )

    ranked = get_directory(client, h, search=tok)
    assert directory_names(ranked) == [tok, f"{tok}-extra", bad_name, n_title, n_tag, n_desc]
    assert directory_entry(ranked, bad_name)["status"] == "unsupported"
    assert directory_entry(ranked, bad_name)["unsupported_reason"]
    assert directory_entry(ranked, bad_name)["install"] is None
    assert ranked["count"] == 6

    # Filters
    mcps = get_directory(client, h, kind="mcp")
    assert {e["kind"] for e in mcps["data"]} == {"mcp"} and tok in directory_names(mcps)
    plugins = get_directory(client, h, kind="plugin")
    assert directory_names(plugins) == [bad_name]
    sources = get_directory(client, h, source="core-marketplace")
    assert directory_names(sources) == [bad_name]
    assert directory_names(get_directory(client, h, source="core-catalog")) == []
    assert directory_names(get_directory(client, h, category=tok)) == [n_tag]
    assert directory_names(get_directory(client, h, search="no-such-" + tok)) == []

    page = get_directory(client, h, search=tok, skip=1, limit=2)
    assert directory_names(page) == [f"{tok}-extra", bad_name]
    assert page["count"] == 6, "count is the total, not the page size"
    assert get_directory(client, h, search=tok, skip=10)["data"] == []
    get_directory(client, h, limit=0, expected_status=422)
    get_directory(client, h, limit=5001, expected_status=422)
    get_directory(client, h, kind="nope", expected_status=422)


def test_runs_on_desktop_drops_a_skill_needing_a_cloud_only_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A catalog skill declaring an ssh_key slot is not desktop-runnable; an api_token one is."""
    h = superuser_token_headers
    dev = make_person(client, h)
    local_name, cloud_name = _slug("local-cred"), _slug("cloud-cred")
    for name, ctype in ((local_name, "api_token"), (cloud_name, "ssh_key")):
        agent_id, env_id = make_agent_with_env(client, dev.headers, f"src-{name}")
        write_skill_with_credentials(env_id, name, [{"slot": f"slot-{name}", "type": ctype}])
        publish_skill(client, dev.headers, agent_id, name, visibility="public")

    everything = directory_names(get_directory(client, dev.headers))
    assert local_name in everything and cloud_name in everything
    on_desktop = directory_names(get_directory(client, dev.headers, runs_on="desktop"))
    assert local_name in on_desktop
    assert cloud_name not in on_desktop
    get_directory(client, dev.headers, runs_on="cloud", expected_status=422)
    cloud = directory_entry(get_directory(client, dev.headers), cloud_name)
    assert [c["type"] for c in cloud["requires"]["credentials"]] == ["ssh_key"]


def test_etag_conditional_get_and_immediate_change_after_each_write(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Repeat reads share an ETag; If-None-Match -> 304 with no body.
    2. An MCP create, an MCP import, a ban and a marketplace sync each change
       the ETag on the very next read (no waiting for the cache TTL).
    """
    h = superuser_token_headers
    first = _etag(client, h)
    assert _etag(client, h) == first

    r = client.get(DIRECTORY_URL, headers={**h, "If-None-Match": first})
    assert r.status_code == 304 and r.content == b""
    assert r.headers["ETag"] == first
    r = client.get(DIRECTORY_URL, headers={**h, "If-None-Match": '"other", ' + first})
    assert r.status_code == 304
    r = client.get(DIRECTORY_URL, headers={**h, "If-None-Match": '"stale"'})
    assert r.status_code == 200 and r.json()["etag"] == first

    # MCP create
    name = _slug("etag-mcp")
    entry = create_mcp_entry(client, h, mcp_http_body(name))
    after_create = _etag(client, h)
    assert after_create != first
    assert client.get(DIRECTORY_URL, headers={**h, "If-None-Match": first}).status_code == 200

    # MCP import
    import_mcp_index(client, h, {"version": 1, "servers": [opencinna_server(_slug("imp"))]})
    after_import = _etag(client, h)
    assert after_import != after_create

    # Ban hides, unban restores: the etag follows
    set_mcp_access(client, h, entry["id"], "banned", reason="nope")
    banned = _etag(client, h)
    assert banned != after_import
    assert name not in directory_names(get_directory(client, h))
    set_mcp_access(client, h, entry["id"], "default")
    assert name in directory_names(get_directory(client, h))

    # Marketplace sync adding a skill
    before_sync = _etag(client, h)
    new_skills_marketplace(client, h, skills_repo(("etag-skill",), broken=()))
    assert _etag(client, h) != before_sync
    assert "etag-skill" in directory_names(get_directory(client, h))


def test_marketplace_publisher_is_verified_only_for_the_owners_own_entries(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Verified = entry author equals the marketplace owner; no author -> not verified, owner name."""
    h = superuser_token_headers
    own, foreign, anon = _slug("own"), _slug("foreign"), _slug("anon")
    entries = [
        _local_entry(own, author={"name": "Snap"}),
        _local_entry(foreign, author={"name": "Someone Else"}),
        _local_entry(anon),
    ]
    files = {f"plugins/{n}/commands/c.md": "#" for n in (own, foreign, anon)}
    market = create_marketplace(client, h, url=f"https://github.com/example/ver-{uuid.uuid4().hex[:8]}.git")
    sync_marketplace(client, h, market["id"],
                     populate=claude_repo(entries, {f"plugins/{n}/commands/c.md": "#" for n in (own, foreign, anon)}, name="ver"))
    assert files
    payload = get_directory(client, h)
    assert directory_entry(payload, own)["publisher"]["verified"] is True
    assert directory_entry(payload, foreign)["publisher"] == {"name": "Someone Else", "verified": False}
    anon_pub = directory_entry(payload, anon)["publisher"]
    assert anon_pub["verified"] is False and anon_pub["name"] == "Snap"
