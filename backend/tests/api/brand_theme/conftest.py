from unittest.mock import patch

import pytest

from tests.stubs.socketio_stub import StubSocketIOConnector


@pytest.fixture(autouse=True)
def _fresh_brand_theme_limiter(monkeypatch):
    """The public limiter is module-global; give each test its own bucket."""
    from app.api.routes import brand_theme
    from app.services.common.rate_limiter import RateLimiter

    monkeypatch.setattr(brand_theme, "_public_limiter", RateLimiter())


@pytest.fixture
def socket_stub():
    """Capture Socket.IO emits (``emitted_events`` entries: event/data/room)."""
    stub = StubSocketIOConnector()
    with patch("app.services.events.event_service.socketio_connector", stub):
        yield stub
