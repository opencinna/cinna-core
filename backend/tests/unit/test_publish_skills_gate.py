"""Unit tests: the publish-time skills gate and the derived skills summary.

Both halves read the publisher's LIVE workspace tree — the same bytes the
snapshot is about to copy — so they are pure filesystem functions and testable
without a DB, an env or a bundle.

What is pinned here is the pair's contract: what blocks a publish, what does
*not* block it, and that the summary describes exactly the skills that survived
the gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.bundles.publish_service import PublishService
from app.services.environments.workspace_classification import WORKSPACE_ROOT_REL


def _workspace(tmp_path: Path) -> Path:
    """Build an ``ENV_INSTANCES_DIR/<env id>`` shaped root and return it."""
    (tmp_path / WORKSPACE_ROOT_REL / "skills").mkdir(parents=True)
    return tmp_path


def _skill(
    env_root: Path, name: str, *, frontmatter: str | None = None, body: str = "Body."
) -> Path:
    skill_dir = env_root / WORKSPACE_ROOT_REL / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    front = frontmatter or f"name: {name}\ndescription: Does {name} things."
    (skill_dir / "SKILL.md").write_text(f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")
    return skill_dir


class TestPublishGate:

    def test_no_env_is_allowed(self):
        # A prompts-only revision has no workspace to inspect.
        PublishService._ensure_publisher_skills_publishable(None)

    def test_no_skills_folder_is_allowed(self, tmp_path: Path):
        (tmp_path / WORKSPACE_ROOT_REL).mkdir(parents=True)
        PublishService._ensure_publisher_skills_publishable(tmp_path)

    def test_a_valid_tree_passes(self, tmp_path: Path):
        env_root = _workspace(tmp_path)
        _skill(env_root, "pdf-report")

        PublishService._ensure_publisher_skills_publishable(env_root)

    def test_an_invalid_skill_blocks_and_is_named(self, tmp_path: Path):
        env_root = _workspace(tmp_path)
        _skill(env_root, "on-disk", frontmatter="name: in-frontmatter\ndescription: x")

        with pytest.raises(ValueError) as excinfo:
            PublishService._ensure_publisher_skills_publishable(env_root)

        message = str(excinfo.value)
        assert "on-disk" in message           # names the skill…
        assert "folder name" in message       # …and says what is wrong with it

    def test_a_secret_file_blocks_and_names_the_path(self, tmp_path: Path):
        env_root = _workspace(tmp_path)
        skill_dir = _skill(env_root, "leaky")
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / ".env").write_text("TOKEN=abc\n", encoding="utf-8")

        with pytest.raises(ValueError) as excinfo:
            PublishService._ensure_publisher_skills_publishable(env_root)

        assert "skills/leaky/scripts/.env" in str(excinfo.value)

    def test_an_example_dotenv_does_not_block(self, tmp_path: Path):
        env_root = _workspace(tmp_path)
        skill_dir = _skill(env_root, "documented")
        (skill_dir / ".env.example").write_text("TOKEN=\n", encoding="utf-8")

        PublishService._ensure_publisher_skills_publishable(env_root)

    def test_budget_overflow_does_not_block(self, tmp_path: Path):
        # A 51st skill is excluded from the index, not malformed. Blocking on it
        # would leave the publisher with a refusal and no route to a fix.
        env_root = _workspace(tmp_path)
        for index in range(55):
            _skill(env_root, f"skill-{index:03d}")

        PublishService._ensure_publisher_skills_publishable(env_root)


class TestSkillsSummary:

    def test_no_env_summarises_as_empty_not_missing(self):
        # ``[]`` = "published with no skills"; only a revision that predates the
        # feature carries ``None``, and that comes from the absent manifest key.
        assert PublishService._collect_skills_summary(None) == []

    def test_summary_carries_name_description_and_scripts_only(self, tmp_path: Path):
        env_root = _workspace(tmp_path)
        skill_dir = _skill(env_root, "pdf-report")
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
        _skill(env_root, "plain")

        summary = PublishService._collect_skills_summary(env_root)

        assert summary == [
            {
                "name": "pdf-report",
                "description": "Does pdf-report things.",
                "has_scripts": True,
            },
            {
                "name": "plain",
                "description": "Does plain things.",
                "has_scripts": False,
            },
        ]

    def test_invalid_skills_are_left_out_of_the_summary(self, tmp_path: Path):
        env_root = _workspace(tmp_path)
        _skill(env_root, "good")
        _skill(env_root, "bad", frontmatter="name: other\ndescription: x")

        summary = PublishService._collect_skills_summary(env_root)

        assert [entry["name"] for entry in summary] == ["good"]

    def test_a_plain_file_at_the_skills_root_is_ignored(self, tmp_path: Path):
        # The Local Agent Kit ships ``skills/README.md``.
        env_root = _workspace(tmp_path)
        (env_root / WORKSPACE_ROOT_REL / "skills" / "README.md").write_text(
            "How skills work\n", encoding="utf-8"
        )
        _skill(env_root, "real")

        PublishService._ensure_publisher_skills_publishable(env_root)
        assert [e["name"] for e in PublishService._collect_skills_summary(env_root)] == [
            "real"
        ]
