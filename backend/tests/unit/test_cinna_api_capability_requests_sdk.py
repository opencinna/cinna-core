"""Container SDK: ``AgentApiSession`` capability-request helpers (URL, payload, error mapping)."""
import sys

import pytest

import core.cinna_api  # noqa: F401 (load the shadowed credentials submodule)

credentials_module = sys.modules["core.cinna_api.credentials"]
BASE = "http://backend:8000/api/v1/agent-api/abc"


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _session(monkeypatch, response, url=None):
    cls = credentials_module._get_agent_api_session_class()
    s = cls(BASE, f"{BASE}/openapi.json", url)
    calls = []

    def fake(method):
        def _call(u, **kw):
            calls.append((method, u, kw))
            return response
        return _call

    monkeypatch.setattr(s, "get", fake("GET"))
    monkeypatch.setattr(s, "post", fake("POST"))
    return s, calls


def test_url_derived_from_base_when_not_provided(monkeypatch):
    s, calls = _session(monkeypatch, _Resp(200, {"data": [{"id": "1"}], "count": 1}))
    assert s.capability_requests() == [{"id": "1"}]
    assert calls[0][:2] == ("GET", f"{BASE}/_cinna/improvement-requests")


def test_explicit_url_wins(monkeypatch):
    s, calls = _session(monkeypatch, _Resp(200, {"data": []}), url="http://x/y")
    s.capability_requests()
    assert calls[0][1] == "http://x/y"


def test_file_sends_only_given_fields_and_returns_row(monkeypatch):
    s, calls = _session(monkeypatch, _Resp(201, {"id": "9", "status": "new"}))
    out = s.file_capability_request("A title", "A need", spec_version_seen="1.2")
    assert out == {"id": "9", "status": "new"}
    method, url, kw = calls[0]
    assert method == "POST" and url.endswith("/_cinna/improvement-requests")
    assert kw["json"] == {"title": "A title", "need": "A need", "spec_version_seen": "1.2"}


@pytest.mark.parametrize("status", [401, 413, 422])
def test_refusals_map_to_value_error_with_detail(monkeypatch, status):
    s, _ = _session(monkeypatch, _Resp(status, {"detail": "nope detail"}))
    with pytest.raises(ValueError, match="nope detail"):
        s.file_capability_request("A title", "A need")


def test_422_list_detail_is_flattened(monkeypatch):
    detail = [{"loc": ["title"], "msg": "too short"}]
    s, _ = _session(monkeypatch, _Resp(422, {"detail": detail}))
    with pytest.raises(ValueError, match="title: too short"):
        s.file_capability_request("A title", "A need")


def test_429_surfaces_retry_after(monkeypatch):
    s, _ = _session(monkeypatch, _Resp(429, {"detail": "limit"}, {"Retry-After": "120"}))
    with pytest.raises(credentials_module.CapabilityRequestRateLimited) as ei:
        s.file_capability_request("A title", "A need")
    assert ei.value.retry_after == 120
    assert isinstance(ei.value, ValueError)


def test_429_without_header_has_no_retry_after(monkeypatch):
    s, _ = _session(monkeypatch, _Resp(429, {"detail": "limit"}))
    with pytest.raises(credentials_module.CapabilityRequestRateLimited) as ei:
        s.capability_requests()
    assert ei.value.retry_after is None


def test_other_errors_raise_via_raise_for_status(monkeypatch):
    s, _ = _session(monkeypatch, _Resp(500, {"detail": "boom"}))
    with pytest.raises(RuntimeError):
        s.capability_requests()
