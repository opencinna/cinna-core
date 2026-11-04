"""
Env-core ``GET /prompt/building``: bundle_id reference line and platform skills.

Calls the route function directly against a temp workspace and a temp platform
skills directory (no Docker, no database).

Run:
    docker compose exec backend python -m pytest \
        tests/unit/test_building_prompt_route.py -v
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from core.server import prompt_generator as pg_module
from core.server import routes
from core.server.prompt_generator import PromptGenerator
from core.server.skill_manifest import PLATFORM_SKILL_NAMES

_BACKEND = Path(__file__).resolve().parents[2]
_KIT_SKILLS = (
    _BACKEND / "app" / "env-templates" / "platform-knowledge-env" / "app"
    / "workspace" / "knowledge" / "local-kit" / "skills"
)
BUNDLE_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def building_base(monkeypatch) -> None:
    # BUILDING_AGENT.md lives in /app/core/prompts, absent outside the env image.
    monkeypatch.setattr(
        PromptGenerator, "_load_building_agent_prompt", lambda self: "BUILDING BASE"
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    for sub in ("scripts", "skills", "docs", "credentials"):
        (ws / sub).mkdir(parents=True)
    return ws


@pytest.fixture
def platform_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "core_skills"
    d.mkdir()
    monkeypatch.setattr(pg_module, "DEFAULT_PLATFORM_SKILLS_DIR", str(d))
    return d


def _install(platform_dir: Path, *names: str) -> None:
    for name in names:
        shutil.copytree(_KIT_SKILLS / name, platform_dir / name)


def _call(workspace: Path, **kwargs) -> dict:
    routes.WORKSPACE_DIR, original = str(workspace), routes.WORKSPACE_DIR
    try:
        return asyncio.run(routes.get_building_prompt(**kwargs))
    finally:
        routes.WORKSPACE_DIR = original


def _dashboards_section(prompt: str) -> str:
    assert "## Dashboards" in prompt
    return prompt.split("## Dashboards", 1)[1]


def test_bundle_id_renders_component_reference_line(workspace, platform_dir) -> None:
    _install(platform_dir, "cinna-dashboards")
    result = _call(workspace, bundle_id=BUNDLE_ID)
    section = _dashboards_section(result["building_prompt"])
    assert f"cinna://component/bundle:{BUNDLE_ID}/" in section


def test_without_bundle_id_route_answers_with_plain_words_line(workspace, platform_dir) -> None:
    _install(platform_dir, "cinna-dashboards")
    result = _call(workspace)  # bundle_id defaults to None
    section = _dashboards_section(result["building_prompt"])
    assert "cinna://component/bundle:" not in section
    assert "in plain words" in section
    for key in ("building_prompt", "building_prompt_parts", "prompt_files", "settings", "platform_skills"):
        assert key in result


def test_env_without_dashboards_skill_has_no_dashboards_section(workspace, platform_dir) -> None:
    _install(platform_dir, "cinna-tasks")
    result = _call(workspace, bundle_id=BUNDLE_ID)
    assert "## Dashboards" not in result["building_prompt"]
    assert "cinna://component/bundle:" not in result["building_prompt"]
    assert result["platform_skills"] == [
        {"name": "cinna-tasks", "kit_path": "skills/cinna-tasks"}
    ]


def test_platform_skills_lists_every_installed_platform_skill(workspace, platform_dir) -> None:
    _install(platform_dir, *sorted(PLATFORM_SKILL_NAMES))
    result = _call(workspace, bundle_id=BUNDLE_ID)
    skills = result["platform_skills"]
    assert {s["name"] for s in skills} == set(PLATFORM_SKILL_NAMES)
    assert all(s["kit_path"] == f"skills/{s['name']}" for s in skills)
    assert [s["name"] for s in skills] == sorted(s["name"] for s in skills)


def test_platform_skills_ignores_non_platform_and_invalid_entries(workspace, platform_dir) -> None:
    _install(platform_dir, "cinna-tasks")
    (platform_dir / "not-a-platform-skill").mkdir()
    (platform_dir / "not-a-platform-skill" / "SKILL.md").write_text(
        "---\nname: not-a-platform-skill\ndescription: x\n---\nbody\n", encoding="utf-8"
    )
    (platform_dir / "cinna-artifacts").mkdir()  # no SKILL.md -> invalid
    names = [s["name"] for s in PromptGenerator(str(workspace)).platform_skills()]
    assert names == ["cinna-tasks"]


def test_platform_skills_empty_when_directory_missing(workspace, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pg_module, "DEFAULT_PLATFORM_SKILLS_DIR", str(tmp_path / "absent"))
    assert PromptGenerator(str(workspace)).platform_skills() == []
    assert _call(workspace, bundle_id=BUNDLE_ID)["platform_skills"] == []
