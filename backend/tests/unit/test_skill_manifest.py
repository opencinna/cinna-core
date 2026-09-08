"""Unit tests: agent skills manifest parser, vendored-copy drift guard, projection.

``skill_manifest.py`` exists twice on purpose — once for the host backend and
once vendored into env-core, which runs inside the agent container and cannot
import backend modules. The two files MUST stay byte-identical, so the drift
guard below is the same shape as ``test_synced_files_registry.py``: a cheap CI
assertion instead of a runtime surprise where the container validates a skill
differently from the host that published it.

``skills_projection`` is env-core-only (it writes into ``/root/.claude``), and
is imported here through the ``app_core_base`` path that ``unit/conftest.py``
puts on ``sys.path``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.services.agents import skill_manifest

from core.server import skills_projection


_APP_CORE_BASE = (
    Path(__file__).parents[2] / "app" / "env-templates" / "app_core_base"
)
_HOST_COPY = (
    Path(__file__).parents[2] / "app" / "services" / "agents" / "skill_manifest.py"
)
_VENDORED_COPY = _APP_CORE_BASE / "core" / "server" / "skill_manifest.py"


def _write_skill(root: Path, name: str, *, frontmatter: str, body: str = "Body.") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8"
    )
    return skill_dir


def _valid(name: str = "pdf-report") -> str:
    return f"name: {name}\ndescription: Build a PDF report from a CSV."


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------


class TestVendoredCopyIdentity:

    def test_host_and_env_core_copies_are_byte_identical(self):
        assert _HOST_COPY.is_file(), f"missing host parser at {_HOST_COPY}"
        assert _VENDORED_COPY.is_file(), f"missing vendored parser at {_VENDORED_COPY}"

        host_digest = hashlib.sha256(_HOST_COPY.read_bytes()).hexdigest()
        vendored_digest = hashlib.sha256(_VENDORED_COPY.read_bytes()).hexdigest()

        assert host_digest == vendored_digest, (
            "skill_manifest.py has drifted between the host backend and the "
            "vendored env-core copy. The host file is the source: copy it over "
            f"{_VENDORED_COPY.relative_to(_APP_CORE_BASE.parents[2])} verbatim. "
            "A drifted parser means the container validates a skill differently "
            "from the host that published it."
        )


# ---------------------------------------------------------------------------
# parse_skill_dir
# ---------------------------------------------------------------------------


class TestParseSkillDir:

    def test_valid_skill_is_parsed(self, tmp_path: Path):
        skill_dir = _write_skill(tmp_path, "pdf-report", frontmatter=_valid())
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")

        entry = skill_manifest.parse_skill_dir(skill_dir)

        assert entry.error is None
        assert entry.is_valid
        assert entry.name == "pdf-report"
        assert entry.description == "Build a PDF report from a CSV."
        assert entry.path == "skills/pdf-report"
        assert entry.has_scripts is True
        assert entry.user_invocable is True
        assert entry.model_invocable is True
        assert entry.size_bytes > 0

    def test_missing_skill_md(self, tmp_path: Path):
        (tmp_path / "empty").mkdir()
        assert skill_manifest.parse_skill_dir(tmp_path / "empty").error.code == "missing_skill_md"

    def test_missing_frontmatter_fence(self, tmp_path: Path):
        skill_dir = tmp_path / "no-fence"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Just markdown\n", encoding="utf-8")
        assert skill_manifest.parse_skill_dir(skill_dir).error.code == "invalid_frontmatter"

    def test_name_must_equal_directory_name(self, tmp_path: Path):
        _write_skill(tmp_path, "on-disk", frontmatter=_valid("in-frontmatter"))
        assert skill_manifest.parse_skill_dir(tmp_path / "on-disk").error.code == "name_mismatch"

    def test_invalid_name_shape(self, tmp_path: Path):
        _write_skill(tmp_path, "Bad_Name", frontmatter="name: Bad_Name\ndescription: x")
        assert skill_manifest.parse_skill_dir(tmp_path / "Bad_Name").error.code == "invalid_name"

    def test_reserved_name_is_rejected(self, tmp_path: Path):
        _write_skill(tmp_path, "files-all", frontmatter=_valid("files-all"))
        assert skill_manifest.parse_skill_dir(tmp_path / "files-all").error.code == "reserved_name"

    def test_missing_description(self, tmp_path: Path):
        _write_skill(tmp_path, "no-desc", frontmatter="name: no-desc")
        assert skill_manifest.parse_skill_dir(tmp_path / "no-desc").error.code == "missing_description"

    def test_description_cap(self, tmp_path: Path):
        long_description = "x" * (skill_manifest.MAX_DESCRIPTION_LENGTH + 1)
        _write_skill(
            tmp_path, "long-desc", frontmatter=f"name: long-desc\ndescription: {long_description}"
        )
        assert (
            skill_manifest.parse_skill_dir(tmp_path / "long-desc").error.code
            == "description_too_long"
        )

    def test_oversized_body_is_a_warning_not_an_error(self, tmp_path: Path):
        body = "a" * (skill_manifest.MAX_BODY_BYTES + 1)
        _write_skill(tmp_path, "big", frontmatter=_valid("big"), body=body)
        entry = skill_manifest.parse_skill_dir(tmp_path / "big")
        assert entry.error is None
        assert entry.warning.code == "oversized"

    def test_secret_file_is_a_structured_warning_not_an_error(self, tmp_path: Path):
        skill_dir = _write_skill(tmp_path, "leaky", frontmatter=_valid("leaky"))
        (skill_dir / ".env").write_text("TOKEN=abc\n", encoding="utf-8")

        entry = skill_manifest.parse_skill_dir(skill_dir)

        # Still projectable — the skill works; only publishing is blocked.
        assert entry.error is None
        assert entry.is_valid is True
        assert entry.is_publishable is False
        assert entry.secret_paths == [".env"]
        assert entry.warning.code == "secrets"
        assert entry.warning.paths == [".env"]

    def test_secrets_outrank_oversized_in_the_single_warning_slot(self, tmp_path: Path):
        body = "a" * (skill_manifest.MAX_BODY_BYTES + 1)
        skill_dir = _write_skill(
            tmp_path, "big-leaky", frontmatter=_valid("big-leaky"), body=body
        )
        (skill_dir / "id_rsa").write_text("KEY\n", encoding="utf-8")

        assert skill_manifest.parse_skill_dir(skill_dir).warning.code == "secrets"

    def test_clean_skill_is_publishable_and_carries_no_secret_paths(self, tmp_path: Path):
        skill_dir = _write_skill(tmp_path, "clean", frontmatter=_valid("clean"))
        (skill_dir / ".env.example").write_text("TOKEN=\n", encoding="utf-8")

        entry = skill_manifest.parse_skill_dir(skill_dir)

        assert entry.secret_paths == []
        assert entry.is_publishable is True

    def test_every_issue_code_has_a_message(self, tmp_path: Path):
        _write_skill(tmp_path, "on-disk", frontmatter=_valid("in-frontmatter"))
        issue = skill_manifest.parse_skill_dir(tmp_path / "on-disk").error
        assert issue.message == skill_manifest.ISSUE_MESSAGES["name_mismatch"]
        assert issue.message != issue.code

    def test_issue_round_trips_through_its_json_form(self, tmp_path: Path):
        skill_dir = _write_skill(tmp_path, "leaky2", frontmatter=_valid("leaky2"))
        (skill_dir / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
        original = skill_manifest.parse_skill_dir(skill_dir).warning

        restored = skill_manifest.issue_from_dict(original.to_dict())

        assert restored == original

    def test_bare_code_string_still_reads_as_an_issue(self):
        # A cache row written before ``{code, message}`` existed.
        restored = skill_manifest.issue_from_dict("budget")
        assert restored.code == "budget"
        assert restored.message == skill_manifest.ISSUE_MESSAGES["budget"]

    def test_optional_invocation_keys_are_interpreted(self, tmp_path: Path):
        _write_skill(
            tmp_path,
            "internal",
            frontmatter=(
                "name: internal\ndescription: An internal helper.\n"
                "user-invocable: false\ndisable-model-invocation: true"
            ),
        )
        entry = skill_manifest.parse_skill_dir(tmp_path / "internal")
        assert entry.error is None
        assert entry.user_invocable is False
        assert entry.model_invocable is False

    def test_frontmatter_passes_unknown_keys_through(self, tmp_path: Path):
        _write_skill(
            tmp_path,
            "tooled",
            frontmatter=(
                "name: tooled\ndescription: Uses tools.\n"
                "allowed-tools: [Bash, Read]\nlicense: MIT"
            ),
        )
        entry = skill_manifest.parse_skill_dir(tmp_path / "tooled")
        assert entry.frontmatter["allowed-tools"] == ["Bash", "Read"]
        assert entry.frontmatter["license"] == "MIT"


# ---------------------------------------------------------------------------
# scan_skills_root / tree_hash / validate_tree
# ---------------------------------------------------------------------------


class TestScanSkillsRoot:

    def test_missing_root_is_empty_not_an_error(self, tmp_path: Path):
        assert skill_manifest.scan_skills_root(tmp_path / "skills") == []

    def test_plain_file_at_the_skills_root_is_skipped(self, tmp_path: Path):
        # The Local Agent Kit ships ``skills/README.md``; it is scaffolding, not
        # a broken skill, so it must not appear in the index at all.
        (tmp_path / "README.md").write_text("How skills work\n", encoding="utf-8")
        _write_skill(tmp_path, "real", frontmatter=_valid("real"))

        entries = skill_manifest.scan_skills_root(tmp_path)

        assert [e.name for e in entries] == ["real"]

    def test_entries_are_name_sorted(self, tmp_path: Path):
        for name in ("zulu", "alpha", "mike"):
            _write_skill(tmp_path, name, frontmatter=_valid(name))
        names = [e.name for e in skill_manifest.scan_skills_root(tmp_path)]
        assert names == ["alpha", "mike", "zulu"]

    def test_skill_cap_marks_overflow_as_budget(self, tmp_path: Path):
        for index in range(4):
            name = f"skill-{index}"
            _write_skill(tmp_path, name, frontmatter=_valid(name))
        entries = skill_manifest.scan_skills_root(tmp_path, max_skills=2)
        assert [e.error.code if e.error else None for e in entries] == [
            None,
            None,
            "budget",
            "budget",
        ]

    def test_byte_budget_marks_overflow_as_budget(self, tmp_path: Path):
        for name in ("aaa", "bbb"):
            _write_skill(tmp_path, name, frontmatter=_valid(name), body="x" * 200)
        entries = skill_manifest.scan_skills_root(tmp_path, max_total_bytes=300)
        assert entries[0].error is None
        assert entries[1].error.code == "budget"

    def test_invalid_entries_do_not_consume_budget(self, tmp_path: Path):
        _write_skill(tmp_path, "aaa", frontmatter="name: wrong\ndescription: x")
        _write_skill(tmp_path, "bbb", frontmatter=_valid("bbb"))
        entries = skill_manifest.scan_skills_root(tmp_path, max_skills=1)
        assert entries[0].error.code == "name_mismatch"
        assert entries[1].error is None


class TestApplyBudget:
    """The caps are per AGENT, so they are applied over the merged index."""

    def _entry(self, name: str, size: int = 100, error=None):
        return skill_manifest.SkillEntry(name=name, size_bytes=size, error=error)

    def test_overflow_past_the_count_cap_is_excluded(self):
        entries = [self._entry(f"s-{i:02d}") for i in range(5)]

        skill_manifest.apply_budget(entries, max_skills=3)

        assert [e.error.code if e.error else None for e in entries] == [
            None, None, None, "budget", "budget",
        ]

    def test_overflow_past_the_byte_cap_is_excluded(self):
        entries = [self._entry("a", size=200), self._entry("b", size=200)]

        skill_manifest.apply_budget(entries, max_total_bytes=300)

        assert entries[0].error is None
        assert entries[1].error.code == "budget"

    def test_an_already_broken_entry_charges_no_budget(self):
        broken = self._entry("broken", error=skill_manifest.SkillIssue("name_mismatch"))
        entries = [broken, self._entry("good")]

        skill_manifest.apply_budget(entries, max_skills=1)

        assert entries[0].error.code == "name_mismatch"  # untouched
        assert entries[1].error is None                  # still got the slot

    def test_re_application_is_a_fixed_point(self):
        # scan_skills_root already applies the caps per root; the index builder
        # applies them again over the merged list. The second pass must only be
        # able to ADD exclusions, and a third must change nothing.
        entries = [self._entry(f"s-{i:02d}") for i in range(5)]

        skill_manifest.apply_budget(entries, max_skills=3)
        after_first = [e.error.code if e.error else None for e in entries]
        skill_manifest.apply_budget(entries, max_skills=3)

        assert [e.error.code if e.error else None for e in entries] == after_first

    def test_merged_budget_equals_the_first_n_of_the_merged_order(self):
        # Two roots that each fit their own cap can jointly exceed it. Whoever
        # sorts first survives, regardless of which root they came from.
        local = [self._entry(f"m-{i:02d}") for i in range(3)]
        plugin = [self._entry("a-00"), self._entry("z-00")]
        merged = sorted(local + plugin, key=lambda e: e.name)

        skill_manifest.apply_budget(merged, max_skills=3)

        surviving = [e.name for e in merged if e.error is None]
        assert surviving == ["a-00", "m-00", "m-01"]


class TestTreeHash:

    def test_missing_root_hashes_stably(self, tmp_path: Path):
        assert skill_manifest.tree_hash(tmp_path / "nope") == skill_manifest.tree_hash(
            tmp_path / "also-nope"
        )

    def test_hash_changes_when_content_changes(self, tmp_path: Path):
        _write_skill(tmp_path, "aaa", frontmatter=_valid("aaa"))
        before = skill_manifest.tree_hash(tmp_path)
        (tmp_path / "aaa" / "SKILL.md").write_text(
            "---\nname: aaa\ndescription: A longer description now.\n---\n\nBody.\n",
            encoding="utf-8",
        )
        assert skill_manifest.tree_hash(tmp_path) != before

    def test_hash_is_stable_for_an_untouched_tree(self, tmp_path: Path):
        _write_skill(tmp_path, "aaa", frontmatter=_valid("aaa"))
        assert skill_manifest.tree_hash(tmp_path) == skill_manifest.tree_hash(tmp_path)


class TestValidateTree:

    def test_clean_tree_has_no_hits(self, tmp_path: Path):
        skill_dir = _write_skill(tmp_path, "aaa", frontmatter=_valid("aaa"))
        (skill_dir / "config.yaml").write_text("k: v\n", encoding="utf-8")
        assert skill_manifest.validate_tree(tmp_path) == []

    def test_dotenv_shapes_are_reported(self, tmp_path: Path):
        skill_dir = _write_skill(tmp_path, "aaa", frontmatter=_valid("aaa"))
        (skill_dir / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (skill_dir / ".env.production").write_text("SECRET=1\n", encoding="utf-8")
        (skill_dir / "service.env").write_text("SECRET=1\n", encoding="utf-8")
        hits = skill_manifest.validate_tree(tmp_path)
        assert set(hits) == {"aaa/.env", "aaa/.env.production", "aaa/service.env"}

    def test_example_dotenv_is_allowed(self, tmp_path: Path):
        skill_dir = _write_skill(tmp_path, "aaa", frontmatter=_valid("aaa"))
        (skill_dir / ".env.example").write_text("SECRET=\n", encoding="utf-8")
        assert skill_manifest.validate_tree(tmp_path) == []

    def test_key_material_filenames_are_reported(self, tmp_path: Path):
        skill_dir = _write_skill(tmp_path, "aaa", frontmatter=_valid("aaa"))
        (skill_dir / "id_rsa").write_text("key\n", encoding="utf-8")
        (skill_dir / "cert.pem").write_text("cert\n", encoding="utf-8")
        assert set(skill_manifest.validate_tree(tmp_path)) == {"aaa/id_rsa", "aaa/cert.pem"}


class TestReservedNames:

    def test_every_registered_platform_command_is_reserved(self):
        """The reserved list is a vendored mirror of the command registry.

        env-core cannot import the registry, so the tuple is copied. This test
        is the thing that keeps the copy honest: a new slash command whose name
        a skill could shadow must be added to RESERVED_SKILL_NAMES.
        """
        import app.services.agents.commands  # noqa: F401  (registers handlers)
        from app.services.agents.command_service import CommandService

        registered = {
            handler.name.lstrip("/") for handler in CommandService.list_handlers()
        }
        missing = registered - skill_manifest.RESERVED_SKILL_NAMES
        assert not missing, (
            f"platform commands not reserved against skill names: {sorted(missing)!r}. "
            "Add them to RESERVED_SKILL_NAMES in skill_manifest.py (both copies)."
        )


# ---------------------------------------------------------------------------
# skills_projection (env-core)
# ---------------------------------------------------------------------------


class TestSkillsProjection:

    def _workspace(self, tmp_path: Path) -> tuple[Path, Path]:
        workspace = tmp_path / "workspace"
        (workspace / "skills").mkdir(parents=True)
        home = tmp_path / "claude_home"
        home.mkdir()
        return workspace, home

    def test_first_run_projects_valid_skills(self, tmp_path: Path):
        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "aaa", frontmatter=_valid("aaa"))

        result = skills_projection.refresh(workspace, home)

        assert result.changed is True
        assert result.projected == 1
        projected = home / "skills" / "aaa"
        assert (projected / "SKILL.md").is_file()
        assert (projected / skills_projection.PROJECTION_MARKER).is_file()

    def test_unchanged_tree_is_a_no_op(self, tmp_path: Path):
        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "aaa", frontmatter=_valid("aaa"))
        skills_projection.refresh(workspace, home)

        second = skills_projection.refresh(workspace, home)

        assert second.changed is False
        assert second.projected == 1

    def test_no_skills_creates_nothing_and_reports_unchanged(self, tmp_path: Path):
        """D6: an agent with no skills behaves exactly as before the feature.

        `changed` drives OpenCode's instance dispose, so reporting True here
        would cost every skill-less environment a dispose on its first message
        for a directory that does not exist.
        """
        workspace, home = self._workspace(tmp_path)

        result = skills_projection.refresh(workspace, home)

        assert result.changed is False
        assert result.projected == 0
        assert not (home / "skills").exists()

    def test_only_invalid_skills_reports_unchanged(self, tmp_path: Path):
        """`changed` reports what the ENGINE would see, not that the hash moved.

        A tree of nothing but excluded skills projects nothing and prunes
        nothing, so it must not cost an OpenCode dispose.
        """
        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "bad", frontmatter="name: wrong\ndescription: x")

        result = skills_projection.refresh(workspace, home)

        assert result.changed is False
        assert result.projected == 0

    def test_invalid_skill_is_not_projected(self, tmp_path: Path):
        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "aaa", frontmatter="name: wrong\ndescription: x")

        result = skills_projection.refresh(workspace, home)

        assert result.projected == 0
        assert not (home / "skills" / "aaa").exists()

    def test_removed_skill_is_pruned(self, tmp_path: Path):
        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "aaa", frontmatter=_valid("aaa"))
        _write_skill(workspace / "skills", "bbb", frontmatter=_valid("bbb"))
        skills_projection.refresh(workspace, home)

        import shutil

        shutil.rmtree(workspace / "skills" / "bbb")
        result = skills_projection.refresh(workspace, home)

        assert result.changed is True
        assert (home / "skills" / "aaa").is_dir()
        assert not (home / "skills" / "bbb").exists()

    def test_foreign_directory_under_the_skill_root_is_untouched(self, tmp_path: Path):
        workspace, home = self._workspace(tmp_path)
        foreign = home / "skills" / "hand-placed"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("mine\n", encoding="utf-8")
        _write_skill(workspace / "skills", "aaa", frontmatter=_valid("aaa"))

        skills_projection.refresh(workspace, home)

        assert (foreign / "SKILL.md").read_text(encoding="utf-8") == "mine\n"

    def test_a_symlinked_skill_is_refused(self, tmp_path: Path):
        workspace, home = self._workspace(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(
            f"---\n{_valid('linked')}\n---\n\nBody.\n", encoding="utf-8"
        )
        (workspace / "skills" / "linked").symlink_to(outside, target_is_directory=True)

        result = skills_projection.refresh(workspace, home)

        assert result.projected == 0
        assert not (home / "skills" / "linked").exists()

    def test_symlink_inside_a_skill_is_not_followed(self, tmp_path: Path):
        """The projector copies a tree, never what a link points at.

        Following links would let an agent-authored workspace pull host-visible
        content into the projection and bypass the size budget, which the
        manifest's own walk already refuses to count.
        """
        workspace, home = self._workspace(tmp_path)
        secret_dir = tmp_path / "outside"
        secret_dir.mkdir()
        (secret_dir / "loot.txt").write_text("secret\n", encoding="utf-8")
        skill_dir = _write_skill(workspace / "skills", "aaa", frontmatter=_valid("aaa"))
        (skill_dir / "escape").symlink_to(secret_dir, target_is_directory=True)

        skills_projection.refresh(workspace, home)

        projected = home / "skills" / "aaa"
        assert (projected / "SKILL.md").is_file()
        assert not (projected / "escape").exists()

    def test_a_failed_skill_is_retried_and_recovers(self, tmp_path: Path, monkeypatch):
        """A partial projection is retried on the next message, not written off."""
        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "aaa", frontmatter=_valid("aaa"))

        def _boom(source, destination):
            raise OSError("no space left on device")

        monkeypatch.setattr(skills_projection, "_copy_skill", _boom)
        first = skills_projection.refresh(workspace, home)
        assert first.errors
        assert first.projected == 0

        monkeypatch.undo()
        second = skills_projection.refresh(workspace, home)
        assert second.projected == 1
        assert second.changed is True
        assert (home / "skills" / "aaa" / "SKILL.md").is_file()

    def test_a_durable_failure_retries_only_the_failed_skill(
        self, tmp_path: Path, monkeypatch
    ):
        """A permanently broken skill must not re-copy the whole tree forever.

        Without the persisted failed-name set, the hash could never latch, so
        every message would re-copy every healthy skill AND report `changed`,
        disposing the OpenCode instance on every single turn for as long as the
        failure lasted.
        """
        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "good", frontmatter=_valid("good"))
        _write_skill(workspace / "skills", "broken", frontmatter=_valid("broken"))

        real_copy = skills_projection._copy_skill
        copied: list[str] = []

        def _copy(source, destination):
            copied.append(Path(source).name)
            if Path(source).name == "broken":
                raise OSError("permission denied")
            real_copy(source, destination)

        monkeypatch.setattr(skills_projection, "_copy_skill", _copy)

        first = skills_projection.refresh(workspace, home)
        assert first.projected == 1
        assert first.changed is True

        copied.clear()
        second = skills_projection.refresh(workspace, home)

        # Only the broken one is attempted again, and the identity holds still
        # — so no engine is asked to rebuild on an otherwise idle turn, for as
        # long as the failure lasts.
        assert copied == ["broken"]
        assert second.changed is False
        assert second.projected == 1
        assert second.identity == first.identity

        third = skills_projection.refresh(workspace, home)
        assert third.identity == first.identity

    def test_identity_moves_when_a_retry_finally_lands(
        self, tmp_path: Path, monkeypatch
    ):
        """The workspace tree never moved, but what the engine sees did."""
        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "good", frontmatter=_valid("good"))
        _write_skill(workspace / "skills", "flaky", frontmatter=_valid("flaky"))

        real_copy = skills_projection._copy_skill
        fail = {"active": True}

        def _copy(source, destination):
            if fail["active"] and Path(source).name == "flaky":
                raise OSError("temporarily unavailable")
            real_copy(source, destination)

        monkeypatch.setattr(skills_projection, "_copy_skill", _copy)
        first = skills_projection.refresh(workspace, home)
        assert first.projected == 1

        fail["active"] = False
        second = skills_projection.refresh(workspace, home)

        assert second.hash == first.hash
        assert second.identity != first.identity
        assert second.projected == 2
        assert second.changed is True

    def test_a_failed_copy_leaves_nothing_behind(self, tmp_path: Path, monkeypatch):
        """A partial copy must not survive as an unprunable skill.

        `_prune_stale` deliberately refuses to touch a directory without the
        marker ("somebody else's skill"), but both engines index any SKILL.md
        they find — so a half-written directory would be a broken skill in the
        index that no later run could clean up.
        """
        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "aaa", frontmatter=_valid("aaa"))

        def _copy(source, destination):
            Path(destination).mkdir(parents=True, exist_ok=True)
            (Path(destination) / "SKILL.md").write_text("partial\n", encoding="utf-8")
            raise OSError("no space left on device")

        monkeypatch.setattr(skills_projection, "_copy_skill", _copy)
        result = skills_projection.refresh(workspace, home)

        assert result.errors
        assert not (home / "skills" / "aaa").exists()

    def test_the_fast_path_does_not_parse_any_skill(self, tmp_path: Path, monkeypatch):
        """The steady state is a stat walk plus one iterdir — nothing more.

        The projected name set is persisted rather than re-derived precisely so
        the common case never re-reads 50 SKILL.md files just to learn what it
        already recorded.
        """
        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "aaa", frontmatter=_valid("aaa"))
        skills_projection.refresh(workspace, home)

        def _should_not_run(*args, **kwargs):
            raise AssertionError("fast path must not scan the skills root")

        monkeypatch.setattr(skills_projection, "scan_skills_root", _should_not_run)
        monkeypatch.setattr(skills_projection, "parse_skill_dir", _should_not_run)

        result = skills_projection.refresh(workspace, home)

        assert result.changed is False
        assert result.projected == 1

    def test_a_corrupt_state_file_self_heals(self, tmp_path: Path):
        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "aaa", frontmatter=_valid("aaa"))
        skills_projection.refresh(workspace, home)
        (home / skills_projection.STATE_FILENAME).write_text("{not json", encoding="utf-8")

        result = skills_projection.refresh(workspace, home)

        assert result.projected == 1
        assert (home / "skills" / "aaa" / "SKILL.md").is_file()

    def test_wiped_projection_is_rebuilt_even_though_the_hash_matches(
        self, tmp_path: Path
    ):
        """A hash file outliving the directory it describes must not latch.

        `claude_sessions/` can be wiped independently of the workspace; trusting
        the hash alone would report "up to date" forever while the engine sees
        no skills at all.
        """
        import shutil

        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "aaa", frontmatter=_valid("aaa"))
        skills_projection.refresh(workspace, home)
        shutil.rmtree(home / "skills")

        result = skills_projection.refresh(workspace, home)

        assert result.projected == 1
        assert (home / "skills" / "aaa" / "SKILL.md").is_file()

    def test_failure_never_raises(self, tmp_path: Path, monkeypatch):
        workspace, home = self._workspace(tmp_path)
        _write_skill(workspace / "skills", "aaa", frontmatter=_valid("aaa"))

        def _boom(*args, **kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(skills_projection, "scan_skills_root", _boom)

        result = skills_projection.refresh(workspace, home)

        assert result.changed is False
        assert result.errors


# ---------------------------------------------------------------------------
# SDKManager: the per-mode skills_changed signal
# ---------------------------------------------------------------------------


class TestSkillsChangedIsPerMode:
    """Building and conversation run separate engine processes.

    Each memoizes its own skill list, so "the projection changed" has to be
    answered once per mode. A single shared flag loses the feature's headline
    flow — the building agent writes a skill, the user then uses it in
    conversation — because the building message consumed the signal.
    """

    def _manager_with_fake_adapter(self, monkeypatch):
        from core.server import sdk_manager as sdk_manager_module

        seen: list[tuple[str, bool]] = []

        class _FakeAdapter:
            ADAPTER_TYPE = "fake"
            workspace_dir = "/app/workspace"

            async def send_message_stream(self, *, mode, skills_changed, **kwargs):
                seen.append((mode, skills_changed))
                return
                yield  # pragma: no cover — makes this an async generator

        manager = sdk_manager_module.SDKManager()
        monkeypatch.setattr(manager, "_get_adapter", lambda mode: _FakeAdapter())
        return manager, seen

    async def _send(self, manager, mode):
        async for _ in manager.send_message_stream(message="hi", mode=mode):
            pass

    def test_each_mode_is_told_about_a_change_once(self, monkeypatch):
        import asyncio

        from core.server import sdk_manager as sdk_manager_module

        manager, seen = self._manager_with_fake_adapter(monkeypatch)

        identities = iter(["ident-a", "ident-a", "ident-b", "ident-b"])

        def _fake_refresh(workspace_dir, *args, **kwargs):
            return skills_projection.ProjectionResult(identity=next(identities))

        monkeypatch.setattr(
            sdk_manager_module.skills_projection, "refresh", _fake_refresh
        )

        async def _run():
            await self._send(manager, "building")
            await self._send(manager, "conversation")
            await self._send(manager, "building")
            await self._send(manager, "conversation")

        asyncio.run(_run())

        assert seen == [
            ("building", True),      # first message of the mode
            ("conversation", True),  # same hash, but THIS mode had not seen it
            ("building", True),      # hash moved
            ("conversation", True),  # the move reaches conversation too
        ]

    def test_an_unchanged_projection_is_reported_once_per_mode(self, monkeypatch):
        import asyncio

        from core.server import sdk_manager as sdk_manager_module

        manager, seen = self._manager_with_fake_adapter(monkeypatch)
        monkeypatch.setattr(
            sdk_manager_module.skills_projection,
            "refresh",
            lambda workspace_dir, *a, **k: skills_projection.ProjectionResult(
                identity="steady"
            ),
        )

        async def _run():
            await self._send(manager, "building")
            await self._send(manager, "building")
            await self._send(manager, "building")

        asyncio.run(_run())

        assert seen == [("building", True), ("building", False), ("building", False)]

    def test_a_durable_failure_is_reported_once_not_every_message(self, monkeypatch):
        """A permanently broken skill must not rebuild the engine every turn.

        The projection retries a failed skill on every message and keeps
        reporting the error, so gating on "did this run report errors" would
        dispose the OpenCode instance forever. The identity is what holds
        still: the engine sees the same thing each time, so it is told once.
        """
        import asyncio

        from core.server import sdk_manager as sdk_manager_module

        manager, seen = self._manager_with_fake_adapter(monkeypatch)
        monkeypatch.setattr(
            sdk_manager_module.skills_projection,
            "refresh",
            lambda workspace_dir, *a, **k: skills_projection.ProjectionResult(
                hash="steady",
                identity="ident-1",
                errors=["broken: permission denied"],
            ),
        )

        async def _run():
            await self._send(manager, "building")
            await self._send(manager, "building")
            await self._send(manager, "building")

        asyncio.run(_run())

        assert seen == [("building", True), ("building", False), ("building", False)]

    def test_a_recovered_skill_is_reported_even_though_the_tree_did_not_change(
        self, monkeypatch
    ):
        """A retry that finally lands changes what the engine sees.

        The workspace tree hash cannot see this — nothing in `skills/` moved —
        so a hash-keyed latch would leave the recovered skill invisible to a
        mode that had already latched that hash.
        """
        import asyncio

        from core.server import sdk_manager as sdk_manager_module

        manager, seen = self._manager_with_fake_adapter(monkeypatch)
        identities = iter(["before-retry", "after-retry"])
        monkeypatch.setattr(
            sdk_manager_module.skills_projection,
            "refresh",
            lambda workspace_dir, *a, **k: skills_projection.ProjectionResult(
                hash="steady", identity=next(identities)
            ),
        )

        async def _run():
            await self._send(manager, "building")
            await self._send(manager, "building")

        asyncio.run(_run())

        assert seen == [("building", True), ("building", True)]

    def test_a_projection_that_determined_nothing_asks_for_no_rebuild(
        self, monkeypatch
    ):
        """An empty identity is "I could not tell", not "everything changed"."""
        import asyncio

        from core.server import sdk_manager as sdk_manager_module

        manager, seen = self._manager_with_fake_adapter(monkeypatch)
        monkeypatch.setattr(
            sdk_manager_module.skills_projection,
            "refresh",
            lambda workspace_dir, *a, **k: skills_projection.ProjectionResult(
                errors=["disk on fire"]
            ),
        )

        async def _run():
            await self._send(manager, "building")

        asyncio.run(_run())

        assert seen == [("building", False)]
