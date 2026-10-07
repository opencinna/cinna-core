"""Unit tests: the container SDK's credential-slot accessor (C7).

``core/cinna_api/credentials.py`` is the vendored, network-free accessor a
workspace or skill script uses to find its credential by **slot** (the
credential's ``service_uri``). See
docs/plans/skill_credential_requirements_plan.md §4 (C6/C7), §7.9.

Import trap (verified by code review, reproduced here so the fix does not
regress): ``core/cinna_api/__init__.py`` does
``from .credentials import CredentialMissing, credentials`` — which REBINDS
the ``credentials`` attribute of the ``core.cinna_api`` package to the
*singleton instance*, shadowing the submodule object Python's import
machinery would otherwise have placed there. Consequently both
``import core.cinna_api.credentials as m`` and
``monkeypatch.setattr("core.cinna_api.credentials._CREDENTIALS_PATH", ...)``
resolve the attribute chain ``core.cinna_api.credentials`` and land on the
*instance*, not the module — so a plain ``import core.cinna_api.credentials``
followed by attribute access, or a dotted monkeypatch target string, silently
patches (or reads) the wrong object. The only path that reaches the real
module is ``sys.modules["core.cinna_api.credentials"]`` taken **after**
``core.cinna_api`` (the package) has been imported.

The API-observable path — credentials.json's top-level ``service_uri`` /
``is_placeholder`` fields this module reads — is covered end to end in
``tests/api/credentials/test_credential_service_uri_env_sync.py``.
"""
from __future__ import annotations

import json
import pickle
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from app.services.agent_api.agent_api_identity_service import OWNER_IDENTITY_TYPE
from app.services.users.user_details_service import CURRENT_USER_TYPE

# ---------------------------------------------------------------------------
# Reach the real submodule, not the shadowed package attribute (see the
# module docstring). ``app_core_base`` is already on sys.path via
# tests/unit/conftest.py, the same seam test_skill_manifest.py uses to reach
# the vendored parser.
# ---------------------------------------------------------------------------

_APP_CORE_BASE = (
    Path(__file__).parents[2] / "app" / "env-templates" / "app_core_base"
)

import core.cinna_api  # noqa: E402, F401 (load the shadowed credentials submodule)

credentials_module = sys.modules["core.cinna_api.credentials"]


def _write_credentials(tmp_path: Path, monkeypatch, entries: list[dict]) -> Path:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(credentials_module, "_CREDENTIALS_PATH", path)
    return path


# ---------------------------------------------------------------------------
# by_slot
# ---------------------------------------------------------------------------


class TestBySlot:

    def test_hit_returns_the_matching_real_entry(self, tmp_path, monkeypatch):
        entries = [
            {"id": "1", "type": "api_token", "service_uri": "billing", "is_placeholder": False},
        ]
        _write_credentials(tmp_path, monkeypatch, entries)
        found = credentials_module.credentials.by_slot("billing")
        assert found is not None
        assert found["id"] == "1"

    def test_miss_returns_none(self, tmp_path, monkeypatch):
        _write_credentials(tmp_path, monkeypatch, [])
        assert credentials_module.credentials.by_slot("nope") is None

    def test_synthetic_entries_are_never_matched_even_with_a_colliding_slot(
        self, tmp_path, monkeypatch
    ):
        # A synthetic entry never carries service_uri in production; forcing
        # one here proves the type check excludes it BEFORE any slot
        # comparison, not merely that it lacks the key by accident.
        entries = [
            {
                "id": "current_user", "type": CURRENT_USER_TYPE,
                "service_uri": "billing", "credential_data": {},
            },
        ]
        _write_credentials(tmp_path, monkeypatch, entries)
        assert credentials_module.credentials.by_slot("billing") is None


# ---------------------------------------------------------------------------
# require_slot
# ---------------------------------------------------------------------------


class TestRequireSlot:

    @pytest.mark.parametrize("filled_first", [False, True])
    def test_filled_credential_wins_over_an_old_placeholder(
        self, tmp_path, monkeypatch, filled_first
    ):
        placeholder = {
            "id": "placeholder", "type": "api_token",
            "service_uri": "billing", "is_placeholder": True,
        }
        filled = {
            "id": "filled", "type": "api_token",
            "service_uri": "billing", "is_placeholder": False,
        }
        entries = [filled, placeholder] if filled_first else [placeholder, filled]
        _write_credentials(tmp_path, monkeypatch, entries)

        assert credentials_module.credentials.by_slot("billing")["id"] == "filled"
        assert credentials_module.credentials.require_slot("billing")["id"] == "filled"

    def test_not_linked_raises_with_the_exact_c7_message(self, tmp_path, monkeypatch):
        _write_credentials(tmp_path, monkeypatch, [])
        with pytest.raises(credentials_module.CredentialMissing) as exc_info:
            credentials_module.credentials.require_slot("erp-public-api")
        exc = exc_info.value
        assert exc.slot == "erp-public-api"
        assert exc.reason == "not_linked"
        assert str(exc) == (
            "credential_missing: no credential for slot 'erp-public-api' is "
            "linked to this agent. Fix: open the agent's Credentials tab and "
            "link a credential whose service URI (slot) is 'erp-public-api'."
        )

    def test_not_configured_raises_with_the_exact_c7_message(self, tmp_path, monkeypatch):
        entries = [
            {"id": "1", "type": "api_token", "service_uri": "billing", "is_placeholder": True},
        ]
        _write_credentials(tmp_path, monkeypatch, entries)
        with pytest.raises(credentials_module.CredentialMissing) as exc_info:
            credentials_module.credentials.require_slot("billing")
        exc = exc_info.value
        assert exc.slot == "billing"
        assert exc.reason == "not_configured"
        assert str(exc) == (
            "credential_missing: the credential for slot 'billing' is linked "
            "but not filled in yet. Fix: open the agent's Credentials tab and "
            "complete it."
        )

    def test_filled_slot_returns_the_entry(self, tmp_path, monkeypatch):
        entries = [
            {"id": "1", "type": "api_token", "service_uri": "billing", "is_placeholder": False},
        ]
        _write_credentials(tmp_path, monkeypatch, entries)
        entry = credentials_module.credentials.require_slot("billing")
        assert entry["id"] == "1"


# ---------------------------------------------------------------------------
# _SYNTHETIC_TYPES mirror
# ---------------------------------------------------------------------------


def test_synthetic_types_include_owner_identity_and_current_user_types():
    assert credentials_module._SYNTHETIC_TYPES >= {OWNER_IDENTITY_TYPE, CURRENT_USER_TYPE}


# ---------------------------------------------------------------------------
# CredentialMissing — pickling
# ---------------------------------------------------------------------------


class TestCredentialMissingPickling:

    def test_round_trips_through_pickle(self):
        exc = credentials_module.CredentialMissing("erp-public-api", "not_linked")
        restored = pickle.loads(pickle.dumps(exc))
        assert str(restored) == str(exc)
        assert restored.slot == "erp-public-api"
        assert restored.reason == "not_linked"


# ---------------------------------------------------------------------------
# agent_api_session
# ---------------------------------------------------------------------------


class TestAgentApiSessionConstruction:

    def test_new_accesses_observe_setup_refresh_and_revocation(self, tmp_path, monkeypatch):
        entry = {
            "id": "connection", "type": "agent_api", "service_uri": "erp",
            "is_placeholder": True, "credential_data": {},
        }
        path = _write_credentials(tmp_path, monkeypatch, [entry])
        with pytest.raises(credentials_module.CredentialMissing) as missing:
            credentials_module.credentials.agent_api_session("erp")
        assert missing.value.reason == "not_configured"

        for token in ("original-token", "refreshed-token"):
            entry.update(
                is_placeholder=False,
                credential_data={"base_url": "https://producer.example/api", "token": token},
            )
            path.write_text(json.dumps([entry]), encoding="utf-8")
            session = credentials_module.credentials.agent_api_session("erp")
            assert session.headers["Authorization"] == f"Bearer {token}"

        path.write_text("[]", encoding="utf-8")
        assert credentials_module.credentials.by_slot("erp") is None
        with pytest.raises(credentials_module.CredentialMissing) as missing:
            credentials_module.credentials.require_slot("erp")
        assert missing.value.reason == "not_linked"

    def test_sets_bearer_and_identity_headers(self, tmp_path, monkeypatch):
        entries = [
            {
                "id": "1", "name": "erp", "type": "agent_api",
                "service_uri": "erp-public-api", "is_placeholder": False,
                "credential_data": {
                    "base_url": "https://backend.internal/api/v1/agent-api/AGENT",
                    "spec_url": "https://backend.internal/api/v1/agent-api/AGENT/spec",
                    "token": "tok-abc",
                },
            },
            {
                "id": "owner_identity", "name": "Owner Identity Token",
                "type": OWNER_IDENTITY_TYPE,
                "credential_data": {"header": "X-Cinna-Caller-Identity", "token": "ident-tok"},
            },
        ]
        _write_credentials(tmp_path, monkeypatch, entries)

        session = credentials_module.credentials.agent_api_session("erp-public-api")

        assert session.base_url == "https://backend.internal/api/v1/agent-api/AGENT"
        assert session.spec_url == "https://backend.internal/api/v1/agent-api/AGENT/spec"
        assert session.headers["Authorization"] == "Bearer tok-abc"
        assert session.headers["X-Cinna-Caller-Identity"] == "ident-tok"

    def test_without_an_identity_entry_has_no_identity_header(self, tmp_path, monkeypatch):
        entries = [
            {
                "id": "1", "type": "agent_api", "service_uri": "erp",
                "is_placeholder": False,
                "credential_data": {"base_url": "https://x", "token": "t"},
            },
        ]
        _write_credentials(tmp_path, monkeypatch, entries)
        session = credentials_module.credentials.agent_api_session("erp")
        assert "X-Cinna-Caller-Identity" not in session.headers

    def test_wrong_type_raises_value_error_naming_the_type(self, tmp_path, monkeypatch):
        entries = [
            {"id": "1", "type": "api_token", "service_uri": "billing", "is_placeholder": False},
        ]
        _write_credentials(tmp_path, monkeypatch, entries)
        with pytest.raises(ValueError, match="not 'agent_api'"):
            credentials_module.credentials.agent_api_session("billing")

    def test_missing_slot_raises_credential_missing(self, tmp_path, monkeypatch):
        _write_credentials(tmp_path, monkeypatch, [])
        with pytest.raises(credentials_module.CredentialMissing):
            credentials_module.credentials.agent_api_session("erp-public-api")


# ---------------------------------------------------------------------------
# AgentApiSession — URL join + origin-scoped credential headers
# ---------------------------------------------------------------------------


def _capture_sent_requests(monkeypatch) -> list:
    """Patch ``requests.Session.send`` so no network call is ever made."""
    captured: list = []

    def _fake_send(_self, request, **_kwargs):
        captured.append(request)
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response.url = request.url
        return response

    monkeypatch.setattr(requests.Session, "send", _fake_send)
    return captured


def _producer_session(base_url: str = "https://producer.example.com/api"):
    session_class = credentials_module._get_agent_api_session_class()
    session = session_class(base_url=base_url, spec_url=None)
    session.trust_env = False
    session.set_credential_header("Authorization", "Bearer tok")
    session.set_credential_header("X-Cinna-Caller-Identity", "ident")
    return session


class TestAgentApiSessionUrlJoin:

    def test_relative_url_with_no_leading_slash_joins_with_exactly_one_slash(
        self, monkeypatch
    ):
        session = _producer_session()
        captured = _capture_sent_requests(monkeypatch)
        session.get("orders")
        assert captured[0].url == "https://producer.example.com/api/orders"

    def test_relative_url_with_a_leading_slash_joins_with_exactly_one_slash(
        self, monkeypatch
    ):
        session = _producer_session()
        captured = _capture_sent_requests(monkeypatch)
        session.get("/orders")
        assert captured[0].url == "https://producer.example.com/api/orders"

    def test_absolute_url_passes_through_unchanged(self, monkeypatch):
        session = _producer_session()
        captured = _capture_sent_requests(monkeypatch)
        session.get("https://other.example.com/x")
        assert captured[0].url == "https://other.example.com/x"


class TestAgentApiSessionOriginScopedHeaders:

    def test_relative_url_gets_both_credential_headers(self, monkeypatch):
        session = _producer_session()
        captured = _capture_sent_requests(monkeypatch)
        session.get("orders")
        assert captured[0].headers["Authorization"] == "Bearer tok"
        assert captured[0].headers["X-Cinna-Caller-Identity"] == "ident"

    def test_same_origin_absolute_url_case_and_default_port_still_get_headers(
        self, monkeypatch
    ):
        session = _producer_session()
        captured = _capture_sent_requests(monkeypatch)
        session.get("HTTPS://Producer.Example.com:443/api/orders")
        assert captured[0].headers.get("Authorization") == "Bearer tok"
        assert captured[0].headers.get("X-Cinna-Caller-Identity") == "ident"

    def test_a_different_host_drops_both_credential_headers(self, monkeypatch):
        session = _producer_session()
        captured = _capture_sent_requests(monkeypatch)
        session.get("https://evil.example.com/orders")
        assert "Authorization" not in captured[0].headers
        assert "X-Cinna-Caller-Identity" not in captured[0].headers

    def test_the_same_host_on_a_different_port_drops_both_headers(self, monkeypatch):
        session = _producer_session()
        captured = _capture_sent_requests(monkeypatch)
        session.get("https://producer.example.com:8443/orders")
        assert "Authorization" not in captured[0].headers
        assert "X-Cinna-Caller-Identity" not in captured[0].headers

    def test_the_same_host_over_plain_http_drops_both_headers(self, monkeypatch):
        session = _producer_session()
        captured = _capture_sent_requests(monkeypatch)
        session.get("http://producer.example.com/orders")
        assert "Authorization" not in captured[0].headers
        assert "X-Cinna-Caller-Identity" not in captured[0].headers

    def test_a_caller_passed_explicit_header_is_still_sent_cross_origin(self, monkeypatch):
        session = _producer_session()
        captured = _capture_sent_requests(monkeypatch)
        session.get(
            "https://evil.example.com/orders",
            headers={"Authorization": "Bearer explicit"},
        )
        assert captured[0].headers["Authorization"] == "Bearer explicit"
        assert "X-Cinna-Caller-Identity" not in captured[0].headers


# ---------------------------------------------------------------------------
# rebuild_auth — redirect handling
# ---------------------------------------------------------------------------


class TestRebuildAuthOnRedirect:
    """The producer-origin override of ``requests``' redirect-auth stripping.

    ``requests`` strips only ``Authorization``, and only on a host change; the
    caller-identity header would otherwise follow a redirect anywhere. Written
    LAST per instructions — depends on a sibling change (``rebuild_auth`` on
    ``AgentApiSession``) landing in ``core/cinna_api/credentials.py``.
    """

    def _prepared(self, url: str):
        return requests.Request(
            method="GET",
            url=url,
            headers={"Authorization": "Bearer tok", "X-Cinna-Caller-Identity": "ident"},
        ).prepare()

    def test_cross_origin_redirect_strips_both_credential_headers(self):
        session = _producer_session()
        prepared = self._prepared("https://evil.example.com/other")
        response = SimpleNamespace(request=SimpleNamespace(url=session.base_url))

        session.rebuild_auth(prepared, response)

        assert "Authorization" not in prepared.headers
        assert "X-Cinna-Caller-Identity" not in prepared.headers

    def test_same_origin_redirect_keeps_both_credential_headers(self):
        session = _producer_session()
        prepared = self._prepared("https://producer.example.com/api/other")
        response = SimpleNamespace(request=SimpleNamespace(url=session.base_url))

        session.rebuild_auth(prepared, response)

        assert prepared.headers["Authorization"] == "Bearer tok"
        assert prepared.headers["X-Cinna-Caller-Identity"] == "ident"


# ---------------------------------------------------------------------------
# Import cost — no requests at import time
# ---------------------------------------------------------------------------


class TestNoRequestsAtImportTime:

    def test_importing_core_cinna_api_does_not_import_requests(self):
        """Run in a fresh interpreter: other tests in this session may
        already have imported ``requests`` for unrelated reasons, which would
        make an in-process ``sys.modules`` check meaningless."""
        code = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(_APP_CORE_BASE)!r})\n"
            "import core.cinna_api\n"
            "print(json.dumps('requests' in sys.modules))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip()) is False


# ---------------------------------------------------------------------------
# Issued agent_api keys synced without their token (plan §5, D21)
# ---------------------------------------------------------------------------


def _unavailable_entry(reason: str, key_status: str, slot: str = "crm-api") -> dict:
    return {
        "id": f"key-{reason}",
        "type": "agent_api",
        "service_uri": slot,
        "is_placeholder": False,
        "unavailable_reason": reason,
        "key_status": key_status,
        "credential_data": {
            "base_url": "http://backend:8000/api/v1/agent-api/p",
            "spec_url": "http://backend:8000/api/v1/agent-api/p/openapi.json",
            "label": "CRM",
            "producer_agent_id": "p",
        },
    }


def _active_entry(slot: str = "crm-api") -> dict:
    return {
        "id": "key-active",
        "type": "agent_api",
        "service_uri": slot,
        "is_placeholder": False,
        "credential_data": {
            "base_url": "http://backend:8000/api/v1/agent-api/p",
            "spec_url": "http://backend:8000/api/v1/agent-api/p/openapi.json",
            "token": "secret-value",
            "label": "CRM",
            "producer_agent_id": "p",
        },
    }


class TestUnavailableIssuedKeys:

    @pytest.mark.parametrize(
        "reason,key_status",
        [
            ("key_revoked", "revoked_by_issuer"),
            ("key_revoked", "producer_removed"),
            ("key_expired", "expired"),
            ("key_paused", "external_access_paused"),
            ("key_paused", "api_disabled"),
        ],
    )
    def test_require_slot_raises_with_reason_and_key_status(
        self, tmp_path, monkeypatch, reason, key_status
    ):
        _write_credentials(
            tmp_path, monkeypatch, [_unavailable_entry(reason, key_status)]
        )
        with pytest.raises(credentials_module.CredentialMissing) as exc:
            credentials_module.credentials.require_slot("crm-api")
        assert exc.value.reason == reason
        assert exc.value.key_status == key_status
        assert exc.value.slot == "crm-api"
        message = str(exc.value)
        assert "crm-api" in message and reason in message and key_status in message

    def test_agent_api_session_raises_on_unavailable_entry(self, tmp_path, monkeypatch):
        _write_credentials(
            tmp_path, monkeypatch, [_unavailable_entry("key_expired", "expired")]
        )
        with pytest.raises(credentials_module.CredentialMissing) as exc:
            credentials_module.credentials.agent_api_session("crm-api")
        assert exc.value.reason == "key_expired"
        assert exc.value.key_status == "expired"

    def test_unavailable_entry_is_still_returned_by_by_slot_when_alone(
        self, tmp_path, monkeypatch
    ):
        _write_credentials(
            tmp_path, monkeypatch, [_unavailable_entry("key_paused", "api_disabled")]
        )
        found = credentials_module.credentials.by_slot("crm-api")
        assert found is not None and found["unavailable_reason"] == "key_paused"

    def test_available_entry_is_preferred_whatever_the_order(self, tmp_path, monkeypatch):
        unavailable = _unavailable_entry("key_revoked", "revoked_by_issuer")
        for entries in ([unavailable, _active_entry()], [_active_entry(), unavailable]):
            _write_credentials(tmp_path, monkeypatch, entries)
            assert credentials_module.credentials.by_slot("crm-api")["id"] == "key-active"
            assert credentials_module.credentials.require_slot("crm-api")["id"] == "key-active"

    def test_unavailable_beats_placeholder_and_exception_pickles(self, tmp_path, monkeypatch):
        placeholder = {
            "id": "ph", "type": "agent_api", "service_uri": "crm-api",
            "is_placeholder": True,
        }
        _write_credentials(
            tmp_path, monkeypatch,
            [placeholder, _unavailable_entry("key_revoked", "revoked_by_issuer")],
        )
        with pytest.raises(credentials_module.CredentialMissing) as exc:
            credentials_module.credentials.require_slot("crm-api")
        assert exc.value.reason == "key_revoked"
        clone = pickle.loads(pickle.dumps(exc.value))
        assert (clone.slot, clone.reason, clone.key_status) == (
            "crm-api", "key_revoked", "revoked_by_issuer",
        )


def test_unknown_unavailable_reason_message_names_the_reason(tmp_path, monkeypatch):
    entry = _unavailable_entry("key_from_the_future", "mystery_status")
    _write_credentials(tmp_path, monkeypatch, [entry])
    with pytest.raises(credentials_module.CredentialMissing) as exc:
        credentials_module.credentials.require_slot("crm-api")
    assert exc.value.reason == "key_from_the_future"
    assert "unavailable (key_from_the_future" in str(exc.value)
