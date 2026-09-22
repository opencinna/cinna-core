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
