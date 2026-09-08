"""Unit tests: engine enablement for agent skills (plan Phase 1, §13 rows 3-4).

Projecting a skill into ``/root/.claude/skills`` only helps if the engine is
actually allowed to run it. That takes four separate pieces of wiring, one per
class below, and each one fails silently when it is missing — a skill that is
projected but not invocable looks exactly like a skill that is invocable and
never chosen by the model:

* ``sdk_manager`` refreshes the projection **before** the adapter dispatches, so
  a skill the agent wrote during message N is on disk for message N+1;
* Claude Code pre-allows the ``Skill`` tool — its ``can_use_tool`` denies by
  default, so anything not pre-allowed is refused with an "interactive approval
  is not available" message the model then apologises about;
* OpenCode disposes its memoized workspace instance, but only when the
  projection identity moved, and gets plugin ``skills/`` dirs through the
  config in the object shape its schema demands;
* the generated ``opencode.json`` says ``permission.skill = "allow"``, without
  which invoking a skill headlessly surfaces as a tools-approval prompt.

Parser, projection and the per-mode ``skills_changed`` latch are covered in
``tests/unit/test_skill_manifest.py``; projection path guards in
``tests/unit/test_skills_projection_guards.py``.

Run:
    docker compose exec backend python -m pytest \
        tests/unit/test_skills_engine_enablement.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from pathlib import Path

from core.server import skills_projection
from core.server.adapters.base import SDKConfig
from core.server.adapters.claude_code_sdk_adapter import ClaudeCodeAdapter
from core.server.adapters.opencode_sdk_adapter import OpenCodeAdapter
from core.server.agent_env_service import AgentEnvService
from core.server.prompt_generator import PromptGenerator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_skill(skills_root: Path, name: str, description: str = "Do the thing.") -> None:
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n",
        encoding="utf-8",
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    return workspace


def _config(workspace: Path, adapter_type: str, provider: str = "anthropic") -> SDKConfig:
    return SDKConfig(
        adapter_id=f"{adapter_type}/{provider}",
        adapter_type=adapter_type,
        provider=provider,
        workspace_dir=str(workspace),
    )


# ===========================================================================
# 1. A skill written during one message is on disk for the next
# ===========================================================================


class TestASkillReachesTheEngineOnTheNextMessage:
    """§13: "a skill added between two messages is listed in the next init
    message's ``skills``".

    The Claude CLI builds that ``init`` list by scanning ``~/.claude/skills``
    when its subprocess starts, which is inside the adapter call. So the
    testable half of the claim — the half this codebase owns — is that
    ``sdk_manager`` has already re-projected by the time the adapter runs. If
    the refresh happened after dispatch (or once per process instead of once
    per message), the CLI would scan a directory that does not yet hold the
    skill and the feature's headline flow (build a skill, then use it) would
    need a restart.
    """

    def _manager_over(self, monkeypatch, workspace: Path, home: Path):
        """An SDKManager whose projection writes into ``home``.

        ``refresh``'s ``home`` default is bound at definition time, so pointing
        the constant somewhere else would not work — the call has to be wrapped.
        """
        from core.server import sdk_manager as sdk_manager_module

        seen: list[tuple[str, bool, list[str]]] = []
        projected_root = home / "skills"

        class _RecordingAdapter:
            ADAPTER_TYPE = "fake"
            workspace_dir = str(workspace)

            async def send_message_stream(self, *, mode, skills_changed, **kwargs):
                visible = (
                    sorted(p.name for p in projected_root.iterdir() if p.is_dir())
                    if projected_root.is_dir()
                    else []
                )
                seen.append((mode, skills_changed, visible))
                return
                yield  # pragma: no cover — makes this an async generator

        real_refresh = skills_projection.refresh
        monkeypatch.setattr(
            sdk_manager_module.skills_projection,
            "refresh",
            lambda workspace_dir, *a, **k: real_refresh(workspace_dir, home),
        )

        manager = sdk_manager_module.SDKManager()
        monkeypatch.setattr(manager, "_get_adapter", lambda mode: _RecordingAdapter())
        return manager, seen

    async def _send(self, manager, mode="conversation"):
        async for _ in manager.send_message_stream(message="hi", mode=mode):
            pass

    def test_a_skill_written_between_two_messages_is_visible_to_the_second(
        self, tmp_path: Path, monkeypatch
    ):
        workspace = _workspace(tmp_path)
        home = tmp_path / "claude_home"
        home.mkdir()
        manager, seen = self._manager_over(monkeypatch, workspace, home)

        async def _run():
            await self._send(manager)
            # The building agent writes a skill during (or right after) the
            # first message — exactly the flow the feature exists for.
            _write_skill(workspace / "skills", "pdf-report")
            await self._send(manager)

        asyncio.run(_run())

        first_mode, first_changed, first_visible = seen[0]
        second_mode, second_changed, second_visible = seen[1]

        assert first_visible == []
        assert second_visible == ["pdf-report"]
        assert second_changed is True

        # The first message of a process always reports a change: the mode has
        # not been told about ANY identity yet, and an empty tree still hashes
        # to a real token. It costs nothing because OpenCode's dispose is also
        # gated on `not server_just_started` — see
        # TestOpenCodeDisposesOnlyWhenTheProjectionMoved below, which is the
        # test that makes this free rather than a rebuild on every cold start.
        assert first_changed is True

    def test_a_removed_skill_is_gone_for_the_next_message(
        self, tmp_path: Path, monkeypatch
    ):
        import shutil

        workspace = _workspace(tmp_path)
        home = tmp_path / "claude_home"
        home.mkdir()
        manager, seen = self._manager_over(monkeypatch, workspace, home)
        _write_skill(workspace / "skills", "pdf-report")
        _write_skill(workspace / "skills", "csv-import")

        async def _run():
            await self._send(manager)
            shutil.rmtree(workspace / "skills" / "csv-import")
            await self._send(manager)

        asyncio.run(_run())

        assert seen[0][2] == ["csv-import", "pdf-report"]
        assert seen[1][2] == ["pdf-report"]
        assert seen[1][1] is True


# ===========================================================================
# 2. Claude Code: the Skill tool is pre-allowed
# ===========================================================================


class _FakeOptions:
    """Stand-in for ``ClaudeAgentOptions`` — records whatever the adapter sets."""

    def __init__(self, **kwargs):
        self.mcp_servers = None
        self.plugins = None
        self.model = None
        self.system_prompt = None
        self.resume = None
        self.settings = None
        self.__dict__.update(kwargs)


class _FakeClient:
    """Stand-in for ``ClaudeSDKClient`` that completes with no messages.

    Yielding nothing from ``receive_messages`` lets the adapter's stream loop
    fall through to its ``finally`` and end the generator normally, so the test
    never has to abandon a half-consumed async generator.
    """

    captured: list[_FakeOptions] = []

    def __init__(self, options=None):
        type(self).captured.append(options)
        self.options = options

    async def connect(self):
        return None

    async def query(self, message):
        return None

    async def receive_messages(self):
        return
        yield  # pragma: no cover — makes this an async generator

    async def disconnect(self):
        return None


def _install_fake_claude_sdk(monkeypatch) -> type[_FakeClient]:
    """Put a minimal ``claude_agent_sdk`` on ``sys.modules``.

    The real SDK is only installed inside the agent container, so without this
    the adapter's ``from claude_agent_sdk import ...`` raises ImportError and it
    yields "Claude Code SDK is not installed" — a green-looking run that proves
    nothing about the allowed-tools list.
    """

    class _Client(_FakeClient):
        captured = []

    class _PermissionResultDeny:
        def __init__(self, message: str = "", interrupt: bool = False):
            self.message = message
            self.interrupt = interrupt

    module = types.ModuleType("claude_agent_sdk")
    module.ClaudeSDKClient = _Client
    module.ClaudeAgentOptions = _FakeOptions
    module.ResultMessage = type("ResultMessage", (), {})
    module.PermissionResultDeny = _PermissionResultDeny
    module.create_sdk_mcp_server = lambda **kwargs: object()
    module.tool = lambda *a, **k: (lambda fn: fn)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return _Client


class TestClaudeCodePreAllowsTheSkillTool:
    """``can_use_tool`` is a deny-by-default callback.

    Anything the CLI cannot resolve from ``allowed_tools`` lands there and comes
    back as "requires interactive approval, which is not available in this
    environment". So "``Skill`` is not denied" is not a property of the callback
    — it is a property of ``Skill`` being in the list the callback never sees.
    That is what these tests pin.
    """

    def _events(self, tmp_path: Path, monkeypatch) -> tuple[list, type[_FakeClient]]:
        workspace = _workspace(tmp_path)
        client_cls = _install_fake_claude_sdk(monkeypatch)
        adapter = ClaudeCodeAdapter(_config(workspace, "claude-code"))

        async def _run():
            collected = []
            async for event in adapter.send_message_stream(
                message="hello", mode="conversation"
            ):
                collected.append(event)
            return collected

        return asyncio.run(_run()), client_cls

    def test_skill_reaches_the_sdk_allowed_tools(self, tmp_path: Path, monkeypatch):
        events, client_cls = self._events(tmp_path, monkeypatch)

        assert client_cls.captured, (
            "the adapter never built an SDK client — the fake claude_agent_sdk "
            "was not used and this test proves nothing"
        )
        options = client_cls.captured[-1]
        assert "Skill" in options.allowed_tools

    def test_the_tools_init_event_advertises_skill(self, tmp_path: Path, monkeypatch):
        """The unified (lowercase) name is what the chat UI and the backend see."""
        events, _ = self._events(tmp_path, monkeypatch)

        tools_init = [
            event
            for event in events
            if getattr(event, "subtype", None) == "tools_init"
        ]
        assert tools_init, "no tools_init event was emitted"
        assert "skill" in tools_init[-1].data["tools"]

    def test_the_deny_by_default_callback_is_why_pre_allowing_matters(
        self, tmp_path: Path, monkeypatch
    ):
        """A tool that is NOT pre-allowed is refused outright.

        Without this the first test above reads as a redundant assertion about a
        list; with it, the list is demonstrably the only thing standing between
        a projected skill and a refusal.
        """
        events, client_cls = self._events(tmp_path, monkeypatch)
        options = client_cls.captured[-1]

        async def _ask(tool_name):
            return await options.can_use_tool(tool_name, {}, None)

        denied = asyncio.run(_ask("ExitPlanMode"))
        assert "interactive approval" in denied.message
        assert "ExitPlanMode" not in options.allowed_tools

    def test_claude_code_declares_native_skill_support(self):
        """SUPPORTS_SKILLS drives the prompt generator's fallback block."""
        assert ClaudeCodeAdapter.SUPPORTS_SKILLS is True
        assert OpenCodeAdapter.SUPPORTS_SKILLS is True


# ===========================================================================
# 3. OpenCode: dispose only on a change; plugin skills reach the config
# ===========================================================================


class _StopSession(Exception):
    """Sentinel raised from a stubbed ``_create_session`` to end the stream."""


class TestOpenCodeDisposesOnlyWhenTheProjectionMoved:
    """Disposing throws away the warm workspace instance.

    Doing it on every message would cost the rebuild on every turn; never doing
    it leaves a freshly projected skill invisible until the next server start.
    The gate is ``skills_changed and not server_just_started``.
    """

    def _drive(self, tmp_path: Path, monkeypatch, *, skills_changed, just_started):
        workspace = _workspace(tmp_path)
        adapter = OpenCodeAdapter(_config(workspace, "opencode"))

        disposed: list[str] = []

        async def _ensure(*args, **kwargs):
            return just_started

        async def _dispose():
            disposed.append(adapter._mode or "?")

        async def _create_session():
            raise _StopSession()

        monkeypatch.setattr(adapter, "_ensure_server_running", _ensure)
        monkeypatch.setattr(adapter, "_dispose_instance", _dispose)
        monkeypatch.setattr(adapter, "_create_session", _create_session)

        async def _run():
            try:
                async for _ in adapter.send_message_stream(
                    message="hi", mode="conversation", skills_changed=skills_changed
                ):
                    pass
            except _StopSession:
                pass

        asyncio.run(_run())
        return disposed

    def test_no_change_means_no_dispose(self, tmp_path: Path, monkeypatch):
        assert (
            self._drive(tmp_path, monkeypatch, skills_changed=False, just_started=False)
            == []
        )

    def test_a_change_disposes_the_workspace_instance(
        self, tmp_path: Path, monkeypatch
    ):
        assert (
            self._drive(tmp_path, monkeypatch, skills_changed=True, just_started=False)
            == ["conversation"]
        )

    def test_a_just_started_server_is_not_disposed(self, tmp_path: Path, monkeypatch):
        """A fresh process already read the current projection.

        This is the case that makes the very first message of a container cheap:
        the projection is always "changed" for a mode that has not been told
        about it yet, and the server it would dispose was launched microseconds
        earlier from that same projection.
        """
        assert (
            self._drive(tmp_path, monkeypatch, skills_changed=True, just_started=True)
            == []
        )


class TestOpenCodePluginSkillsReachTheConfig:
    """Merge behaviour of the ``skills`` section in the materialised config.

    That the section exists at all, and that it is an OBJECT
    (``{"paths": [...]}``) rather than an array, is covered by
    ``tests/unit/test_opencode_mcp_bridge.py::TestOpenCodeConfigMaterialization``.
    What is not covered there is what happens when the section is contested:
    a base template that already declares ``skills``, and a plugin set that
    declares none. Both matter because ``OPENCODE_CONFIG`` pins this file — a
    clobbered or malformed section does not degrade to "plugin skills missing",
    it can stop the server starting at all.
    """

    def _adapter_with_plugin(
        self, tmp_path: Path, *, with_skills: bool, base_config: dict | None = None
    ) -> tuple[OpenCodeAdapter, Path]:
        workspace = _workspace(tmp_path)
        plugin_dir = workspace / "plugins" / "acme" / "reporter"
        plugin_dir.mkdir(parents=True)
        if with_skills:
            _write_skill(plugin_dir / "skills", "invoice-parser")
        (workspace / "plugins" / "settings.json").write_text(
            json.dumps(
                {
                    "active_plugins": [
                        {
                            "path": str(plugin_dir),
                            "marketplace_name": "acme",
                            "plugin_name": "reporter",
                            "conversation_mode": True,
                            "building_mode": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        mode_config_dir = tmp_path / "mode_config"
        mode_config_dir.mkdir()
        (mode_config_dir / "opencode.json").write_text(
            json.dumps(base_config if base_config is not None else {"model": "sonnet"}),
            encoding="utf-8",
        )

        adapter = OpenCodeAdapter(_config(workspace, "opencode"))
        adapter._mode = "conversation"
        adapter._runtime_dir = tmp_path / "runtime"
        adapter._runtime_dir.mkdir()
        return adapter, mode_config_dir

    def _materialized(self, adapter: OpenCodeAdapter, mode_config_dir: Path) -> dict:
        adapter._materialize_opencode_config(mode_config_dir)
        return json.loads(
            (adapter._runtime_dir / "opencode.json").read_text(encoding="utf-8")
        )

    def test_base_template_paths_are_kept_and_deduped(self, tmp_path: Path):
        """A hand-tuned template keeps its entries; ours are appended once."""
        plugin_skills = str(
            tmp_path / "workspace" / "plugins" / "acme" / "reporter" / "skills"
        )
        adapter, mode_config_dir = self._adapter_with_plugin(
            tmp_path,
            with_skills=True,
            base_config={
                "model": "sonnet",
                "skills": {"paths": ["/opt/house-skills", plugin_skills], "urls": []},
            },
        )

        config = self._materialized(adapter, mode_config_dir)

        assert config["skills"]["paths"] == ["/opt/house-skills", plugin_skills]
        # Sibling keys the template declared survive the merge.
        assert config["skills"]["urls"] == []

    def test_a_plugin_without_skills_leaves_the_section_alone(self, tmp_path: Path):
        """No plugin skills means no `skills` key at all.

        The agent's OWN skills are deliberately absent too: they are projected
        into ``/root/.claude/skills``, which OpenCode already reads, and listing
        them here as well would show every skill twice in the index.
        """
        adapter, mode_config_dir = self._adapter_with_plugin(tmp_path, with_skills=False)

        config = self._materialized(adapter, mode_config_dir)

        assert "skills" not in config


class TestPluginSkillsAreNoLongerUnsupportedUnderOpenCode:
    """``skills`` left ``_OPENCODE_UNSUPPORTED_DIRS``; ``agents``/``hooks`` did not.

    The unsupported list drives a SYSTEM event shown to the owner at session
    start ("... was skipped"). Leaving ``skills`` on it after wiring the config
    path would tell users a working capability is broken.

    The single-capability cases live in
    ``tests/unit/test_opencode_mcp_bridge.py::TestOpenCodePluginArtifacts``.
    Here: the constant itself, and the mixed tree — a plugin shipping skills
    AND an unsupported capability, where a sloppy "any capability dir means
    report it" would drag `skills` back into the report.
    """

    def _artifacts(self, tmp_path: Path, capability_dirs: tuple[str, ...]) -> dict:
        workspace = _workspace(tmp_path)
        plugin_dir = workspace / "plugins" / "acme" / "reporter"
        plugin_dir.mkdir(parents=True)
        for capability in capability_dirs:
            if capability == "skills":
                _write_skill(plugin_dir / "skills", "invoice-parser")
            else:
                (plugin_dir / capability).mkdir()
                (plugin_dir / capability / "thing.md").write_text("x\n", encoding="utf-8")
        (workspace / "plugins" / "settings.json").write_text(
            json.dumps(
                {
                    "active_plugins": [
                        {
                            "path": str(plugin_dir),
                            "marketplace_name": "acme",
                            "plugin_name": "reporter",
                            "conversation_mode": True,
                            "building_mode": False,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return AgentEnvService(str(workspace)).get_opencode_plugin_artifacts(
            "conversation"
        )

    def test_skills_is_not_in_the_unsupported_dirs(self):
        assert "skills" not in AgentEnvService._OPENCODE_UNSUPPORTED_DIRS
        assert set(AgentEnvService._OPENCODE_UNSUPPORTED_DIRS) == {"agents", "hooks"}

    def test_agents_and_hooks_are_still_reported_alongside_skills(
        self, tmp_path: Path
    ):
        artifacts = self._artifacts(tmp_path, ("skills", "agents", "hooks"))

        assert len(artifacts["skill_dirs"]) == 1
        assert sorted(r["capability"] for r in artifacts["unsupported"]) == [
            "agents",
            "hooks",
        ]


class _FakeProcess:
    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):  # pragma: no cover — only on the timeout branch
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class TestOpenCodeServerStop:
    """``routes.install_plugins`` stops the server so the next message relaunches.

    The plugin-derived config (MCP servers, the ``skills`` paths above, plugin
    commands) is materialized once at start, so a plugin install is invisible
    until the process is replaced.
    """

    def test_stopping_a_live_server_clears_the_session_binding(
        self, tmp_path: Path
    ):
        adapter = OpenCodeAdapter(_config(_workspace(tmp_path), "opencode"))
        adapter._mode = "conversation"
        process = _FakeProcess()
        adapter._server_process = process
        adapter._current_session_id = "ses_old"

        assert asyncio.run(adapter.stop()) is True
        assert process.terminated is True
        assert adapter._server_process is None
        # The sessions belonged to the dead process — resuming one against the
        # replacement server would fail with "no conversation found".
        assert adapter._current_session_id is None

    def test_stopping_when_nothing_runs_is_a_no_op(self, tmp_path: Path):
        adapter = OpenCodeAdapter(_config(_workspace(tmp_path), "opencode"))
        adapter._mode = "conversation"

        assert asyncio.run(adapter.stop()) is False

    def test_stopping_an_already_dead_process_reports_false(self, tmp_path: Path):
        adapter = OpenCodeAdapter(_config(_workspace(tmp_path), "opencode"))
        adapter._mode = "conversation"
        process = _FakeProcess()
        process.returncode = 0
        adapter._server_process = process

        assert asyncio.run(adapter.stop()) is False
        assert adapter._server_process is None
        assert process.terminated is False


# ===========================================================================
# 4. The generated opencode.json permits the skill tool
# ===========================================================================


class TestGeneratedOpenCodeConfigAllowsSkill:
    """Without ``permission.skill = "allow"`` OpenCode asks for approval.

    Headless, that ask surfaces as the tools-approval flow instead of the skill
    running — the model appears to stall on its own skill.
    """

    def _generate(self, tmp_path: Path, sdk: str) -> Path:
        from app.models import AgentEnvironment
        from app.services.environments.environment_lifecycle import (
            EnvironmentLifecycleManager,
        )

        environment = AgentEnvironment(
            id=uuid.uuid4(),
            agent_sdk_building=sdk,
            agent_sdk_conversation=sdk,
        )
        EnvironmentLifecycleManager()._generate_opencode_config_files(
            tmp_path, environment, anthropic_api_key="sk-test"
        )
        return tmp_path / "app" / "core" / ".opencode"

    def test_both_modes_allow_the_skill_tool(self, tmp_path: Path):
        opencode_dir = self._generate(tmp_path, "opencode/anthropic")

        for mode in ("building", "conversation"):
            config = json.loads(
                (opencode_dir / mode / "opencode.json").read_text(encoding="utf-8")
            )
            assert config["permission"]["skill"] == "allow", mode
            # The wildcard is not enough on its own — opencode resolves the
            # specific key first, and a missing one falls back to asking.
            assert config["permission"]["*"] == "allow", mode

    def test_a_claude_code_environment_generates_no_opencode_config(
        self, tmp_path: Path
    ):
        opencode_dir = self._generate(tmp_path, "claude-code/anthropic")

        assert not (opencode_dir / "conversation").exists()
        assert not (opencode_dir / "building").exists()


# ===========================================================================
# 5. Prompt fallback for a future engine with no native skill index
# ===========================================================================


class TestPromptFallbackForAnEngineWithoutSkills:
    """Both shipped engines index skills natively, so the block costs nothing.

    It exists so a future adapter can opt out without the feature going dark —
    but it is the degraded path (every name and description on every turn, and
    no progressive disclosure), so the default must stay silent.
    """

    def test_a_native_engine_gets_no_skills_block(self, tmp_path: Path):
        workspace = _workspace(tmp_path)
        _write_skill(workspace / "skills", "pdf-report")

        generator = PromptGenerator(str(workspace), supports_skills=True)

        assert generator._get_agent_skills_section() is None

    def test_an_opted_out_engine_gets_names_and_descriptions(self, tmp_path: Path):
        workspace = _workspace(tmp_path)
        _write_skill(workspace / "skills", "pdf-report", "Build a PDF from a CSV.")
        _write_skill(workspace / "skills", "broken")
        (workspace / "skills" / "broken" / "SKILL.md").write_text(
            "---\nname: not-broken\ndescription: Mismatched.\n---\n\nBody.\n",
            encoding="utf-8",
        )

        section = PromptGenerator(
            str(workspace), supports_skills=False
        )._get_agent_skills_section()

        assert section is not None
        assert "pdf-report" in section
        assert "Build a PDF from a CSV." in section
        # An invalid skill is excluded from the projection, so advertising it in
        # the prompt would point the model at something it cannot invoke.
        assert "not-broken" not in section

    def test_an_agent_with_no_skills_gets_no_block_either(self, tmp_path: Path):
        workspace = _workspace(tmp_path)

        assert (
            PromptGenerator(str(workspace), supports_skills=False)
            ._get_agent_skills_section()
            is None
        )
