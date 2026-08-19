"""
Agent Improvement Requests — secret scrubbing, archive contents, audit, and
the snapshot capture caps.

See docs/plans/agent_improvement_requests_plan.md §4.2 (SecretScrubber),
§5.4 (ImprovementArchiveService), §4.6 (audit), §3.2 (capture caps).
"""
import json
import zipfile
from io import BytesIO
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle,
    link_bundle_credential_to_agent,
    make_bundle_public,
    make_user_and_headers,
    publish_bundle_and_make_public,
)
from tests.utils.credential import create_random_credential
from tests.utils.message import list_messages, send_message
from tests.utils.session import create_session_via_api

API = settings.API_V1_STR


def _seed_message(
    client: TestClient, headers: dict[str, str], session_id: str, content: str
) -> None:
    stub = StubAgentEnvConnector(response_text="Understood.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, headers, session_id, content=content)
        drain_tasks()


def _submit(
    client: TestClient, headers: dict[str, str], session_id: str, comment: str | None = None
) -> dict:
    body: dict = {"session_id": session_id}
    if comment is not None:
        body["comment"] = comment
    r = client.post(f"{API}/improvement-requests", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _download(client: TestClient, headers: dict[str, str], request_id: str):
    r = client.get(f"{API}/improvement-requests/{request_id}/archive", headers=headers)
    assert r.status_code == 200, r.text
    return r


def test_secret_scrubbing_redacts_long_values_not_short_ones(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A credential value (>= 8 chars) linked to the source agent, echoed into a
    message, comes back ***REDACTED*** in the archive. A value shorter than
    the 8-char floor is left alone — scrubbing it would shred ordinary prose.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()

    long_cred = create_random_credential(
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
        client, superuser_token_headers, agent["id"], long_cred["id"]
    )

    short_cred = create_random_credential(
        client,
        superuser_token_headers,
        credential_type="odoo",
        credential_data={
            "url": "https://odoo2.example.com",
            "database_name": "db2",
            "login": "admin",
            "api_token": "abc123",
        },
    )
    link_bundle_credential_to_agent(
        client, superuser_token_headers, agent["id"], short_cred["id"]
    )

    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(
        client,
        superuser_token_headers,
        session["id"],
        content="The token is super-secret-odoo-token-123 and also abc123.",
    )

    request = _submit(client, superuser_token_headers, session["id"])
    archive = _download(client, superuser_token_headers, request["id"])
    zf = zipfile.ZipFile(BytesIO(archive.content))
    transcript = zf.read("session/messages.json").decode()

    assert "super-secret-odoo-token-123" not in transcript
    assert "***REDACTED***" in transcript
    assert "abc123" in transcript


def test_archive_zip_contains_all_members_and_context_fields(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The archive opens as a valid ZIP with exactly the five base members, and
    context.json carries bundle version, installed/latest revision, session
    mode, SDK engine, and effective model for a bundle-installed consumer.

    Five, not more: this agent has no prompt text and no reachable memory
    area, so the optional ``prompts/`` and ``memory/`` folders are absent.
    Their presence is covered by
    ``agents_improvement_requests_prompts_memory_test.py``.
    """
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Archive Publisher"
    )
    drain_tasks()
    # An explicit ``version`` label is required for context.json's
    # installed/latest version fields to be non-null — ``publish_bundle_and_make_public``
    # doesn't take one, so the publish call is made directly here.
    publish_r = client.post(
        f"{API}/agents/{publisher_agent['id']}/publish",
        headers=superuser_token_headers,
        json={"version": "1.0.0"},
    )
    assert publish_r.status_code == 200, publish_r.text
    drain_tasks()
    publisher_agent = get_agent(client, superuser_token_headers, publisher_agent["id"])
    make_bundle_public(client, superuser_token_headers, publisher_agent["bundle_uuid"])
    bundle_id = publisher_agent["bundle_id"]

    consumer, consumer_headers = make_user_and_headers(client)
    installed = install_bundle(client, consumer_headers, bundle_id)

    session = create_session_via_api(client, consumer_headers, installed["id"])
    _seed_message(client, consumer_headers, session["id"], content="It got confused.")

    request = _submit(client, consumer_headers, session["id"], comment="Confusing reply.")
    archive = _download(client, superuser_token_headers, request["id"])  # publisher downloads

    zf = zipfile.ZipFile(BytesIO(archive.content))
    assert zf.testzip() is None
    assert set(zf.namelist()) == {
        "README.md",
        "metadata.json",
        "context.json",
        "session/messages.md",
        "session/messages.json",
    }

    context = json.loads(zf.read("context.json").decode())
    assert context["agent"]["bundle_id"] == bundle_id
    assert context["agent"]["installed_version"] is not None
    assert context["agent"]["installed_revision_number"] is not None
    assert context["agent"]["latest_version"] is not None
    assert context["agent"]["latest_revision_number"] is not None
    assert context["sdk"]["session_mode"] == "conversation"
    assert context["sdk"]["effective_engine"] is not None
    assert context["sdk"]["effective_model"] is not None

    metadata = json.loads(zf.read("metadata.json").decode())
    assert metadata["request_id"] == request["id"]

    readme = zf.read("README.md").decode()
    assert "Confusing reply." in readme
    assert "Bundle id" in readme


def test_cross_user_download_audited_once_same_user_download_not_audited(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A download of a *cross-user* request (owner_user_id != requester_user_id)
    writes exactly one IMPROVEMENT_ARCHIVE_DOWNLOADED SecurityEvent,
    attributed to whoever downloaded it. A download of a *same-user* request
    (self-targeted, standalone agent) writes none.
    """
    # ── Cross-user: bundle consumer's request, publisher downloads ──────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Audit Publisher"
    )
    drain_tasks()
    publish_bundle_and_make_public(client, superuser_token_headers, publisher_agent["id"])
    publisher_agent = get_agent(client, superuser_token_headers, publisher_agent["id"])
    bundle_id = publisher_agent["bundle_id"]

    consumer, consumer_headers = make_user_and_headers(client)
    installed = install_bundle(client, consumer_headers, bundle_id)
    session = create_session_via_api(client, consumer_headers, installed["id"])
    _seed_message(client, consumer_headers, session["id"], content="Bug report.")
    cross_request = _submit(client, consumer_headers, session["id"])

    _download(client, superuser_token_headers, cross_request["id"])

    events = client.get(
        f"{API}/security-events/",
        headers=superuser_token_headers,
        params={
            "event_type": "IMPROVEMENT_ARCHIVE_DOWNLOADED",
            "agent_id": publisher_agent["id"],
        },
    )
    assert events.status_code == 200, events.text
    matching = [
        e for e in events.json()["data"] if e["details"].get("request_id") == cross_request["id"]
    ]
    assert len(matching) == 1, matching

    # ── Same-user: standalone agent, owner downloads their own request ──
    own_agent = create_agent_via_api(client, superuser_token_headers, name="Audit Standalone")
    drain_tasks()
    own_session = create_session_via_api(client, superuser_token_headers, own_agent["id"])
    _seed_message(client, superuser_token_headers, own_session["id"], content="Self note.")
    own_request = _submit(client, superuser_token_headers, own_session["id"])

    _download(client, superuser_token_headers, own_request["id"])

    events_after = client.get(
        f"{API}/security-events/",
        headers=superuser_token_headers,
        params={
            "event_type": "IMPROVEMENT_ARCHIVE_DOWNLOADED",
            "agent_id": own_agent["id"],
        },
    )
    assert events_after.status_code == 200, events_after.text
    own_matching = [
        e
        for e in events_after.json()["data"]
        if e["details"].get("request_id") == own_request["id"]
    ]
    assert own_matching == []


def test_snapshot_total_cap_drops_oldest_messages_first(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    When the frozen transcript would exceed the total snapshot byte cap, the
    OLDEST messages are dropped first (defects cluster at the end of a
    conversation) — never the newest. The total cap is patched down so the
    test doesn't need to push megabytes of content through the stub.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    session = create_session_via_api(client, superuser_token_headers, agent["id"])

    turns = 6
    for i in range(turns):
        _seed_message(
            client, superuser_token_headers, session["id"], content=f"turn-marker-{i:02d}"
        )

    all_messages = list_messages(client, superuser_token_headers, session["id"])
    assert len(all_messages) == turns * 2  # one user + one agent message per turn

    with patch(
        "app.services.improvement.session_snapshot_service.MAX_SNAPSHOT_BYTES", 1200
    ):
        request = _submit(client, superuser_token_headers, session["id"])

    assert request["snapshot_truncated"] is True

    archive = _download(client, superuser_token_headers, request["id"])
    zf = zipfile.ZipFile(BytesIO(archive.content))
    snapshot = json.loads(zf.read("session/messages.json").decode())
    assert snapshot["truncated"] is True
    assert snapshot["omitted_message_count"] > 0

    kept = snapshot["messages"]
    assert kept, "expected at least one surviving message"
    kept_seqs = [m["sequence_number"] for m in kept]
    all_seqs = sorted(m["sequence_number"] for m in all_messages)
    # Kept messages are a contiguous, newest suffix of the full transcript —
    # never a prefix and never a scattered subset.
    assert kept_seqs == all_seqs[-len(kept_seqs):]

    kept_contents = " ".join(m["content"] for m in kept)
    assert f"turn-marker-{turns - 1:02d}" in kept_contents  # newest survives
    assert "turn-marker-00" not in kept_contents  # oldest dropped

    readme = zf.read("README.md").decode()
    assert "Truncated snapshot" in readme


def test_tool_digest_keeps_newest_entries_with_leading_gap_marker(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Regression guard for a fixed bug: when a message's tool digest exceeds
    the per-message entry cap, the digest keeps the NEWEST entries (not the
    oldest) and prepends a single omission marker recording how many were
    dropped.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    session = create_session_via_api(client, superuser_token_headers, agent["id"])

    total_events = 12
    events = [
        {"type": "tool", "tool_name": "bash", "content": f"call-{i:02d}", "metadata": {}}
        for i in range(total_events)
    ]
    events.append({"type": "assistant", "content": "Done.", "metadata": {}})
    events.append({"type": "done"})
    stub = StubAgentEnvConnector(events=events)

    with (
        patch("app.services.sessions.message_service.agent_env_connector", stub),
        patch(
            "app.services.improvement.session_snapshot_service.MAX_TOOL_DIGEST_ENTRIES", 5
        ),
    ):
        send_message(client, superuser_token_headers, session["id"], content="Do the thing.")
        drain_tasks()
        request = _submit(client, superuser_token_headers, session["id"])

    archive = _download(client, superuser_token_headers, request["id"])
    zf = zipfile.ZipFile(BytesIO(archive.content))
    snapshot = json.loads(zf.read("session/messages.json").decode())

    agent_messages = [m for m in snapshot["messages"] if m["role"] == "agent"]
    assert len(agent_messages) == 1
    digest = agent_messages[0]["tool_digest"]

    assert digest[0]["type"] == "omitted"
    omitted = total_events - 5
    assert str(omitted) in digest[0]["brief"]

    kept = digest[1:]
    assert len(kept) == 5
    kept_briefs = [d["brief"] for d in kept]
    expected = [f"call-{i:02d}" for i in range(total_events - 5, total_events)]
    assert kept_briefs == expected
