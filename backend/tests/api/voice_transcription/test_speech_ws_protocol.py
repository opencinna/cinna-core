"""Voice transcription relay: protocol v1 behaviour (fake adapter, no network).

Scenarios:
  1. Stop flow: start event, partial/final relay in order, stop flushes the
     trailing final then ``stop`` with ``full_text``; close 1000; ledger row;
     the log line carries metadata only (no transcript, key or token).
  2. Cancel and client disconnect: nothing more is sent; ledger ``cancelled``.
  3. Bad first frames (no ledger row) and bad frames mid-session (ledger row).
  4. Provider failures pass their code through; finals already sent stay sent.
"""
from fastapi.testclient import TestClient

from app.services.voice_transcription.providers.base import Failed, Final, Partial
from tests.utils.voice_transcription import (
    FAKE_PROVIDER_KEY,
    assert_closed_normally,
    enable_voice_transcription,
    get_my_user_id,
    ledger_rows,
    open_session,
    outcomes,
    pcm,
    read_until_terminal,
    speech_ws,
    start_frame,
    token_of,
    capture_voice_logs,
)


def test_stop_flushes_finals_then_stop_with_full_text_and_logs_metadata_only(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    db,
    speech,
) -> None:
    enable_voice_transcription(client, superuser_token_headers)
    user_id = get_my_user_id(client, normal_user_token_headers)
    token = token_of(normal_user_token_headers)
    speech.script = [
        (3200, Partial("secretword")),
        (6400, Partial("secretword two")),
        (9600, Final("secretword two three")),
    ]
    speech.on_end = [Final("trailing sentence")]

    with capture_voice_logs() as logs, speech_ws(client, token) as ws:
        started = open_session(ws, locale="en-GB")
        assert started["type"] == "start"
        assert started["backend"] == "core-relay"
        assert started["session_id"]
        assert started["remaining_seconds"] is None
        for _ in range(4):
            ws.send_bytes(pcm(0.1))
        ws.send_json({"type": "stop"})
        events = read_until_terminal(ws)
        assert_closed_normally(ws)

    assert events == [
        {"type": "partial", "text": "secretword"},
        {"type": "partial", "text": "secretword two"},
        {"type": "final", "text": "secretword two three"},
        {"type": "final", "text": "trailing sentence"},
        {"type": "stop", "full_text": "secretword two three trailing sentence"},
    ]
    assert speech.open_locales == ["en-GB"]
    assert speech.open_keys == [FAKE_PROVIDER_KEY]
    assert speech.stream.ends == 1

    rows = ledger_rows(db, user_id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "stop"
    assert rows[0]["session_id"] == started["session_id"]
    assert rows[0]["audio_seconds"] == 4 * 3200 / 32000
    assert rows[0]["provider_type"] == "google" and rows[0]["model"] == "fake-live-1"
    assert rows[0]["client_kind"] == "web"

    # Positive first: the metadata line exists, so the absence checks mean something.
    finished = [m for m in logs if "voice transcription session finished" in m]
    assert len(finished) == 1
    assert "outcome=stop" in finished[0] and f"user_id={user_id}" in finished[0]
    assert "finals=2" in finished[0] and "partials=2" in finished[0]
    for message in logs:
        assert "secretword" not in message and "trailing" not in message
        assert FAKE_PROVIDER_KEY not in message and token not in message


def test_cancel_and_disconnect_send_nothing_more_and_are_recorded(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    db,
    speech,
) -> None:
    enable_voice_transcription(client, superuser_token_headers)
    token = token_of(normal_user_token_headers)
    speech.on_end = [Final("must never be sent")]

    # ── cancel ───────────────────────────────────────────────────────
    with speech_ws(client, token) as ws:
        assert open_session(ws)["type"] == "start"
        ws.send_bytes(pcm(0.1))
        ws.send_json({"type": "cancel"})
        assert_closed_normally(ws)
    assert speech.stream.ends == 0
    assert speech.stream.cancels >= 1

    # ── disconnect without a word ────────────────────────────────────
    with speech_ws(client, token) as ws:
        assert open_session(ws)["type"] == "start"
        ws.send_bytes(pcm(0.1))
    assert speech.stream.ends == 0

    assert outcomes(db) == ["cancelled", "cancelled"]


def test_bad_first_frames_and_bad_mid_session_frames_are_bad_request(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    db,
    speech,
) -> None:
    enable_voice_transcription(client, superuser_token_headers)
    token = token_of(normal_user_token_headers)

    def _expect_bad_request(ws) -> None:
        events = read_until_terminal(ws)
        assert len(events) == 1, events
        assert events[0]["type"] == "error" and events[0]["code"] == "bad_request"
        assert events[0]["recoverable"] is False and events[0]["message"]
        assert_closed_normally(ws)

    # ── First frame off-contract: no ledger row, no provider ─────────
    for bad in (
        start_frame(type="stop"),
        start_frame(version=2),
        start_frame(sample_rate=8000),
        start_frame(replay_seconds=-1),
        start_frame(locale="x" * 36),
        start_frame(locale="not a locale"),
        {"type": "start"},
    ):
        with speech_ws(client, token) as ws:
            ws.send_json(bad)
            _expect_bad_request(ws)
    with speech_ws(client, token) as ws:  # binary first frame
        ws.send_bytes(pcm(0.1))
        _expect_bad_request(ws)
    with speech_ws(client, token) as ws:  # text that is not JSON
        ws.send_text("hello")
        _expect_bad_request(ws)
    assert ledger_rows(db) == []
    assert speech.opens == 0

    # ── Off-contract mid-session: ledger row ─────────────────────────
    for send in (
        lambda ws: ws.send_json(start_frame()),  # second start
        lambda ws: ws.send_json({"type": "rewind"}),
        lambda ws: ws.send_text("not json"),
        lambda ws: ws.send_bytes(b"\x00" * (64 * 1024 + 2)),  # over 64 KB
    ):
        with speech_ws(client, token) as ws:
            assert open_session(ws)["type"] == "start"
            send(ws)
            _expect_bad_request(ws)
    assert outcomes(db) == ["bad_request"] * 4

    # A frame of exactly 64 KB is fine.
    with speech_ws(client, token) as ws:
        assert open_session(ws)["type"] == "start"
        ws.send_bytes(b"\x00" * 65536)
        ws.send_json({"type": "stop"})
        assert read_until_terminal(ws)[-1]["type"] == "stop"
        assert_closed_normally(ws)
    assert speech.stream.total_bytes == 65536


def test_provider_failures_pass_their_code_and_keep_sent_finals(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    db,
    speech,
) -> None:
    enable_voice_transcription(client, superuser_token_headers)
    token = token_of(normal_user_token_headers)

    for code, message in (
        ("unavailable", "The speech service is busy. Contact your admin."),
        ("failed", "The speech service rejected the request."),
    ):
        speech.script = [(3200, Final("kept text")), (6400, Failed(code, message))]
        with speech_ws(client, token) as ws:
            assert open_session(ws)["type"] == "start"
            ws.send_bytes(pcm(0.2))
            events = read_until_terminal(ws)
            assert_closed_normally(ws)
        assert events == [
            {"type": "final", "text": "kept text"},
            {"type": "error", "code": code, "message": message, "recoverable": False},
        ]

    # Provider refuses to open at all: unavailable, never its raw text.
    speech.script = []
    speech.open_error = RuntimeError("upstream said key AIzaSy-leaked-secret is bad")
    with speech_ws(client, token) as ws:
        ws.send_json(start_frame())
        events = read_until_terminal(ws)
        assert_closed_normally(ws)
    assert len(events) == 1
    assert events[0]["code"] == "unavailable"
    assert "leaked" not in events[0]["message"] and "AIza" not in events[0]["message"]

    assert outcomes(db) == ["unavailable", "failed", "unavailable"]
