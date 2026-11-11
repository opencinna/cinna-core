"""Voice transcription relay: WebSocket authentication.

Scenarios:
  1. Refused before accept (close 1008): no header, ``?token=`` alone, a bad
     token, and every scoped token type (guest, webapp, agent env, agent).
  2. A desktop device: session works, is ended with ``unauthorized`` when the
     device is revoked mid-session (re-check interval patched short), and a
     later open with the revoked token is refused with 1008.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.desktop_auth import obtain_desktop_tokens, revoke_desktop_client
from tests.utils.platform_token import mint_platform_token
from tests.utils.voice_transcription import (
    STREAM_URL,
    assert_closed_normally,
    enable_voice_transcription,
    expect_ws_rejected,
    gated_recheck,
    get_my_user_id,
    ledger_rows,
    open_session,
    outcomes,
    read_until_terminal,
    speech_ws,
    token_of,
)


def test_ws_refuses_missing_bad_and_scoped_tokens_before_accept(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    user_id = get_my_user_id(client, normal_user_token_headers)
    good = token_of(normal_user_token_headers)

    # ── No header, and a token only in the query string ──────────────
    assert expect_ws_rejected(client, STREAM_URL) == 1008
    assert expect_ws_rejected(client, f"{STREAM_URL}?token={good}") == 1008

    # ── Garbage / wrong scheme ────────────────────────────────────────
    assert expect_ws_rejected(client, STREAM_URL, {"Authorization": "Bearer nope"}) == 1008
    assert expect_ws_rejected(client, STREAM_URL, {"Authorization": f"Basic {good}"}) == 1008

    # ── Scoped tokens that carry a real user id still do not qualify ──
    for token_type in ("guest_share", "webapp_share", "agent_env", "agent"):
        scoped = mint_platform_token(user_id, extra_claims={"token_type": token_type})
        assert (
            expect_ws_rejected(client, STREAM_URL, {"Authorization": f"Bearer {scoped}"})
            == 1008
        ), token_type

    # ── Nobody got a ledger row: the sessions never started ───────────
    # (a good token is accepted: proves the refusals above were about auth)
    with speech_ws(client, good) as ws:
        ws.send_json({"type": "start"})  # off-contract on purpose
        assert ws.receive_json()["code"] == "bad_request"


def test_revoked_desktop_device_is_ended_mid_session_and_refused_at_open(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    db,
    speech,
) -> None:
    enable_voice_transcription(client, superuser_token_headers)
    user_id = get_my_user_id(client, normal_user_token_headers)
    desktop = obtain_desktop_tokens(client, normal_user_token_headers, device_name="Voice Mac")
    token = desktop["access_token"]

    with patch.object(settings, "VOICE_TRANSCRIPTION_REVOCATION_CHECK_SECONDS", 0.2), gated_recheck() as gate:
        # ── Phase 1: a healthy desktop session ────────────────────────
        with speech_ws(client, token) as ws:
            assert open_session(ws)["type"] == "start"
            ws.send_json({"type": "stop"})
            events = read_until_terminal(ws)
            assert events[-1] == {"type": "stop", "full_text": ""}
            assert_closed_normally(ws)

        # ── Phase 2: revoked while a session is live ──────────────────
        with speech_ws(client, token) as ws:
            assert open_session(ws)["type"] == "start"
            revoke_desktop_client(client, normal_user_token_headers, desktop["client_id"])
            gate.set()  # let the real re-check run now
            events = read_until_terminal(ws)
            assert len(events) == 1
            assert events[0]["type"] == "error"
            assert events[0]["code"] == "unauthorized"
            assert events[0]["recoverable"] is False
            assert_closed_normally(ws)

    # Provider was cancelled, not flushed.
    assert speech.stream.ends == 0

    # ── Phase 3: the revoked token is refused at the upgrade ─────────
    assert expect_ws_rejected(client, STREAM_URL, {"Authorization": f"Bearer {token}"}) == 1008

    # ── Ledger: both sessions recorded, attributed to the desktop ────
    rows = ledger_rows(db, user_id)
    assert [r["outcome"] for r in rows] == ["stop", "unauthorized"]
    assert {r["client_kind"] for r in rows} == {"desktop"}
    assert outcomes(db) == ["stop", "unauthorized"]


def test_recheck_failures_end_the_session_only_when_repeated(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    db,
    speech,
) -> None:
    """One failed re-check (e.g. a DB blip) keeps the session; two in a row
    end it with ``unauthorized``; a success in between resets the count."""
    enable_voice_transcription(client, superuser_token_headers)
    token = token_of(normal_user_token_headers)
    calls: list[str] = []
    script = iter(["error", "ok", "error", "ok", "error", "error"])

    def _recheck(principal) -> bool:
        step = next(script, "error")
        calls.append(step)
        if step == "error":
            raise RuntimeError("db blip")
        return True

    with (
        patch.object(settings, "VOICE_TRANSCRIPTION_REVOCATION_CHECK_SECONDS", 0.1),
        patch("app.api.routes.voice_transcription.recheck_speech_principal", _recheck),
        speech_ws(client, token) as ws,
    ):
        assert open_session(ws)["type"] == "start"
        events = read_until_terminal(ws)
        assert_closed_normally(ws)

    # Survived error / ok / error / ok, ended on the two consecutive errors.
    assert calls == ["error", "ok", "error", "ok", "error", "error"]
    assert len(events) == 1 and events[0]["code"] == "unauthorized"
    assert outcomes(db) == ["unauthorized"]
