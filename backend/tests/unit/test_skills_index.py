"""Unit tests: env-core's skill index builder.

``AgentEnvService.build_skills_index`` is what ``GET /config/skills`` returns
and therefore what the whole platform-side feature is built on — the agent card,
the ``/skills`` command, the slash-command popup and the bundle catalog line all
read a projection of it.

It is also where the merging happens: the workspace's own ``skills/`` plus every
active plugin's, one shared per-agent budget over the union, ``shadowed`` marking
across sources, and the ``projection_error`` overlay from the projector's latched
state. None of that is visible from either side alone, so it is pinned here.

Imported through the ``app_core_base`` path that ``unit/conftest.py`` puts on
``sys.path``, the same way the projection tests reach ``skills_projection``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.server.agent_env_service import AgentEnvService


def _write_skill(root: Path, name: str, *, description: str = "Does things.") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "skills").mkdir()
    return tmp_path


def _activate_plugin(workspace: Path, marketplace: str, plugin: str) -> Path:
    """Register a plugin in ``plugins/settings.json`` and return its skills dir."""
    plugin_dir = workspace / "plugins" / marketplace / plugin
    skills_dir = plugin_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    settings_file = workspace / "plugins" / "settings.json"
    settings = (
        json.loads(settings_file.read_text()) if settings_file.exists() else {}
    )
    active = settings.setdefault("active_plugins", [])
    active.append(
        {
            "marketplace_name": marketplace,
            "plugin_name": plugin,
            "path": str(plugin_dir),
            "conversation_mode": True,
            "building_mode": False,
        }
    )
    settings_file.write_text(json.dumps(settings), encoding="utf-8")
    return skills_dir


def _index(workspace: Path) -> dict:
    return AgentEnvService(str(workspace)).build_skills_index()


class TestSources:

    def test_an_agent_with_no_skills_folder_reports_an_empty_index(self, tmp_path: Path):
        index = _index(tmp_path)

        assert index["skills"] == []
        assert index["errors"] == []
        assert index["hash"]  # a stable digest, not an error

    def test_local_skills_are_reported(self, workspace: Path):
        _write_skill(workspace / "skills", "pdf-report")

        entries = _index(workspace)["skills"]

        assert [e["name"] for e in entries] == ["pdf-report"]
        assert entries[0]["source"] == "local"
        assert entries[0]["path"] == "skills/pdf-report"
        assert entries[0]["plugin_ref"] is None

    def test_a_plugin_skill_carries_its_real_workspace_path(self, workspace: Path):
        # The path is what the backend reads the SKILL.md back through, so a
        # plugin skill must NOT report "skills/<name>".
        _write_skill(_activate_plugin(workspace, "mkt", "p"), "helper")

        entry = _index(workspace)["skills"][0]

        assert entry["source"] == "plugin"
        assert entry["plugin_ref"] == "mkt/p"
        assert entry["path"] == "plugins/mkt/p/skills/helper"

    def test_a_plugin_listed_twice_contributes_its_skills_once(self, workspace: Path):
        # settings.json carries one row per mode-enabled plugin.
        skills_dir = _activate_plugin(workspace, "mkt", "p")
        _activate_plugin(workspace, "mkt", "p")
        _write_skill(skills_dir, "helper")

        assert [e["name"] for e in _index(workspace)["skills"]] == ["helper"]

    def test_the_hash_covers_the_workspace_tree_only(self, workspace: Path):
        _write_skill(workspace / "skills", "local-one")
        before = _index(workspace)["hash"]

        _write_skill(_activate_plugin(workspace, "mkt", "p"), "helper")
        after = _index(workspace)

        # Plugin skills are in the list but must not move the tree hash — the
        # hash is the workspace's, and a plugin toggle is not a workspace edit.
        assert after["hash"] == before
        assert len(after["skills"]) == 2

    def test_a_plain_file_at_the_skills_root_is_not_a_row(self, workspace: Path):
        (workspace / "skills" / "README.md").write_text("scaffold\n", encoding="utf-8")
        _write_skill(workspace / "skills", "real")

        assert [e["name"] for e in _index(workspace)["skills"]] == ["real"]


class TestShadowing:

    def test_a_name_in_two_sources_flags_both_rows(self, workspace: Path):
        _write_skill(workspace / "skills", "helper", description="Mine.")
        _write_skill(_activate_plugin(workspace, "mkt", "p"), "helper", description="Theirs.")

        entries = _index(workspace)["skills"]

        assert [e["warning"]["code"] for e in entries] == ["shadowed", "shadowed"]

    def test_the_local_copy_sorts_first(self, workspace: Path):
        # The content route resolves a shadowed name to the first entry, and the
        # agent's own copy is the one its owner came to read.
        _write_skill(workspace / "skills", "helper")
        _write_skill(_activate_plugin(workspace, "mkt", "p"), "helper")

        assert _index(workspace)["skills"][0]["source"] == "local"

    def test_a_unique_name_is_not_flagged(self, workspace: Path):
        _write_skill(workspace / "skills", "mine")
        _write_skill(_activate_plugin(workspace, "mkt", "p"), "theirs")

        assert all(e["warning"] is None for e in _index(workspace)["skills"])


class TestPerAgentBudget:

    def test_the_cap_is_shared_across_roots(self, workspace: Path):
        # 30 + 30 with a 50-skill cap: a per-root budget would admit all 60.
        for index in range(30):
            _write_skill(workspace / "skills", f"local-{index:02d}")
        plugin_skills = _activate_plugin(workspace, "mkt", "p")
        for index in range(30):
            _write_skill(plugin_skills, f"plug-{index:02d}")

        entries = _index(workspace)["skills"]

        accepted = [e for e in entries if e["error"] is None]
        excluded = [e for e in entries if e["error"] and e["error"]["code"] == "budget"]
        assert len(entries) == 60
        assert len(accepted) == 50
        assert len(excluded) == 10
        # Deterministic by name order, so which skills survive is stable.
        assert {e["name"] for e in excluded} == {
            f"plug-{index:02d}" for index in range(20, 30)
        }


class TestProjectionErrors:

    def test_a_skill_the_projector_could_not_copy_is_reported(
        self, workspace: Path, tmp_path: Path, monkeypatch
    ):
        _write_skill(workspace / "skills", "unlucky")
        _write_skill(workspace / "skills", "fine")

        from core.server import skills_projection

        monkeypatch.setattr(
            skills_projection, "projection_failures", lambda *_a, **_k: {"unlucky"}
        )

        entries = {e["name"]: e for e in _index(workspace)["skills"]}

        # Valid on disk, invisible to the engine — the card must say so.
        assert entries["unlucky"]["error"]["code"] == "projection_error"
        assert entries["fine"]["error"] is None

    def test_a_plugin_skill_is_never_marked_projection_error(
        self, workspace: Path, monkeypatch
    ):
        # Plugin skills are loaded from the plugin path and never travel through
        # the projection, so the projector's failure list cannot be about them.
        _write_skill(_activate_plugin(workspace, "mkt", "p"), "helper")

        from core.server import skills_projection

        monkeypatch.setattr(
            skills_projection, "projection_failures", lambda *_a, **_k: {"helper"}
        )

        assert _index(workspace)["skills"][0]["error"] is None
