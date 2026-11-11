"""Voice transcription relay: usage endpoints and admin surface.

Scenarios:
  1. ``GET /speech/usage`` shape and ``GET /admin/voice-transcription/usage``
     sorting, totals and access control.
  2. ``GET /admin/voice-transcription/providers`` lists speech-capable only.
  3. ``POST /admin/voice-transcription/test`` maps ok / bad key / unreachable /
     disabled, never leaks the key, and is rate limited to 10/min.
"""
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.utils.user import create_random_user_with_headers
from tests.utils.voice_transcription import (
    API,
    FAKE_PROVIDER_KEY,
    create_ai_provider,
    enable_voice_transcription,
    open_session,
    read_until_terminal,
    send_audio,
    speech_ws,
    token_of,
)

ADMIN = f"{API}/admin/voice-transcription"


def _dictate(client, headers, seconds: float) -> None:
    with speech_ws(client, token_of(headers)) as ws:
        assert open_session(ws)["type"] == "start"
        send_audio(ws, seconds)
        ws.send_json({"type": "stop"})
        assert read_until_terminal(ws)[-1]["type"] == "stop"


def test_user_and_admin_usage_reports(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    speech,
) -> None:
    su, user = superuser_token_headers, normal_user_token_headers

    # ── Disabled: shape still answers ────────────────────────────────
    body = client.get(f"{API}/speech/usage", headers=user).json()
    assert body["enabled"] is False and body["used_seconds"] == 0 and body["cap_seconds"] is None
    now = datetime.now(UTC)
    nxt = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    assert datetime.fromisoformat(body["period_end"]).astimezone(UTC) == nxt
    assert client.get(f"{API}/speech/usage").status_code in (401, 403)

    # ── Usage by two users ───────────────────────────────────────────
    enable_voice_transcription(client, su, cap_minutes=10)
    other, other_headers = create_random_user_with_headers(client)
    _dictate(client, user, 2)
    _dictate(client, user, 1)
    _dictate(client, other_headers, 5)

    mine = client.get(f"{API}/speech/usage", headers=user).json()
    assert mine["enabled"] is True and mine["used_seconds"] == 3.0 and mine["cap_seconds"] == 600.0
    theirs = client.get(f"{API}/speech/usage", headers=other_headers).json()
    assert theirs["used_seconds"] == 5.0

    report = client.get(f"{ADMIN}/usage", headers=su)
    assert report.status_code == 200, report.text
    report = report.json()
    assert [r["email"] for r in report["rows"]] == [other["email"], "test@example.com"]
    assert [r["seconds"] for r in report["rows"]] == [5.0, 3.0]
    assert [r["sessions"] for r in report["rows"]] == [1, 2]
    assert report["total_seconds"] == 8.0 and report["total_sessions"] == 3
    assert report["period_start"] < report["period_end"]

    # ── Access control ───────────────────────────────────────────────
    for path in ("usage", "providers"):
        assert client.get(f"{ADMIN}/{path}", headers=user).status_code == 403
        assert client.get(f"{ADMIN}/{path}").status_code in (401, 403)
    assert client.post(f"{ADMIN}/test", headers=user, json={}).status_code == 403


def test_provider_options_list_only_speech_capable_fixed_key_providers(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    assert client.get(f"{ADMIN}/providers", headers=su).json() == []

    google = create_ai_provider(client, su, name="Google speech")
    create_ai_provider(client, su, provider_type="openai", secret="sk-openai-not-speech")
    create_ai_provider(client, su, provider_type="anthropic", secret="sk-ant-not-speech")

    options = client.get(f"{ADMIN}/providers", headers=su).json()
    assert options == [{"id": google["id"], "name": "Google speech", "type": "google"}]


def test_admin_test_button_maps_outcomes_and_is_rate_limited(
    client: TestClient, superuser_token_headers: dict[str, str], speech
) -> None:
    su = superuser_token_headers

    # ── Nothing configured / unknown provider ────────────────────────
    r = client.post(f"{ADMIN}/test", headers=su, json={})
    assert r.status_code == 200 and r.json() == {"ok": False, "error": "disabled"}

    provider = create_ai_provider(client, su)
    # Explicit provider works even before it is selected.
    r = client.post(f"{ADMIN}/test", headers=su, json={"provider_id": provider["id"]})
    assert r.json() == {"ok": True, "error": None}
    assert speech.probe_keys == [FAKE_PROVIDER_KEY]
    assert FAKE_PROVIDER_KEY not in r.text

    # ── Each probe outcome, via the configured provider ──────────────
    enable_voice_transcription(client, su, provider=provider)
    for result in ("invalid_key", "unreachable", "quota"):
        speech.probe_result = result
        r = client.post(f"{ADMIN}/test", headers=su, json={})
        assert r.status_code == 200 and r.json() == {"ok": False, "error": result}
    speech.probe_result = "ok"
    speech.probe_error = RuntimeError(f"boom {FAKE_PROVIDER_KEY}")
    r = client.post(f"{ADMIN}/test", headers=su, json={})
    assert r.json() == {"ok": False, "error": "unreachable"}
    assert FAKE_PROVIDER_KEY not in r.text and "boom" not in r.text

    # ── Rate limit: 10 per minute per admin (6 used so far) ──────────
    speech.probe_error = None
    for _ in range(4):
        assert client.post(f"{ADMIN}/test", headers=su, json={}).status_code == 200
    limited = client.post(f"{ADMIN}/test", headers=su, json={})
    assert limited.status_code == 429
    assert "retry-after" in limited.headers
