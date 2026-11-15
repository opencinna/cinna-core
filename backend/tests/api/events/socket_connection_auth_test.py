"""The Socket.IO surface authenticates, and joins only rooms the caller owns.

The socket app is mounted publicly at ``/ws`` with ``cors_allowed_origins="*"``.
Before the fix these tests pin, ``connect`` read ``user_id`` out of the
client-supplied ``auth`` dict and joined that client to ``user_{user_id}`` with
no verification at all — so membership of a user's live event room (session
streams, task status changes, activities) was self-asserted by whoever could
reach the port. ``subscribe`` had the same gap one level down: it entered any
room by name.

Everything here goes over HTTP through the real mount using the polling
transport (``tests/utils/socket_client.py``), so these are ordinary API tests:
no handler closure is called directly, and a regression that breaks the ASGI
wiring fails them too.

Two properties are worth stating outright, because both are easy to
re-introduce:

1. **A client-supplied identity is ignored, not merely unverified.** The test
   below forges the ``user_id`` of a user who really exists, so it fails if the
   old shape is restored even partially — a check that only rejected
   *nonexistent* ids would pass a weaker test and still leak.
2. **There is no transition window.** ``{"user_id": ...}`` is refused outright
   rather than accepted alongside ``{"token": ...}``; an either-shape grace
   period would re-open the hole for exactly as long as it lasted, so the
   frontend change ships in the same commit.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.platform_token import mint_platform_token
from tests.utils.session import create_session_via_api
from tests.utils.socket_client import (
    SocketRejected,
    connect_socket,
    socket_is_accepted,
)
from tests.utils.user import create_random_user_with_headers
from tests.utils.webapp_share import authenticate_webapp_share, setup_webapp_agent

API = settings.API_V1_STR


def _raw_token(headers: dict[str, str]) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


def _me(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get(f"{API}/users/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _session_room(session_id: str) -> str:
    return f"session_{session_id}_stream"


@pytest.fixture
def member(client: TestClient):
    """An ordinary signed-up user, as ``(user, headers, raw_token)``.

    Deliberately not a superuser: a superuser can read every session, so it
    could not show the room gate refusing anything.
    """
    user, headers = create_random_user_with_headers(client)
    return user, headers, _raw_token(headers)


# ── The connect gate ────────────────────────────────────────────────────


def test_connect_without_any_auth_is_rejected(client: TestClient):
    assert socket_is_accepted(client, {}) is False
    assert socket_is_accepted(client, None) is False


def test_connect_with_a_forged_user_id_is_rejected(
    client: TestClient, member
):
    """The original hole: assert the server does not believe a bare user id.

    The forged id belongs to a user that really exists, so this cannot pass by
    accident on a server that merely rejects unknown ids.
    """
    user, _, _ = member

    assert socket_is_accepted(client, {"user_id": user["id"]}) is False


def test_connect_with_a_valid_user_token_is_accepted(
    client: TestClient, member
):
    _, _, token = member

    connect_socket(client, {"token": token})  # raises SocketRejected on failure


def test_connect_with_a_malformed_or_expired_token_is_rejected(
    client: TestClient, member
):
    user, _, _ = member

    assert socket_is_accepted(client, {"token": "not-a-jwt"}) is False
    assert socket_is_accepted(client, {"token": ""}) is False
    expired = mint_platform_token(user["id"], expires_delta=timedelta(minutes=-5))
    assert socket_is_accepted(client, {"token": expired}) is False
    # Stale or missing session version (``sv``): ended by a password change etc.
    stale = mint_platform_token(user["id"], extra_claims={"sv": 2})
    assert socket_is_accepted(client, {"token": stale}) is False
    legacy = mint_platform_token(user["id"], extra_claims={"sv": None})
    assert socket_is_accepted(client, {"token": legacy}) is False


def test_connect_with_a_well_signed_token_carrying_a_nonsense_sub_is_rejected(
    client: TestClient
):
    """Refusal, not a raised handler.

    A token signed with the real key but carrying a ``sub`` that is not a user
    id reaches ``session.get(User, sub)`` and fails in the driver rather than as
    an ``HTTPException``. Only someone holding ``SECRET_KEY`` can mint one, so
    this is robustness rather than a live attack — but an authentication gate
    that raises is a gate nobody can reason about.
    """
    assert socket_is_accepted(client, {"token": mint_platform_token("not-a-uuid")}) is False


def test_connect_with_an_agent_env_token_is_rejected(
    client: TestClient, member
):
    """An agent-environment token carries the owner's id in ``sub``.

    That is the same escalation ``deps.get_current_user`` guards against with
    its decode-time ``aud`` gate, and the socket has to inherit it: a single
    compromised container must not be able to hold the owner's event stream.
    Resolving the token through ``get_current_user`` rather than a second
    decode written for the socket is what makes that automatic.
    """
    user, _, _ = member

    env_token = mint_platform_token(
        user["id"],
        extra_claims={"aud": "agent_env", "token_type": "agent_env"},
    )
    assert socket_is_accepted(client, {"token": env_token}) is False


def test_connect_ignores_a_user_id_sent_alongside_a_valid_token(
    client: TestClient, member, superuser_token_headers: dict[str, str]
):
    """Identity comes from the token; a contradicting hint changes nothing."""
    _, _, token = member
    superuser = _me(client, superuser_token_headers)

    socket = connect_socket(
        client, {"token": token, "user_id": superuser["id"]}
    )

    ack = socket.emit("subscribe", {"room": f"user_{superuser['id']}"})
    assert ack["status"] == "error"


# ── The room gate ───────────────────────────────────────────────────────


def test_subscribe_to_own_user_room_is_allowed(client: TestClient, member):
    user, _, token = member
    socket = connect_socket(client, {"token": token})

    ack = socket.emit("subscribe", {"room": f"user_{user['id']}"})

    assert ack["status"] == "success"


def test_subscribe_to_another_users_room_is_refused(
    client: TestClient, member, superuser_token_headers: dict[str, str]
):
    _, _, token = member
    other = _me(client, superuser_token_headers)
    socket = connect_socket(client, {"token": token})

    ack = socket.emit("subscribe", {"room": f"user_{other['id']}"})

    assert ack["status"] == "error"
    assert "authorized" in ack["message"].lower()


def test_subscribe_to_an_unrecognised_room_is_refused(
    client: TestClient, member
):
    _, _, token = member
    socket = connect_socket(client, {"token": token})

    for room in ("", "broadcast", "session_not-a-uuid_stream", "admin"):
        ack = socket.emit("subscribe", {"room": room})
        assert ack["status"] == "error", room


def test_session_stream_room_is_readable_by_its_owner_and_nobody_else(
    client: TestClient, member, superuser_token_headers: dict[str, str]
):
    """A session id is the only secret protecting a live stream — it is not one.

    Both halves are asserted against the same room in the same test, so neither
    can pass for an unrelated reason: the owner is let in, and a second user
    holding a perfectly valid token of their own is refused. Before the fix both
    succeeded.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="Private Agent"
    )
    drain_tasks()
    chat_session = create_session_via_api(
        client, superuser_token_headers, agent["id"]
    )
    room = _session_room(chat_session["id"])

    _, _, stranger_token = member
    stranger_socket = connect_socket(client, {"token": stranger_token})
    owner_socket = connect_socket(
        client, {"token": _raw_token(superuser_token_headers)}
    )

    assert stranger_socket.emit("subscribe", {"room": room})["status"] == "error"
    assert owner_socket.emit("subscribe", {"room": room})["status"] == "success"


def test_subscribe_to_a_nonexistent_session_room_is_refused(
    client: TestClient, member
):
    _, _, token = member
    socket = connect_socket(client, {"token": token})

    ack = socket.emit(
        "subscribe",
        {"room": _session_room("00000000-0000-0000-0000-0000000000ff")},
    )

    assert ack["status"] == "error"


# ── The public webapp viewer ────────────────────────────────────────────


def test_webapp_viewer_connects_with_its_share_token(
    client: TestClient, superuser_token_headers: dict[str, str]
):
    """Public webapp chat is the second front end that holds a socket.

    Its JWT's ``sub`` is an ``AgentWebappShare`` id, not a user id, so it gets
    a principal kind of its own rather than being squeezed into the user path.
    Without this the fix would silently kill realtime on every shared webapp.
    """
    _, share = setup_webapp_agent(client, superuser_token_headers)
    viewer_token = authenticate_webapp_share(client, share["token"])["access_token"]

    connect_socket(client, {"token": viewer_token})


def test_webapp_viewer_is_refused_a_foreign_session_room(
    client: TestClient, superuser_token_headers: dict[str, str]
):
    """A share token authenticates, but it is not a key to the owner's sessions."""
    _, share = setup_webapp_agent(client, superuser_token_headers)
    viewer_token = authenticate_webapp_share(client, share["token"])["access_token"]

    owner_agent = create_agent_via_api(
        client, superuser_token_headers, name="Owner Only"
    )
    drain_tasks()
    owner_session = create_session_via_api(
        client, superuser_token_headers, owner_agent["id"]
    )

    socket = connect_socket(client, {"token": viewer_token})
    ack = socket.emit(
        "subscribe", {"room": _session_room(owner_session["id"])}
    )

    assert ack["status"] == "error"


def test_webapp_viewer_cannot_borrow_the_owners_user_room(
    client: TestClient, superuser_token_headers: dict[str, str]
):
    _, share = setup_webapp_agent(client, superuser_token_headers)
    viewer_token = authenticate_webapp_share(client, share["token"])["access_token"]
    owner = _me(client, superuser_token_headers)

    socket = connect_socket(client, {"token": viewer_token})
    ack = socket.emit("subscribe", {"room": f"user_{owner['id']}"})

    assert ack["status"] == "error"


def test_unsubscribe_before_subscribe_does_not_crash(
    client: TestClient, member
):
    """Leaving a room you were never in is harmless and must stay harmless."""
    _, _, token = member
    socket = connect_socket(client, {"token": token})

    ack = socket.emit("unsubscribe", {"room": "session_x_stream"})

    assert ack["status"] in {"success", "error"}


def test_a_rejected_connection_raises_rather_than_silently_degrading(
    client: TestClient,
):
    """Guard the helper itself, so the assertions above cannot go vacuous."""
    with pytest.raises(SocketRejected):
        connect_socket(client, {"user_id": "00000000-0000-0000-0000-000000000001"})
