"""Agent Addons projection — plugins and skills as one deduplicated list.

    GET  /agents/{id}/addons          — cached, never wakes a container
    POST /agents/{id}/addons/refresh  — re-read the index, then re-project

The projection exists because the agent page used to answer "what can this
agent do?" twice: a catalog install was a row on the Plugins tab *and* a row on
the Skills card. These tests pin the fold that removes the double listing, and
the three things that fold could plausibly get wrong — hiding a skill whose
plugin is gone, blanking the plugin half when the environment cannot answer,
and letting a client re-derive capabilities from a role.

Scenarios:
  1. The dedupe rule end to end: one row per catalog install, a marketplace
     plugin's skills folded into its own row, a local skill as its own row, an
     orphan row for an index entry no link owns, the derived status, the
     status-then-alphabetical order and the counts — plus the ownership guards.
  2. An unreadable index reports ``skills_error`` while every plugin link row
     still comes back with ``skills=[]``; an agent with no environment at all
     reports ``environment_id=null`` and **no** error, because there is nothing
     to have failed.
  3. ``can_add`` / ``can_manage`` / ``can_share`` are server-computed capability
     replies: false for a non-developer who owns the agent, and false on a
     foreign (consumer) install even for a developer.
  4. A deleted marketplace: the agent that had one of its plugins installed
     keeps the row, flagged ``source_unavailable``, and the skills the engine
     still loads stay folded into it — an admin's action on a catalog never
     uninstalls somebody else's plugin behind their back.
  5. The same, through the other delete path: an entry dropped upstream is
     removed by the next sync, the install survives it, upgrade answers 409
     ``source_unavailable`` and uninstall still works.
  6. The way back: an entry that reappears upstream re-adopts the install it
     left behind, so a transient upstream mistake is not a permanent one.
  7. Two links claiming one plugin directory — a dead install and the live one
     beside it — fold that directory's skills onto the row that can still
     deliver files, whatever order the database returns them in.
  8. The limits of that adoption: a *different* repository that later claims
     the same marketplace name does not capture the installs left behind, and
     a marketplace renamed upstream takes its live installs' frozen directory
     name with it.
  9. What actually proves origin: the repository. An *older* marketplace that
     takes over a freed name — the direction the age guard cannot see — adopts
     nothing unless its URL names the same repository the install was made
     from; the same repository under that name does adopt, across the ``.git``
     spelling difference; an install with no recorded repository is never
     adopted at all; and the age guard still stands on its own.

Test seam:
  ``patch_environment_adapter`` (the agent-domain lifecycle-manager fixture) is
  re-pointed at one ``EnvironmentTestAdapter`` the test owns, so the skills
  index the environment "reports" is whatever the scenario sets. Marketplace
  plugin rows are seeded through the real sync route with the clone and the
  parser stubbed (``tests/utils/llm_plugin.py``). Publishing reads the host-side
  workspace seeded by ``tests/utils/skill_catalog.write_skill``.

Notes:
  The skills index route itself (cache-first reads, refresh posture, the
  ``adapter_error`` vs ``env_not_running`` split) is covered in
  ``agents_skills_routes_test.py``; the catalog install lifecycle in
  ``agents_skills_catalog_install_test.py``.
"""
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.addons import (
    addons_by_name,
    get_agent_addons,
    refresh_agent_addons,
    skill_names,
)
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import install_bundle, publish_bundle_and_make_public
from tests.utils.environment import delete_environment
from tests.utils.llm_plugin import (
    clear_link_repository_url,
    create_marketplace,
    delete_marketplace,
    discover_plugins,
    get_marketplace,
    install_agent_plugin,
    list_agent_plugins,
    orphan_plugin_link,
    plugins_by_name,
    seed_marketplace_plugin,
    sync_marketplace,
    update_marketplace,
    upgrade_agent_plugin,
)
from tests.utils.skill_catalog import (
    install_skill,
    list_skill_catalog,
    make_agent_with_env,
    make_developer,
    patched_skill_storage,
    publish_skill,
    workspace_root,
    write_skill,
)
from tests.utils.user import (
    create_random_user_with_headers,
    promote_to_developer,
)

API = settings.API_V1_STR


@pytest.fixture(autouse=True)
def skill_storage(tmp_path: Path):
    """Redirect ``SKILL_STORAGE_DIR`` at a tmp tree for every test here."""
    with patched_skill_storage(tmp_path / "skill-storage") as root:
        yield root


# ── Index payload helpers ──────────────────────────────────────────────────


def _row(name: str, **overrides: Any) -> dict:
    """One entry in the shape env-core's ``GET /config/skills`` reports."""
    entry = {
        "name": name,
        "description": f"Does {name} things.",
        "source": "local",
        "plugin_ref": None,
        "path": f"skills/{name}",
        "has_scripts": False,
        "user_invocable": True,
        "model_invocable": True,
        "size_bytes": 512,
        "error": None,
        "warning": None,
        "secret_paths": [],
    }
    entry.update(overrides)
    return entry


def _index(rows: list[dict], tree_hash: str = "hash-1") -> dict:
    return {"hash": tree_hash, "skills": rows, "errors": []}


def _install_adapter(lifecycle_manager, *, index: dict | None = None):
    """Route every ``get_adapter`` call at one adapter the test controls."""
    adapter = EnvironmentTestAdapter()
    adapter.skills_index = index or _index([])
    adapter.workspace_files = {}
    lifecycle_manager.get_adapter = lambda env: adapter
    return adapter


def _demote_to_user(
    client: TestClient, superuser_headers: dict[str, str], user_id: str
) -> None:
    """Take the ``agent-developer`` role back off a user."""
    r = client.patch(
        f"{API}/users/{user_id}/role",
        headers=superuser_headers,
        json={"role": "agent-user"},
    )
    assert r.status_code == 200, r.text


# ── Scenario 1: the dedupe rule end to end ─────────────────────────────────


def test_addons_projection_dedupes_folds_and_orders_every_source(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    One agent carrying every kind of addon at once:

      1. A publisher publishes ``pdf-report`` to the skills catalog.
      2. A consumer installs it AND a marketplace plugin, then publishes a
         local skill of their own.
      3. The environment's index reports five entries: the catalog skill, two
         skills belonging to the marketplace plugin, one local skill, and one
         belonging to a plugin whose link no longer exists.
      4. The projection answers FOUR rows — the catalog install appears once
         (as a skill, not twice), the plugin's two skills hang off its row
         rather than becoming rows, the local skill is its own row, and the
         unowned entry gets an orphan row so nothing loadable is invisible.
      5. Derived fields: status from the worst folded skill, ``source``,
         ``plugin_type``, ``version``, ``published_package_id``, the counts.
      6. Order is attention-first then alphabetical — deliberately the opposite
         of alphabetical here, so the status grouping cannot pass by accident.
      7. An administrator reading somebody else's agent: rows visible, every
         capability false, and ``published_package_id`` withheld with them.
      8. Auth and ownership guards; an unknown agent id is a 404.
    """
    # ── Phase 1: a published catalog skill ───────────────────────────────
    publisher, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "Addon-Publisher")
    write_skill(pub_env, "pdf-report", description="Renders a PDF report.")
    publish_skill(
        client, pub_headers, pub_agent, "pdf-report", version="1.0",
        visibility="public",
    )
    package_uuid = list_skill_catalog(client, pub_headers)[0]["id"]

    # ── Phase 2: a consumer with one of everything ───────────────────────
    consumer, con_headers = make_developer(client, superuser_token_headers)
    con_agent, con_env = make_agent_with_env(client, con_headers, "Addon-Consumer")
    adapter = _install_adapter(patch_environment_adapter)

    marketplace = create_marketplace(client, superuser_token_headers)
    plugin = seed_marketplace_plugin(
        client,
        superuser_token_headers,
        marketplace["id"],
        name="reporting",
        description="Reporting helpers.",
        version="2.3",
    )
    install_agent_plugin(client, con_headers, con_agent, plugin["id"])
    install_skill(client, con_headers, con_agent, package_uuid)

    # The consumer's own skill, published from this agent — that is what makes
    # ``published_package_id`` non-null below ("Update published skill" rather
    # than "Share").
    write_skill(con_env, "zeta-local", description="Files the weekly note.")
    local_revision = publish_skill(
        client, con_headers, con_agent, "zeta-local", version="0.1"
    )

    # The links exist before the index does: the plugin half must stand alone.
    early = get_agent_addons(client, con_headers, con_agent)
    assert early["counts"]["plugins"] == 1
    assert early["counts"]["skills"] == 1, "the catalog link is a skill row"
    early_rows = addons_by_name(early)
    assert skill_names(early_rows["reporting"]) == []
    marketplace_ref = (
        f"{early_rows['reporting']['marketplace_name']}/"
        f"{early_rows['reporting']['name']}"
    )

    # ── Phase 3: the environment reports the whole picture ───────────────
    adapter.skills_index = _index(
        [
            _row(
                "pdf-report",
                source="catalog",
                plugin_ref="cinna-skills/pdf-report",
                path="plugins/cinna-skills/pdf-report/skills/pdf-report",
            ),
            _row(
                "alpha-report",
                source="plugin",
                plugin_ref=marketplace_ref,
                path=f"plugins/{marketplace_ref}/skills/alpha-report",
            ),
            _row(
                "beta-tools",
                source="plugin",
                plugin_ref=marketplace_ref,
                path=f"plugins/{marketplace_ref}/skills/beta-tools",
                warning={
                    "code": "shadowed",
                    "message": "A local skill of the same name takes priority.",
                    "paths": [],
                },
            ),
            _row("zeta-local"),
            _row(
                "ghost-skill",
                source="plugin",
                plugin_ref="test-marketplace/removed-plugin",
                path="plugins/test-marketplace/removed-plugin/skills/ghost-skill",
            ),
        ],
        tree_hash="hash-2",
    )
    payload = refresh_agent_addons(client, con_headers, con_agent)

    # ── Phase 4: five index entries, four rows ───────────────────────────
    assert payload["agent_id"] == con_agent
    assert payload["environment_id"] is not None
    assert payload["skills_error"] is None
    assert payload["fetched_at"] is not None
    assert payload["can_add"] is True

    rows = addons_by_name(payload)
    assert set(rows) == {"pdf-report", "reporting", "zeta-local", "removed-plugin"}
    for folded in ("alpha-report", "beta-tools", "ghost-skill"):
        assert folded not in rows, (
            f"{folded} belongs to a plugin — it must hang off that plugin's "
            "row, not become a row of its own"
        )

    catalog_row = rows["pdf-report"]
    assert catalog_row["kind"] == "skill", "a catalog link wraps exactly one skill"
    assert catalog_row["source"] == "catalog"
    assert catalog_row["marketplace_name"] == "cinna-skills"
    assert catalog_row["plugin_type"] is None
    assert catalog_row["key"] == f"plugin:{catalog_row['link']['id']}"
    assert catalog_row["link"]["source"] == "catalog"
    assert catalog_row["link"]["skill_package_id"] == package_uuid
    assert catalog_row["version"] == "1.0"
    assert catalog_row["author"] == (
        publisher.get("full_name") or publisher["email"]
    ), "a catalog row's author is the package's publisher, name before email"
    assert skill_names(catalog_row) == ["pdf-report"], (
        "the catalog install's index entry folds into its own row — the "
        "double listing this projection exists to remove"
    )
    assert catalog_row["status"] == "ok"

    plugin_row = rows["reporting"]
    assert plugin_row["kind"] == "plugin"
    assert plugin_row["source"] == "marketplace"
    assert plugin_row["plugin_type"] == "claude"
    assert plugin_row["version"] == "2.3"
    assert plugin_row["author"] == "tester", (
        "a marketplace row's author is the manifest's author_name"
    )
    assert plugin_row["repository_url"] == "https://example.com/plugins.git", (
        "no homepage and a local source: the marketplace repository is where "
        "the source lives"
    )
    assert skill_names(plugin_row) == ["alpha-report", "beta-tools"]
    assert plugin_row["status"] == "warning"
    assert plugin_row["status_code"] == "shadowed"

    local_row = rows["zeta-local"]
    assert local_row["kind"] == "skill"
    assert local_row["source"] == "local"
    assert local_row["key"] == "skill:local:zeta-local"
    assert local_row["link"] is None, "a workspace folder has no link"
    assert local_row["marketplace_name"] is None
    assert local_row["author"] is None, "a workspace folder names no author"
    assert local_row["repository_url"] is None
    assert skill_names(local_row) == ["zeta-local"]
    assert local_row["published_package_id"] == local_revision["package_id"]
    assert local_row["status"] == "ok"

    orphan_row = rows["removed-plugin"]
    assert orphan_row["orphan"] is True
    assert orphan_row["kind"] == "plugin"
    assert orphan_row["key"] == "plugin:orphan:test-marketplace/removed-plugin"
    assert orphan_row["marketplace_name"] == "test-marketplace"
    assert orphan_row["link"] is None
    assert orphan_row["can_manage"] is False, "there is nothing left to manage"
    assert orphan_row["can_share"] is False
    assert orphan_row["status"] == "error"
    assert orphan_row["status_code"] == "orphan"
    assert skill_names(orphan_row) == ["ghost-skill"], (
        "the entry the engine can still load must be visible somewhere"
    )

    # ── Phase 5: capabilities and counts ─────────────────────────────────
    assert catalog_row["can_manage"] is True
    assert plugin_row["can_manage"] is True
    assert local_row["can_manage"] is False, (
        "a local skill has no toggles, no upgrade and no uninstall — its verb "
        "is can_share"
    )
    assert local_row["can_share"] is True
    for row in (catalog_row, plugin_row, orphan_row):
        assert row["can_share"] is False, row["name"]

    assert payload["counts"] == {
        "plugins": 2,  # the marketplace row and the orphan row
        "skills": 2,  # the catalog install and the local folder
        "local_skills": 1,
    }

    # ── Phase 6: errors, then warnings, then alphabetical ────────────────
    assert [row["name"] for row in payload["addons"]] == [
        "removed-plugin",  # error
        "reporting",  # warning
        "pdf-report",  # ok, alphabetically first
        "zeta-local",  # ok
    ], "status groups the list; display name orders inside a group"

    # A plain GET serves the same projection without touching the container.
    adapter.skills_index = _index([_row("never-read")], tree_hash="hash-3")
    assert [row["name"] for row in get_agent_addons(
        client, con_headers, con_agent
    )["addons"]] == [row["name"] for row in payload["addons"]], (
        "GET /addons is cache-only — a poll must never wake an environment"
    )

    # ── Phase 7: a superuser reading somebody else's agent ───────────────
    # An administrator may READ the projection, but they do not own this agent,
    # so ``can_build`` is false for them — and every capability that hangs off
    # it goes with it, including the published marker. Answering it would offer
    # "Update published skill" to somebody the publish route would refuse.
    admin_view = get_agent_addons(client, superuser_token_headers, con_agent)
    assert admin_view["can_add"] is False
    admin_rows = addons_by_name(admin_view)
    assert set(admin_rows) == set(rows), "reading is not gated, acting is"
    admin_local = admin_rows["zeta-local"]
    assert admin_local["can_share"] is False
    assert admin_local["published_package_id"] is None, (
        "the published marker rides on can_share — a viewer who cannot publish "
        "must not be told which package they would be updating"
    )
    for row in admin_view["addons"]:
        assert row["can_manage"] is False, row["name"]

    # ── Phase 8: auth and ownership guards ───────────────────────────────
    assert client.get(f"{API}/agents/{con_agent}/addons").status_code in (401, 403)
    assert client.post(
        f"{API}/agents/{con_agent}/addons/refresh"
    ).status_code in (401, 403)

    _stranger, stranger_headers = create_random_user_with_headers(client)
    for method, url in (
        ("get", f"{API}/agents/{con_agent}/addons"),
        ("post", f"{API}/agents/{con_agent}/addons/refresh"),
    ):
        r = getattr(client, method)(url, headers=stranger_headers)
        assert r.status_code in (403, 404), f"{method} {url} → {r.status_code}"

    assert client.get(
        f"{API}/agents/{uuid.uuid4()}/addons", headers=con_headers
    ).status_code == 404


# ── Scenario 2: the skill half may fail alone ──────────────────────────────


def test_an_unreadable_index_never_takes_the_plugin_rows_with_it(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    The tab must not blank because the environment is asleep:

      1. An agent with a marketplace plugin whose environment cannot answer
         ``/config/skills`` (what a pre-feature container looks like).
      2. GET /addons → 200 with the plugin row present and ``skills=[]``, the
         reason in ``skills_error``, and ``environment_id`` still set.
      3. The environment answers again → the banner clears and the plugin's
         skills appear inside its row, still without a second top-level row.
      4. An agent with NO environment at all → ``environment_id`` null and
         ``skills_error`` null: nothing failed, there is simply nothing to read.
    """
    # ── Phase 1: an environment that cannot answer ───────────────────────
    adapter = _install_adapter(patch_environment_adapter)

    async def _no_endpoint():
        raise RuntimeError("404 Not Found: /config/skills")

    adapter.get_skills_index = _no_endpoint

    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Silent-Env")

    marketplace = create_marketplace(client, superuser_token_headers)
    plugin = seed_marketplace_plugin(
        client, superuser_token_headers, marketplace["id"], name="reporting"
    )
    install_agent_plugin(client, headers, agent_id, plugin["id"])

    # ── Phase 2: the plugin half stands on its own ───────────────────────
    payload = get_agent_addons(client, headers, agent_id)
    assert payload["skills_error"] == "adapter_error"
    assert payload["environment_id"] is not None
    rows = addons_by_name(payload)
    assert set(rows) == {"reporting"}
    assert skill_names(rows["reporting"]) == []
    assert rows["reporting"]["status"] == "ok", (
        "a plugin is not broken because the index could not be read"
    )
    assert rows["reporting"]["can_manage"] is True
    assert payload["counts"]["plugins"] == 1

    # A refresh against an unreachable environment is still a 200.
    assert refresh_agent_addons(client, headers, agent_id)[
        "skills_error"
    ] == "adapter_error"

    # ── Phase 3: the environment comes back ──────────────────────────────
    marketplace_ref = f"{rows['reporting']['marketplace_name']}/reporting"

    async def _answers():
        return _index(
            [_row("alpha-report", source="plugin", plugin_ref=marketplace_ref)],
            tree_hash="hash-2",
        )

    adapter.get_skills_index = _answers

    payload = refresh_agent_addons(client, headers, agent_id)
    assert payload["skills_error"] is None
    rows = addons_by_name(payload)
    assert set(rows) == {"reporting"}, "the plugin's skill is not a second row"
    assert skill_names(rows["reporting"]) == ["alpha-report"]

    # ── Phase 4: no environment is not an error ──────────────────────────
    bare_agent = create_agent_via_api(client, headers, name="Bare-Agent")["id"]
    drain_tasks()
    bare_env = get_agent(client, headers, bare_agent)["active_environment_id"]
    delete_environment(client, headers, bare_env)

    payload = get_agent_addons(client, headers, bare_agent)
    assert payload["environment_id"] is None
    assert payload["skills_error"] is None, (
        "environment_id=null already says 'never had one' — an error code here "
        "would claim a read failed that was never attempted"
    )
    assert payload["addons"] == []
    assert payload["counts"] == {"plugins": 0, "skills": 0, "local_skills": 0}


# ── Scenario 2b: the author label and the repository link ─────────────────


def test_a_blank_manifest_author_falls_back_to_the_marketplace_owner(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    The official marketplace leaves ``author`` blank on most entries and names
    itself once, at the top. The row's ``author`` therefore falls back to the
    marketplace owner, on the install list **and** in discovery, so the badge
    an entry shows does not change the moment it is installed. A manifest
    ``homepage`` is the repository link; without one, the marketplace
    repository is — rewritten from its SSH form so a browser can open it.
    """
    _install_adapter(patch_environment_adapter)
    _owner, headers = make_developer(client, superuser_token_headers)
    agent_id, _env = make_agent_with_env(client, headers, "Author-Fallback")

    marketplace = create_marketplace(
        client, superuser_token_headers, url="git@github.com:acme/plugins.git"
    )
    blank = seed_marketplace_plugin(
        client,
        superuser_token_headers,
        marketplace["id"],
        name="blank-author",
        author_name="",
        author_email="",
        owner_name="Acme Tools",
    )
    assert blank["marketplace_owner"] == "Acme Tools"
    assert blank["author_name"] == ""
    assert blank["repository_url"] == "https://github.com/acme/plugins.git", (
        "the SSH form the marketplace was registered with is rewritten for a "
        "browser, not handed to an anchor tag as-is"
    )
    install_agent_plugin(client, headers, agent_id, blank["id"])

    # Its own marketplace: a second sync of the first one with a catalog that
    # no longer lists ``blank-author`` would drop that row and orphan the link.
    other = create_marketplace(
        client, superuser_token_headers, url="https://github.com/acme/other.git"
    )
    named = seed_marketplace_plugin(
        client,
        superuser_token_headers,
        other["id"],
        name="named-author",
        marketplace_name="other-marketplace",
        author_name="Jo Author",
        homepage="https://github.com/acme/plugins/tree/main/plugins/named-author",
        owner_name="Acme Tools",
    )
    install_agent_plugin(client, headers, agent_id, named["id"])

    rows = addons_by_name(get_agent_addons(client, headers, agent_id))
    assert rows["blank-author"]["author"] == "Acme Tools", (
        "a blank manifest author reads as the marketplace owner"
    )
    assert rows["blank-author"]["repository_url"] == (
        "https://github.com/acme/plugins.git"
    )
    assert rows["named-author"]["author"] == "Jo Author", (
        "a named manifest author outranks the marketplace owner"
    )
    assert rows["named-author"]["repository_url"] == (
        "https://github.com/acme/plugins/tree/main/plugins/named-author"
    ), "the manifest's homepage is the most specific place on record"


# ── Scenario 3: capabilities are replies, not role guesses ─────────────────


def test_addon_capabilities_are_server_replies_not_role_guesses(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    ``can_add`` / ``can_manage`` / ``can_share`` answer two questions the client
    only holds half of — the role, AND whether this agent is a consumer install:

      1. A developer's own agent with a marketplace plugin and a local skill →
         every capability true where it applies.
      2. The same user demoted to ``agent-user`` → every capability false, with
         the rows themselves unchanged (read-only, not hidden).
      3. A second developer installing that agent's bundle gets a FOREIGN
         install: ``can_add`` false and ``can_share`` false on the very same
         local skill, even though the role says developer.
    """
    # ── Phase 1: the owning developer ────────────────────────────────────
    _install_adapter(patch_environment_adapter, index=_index([_row("handbook")]))
    owner, owner_headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, owner_headers, "Capability-Agent")

    marketplace = create_marketplace(client, superuser_token_headers)
    plugin = seed_marketplace_plugin(
        client, superuser_token_headers, marketplace["id"], name="reporting"
    )
    install_agent_plugin(client, owner_headers, agent_id, plugin["id"])

    payload = refresh_agent_addons(client, owner_headers, agent_id)
    assert payload["can_add"] is True
    rows = addons_by_name(payload)
    assert set(rows) == {"reporting", "handbook"}
    assert rows["reporting"]["can_manage"] is True
    assert rows["handbook"]["can_share"] is True
    assert rows["handbook"]["can_manage"] is False

    # ── Phase 2: the same person, without the role ───────────────────────
    _demote_to_user(client, superuser_token_headers, owner["id"])

    payload = get_agent_addons(client, owner_headers, agent_id)
    assert payload["can_add"] is False
    rows = addons_by_name(payload)
    assert set(rows) == {"reporting", "handbook"}, (
        "losing the role makes the tab read-only, it does not hide the rows"
    )
    assert rows["reporting"]["can_manage"] is False
    assert rows["handbook"]["can_share"] is False
    assert rows["handbook"]["can_manage"] is False

    promote_to_developer(client, superuser_token_headers, owner["id"])

    # ── Phase 3: a foreign install is use-only for every role ────────────
    # Bundle publish refuses an agent whose plugin files are not on disk (a
    # revision that ships a plugin nobody can install would be broken and
    # immutable), so seed the directory the container would have created.
    plugin_dir = (
        workspace_root(env_id)
        / "plugins"
        / rows["reporting"]["marketplace_name"]
        / "reporting"
    )
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.md").write_text("Reporting helpers.", encoding="utf-8")

    publish_bundle_and_make_public(
        client, owner_headers, agent_id, notes="v1 with a handbook"
    )
    consumer, con_headers = make_developer(client, superuser_token_headers)
    fresh_owner = get_agent(client, owner_headers, agent_id)
    installed = install_bundle(client, con_headers, fresh_owner["bundle_id"])
    installed_id = installed["id"]

    payload = refresh_agent_addons(client, con_headers, installed_id)
    assert payload["can_add"] is False, (
        "a consumer install is use-only — the developer role must not unlock "
        "adding addons to somebody else's bundle"
    )
    rows = addons_by_name(payload)
    assert "handbook" in rows, (
        "the install's env reports the same index — an empty list here would "
        "make the assertion below vacuous"
    )
    assert rows["reporting"]["source"] == "bundle", (
        "the publisher's plugin arrives as a bundle-delivered row, so the "
        "capability assertions below cover a real link and not just a folder"
    )
    for row in payload["addons"]:
        assert row["can_share"] is False, row["name"]
        assert row["can_manage"] is False, row["name"]


# ── Scenario 4: a link whose upstream is gone ──────────────────────────────


def test_deleting_a_marketplace_leaves_the_install_flagged_not_deleted(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    A marketplace disappearing underneath an install must not uninstall it.

      1. A healthy marketplace install: one skill folded into its row, ``ok``.
      2. The administrator deletes the marketplace → the agent's row **stays**,
         with ``plugin_id`` null, reported ``error`` / ``source_unavailable``.
         Its files are about to be pruned and there is nothing left to upgrade
         from, so the row says so; the only remaining verb is uninstall, and
         that is the owner's to press.
      3. The skill the engine still loads stays folded into that row. It does
         not reappear as a second, orphan row: the link snapshots its
         ``<marketplace>/<plugin>`` directory identity at install time, so the
         fold survives the loss of the live rows.

    This is the ``ON DELETE SET NULL`` the ``plugin_id`` column has always
    documented, finally reachable: ``LLMPluginMarketplacePlugin.agent_links``
    used to declare ``cascade="all, delete-orphan"``, so SQLAlchemy deleted the
    user's link in Python before the database constraint was ever consulted and
    an admin's delete silently uninstalled the plugin from every agent that had
    it. The relationship is now ``passive_deletes``.
    """
    # ── Phase 1: a healthy marketplace install ───────────────────────────
    adapter = _install_adapter(patch_environment_adapter)
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Orphaned-Marketplace")

    marketplace = create_marketplace(client, superuser_token_headers)
    plugin = seed_marketplace_plugin(
        client,
        superuser_token_headers,
        marketplace["id"],
        name="reporting",
        version="2.3",
    )
    install_agent_plugin(client, headers, agent_id, plugin["id"])

    healthy = get_agent_addons(client, headers, agent_id)
    marketplace_ref = (
        f"{addons_by_name(healthy)['reporting']['marketplace_name']}/reporting"
    )
    adapter.skills_index = _index(
        [_row("alpha-report", source="plugin", plugin_ref=marketplace_ref)],
        tree_hash="hash-2",
    )
    healthy = refresh_agent_addons(client, headers, agent_id)
    healthy_rows = addons_by_name(healthy)
    assert set(healthy_rows) == {"reporting"}
    assert healthy_rows["reporting"]["status"] == "ok"
    assert healthy_rows["reporting"]["status_code"] is None
    assert healthy_rows["reporting"]["link"]["plugin_id"] == plugin["id"]
    assert skill_names(healthy_rows["reporting"]) == ["alpha-report"]

    # ── Phase 2: the marketplace is deleted underneath it ────────────────
    delete_marketplace(client, superuser_token_headers, marketplace["id"])

    payload = get_agent_addons(client, headers, agent_id)
    rows = addons_by_name(payload)
    assert set(rows) == {"reporting"}, (
        "the install survives its marketplace: an admin deleting a catalog "
        "must not uninstall the plugin from somebody else's agent"
    )

    row = rows["reporting"]
    assert row["link"] is not None
    assert row["link"]["plugin_id"] is None, (
        "ON DELETE SET NULL is what keeps the row: the link outlives the "
        "plugin row it pointed at"
    )
    assert row["orphan"] is False
    assert row["status"] == "error"
    assert row["status_code"] == "source_unavailable", (
        "nothing to re-sync from and no version to compare — reporting this "
        "as healthy would leave the one row the user can act on looking fine"
    )

    # ── Phase 3: the skill stays where it was, on the row ────────────────
    assert skill_names(row) == ["alpha-report"], (
        "the plugin directory is still on disk and the engine still loads its "
        "skill; the snapshotted directory identity keeps it folded into the "
        "install row instead of splitting off a second, orphan one"
    )
    assert [r for r in payload["addons"] if r["orphan"]] == []
    assert payload["counts"] == {"plugins": 1, "skills": 0, "local_skills": 0}


# ── Scenario 5: an entry dropped upstream ──────────────────────────────────


def _claude_repo(names: list[str], marketplace_name: str = "shared-tools"):
    """A ``.claude-plugin/marketplace.json`` listing exactly ``names``.

    ``marketplace_name`` is the name the repository claims for itself — the
    sync copies it onto the row, and it is half of every install's on-disk
    directory identity.
    """

    def populate(root: Path) -> None:
        catalog = root / ".claude-plugin" / "marketplace.json"
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_text(
            json.dumps(
                {
                    "name": marketplace_name,
                    "plugins": [
                        {
                            "name": name,
                            "description": f"The {name} plugin.",
                            "version": "1.0",
                            "source": f"./plugins/{name}",
                        }
                        for name in names
                    ],
                }
            ),
            encoding="utf-8",
        )

    return populate


def test_an_entry_dropped_upstream_keeps_its_install_and_flags_it(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    The other way a plugin row disappears: not the marketplace being deleted,
    but the upstream repository dropping the entry, which the sync deletes on
    its "remove plugins no longer in marketplace" pass.

      1. A marketplace with two plugins; the agent installs one.
      2. Upstream drops it and the marketplace re-syncs → the plugin row is
         gone, the **link is not**: ``plugin_id`` is NULL and the snapshot
         names it was installed with are intact.
      3. The projection keeps the row, flags it ``error`` /
         ``source_unavailable``, and folds the skill the engine still loads
         into it — not into a second, orphan row.
      4. Upgrade answers ``409 source_unavailable``: there is nothing to move
         to, and answering "link not found" would send the owner looking for a
         row they can plainly see.
      5. Uninstall — the one verb left — still works, and takes the row away.

    The sibling path (an administrator deleting the whole marketplace) is
    ``test_deleting_a_marketplace_leaves_the_install_flagged_not_deleted``;
    both are the same ``passive_deletes`` + ``ON DELETE SET NULL`` pair.
    """
    # ── Phase 1: two entries upstream, one installed ─────────────────────
    adapter = _install_adapter(patch_environment_adapter)
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Dropped-Upstream")

    marketplace = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/shared-tools.git",
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting", "charting"]),
        commit_hash="aaaa11112222",
    )
    rows = plugins_by_name(discover_plugins(client, headers))
    assert set(rows) == {"reporting", "charting"}

    install_agent_plugin(client, headers, agent_id, rows["reporting"]["id"])
    link = list_agent_plugins(client, headers, agent_id)[0]
    assert link["plugin_id"] == rows["reporting"]["id"]
    assert link["snapshot_marketplace_name"] == "shared-tools"
    assert link["snapshot_plugin_name"] == "reporting"

    adapter.skills_index = _index(
        [_row("alpha-report", source="plugin", plugin_ref="shared-tools/reporting")],
        tree_hash="hash-2",
    )
    healthy = addons_by_name(refresh_agent_addons(client, headers, agent_id))
    assert healthy["reporting"]["status"] == "ok"
    assert skill_names(healthy["reporting"]) == ["alpha-report"]

    # ── Phase 2: upstream drops the entry ────────────────────────────────
    resynced = sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["charting"]),
        commit_hash="bbbb33334444",
    )
    assert resynced["status"] == "connected"
    assert set(plugins_by_name(discover_plugins(client, headers))) == {"charting"}

    links = list_agent_plugins(client, headers, agent_id)
    assert len(links) == 1, (
        "an upstream repo dropping an entry is not consent to uninstall it "
        "from somebody's agent"
    )
    orphaned_link = links[0]
    assert orphaned_link["id"] == link["id"]
    assert orphaned_link["plugin_id"] is None, "ON DELETE SET NULL, not a cascade"
    assert orphaned_link["snapshot_marketplace_name"] == "shared-tools"
    assert orphaned_link["snapshot_plugin_name"] == "reporting", (
        "the frozen names are all that is left to name the row and to match "
        "it against what the environment still has on disk"
    )

    # ── Phase 3: the projection keeps it, flagged ────────────────────────
    payload = get_agent_addons(client, headers, agent_id)
    projected = addons_by_name(payload)
    assert set(projected) == {"reporting"}
    row = projected["reporting"]
    assert row["orphan"] is False
    assert row["status"] == "error"
    assert row["status_code"] == "source_unavailable"
    assert skill_names(row) == ["alpha-report"]
    assert [r for r in payload["addons"] if r["orphan"]] == [], (
        "one directory, one row — the snapshotted identity is what keeps the "
        "skill folded instead of splitting off a duplicate"
    )

    # ── Phase 4: upgrade says what is actually wrong ─────────────────────
    refused = upgrade_agent_plugin(
        client, headers, agent_id, row["link"]["id"], expected_status=409
    )
    assert refused["detail"]["code"] == "source_unavailable"
    assert refused["detail"]["message"]

    # ── Phase 5: uninstall is the verb that is left ──────────────────────
    r = client.delete(
        f"{API}/llm-plugins/agents/{agent_id}/plugins/{row['link']['id']}",
        headers=headers,
    )
    assert r.status_code == 200, r.text

    after = get_agent_addons(client, headers, agent_id)
    remaining = after["addons"]
    assert [r["name"] for r in remaining] == ["reporting"], (
        "the link is gone; what the index still reports from a directory the "
        "next environment sync will prune is the orphan row — named for the "
        "plugin directory it came from, not for a second install"
    )
    orphan = remaining[0]
    assert orphan["orphan"] is True
    assert orphan["status"] == "error"
    assert orphan["status_code"] == "orphan"
    assert orphan["link"] is None
    assert orphan["can_manage"] is False, "there is nothing left to manage"
    assert skill_names(orphan) == ["alpha-report"], (
        "the skill the engine can still load has to stay visible somewhere"
    )


# ── Scenario 6: the entry comes back ───────────────────────────────────────


def test_an_entry_that_comes_back_adopts_the_install_it_left_behind(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    An install surviving its plugin row is only half a policy. Without a way
    back, one bad sync upstream — an entry dropped for a day, a directory
    renamed and renamed back — is permanent: the manifest stops shipping the
    plugin, the environment prunes its directory, the row reads
    ``source_unavailable`` for the rest of the install's life, and re-installing
    stacks a second link beside the dead one.

      1. Two entries upstream, one installed, one skill folded into its row.
      2. Upstream drops it → the link survives, orphaned and flagged.
      3. Upstream puts it back → the next sync re-attaches **the same link** to
         the new plugin row: one row again, healthy, with its skill, and no
         second install anywhere.

    Matching is on ``<marketplace>/<plugin>`` — the directory identity frozen
    at install time and backfilled by migration ``c8d2e5b71a04`` — the same
    pair the skills fold on.
    """
    # ── Phase 1: installed and healthy ───────────────────────────────────
    adapter = _install_adapter(patch_environment_adapter)
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Entry-Returns")

    marketplace = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/shared-tools.git",
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting", "charting"]),
        commit_hash="aaaa11112222",
    )
    rows = plugins_by_name(discover_plugins(client, headers))
    install_agent_plugin(client, headers, agent_id, rows["reporting"]["id"])
    link = list_agent_plugins(client, headers, agent_id)[0]

    adapter.skills_index = _index(
        [_row("alpha-report", source="plugin", plugin_ref="shared-tools/reporting")],
        tree_hash="hash-2",
    )
    healthy = addons_by_name(refresh_agent_addons(client, headers, agent_id))
    assert healthy["reporting"]["status"] == "ok"

    # ── Phase 2: dropped upstream ────────────────────────────────────────
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["charting"]),
        commit_hash="bbbb33334444",
    )
    orphaned = addons_by_name(get_agent_addons(client, headers, agent_id))
    assert orphaned["reporting"]["link"]["plugin_id"] is None
    assert orphaned["reporting"]["status_code"] == "source_unavailable"

    # ── Phase 3: back upstream, and back on the agent ────────────────────
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting", "charting"]),
        commit_hash="cccc55556666",
    )
    returned = plugins_by_name(discover_plugins(client, headers))["reporting"]

    links = list_agent_plugins(client, headers, agent_id)
    assert len(links) == 1, "the install was re-attached, not duplicated"
    assert links[0]["id"] == link["id"], "the owner's row, with its own toggles"
    assert links[0]["plugin_id"] == returned["id"], (
        "an entry that comes back has to adopt the installs it left behind — "
        "nothing else in the service ever re-points a link, so without this "
        "the row is source_unavailable forever"
    )

    payload = get_agent_addons(client, headers, agent_id)
    healed = addons_by_name(payload)
    assert set(healed) == {"reporting"}
    assert healed["reporting"]["status"] == "ok"
    assert healed["reporting"]["status_code"] is None
    assert skill_names(healed["reporting"]) == ["alpha-report"]
    assert [r for r in payload["addons"] if r["orphan"]] == []


def test_a_link_orphaned_by_an_older_build_is_healed_by_the_next_sync(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    db: Session,
) -> None:
    """
    The same re-attachment, applied backwards: an install orphaned while the
    platform had no way back is healed by the next ordinary sync, with nobody
    doing anything about it.

    The seam (``orphan_plugin_link``) writes the one state the code no longer
    produces — ``plugin_id`` NULL with the snapshot names intact — because that
    is precisely the fleet of installs this fix has to reach.
    """
    adapter = _install_adapter(patch_environment_adapter)
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Legacy-Orphan")

    marketplace = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/shared-tools.git",
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting"]),
        commit_hash="aaaa11112222",
    )
    plugin = plugins_by_name(discover_plugins(client, headers))["reporting"]
    install_agent_plugin(client, headers, agent_id, plugin["id"])
    link = list_agent_plugins(client, headers, agent_id)[0]

    adapter.skills_index = _index(
        [_row("alpha-report", source="plugin", plugin_ref="shared-tools/reporting")],
        tree_hash="hash-2",
    )
    refresh_agent_addons(client, headers, agent_id)

    # The state an older build left behind.
    orphan_plugin_link(db, link["id"])
    stranded = addons_by_name(get_agent_addons(client, headers, agent_id))
    assert stranded["reporting"]["status_code"] == "source_unavailable"

    # An ordinary re-sync — the entry never went anywhere.
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting"]),
        commit_hash="bbbb33334444",
    )

    healed = addons_by_name(get_agent_addons(client, headers, agent_id))
    assert healed["reporting"]["link"]["plugin_id"] == plugin["id"], (
        "the fix has to reach the installs that were already broken, not only "
        "the ones broken from here on"
    )
    assert healed["reporting"]["status"] == "ok"
    assert skill_names(healed["reporting"]) == ["alpha-report"]


# ── Scenario 7: one directory, two links ───────────────────────────────────


def test_a_directory_claimed_by_two_links_folds_onto_the_one_that_still_works(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    db: Session,
) -> None:
    """
    The pre-fix world's leftovers: a dead install and the re-install the owner
    made beside it, both naming ``shared-tools/reporting``, which is one
    directory on disk and therefore one set of skills.

    The link query is unordered, so folding onto "whichever came back first"
    is a coin flip — and the losing outcome is the bad one: the working install
    renders as an empty row while the error row above it lists the skills the
    engine actually loaded. The row that can still deliver files wins.

      1. An install, orphaned by an older build (the seam), then re-installed —
         two links, one directory.
      2. The projection folds the directory's skills onto the live row; the
         dead one keeps its ``source_unavailable`` flag and no skills.
      3. Re-syncing the marketplace leaves the dead link alone: the owner has
         a working install of that plugin already, and re-attaching would put
         two rows on one ``(agent, plugin)``. Which of the two to remove is the
         owner's call, not a sync's.
    """
    # ── Phase 1: one directory, two links ────────────────────────────────
    adapter = _install_adapter(patch_environment_adapter)
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Two-Links")

    marketplace = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/shared-tools.git",
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting"]),
        commit_hash="aaaa11112222",
    )
    plugin = plugins_by_name(discover_plugins(client, headers))["reporting"]
    install_agent_plugin(client, headers, agent_id, plugin["id"])
    dead_link = list_agent_plugins(client, headers, agent_id)[0]

    orphan_plugin_link(db, dead_link["id"])
    install_agent_plugin(client, headers, agent_id, plugin["id"])
    links = list_agent_plugins(client, headers, agent_id)
    assert len(links) == 2
    live_link = next(link for link in links if link["plugin_id"] == plugin["id"])

    adapter.skills_index = _index(
        [_row("alpha-report", source="plugin", plugin_ref="shared-tools/reporting")],
        tree_hash="hash-2",
    )
    refresh_agent_addons(client, headers, agent_id)

    # ── Phase 2: the fold goes to the row that works ─────────────────────
    payload = get_agent_addons(client, headers, agent_id)
    by_link = {r["link"]["id"]: r for r in payload["addons"] if r["link"]}
    assert set(by_link) == {dead_link["id"], live_link["id"]}

    assert skill_names(by_link[live_link["id"]]) == ["alpha-report"], (
        "the working install is the row the user acts on; handing its skills "
        "to the dead row beside it renders it as an empty plugin"
    )
    assert by_link[live_link["id"]]["status"] == "ok"
    assert by_link[dead_link["id"]]["skills"] == []
    assert by_link[dead_link["id"]]["status_code"] == "source_unavailable"
    assert [r for r in payload["addons"] if r["orphan"]] == [], (
        "the directory is claimed; nothing about it is unattributable"
    )

    # ── Phase 3: the sync leaves the standing install alone ──────────────
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting"]),
        commit_hash="bbbb33334444",
    )
    after = {
        link["id"]: link for link in list_agent_plugins(client, headers, agent_id)
    }
    assert after[dead_link["id"]]["plugin_id"] is None, (
        "the agent already holds a live link to this plugin — a second one "
        "would break the (agent, plugin) uniqueness the install path relies on"
    )
    assert after[live_link["id"]]["plugin_id"] == plugin["id"]


# ── Scenario 8: the adoption's two edges ───────────────────────────────────


def test_a_later_repository_claiming_the_name_does_not_capture_the_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    ``<marketplace>/<plugin>`` is unique at any instant, but it is not proof
    of origin: a marketplace name is released when its row is deleted, and the
    next repository to claim it is chosen by whatever an upstream author wrote
    in their ``marketplace.json``.

    Adopting an orphan on the name alone would therefore re-point installs on
    agents belonging to people who never chose that repository — and the next
    environment sync would fetch and run its files. An install is necessarily
    younger than the marketplace it was made from, so a marketplace registered
    afterwards cannot be that one, and does not adopt.

      1. An install from ``shared-tools``, orphaned when it is deleted.
      2. A different repository is registered and declares the same
         marketplace name and the same entry name.
      3. The orphan stays an orphan; the stranger's plugin row is installable
         on its own terms, by somebody who asks for it.
    """
    _install_adapter(patch_environment_adapter)
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Name-Squatter")

    marketplace = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/shared-tools.git",
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting"]),
        commit_hash="aaaa11112222",
    )
    plugin = plugins_by_name(discover_plugins(client, headers))["reporting"]
    install_agent_plugin(client, headers, agent_id, plugin["id"])
    link = list_agent_plugins(client, headers, agent_id)[0]

    delete_marketplace(client, superuser_token_headers, marketplace["id"])
    assert list_agent_plugins(client, headers, agent_id)[0]["plugin_id"] is None

    # Somebody else's repository, claiming the name that was just released.
    stranger = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/someone-else/lookalike.git",
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        stranger["id"],
        populate=_claude_repo(["reporting"]),
        commit_hash="bbbb33334444",
    )
    stranger_plugin = plugins_by_name(discover_plugins(client, headers))["reporting"]
    assert stranger_plugin["id"] != plugin["id"]

    after = list_agent_plugins(client, headers, agent_id)
    assert len(after) == 1
    assert after[0]["id"] == link["id"]
    assert after[0]["plugin_id"] is None, (
        "an install must never be re-pointed at a repository its owner never "
        "chose — the name was free, which is exactly why it proves nothing"
    )
    row = addons_by_name(get_agent_addons(client, headers, agent_id))["reporting"]
    assert row["status_code"] == "source_unavailable"


def test_a_marketplace_renamed_upstream_takes_its_installs_with_it(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    The marketplace name is not a label — it is the directory every install
    resolves through, and the sync rewrites it from repository metadata. So a
    rename upstream moves the directory, and the frozen copy each install
    carries has to move with it: left stale, the projection would fold the
    directory's skills onto a second, synthetic row, and a later orphan of
    that install would name a marketplace that no longer exists.
    """
    adapter = _install_adapter(patch_environment_adapter)
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Renamed-Upstream")

    marketplace = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/shared-tools.git",
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting"]),
        commit_hash="aaaa11112222",
    )
    plugin = plugins_by_name(discover_plugins(client, headers))["reporting"]
    install_agent_plugin(client, headers, agent_id, plugin["id"])
    assert (
        list_agent_plugins(client, headers, agent_id)[0]["snapshot_marketplace_name"]
        == "shared-tools"
    )

    # Upstream renames the marketplace; the entry is untouched.
    renamed = sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting"], marketplace_name="house-tools"),
        commit_hash="bbbb33334444",
    )
    assert renamed["name"] == "house-tools"

    link = list_agent_plugins(client, headers, agent_id)[0]
    assert link["snapshot_marketplace_name"] == "house-tools", (
        "the install's frozen directory identity has to follow the directory "
        "it names, or it names one nothing resolves to"
    )

    # The environment re-materialises the plugin under the new directory.
    adapter.skills_index = _index(
        [_row("alpha-report", source="plugin", plugin_ref="house-tools/reporting")],
        tree_hash="hash-2",
    )
    payload = refresh_agent_addons(client, headers, agent_id)
    rows = addons_by_name(payload)
    assert set(rows) == {"reporting"}
    assert skill_names(rows["reporting"]) == ["alpha-report"]
    assert [r for r in payload["addons"] if r["orphan"]] == []


# ── Scenario 9: the repository is what proves origin ───────────────────────

#: The repository the install in the takeover tests is actually made from.
_VICTIM_REPO = "https://github.com/example/shared-tools.git"


def _install_then_free_the_name(
    client: TestClient,
    superuser_headers: dict[str, str],
    headers: dict[str, str],
    agent_id: str,
    *,
    elder_url: str,
) -> tuple[dict, dict]:
    """Set up the takeover the two edge tests below share.

    An **older** marketplace already exists (``elder_url``), then a second one
    is registered from ``_VICTIM_REPO``, publishes ``shared-tools/reporting``,
    is installed onto ``agent_id``, and is deleted — which releases the name
    ``shared-tools`` and orphans the install.

    Everything here is identical between the two tests; ``elder_url`` is the
    only difference, and it is the only thing that changes the outcome. The
    elder is registered under a neutral URL and then re-pointed, because the
    name a registration derives from its URL
    (``_generate_name_from_url``) would otherwise collide with the victim's.

    Returns ``(elder marketplace, the orphaned link)``.
    """
    # Registered BEFORE the install, so ``link.created_at >= elder.created_at``
    # holds and the age guard has nothing to say about what follows.
    elder = create_marketplace(
        client, superuser_headers, url="https://github.com/example/elder-place.git"
    )
    update_marketplace(client, superuser_headers, elder["id"], {"url": elder_url})

    victim = create_marketplace(client, superuser_headers, url=_VICTIM_REPO)
    sync_marketplace(
        client,
        superuser_headers,
        victim["id"],
        populate=_claude_repo(["reporting"]),
        commit_hash="aaaa11112222",
    )
    plugin = plugins_by_name(discover_plugins(client, headers))["reporting"]
    install_agent_plugin(client, headers, agent_id, plugin["id"])
    link = list_agent_plugins(client, headers, agent_id)[0]
    assert link["snapshot_marketplace_name"] == "shared-tools"

    delete_marketplace(client, superuser_headers, victim["id"])
    orphaned = list_agent_plugins(client, headers, agent_id)[0]
    assert orphaned["plugin_id"] is None, "the delete orphans, it does not uninstall"
    return elder, orphaned


def test_an_older_marketplace_taking_the_freed_name_does_not_capture_the_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    **The security case.** Refusing adoption on age alone is not enough, and
    this is the direction that gets through.

    ``idx_marketplace_name_unique`` frees a marketplace name exactly when its
    row is deleted — which is the very state that produced these orphans — and
    ``sync_marketplace`` reassigns ``marketplace.name`` from the third-party
    repository's own ``marketplace.json`` on every sync
    (``llm_plugin_service.py:497-498``). So a name moves to another row in
    *either* age direction, and the ``created_at`` guard only sees one of them:
    it rejects a marketplace row **younger** than the link
    (``test_a_later_repository_claiming_the_name_does_not_capture_the_install``),
    and an upstream rename goes the other way.

    Without the repository check, an **older** marketplace — registered long
    before anybody installed anything, then renamed upstream onto the freed
    name — would adopt every orphan the deleted one left behind, and the next
    environment sync would clone and run code from a repository the agent's
    owner never chose.

      1. An install from ``shared-tools``, orphaned when that marketplace is
         deleted and its name released.
      2. An older marketplace, pointing at **someone else's repository**, syncs
         and its ``marketplace.json`` claims the freed name and the same entry
         name. Directory identity matches; the age guard is satisfied.
      3. Nothing is adopted. The link stays orphaned and the tab still says
         ``source_unavailable`` rather than quietly pointing at the stranger.

    The mirror of this test — same setup, same rename, only the elder's URL
    changed to the same repository — is
    ``test_the_same_repository_under_the_freed_name_adopts_its_orphans``, and it
    *does* adopt. The pair is the whole rule: the URL is the difference.
    """
    _install_adapter(patch_environment_adapter)
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Elder-Squatter")

    elder, link = _install_then_free_the_name(
        client,
        superuser_token_headers,
        headers,
        agent_id,
        # Somebody else's repository entirely.
        elder_url="https://github.com/someone-else/lookalike.git",
    )

    # ── The rename that takes the freed name ─────────────────────────────
    renamed = sync_marketplace(
        client,
        superuser_token_headers,
        elder["id"],
        populate=_claude_repo(["reporting"], marketplace_name="shared-tools"),
        commit_hash="bbbb33334444",
    )
    assert renamed["name"] == "shared-tools", (
        "the takeover has to actually happen, or this test proves nothing"
    )
    stranger_plugin = plugins_by_name(discover_plugins(client, headers))["reporting"]

    # ── The orphan is not adopted ────────────────────────────────────────
    after = list_agent_plugins(client, headers, agent_id)
    assert len(after) == 1
    assert after[0]["id"] == link["id"]
    assert after[0]["plugin_id"] is None, (
        "an older row that acquires a freed name is not the marketplace this "
        "install came from — adopting it would fetch and run a stranger's "
        "code in this agent's container on the next environment sync"
    )
    assert after[0]["plugin_id"] != stranger_plugin["id"]

    row = addons_by_name(get_agent_addons(client, headers, agent_id))["reporting"]
    assert row["status"] == "error"
    assert row["status_code"] == "source_unavailable", (
        "the owner has to keep seeing that this install has no source, not a "
        "healthy row silently re-pointed at a repository they never chose"
    )


def test_the_same_repository_under_the_freed_name_adopts_its_orphans(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    The other half of the pair: byte-for-byte the setup of
    ``test_an_older_marketplace_taking_the_freed_name_does_not_capture_the_install``
    — same ages, same names, same rename — with the elder pointed at **the same
    repository the install was made from**. That one difference flips the
    outcome, which is what makes the rule "the repository decides" rather than
    "older rows are refused".

    The two URLs are also spelled differently: the install was made from
    ``…/shared-tools.git`` and the elder is registered as ``…/shared-tools``.
    ``_canonical_repository_url`` normalises the trailing ``.git`` away on
    purpose — ``_generate_name_from_url`` already treats the two spellings as
    one repository when it derives a marketplace's identity — so an
    administrator who re-registered the repo with the other spelling is not
    punished for it.
    """
    adapter = _install_adapter(patch_environment_adapter)
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Elder-Same-Repo")

    elder, link = _install_then_free_the_name(
        client,
        superuser_token_headers,
        headers,
        agent_id,
        # The same repository as ``_VICTIM_REPO``, spelled without ``.git``.
        elder_url="https://github.com/example/shared-tools",
    )
    assert get_marketplace(client, superuser_token_headers, elder["id"])["url"] != (
        _VICTIM_REPO
    ), "the spelling difference is the point; identical strings prove less"

    renamed = sync_marketplace(
        client,
        superuser_token_headers,
        elder["id"],
        populate=_claude_repo(["reporting"], marketplace_name="shared-tools"),
        commit_hash="bbbb33334444",
    )
    assert renamed["name"] == "shared-tools"
    returned = plugins_by_name(discover_plugins(client, headers))["reporting"]

    after = list_agent_plugins(client, headers, agent_id)
    assert len(after) == 1, "adopted, not duplicated"
    assert after[0]["id"] == link["id"], "the owner's row, with its own toggles"
    assert after[0]["plugin_id"] == returned["id"], (
        "same repository, same directory identity, and the marketplace "
        "predates the install — there is nothing left to be suspicious of, and "
        "refusing here would strand an install for a trailing '.git'"
    )

    adapter.skills_index = _index(
        [_row("alpha-report", source="plugin", plugin_ref="shared-tools/reporting")],
        tree_hash="hash-2",
    )
    payload = refresh_agent_addons(client, headers, agent_id)
    rows = addons_by_name(payload)
    assert set(rows) == {"reporting"}
    assert rows["reporting"]["status"] == "ok"
    assert rows["reporting"]["status_code"] is None
    assert skill_names(rows["reporting"]) == ["alpha-report"]
    assert [r for r in payload["addons"] if r["orphan"]] == []


def test_an_install_with_no_recorded_repository_is_never_adopted(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    db: Session,
) -> None:
    """
    Null fails closed.

    A marketplace install made before ``snapshot_repository_url`` existed AND
    already orphaned before migration ``d7b41e0c9a35`` ran keeps a NULL there:
    the migration backfills from the live marketplace row, and an orphan has
    none. Such a link carries no evidence of where its files came from, and
    "unknown" must never read as "matches" — the alternative is guessing a
    repository from a name, which is the confusion the column was added to end.

    This is
    ``test_an_entry_that_comes_back_adopts_the_install_it_left_behind`` with one
    column cleared: same marketplace row (so the name and the age match beyond
    argument), same drop, same return. It adopts there and must not adopt here.

    The remedy for these links is uninstall and re-install, which writes the
    URL — so the row stays named and reported ``source_unavailable`` rather
    than vanishing.
    """
    _install_adapter(patch_environment_adapter)
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "No-Repo-Snapshot")

    marketplace = create_marketplace(
        client, superuser_token_headers, url=_VICTIM_REPO
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting", "charting"]),
        commit_hash="aaaa11112222",
    )
    plugin = plugins_by_name(discover_plugins(client, headers))["reporting"]
    install_agent_plugin(client, headers, agent_id, plugin["id"])
    link = list_agent_plugins(client, headers, agent_id)[0]

    # Dropped upstream — the ordinary way a link is orphaned, with the
    # marketplace row itself untouched.
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["charting"]),
        commit_hash="bbbb33334444",
    )
    assert list_agent_plugins(client, headers, agent_id)[0]["plugin_id"] is None

    # The state migration d7b41e0c9a35 could not repair.
    clear_link_repository_url(db, link["id"])

    # The entry comes back. Name matches, plugin name matches, and the
    # marketplace is the very one the install was made from, so its age matches
    # too. The missing URL is the only thing standing in the way.
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting", "charting"]),
        commit_hash="cccc55556666",
    )

    after = list_agent_plugins(client, headers, agent_id)
    assert len(after) == 1
    assert after[0]["id"] == link["id"]
    assert after[0]["plugin_id"] is None, (
        "a link with no recorded repository has no identity to verify; "
        "adopting it on the strength of its name is the exact inference the "
        "repository column exists to refuse"
    )
    row = addons_by_name(get_agent_addons(client, headers, agent_id))["reporting"]
    assert row["status"] == "error"
    assert row["status_code"] == "source_unavailable"


def test_a_marketplace_registered_after_the_install_never_adopts_it(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    The age guard, in isolation: matching repositories are **necessary, not
    sufficient**.

    ``test_a_later_repository_claiming_the_name_does_not_capture_the_install``
    exercises a younger row with a *different* URL, so either condition could
    be the one refusing it. Here the URL is identical — an administrator
    deleting a marketplace and registering the very same repository again — and
    the refusal can only be ``link.created_at >= marketplace.created_at``. That
    keeps the age guard from being quietly dropped as redundant once the
    repository check exists; two independent conditions are what stop one
    mistake in either from being total.

    Note for whoever changes this next: ``_reattach_orphaned_links``' own
    docstring claims that "an administrator who deletes a marketplace and
    re-adds the same repository is now adopted again". This test is that
    scenario, and it is refused — the re-added row is younger than the install.
    Refusing is the safe direction (the remedy is a re-install), so the
    behaviour is pinned as it stands and the docstring is what disagrees with
    the code.
    """
    _install_adapter(patch_environment_adapter)
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Re-Registered")

    marketplace = create_marketplace(
        client, superuser_token_headers, url=_VICTIM_REPO
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo(["reporting"]),
        commit_hash="aaaa11112222",
    )
    plugin = plugins_by_name(discover_plugins(client, headers))["reporting"]
    install_agent_plugin(client, headers, agent_id, plugin["id"])
    link = list_agent_plugins(client, headers, agent_id)[0]

    delete_marketplace(client, superuser_token_headers, marketplace["id"])
    assert list_agent_plugins(client, headers, agent_id)[0]["plugin_id"] is None

    # The same repository, registered again — and therefore a row younger than
    # the install it would be adopting.
    again = create_marketplace(client, superuser_token_headers, url=_VICTIM_REPO)
    assert again["id"] != marketplace["id"]
    resynced = sync_marketplace(
        client,
        superuser_token_headers,
        again["id"],
        populate=_claude_repo(["reporting"], marketplace_name="shared-tools"),
        commit_hash="bbbb33334444",
    )
    # Without this, a refusal caused by a name that failed to match would read
    # as the age guard doing its job. Every condition but the age one holds.
    assert resynced["name"] == "shared-tools"
    assert resynced["url"] == _VICTIM_REPO
    assert "reporting" in plugins_by_name(discover_plugins(client, headers))

    after = list_agent_plugins(client, headers, agent_id)
    assert len(after) == 1
    assert after[0]["id"] == link["id"]
    assert after[0]["plugin_id"] is None, (
        "an install cannot predate the marketplace it was made from, so a row "
        "registered afterwards is not that marketplace whatever its URL says"
    )
    row = addons_by_name(get_agent_addons(client, headers, agent_id))["reporting"]
    assert row["status_code"] == "source_unavailable"
