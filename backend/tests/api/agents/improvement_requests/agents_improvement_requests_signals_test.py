"""
Agent Improvement Requests — the signals a recipient acts on.

Every assertion here is about a recipient reading a request and drawing the
right conclusion from it. A signal that is usually wrong is worse than no
signal: it gets ignored, and it takes the true ones down with it.

  - The prompt-divergence rollup covers the *published* prompt documents only.
    ``router_trigger_prompt`` is routing metadata — the platform generates one
    for installs that have none, and the install's owner may set their own — so
    it is reported without ever claiming the consumer edited the publisher's
    text.
  - The list projection names the session and the install kind, rather than
    leaving both to be inferred from nullable strings.
  - ``allowed_tools`` distinguishes "no auto-approval list" from "an empty one".

See docs/plans/agent_improvement_requests_plan.md §3.3.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle,
    make_user_and_headers,
    publish_bundle_and_make_public,
)
from tests.utils.message import send_message
from tests.utils.session import create_session_via_api

API = settings.API_V1_STR

CONSUMER_TRIGGER = "Use this agent for live BTC and crypto rate lookups."


def _seed_message(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
    content: str = "It answered the wrong question.",
) -> None:
    stub = StubAgentEnvConnector(response_text="Noted.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, headers, session_id, content=content)
        drain_tasks()


def _submit(
    client: TestClient, headers: dict[str, str], session_id: str, **extra
) -> dict:
    r = client.post(
        f"{API}/improvement-requests",
        headers=headers,
        json={"session_id": session_id, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _context(client: TestClient, headers: dict[str, str], request_id: str) -> dict:
    r = client.get(f"{API}/improvement-requests/{request_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["context"]



def test_router_trigger_prompt_on_an_install_is_not_reported_as_a_prompt_edit(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A consumer sets a router trigger prompt on their install of a bundle whose
    publisher never set one. The publisher must NOT be told their prompts were
    edited: nothing they published changed.

    Publishers rarely set `router_trigger_prompt`, and the platform generates
    one for every foreign install that has no auto-managed route, so comparing
    it against a null baseline fires on nearly every consumer install — on the
    one field a consumer is entitled to change.
    """
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Trigger Publisher"
    )
    drain_tasks()
    update_agent(
        client,
        superuser_token_headers,
        publisher_agent["id"],
        workflow_prompt="Answer rate questions with the source and timestamp.",
    )
    publish_bundle_and_make_public(
        client, superuser_token_headers, publisher_agent["id"]
    )
    bundle_id = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()["bundle_id"]

    _consumer, consumer_headers = make_user_and_headers(client)
    installed = install_bundle(client, consumer_headers, bundle_id)

    # The write path an `agent-user` account has on its own install — the same
    # column the platform's router backfill writes.
    r = client.patch(
        f"{API}/agents/{installed['id']}/router-trigger-prompt",
        headers=consumer_headers,
        json={"router_trigger_prompt": CONSUMER_TRIGGER},
    )
    assert r.status_code == 200, r.text

    session = create_session_via_api(client, consumer_headers, installed["id"])
    _seed_message(client, consumer_headers, session["id"])
    request = _submit(client, consumer_headers, session["id"], comment="Wrong rate.")

    prompts = _context(client, superuser_token_headers, request["id"])["prompts"]
    assert prompts["baseline"] == "installed_revision"
    # The signal that matters: nothing the publisher published diverged.
    assert prompts["diverged"] is False
    assert prompts["diverged_fields"] == []
    # The field is still reported — as unknown, with the reason, never as an edit.
    router = prompts["router_trigger"]
    assert router["role"] == "routing_metadata"
    assert router["text"] == CONSUMER_TRIGGER
    assert router["diverged_from_installed_revision"] is None
    assert router["divergence_reason"] == "platform_managed_no_baseline"
    assert prompts["workflow"]["role"] == "published_prompt"

    archive = client.get(
        f"{API}/improvement-requests/{request['id']}/archive",
        headers=superuser_token_headers,
    )
    assert archive.status_code == 200, archive.text
    import zipfile
    from io import BytesIO

    readme = (
        zipfile.ZipFile(BytesIO(archive.content)).read("prompts/README.md").decode()
    )
    assert "not compared (routing metadata)" in readme
    # And the front page must not open with a divergence warning.
    front = zipfile.ZipFile(BytesIO(archive.content)).read("README.md").decode()
    assert "differ from the revision it was installed from" not in front


def test_listing_names_the_session_and_the_install_kind(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Two requests from one session must be distinguishable without downloading
    both archives, and a bundle install must not read as standalone because its
    revision carries no version label.
    """
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Listing Publisher"
    )
    drain_tasks()
    publish_bundle_and_make_public(
        client, superuser_token_headers, publisher_agent["id"]
    )
    bundle_id = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()["bundle_id"]

    _consumer, consumer_headers = make_user_and_headers(client)
    installed = install_bundle(client, consumer_headers, bundle_id)
    session = create_session_via_api(client, consumer_headers, installed["id"])
    _seed_message(client, consumer_headers, session["id"])

    first = _submit(client, consumer_headers, session["id"], comment="First look.")
    _seed_message(client, consumer_headers, session["id"], content="Still wrong.")
    second = _submit(client, consumer_headers, session["id"], comment="Again.")

    listing = client.get(
        f"{API}/agents/{publisher_agent['id']}/improvement-requests",
        headers=superuser_token_headers,
    )
    assert listing.status_code == 200, listing.text
    rows = {row["id"]: row for row in listing.json()["data"]}
    assert {first["id"], second["id"]} <= set(rows)
    # One conversation, two captures — visible from the listing alone.
    assert rows[first["id"]]["session_id"] == session["id"]
    assert rows[second["id"]]["session_id"] == session["id"]
    # Stated, not inferred from `installed_version`, which a git-origin
    # revision leaves null on a perfectly real bundle install.
    assert rows[first["id"]]["is_bundle_install"] is True
    assert rows[first["id"]]["bundle_id"] == bundle_id
    assert rows[first["id"]]["installed_revision_number"] is not None


def test_standalone_agent_request_is_not_flagged_as_a_bundle_install(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The other half of the same claim: standalone stays standalone."""
    agent = create_agent_via_api(
        client, superuser_token_headers, name="Standalone Listing"
    )
    drain_tasks()
    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session["id"])
    request = _submit(
        client, superuser_token_headers, session["id"], comment="Odd answer."
    )

    listing = client.get(
        f"{API}/agents/{agent['id']}/improvement-requests",
        headers=superuser_token_headers,
    )
    row = next(r for r in listing.json()["data"] if r["id"] == request["id"])
    assert row["is_bundle_install"] is False
    assert row["bundle_id"] is None
    assert row["session_id"] == session["id"]


def test_empty_allowed_tools_is_spelled_out_rather_than_left_as_an_empty_list(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    An empty auto-approval list answers "why did it not use that tool?" only if
    the reader knows what empty means. The archive says it in words, and says
    that the list never restricts which tools exist.

    The "never configured at all" case renders as its own sentence and is
    covered in tests/unit/test_improvement_archive_rendering.py — an agent with
    a provisioned environment always has the key.
    """
    import zipfile
    from io import BytesIO

    agent = create_agent_via_api(client, superuser_token_headers, name="Tools Empty")
    drain_tasks()
    # The prompts folder (and with it the tool-configuration section) is only
    # written when at least one document has text.
    update_agent(
        client,
        superuser_token_headers,
        agent["id"],
        workflow_prompt="Answer with the tool output, not a summary of it.",
    )
    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session["id"])
    request = _submit(
        client, superuser_token_headers, session["id"], comment="Never used the tool."
    )

    prompts = _context(client, superuser_token_headers, request["id"])["prompts"]
    assert prompts["allowed_tools"] in (None, [])

    archive = client.get(
        f"{API}/improvement-requests/{request['id']}/archive",
        headers=superuser_token_headers,
    )
    readme = (
        zipfile.ZipFile(BytesIO(archive.content)).read("prompts/README.md").decode()
    )
    assert "every tool use prompted the user" in readme
    assert "never restricts which tools exist" in readme

    r = client.patch(
        f"{API}/agents/{agent['id']}/allowed-tools",
        headers=superuser_token_headers,
        json={"tools": ["Read"]},
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    session_two = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session_two["id"])
    request_two = _submit(
        client, superuser_token_headers, session_two["id"], comment="Second look."
    )
    prompts_two = _context(client, superuser_token_headers, request_two["id"])["prompts"]
    assert prompts_two["allowed_tools"] == ["Read"]
