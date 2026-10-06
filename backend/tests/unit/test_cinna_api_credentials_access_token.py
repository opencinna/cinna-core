"""Unit tests: container SDK ``credentials.access_token`` + atomic credentials.json write (Phase 3).

Plan: docs/plans/oauth_credential_refresh_plan.md §3, §6.4. Pattern:
test_cinna_api_credentials_slots.py. The platform HTTP call (``urllib.request.urlopen``)
is faked; nothing touches the network.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

import core.cinna_api  # noqa: F401 (load the shadowed credentials submodule)

credentials_module = sys.modules["core.cinna_api.credentials"]
creds = credentials_module.credentials
LOCAL = "ya29.local-token"
REMOTE = "ya29.remote-token"
CID = "11111111-1111-1111-1111-111111111111"
_APP_CORE_BASE = Path(__file__).parents[2] / "app" / "env-templates" / "app_core_base"


def _write(tmp_path, monkeypatch, expires_in: float | None, token: str | None = LOCAL) -> None:
    data = {"refresh_token_is_never_here": False}
    if token:
        data["access_token"] = token
    if expires_in is not None:
        data["expires_at"] = time.time() + expires_in
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps([{"id": CID, "type": "gmail_oauth", "credential_data": data}]))
    monkeypatch.setattr(credentials_module, "_CREDENTIALS_PATH", path)


@pytest.fixture
def platform_env(monkeypatch):
    monkeypatch.setenv("BACKEND_URL", "http://backend.test")
    monkeypatch.setenv("AGENT_AUTH_TOKEN", "env-jwt")
    monkeypatch.setenv("ENV_ID", "env-123")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(monkeypatch, *, body=None, status=200, error_body=None, raises=None):
    calls = []

    def fake(request, timeout=None):
        calls.append((request, timeout))
        if raises:
            raise raises
        if status != 200:
            raise urllib.error.HTTPError(
                request.full_url, status, "err", {}, io.BytesIO(error_body or b"")
            )
        return _Resp(json.dumps(body).encode())

    monkeypatch.setattr(credentials_module.urllib.request, "urlopen", fake)
    return calls


def test_fresh_local_token_makes_no_http_call(tmp_path, monkeypatch, platform_env):
    _write(tmp_path, monkeypatch, expires_in=3600)
    calls = _fake_urlopen(monkeypatch, body={"access_token": REMOTE})
    assert creds.access_token(CID, min_ttl=300) == LOCAL
    assert calls == []


def test_unknown_credential_raises_not_linked(tmp_path, monkeypatch, platform_env):
    _write(tmp_path, monkeypatch, expires_in=3600)
    with pytest.raises(credentials_module.CredentialRefreshError) as ei:
        creds.access_token("nope")
    assert ei.value.code == "credential_not_linked"


def test_stale_token_calls_platform_with_both_headers(tmp_path, monkeypatch, platform_env):
    _write(tmp_path, monkeypatch, expires_in=30)
    calls = _fake_urlopen(monkeypatch, body={"access_token": REMOTE})
    assert creds.access_token(CID, min_ttl=300) == REMOTE
    (request, timeout), = calls
    assert request.full_url == f"http://backend.test/api/v1/agent/credentials/{CID}/access-token"
    assert request.get_method() == "POST"
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["authorization"] == "Bearer env-jwt"
    assert headers["x-agent-env-id"] == "env-123"
    body = json.loads(request.data)
    assert body["min_ttl"] == 300
    assert isinstance(body["known_expires_at"], int)  # expiry of the token the env holds
    assert timeout == 20


def test_409_raises_actionable_reauthorization_error(tmp_path, monkeypatch, platform_env):
    _write(tmp_path, monkeypatch, expires_in=30)
    _fake_urlopen(
        monkeypatch, status=409,
        error_body=json.dumps({"detail": {"code": "reauthorization_required", "message": "x"}}).encode(),
    )
    with pytest.raises(credentials_module.CredentialRefreshError) as ei:
        creds.access_token(CID)
    exc = ei.value
    assert exc.code == "reauthorization_required"
    assert exc.credential_id == CID
    assert "re-authorize" in str(exc) and CID in str(exc)
    assert LOCAL not in str(exc)


def test_env_vars_missing_unexpired_local_token_is_returned(tmp_path, monkeypatch):
    for v in ("BACKEND_URL", "AGENT_AUTH_TOKEN", "ENV_ID"):
        monkeypatch.delenv(v, raising=False)
    _write(tmp_path, monkeypatch, expires_in=100)  # below min_ttl, still valid
    calls = _fake_urlopen(monkeypatch, body={"access_token": REMOTE})
    assert creds.access_token(CID, min_ttl=300) == LOCAL
    assert calls == []


def test_env_vars_missing_expired_local_token_raises_unavailable(tmp_path, monkeypatch):
    for v in ("BACKEND_URL", "AGENT_AUTH_TOKEN", "ENV_ID"):
        monkeypatch.delenv(v, raising=False)
    _write(tmp_path, monkeypatch, expires_in=-10)
    with pytest.raises(credentials_module.CredentialRefreshError) as ei:
        creds.access_token(CID)
    assert ei.value.code == "unavailable"


def test_network_error_falls_back_to_unexpired_local_token(tmp_path, monkeypatch, platform_env):
    _write(tmp_path, monkeypatch, expires_in=100)
    _fake_urlopen(monkeypatch, raises=urllib.error.URLError("down"))
    assert creds.access_token(CID, min_ttl=300) == LOCAL


def test_network_error_with_expired_token_raises_unavailable(tmp_path, monkeypatch, platform_env):
    _write(tmp_path, monkeypatch, expires_in=-10)
    _fake_urlopen(monkeypatch, raises=urllib.error.URLError("down"))
    with pytest.raises(credentials_module.CredentialRefreshError) as ei:
        creds.access_token(CID)
    assert ei.value.code == "unavailable"


def test_module_imports_with_requests_absent():
    code = (
        "import sys\nsys.modules['requests'] = None\n"
        f"sys.path.insert(0, {str(_APP_CORE_BASE)!r})\n"
        "import core.cinna_api as m\n"
        "assert m.CredentialRefreshError\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_update_credentials_writes_atomically_without_tmp_leftover(tmp_path):
    from core.server.agent_env_service import AgentEnvService

    svc = AgentEnvService(str(tmp_path))
    svc.update_credentials([{"id": "a", "n": 1}], "# readme")
    svc.update_credentials([{"id": "b", "n": 2}], "# readme2")
    cdir = tmp_path / "credentials"
    assert json.loads((cdir / "credentials.json").read_text()) == [{"id": "b", "n": 2}]
    assert not (cdir / "credentials.json.tmp").exists()


def test_readme_oauth_blocks_use_access_token_helper():
    from app.services.credentials.credentials_service import CredentialsService

    entries = [
        {
            "id": f"id-{t}", "name": t, "type": t, "notes": None,
            "credential_data": {"access_token": "x"},
        }
        for t in ("gmail_oauth", "gdrive_oauth", "gcalendar_oauth_readonly", "gmeet_oauth", "gmeet_oauth_readonly")
    ]
    readme = CredentialsService.generate_credentials_readme(entries)
    for t in ("gmail_oauth", "gdrive_oauth", "gcalendar_oauth_readonly", "gmeet_oauth", "gmeet_oauth_readonly"):
        assert f"credentials.access_token('id-{t}'" in readme
    assert "from_authorized_user_info" not in readme
    # Google Meet section (read-write and read-only variants)
    assert "### Google Meet OAuth Credential: gmeet_oauth\n" in readme
    assert "### Google Meet OAuth Credential (Read-Only): gmeet_oauth_readonly" in readme
    assert "build('meet', 'v2', credentials=creds)" in readme
    assert "drive.meet.readonly" in readme
    assert "from core.cinna_api import credentials\n" in readme.replace("```", "\n")
    assert "CredentialRefreshError" not in readme.split("## Available Credentials")[1].split(
        "access_token("
    )[0]
    assert "AttributeError" in readme  # old-env hint: rebuild or use the HTTP fallback
    assert "/api/v1/agent/credentials/" in readme  # raw HTTP fallback section


@pytest.mark.parametrize("status", [401, 403])
def test_401_403_raise_unauthorized_without_local_fallback(
    tmp_path, monkeypatch, platform_env, status
):
    _write(tmp_path, monkeypatch, expires_in=100)  # unexpired local token exists
    _fake_urlopen(monkeypatch, status=status, error_body=b"{}")
    with pytest.raises(credentials_module.CredentialRefreshError) as ei:
        creds.access_token(CID, min_ttl=300)
    assert ei.value.code == "unauthorized"


def test_refresh_in_progress_is_retried_once_then_succeeds(tmp_path, monkeypatch, platform_env):
    _write(tmp_path, monkeypatch, expires_in=30)
    monkeypatch.setattr(credentials_module, "_IN_PROGRESS_RETRY_DELAY_SECONDS", 0)
    busy = json.dumps({"detail": {"code": "refresh_in_progress", "message": "x"}}).encode()
    seq = [("err", busy), ("ok", {"access_token": REMOTE})]
    calls = []

    def fake(request, timeout=None):
        calls.append(request)
        kind, payload = seq[len(calls) - 1]
        if kind == "err":
            raise urllib.error.HTTPError(request.full_url, 502, "e", {}, io.BytesIO(payload))
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr(credentials_module.urllib.request, "urlopen", fake)
    assert creds.access_token(CID) == REMOTE
    assert len(calls) == 2


def test_refresh_in_progress_twice_raises_after_exactly_one_retry(
    tmp_path, monkeypatch, platform_env
):
    _write(tmp_path, monkeypatch, expires_in=30)
    monkeypatch.setattr(credentials_module, "_IN_PROGRESS_RETRY_DELAY_SECONDS", 0)
    busy = json.dumps({"detail": {"code": "refresh_in_progress", "message": "x"}}).encode()
    calls = _fake_urlopen(monkeypatch, status=502, error_body=busy)
    with pytest.raises(credentials_module.CredentialRefreshError) as ei:
        creds.access_token(CID)
    assert ei.value.code == "refresh_in_progress"
    assert len(calls) == 2


def test_min_ttl_is_clamped_to_1800_in_the_request(tmp_path, monkeypatch, platform_env):
    _write(tmp_path, monkeypatch, expires_in=30)
    calls = _fake_urlopen(monkeypatch, body={"access_token": REMOTE})
    creds.access_token(CID, min_ttl=99999)
    assert json.loads(calls[0][0].data)["min_ttl"] == 1800


def test_atomic_write_failure_removes_tmp_and_reraises(tmp_path, monkeypatch):
    import os

    from core.server.agent_env_service import AgentEnvService

    svc = AgentEnvService(str(tmp_path))
    svc.update_credentials([{"id": "old"}], "# r")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(Exception):
        svc.update_credentials([{"id": "new"}], "# r")
    cdir = tmp_path / "credentials"
    assert not (cdir / "credentials.json.tmp").exists()
    assert json.loads((cdir / "credentials.json").read_text()) == [{"id": "old"}]
