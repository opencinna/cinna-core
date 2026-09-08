"""``/skills`` and the dynamic ``/<skill>`` entries in the command popup.

Scenarios:
  1. ``/skills`` renders the cached index as a document table — sources, invoke
     cells, flagged rows last, and a description that carries a ``|`` without
     breaking the table.
  2. Empty states: an agent with no skills gets the "what a skill is" copy (not
     an error), and a container built before agent skills existed gets the
     "rebuild the environment" copy (which IS an error).
  3. The popup: ``/skills`` is a registered command, each valid user-invocable
     skill is appended as ``/<name>`` with ``kind="skill"``, an invalid skill is
     not offered, and a duplicated name appears once.
  4. ``/<skill>`` is deliberately NOT a handler: it fails ``is_command()`` and
     the whole line reaches the model verbatim.

Notes:
  The ``/skills`` handler reads the cache written by the env-start sweep, so
  each scenario seeds the stub adapter's ``skills_index`` before creating the
  agent. Unit coverage of the cache rows themselves lives in
  ``tests/unit/test_agent_skills_service.py``; the agent-page routes over the
  same cache are covered in
  ``tests/api/agents/core/agents_skills_routes_test.py``.
"""
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role, list_messages, send_message
from tests.utils.session import create_session_via_api

API = settings.API_V1_STR


# ── Helpers ────────────────────────────────────────────────────────────────


def _row(name: str, **overrides: Any) -> dict:
    entry = {
        "name": name,
        "description": f"Does {name} things.",
        "source": "local",
        "plugin_ref": None,
        "path": f"skills/{name}",
        "has_scripts": False,
        "user_invocable": True,
        "model_invocable": True,
        "size_bytes": 256,
        "error": None,
        "warning": None,
        "secret_paths": [],
    }
    entry.update(overrides)
    return entry


def _index(rows: list[dict], tree_hash: str = "hash-1") -> dict:
    return {"hash": tree_hash, "skills": rows, "errors": []}


def _agent_with_skills(
    client: TestClient,
    headers: dict[str, str],
    lifecycle_manager,
    rows: list[dict],
    *,
    name: str = "Skills-Command",
    fail_index: bool = False,
) -> tuple[dict, str]:
    """Create an agent whose env reports ``rows``; return (agent, session_id)."""
    adapter = EnvironmentTestAdapter()
    adapter.skills_index = _index(rows)
    if fail_index:
        async def _no_endpoint():
            raise RuntimeError("404 Not Found: /config/skills")

        adapter.get_skills_index = _no_endpoint
    lifecycle_manager.get_adapter = lambda env: adapter

    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    session = create_session_via_api(client, headers, agent["id"])
    return agent, session["id"]


def _command_messages(client, headers, session_id) -> list[dict]:
    return [
        m
        for m in list_messages(client, headers, session_id)
        if m["role"] == "system"
        and (m.get("message_metadata") or {}).get("command") is True
    ]


def _session_commands(client, headers, session_id) -> list[dict]:
    r = client.get(f"{API}/sessions/{session_id}/commands", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["commands"]


# ── Scenario 1: the table ──────────────────────────────────────────────────


def test_skills_command_renders_the_cached_index_as_a_document(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    ``/skills`` answers from the cache, with no LLM call:
      1. A local skill, a plugin skill, a model-only one and a broken one.
      2. The response is a markdown table marked ``display="document"``.
      3. Each row says where the skill came from and how to invoke it.
      4. The flagged row sorts last and leads with the problem.
      5. A ``|`` inside a description is escaped, so the table cannot lie about
         which skill does what.
      6. ``/skills`` with arguments is refused.
    """
    # ── Phase 1: the cache ───────────────────────────────────────────────
    agent, session_id = _agent_with_skills(
        client,
        superuser_token_headers,
        patch_environment_adapter,
        [
            _row("pdf-report", description="Build a PDF | fast"),
            _row(
                "from-plugin",
                source="plugin",
                plugin_ref="mkt/reporting",
                path="plugins/mkt/reporting/skills/from-plugin",
            ),
            _row("model-only", user_invocable=False),
            _row(
                "broken",
                error={
                    "code": "reserved_name",
                    "message": "This name is reserved by a platform command.",
                    "paths": [],
                },
            ),
        ],
    )

    stub = StubAgentEnvConnector(response_text="should not be called")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        # ── Phase 2: run the command ─────────────────────────────────────
        result = send_message(
            client, superuser_token_headers, session_id, content="/skills"
        )
        drain_tasks()
        assert result.get("command_executed") is True
        assert len(stub.stream_calls) == 0, "/skills must never reach the LLM"

    messages = _command_messages(client, superuser_token_headers, session_id)
    assert len(messages) == 1
    body = messages[0]["content"]
    metadata = messages[0]["message_metadata"]
    assert metadata["command_name"] == "/skills"
    assert metadata["command_display"] == "document"
    assert messages[0]["status"] != "error"

    # ── Phase 3: one row per skill, with source and invocation ───────────
    assert "| Skill | Source | What it does | Invoke |" in body
    assert "`pdf-report`" in body
    assert "plugin: mkt/reporting" in body
    assert "`/pdf-report`" in body, "a user-invocable skill shows how to call it"
    assert "model only" in body, "a model-only skill says so instead"
    assert "unavailable" in body, "a broken skill cannot be invoked at all"

    # ── Phase 4: the flagged row is last ─────────────────────────────────
    lines = [line for line in body.splitlines() if line.startswith("| `")]
    assert lines[-1].startswith("| `broken`"), (
        f"a broken skill is not an answer to 'what can this agent do': {lines}"
    )
    assert "reserved by a platform command" in lines[-1], (
        "the row leads with the problem, not with the description"
    )

    # ── Phase 5: the pipe is escaped ─────────────────────────────────────
    pdf_line = next(line for line in lines if line.startswith("| `pdf-report`"))
    assert "Build a PDF \\| fast" in pdf_line
    assert pdf_line.count("|") - pdf_line.count("\\|") == 5, (
        f"an unescaped pipe would shift every later column: {pdf_line!r}"
    )

    # ── Phase 6: arguments are refused ───────────────────────────────────
    send_message(
        client, superuser_token_headers, session_id, content="/skills pdf-report"
    )
    drain_tasks()
    refusal = _command_messages(client, superuser_token_headers, session_id)[-1]
    assert "takes no arguments" in refusal["content"]
    assert refusal["status"] == "error"


# ── Scenario 2: the two empty states ───────────────────────────────────────


def test_skills_command_empty_states_tell_the_user_which_one_they_are_in(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    "No skills" and "this container cannot report skills" need different
    actions from the user, so they are different answers:
      1. An agent with an empty index → the explanatory copy, NOT an error.
      2. An agent whose env has no ``/config/skills`` (a container built before
         the feature) → the rebuild copy, marked as an error.
    """
    # ── Phase 1: genuinely no skills ─────────────────────────────────────
    _, session_id = _agent_with_skills(
        client, superuser_token_headers, patch_environment_adapter, [],
        name="No-Skills",
    )
    send_message(client, superuser_token_headers, session_id, content="/skills")
    drain_tasks()

    message = _command_messages(client, superuser_token_headers, session_id)[-1]
    assert "no skills yet" in message["content"]
    assert "skills/" in message["content"], "the copy says how to add one"
    assert message["status"] != "error", (
        "an agent with no skills is an empty state, not a breakage"
    )

    # ── Phase 2: a pre-feature container ─────────────────────────────────
    _, stale_session_id = _agent_with_skills(
        client,
        superuser_token_headers,
        patch_environment_adapter,
        [],
        name="Pre-Feature",
        fail_index=True,
    )
    send_message(client, superuser_token_headers, stale_session_id, content="/skills")
    drain_tasks()

    message = _command_messages(
        client, superuser_token_headers, stale_session_id
    )[-1]
    assert "Rebuild the environment" in message["content"]
    assert message["status"] == "error"


# ── Scenario 3: the popup ──────────────────────────────────────────────────


def test_the_popup_lists_skills_as_their_own_kind(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    ``GET /sessions/{id}/commands`` drives the slash popup:
      1. ``/skills`` is there as an ordinary registered command.
      2. Each valid, user-invocable skill is appended as ``/<name>`` with
         ``kind="skill"`` and its description as the tooltip.
      3. An invalid skill is not offered — the engine would not run it.
      4. A model-only skill is not offered either — the user cannot invoke it.
      5. A name reported by two sources (the ``shadowed`` case) is listed once.
      6. Registered commands are not mislabelled as skills.
    """
    _, session_id = _agent_with_skills(
        client,
        superuser_token_headers,
        patch_environment_adapter,
        [
            _row("pdf-report", description="Build a PDF report."),
            _row(
                "pdf-report",
                source="plugin",
                plugin_ref="mkt/reporting",
                description="A plugin's copy of the same name.",
                warning={
                    "code": "shadowed",
                    "message": "Another installed skill uses this name.",
                    "paths": [],
                },
            ),
            _row("model-only", user_invocable=False),
            _row(
                "invalid",
                error={
                    "code": "missing_description",
                    "message": "SKILL.md frontmatter has no description.",
                    "paths": [],
                },
            ),
        ],
        name="Popup-Skills",
    )

    commands = _session_commands(client, superuser_token_headers, session_id)
    by_name = {c["name"]: c for c in commands}

    # ── Phase 1: the handler ─────────────────────────────────────────────
    assert "/skills" in by_name
    assert by_name["/skills"].get("kind") != "skill", (
        "/skills is a platform command, not a skill entry"
    )

    # ── Phase 2: the skill entry ─────────────────────────────────────────
    assert "/pdf-report" in by_name
    entry = by_name["/pdf-report"]
    assert entry["kind"] == "skill"
    assert entry["is_available"] is True
    assert entry["description"] == "Build a PDF report.", (
        "the local copy wins the name, and its description is the tooltip"
    )

    # ── Phases 3+4: what is deliberately absent ──────────────────────────
    assert "/invalid" not in by_name, (
        "offering a skill the engine will not project promises nothing"
    )
    assert "/model-only" not in by_name

    # ── Phase 5: one row per name ────────────────────────────────────────
    assert [c["name"] for c in commands].count("/pdf-report") == 1, (
        "typing the name can only mean one thing to the user"
    )

    # ── Phase 6: nothing else claims the kind ────────────────────────────
    skill_entries = [c["name"] for c in commands if c.get("kind") == "skill"]
    assert skill_entries == ["/pdf-report"]


# ── Scenario 4: /<skill> is not a command ──────────────────────────────────


def test_an_unregistered_skill_slash_passes_through_to_the_model(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    The popup offers ``/pdf-report``, but nothing registers it as a handler: the
    line has to reach the model verbatim, because that is what makes Claude Code
    treat it as an explicit skill invocation.

    Phases:
      1. Agent with a ``pdf-report`` skill, listed in the popup.
      2. Send ``/pdf-report for Q3`` → no command executed, the LLM is called,
         and the user message keeps the leading slash.
      3. A registered command in the same session still short-circuits, so the
         difference is about registration and not about the leading slash.
    """
    # ── Phase 1: the skill is offered ────────────────────────────────────
    _, session_id = _agent_with_skills(
        client,
        superuser_token_headers,
        patch_environment_adapter,
        [_row("pdf-report")],
        name="Passthrough",
    )
    assert "/pdf-report" in {
        c["name"] for c in _session_commands(
            client, superuser_token_headers, session_id
        )
    }

    stub = StubAgentEnvConnector(response_text="Running the pdf-report skill.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        # ── Phase 2: it goes to the model ────────────────────────────────
        result = send_message(
            client,
            superuser_token_headers,
            session_id,
            content="/pdf-report for Q3",
        )
        drain_tasks()
        assert result.get("command_executed") is not True
        assert len(stub.stream_calls) == 1, (
            "an unregistered /<skill> must reach the LLM, not a handler"
        )

        user_messages = get_messages_by_role(
            client, superuser_token_headers, session_id, "user"
        )
        assert user_messages[-1]["content"] == "/pdf-report for Q3", (
            "the slash travels verbatim — the engine reads it as the invocation"
        )
        agent_messages = get_messages_by_role(
            client, superuser_token_headers, session_id, "agent"
        )
        assert "pdf-report skill" in agent_messages[-1]["content"]
        assert _command_messages(client, superuser_token_headers, session_id) == []

        # ── Phase 3: a registered command still short-circuits ───────────
        result = send_message(
            client, superuser_token_headers, session_id, content="/skills"
        )
        drain_tasks()
        assert result.get("command_executed") is True
        assert len(stub.stream_calls) == 1, "still one — /skills added no call"
