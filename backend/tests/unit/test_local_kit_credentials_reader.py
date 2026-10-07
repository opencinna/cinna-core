"""Kit credential reader (``templates/agent/scripts/cinna_credentials.py``).

Covers the reader-side half of Desktop credential delivery
(docs/application/desktop_credentials/README.md): a type/service_uri lookup
that matches several declared slots refuses to guess, and an attached entry the
host could not deliver (``unavailable_reason``) blocks the env fallback and is
named in ``require_*`` errors and in ``main()``'s report.

The script is located like ``test_local_kit_tool.py`` locates the kit: repo
``docs/local_agent_kit/`` first, then the synced snapshot under
``app/env-templates/.../knowledge/local-kit/`` (the only copy mounted in the
backend container). ``KIT_SOURCE`` names
which copy ran. The API side of the same contract lives in
``tests/api/external/test_desktop_credentials.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest


def _find_script() -> Path | None:
    rel = Path("templates") / "agent" / "scripts" / "cinna_credentials.py"
    override = os.environ.get("LOCAL_AGENT_KIT_DIR")
    if override:
        candidate = Path(override) / rel
        return candidate if candidate.is_file() else None
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "docs" / "local_agent_kit" / rel
        if candidate.is_file():
            return candidate
    snapshot = (
        here.parents[2]
        / "app"
        / "env-templates"
        / "platform-knowledge-env"
        / "app"
        / "workspace"
        / "knowledge"
        / "local-kit"
        / rel
    )
    return snapshot if snapshot.is_file() else None


SCRIPT = _find_script()
if SCRIPT is None:
    pytest.skip("cinna_credentials.py not found in any kit copy", allow_module_level=True)
KIT_SOURCE = "docs" if "local_agent_kit" in SCRIPT.parts else "snapshot"


@pytest.fixture
def reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Load a fresh copy of the reader rooted at a temporary agent folder."""
    spec = importlib.util.spec_from_file_location("cinna_credentials_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "credentials").mkdir()
    monkeypatch.setattr(module, "AGENT_ROOT", tmp_path)
    monkeypatch.setattr(module, "CLOUD_CREDENTIALS", tmp_path / "credentials" / "credentials.json")
    monkeypatch.setattr(module, "LOCAL_ENV", tmp_path / "credentials" / ".env")
    monkeypatch.setattr(module, "MANIFEST", tmp_path / "cinna-agent.json")
    monkeypatch.delenv("CINNA_CREDENTIALS_PATH", raising=False)
    for key in list(os.environ):
        if key.startswith(("GMAIL_", "ERP_", "CRM_", "API_TOKEN_")):
            monkeypatch.delenv(key)
    return module


def _write(reader, slots: list[dict], entries: list[dict]) -> None:
    reader.MANIFEST.write_text(json.dumps({"credentials": slots}))
    reader.CLOUD_CREDENTIALS.write_text(json.dumps(entries))


def test_script_source_is_resolved() -> None:
    assert SCRIPT.is_file()
    assert KIT_SOURCE in ("docs", "snapshot")


def test_ambiguous_type_lookup_raises_instead_of_guessing(reader) -> None:
    _write(
        reader,
        [
            {"name": "ERP", "type": "api_token", "service_uri": "erp"},
            {"name": "CRM", "type": "api_token", "service_uri": "crm"},
        ],
        [
            {"name": "ERP", "type": "api_token", "service_uri": "erp",
             "credential_data": {"http_header_value": "Bearer erp"}},
            {"name": "CRM", "type": "api_token", "service_uri": "crm",
             "credential_data": {"http_header_value": "Bearer crm"}},
        ],
    )
    with pytest.raises(reader.CredentialError) as exc:
        reader.require_credential("api_token")
    message = str(exc.value)
    assert "several declared slots" in message
    assert "'ERP'" in message and "'CRM'" in message
    # Slot names and unambiguous service_uris still resolve.
    assert reader.require_credential("CRM", "http_header_value") == "Bearer crm"
    assert reader.require_credential("erp", "http_header_value") == "Bearer erp"


def test_unique_type_lookup_still_resolves(reader) -> None:
    _write(
        reader,
        [{"name": "ERP", "type": "api_token", "service_uri": "erp"}],
        [{"name": "ERP", "type": "api_token", "service_uri": "erp",
          "credential_data": {"http_header_value": "Bearer erp"}}],
    )
    assert reader.require_credential("api_token", "http_header_value") == "Bearer erp"


def test_unavailable_reason_blocks_env_fallback_and_is_reported(
    reader, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        reader,
        [
            {"name": "Mailbox", "type": "email_imap", "service_uri": "mailbox",
             "env_prefix": "MAILBOX_"},
            {"name": "ERP", "type": "api_token", "service_uri": "erp"},
            {"name": "CRM", "type": "odoo", "service_uri": "crm"},
        ],
        [
            {"name": "Mailbox", "type": "email_imap", "service_uri": "mailbox",
             "credential_data": {}, "unavailable_reason": "local_use_not_allowed"},
            {"name": "ERP", "type": "api_token", "service_uri": "erp",
             "credential_data": {"http_header_value": "Bearer erp"}},
        ],
    )
    # A local .env value must not paper over an attached-but-undelivered entry.
    monkeypatch.setenv("MAILBOX_PASSWORD", "stale-local-token")

    assert reader.unavailable_reason("Mailbox") == (
        "its owner has not allowed use on this computer (local_use_not_allowed)"
    )
    assert reader.has_credential("Mailbox") is False
    with pytest.raises(reader.CredentialError) as exc:
        reader.require_credential("Mailbox")
    assert "is unavailable" in str(exc.value)
    assert "local_use_not_allowed" in str(exc.value)
    assert "stale-local-token" not in str(exc.value)

    with pytest.raises(reader.CredentialError) as exc:
        reader.require_slot("mailbox")
    assert "unavailable" in str(exc.value)
    assert "local_use_not_allowed" in str(exc.value)

    # A slot with no entry at all keeps the "not configured" wording.
    with pytest.raises(reader.CredentialError) as exc:
        reader.require_credential("CRM")
    assert "not configured" in str(exc.value)

    assert reader.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "Mailbox: unavailable: its owner has not allowed use on this computer "
        "(local_use_not_allowed)",
        "ERP: configured",
        "CRM: missing",
    ]
    assert not any("Bearer" in line or "stale-local" in line for line in out)


def test_unknown_unavailable_reason_uses_generic_hint(reader) -> None:
    _write(
        reader,
        [{"name": "ERP", "type": "api_token", "service_uri": "erp"}],
        [{"name": "ERP", "type": "api_token", "service_uri": "erp",
          "credential_data": {}, "unavailable_reason": "something_new"}],
    )
    with pytest.raises(reader.CredentialError) as exc:
        reader.require_slot("erp")
    assert "it could not be delivered (something_new)" in str(exc.value)


# ---------------------------------------------------------------------------
# Kit contract 1.7.0: issued agent_api keys + agent_api_session
# ---------------------------------------------------------------------------

import http.server
import threading
from contextlib import contextmanager


def test_helper_version_matches_contract_version_1_7_0(reader) -> None:
    assert reader.HELPER_VERSION == "1.7.0"
    if KIT_SOURCE == "docs":
        contract = (SCRIPT.parents[3] / "CONTRACT_VERSION").read_text().strip()
        assert contract == "1.7.0" == reader.HELPER_VERSION


@pytest.mark.parametrize(
    "reason,key_status,hint",
    [
        ("key_revoked", "revoked_by_issuer", "revoked"),
        ("key_revoked", "producer_removed", "revoked"),
        ("key_expired", "expired", "expired"),
        ("key_paused", "external_access_paused", "paused"),
        ("key_paused", "api_disabled", "paused"),
    ],
)
def test_unavailable_issued_key_raises_with_reason_and_key_status(
    reader, reason: str, key_status: str, hint: str
) -> None:
    _write(
        reader,
        [{"name": "CRM API", "type": "agent_api", "service_uri": "crm-api"}],
        [{"name": "CRM API", "type": "agent_api", "service_uri": "crm-api",
          "unavailable_reason": reason, "key_status": key_status,
          "credential_data": {"base_url": "https://x/api", "spec_url": "https://x/s",
                              "label": "k", "producer_agent_id": "p"}}],
    )
    expected_hints = {"revoked": "revoked", "expired": "expired", "paused": "paused"}
    for call in (
        lambda: reader.agent_api_session("crm-api"),
        lambda: reader.agent_api_session("CRM API"),
        lambda: reader.require_slot("crm-api"),
    ):
        with pytest.raises(reader.CredentialError) as exc:
            call()
        message = str(exc.value)
        assert "unavailable" in message
        assert reason in message and key_status in message
        assert expected_hints[hint] in message
    assert reader.has_credential("CRM API") is False


def test_agent_api_session_rejects_other_types_and_unconfigured_slots(reader) -> None:
    _write(
        reader,
        [{"name": "ERP", "type": "api_token", "service_uri": "erp"},
         {"name": "CRM API", "type": "agent_api", "service_uri": "crm-api"}],
        [{"name": "ERP", "type": "api_token", "service_uri": "erp",
          "credential_data": {"http_header_value": "Bearer x", "token": "t", "base_url": "u"}}],
    )
    with pytest.raises(reader.CredentialError) as exc:
        reader.agent_api_session("erp")
    assert "not 'agent_api'" in str(exc.value)
    with pytest.raises(reader.CredentialError) as exc:
        reader.agent_api_session("crm-api")
    assert "not configured" in str(exc.value)


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Records (path, Authorization) and serves a few redirect shapes."""

    seen: list[tuple[str, str | None]]
    cross_target: str = ""
    same_target: str = ""

    def log_message(self, *args) -> None:  # silence
        pass

    def _handle(self) -> None:
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        if self.path.endswith("/redir-same"):
            self.send_response(302)
            self.send_header("Location", type(self).same_target)
            self.end_headers()
            return
        if self.path.endswith("/redir-cross"):
            self.send_response(302)
            self.send_header("Location", type(self).cross_target)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        echoed = self.rfile.read(length).decode() if length else ""
        body = json.dumps({"path": self.path, "method": self.command, "body": echoed}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _handle


@contextmanager
def _server():
    handler = type("H", (_Recorder,), {"seen": []})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, handler
    finally:
        httpd.shutdown()
        httpd.server_close()


def _key_entry(base_url: str, token: str) -> dict:
    return {"name": "CRM API", "type": "agent_api", "service_uri": "crm-api",
            "credential_data": {"base_url": base_url, "token": token,
                                "spec_url": f"{base_url}/openapi.json",
                                "label": "k", "producer_agent_id": "p"}}


def test_agent_api_session_joins_paths_attaches_bearer_and_rereads_token(reader) -> None:
    with _server() as (httpd, handler):
        base = f"http://127.0.0.1:{httpd.server_port}/api/v1/agent-api/p"
        slots = [{"name": "CRM API", "type": "agent_api", "service_uri": "crm-api"}]
        _write(reader, slots, [_key_entry(base, "tok-1")])

        session = reader.agent_api_session("crm-api")
        assert session.base_url == base
        assert session.spec_url == f"{base}/openapi.json"

        assert session.get_json("orders", params={"q": "a b"}) == {
            "path": "/api/v1/agent-api/p/orders?q=a+b", "method": "GET", "body": "",
        }
        assert session.get("/leading/slash").ok
        out = session.post_json("orders", {"n": 1})
        assert out["method"] == "POST" and json.loads(out["body"]) == {"n": 1}
        assert [p for p, _ in handler.seen][:2] == [
            "/api/v1/agent-api/p/orders?q=a+b", "/api/v1/agent-api/p/leading/slash",
        ]
        assert {a for _, a in handler.seen} == {"Bearer tok-1"}

        # Rotation: a new token in the file is used by the SAME session object.
        reader.CLOUD_CREDENTIALS.write_text(json.dumps([_key_entry(base, "tok-2")]))
        session.get("ping")
        assert handler.seen[-1][1] == "Bearer tok-2"

        # A caller-supplied Authorization header never overrides the key.
        session.get("ping", headers={"Authorization": "Bearer evil"})
        assert handler.seen[-1][1] == "Bearer tok-2"


def test_agent_api_session_strips_authorization_on_cross_origin_redirect_keeps_same_origin(
    reader,
) -> None:
    with _server() as (a, a_h), _server() as (b, b_h):
        base = f"http://127.0.0.1:{a.server_port}/api/v1/agent-api/p"
        a_h.same_target = f"{base}/landed"
        a_h.cross_target = f"http://127.0.0.1:{b.server_port}/landing"
        _write(
            reader,
            [{"name": "CRM API", "type": "agent_api", "service_uri": "crm-api"}],
            [_key_entry(base, "tok-1")],
        )
        session = reader.agent_api_session("crm-api")

        same = session.get("redir-same")
        assert same.ok and same.json()["path"].endswith("/landed")
        assert [auth for path, auth in a_h.seen if path.endswith("/landed")] == ["Bearer tok-1"]

        cross = session.get("redir-cross")
        assert cross.ok
        assert b_h.seen == [("/landing", None)], "Authorization must not cross origins"
        # The first hop (same origin as base_url) did carry it.
        assert [auth for path, auth in a_h.seen if path.endswith("/redir-cross")] == ["Bearer tok-1"]

        # An absolute URL on another origin never receives the key either.
        session.get(f"http://127.0.0.1:{b.server_port}/direct")
        assert b_h.seen[-1] == ("/direct", None)


def test_agent_api_session_requests_mode(reader) -> None:
    requests = pytest.importorskip("requests")
    with _server() as (a, a_h), _server() as (b, b_h):
        base = f"http://127.0.0.1:{a.server_port}/api/v1/agent-api/p"
        a_h.cross_target = f"http://127.0.0.1:{b.server_port}/landing"
        _write(
            reader,
            [{"name": "CRM API", "type": "agent_api", "service_uri": "crm-api"}],
            [_key_entry(base, "tok-1")],
        )
        session = reader.agent_api_session("crm-api", requests=True)
        assert isinstance(session, requests.Session)
        assert session.base_url == base
        assert session.get("orders").status_code == 200
        assert a_h.seen[-1] == ("/api/v1/agent-api/p/orders", "Bearer tok-1")
        assert session.get("redir-cross").status_code == 200
        assert b_h.seen[-1] == ("/landing", None)


def test_agent_api_session_refuses_when_base_url_origin_changes_after_construction(
    reader,
) -> None:
    """The entry's base_url is re-read per request; a different origin than the
    one fixed at construction raises CredentialError and sends nothing."""
    with _server() as (a, a_h), _server() as (b, b_h):
        base = f"http://127.0.0.1:{a.server_port}/api/v1/agent-api/p"
        slots = [{"name": "CRM API", "type": "agent_api", "service_uri": "crm-api"}]
        _write(reader, slots, [_key_entry(base, "tok-1")])
        session = reader.agent_api_session("crm-api")
        assert session.get("ping").ok
        sent_before = len(a_h.seen)

        moved = f"http://127.0.0.1:{b.server_port}/api/v1/agent-api/p"
        reader.CLOUD_CREDENTIALS.write_text(json.dumps([_key_entry(moved, "tok-1")]))
        with pytest.raises(reader.CredentialError):
            session.get("ping")
        assert len(a_h.seen) == sent_before
        assert b_h.seen == []

        # A same-origin path change (e.g. a different producer id) is allowed.
        same_origin = f"http://127.0.0.1:{a.server_port}/api/v1/agent-api/other"
        reader.CLOUD_CREDENTIALS.write_text(json.dumps([_key_entry(same_origin, "tok-1")]))
        assert session.get("ping").ok
