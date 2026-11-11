"""Voice transcription relay: configuration, availability and provider lifecycle.

Scenarios:
  1. Off by default (``disabled``, ledger row, no discovery); on (works);
     provider force-deleted (``disabled`` again, discovery gone, provider null).
  2. ``PUT /admin/server-config`` validation reasons, partial updates, and an
     explicit ``null`` clearing the cap / provider.
  3. Deleting the selected provider: 409 with ``voice_transcription_in_use``,
     then forced.
  4. ``voice_transcription.updated`` is emitted on a voice change only.
"""
import uuid

from fastapi.testclient import TestClient

from tests.utils.voice_transcription import (
    API,
    assert_closed_normally,
    create_ai_provider,
    enable_voice_transcription,
    get_my_user_id,
    get_server_config,
    ledger_rows,
    open_session,
    put_server_config,
    read_until_terminal,
    speech_ws,
    token_of,
)

DISCOVERY = ("/.well-known/cinna-desktop", "/.well-known/cinna-app")


def _voice_events(socket_stub) -> list[dict]:
    return [
        e["data"]
        for e in socket_stub.emitted_events
        if e["data"].get("type") == "voice_transcription.updated"
    ]


def _dictate_once(client, headers) -> list[dict]:
    with speech_ws(client, token_of(headers)) as ws:
        ws.send_json({"type": "start", "version": 1, "locale": "en-US", "sample_rate": 16000, "replay_seconds": 0})
        first = ws.receive_json()
        if first["type"] == "start":
            ws.send_json({"type": "stop"})
            events = [first, *read_until_terminal(ws)]
        else:
            events = [first]
        assert_closed_normally(ws)
        return events


def test_relay_is_disabled_by_default_then_on_then_unavailable_when_provider_deleted(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    db,
    socket_stub,
) -> None:
    su, user = superuser_token_headers, normal_user_token_headers
    user_id = get_my_user_id(client, user)

    # ── Phase 1: off by default ──────────────────────────────────────
    events = _dictate_once(client, user)
    assert len(events) == 1
    assert events[0]["type"] == "error" and events[0]["code"] == "disabled"
    assert events[0]["recoverable"] is False and events[0]["message"]
    rows = ledger_rows(db, user_id)
    assert [r["outcome"] for r in rows] == ["disabled"]
    assert rows[0]["audio_seconds"] == 0
    for path in DISCOVERY:
        assert "voice_transcription" not in client.get(path).json()
    usage = client.get(f"{API}/speech/usage", headers=user).json()
    assert usage["enabled"] is False

    # ── Phase 2: switched on ─────────────────────────────────────────
    provider = enable_voice_transcription(client, su)
    for path in DISCOVERY:
        assert client.get(path).json()["voice_transcription"]["version"] == 1
    events = _dictate_once(client, user)
    assert [e["type"] for e in events] == ["start", "stop"]
    assert client.get(f"{API}/speech/usage", headers=user).json()["enabled"] is True

    # ── Phase 3: provider force-deleted ──────────────────────────────
    before = len(_voice_events(socket_stub))
    refused = client.delete(f"{API}/admin/ai-providers/{provider['id']}", headers=su)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["impact"]["voice_transcription_in_use"] is True
    r = client.delete(f"{API}/admin/ai-providers/{provider['id']}?force=true", headers=su)
    assert r.status_code == 200, r.text
    assert _voice_events(socket_stub)[before:][-1]["meta"] == {"available": False}

    config = get_server_config(client, su)
    assert config["voice_transcription_provider_id"] is None
    assert config["voice_transcription_enabled"] is True  # on, but nothing to serve it

    events = _dictate_once(client, user)
    assert len(events) == 1 and events[0]["code"] == "disabled"
    for path in DISCOVERY:
        assert "voice_transcription" not in client.get(path).json()
    assert client.get(f"{API}/speech/usage", headers=user).json()["enabled"] is False
    assert [r["outcome"] for r in ledger_rows(db, user_id)] == [
        "disabled", "stop", "disabled",
    ]


def test_server_config_validation_and_explicit_null(
    client: TestClient, superuser_token_headers: dict[str, str], normal_user_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers

    # ── Refusals, each with its machine-readable reason ──────────────
    r = put_server_config(client, su, voice_transcription_provider_id=str(uuid.uuid4()))
    assert r.status_code == 400 and r.json()["detail"] == "voice_transcription_provider_not_found"

    other = create_ai_provider(client, su, provider_type="openai", secret="sk-not-speech-capable")
    r = put_server_config(client, su, voice_transcription_provider_id=other["id"])
    assert r.status_code == 400
    assert r.json()["detail"] == "voice_transcription_provider_not_speech_capable"

    r = put_server_config(client, su, voice_transcription_enabled=True)
    assert r.status_code == 400 and r.json()["detail"] == "voice_transcription_provider_required"

    r = put_server_config(client, su, voice_transcription_monthly_minutes_per_user=0)
    assert r.status_code == 422
    assert put_server_config(client, normal_user_token_headers, voice_transcription_enabled=False).status_code == 403

    # Nothing above changed anything.
    config = get_server_config(client, su)
    assert config["voice_transcription_enabled"] is False
    assert config["voice_transcription_provider_id"] is None

    # ── A valid provider with a cap ──────────────────────────────────
    provider = enable_voice_transcription(client, su, cap_minutes=30)
    config = get_server_config(client, su)
    assert config["voice_transcription_enabled"] is True
    assert config["voice_transcription_provider_id"] == provider["id"]
    assert config["voice_transcription_monthly_minutes_per_user"] == 30

    # ── Omitted fields are left alone ────────────────────────────────
    r = put_server_config(client, su, voice_transcription_enabled=True)
    assert r.status_code == 200
    assert r.json()["voice_transcription_provider_id"] == provider["id"]
    assert r.json()["voice_transcription_monthly_minutes_per_user"] == 30

    # ── Explicit null clears the cap, not the provider ───────────────
    r = put_server_config(client, su, voice_transcription_monthly_minutes_per_user=None)
    assert r.status_code == 200
    assert r.json()["voice_transcription_monthly_minutes_per_user"] is None
    assert r.json()["voice_transcription_provider_id"] == provider["id"]

    # ── Clearing the provider while enabled is refused; off + null is fine
    r = put_server_config(client, su, voice_transcription_provider_id=None)
    assert r.status_code == 400 and r.json()["detail"] == "voice_transcription_provider_required"
    r = put_server_config(
        client, su, voice_transcription_enabled=False, voice_transcription_provider_id=None
    )
    assert r.status_code == 200
    assert r.json()["voice_transcription_provider_id"] is None
    assert r.json()["voice_transcription_enabled"] is False


def test_deleting_the_selected_provider_is_refused_until_forced(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    provider = enable_voice_transcription(client, su)
    unused = create_ai_provider(client, su, name="Spare Google", secret="AIzaSy-spare-key-0001")

    # An unselected provider with no members deletes without a confirm.
    assert client.delete(f"{API}/admin/ai-providers/{unused['id']}", headers=su).status_code == 200

    refused = client.delete(f"{API}/admin/ai-providers/{provider['id']}", headers=su)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "ai_provider_in_use"
    assert detail["impact"]["voice_transcription_in_use"] is True
    assert detail["impact"]["member_count"] == 0
    # Refusal changed nothing.
    assert get_server_config(client, su)["voice_transcription_provider_id"] == provider["id"]

    forced = client.delete(f"{API}/admin/ai-providers/{provider['id']}?force=true", headers=su)
    assert forced.status_code == 200
    assert get_server_config(client, su)["voice_transcription_provider_id"] is None


def test_voice_change_emits_updated_event_and_other_changes_do_not(
    client: TestClient, superuser_token_headers: dict[str, str], socket_stub
) -> None:
    su = superuser_token_headers

    r = put_server_config(client, su, landing_markdown="hello")
    assert r.status_code == 200
    assert _voice_events(socket_stub) == []

    provider = enable_voice_transcription(client, su)
    events = _voice_events(socket_stub)
    assert [e["meta"] for e in events] == [{"available": True}]

    # Same values again: nothing changed, nothing emitted.
    put_server_config(
        client, su, voice_transcription_enabled=True, voice_transcription_provider_id=provider["id"]
    )
    assert len(_voice_events(socket_stub)) == 1

    put_server_config(client, su, voice_transcription_monthly_minutes_per_user=10)
    put_server_config(client, su, voice_transcription_enabled=False)
    assert [e["meta"] for e in _voice_events(socket_stub)] == [
        {"available": True}, {"available": True}, {"available": False},
    ]
