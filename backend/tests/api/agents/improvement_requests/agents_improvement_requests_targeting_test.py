"""
Agent Improvement Requests — target resolution and cross-user authorization.

Covers ``ImprovementRequestService.resolve_target`` (plan §5.2) and the
authorization matrix (plan §4.3):
  - Standalone agent → self-targeted request; a third party gets 404 (not
    403) on read/download/patch/delete.
  - Bundle consumer install → targets the PUBLISHER install; visible only to
    the publisher and the requester, and a requester (party to the row, but
    not the owner) gets 403 — not 404 — trying to mutate it.
  - Publisher install deleted → falls back to self-target with
    ``fallback_reason="publisher_unavailable"``.
  - Guest-share / webapp-share sessions are rejected by the eligibility gate
    (400 ``not_eligible``), and ``/session-improve`` is marked unavailable in
    the session's command autocomplete list.

See docs/plans/agent_improvement_requests_plan.md §4.3, §4.4, §5.2.
"""
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent, update_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle,
    make_user_and_headers,
    publish_bundle_and_make_public,
)
from tests.utils.guest_share import create_guest_share
from tests.utils.message import send_message
from tests.utils.session import create_session_via_api
from tests.utils.user import create_random_user, user_authentication_headers
from tests.utils.webapp_share import create_webapp_share

API = settings.API_V1_STR


def _seed_message(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
    content: str = "It kept re-reading the same file over and over.",
) -> None:
    """Get one agent-role message into a session so the eligibility gate passes."""
    stub = StubAgentEnvConnector(response_text="Let me look into that.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, headers, session_id, content=content)
        drain_tasks()


def test_standalone_agent_self_targets_and_third_party_gets_404(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Standalone (non-bundle) agent:
      1. Owner creates an agent + session, seeds a message, submits an
         improvement request → self-targeted (target == source agent,
         owner == requester), preview and detail both show
         is_shared_externally=False.
      2. A third, unrelated user gets 404 — not 403 — on GET, PATCH, DELETE,
         and archive download of the request. A non-existent id also 404s.
    """
    # ── Phase 1: Standalone agent, self-targeted request ────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session["id"])

    preview = client.get(
        f"{API}/sessions/{session['id']}/improvement-context",
        headers=superuser_token_headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible"] is True
    assert preview.json()["is_shared_externally"] is False

    r = client.post(
        f"{API}/improvement-requests",
        headers=superuser_token_headers,
        json={"session_id": session["id"], "comment": "It kept re-reading the same file."},
    )
    assert r.status_code == 201, r.text
    request = r.json()
    assert request["target_agent_id"] == agent["id"]
    assert request["source_agent_id"] == agent["id"]
    request_id = request["id"]

    detail = client.get(
        f"{API}/improvement-requests/{request_id}", headers=superuser_token_headers
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["context"]["recipient"]["is_shared_externally"] is False
    assert detail.json()["context"]["recipient"]["fallback_reason"] is None

    # ── Phase 2: Third party gets 404, not 403 ───────────────────────────
    stranger = create_random_user(client)
    stranger_headers = user_authentication_headers(
        client=client, email=stranger["email"], password=stranger["_password"]
    )
    assert (
        client.get(f"{API}/improvement-requests/{request_id}", headers=stranger_headers).status_code
        == 404
    )
    assert (
        client.get(
            f"{API}/improvement-requests/{request_id}/archive", headers=stranger_headers
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"{API}/improvement-requests/{request_id}",
            headers=stranger_headers,
            json={"status": "in_progress"},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"{API}/improvement-requests/{request_id}", headers=stranger_headers
        ).status_code
        == 404
    )

    ghost = str(uuid.uuid4())
    assert (
        client.get(f"{API}/improvement-requests/{ghost}", headers=superuser_token_headers).status_code
        == 404
    )


def test_bundle_consumer_targets_publisher_install_visible_only_to_publisher(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A consumer install of a published bundle:
      1. Publisher publishes + lists a bundle; a consumer installs it.
      2. Consumer submits an improvement request from a session on their
         install → the request lands on the PUBLISHER's install, owner ==
         publisher, is_shared_externally=True.
      3. Publisher sees it via GET /agents/{publisher_agent}/improvement-requests
         and the detail route; the requester sees it via
         GET /improvement-requests/mine and the detail route too.
      4. A third, unrelated user gets 404. The consumer (a party to the row
         as requester, but not the owner) gets 403 — not 404 — trying to
         PATCH/DELETE it, matching the deliberate exception in plan §4.3.
    """
    publisher_headers = superuser_token_headers
    publisher_agent = create_agent_via_api(client, publisher_headers, name="Targeting Publisher")
    drain_tasks()
    publish_bundle_and_make_public(client, publisher_headers, publisher_agent["id"])
    publisher_agent = get_agent(client, publisher_headers, publisher_agent["id"])
    assert publisher_agent["is_publisher_install"] is True
    bundle_id = publisher_agent["bundle_id"]

    consumer, consumer_headers = make_user_and_headers(client)
    installed = install_bundle(client, consumer_headers, bundle_id)
    consumer_agent_id = installed["id"]
    assert installed["is_publisher_install"] is False

    session = create_session_via_api(client, consumer_headers, consumer_agent_id)
    _seed_message(client, consumer_headers, session["id"], content="Confused about pricing.")

    r = client.post(
        f"{API}/improvement-requests",
        headers=consumer_headers,
        json={"session_id": session["id"], "comment": "Confused about pricing."},
    )
    assert r.status_code == 201, r.text
    request = r.json()
    request_id = request["id"]
    assert request["target_agent_id"] == publisher_agent["id"]
    assert request["source_agent_id"] == consumer_agent_id

    detail_for_publisher = client.get(
        f"{API}/improvement-requests/{request_id}", headers=publisher_headers
    )
    assert detail_for_publisher.status_code == 200, detail_for_publisher.text
    assert detail_for_publisher.json()["context"]["recipient"]["is_shared_externally"] is True

    # Publisher's Configuration-tab card query
    card = client.get(
        f"{API}/agents/{publisher_agent['id']}/improvement-requests", headers=publisher_headers
    )
    assert card.status_code == 200, card.text
    assert any(row["id"] == request_id for row in card.json()["data"])

    # Requester's own view
    mine = client.get(f"{API}/improvement-requests/mine", headers=consumer_headers)
    assert mine.status_code == 200, mine.text
    assert any(row["id"] == request_id for row in mine.json()["data"])
    assert (
        client.get(f"{API}/improvement-requests/{request_id}", headers=consumer_headers).status_code
        == 200
    )

    # Stranger → 404 on both the detail route and the owner's card query
    stranger = create_random_user(client)
    stranger_headers = user_authentication_headers(
        client=client, email=stranger["email"], password=stranger["_password"]
    )
    assert (
        client.get(f"{API}/improvement-requests/{request_id}", headers=stranger_headers).status_code
        == 404
    )
    assert (
        client.get(
            f"{API}/agents/{publisher_agent['id']}/improvement-requests", headers=stranger_headers
        ).status_code
        == 404
    )

    # Consumer (requester, not owner) → 403 on mutate, not 404
    assert (
        client.patch(
            f"{API}/improvement-requests/{request_id}",
            headers=consumer_headers,
            json={"status": "in_progress"},
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"{API}/improvement-requests/{request_id}", headers=consumer_headers
        ).status_code
        == 403
    )


def test_publisher_install_deleted_falls_back_to_self_with_reason(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    When the publisher install is deleted after a consumer installed the
    bundle, ``resolve_target`` cannot reach it and falls back to self: the
    request lands on the consumer's OWN install, owner == consumer,
    ``context.recipient.fallback_reason == "publisher_unavailable"``, and
    the preview shows the non-shared copy.
    """
    publisher_headers = superuser_token_headers
    publisher_agent = create_agent_via_api(client, publisher_headers, name="Fallback Publisher")
    drain_tasks()
    publish_bundle_and_make_public(client, publisher_headers, publisher_agent["id"])
    publisher_agent = get_agent(client, publisher_headers, publisher_agent["id"])
    bundle_id = publisher_agent["bundle_id"]

    consumer, consumer_headers = make_user_and_headers(client)
    installed = install_bundle(client, consumer_headers, bundle_id)
    consumer_agent_id = installed["id"]

    # Publisher install deleted (require_developer — superuser always passes).
    del_r = client.delete(f"{API}/agents/{publisher_agent['id']}", headers=publisher_headers)
    assert del_r.status_code == 200, del_r.text

    session = create_session_via_api(client, consumer_headers, consumer_agent_id)
    _seed_message(client, consumer_headers, session["id"], content="Publisher install gone.")

    preview = client.get(
        f"{API}/sessions/{session['id']}/improvement-context", headers=consumer_headers
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["is_shared_externally"] is False

    r = client.post(
        f"{API}/improvement-requests",
        headers=consumer_headers,
        json={"session_id": session["id"], "comment": "Publisher install gone."},
    )
    assert r.status_code == 201, r.text
    request = r.json()
    assert request["target_agent_id"] == consumer_agent_id

    detail = client.get(f"{API}/improvement-requests/{request['id']}", headers=consumer_headers)
    assert detail.status_code == 200, detail.text
    recipient = detail.json()["context"]["recipient"]
    assert recipient["fallback_reason"] == "publisher_unavailable"
    assert recipient["is_shared_externally"] is False


def test_guest_and_webapp_share_sessions_rejected_and_command_marked_unavailable(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Sessions tied to a guest share or a webapp share have no identifiable
    consenting account behind them:
      - The eligibility gate rejects submission with reason "not_eligible"
        (400), both on the preview and on the actual POST.
      - /session-improve is marked unavailable in the session's command
        autocomplete list (``CommandService.list_for_session``).
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    update_agent(client, superuser_token_headers, agent["id"], webapp_enabled=True)

    guest_share = create_guest_share(client, superuser_token_headers, agent["id"])
    webapp_share = create_webapp_share(client, superuser_token_headers, agent["id"])

    guest_session = create_session_via_api(
        client, superuser_token_headers, agent["id"], guest_share_id=guest_share["id"]
    )

    webapp_session_resp = client.post(
        f"{API}/sessions/",
        headers=superuser_token_headers,
        json={
            "agent_id": agent["id"],
            "mode": "conversation",
            "webapp_share_id": webapp_share["id"],
        },
    )
    assert webapp_session_resp.status_code == 200, webapp_session_resp.text
    webapp_session = webapp_session_resp.json()
    assert webapp_session["webapp_share_id"] == webapp_share["id"]

    for session in (guest_session, webapp_session):
        _seed_message(client, superuser_token_headers, session["id"])

        preview = client.get(
            f"{API}/sessions/{session['id']}/improvement-context",
            headers=superuser_token_headers,
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["eligible"] is False
        assert body["reason"] == "not_eligible"

        submit = client.post(
            f"{API}/improvement-requests",
            headers=superuser_token_headers,
            json={"session_id": session["id"]},
        )
        assert submit.status_code == 400, submit.text

        commands = client.get(
            f"{API}/sessions/{session['id']}/commands", headers=superuser_token_headers
        )
        assert commands.status_code == 200, commands.text
        session_improve = next(
            c for c in commands.json()["commands"] if c["name"] == "/session-improve"
        )
        assert session_improve["is_available"] is False
