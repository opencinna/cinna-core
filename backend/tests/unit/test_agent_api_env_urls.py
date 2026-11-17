"""``CredentialsService.agent_api_env_urls``: the single source of the agent_api
URL shape an env receives (env prep and the account CLI connections listing).

API-observable path: ``tests/api/cli/test_account_cli_agent_api_connections.py``.
"""
import copy
from urllib.parse import urlsplit

from app.core.config import settings
from app.services.credentials.credentials_service import (
    AGENT_API_CAPABILITY_REQUESTS_PATH,
    CredentialsService,
)

_DATA = {
    "base_url": "https://public.example.com/api/v1/agent-api/abc",
    "spec_url": "https://public.example.com/api/v1/agent-api/abc/openapi.json",
}


def test_rewrite_on_swaps_origin_and_derives_requests_url() -> None:
    internal = urlsplit(settings.AGENT_ENV_BACKEND_URL.rstrip("/"))
    out = CredentialsService.agent_api_env_urls(_DATA, rewrite_origin=True, usable=True)
    for field in ("base_url", "spec_url"):
        assert urlsplit(out[field]).netloc == internal.netloc
    assert out["base_url"].endswith("/api/v1/agent-api/abc")
    assert out["spec_url"].endswith("/abc/openapi.json")
    assert out["improvement_requests_url"] == (
        out["base_url"].rstrip("/") + AGENT_API_CAPABILITY_REQUESTS_PATH
    )


def test_rewrite_off_keeps_public_origin() -> None:
    out = CredentialsService.agent_api_env_urls(_DATA, rewrite_origin=False, usable=True)
    assert out["base_url"] == _DATA["base_url"]
    assert out["spec_url"] == _DATA["spec_url"]
    assert out["improvement_requests_url"] == (
        _DATA["base_url"] + AGENT_API_CAPABILITY_REQUESTS_PATH
    )


def test_not_usable_gets_no_requests_url() -> None:
    for rewrite in (True, False):
        out = CredentialsService.agent_api_env_urls(
            _DATA, rewrite_origin=rewrite, usable=False
        )
        assert "improvement_requests_url" not in out
        assert out["base_url"]


def test_trailing_slash_and_missing_base_url() -> None:
    out = CredentialsService.agent_api_env_urls(
        {"base_url": "https://h.example.com/x/"}, rewrite_origin=False, usable=True
    )
    assert out["improvement_requests_url"] == (
        "https://h.example.com/x" + AGENT_API_CAPABILITY_REQUESTS_PATH
    )
    empty = CredentialsService.agent_api_env_urls({}, rewrite_origin=True, usable=True)
    assert empty == {}


def test_input_is_not_mutated() -> None:
    original = copy.deepcopy(_DATA)
    data = dict(_DATA)
    CredentialsService.agent_api_env_urls(data, rewrite_origin=True, usable=True)
    CredentialsService.agent_api_env_urls(data, rewrite_origin=False, usable=True)
    assert data == original
