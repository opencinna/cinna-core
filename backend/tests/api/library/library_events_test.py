"""Library realtime hints and audit trail.

Accepted push -> ``library.updated`` to each reader's ``user_<id>`` room only
and a ``LIBRARY_PUSH_ACCEPTED`` security event; refused push -> neither.
Grant changes -> ``library.granted`` to newly gained readers only.
"""
from fastapi.testclient import TestClient

from tests.utils.library import (
    GitClient,
    create_library_agent,
    everyone_grant,
    make_person,
    put_grants,
    user_grant,
    write_agent_tree,
)
from tests.utils.security_event import events_of_type


def _emits(stub, event_type: str) -> list[dict]:
    return [
        e for e in stub.emitted_events
        if e["event"] == "event" and e["data"]["type"] == event_type
    ]


def test_push_events_audit_and_refused_push_silence(
    client: TestClient, bridge, tmp_path, superuser_token_headers, socket_stub
) -> None:
    """
    1. Owner shares with one reader only (a bystander has no grant)
    2. Accepted push -> library.updated to owner + reader rooms, not bystander
    3. Refused push -> no further library.updated, no further audit row
    """
    owner = make_person(client, superuser_token_headers)
    reader = make_person(client, superuser_token_headers)
    bystander = make_person(client, superuser_token_headers)
    repo = create_library_agent(client, owner.headers)
    repo_id = repo["repo_id"]
    put_grants(
        client, owner.headers, repo_id,
        [user_grant(owner.id, "maintain"), user_grant(reader.id, "read")],
    )

    git = GitClient(tmp_path, token=owner.token)
    work = tmp_path / "w"
    assert git.clone(bridge.repo_url(repo_id), work).ok
    write_agent_tree(work, slug=repo["slug"])
    sha = git.commit_all(work, "first")
    socket_stub.emitted_events.clear()
    assert git.push(work).ok

    updated = _emits(socket_stub, "library.updated")
    rooms = {e["room"] for e in updated}
    assert rooms == {f"user_{owner.id}", f"user_{reader.id}"}
    assert f"user_{bystander.id}" not in rooms
    assert all(e["room"] for e in socket_stub.emitted_events)  # never a no-room broadcast
    meta = updated[0]["data"]["meta"]
    assert meta["repo_id"] == repo_id and meta["head_sha"] == sha
    assert meta["pushed_by"]["user_id"] == owner.id

    audit = events_of_type(client, owner.headers, "LIBRARY_PUSH_ACCEPTED")
    assert len(audit) == 1
    assert audit[0]["details"]["slug"] == repo["slug"]
    assert audit[0]["details"]["after"] == sha and audit[0]["details"]["before"] is None
    assert events_of_type(client, owner.headers, "LIBRARY_AGENT_CREATED")
    assert events_of_type(client, owner.headers, "LIBRARY_GRANTS_CHANGED")

    # ── refused push ──────────────────────────────────────────────────
    (work / ".env").write_text("K=1")
    git.commit_all(work, "secret")
    socket_stub.emitted_events.clear()
    assert not git.push(work).ok
    assert _emits(socket_stub, "library.updated") == []
    assert len(events_of_type(client, owner.headers, "LIBRARY_PUSH_ACCEPTED")) == 1


def test_granted_event_goes_only_to_newly_gained_readers(
    client: TestClient, superuser_token_headers, socket_stub
) -> None:
    owner = make_person(client, superuser_token_headers)
    first = make_person(client, superuser_token_headers)
    second = make_person(client, superuser_token_headers)
    repo_id = create_library_agent(client, owner.headers)["repo_id"]
    # Created without grants: creator only, nobody else is told.
    assert _emits(socket_stub, "library.granted") == []

    put_grants(
        client, owner.headers, repo_id,
        [user_grant(owner.id, "maintain"), user_grant(first.id, "read")],
    )
    assert {e["room"] for e in _emits(socket_stub, "library.granted")} == {f"user_{first.id}"}

    socket_stub.emitted_events.clear()
    put_grants(
        client, owner.headers, repo_id,
        [user_grant(owner.id, "maintain"), user_grant(first.id, "write"),
         user_grant(second.id, "read")],
    )
    granted = _emits(socket_stub, "library.granted")
    assert {e["room"] for e in granted} == {f"user_{second.id}"}
    assert granted[0]["data"]["meta"]["repo_id"] == repo_id

    socket_stub.emitted_events.clear()
    put_grants(client, owner.headers, repo_id, [user_grant(owner.id, "maintain")])
    assert _emits(socket_stub, "library.granted") == []
    _ = everyone_grant  # (everyone grants are covered in library_api_test)
