"""
Agent Improvement Requests — the frozen-snapshot invariant and status lifecycle.

Plan §2.2's "no live read-through" invariant is the entire privacy argument for
this feature and, prior to this file, has no regression guard: once the row is
written, nothing in this feature reads the source ``Session`` again — continuing
the conversation or deleting the session must not change what the archive
contains.

See docs/plans/agent_improvement_requests_plan.md §2.2 and §5.2 (update_status).
"""
import json
import zipfile
from io import BytesIO
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.socketio_stub import StubSocketIOConnector
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle,
    make_user_and_headers,
    publish_bundle_and_make_public,
)
from tests.utils.message import send_message
from tests.utils.session import create_session_via_api

API = settings.API_V1_STR


def _seed_message(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
    content: str = "Please help me with X.",
) -> None:
    stub = StubAgentEnvConnector(response_text="Sure, here is some help.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, headers, session_id, content=content)
        drain_tasks()


def test_frozen_snapshot_unaffected_by_continued_conversation_or_session_deletion(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Create a session, seed one exchange, submit an improvement request.
    2. Download the archive → capture its bytes and the request's
       snapshot_message_count.
    3. Continue the conversation with two more exchanges.
    4. Download the archive again → byte-identical, identical
       snapshot_message_count (no live read-through — the archive is a pure
       function of the frozen row, and its ZIP timestamps are fixed, so an
       unchanged row always serializes to the same bytes).
    5. Delete the source session entirely.
    6. Download the archive a third time → still succeeds, and the frozen
       payload (transcript, context, README) is byte-identical to before.
       The request row survives the session's deletion — only its
       provenance backlink (``metadata.json``'s ``session_id``, and the
       ``session_id`` field on the request itself) goes null, per the
       model's ``ON DELETE SET NULL`` (the snapshot is the payload;
       provenance is best-effort).
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session["id"], content="It failed on step 3.")

    r = client.post(
        f"{API}/improvement-requests",
        headers=superuser_token_headers,
        json={"session_id": session["id"], "comment": "Failed midway."},
    )
    assert r.status_code == 201, r.text
    request = r.json()
    request_id = request["id"]
    first_count = request["snapshot_message_count"]
    assert first_count >= 1

    archive_1 = client.get(
        f"{API}/improvement-requests/{request_id}/archive", headers=superuser_token_headers
    )
    assert archive_1.status_code == 200, archive_1.text
    assert archive_1.headers["content-type"] == "application/zip"

    # ── Phase 3: Continue the conversation ───────────────────────────────
    _seed_message(client, superuser_token_headers, session["id"], content="Here's more context.")
    _seed_message(client, superuser_token_headers, session["id"], content="And even more detail.")

    # ── Phase 4: Snapshot count and archive bytes are unchanged ─────────
    detail_after = client.get(
        f"{API}/improvement-requests/{request_id}", headers=superuser_token_headers
    )
    assert detail_after.status_code == 200, detail_after.text
    assert detail_after.json()["snapshot_message_count"] == first_count

    archive_2 = client.get(
        f"{API}/improvement-requests/{request_id}/archive", headers=superuser_token_headers
    )
    assert archive_2.status_code == 200, archive_2.text
    assert archive_2.content == archive_1.content

    # ── Phase 5: Delete the source session ───────────────────────────────
    del_r = client.delete(f"{API}/sessions/{session['id']}", headers=superuser_token_headers)
    assert del_r.status_code == 200, del_r.text

    # ── Phase 6: Archive still intact after the session is gone ─────────
    detail_final = client.get(
        f"{API}/improvement-requests/{request_id}", headers=superuser_token_headers
    )
    assert detail_final.status_code == 200, detail_final.text
    assert detail_final.json()["snapshot_message_count"] == first_count

    archive_3 = client.get(
        f"{API}/improvement-requests/{request_id}/archive", headers=superuser_token_headers
    )
    assert archive_3.status_code == 200, archive_3.text

    # The frozen payload members are byte-identical to the pre-deletion
    # archive — the snapshot/context never re-read the (now gone) session.
    zf_1 = zipfile.ZipFile(BytesIO(archive_1.content))
    zf_3 = zipfile.ZipFile(BytesIO(archive_3.content))
    for member in ("context.json", "session/messages.json", "session/messages.md"):
        assert zf_3.read(member) == zf_1.read(member), member

    # Only the provenance backlink goes null (ON DELETE SET NULL) — every
    # other metadata.json field is unchanged.
    metadata_1 = json.loads(zf_1.read("metadata.json").decode())
    metadata_3 = json.loads(zf_3.read("metadata.json").decode())
    assert metadata_1["session_id"] is not None
    assert metadata_3["session_id"] is None
    assert {**metadata_1, "session_id": None} == metadata_3


def test_status_transition_stamps_timestamp_and_emits_update_event(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    PATCH /improvement-requests/{id} (owner-only):
      - Starts at status "new" with status_changed_at=None.
      - Setting status + resolution_note stamps status_changed_at and emits
        IMPROVEMENT_REQUEST_UPDATED to the owner's room.
      - resolution_note is visible to the requester via /improvement-requests/mine.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session["id"])

    r = client.post(
        f"{API}/improvement-requests",
        headers=superuser_token_headers,
        json={"session_id": session["id"]},
    )
    assert r.status_code == 201, r.text
    request = r.json()
    assert request["status"] == "new"
    assert request["status_changed_at"] is None

    sio_stub = StubSocketIOConnector()
    with patch("app.services.events.event_service.socketio_connector", sio_stub):
        patch_r = client.patch(
            f"{API}/improvement-requests/{request['id']}",
            headers=superuser_token_headers,
            json={"status": "in_progress", "resolution_note": "Looking into it."},
        )
        assert patch_r.status_code == 200, patch_r.text

    updated = patch_r.json()
    assert updated["status"] == "in_progress"
    assert updated["resolution_note"] == "Looking into it."
    assert updated["status_changed_at"] is not None

    update_events = [
        e
        for e in sio_stub.emitted_events
        if e["data"].get("type") == "improvement_request_updated"
        and e["data"].get("meta", {}).get("request_id") == request["id"]
    ]
    assert len(update_events) == 1, sio_stub.emitted_events
    assert update_events[0]["data"]["meta"]["status"] == "in_progress"

    mine = client.get(f"{API}/improvement-requests/mine", headers=superuser_token_headers)
    assert mine.status_code == 200, mine.text
    mine_row = next(row for row in mine.json()["data"] if row["id"] == request["id"])
    assert mine_row["resolution_note"] == "Looking into it."
    assert mine_row["status"] == "in_progress"


def test_cascade_deletes_on_target_agent_and_requester_account_removal(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Deleting the TARGET agent cascade-deletes requests that landed on it
    (CASCADE on target_agent_id); deleting the REQUESTER's account
    cascade-deletes the requests they submitted (CASCADE on
    requester_user_id) — plan §3.1's deliberate cascade rationale. Verified
    via the API only: after each deletion, the request id that used to
    resolve now 404s.
    """
    # ── Target-agent cascade: standalone agent, self-targeted request ───
    target_agent = create_agent_via_api(client, superuser_token_headers, name="Cascade Target")
    drain_tasks()
    target_session = create_session_via_api(client, superuser_token_headers, target_agent["id"])
    _seed_message(client, superuser_token_headers, target_session["id"])
    r = client.post(
        f"{API}/improvement-requests",
        headers=superuser_token_headers,
        json={"session_id": target_session["id"]},
    )
    assert r.status_code == 201, r.text
    target_request_id = r.json()["id"]

    del_agent = client.delete(
        f"{API}/agents/{target_agent['id']}", headers=superuser_token_headers
    )
    assert del_agent.status_code == 200, del_agent.text
    assert (
        client.get(
            f"{API}/improvement-requests/{target_request_id}", headers=superuser_token_headers
        ).status_code
        == 404
    )

    # ── Requester-account cascade: bundle consumer's request lands on the
    #    PUBLISHER's (superuser-owned) agent — it must survive independently
    #    of the target agent, and disappear only once the consumer's own
    #    account is deleted ────────────────────────────────────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Cascade Publisher"
    )
    drain_tasks()
    publish_bundle_and_make_public(client, superuser_token_headers, publisher_agent["id"])
    publisher_agent = get_agent(client, superuser_token_headers, publisher_agent["id"])
    bundle_id = publisher_agent["bundle_id"]

    consumer, consumer_headers = make_user_and_headers(client)
    installed = install_bundle(client, consumer_headers, bundle_id)
    consumer_session = create_session_via_api(client, consumer_headers, installed["id"])
    _seed_message(client, consumer_headers, consumer_session["id"])
    r2 = client.post(
        f"{API}/improvement-requests",
        headers=consumer_headers,
        json={"session_id": consumer_session["id"]},
    )
    assert r2.status_code == 201, r2.text
    cross_request_id = r2.json()["id"]
    assert r2.json()["target_agent_id"] == publisher_agent["id"]

    del_user = client.delete(f"{API}/users/{consumer['id']}", headers=superuser_token_headers)
    assert del_user.status_code == 200, del_user.text

    assert (
        client.get(
            f"{API}/improvement-requests/{cross_request_id}", headers=superuser_token_headers
        ).status_code
        == 404
    )
