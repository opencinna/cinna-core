"""Unit tests: the adapter's 404-versus-everything-else split.

``EndpointUnsupportedError`` exists to carry ONE fact that a flattened
``Exception(str(e))`` destroys: the container answered, and it has no such
route. That is the only failure here whose remedy is a *rebuild* — a restart
re-runs the same image and can never grow a route — so the moment 404 and 500
share an exception type, every surface downstream has to guess which remedy to
name and is wrong half the time.

These tests pin the split at its source, on ``DockerEnvironmentAdapter``:

* ``get_skills_index`` and ``set_plugins`` raise ``EndpointUnsupportedError``
  on 404, tagged with the endpoint that was missing;
* **every other status** — and every transport failure — keeps raising the
  plain ``Exception`` it always raised. This is the half that can silently
  rot: widening the ``if`` to ``>= 400`` would make a 500 borrow the rebuild
  copy, and nothing else in the suite would notice.

No Docker daemon and no container: ``docker.from_env`` is stubbed (the adapter
builds a client in its constructor) and ``httpx.AsyncClient`` is replaced with
a fake that returns real ``httpx.Response`` objects, so the ``HTTPStatusError``
the code catches is the genuine one ``raise_for_status()`` produces rather than
a hand-rolled look-alike.

Notes:
  What the cache service does with the distinction (``adapter_unsupported`` vs
  ``adapter_error``, and its precedence over a sleeping environment) is in
  ``tests/unit/test_agent_skills_service.py``; the API-observable end of it is
  ``tests/api/agents/core/agents_pre_feature_env_test.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from app.services.environments.adapters.base import EndpointUnsupportedError
from app.services.environments.adapters.docker_adapter import (
    DockerEnvironmentAdapter,
)


# ── Test doubles ───────────────────────────────────────────────────────────


class _FakeAsyncClient:
    """Stands in for ``httpx.AsyncClient`` for one request.

    Returns a real ``httpx.Response`` so ``raise_for_status()`` raises the
    genuine ``httpx.HTTPStatusError`` — the exception the adapter branches on.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_body: dict | None = None,
        transport_error: Exception | None = None,
    ):
        self.status_code = status_code
        self.json_body = json_body if json_body is not None else {}
        self.transport_error = transport_error
        self.requests: list[str] = []

    def __call__(self, *args, **kwargs):
        # The adapter constructs the client (``httpx.AsyncClient()``); the
        # instance IS the factory so one object records the whole exchange.
        return self

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def _respond(self, method: str, url: str) -> httpx.Response:
        self.requests.append(url)
        if self.transport_error is not None:
            raise self.transport_error
        return httpx.Response(
            self.status_code,
            json=self.json_body,
            request=httpx.Request(method, url),
        )

    async def get(self, url: str, **_kwargs) -> httpx.Response:
        return await self._respond("GET", url)

    async def post(self, url: str, **_kwargs) -> httpx.Response:
        return await self._respond("POST", url)


@pytest.fixture
def adapter(monkeypatch) -> DockerEnvironmentAdapter:
    """A Docker adapter with no Docker behind it."""
    monkeypatch.setattr(
        "docker.from_env", lambda *args, **kwargs: MagicMock(), raising=True
    )
    return DockerEnvironmentAdapter(
        env_id=uuid.uuid4(),
        env_dir=Path("/tmp/does-not-exist"),
        port=8000,
        auth_token="test-token",
    )


@pytest.fixture
def http(monkeypatch):
    """Install a fake ``httpx.AsyncClient`` and hand it back for assertions."""

    def _install(**kwargs) -> _FakeAsyncClient:
        fake = _FakeAsyncClient(**kwargs)
        monkeypatch.setattr(httpx, "AsyncClient", fake)
        return fake

    return _install


# ── GET /config/skills ─────────────────────────────────────────────────────


class TestSkillsIndex:

    @pytest.mark.anyio
    async def test_a_reachable_container_returns_the_body(self, adapter, http):
        """The control: without this the failure tests could pass vacuously."""
        fake = http(json_body={"hash": "h1", "skills": [], "errors": []})

        payload = await adapter.get_skills_index()

        assert payload == {"hash": "h1", "skills": [], "errors": []}
        assert fake.requests and fake.requests[0].endswith("/config/skills")

    @pytest.mark.anyio
    async def test_a_404_is_endpoint_unsupported_and_names_the_route(
        self, adapter, http
    ):
        http(status_code=404)

        with pytest.raises(EndpointUnsupportedError) as excinfo:
            await adapter.get_skills_index()

        # The endpoint travels on the exception: a caller that has to re-parse
        # a message string to learn which route was missing is back where it
        # started.
        assert excinfo.value.endpoint == "/config/skills"
        assert "/config/skills" in str(excinfo.value)

    @pytest.mark.anyio
    @pytest.mark.parametrize("status_code", [500, 502, 403, 409])
    async def test_any_other_status_stays_a_plain_failure(
        self, adapter, http, status_code
    ):
        """A container that HAS the route and failed inside it is not "too old".

        This is the assertion that stops the 404 branch widening: a 500 means
        the endpoint ran and broke, and telling that user to rebuild sends them
        through minutes of container recreation for a fault a rebuild does not
        touch.
        """
        http(status_code=status_code)

        with pytest.raises(Exception) as excinfo:
            await adapter.get_skills_index()

        assert not isinstance(excinfo.value, EndpointUnsupportedError), (
            f"HTTP {status_code} must not borrow the rebuild remedy"
        )
        assert "Failed to get skills index" in str(excinfo.value)

    @pytest.mark.anyio
    async def test_a_transport_failure_stays_a_plain_failure(self, adapter, http):
        """No answer at all is "unreachable", which a restart can fix."""
        http(transport_error=httpx.ConnectError("connection refused"))

        with pytest.raises(Exception) as excinfo:
            await adapter.get_skills_index()

        assert not isinstance(excinfo.value, EndpointUnsupportedError)
        assert "Failed to get skills index" in str(excinfo.value)


# ── POST /config/plugins ───────────────────────────────────────────────────


class TestSetPlugins:

    @pytest.mark.anyio
    async def test_a_reachable_container_returns_the_install_results(
        self, adapter, http
    ):
        fake = http(
            json_body={
                "results": [
                    {
                        "plugin_name": "reporting",
                        "marketplace_name": "tools",
                        "source": "marketplace",
                        "status": "installed",
                        "error_message": None,
                    }
                ]
            }
        )

        results = await adapter.set_plugins({"plugins": []})

        assert [r["plugin_name"] for r in results] == ["reporting"]
        assert fake.requests and fake.requests[0].endswith("/config/plugins")

    @pytest.mark.anyio
    async def test_a_404_is_endpoint_unsupported_and_names_the_route(
        self, adapter, http
    ):
        http(status_code=404)

        with pytest.raises(EndpointUnsupportedError) as excinfo:
            await adapter.set_plugins({"plugins": []})

        assert excinfo.value.endpoint == "/config/plugins"

    @pytest.mark.anyio
    @pytest.mark.parametrize("status_code", [500, 502, 403])
    async def test_any_other_status_stays_a_plain_failure(
        self, adapter, http, status_code
    ):
        http(status_code=status_code)

        with pytest.raises(Exception) as excinfo:
            await adapter.set_plugins({"plugins": []})

        assert not isinstance(excinfo.value, EndpointUnsupportedError), (
            f"HTTP {status_code} must not be counted as unsupported"
        )
        assert "Failed to set plugins" in str(excinfo.value)

    @pytest.mark.anyio
    async def test_a_transport_failure_stays_a_plain_failure(self, adapter, http):
        http(transport_error=httpx.ConnectError("connection refused"))

        with pytest.raises(Exception) as excinfo:
            await adapter.set_plugins({"plugins": []})

        assert not isinstance(excinfo.value, EndpointUnsupportedError)


# ── The export surface ─────────────────────────────────────────────────────


def test_the_error_is_exported_from_the_adapters_package():
    """Callers import it from ``adapters``; the re-export is part of the API."""
    from app.services.environments import adapters

    assert adapters.EndpointUnsupportedError is EndpointUnsupportedError
    assert "EndpointUnsupportedError" in adapters.__all__
