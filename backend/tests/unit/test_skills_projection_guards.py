"""Unit tests: path-safety guards on the agent-skills projection (plan Phase 1).

``skills_projection.refresh()`` runs before every single message, reads a tree
the agent itself can write, and writes into ``/root/.claude/skills``. The
projector's whole safety story is therefore about what it *refuses*: nothing
outside ``<home>/skills/<name>/`` may ever be written, and nothing it did not
place there may ever be removed.

The happy path, the no-op fast path, prune, marker-guarded foreign dirs,
symlink refusal and the never-raises contract are covered in
``test_skill_manifest.py``. This file covers the guards §13 lists that are not
reachable from those scenarios:

* a ``..`` component can never reach the destination path;
* a plain file at the root of ``skills/`` (``skills/README.md`` ships with the
  Local Agent Kit template) is neither projected nor fatal;
* a write failure is reported and returned, never raised into the message path,
  and never leaves the projection root behind as a side effect.

Run:
    docker compose exec backend python -m pytest \
        tests/unit/test_skills_projection_guards.py -v
"""

from __future__ import annotations

from pathlib import Path

from app.services.agents import skill_manifest

from core.server import skills_projection


def _write_skill(root: Path, name: str, *, frontmatter: str, body: str = "Body.") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8"
    )
    return skill_dir


def _valid(name: str) -> str:
    return f"name: {name}\ndescription: Build a PDF report from a CSV."


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    home = tmp_path / "claude_home"
    home.mkdir()
    return workspace, home


class TestPathComponentsAreRefused:
    """Only a name the skill-name regex already accepted becomes a destination.

    ``_project_one`` builds its destination as ``projected_root / name``, so a
    name carrying a separator or a ``..`` component would resolve *outside* the
    projection root — and the projection root is the only place this module is
    allowed to write. The regex is what stops that, and these are the cases
    that prove the regex is actually load-bearing rather than cosmetic.
    """

    def test_a_dot_dot_name_never_becomes_a_destination(self, tmp_path: Path):
        """A skill claiming ``name: ..`` is excluded, not resolved upward."""
        workspace, home = _workspace(tmp_path)
        skill_dir = workspace / "skills" / "escape"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: ..\ndescription: Escape upward.\n---\n\nBody.\n",
            encoding="utf-8",
        )

        entry = skill_manifest.parse_skill_dir(skill_dir)
        assert entry.is_valid is False

        result = skills_projection.refresh(workspace, home)

        assert result.projected == 0
        # Nothing was written next to the projection root, which is where a
        # `..` destination would have landed.
        assert sorted(p.name for p in home.iterdir()) == [
            skills_projection.STATE_FILENAME
        ]

    def test_a_separator_bearing_name_is_excluded(self, tmp_path: Path):
        """``name: nested/child`` cannot open a second path segment."""
        workspace, home = _workspace(tmp_path)
        skill_dir = workspace / "skills" / "nested"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: nested/child\ndescription: Two segments.\n---\n\nBody.\n",
            encoding="utf-8",
        )

        result = skills_projection.refresh(workspace, home)

        assert result.projected == 0
        assert not (home / "skills" / "nested").exists()

    def test_only_regex_clean_names_are_ever_projected(self, tmp_path: Path):
        """A mixed tree projects the clean name and nothing else.

        The valid skill is what makes this test able to fail: without it the
        projection root would be absent for the trivial reason that nothing was
        projected at all, and the assertion would prove nothing.
        """
        workspace, home = _workspace(tmp_path)
        skills_root = workspace / "skills"
        _write_skill(skills_root, "clean-name", frontmatter=_valid("clean-name"))
        for dir_name, declared in (
            ("dots", ".."),
            ("slashy", "a/b"),
            ("spacey", "has space"),
            ("shouty", "UPPER"),
        ):
            bad = skills_root / dir_name
            bad.mkdir()
            (bad / "SKILL.md").write_text(
                f"---\nname: {declared}\ndescription: Nope.\n---\n\nBody.\n",
                encoding="utf-8",
            )

        result = skills_projection.refresh(workspace, home)

        assert result.projected == 1
        assert sorted(p.name for p in (home / "skills").iterdir()) == ["clean-name"]


class TestNonDirectoriesAtTheSkillsRoot:
    """``skills/README.md`` is a shipped file, not a broken skill.

    The Local Agent Kit template puts a README at the root of ``skills/`` to
    explain the layout, so a plain file there is the normal case. It must not
    be projected, must not stop the valid skills beside it, and must not turn
    into a red row.
    """

    def test_a_readme_at_the_root_is_ignored_and_the_rest_still_projects(
        self, tmp_path: Path
    ):
        workspace, home = _workspace(tmp_path)
        skills_root = workspace / "skills"
        (skills_root / "README.md").write_text(
            "Put one folder per skill here.\n", encoding="utf-8"
        )
        _write_skill(skills_root, "pdf-report", frontmatter=_valid("pdf-report"))

        result = skills_projection.refresh(workspace, home)

        assert result.projected == 1
        assert (home / "skills" / "pdf-report" / "SKILL.md").is_file()
        assert not (home / "skills" / "README.md").exists()

    def test_a_root_readme_alone_projects_nothing_and_asks_for_no_rebuild(
        self, tmp_path: Path
    ):
        """A skills/ dir holding only the template README is "no skills".

        `changed` drives OpenCode's instance dispose, so a README-only tree
        reporting a change would cost every kit-scaffolded agent a dispose on
        its first message for a folder with nothing in it.
        """
        workspace, home = _workspace(tmp_path)
        (workspace / "skills" / "README.md").write_text("Layout.\n", encoding="utf-8")

        result = skills_projection.refresh(workspace, home)

        assert result.projected == 0
        assert result.changed is False
        assert not (home / "skills").exists()

    def test_editing_the_root_readme_keeps_the_projection_intact(
        self, tmp_path: Path
    ):
        """A README edit moves the tree hash, so the fast path is skipped and
        the skills beside it are re-copied.

        Documented cost, not a defect: the hash covers everything under
        ``skills/``, so a scaffolding file nobody projects still invalidates
        the projection and (under OpenCode) buys one instance dispose. The
        alternative — hashing only the directories that parse as skills — would
        cost a full parse on the fast path this design exists to keep cheap.
        What matters here is that the projection survives it correctly.
        """
        workspace, home = _workspace(tmp_path)
        skills_root = workspace / "skills"
        (skills_root / "README.md").write_text("v1\n", encoding="utf-8")
        _write_skill(skills_root, "pdf-report", frontmatter=_valid("pdf-report"))
        first = skills_projection.refresh(workspace, home)

        (skills_root / "README.md").write_text("v2 — longer text\n", encoding="utf-8")
        second = skills_projection.refresh(workspace, home)

        assert second.hash != first.hash
        assert second.projected == 1
        assert (home / "skills" / "pdf-report" / "SKILL.md").is_file()
        assert sorted(p.name for p in (home / "skills").iterdir()) == ["pdf-report"]

    def test_a_symlink_at_the_root_is_neither_projected_nor_followed(
        self, tmp_path: Path
    ):
        workspace, home = _workspace(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "loot.txt").write_text("secret\n", encoding="utf-8")
        (workspace / "skills" / "linked.md").symlink_to(outside / "loot.txt")
        _write_skill(workspace / "skills", "pdf-report", frontmatter=_valid("pdf-report"))

        result = skills_projection.refresh(workspace, home)

        assert result.projected == 1
        assert sorted(p.name for p in (home / "skills").iterdir()) == ["pdf-report"]


class TestAWriteFailureIsReportedNotRaised:
    """§9: "Projection write fails (disk, permission) → logged, message
    proceeds". ``refresh`` is called from the chat path, so a failure has to
    come back as data on the result, never as an exception."""

    def test_an_unwritable_projection_root_returns_instead_of_raising(
        self, tmp_path: Path, monkeypatch
    ):
        """The failure is at ``mkdir`` — before any per-skill guard runs — so
        it exercises the outermost catch rather than ``_project_one``'s."""
        workspace, home = _workspace(tmp_path)
        _write_skill(workspace / "skills", "pdf-report", frontmatter=_valid("pdf-report"))

        real_mkdir = Path.mkdir

        def _mkdir(self, *args, **kwargs):
            if self.name == "skills" and self.parent == home:
                raise PermissionError("read-only file system")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", _mkdir)

        result = skills_projection.refresh(workspace, home)

        assert result.changed is False
        assert result.projected == 0
        assert result.errors
        # An empty identity is "I could not tell", which sdk_manager reads as
        # "ask no engine to rebuild" — see TestSkillsChangedIsPerMode in
        # tests/unit/test_skill_manifest.py.
        assert result.identity == ""

    def test_a_per_skill_failure_still_returns_a_usable_result(
        self, tmp_path: Path, monkeypatch
    ):
        """One broken skill must not cost the healthy ones their projection."""
        workspace, home = _workspace(tmp_path)
        _write_skill(workspace / "skills", "good", frontmatter=_valid("good"))
        _write_skill(workspace / "skills", "broken", frontmatter=_valid("broken"))

        real_copy = skills_projection._copy_skill

        def _copy(source, destination):
            if Path(source).name == "broken":
                raise PermissionError("permission denied")
            real_copy(source, destination)

        monkeypatch.setattr(skills_projection, "_copy_skill", _copy)

        result = skills_projection.refresh(workspace, home)

        assert result.projected == 1
        assert result.identity != ""
        assert any("broken" in error for error in result.errors)
        assert (home / "skills" / "good" / "SKILL.md").is_file()
        assert not (home / "skills" / "broken").exists()
