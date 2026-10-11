"""
Integration tests: agent description auto-generation is opt-in.

A workflow-prompt change (a UI edit via PUT, or the env→DB pull of the first
prompt sync) regenerates the agent's description with a billed model call
only when the agent opted in through ``AgentCreate.auto_generate_description``.

Scenarios
---------
1. API-created agent without the flag: neither the first prompt sync nor a
   later workflow edit calls the generator; the description stays as created.
2. API-created agent with ``auto_generate_description=true``: the first prompt
   sync and later workflow edits call the generator.
3. Caller supplies a description *and* the flag: the flag is stored off, the
   description is never rewritten.
4. Owner edits the description of an opted-in agent: the opt-in is cleared;
   later workflow changes (also one sent together with the description in a
   single PUT) no longer regenerate it.
5. POST /agents/create-flow opts the agent in.

Seam: the generation runs in a daemon thread (``_regenerate_in_background``)
that calls ``_generate_description_background``, which opens its own
``Session(engine)`` — outside the test transaction, so it cannot see the test
agent. The tests therefore patch ``_generate_description_background`` and
assert on its calls; the flag itself is not exposed on ``AgentPublic``. Each
check joins the regeneration thread first, so a "not called" assertion is not
racing the thread.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models.environments.environment import AgentEnvironment, AgentEnvironmentCreate
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import get_agent, update_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import send_message
from tests.utils.session import create_session_via_api
from tests.utils.utils import random_lower_string


API = settings.API_V1_STR

_GENERATOR_TARGET = "app.services.agents.agent_service._generate_description_background"

_WORKFLOW_FROM_ENV = "## Workflow (template default)\nStep 1: greet.\nStep 2: help."
_WORKFLOW_UI_EDIT = "## Workflow (UI edit)\nStep 1: collect data.\nStep 2: report."
_WORKFLOW_UI_EDIT_2 = "## Workflow (UI edit 2)\nStep 1: collect data.\nStep 2: email."


# ── Local helpers ─────────────────────────────────────────────────────────────


def _join_regeneration_threads(timeout: float = 10.0) -> None:
    """Wait for every in-flight ``_regenerate_in_background`` thread to finish."""
    for thread in threading.enumerate():
        if thread is threading.current_thread():
            continue
        target = getattr(thread, "_target", None)
        if getattr(target, "__name__", "") == "_regenerate_in_background":
            thread.join(timeout)
            assert not thread.is_alive(), "description regeneration thread did not finish"


@contextmanager
def _description_generator():
    """Patch the description generator with a recorder; join threads on exit."""
    generator = MagicMock(name="generate_description_background")
    with patch(_GENERATOR_TARGET, generator):
        try:
            yield generator
        finally:
            _join_regeneration_threads()


def _me_id(client: TestClient, headers: dict[str, str]) -> str:
    r = client.get(f"{API}/users/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _create_agent(
    client: TestClient,
    headers: dict[str, str],
    **fields,
) -> dict:
    body = {"name": f"Desc Agent {random_lower_string()[:8]}", **fields}
    r = client.post(f"{API}/agents/", headers=headers, json=body)
    assert r.status_code == 200, f"Agent creation failed: {r.text}"
    drain_tasks()
    return r.json()


def _first_prompt_sync(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    adapter: EnvironmentTestAdapter,
) -> None:
    """Env holds a template workflow prompt; a building turn reconciles it into DB."""
    adapter.prompts_set["workflow_prompt"] = _WORKFLOW_FROM_ENV
    adapter.prompt_mtimes["workflow_prompt"] = time.time()

    session_data = create_session_via_api(client, headers, agent_id, mode="building")
    stub = StubAgentEnvConnector(response_text="done")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, headers, session_data["id"], content="build it")
        drain_tasks()

    # The sync really happened — otherwise a "not called" assertion is vacuous.
    assert get_agent(client, headers, agent_id)["workflow_prompt"] == _WORKFLOW_FROM_ENV


def _assert_generated_for(
    generator: MagicMock, agent: dict, workflow_prompt: str, user_id: str
) -> None:
    generator.assert_called_once()
    args, kwargs = generator.call_args
    assert str(args[0]) == agent["id"]
    assert args[1] == workflow_prompt
    assert args[2] == agent["name"]
    assert str(kwargs.get("user_id")) == user_id


class _StubCreateEnvironment:
    """Creates a ready environment row so the create-flow polling loop exits."""

    async def __call__(
        self,
        session,
        agent_id,
        data: AgentEnvironmentCreate,
        user,
        auto_start: bool = False,
        source_environment_id=None,
    ) -> AgentEnvironment:
        environment = AgentEnvironment(
            agent_id=agent_id,
            env_name=data.env_name,
            env_version=data.env_version,
            instance_name=data.instance_name or "stub-instance",
            type=data.type,
            config=data.config if data.config is not None else {},
            agent_sdk_conversation=data.agent_sdk_conversation,
            agent_sdk_building=data.agent_sdk_building,
            status="running",
            is_active=True,
        )
        session.add(environment)
        session.commit()
        session.refresh(environment)
        return environment


# ── Scenario 1: no opt-in → never generated ──────────────────────────────────


def test_api_agent_without_opt_in_never_generates_description(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. POST /agents/ with neither workflow_prompt nor auto_generate_description.
    2. First prompt sync pulls the template workflow prompt into DB.
    3. Generator not called; description still None.
    4. A later UI workflow edit does not call it either.
    """
    adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: adapter

    agent = _create_agent(client, superuser_token_headers)
    assert agent["description"] is None
    assert not agent.get("workflow_prompt")

    with _description_generator() as generator:
        _first_prompt_sync(client, superuser_token_headers, agent["id"], adapter)
    generator.assert_not_called()
    assert get_agent(client, superuser_token_headers, agent["id"])["description"] is None

    with _description_generator() as generator:
        updated = update_agent(
            client, superuser_token_headers, agent["id"],
            workflow_prompt=_WORKFLOW_UI_EDIT,
        )
        drain_tasks()
    generator.assert_not_called()
    assert updated["workflow_prompt"] == _WORKFLOW_UI_EDIT
    assert get_agent(client, superuser_token_headers, agent["id"])["description"] is None


# ── Scenario 2: opt-in → generated on sync and on edits ──────────────────────


def test_api_agent_with_opt_in_generates_description_on_first_sync_and_edits(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. POST /agents/ with auto_generate_description=true, no description.
    2. First prompt sync → generator called with the pulled prompt + owner id.
    3. An unrelated PUT (name) keeps the opt-in; next workflow edit → called again.
    """
    adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: adapter
    me = _me_id(client, superuser_token_headers)

    agent = _create_agent(client, superuser_token_headers, auto_generate_description=True)
    assert agent["description"] is None

    with _description_generator() as generator:
        _first_prompt_sync(client, superuser_token_headers, agent["id"], adapter)
    _assert_generated_for(generator, agent, _WORKFLOW_FROM_ENV, me)

    renamed = update_agent(
        client, superuser_token_headers, agent["id"], name=f"{agent['name']} v2"
    )

    with _description_generator() as generator:
        update_agent(
            client, superuser_token_headers, agent["id"],
            workflow_prompt=_WORKFLOW_UI_EDIT,
        )
        drain_tasks()
    _assert_generated_for(generator, renamed, _WORKFLOW_UI_EDIT, me)


def test_unchanged_workflow_prompt_put_does_not_generate(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """PUT re-sending the same workflow prompt to an opted-in agent → no call."""
    agent = _create_agent(client, superuser_token_headers, auto_generate_description=True)
    with _description_generator() as generator:
        update_agent(
            client, superuser_token_headers, agent["id"],
            workflow_prompt=_WORKFLOW_UI_EDIT,
        )
    assert generator.call_count == 1

    with _description_generator() as generator:
        update_agent(
            client, superuser_token_headers, agent["id"],
            workflow_prompt=_WORKFLOW_UI_EDIT,
        )
    generator.assert_not_called()


# ── Scenario 3: caller-supplied description wins over the flag ───────────────


def test_create_with_description_and_opt_in_never_rewrites_description(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. POST /agents/ with description + auto_generate_description=true.
    2. First prompt sync and a later workflow edit → generator never called.
    3. The caller's description is preserved verbatim.
    """
    adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: adapter
    description = "Summarises weekly sales and emails the team."

    agent = _create_agent(
        client, superuser_token_headers,
        description=description, auto_generate_description=True,
    )
    assert agent["description"] == description

    with _description_generator() as generator:
        _first_prompt_sync(client, superuser_token_headers, agent["id"], adapter)
        update_agent(
            client, superuser_token_headers, agent["id"],
            workflow_prompt=_WORKFLOW_UI_EDIT,
        )
        drain_tasks()
    generator.assert_not_called()
    assert get_agent(client, superuser_token_headers, agent["id"])["description"] == description


# ── Scenario 4: owner edits the description → opt-in cleared ─────────────────


def test_owner_description_edit_stops_regeneration(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    1. Opted-in agent; a workflow edit generates (baseline).
    2. Owner PUTs a description → later workflow edit does not regenerate.
    3. The owner's description is kept.
    """
    me = _me_id(client, superuser_token_headers)
    agent = _create_agent(client, superuser_token_headers, auto_generate_description=True)

    with _description_generator() as generator:
        update_agent(
            client, superuser_token_headers, agent["id"],
            workflow_prompt=_WORKFLOW_UI_EDIT,
        )
    _assert_generated_for(generator, agent, _WORKFLOW_UI_EDIT, me)

    owner_description = "Owner-written: triages support tickets."
    updated = update_agent(
        client, superuser_token_headers, agent["id"], description=owner_description
    )
    assert updated["description"] == owner_description

    with _description_generator() as generator:
        update_agent(
            client, superuser_token_headers, agent["id"],
            workflow_prompt=_WORKFLOW_UI_EDIT_2,
        )
        drain_tasks()
    generator.assert_not_called()
    after = get_agent(client, superuser_token_headers, agent["id"])
    assert after["description"] == owner_description
    assert after["workflow_prompt"] == _WORKFLOW_UI_EDIT_2


def test_put_with_description_and_workflow_together_keeps_sent_description(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Opted-in agent; one PUT changing both fields → no generation, description kept."""
    agent = _create_agent(client, superuser_token_headers, auto_generate_description=True)
    owner_description = "Owner-written alongside a workflow edit."

    with _description_generator() as generator:
        updated = update_agent(
            client, superuser_token_headers, agent["id"],
            description=owner_description,
            workflow_prompt=_WORKFLOW_UI_EDIT,
        )
        drain_tasks()
    generator.assert_not_called()
    assert updated["description"] == owner_description
    assert updated["workflow_prompt"] == _WORKFLOW_UI_EDIT

    # The opt-in stays cleared for subsequent edits too.
    with _description_generator() as generator:
        update_agent(
            client, superuser_token_headers, agent["id"],
            workflow_prompt=_WORKFLOW_UI_EDIT_2,
        )
    generator.assert_not_called()


# ── Scenario 5: create-flow opts in ──────────────────────────────────────────


def test_create_flow_agent_is_opted_in(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    1. POST /agents/create-flow (web create flow).
    2. A later workflow edit → generator called (the flow opted the agent in).
    """
    me = _me_id(client, superuser_token_headers)
    with patch(
        "app.services.agents.agent_service.EnvironmentService.create_environment",
        _StubCreateEnvironment(),
    ):
        r = client.post(
            f"{API}/agents/create-flow",
            headers=superuser_token_headers,
            json={"description": "An agent that drafts release notes", "mode": "building"},
        )
    drain_tasks()
    assert r.status_code == 200, r.text

    events = [
        json.loads(line.strip()[6:])
        for line in r.text.strip().split("\n")
        if line.strip().startswith("data: ")
    ]
    created = next((e for e in events if e.get("step") == "agent_created"), None)
    assert created is not None, [e.get("step") for e in events]
    agent = get_agent(client, superuser_token_headers, created["agent_id"])

    with _description_generator() as generator:
        update_agent(
            client, superuser_token_headers, agent["id"],
            workflow_prompt=_WORKFLOW_UI_EDIT,
        )
        drain_tasks()
    _assert_generated_for(generator, agent, _WORKFLOW_UI_EDIT, me)


# ── Scenario 6: the real writer re-checks the opt-in before writing ──────────
#
# Documented exemption from the API-only rule: ``_generate_description_background``
# is called directly. It is only reachable through a daemon thread that opens
# ``Session(engine)``, and the race it guards (opt-in turned off while the model
# call runs) cannot be staged through HTTP. ``engine`` is pointed at the test
# connection so the writer's own session joins the test transaction; the model
# call itself is mocked.


def _run_real_description_writer(
    db: Session, agent: dict, generated: str = "GENERATED DESCRIPTION"
) -> None:
    from app.services.agents.agent_service import _generate_description_background

    with (
        patch("app.services.agents.agent_service.engine", db.connection()),
        patch(
            "app.services.agents.agent_service.AIFunctionsService.is_available",
            return_value=True,
        ),
        patch(
            "app.services.agents.agent_service.AIFunctionsService."
            "generate_description_from_workflow",
            return_value=generated,
        ) as model_call,
    ):
        _generate_description_background(
            uuid.UUID(agent["id"]), _WORKFLOW_UI_EDIT, agent["name"], user_id=None
        )
    model_call.assert_called_once()
    # The writer committed through its own Session on the same connection.
    db.expire_all()


def test_real_writer_updates_description_while_opted_in(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """Positive control for the seam: opted-in agent -> description written."""
    agent = _create_agent(client, superuser_token_headers, auto_generate_description=True)

    _run_real_description_writer(db, agent, generated="Drafts weekly reports.")

    after = get_agent(client, superuser_token_headers, agent["id"])
    assert after["description"] == "Drafts weekly reports."


def test_real_writer_skips_write_when_opt_in_was_turned_off(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    1. Opted-in agent; the owner PUTs a description (opt-in cleared).
    2. A generation that was already in flight finishes afterwards.
    3. The owner's description is kept: the writer re-read the flag.
    """
    agent = _create_agent(client, superuser_token_headers, auto_generate_description=True)
    owner_description = "Owner-written: answers HR questions."
    update_agent(
        client, superuser_token_headers, agent["id"], description=owner_description
    )

    _run_real_description_writer(db, agent, generated="MODEL OVERWRITE")

    after = get_agent(client, superuser_token_headers, agent["id"])
    assert after["description"] == owner_description
