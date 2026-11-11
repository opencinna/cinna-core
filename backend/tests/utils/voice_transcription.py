"""Helpers for the voice transcription relay tests.

DOCUMENTED SEAMS (the only app-internal imports; Rule 1 keeps ``tests/api/``
itself off them): the speech adapter registry override, the in-process tracker
reset, the revocation-recheck gate, and a read of the usage ledger (the REST
surface exposes minutes and sessions but not ``outcome`` / ``replay_seconds``,
which the ledger-on-every-outcome checks need).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.models.voice_transcription.usage import VoiceTranscriptionUsage
from app.services.voice_transcription.providers import registry

API = settings.API_V1_STR
SERVER_CONFIG = f"{API}/admin/server-config"
PROVIDERS = f"{API}/admin/ai-providers"
STREAM_URL = f"{API}/speech/stream"
FAKE_PROVIDER_KEY = "AIzaSy-voice-test-key-not-a-real-key"
PCM_BYTES_PER_SECOND = 32_000
TERMINAL_TYPES = ("error", "stop")


# ── Admin setup ────────────────────────────────────────────────────────


def create_ai_provider(
    client: TestClient,
    headers: dict[str, str],
    *,
    provider_type: str = "google",
    kind: str = "fixed_key",
    name: str | None = None,
    secret: str = FAKE_PROVIDER_KEY,
) -> dict[str, Any]:
    r = client.post(
        f"{PROVIDERS}/",
        headers=headers,
        json={
            "name": name or f"Voice {provider_type} {secret[-6:]}",
            "kind": kind,
            "type": provider_type,
            "secret": secret,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def put_server_config(client: TestClient, headers: dict[str, str], **fields: Any):
    return client.put(SERVER_CONFIG, headers=headers, json=fields)


def enable_voice_transcription(
    client: TestClient,
    headers: dict[str, str],
    *,
    cap_minutes: int | None = None,
    provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a Google fixed-key provider, select it and switch the relay on."""
    provider = provider or create_ai_provider(client, headers)
    fields: dict[str, Any] = {
        "voice_transcription_enabled": True,
        "voice_transcription_provider_id": provider["id"],
    }
    if cap_minutes is not None:
        fields["voice_transcription_monthly_minutes_per_user"] = cap_minutes
    r = put_server_config(client, headers, **fields)
    assert r.status_code == 200, r.text
    return provider


def get_server_config(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    r = client.get(SERVER_CONFIG, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def get_my_user_id(client: TestClient, headers: dict[str, str]) -> str:
    r = client.get(f"{API}/users/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ── WebSocket ──────────────────────────────────────────────────────────


def token_of(headers: dict[str, str]) -> str:
    return headers["Authorization"].split(" ", 1)[1]


def speech_ws(client: TestClient, token: str):
    """``with speech_ws(client, token) as ws:`` — Bearer header, as the desktop does."""
    return client.websocket_connect(
        STREAM_URL, headers={"Authorization": f"Bearer {token}"}
    )


def start_frame(**overrides: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "start",
        "version": 1,
        "locale": "en-US",
        "sample_rate": 16000,
        "replay_seconds": 0,
    }
    frame.update(overrides)
    return frame


def pcm(seconds: float) -> bytes:
    return bytes(int(seconds * PCM_BYTES_PER_SECOND) // 2 * 2)


def open_session(ws, **start_overrides: Any) -> dict[str, Any]:
    """Send ``start`` and return the server's first event."""
    ws.send_json(start_frame(**start_overrides))
    return ws.receive_json()


def read_until_terminal(ws) -> list[dict[str, Any]]:
    """Every event up to and including the terminal one."""
    events: list[dict[str, Any]] = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] in TERMINAL_TYPES:
            return events


def assert_closed_normally(ws) -> None:
    """Nothing more arrives after the terminal event; the close is 1000."""
    try:
        extra = ws.receive_text()
    except WebSocketDisconnect as exc:
        assert exc.code == 1000, f"expected close 1000, got {exc.code}"
    else:
        raise AssertionError(f"unexpected frame after the terminal event: {extra!r}")


def send_audio(ws, seconds: float, *, frame_seconds: float = 1.0) -> None:
    remaining = seconds
    while remaining > 1e-9:
        chunk = min(frame_seconds, remaining)
        ws.send_bytes(pcm(chunk))
        remaining -= chunk


def expect_ws_rejected(client: TestClient, url: str, headers: dict[str, str] | None = None) -> int:
    """Connect to a socket the server refuses; return the close code."""
    try:
        with client.websocket_connect(url, headers=headers or {}) as ws:
            ws.receive_text()
    except WebSocketDisconnect as exc:
        return exc.code
    raise AssertionError("socket was accepted")


# ── Ledger ─────────────────────────────────────────────────────────────


def ledger_rows(db: Session, user_id: str | None = None) -> list[dict[str, Any]]:
    """The usage ledger, oldest first (a read-only documented DB seam)."""
    stmt = select(VoiceTranscriptionUsage).order_by(VoiceTranscriptionUsage.started_at)
    rows = db.exec(stmt).all()
    out = [
        {
            "user_id": str(r.user_id),
            "session_id": str(r.session_id),
            "audio_seconds": r.audio_seconds,
            "replay_seconds": r.replay_seconds,
            "provider_type": r.provider_type,
            "model": r.model,
            "client_kind": r.client_kind,
            "outcome": r.outcome,
        }
        for r in rows
    ]
    if user_id is not None:
        out = [r for r in out if r["user_id"] == user_id]
    return out


def outcomes(db: Session, user_id: str | None = None) -> list[str]:
    return [r["outcome"] for r in ledger_rows(db, user_id)]


# ── Seams ──────────────────────────────────────────────────────────────


@contextmanager
def speech_adapter_override(adapter) -> Iterator[Any]:
    """Serve every ``google`` provider with ``adapter`` (no network)."""
    with registry.override_for_tests("google", adapter):
        yield adapter


@contextmanager
def gated_recheck() -> Iterator[threading.Event]:
    """Make the mid-session revocation re-check wait for the test.

    While the gate is closed the re-check answers "still authorized" without
    touching the DB (so the test thread can revoke the device race-free); once
    the test sets the gate, the real re-check runs.
    """
    from app.api import deps

    gate = threading.Event()

    def _recheck(principal) -> bool:
        if not gate.is_set():
            return True
        return deps.recheck_speech_principal(principal)

    with patch("app.api.routes.voice_transcription.recheck_speech_principal", _recheck):
        yield gate


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def capture_voice_logs() -> Iterator[list[str]]:
    """Capture the relay's log lines on a handler attached to the logger itself.

    ``caplog`` is unusable here: ``alembic upgrade`` in ``setup_db`` disables
    every pre-existing app logger for the rest of the session (see
    ``tests/README.md``). Callers must positively assert the line they expect
    was captured, so a disabled logger cannot make an absence check vacuous.
    """
    loggers = [
        logging.getLogger("app.services.voice_transcription.session"),
        logging.getLogger("app.api.routes.voice_transcription"),
    ]
    handler = _Capture()
    saved = [(lg, lg.disabled, lg.level) for lg in loggers]
    for lg in loggers:
        lg.disabled = False
        lg.setLevel(logging.DEBUG)
        lg.addHandler(handler)
    try:
        yield handler.messages
    finally:
        for lg, disabled, level in saved:
            lg.removeHandler(handler)
            lg.disabled = disabled
            lg.setLevel(level)
