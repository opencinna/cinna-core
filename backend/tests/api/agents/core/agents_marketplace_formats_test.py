"""Addon marketplace formats — Codex, bare-skills, and the refusals.

A marketplace's ``type`` decides which parser reads its repository, and the
parser decides what the platform is willing to install from it. These tests
drive the **real** parsers: only the git clone is replaced (by a fixture tree
written into the throwaway clone directory the sync itself created), so the
catalog file, the per-plugin manifest, the directory walk, ``_upsert_plugins``
and discover are all the shipping code path.

Scenarios
---------
  1. A Codex repository: ``local`` / ``url`` / ``git-subdir`` / ``npm`` /
     unrecognised sources map onto rows with the right coordinates and the
     right verdict; an apps-only manifest is refused as ``app_connector_only``;
     a path that escapes the repository is refused per **entry**
     (``unsafe_path``) while its siblings sync; installable rows sort first.
  2. A bare-skills repository in both layouts (``skills/<name>/`` and
     ``<name>/``): one row per skill, a folder with no valid ``SKILL.md``
     listed as ``no_skill_md``, and the marketplace's own name left alone
     (it is the on-disk directory every install resolves through).
  3. An unknown ``type`` is a 422 on create and on update; a legacy row that
     already holds one syncs to ``status=error`` rather than being parsed as
     Claude — and pointing it at a known format recovers it.
  4. Installing an unsupported entry is refused with ``409
     plugin_unsupported`` and creates no link; the supported sibling installs
     and freezes both snapshot names; the ``plugin_type`` and ``marketplace_id``
     discover filters narrow to one format and to one repository.
  5. A catalog that cannot be read — a missing ``marketplace.json``, a
     ``skills`` repository with no skill folders — is ``status=error`` and 422
     ``marketplace_catalog_unreadable``, with every plugin row and every
     install left exactly as it was; a repository that is merely broken still
     syncs.

Notes
-----
  The manifest these rows turn into (git coordinates, ``plugin_type``, the
  Codex ``git-subdir`` → ``subdir`` round-trip) is covered in
  ``agents_resilient_plugins_test.py``; what happens to an install when its
  entry disappears upstream is covered in ``agents_addons_projection_test.py``.
  The container half of a ``skills``-format install — where the fetched tree
  lands and the manifest synthesised for it — is
  ``tests/unit/test_plugin_manifest_normaliser.py``.
"""
import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.llm_plugin import (
    create_marketplace,
    discover_plugins,
    force_marketplace_type,
    get_marketplace,
    install_agent_plugin,
    list_agent_plugins,
    plugins_by_name,
    sync_marketplace,
    update_marketplace,
)
from tests.utils.skill_catalog import make_agent_with_env, make_developer

API = settings.API_V1_STR


# ── Fixture repositories ───────────────────────────────────────────────────


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_skill(folder: Path, name: str, *, description: str = "") -> None:
    """A minimal valid ``SKILL.md`` in ``folder``."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description or f'Does {name} things.'}\n"
        f"---\n\n# {name}\n\nStep one.\n",
        encoding="utf-8",
    )


def _codex_repo(root: Path) -> None:
    """A Codex marketplace holding one entry of every source kind."""
    _write_json(
        root / ".agents" / "plugins" / "marketplace.json",
        {
            "name": "codex-tools",
            "interface": {"displayName": "Codex Tools"},
            "author": {"name": "Codex Team", "email": "team@example.com"},
            "plugins": [
                {
                    "name": "local-plugin",
                    "category": "tools",
                    "description": "catalog blurb",
                    "policy": {"network": "deny"},
                    "source": {"source": "local", "path": "./plugins/local-plugin"},
                },
                {
                    "name": "url-plugin",
                    "source": {
                        "source": "url",
                        "url": "https://github.com/example/url-plugin.git",
                        "ref": "release",
                    },
                },
                {
                    "name": "subdir-plugin",
                    "source": {
                        "source": "git-subdir",
                        "url": "https://github.com/example/monorepo.git",
                        "path": "packages/reporter",
                        "sha": "9f8e7d6c5b4a3928",
                    },
                },
                {
                    "name": "npm-plugin",
                    "source": {"source": "npm", "package": "@scope/thing"},
                },
                {
                    "name": "mystery-plugin",
                    "source": {"source": "ftp", "url": "ftp://example.com/x"},
                },
                {
                    "name": "apps-only-plugin",
                    "source": {"source": "local", "path": "./plugins/apps-only"},
                },
                {
                    "name": "escape-plugin",
                    "source": {"source": "local", "path": "../../../etc"},
                },
            ],
        },
    )

    local = root / "plugins" / "local-plugin"
    _write_json(
        local / ".codex-plugin" / "plugin.json",
        {
            "name": "local-plugin",
            "version": "3.1",
            "description": "Writes reports from a local plugin.",
            "homepage": "https://example.com/local-plugin",
            "author": {"name": "Ada", "email": "ada@example.com"},
            "interface": {"commands": ["report"]},
            "skills": "./skills",
            "mcpServers": {"reporter": {"command": "node", "args": ["server.js"]}},
        },
    )
    _write_skill(local / "skills" / "alpha", "alpha")
    _write_skill(local / "skills" / "beta", "beta")

    _write_json(
        root / "plugins" / "apps-only" / ".codex-plugin" / "plugin.json",
        {"name": "apps-only-plugin", "version": "9.9", "apps": [{"name": "slack"}]},
    )


def _nested_skills_repo(root: Path) -> None:
    """The conventional ``skills/<name>/`` collection, one folder broken."""
    _write_skill(
        root / "skills" / "report-writer",
        "report-writer",
        description="Writes weekly reports.",
    )
    scripts = root / "skills" / "report-writer" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    # Listed, not installable: a folder under skills/ is meant to be a skill,
    # so one without a SKILL.md is a broken skill worth reporting.
    (root / "skills" / "half-done").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "half-done" / "README.md").write_text("todo", encoding="utf-8")


def _flat_skills_repo(root: Path) -> None:
    """A repo that is nothing but skills, plus a folder that is not one."""
    _write_skill(root / "data-cleanup", "data-cleanup", description="Cleans data.")
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "index.md").write_text("# docs", encoding="utf-8")


def _claude_repo(*names: str):
    """A ``.claude-plugin/marketplace.json`` listing exactly ``names``."""

    def populate(root: Path) -> None:
        _write_json(
            root / ".claude-plugin" / "marketplace.json",
            {
                "name": "shared-tools",
                "plugins": [
                    {
                        "name": name,
                        "description": f"The {name} plugin.",
                        "version": "1.0",
                        "source": f"./plugins/{name}",
                    }
                    for name in names
                ],
            },
        )

    return populate


def _repo_without_a_catalog(root: Path) -> None:
    """A checkout of the same repository with the catalog file not there.

    What an upstream rename, a branch that never had the directory, or a
    half-finished restructure looks like from here: a real clone, real files,
    no catalog.
    """
    (root / "README.md").write_text("# moved\n", encoding="utf-8")
    (root / "docs").mkdir(parents=True, exist_ok=True)


def _skills_repo_all_broken(root: Path) -> None:
    """``skills/`` is there and every folder in it is missing its SKILL.md."""
    for name in ("half-done", "also-half-done"):
        (root / "skills" / name).mkdir(parents=True, exist_ok=True)
        (root / "skills" / name / "README.md").write_text("todo", encoding="utf-8")


def _mixed_codex_repo_named(marketplace_name: str):
    """``_mixed_codex_repo`` under another name — the column is unique."""

    def populate(root: Path) -> None:
        _mixed_codex_repo(root)
        catalog = root / ".agents" / "plugins" / "marketplace.json"
        payload = json.loads(catalog.read_text(encoding="utf-8"))
        payload["name"] = marketplace_name
        _write_json(catalog, payload)

    return populate


def _mixed_codex_repo(root: Path) -> None:
    """One installable Codex entry and one published to npm."""
    _write_json(
        root / ".agents" / "plugins" / "marketplace.json",
        {
            "name": "mixed-codex",
            "plugins": [
                {
                    "name": "installable",
                    "source": {"source": "local", "path": "./plugins/installable"},
                },
                {
                    "name": "from-npm",
                    "source": {"source": "npm", "package": "@scope/from-npm"},
                },
            ],
        },
    )
    _write_json(
        root / "plugins" / "installable" / ".codex-plugin" / "plugin.json",
        {"name": "installable", "version": "1.4", "mcpServers": {"x": {}}},
    )


# ── Scenario 1: the Codex format ───────────────────────────────────────────


def test_a_codex_marketplace_maps_every_source_kind_to_its_own_verdict(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    One sync of a Codex repository, seven entries, five outcomes:
      1. Register a ``codex`` marketplace and sync it against a fixture repo.
      2. The repository names itself: metadata comes off the catalog file.
      3. ``local`` → the marketplace repo's subdirectory, enriched from
         ``.codex-plugin/plugin.json`` (description, version, author,
         homepage), with ``interface`` copied into config and the skill folders
         it ships listed there.
      4. ``url`` → the external repo at its ref; ``git-subdir`` → the same,
         carrying the path inside it.
      5. ``npm`` → ``npm_source``; an unrecognised kind → ``unknown_source``;
         an apps-only manifest → ``app_connector_only``; a path escaping the
         repository → ``unsafe_path``, and its six siblings sync anyway.
      6. Discover orders installable rows first.
    """
    # ── Phase 1: register + sync ─────────────────────────────────────────
    marketplace = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/codex-tools.git",
        marketplace_type="codex",
    )
    assert marketplace["type"] == "codex"

    synced = sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_codex_repo,
        commit_hash="cafebabe0001",
    )

    # ── Phase 2: metadata off the catalog file ───────────────────────────
    assert synced["status"] == "connected", synced
    assert synced["name"] == "codex-tools"
    assert synced["owner_name"] == "Codex Team"
    assert "7" in (synced["status_message"] or ""), synced["status_message"]

    payload = discover_plugins(client, superuser_token_headers, plugin_type="codex")
    rows = plugins_by_name(payload)
    assert payload["count"] == 7, sorted(rows)
    assert all(row["plugin_type"] == "codex" for row in rows.values())

    # ── Phase 3: the local entry, enriched from its own manifest ─────────
    local = rows["local-plugin"]
    assert local["supported"] is True
    assert local["unsupported_reason"] is None
    assert local["source_type"] == "local"
    assert local["source_path"] == "plugins/local-plugin", (
        "the leading './' is stripped; the rest is the clone-relative subdir"
    )
    assert local["source_url"] is None
    assert local["description"] == "Writes reports from a local plugin.", (
        "the plugin's own manifest describes it; the catalog entry only names it"
    )
    assert local["version"] == "3.1"
    assert local["author_name"] == "Ada"
    assert local["author_email"] == "ada@example.com"
    assert local["homepage"] == "https://example.com/local-plugin"
    assert local["commit_hash"] == "cafebabe0001"
    assert local["config"]["interface"] == {"commands": ["report"]}
    assert local["config"]["policy"] == {"network": "deny"}, (
        "policy is stored verbatim and never evaluated"
    )
    assert local["config"]["skills"] == ["alpha", "beta"], (
        "the clone is thrown away, so the skill folders are listed at sync time"
    )
    assert local["skill_summary"] is None, (
        "a Codex plugin ships a list of skills, not the single-skill summary a "
        "skills-format entry carries"
    )

    # ── Phase 4: the two external-repo kinds ─────────────────────────────
    url_row = rows["url-plugin"]
    assert url_row["supported"] is True
    assert url_row["source_type"] == "url"
    assert url_row["source_url"] == "https://github.com/example/url-plugin.git"
    assert url_row["source_branch"] == "release"
    assert url_row["source_path"] == "", "the external repo's root IS the plugin"

    subdir_row = rows["subdir-plugin"]
    assert subdir_row["supported"] is True
    assert subdir_row["source_type"] == "url"
    assert subdir_row["source_url"] == "https://github.com/example/monorepo.git"
    assert subdir_row["source_path"] == "packages/reporter", (
        "a git-subdir entry's whole point is the path inside the external repo"
    )
    assert subdir_row["source_commit_hash"] == "9f8e7d6c5b4a3928"

    # ── Phase 5: the four refusals, each listed with its reason ──────────
    assert (rows["npm-plugin"]["supported"], rows["npm-plugin"]["unsupported_reason"]) == (
        False,
        "npm_source",
    )
    assert rows["mystery-plugin"]["unsupported_reason"] == "unknown_source"
    assert rows["apps-only-plugin"]["unsupported_reason"] == "app_connector_only", (
        "a manifest declaring only apps is an app connector, not an agent plugin"
    )
    escape = rows["escape-plugin"]
    assert escape["supported"] is False
    assert escape["unsupported_reason"] == "unsafe_path"
    assert escape["source_path"] == "", (
        "the escaping path is dropped, not stored, so nothing downstream can "
        "resolve it"
    )

    # ── Phase 6: installable first, then alphabetical ────────────────────
    assert [row["name"] for row in payload["data"]] == [
        "local-plugin",
        "subdir-plugin",
        "url-plugin",
        "apps-only-plugin",
        "escape-plugin",
        "mystery-plugin",
        "npm-plugin",
    ], "an unsupported entry must never push an installable one off the page"


# ── Scenario 2: the bare-skills format ─────────────────────────────────────


def test_a_skills_repository_lists_one_row_per_skill_in_either_layout(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A repository that is a collection of skills has no catalog file at all:
      1. The conventional ``skills/<name>/`` layout → one row per folder.
      2. A folder under ``skills/`` with no valid ``SKILL.md`` is still listed,
         as ``no_skill_md``, so a half-broken repo is visible to the admin.
      3. The valid row carries its skill summary (description + has_scripts)
         and the ``skill`` category.
      4. The marketplace keeps the name it was registered under — the repo has
         nothing authoritative to rename itself with, and that name is the
         on-disk directory every install resolves through.
      5. The flat ``<name>/SKILL.md`` layout works too, and a top-level folder
         that is not a skill (``docs/``) is not listed there.
    """
    # ── Phase 1: the nested layout ───────────────────────────────────────
    nested = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/skill-collection.git",
        marketplace_type="skills",
    )
    registered_name = nested["name"]

    synced = sync_marketplace(
        client,
        superuser_token_headers,
        nested["id"],
        populate=_nested_skills_repo,
        commit_hash="beadfeed0002",
    )
    assert synced["status"] == "connected", synced

    rows = plugins_by_name(
        discover_plugins(client, superuser_token_headers, plugin_type="skills")
    )
    assert set(rows) == {"report-writer", "half-done"}

    # ── Phase 2 + 3: the good row and the broken one ─────────────────────
    good = rows["report-writer"]
    assert good["supported"] is True
    assert good["unsupported_reason"] is None
    assert good["plugin_type"] == "skills"
    assert good["category"] == "skill"
    assert good["source_type"] == "local"
    assert good["source_path"] == "skills/report-writer"
    assert good["description"] == "Writes weekly reports."
    assert good["skill_summary"] == {
        "name": "report-writer",
        "description": "Writes weekly reports.",
        "has_scripts": True,
    }

    broken = rows["half-done"]
    assert broken["supported"] is False
    assert broken["unsupported_reason"] == "no_skill_md", (
        "listed rather than dropped: an upstream fix re-syncs it into a "
        "supported row with no action here"
    )

    # ── Phase 4: the repo does not get to rename the marketplace ─────────
    assert get_marketplace(client, superuser_token_headers, nested["id"])["name"] == (
        registered_name
    ), "renaming the marketplace would move every install's plugin directory"

    # ── Phase 5: the flat layout ─────────────────────────────────────────
    flat = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/flat-skills.git",
        marketplace_type="skills",
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        flat["id"],
        populate=_flat_skills_repo,
        commit_hash="beadfeed0003",
    )

    flat_rows = plugins_by_name(
        discover_plugins(
            client, superuser_token_headers, plugin_type="skills", search="data-cleanup"
        )
    )
    assert set(flat_rows) == {"data-cleanup"}
    assert flat_rows["data-cleanup"]["source_path"] == "data-cleanup", (
        "no skills/ prefix in the flat layout — the folder is at the repo root"
    )
    assert flat_rows["data-cleanup"]["supported"] is True

    all_skills = plugins_by_name(
        discover_plugins(client, superuser_token_headers, plugin_type="skills")
    )
    assert "docs" not in all_skills, (
        "in the flat layout a top-level directory only counts when it actually "
        "holds a SKILL.md"
    )


# ── Scenario 3: an unknown format is never parsed as Claude ────────────────


def test_an_unknown_marketplace_format_is_refused_at_create_and_at_sync(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    ``type`` is a closed set, and a repository nobody can read is said so:
      1. Creating a marketplace with an unknown ``type`` is a 422.
      2. Updating an existing one to an unknown ``type`` is a 422; the row
         keeps the format it had.
      3. A legacy row that already holds a bad ``type`` (written before the
         validation existed) syncs to ``status=error`` and answers 422 with a
         coded detail — never a silent fall back to the Claude parser, which
         would report an empty catalog for a repository nobody parsed.
      4. Pointing it at a format we know recovers it: the next sync connects.
    """
    # ── Phase 1: create ──────────────────────────────────────────────────
    create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/unknown.git",
        marketplace_type="anthropic-classic",
        expected_status=422,
    )

    marketplace = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/legacy.git",
        marketplace_type="codex",
    )

    # ── Phase 2: update ──────────────────────────────────────────────────
    update_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        {"type": "anthropic-classic"},
        expected_status=422,
    )
    assert get_marketplace(client, superuser_token_headers, marketplace["id"])[
        "type"
    ] == "codex"

    # ── Phase 3: the legacy row ──────────────────────────────────────────
    force_marketplace_type(db, marketplace["id"], "legacy-format")

    r = sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_codex_repo,
        expected_status=422,
    )
    assert r["detail"]["code"] == "unsupported_marketplace_type"
    assert "legacy-format" in r["detail"]["message"]

    errored = get_marketplace(client, superuser_token_headers, marketplace["id"])
    assert errored["status"] == "error"
    assert "legacy-format" in (errored["status_message"] or ""), (
        "the row has to say which format it declares, not just that it failed"
    )
    assert discover_plugins(client, superuser_token_headers)["count"] == 0, (
        "nothing was parsed, so nothing was listed"
    )

    # ── Phase 4: recovery ────────────────────────────────────────────────
    update_marketplace(
        client, superuser_token_headers, marketplace["id"], {"type": "codex"}
    )
    recovered = sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_codex_repo,
    )
    assert recovered["status"] == "connected"
    assert discover_plugins(client, superuser_token_headers)["count"] == 7


# ── Scenario 4: an unsupported entry is visible but not installable ────────


def test_installing_an_unsupported_entry_is_refused_with_its_reason(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Discovery shows the entries this platform cannot install; the install
    route is what refuses them:
      1. A Codex marketplace with one installable entry and one npm entry, and
         a skills marketplace beside it.
      2. Installing the npm entry → 409 ``plugin_unsupported`` carrying the
         reason code and a sentence; no link is created.
      3. The installable sibling installs, and the link freezes both snapshot
         names — the directory identity that outlives the plugin row.
      4. The ``plugin_type`` filter narrows discovery to one format.
    """
    # ── Phase 1: two marketplaces, three entries ─────────────────────────
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Codex-Consumer")

    codex = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/mixed-codex.git",
        marketplace_type="codex",
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        codex["id"],
        populate=_mixed_codex_repo,
        commit_hash="c0ffee000004",
    )

    skills = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/flat-skills.git",
        marketplace_type="skills",
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        skills["id"],
        populate=_flat_skills_repo,
        commit_hash="c0ffee000005",
    )

    rows = plugins_by_name(discover_plugins(client, headers))
    assert set(rows) == {"installable", "from-npm", "data-cleanup"}

    # ── Phase 2: the refusal ─────────────────────────────────────────────
    refused = install_agent_plugin(
        client, headers, agent_id, rows["from-npm"]["id"], expected_status=409
    )
    detail = refused["detail"]
    assert detail["code"] == "plugin_unsupported"
    assert detail["reason"] == "npm_source"
    assert "npm" in detail["message"].lower(), detail["message"]
    assert list_agent_plugins(client, headers, agent_id) == [], (
        "the refusal lands before the link exists — a row the container would "
        "fail on at every sync must never be written"
    )

    # ── Phase 3: the installable sibling, with its snapshot identity ─────
    install_agent_plugin(client, headers, agent_id, rows["installable"]["id"])
    links = list_agent_plugins(client, headers, agent_id)
    assert len(links) == 1
    link = links[0]
    assert link["source"] == "marketplace"
    assert link["snapshot_marketplace_name"] == "mixed-codex"
    assert link["snapshot_plugin_name"] == "installable", (
        "<marketplace>/<plugin> is the directory identity; frozen at install "
        "so the row survives the loss of the live plugin row"
    )
    assert link["installed_version"] == "1.4"
    drain_tasks()

    # ── Phase 4: the format filter ───────────────────────────────────────
    codex_only = plugins_by_name(
        discover_plugins(client, headers, plugin_type="codex")
    )
    assert set(codex_only) == {"installable", "from-npm"}
    skills_only = plugins_by_name(
        discover_plugins(client, headers, plugin_type="skills")
    )
    assert set(skills_only) == {"data-cleanup"}
    assert discover_plugins(client, headers, plugin_type="claude")["count"] == 0

    # ── Phase 4b: the marketplace filter ─────────────────────────────────
    # "Which of *this* marketplace's entries can the platform install?" is a
    # question about one repository. Answering it out of the server-wide list
    # means paging through every other marketplace to be sure of the tail, so
    # the scope is a filter, not a page size.
    scoped = discover_plugins(client, headers, marketplace_id=codex["id"])
    assert set(plugins_by_name(scoped)) == {"installable", "from-npm"}
    assert scoped["count"] == 2, "the count is the scoped total, not the global one"
    assert set(
        plugins_by_name(discover_plugins(client, headers, marketplace_id=skills["id"]))
    ) == {"data-cleanup"}

    # It composes with the other filters rather than replacing them.
    assert (
        discover_plugins(
            client, headers, marketplace_id=codex["id"], plugin_type="skills"
        )["count"]
        == 0
    )
    assert set(
        plugins_by_name(
            discover_plugins(
                client, headers, marketplace_id=codex["id"], search="installable"
            )
        )
    ) == {"installable"}

    # An id nobody can see is an empty page, never an error and never a
    # different one: discovery does not report on the existence of a
    # marketplace the caller has no access to.
    assert discover_plugins(client, headers, marketplace_id=str(uuid.uuid4()))[
        "count"
    ] == 0

    private = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/private-codex.git",
        marketplace_type="codex",
        public_discovery=False,
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        private["id"],
        populate=_mixed_codex_repo_named("private-codex"),
        commit_hash="c0ffee000006",
    )
    assert discover_plugins(client, headers, marketplace_id=private["id"])[
        "count"
    ] == 0, "a private marketplace answers the same way a missing one does"
    assert (
        discover_plugins(
            client, superuser_token_headers, marketplace_id=private["id"]
        )["count"]
        == 2
    ), "its owner sees it scoped like any other"

    # ── Phase 5: an agent that is not the caller's stays closed ──────────
    other_agent = create_agent_via_api(
        client, superuser_token_headers, name="Somebody-Elses-Agent"
    )
    drain_tasks()
    r = client.get(
        f"{API}/llm-plugins/agents/{other_agent['id']}/plugins", headers=headers
    )
    assert r.status_code in (403, 404), r.text


# ── Scenario 5: a catalog that cannot be read deletes nothing ──────────────


def test_a_catalog_file_gone_for_one_sync_deletes_nothing(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A missing catalog file is corrupt state, not an empty catalog.

    The distinction is the difference between a transient upstream mistake and
    a permanent one. Parsing "no marketplace.json" as zero plugins sends the
    upsert down its "remove plugins no longer in marketplace" pass and deletes
    every row; each install of them then survives with ``plugin_id`` NULL (that
    is the point of the ``SET NULL``), the environment prunes the directories,
    and the row is ``source_unavailable`` until somebody uninstalls it by hand.
    One renamed directory upstream, for one sync, and every install on the
    server is broken.

      1. A marketplace with two entries; one of them installed on an agent.
      2. The next sync clones a tree with no ``.claude-plugin/`` in it → 422
         ``marketplace_catalog_unreadable``, the row records the error, and
         **both** plugin rows are still there.
      3. The install still points at its plugin row — nothing was orphaned.
      4. The upstream fix re-syncs cleanly; the same two rows are updated.
    """
    # ── Phase 1: a healthy marketplace, one entry installed ──────────────
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Catalog-Gone")

    marketplace = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/shared-tools.git",
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo("reporting", "charting"),
        commit_hash="aaaa11112222",
    )
    rows = plugins_by_name(discover_plugins(client, headers))
    assert set(rows) == {"reporting", "charting"}

    install_agent_plugin(client, headers, agent_id, rows["reporting"]["id"])
    drain_tasks()
    installed = list_agent_plugins(client, headers, agent_id)[0]
    assert installed["plugin_id"] == rows["reporting"]["id"]

    # ── Phase 2: the catalog file is not in this checkout ────────────────
    refused = sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_repo_without_a_catalog,
        commit_hash="bbbb33334444",
        expected_status=422,
    )
    assert refused["detail"]["code"] == "marketplace_catalog_unreadable"
    assert ".claude-plugin/marketplace.json" in refused["detail"]["message"]

    errored = get_marketplace(client, superuser_token_headers, marketplace["id"])
    assert errored["status"] == "error"
    assert "marketplace.json" in (errored["status_message"] or ""), (
        "the row has to name what could not be read, or an administrator has "
        "nothing to go and fix"
    )

    after = plugins_by_name(discover_plugins(client, headers))
    assert set(after) == {"reporting", "charting"}, (
        "a sync that read nothing must delete nothing — an unreadable "
        "repository says nothing about which entries the marketplace has"
    )

    # ── Phase 3: the install is untouched ────────────────────────────────
    link = list_agent_plugins(client, headers, agent_id)[0]
    assert link["id"] == installed["id"]
    assert link["plugin_id"] == rows["reporting"]["id"], (
        "orphaning an install on a sync that failed to read anything is the "
        "unrecoverable half: the files are pruned and nothing re-attaches it"
    )

    # ── Phase 4: the upstream fix ────────────────────────────────────────
    recovered = sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_claude_repo("reporting", "charting"),
        commit_hash="cccc55556666",
    )
    assert recovered["status"] == "connected"
    healed = plugins_by_name(discover_plugins(client, headers))
    assert {name: row["id"] for name, row in healed.items()} == {
        name: row["id"] for name, row in rows.items()
    }, "the same rows were updated, not deleted and recreated"


def test_a_skills_repository_that_lost_its_skills_is_an_error_not_an_empty_catalog(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The ``skills`` format has no catalog file — the directory layout *is* the
    catalog — so "no skill folder anywhere" is the same finding a missing
    ``marketplace.json`` is, and gets the same refusal.

    The line it must not cross is the repository that is merely **broken**,
    which stays a successful sync: a folder under ``skills/`` whose SKILL.md
    does not validate is listed as ``no_skill_md``. Zero *supported* rows is a
    connected marketplace; zero rows at all is a repository nobody read.

      1. A skills repository syncs; its rows exist.
      2. A checkout with the ``skills/`` tree gone → 422, both rows kept.
      3. A checkout where ``skills/`` is there and every folder in it is
         broken → ``connected``, two ``no_skill_md`` rows, nothing installable.
    """
    # ── Phase 1: the healthy repository ──────────────────────────────────
    _user, headers = make_developer(client, superuser_token_headers)
    marketplace = create_marketplace(
        client,
        superuser_token_headers,
        url="https://github.com/example/skill-collection.git",
        marketplace_type="skills",
    )
    sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_nested_skills_repo,
        commit_hash="dddd77778888",
    )
    assert set(plugins_by_name(discover_plugins(client, headers))) == {
        "report-writer",
        "half-done",
    }

    # ── Phase 2: the skills are not in this checkout ─────────────────────
    refused = sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_repo_without_a_catalog,
        commit_hash="eeee99990000",
        expected_status=422,
    )
    assert refused["detail"]["code"] == "marketplace_catalog_unreadable"
    assert set(plugins_by_name(discover_plugins(client, headers))) == {
        "report-writer",
        "half-done",
    }, "nothing was read, so nothing is known to have gone"

    # ── Phase 3: broken is not the same as absent ────────────────────────
    connected = sync_marketplace(
        client,
        superuser_token_headers,
        marketplace["id"],
        populate=_skills_repo_all_broken,
        commit_hash="ffff11112222",
    )
    assert connected["status"] == "connected"
    broken = plugins_by_name(discover_plugins(client, headers))
    assert set(broken) == {"half-done", "also-half-done"}
    assert [row["unsupported_reason"] for row in broken.values()] == [
        "no_skill_md",
        "no_skill_md",
    ]
    assert not any(row["supported"] for row in broken.values()), (
        "a half-broken repository is visibly half-broken, not silently empty"
    )
