"""Fixtures for the voice transcription relay tests.

Every test gets: ``create_session`` patched onto the test transaction (the
relay opens its own short sessions), fresh in-process trackers, a captured
Socket.IO, and a ``FakeSpeechAdapter`` serving every ``google`` provider so
nothing can reach a provider. Tests tune the fake via ``speech`` before
connecting.
"""
from unittest.mock import patch

import pytest

from tests.stubs.socketio_stub import StubSocketIOConnector
from tests.stubs.speech_adapter_stub import FakeSpeechAdapter
from tests.utils.fixtures import CREATE_SESSION_TARGETS_BASE, patched_create_sessions
from tests.utils.network_guard import no_outbound_http
from tests.utils.voice_transcription import speech_adapter_override


@pytest.fixture(autouse=True)
def _relay_environment(db):
    from app.services.voice_transcription import tracker

    tracker.reset_for_tests()
    with patched_create_sessions(db, CREATE_SESSION_TARGETS_BASE), no_outbound_http():
        yield
    tracker.reset_for_tests()


@pytest.fixture(autouse=True)
def socket_stub():
    stub = StubSocketIOConnector()
    with patch("app.services.events.event_service.socketio_connector", stub):
        yield stub


@pytest.fixture(autouse=True)
def speech():
    """The fake adapter; replace attributes (``script``, ``on_end`` ...) as needed."""
    adapter = FakeSpeechAdapter()
    with speech_adapter_override(adapter):
        yield adapter
