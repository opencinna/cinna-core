"""
Agent Improvement Requests — prompt capture, divergence, and personal memory.

The archive has to answer "what was the system prompt for this run". Two
sources feed that answer and neither is visible to a bundle publisher from
their own install: the prompt documents (which a consumer can edit after
installing) and the container-only personal memory area.

See docs/plans/agent_improvement_requests_plan.md §3.3.
"""
import json
import zipfile
from io import BytesIO
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent, update_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    link_bundle_credential_to_agent,
    make_bundle_public,
)
from tests.utils.credential import create_random_credential
from tests.utils.message import send_message
from tests.utils.session import create_session_via_api

API = settings.API_V1_STR

PUBLISHED_WORKFLOW = "Answer invoice questions. Always cite the month."
EDITED_WORKFLOW = "Answer invoice questions. Never ask for the file twice."


def _seed_message(
    client: TestClient, headers: dict[str, str], session_id: str, content: str
) -> None:
    stub = StubAgentEnvConnector(response_text="Understood.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, headers, session_id, content=content)
        drain_tasks()


def _submit(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
    **body_extra,
) -> dict:
    r = client.post(
        f"{API}/improvement-requests",
        headers=headers,
        json={"session_id": session_id, **body_extra},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _archive(client: TestClient, headers: dict[str, str], request_id: str) -> zipfile.ZipFile:
    r = client.get(f"{API}/improvement-requests/{request_id}/archive", headers=headers)
    assert r.status_code == 200, r.text
    return zipfile.ZipFile(BytesIO(r.content))


def _context(client: TestClient, headers: dict[str, str], request_id: str) -> dict:
    r = client.get(f"{API}/improvement-requests/{request_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["context"]


class _MemoryStub:
    """Stands in for AgentEnvConnector, capturing whether /exec was reached."""

    def __init__(self, files: list[dict] | None = None, fail: bool = False) -> None:
        self._files = files or []
        self._fail = fail
        self.calls: list[dict] = []

    async def exec_command(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("Environment exec failed: HTTP 500")
        return {"exit_code": 0, "stdout": json.dumps(self._files), "stderr": ""}


def _publish_bundle_with_prompt(
    client: TestClient, headers: dict[str, str], name: str
) -> tuple[dict, str]:
    """Publisher agent with a real workflow prompt, published and public."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    update_agent(client, headers, agent["id"], workflow_prompt=PUBLISHED_WORKFLOW)
    r = client.post(
        f"{API}/agents/{agent['id']}/publish", headers=headers, json={"version": "1.0.0"}
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    make_bundle_public(client, headers, agent["bundle_uuid"])
    return agent, agent["bundle_id"]


def test_prompt_edit_after_install_is_captured_and_reported_as_diverged(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    An install's workflow prompt is edited after it was materialised from a
    revision. The archive ships the EDITED text — not the published text — and
    reports that one field as diverged while the untouched fields are not.

    The edit is made on the publisher's own install rather than a consumer's:
    a foreign install is use-only for every role, so the consumer-side variant
    of this drift only ever arrives through the env→DB prompt reconcile and is
    not reachable over the API. Both land in the same `Agent` column and are
    compared by the same code.
    """
    publisher_agent, _bundle_id = _publish_bundle_with_prompt(
        client, superuser_token_headers, "Prompt Divergence Publisher"
    )
    update_agent(
        client,
        superuser_token_headers,
        publisher_agent["id"],
        workflow_prompt=EDITED_WORKFLOW,
    )

    session = create_session_via_api(
        client, superuser_token_headers, publisher_agent["id"]
    )
    _seed_message(
        client, superuser_token_headers, session["id"], content="It asked twice."
    )
    request = _submit(client, superuser_token_headers, session["id"])

    context = _context(client, superuser_token_headers, request["id"])
    prompts = context["prompts"]
    assert prompts["baseline"] == "installed_revision"
    assert prompts["diverged"] is True
    assert prompts["workflow"]["diverged_from_installed_revision"] is True
    assert prompts["entrypoint"]["diverged_from_installed_revision"] is False
    assert prompts["workflow"]["text"] == EDITED_WORKFLOW

    zf = _archive(client, superuser_token_headers, request["id"])
    assert "prompts/WORKFLOW_PROMPT.md" in zf.namelist()
    shipped = zf.read("prompts/WORKFLOW_PROMPT.md").decode()
    assert EDITED_WORKFLOW in shipped
    assert PUBLISHED_WORKFLOW not in shipped
    # A field the agent never had must not become an empty file — that would
    # read as "the consumer blanked it".
    assert "prompts/REFINER_PROMPT.md" not in zf.namelist()
    assert "diverged" in zf.read("prompts/README.md").decode().lower()


def test_standalone_agent_reports_divergence_as_unknown_not_false(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    With no installed bundle revision there is no baseline. Divergence must be
    null — reporting `false` would assert a match that was never checked.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="Standalone Prompts")
    drain_tasks()
    update_agent(
        client, superuser_token_headers, agent["id"], workflow_prompt=PUBLISHED_WORKFLOW
    )
    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session["id"], content="Odd answer.")
    request = _submit(client, superuser_token_headers, session["id"])

    prompts = _context(client, superuser_token_headers, request["id"])["prompts"]
    assert prompts["baseline"] == "none"
    assert prompts["diverged"] is None
    assert prompts["workflow"]["diverged_from_installed_revision"] is None
    # The text is still captured — divergence is unknown, the prompt is not.
    assert prompts["workflow"]["text"] == PUBLISHED_WORKFLOW

    readme = _archive(client, superuser_token_headers, request["id"]).read(
        "prompts/README.md"
    ).decode()
    assert "no baseline" in readme.lower()


def test_memory_files_are_captured_and_filenames_sanitised(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The memory area is read from the container and lands under memory/. A
    hostile filename cannot escape that directory when the archive is extracted.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="Memory Capture")
    drain_tasks()
    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session["id"], content="Wrong tone.")

    stub = _MemoryStub(
        files=[
            {"filename": "MEMORY.md", "text": "Call me Sam.", "truncated": False},
            {"filename": "../../etc/passwd", "text": "escaped", "truncated": False},
        ]
    )
    with patch(
        "app.services.environments.agent_env_connector.AgentEnvConnector",
        return_value=stub,
    ):
        request = _submit(client, superuser_token_headers, session["id"])

    assert len(stub.calls) == 1
    memory = _context(client, superuser_token_headers, request["id"])["memory"]
    assert memory["available"] is True
    assert memory["file_count"] == 2

    names = _archive(client, superuser_token_headers, request["id"]).namelist()
    assert "memory/MEMORY.md" in names
    assert "memory/passwd" in names
    assert not any(".." in name for name in names)


def test_declining_memory_reads_nothing_from_the_container(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    include_memory=false must not merely drop the result — it must never make
    the container read at all, and the row must record why memory is absent.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="Memory Declined")
    drain_tasks()
    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session["id"], content="Nope.")

    stub = _MemoryStub(files=[{"filename": "MEMORY.md", "text": "private", "truncated": False}])
    with patch(
        "app.services.environments.agent_env_connector.AgentEnvConnector",
        return_value=stub,
    ):
        request = _submit(client, superuser_token_headers, session["id"], include_memory=False)

    assert stub.calls == []
    memory = _context(client, superuser_token_headers, request["id"])["memory"]
    assert memory["available"] is False
    assert memory["unavailable_reason"] == "declined_by_requester"
    assert memory["files"] == []
    assert not any(
        name.startswith("memory/")
        for name in _archive(client, superuser_token_headers, request["id"]).namelist()
    )


def test_unreadable_container_degrades_instead_of_failing_the_request(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A failing exec records `read_failed`; the request still goes through."""
    agent = create_agent_via_api(client, superuser_token_headers, name="Memory Unreadable")
    drain_tasks()
    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session["id"], content="Hmm.")

    with patch(
        "app.services.environments.agent_env_connector.AgentEnvConnector",
        return_value=_MemoryStub(fail=True),
    ):
        request = _submit(client, superuser_token_headers, session["id"])

    memory = _context(client, superuser_token_headers, request["id"])["memory"]
    assert memory["available"] is False
    assert memory["unavailable_reason"] == "read_failed"


def test_secrets_in_prompts_and_memory_are_redacted(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The scrubber now runs over the context block too: a credential value that
    ended up in a prompt document or a memory note must not reach the archive.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="Prompt Secret")
    drain_tasks()
    credential = create_random_credential(
        client,
        superuser_token_headers,
        credential_type="odoo",
        credential_data={
            "url": "https://odoo.example.com",
            "database_name": "db",
            "login": "admin",
            "api_token": "super-secret-odoo-token-123",
        },
    )
    link_bundle_credential_to_agent(
        client, superuser_token_headers, agent["id"], credential["id"]
    )
    update_agent(
        client,
        superuser_token_headers,
        agent["id"],
        workflow_prompt="Call the API with token super-secret-odoo-token-123.",
    )

    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session["id"], content="Failed.")

    stub = _MemoryStub(
        files=[
            {
                "filename": "MEMORY.md",
                "text": "My key is super-secret-odoo-token-123",
                "truncated": False,
            }
        ]
    )
    with patch(
        "app.services.environments.agent_env_connector.AgentEnvConnector",
        return_value=stub,
    ):
        request = _submit(client, superuser_token_headers, session["id"])

    zf = _archive(client, superuser_token_headers, request["id"])
    prompt_text = zf.read("prompts/WORKFLOW_PROMPT.md").decode()
    memory_text = zf.read("memory/MEMORY.md").decode()
    assert "super-secret-odoo-token-123" not in prompt_text
    assert "super-secret-odoo-token-123" not in memory_text
    assert "***REDACTED***" in prompt_text
    assert "***REDACTED***" in memory_text
    assert "super-secret-odoo-token-123" not in zf.read("context.json").decode()
