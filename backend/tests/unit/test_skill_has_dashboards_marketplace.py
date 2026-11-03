"""``has_dashboards`` through the skills-format marketplace parse and summary."""

from pathlib import Path
from types import SimpleNamespace

from app.services.plugins.llm_plugin_service import LLMPluginService


def _skill(root: Path, name: str, dashboards: bool) -> None:
    folder = root / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Does {name}.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    if dashboards:
        (folder / "dashboards" / "main").mkdir(parents=True)
        (folder / "dashboards" / "main" / "component.json").write_text("{}")


def test_skills_marketplace_parse_sets_has_dashboards(tmp_path: Path) -> None:
    _skill(tmp_path, "with-dash", True)
    _skill(tmp_path, "plain", False)
    result = LLMPluginService._parse_skills_marketplace(str(tmp_path))
    blocks = {p["name"]: p["config"]["skill"] for p in result["plugins"]}
    assert blocks["with-dash"]["has_dashboards"] is True
    assert blocks["plain"]["has_dashboards"] is False


def test_skill_summary_reads_has_dashboards() -> None:
    plugin = SimpleNamespace(
        config={"skill": {"name": "s", "description": "d", "has_dashboards": True}}
    )
    summary = LLMPluginService._skill_summary(plugin)
    assert summary.has_dashboards is True


def test_skill_summary_of_older_config_defaults_to_false() -> None:
    plugin = SimpleNamespace(
        config={"skill": {"name": "s", "description": "d", "has_scripts": True}}
    )
    summary = LLMPluginService._skill_summary(plugin)
    assert summary.has_scripts is True
    assert summary.has_dashboards is False
