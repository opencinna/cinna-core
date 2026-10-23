"""RobotsTagMiddleware in isolation: overwrite, unhandled 500, websocket passthrough.

The API-level coverage (every real surface) lives in
tests/api/indexing/test_robots_header.py; these cases need a hand-built app.
"""

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from app.core.robots_middleware import ROBOTS_DIRECTIVE, RobotsTagMiddleware


async def _tagged(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok", headers={"X-Robots-Tag": "all"})


async def _boom(_request: Request) -> PlainTextResponse:
    raise RuntimeError("boom")


async def _ws(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text("hi")
    await websocket.close()


def _client() -> TestClient:
    app = Starlette(
        routes=[
            Route("/tagged", _tagged),
            Route("/boom", _boom),
            WebSocketRoute("/ws", _ws),
        ]
    )
    app.add_middleware(RobotsTagMiddleware)
    return TestClient(app, raise_server_exceptions=False)


def test_overwrites_an_inner_value() -> None:
    r = _client().get("/tagged")
    assert r.headers.get_list("x-robots-tag") == [ROBOTS_DIRECTIVE]


def test_unhandled_exception_becomes_a_tagged_500() -> None:
    r = _client().get("/boom")
    assert r.status_code == 500
    assert r.headers["x-robots-tag"] == ROBOTS_DIRECTIVE


def test_websocket_passes_through() -> None:
    with _client().websocket_connect("/ws") as ws:
        assert ws.receive_text() == "hi"


def test_reraises_for_logging() -> None:
    with pytest.raises(RuntimeError):
        TestClient(_client().app).get("/boom")
