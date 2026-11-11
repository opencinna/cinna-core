"""Voice transcription relay: replay, quota, concurrency, rate and timers.

Scenarios:
  1. Replay burst + live audio passes straight through; ``replay_seconds``
     is recorded.
  2. Monthly cap: remaining announced, mid-session cut (finals then
     ``quota_exceeded``), refused at start; ledger row each time.
  3. Second concurrent session is ``busy``; the 11th open in a minute is ``busy``.
  4. Idle timeout and session cap end like ``stop`` (timers patched short).
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.voice_transcription.providers.base import Final
from tests.utils.voice_transcription import (
    assert_closed_normally,
    enable_voice_transcription,
    get_my_user_id,
    ledger_rows,
    open_session,
    outcomes,
    pcm,
    read_until_terminal,
    send_audio,
    speech_ws,
    token_of,
)


def test_replay_burst_and_live_audio_pass_through_unthrottled(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    db,
    speech,
) -> None:
    enable_voice_transcription(client, superuser_token_headers)
    user_id = get_my_user_id(client, normal_user_token_headers)

    with speech_ws(client, token_of(normal_user_token_headers)) as ws:
        assert open_session(ws, replay_seconds=21)["type"] == "start"
        send_audio(ws, 21, frame_seconds=1.0)  # the burst, as fast as possible
        for _ in range(3):
            ws.send_bytes(pcm(0.1))  # live
        ws.send_json({"type": "stop"})
        assert read_until_terminal(ws)[-1]["type"] == "stop"
        assert_closed_normally(ws)

    stream = speech.stream
    assert stream.total_bytes == 21 * 32000 + 3 * 3200
    burst = stream.pushes[:21]
    assert burst[-1][1] - burst[0][1] < 1.0  # no pacing: 21 s of audio in < 1 s

    rows = ledger_rows(db, user_id)
    assert rows[0]["replay_seconds"] == 21.0
    assert rows[0]["audio_seconds"] == 21.3

    # Declared replay is clamped to the audio actually sent.
    with speech_ws(client, token_of(normal_user_token_headers)) as ws:
        assert open_session(ws, replay_seconds=500)["type"] == "start"
        ws.send_bytes(pcm(1))
        ws.send_json({"type": "stop"})
        read_until_terminal(ws)
    assert ledger_rows(db, user_id)[1]["replay_seconds"] == 1.0


def test_monthly_cap_announced_cut_mid_session_and_refused_at_start(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    db,
    speech,
) -> None:
    enable_voice_transcription(client, superuser_token_headers, cap_minutes=1)
    user_id = get_my_user_id(client, normal_user_token_headers)
    token = token_of(normal_user_token_headers)

    # ── Session 1: 30 of 60 s, then stop ─────────────────────────────
    with speech_ws(client, token) as ws:
        assert open_session(ws)["remaining_seconds"] == 60
        send_audio(ws, 30)
        ws.send_json({"type": "stop"})
        assert read_until_terminal(ws)[-1]["type"] == "stop"
        assert_closed_normally(ws)

    # ── Session 2: 40 s offered, only the remaining 30 s accepted ────
    speech.script = [(32000, Final("so far"))]
    speech.on_end = [Final("flushed")]
    with speech_ws(client, token) as ws:
        assert open_session(ws)["remaining_seconds"] == 30
        send_audio(ws, 40, frame_seconds=2.0)
        events = read_until_terminal(ws)
        assert_closed_normally(ws)
    assert events[:-1] == [
        {"type": "final", "text": "so far"},
        {"type": "final", "text": "flushed"},
    ]
    assert events[-1]["type"] == "error" and events[-1]["code"] == "quota_exceeded"
    assert speech.stream.total_bytes == 30 * 32000
    assert speech.stream.ends == 1

    # ── Session 3: nothing left, refused at start ────────────────────
    opens_before = speech.opens
    with speech_ws(client, token) as ws:
        ws.send_json({"type": "start", "version": 1, "locale": "en-US", "sample_rate": 16000, "replay_seconds": 0})
        events = read_until_terminal(ws)
        assert_closed_normally(ws)
    assert len(events) == 1 and events[0]["code"] == "quota_exceeded"
    assert speech.opens == opens_before

    rows = ledger_rows(db, user_id)
    assert [r["outcome"] for r in rows] == ["stop", "quota_exceeded", "quota_exceeded"]
    assert [r["audio_seconds"] for r in rows] == [30.0, 30.0, 0]

    usage = client.get("/api/v1/speech/usage", headers=normal_user_token_headers).json()
    assert usage["used_seconds"] == 60.0 and usage["cap_seconds"] == 60.0


def test_second_concurrent_session_is_busy_and_opens_are_rate_limited(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    db,
    speech,
) -> None:
    enable_voice_transcription(client, superuser_token_headers)
    token = token_of(normal_user_token_headers)

    # ── One live session per user ────────────────────────────────────
    with speech_ws(client, token) as first:
        assert open_session(first)["type"] == "start"
        with speech_ws(client, token) as second:
            second.send_json({"type": "start", "version": 1, "locale": "en-US", "sample_rate": 16000, "replay_seconds": 0})
            events = read_until_terminal(second)
            assert len(events) == 1 and events[0]["code"] == "busy"
            assert_closed_normally(second)
        # The first is unaffected.
        first.send_json({"type": "stop"})
        assert read_until_terminal(first)[-1]["type"] == "stop"
        assert_closed_normally(first)
    assert speech.opens == 1
    assert sorted(outcomes(db)) == ["busy", "stop"]

    # ── Slot was released: opens 2..10 in this minute succeed ────────
    for _ in range(8):  # 2 opens used so far (busy counted), 8 more = 10
        with speech_ws(client, token) as ws:
            assert open_session(ws)["type"] == "start"
            ws.send_json({"type": "cancel"})
            assert_closed_normally(ws)

    # ── The 11th open within a minute ────────────────────────────────
    with speech_ws(client, token) as ws:
        ws.send_json({"type": "start", "version": 1, "locale": "en-US", "sample_rate": 16000, "replay_seconds": 0})
        events = read_until_terminal(ws)
        assert_closed_normally(ws)
    assert len(events) == 1 and events[0]["code"] == "busy"
    assert outcomes(db)[-1] == "busy"
    assert speech.opens == 9


def test_idle_timeout_and_session_cap_end_like_stop(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    db,
    speech,
) -> None:
    enable_voice_transcription(client, superuser_token_headers)
    token = token_of(normal_user_token_headers)
    speech.on_end = [Final("trailing")]

    # ── Idle: no audio at all for 0.3 s ──────────────────────────────
    with patch.object(settings, "VOICE_TRANSCRIPTION_IDLE_TIMEOUT_SECONDS", 0.3):
        with speech_ws(client, token) as ws:
            assert open_session(ws)["type"] == "start"
            events = read_until_terminal(ws)  # no client action
            assert_closed_normally(ws)
    assert events == [
        {"type": "final", "text": "trailing"},
        {"type": "stop", "full_text": "trailing"},
    ]
    assert speech.stream.ends == 1

    # ── Session cap: 1 s of audio allowed, 2.5 s offered ─────────────
    with patch.object(settings, "VOICE_TRANSCRIPTION_MAX_SESSION_SECONDS", 1):
        with speech_ws(client, token) as ws:
            assert open_session(ws)["type"] == "start"
            send_audio(ws, 2.5, frame_seconds=0.5)
            events = read_until_terminal(ws)
            assert_closed_normally(ws)
    assert events[-1] == {"type": "stop", "full_text": "trailing"}
    assert speech.stream.total_bytes == 32000

    rows = ledger_rows(db, get_my_user_id(client, normal_user_token_headers))
    assert [r["outcome"] for r in rows] == ["stop", "stop"]
    assert rows[0]["audio_seconds"] == 0
    assert rows[1]["audio_seconds"] <= 1.0
