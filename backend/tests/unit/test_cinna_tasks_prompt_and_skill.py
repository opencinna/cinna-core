"""
Cinna Task Protocol, prompts as data and the platform skill.

Covers, without a database:

* the env's task context block is the schema's ``context_block`` — data only,
  counts only while nonzero, the team connection prompt under "Why you
  received this:", nothing for an ordinary chat;
* configured handovers render the same text on the backend and in the env,
  naming only ``tasks_create`` / ``tasks_targets``, and an env config written
  by an older backend still yields the new text;
* a delegation result reaches the requester as the result packet text;
* ``tasks_update``'s manual moves walk the shortest legal status path;
* ``skills_projection`` projects the platform ``cinna-tasks`` skill beside the
  workspace's, refuses a workspace skill of that name, keeps the platform copy
  through refreshes, and the manifest keeps its extra frontmatter keys;
* the fallback "## Agent Skills" block lists the platform skill.

Run:
    docker compose exec backend python -m pytest \
        tests/unit/test_cinna_tasks_prompt_and_skill.py -v
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.services.agents import skill_manifest as host_manifest
from app.services.agents.agent_service import AgentService
from app.services.tasks.input_task_service import (
    RESULT_PACKET_BODY_LIMIT,
    InputTaskService,
)
from core.server import skill_manifest, skills_projection
from core.server.prompt_generator import PromptGenerator, build_handover_prompt

_BACKEND = Path(__file__).resolve().parents[2]
_KIT_SKILLS = (
    _BACKEND / "app" / "env-templates" / "platform-knowledge-env" / "app"
    / "workspace" / "knowledge" / "local-kit" / "skills"
)


# ── Task context block ──────────────────────────────────────────────────────


def _context(**protocol) -> dict:
    return {"task_protocol": {"here": "cinna-core agent Researcher", **protocol}}


def test_context_block_is_data_with_nonzero_counts_only() -> None:
    block = PromptGenerator.build_task_context_section(_context(
        current_task={"ref": "HR-12", "title": 'Add "retry"', "report_required": True},
        chain_depth=1, open_delegations=2, waiting_on_user=1, ready_tasks=0,
    ))
    lines = block.splitlines()
    lines = lines[lines.index("Cinna task context (not part of the conversation):"):]
    assert lines == [
        "Cinna task context (not part of the conversation):",
        "- here: cinna-core agent Researcher",
        "- current task: HR-12 \"Add 'retry'\" (report required: yes)",
        "- chain depth: 1 · open delegations: 2 (1 waiting on the user)",
        "- tasks skill: cinna-tasks v1",
    ]
    for instruction in ("mcp__", "add_comment", "update_status", "create_subtask", "auto-complete"):
        assert instruction not in block


def test_context_block_without_a_task_lists_counts_and_says_none() -> None:
    block = PromptGenerator.build_task_context_section(_context(
        current_task=None, chain_depth=0, open_delegations=0, waiting_on_user=0, ready_tasks=3,
    ))
    assert "- current task: none" in block
    assert "- ready tasks: 3" in block
    assert "chain depth" not in block and "open delegations" not in block


def test_no_block_for_an_ordinary_chat() -> None:
    assert PromptGenerator.build_task_context_section(None) is None
    assert PromptGenerator.build_task_context_section({"integration_type": None}) is None
    assert PromptGenerator.build_task_context_section(_context(
        current_task=None, chain_depth=0, open_delegations=0, ready_tasks=0,
    )) is None


def test_team_connection_prompt_is_quoted_verbatim() -> None:
    prompt = "Hand every invoice over\nwith its PDF."
    context = _context(current_task={"ref": "T-1", "title": "Invoice", "report_required": False})
    context["delegation_connection_prompt"] = prompt
    block = PromptGenerator.build_task_context_section(context)
    assert "(report required: no)" in block
    assert block.endswith(f"Why you received this:\n{prompt}")


def test_older_backend_payload_still_names_the_current_task() -> None:
    block = PromptGenerator.build_task_context_section(
        {"task_short_code": "OPS-3", "task_title": "Rotate keys"}
    )
    assert '- current task: OPS-3 "Rotate keys" (report required: no)' in block


# ── Handover prompt ─────────────────────────────────────────────────────────


_HANDOVERS = [
    {"id": str(uuid4()), "name": "Logger", "prompt": "As soon as rates are fetched, hand them over."},
    {"id": str(uuid4()), "name": "Mailer", "prompt": ""},
]


def test_handover_prompt_is_identical_on_both_sides_and_names_only_protocol_tools() -> None:
    backend = AgentService.build_handover_prompt(_HANDOVERS)
    env = build_handover_prompt(_HANDOVERS)
    assert backend == env
    assert f"- Logger (target `agent:{_HANDOVERS[0]['id']}`)" in env
    assert "  Trigger: As soon as rates are fetched, hand them over." in env
    assert "`tasks_create`" in env and "`tasks_targets`" in env
    for retired in ("create_agent_task", "task_message", "target_agent_id", "respond_to_task", "Sub-task"):
        assert retired not in env
    assert AgentService.build_handover_prompt([]) == build_handover_prompt([]) == ""


def test_env_renders_handovers_from_data_not_stale_prose(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "agent_handover_config.json").write_text(json.dumps({
        "handovers": _HANDOVERS,
        "handover_prompt": "Call `create_agent_task` with task_message and target_agent_id",
    }), encoding="utf-8")
    loaded = PromptGenerator(str(tmp_path))._load_handover_prompt()
    assert loaded == build_handover_prompt(_HANDOVERS)
    assert "create_agent_task" not in loaded

    (docs / "agent_handover_config.json").write_text(
        json.dumps({"handovers": [], "handover_prompt": "## TASK CREATION INSTRUCTIONS"}),
        encoding="utf-8",
    )
    assert PromptGenerator(str(tmp_path))._load_handover_prompt() is None


# ── Result packet ───────────────────────────────────────────────────────────


class _Db:
    def __init__(self, agent):
        self._agent = agent

    def get(self, _model, _id):
        return self._agent


def _task(result: dict | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), short_code="HR-7", selected_agent_id=uuid4(), delegation_result=result,
    )


def test_result_packet_from_a_stored_report() -> None:
    db = _Db(SimpleNamespace(name="Analyst"))
    text = InputTaskService.result_packet_text(db, _task({
        "status": "blocked", "summary": "Need the date range", "body": "Drafted the query.",
        "question": "Which quarter?", "audience": "requester",
        "artifacts": [{"kind": "link", "name": "draft.sql", "ref": "https://example.com/d.sql"}],
    }), "needs_input", "ignored")
    assert text.splitlines() == [
        "Task result from Analyst on HR-7:",
        "Status: blocked",
        "Summary: Need the date range",
        "",
        "Drafted the query.",
        "",
        "Artifacts:",
        "- draft.sql: https://example.com/d.sql",
        "",
        "Question for you (answer with tasks_reply): Which quarter?",
    ]


def test_result_packet_without_a_report_maps_the_session_state() -> None:
    db = _Db(SimpleNamespace(name="Analyst"))
    text = InputTaskService.result_packet_text(db, _task(None), "error", "HR-7 failed: timeout")
    assert text == "Task result from Analyst on HR-7:\nStatus: failed\nSummary: HR-7 failed: timeout"
    assert "[Sub-task" not in text


def test_result_packet_body_is_bounded() -> None:
    db = _Db(SimpleNamespace(name="Analyst"))
    body = "x" * (RESULT_PACKET_BODY_LIMIT + 50)
    text = InputTaskService.result_packet_text(
        db, _task({"status": "done", "summary": "Done", "body": body}), "completed", "",
    )
    assert "x" * RESULT_PACKET_BODY_LIMIT in text
    assert "x" * (RESULT_PACKET_BODY_LIMIT + 1) not in text
    assert "tasks_get HR-7" in text


# ── Manual moves walk the status path ───────────────────────────────────────


def test_status_path_is_the_shortest_legal_route() -> None:
    assert InputTaskService.status_path("new", "completed") == ["in_progress", "completed"]
    assert InputTaskService.status_path("open", "completed") == ["in_progress", "completed"]
    assert InputTaskService.status_path("new", "cancelled") == ["cancelled"]
    assert InputTaskService.status_path("open", "open") == []
    assert InputTaskService.status_path("cancelled", "completed") is None


# ── Platform skill projection ───────────────────────────────────────────────


def _write_skill(root: Path, name: str, description: str = "Workspace skill.") -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n", encoding="utf-8",
    )


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    home = tmp_path / "claude_home"
    home.mkdir()
    platform = tmp_path / "platform_skills"
    shutil.copytree(_KIT_SKILLS, platform)
    return workspace, home, platform


def test_platform_skill_is_projected_beside_workspace_skills(tmp_path: Path) -> None:
    workspace, home, platform = _layout(tmp_path)
    _write_skill(workspace / "skills", "pdf-report")

    result = skills_projection.refresh(workspace, home, platform)

    projected = home / "skills"
    assert result.errors == []
    # Every platform skill rides along (cinna-tasks, cinna-artifacts).
    assert sorted(p.name for p in projected.iterdir()) == ["cinna-artifacts", "cinna-tasks", "pdf-report"]
    assert (projected / "cinna-tasks" / skills_projection.PROJECTION_MARKER).is_file()
    assert (projected / "cinna-tasks" / "SKILL.md").read_bytes() == (
        _KIT_SKILLS / "cinna-tasks" / "SKILL.md"
    ).read_bytes()
    assert (projected / "cinna-tasks" / "references").is_dir()


def test_workspace_skill_named_cinna_tasks_is_refused_as_reserved(tmp_path: Path) -> None:
    workspace, home, platform = _layout(tmp_path)
    _write_skill(workspace / "skills", "cinna-tasks", "An impostor.")

    entry = skill_manifest.parse_skill_dir(workspace / "skills" / "cinna-tasks")
    assert entry.error.code == "reserved_name"
    assert "platform skill" in entry.error.message

    skills_projection.refresh(workspace, home, platform)
    projected = (home / "skills" / "cinna-tasks" / "SKILL.md").read_text(encoding="utf-8")
    assert "An impostor." not in projected
    assert "name: cinna-tasks" in projected


def test_platform_skill_survives_refreshes_and_workspace_removals(tmp_path: Path) -> None:
    workspace, home, platform = _layout(tmp_path)
    _write_skill(workspace / "skills", "pdf-report")
    first = skills_projection.refresh(workspace, home, platform)

    # Unchanged trees: fast path, same identity, platform copy still there.
    again = skills_projection.refresh(workspace, home, platform)
    assert again.identity == first.identity and not again.changed

    # Removing every workspace skill prunes those, never the platform one.
    shutil.rmtree(workspace / "skills" / "pdf-report")
    after_removal = skills_projection.refresh(workspace, home, platform)
    assert sorted(p.name for p in (home / "skills").iterdir()) == ["cinna-artifacts", "cinna-tasks"]
    assert after_removal.identity != first.identity

    # A platform skill change (an image upgrade) re-projects it.
    skill_md = platform / "cinna-tasks" / "SKILL.md"
    skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\nUpgraded.\n", encoding="utf-8")
    os.utime(skill_md, ns=(1, 1))
    upgraded = skills_projection.refresh(workspace, home, platform)
    assert upgraded.changed
    assert "Upgraded." in (home / "skills" / "cinna-tasks" / "SKILL.md").read_text(encoding="utf-8")

    # A wiped projection directory is restored on the next refresh.
    shutil.rmtree(home / "skills" / "cinna-tasks")
    skills_projection.refresh(workspace, home, platform)
    assert (home / "skills" / "cinna-tasks" / "SKILL.md").is_file()


def test_no_platform_tree_keeps_the_previous_behaviour(tmp_path: Path) -> None:
    workspace, home, _platform = _layout(tmp_path)
    result = skills_projection.refresh(workspace, home, tmp_path / "absent")
    assert result.projected == 0
    assert not (home / "skills").exists()


def test_platform_frontmatter_keys_are_accepted_and_kept() -> None:
    for manifest in (skill_manifest, host_manifest):
        entry = manifest.parse_skill_dir(
            _KIT_SKILLS / "cinna-tasks", source=manifest.PLATFORM_SKILL_SOURCE,
        )
        assert entry.is_valid, entry.error
        assert entry.version == "1"
        assert entry.user_invocable is False
        assert entry.frontmatter["metadata"] == {"cinna_protocol": "tasks/1"}
        assert entry.frontmatter["when_to_use"].startswith("The turn context lists a current task")
        assert entry.frontmatter["compatibility"].startswith("Cinna Desktop or a cinna-core")


# ── Fallback skills block ───────────────────────────────────────────────────


def test_fallback_skills_block_lists_the_platform_skill(tmp_path: Path) -> None:
    workspace, _home, platform = _layout(tmp_path)
    generator = PromptGenerator(str(workspace), supports_skills=False)
    generator.platform_skills_dir = platform

    section = generator._get_agent_skills_section()

    assert section is not None
    assert f"- **cinna-tasks** (`{platform / 'cinna-tasks' / 'SKILL.md'}`)" in section
    # Engines with native skills index it themselves: no block.
    native = PromptGenerator(str(workspace), supports_skills=True)
    native.platform_skills_dir = platform
    assert native._get_agent_skills_section() is None


# ── Protocol version announcements ──────────────────────────────────────────


def test_done_without_protocol_version_keeps_the_stored_version() -> None:
    import asyncio

    from app.services.sessions.message_service import MessageService

    environment = SimpleNamespace(task_protocol_version=1)

    class _FakeDb:
        commits = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def get(self, _model, _id):
            return environment

        def add(self, _obj):
            return None

        def commit(self):
            _FakeDb.commits += 1

    for event in ({"type": "done"}, {"type": "done", "protocol_version": None},
                  {"type": "done", "protocol_version": "garbage"}):
        asyncio.run(MessageService._record_task_protocol_version(event, uuid4(), _FakeDb))
        assert environment.task_protocol_version == 1
    assert _FakeDb.commits == 0

    asyncio.run(MessageService._record_task_protocol_version(
        {"type": "done", "protocol_version": 2}, uuid4(), _FakeDb,
    ))
    assert environment.task_protocol_version == 2


def test_protocol_counts_as_installed_only_when_the_schema_landed(tmp_path: Path) -> None:
    from app.services.environments.platform_protocol_files import (
        install_platform_protocol,
        protocol_installed,
    )

    templates = _BACKEND / "app" / "env-templates"
    installed_core = tmp_path / "installed" / "core"
    install_platform_protocol(installed_core, templates)
    assert protocol_installed(installed_core)

    # A missing kit snapshot is only logged by the installer: no schema, so the
    # lifecycle must not raise the environment's task_protocol_version.
    missing_core = tmp_path / "missing" / "core"
    assert install_platform_protocol(missing_core, tmp_path / "no-templates") == []
    assert not protocol_installed(missing_core)
