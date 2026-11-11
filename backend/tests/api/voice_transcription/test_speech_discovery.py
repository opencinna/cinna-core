"""Voice transcription relay: the ``voice_transcription`` discovery block.

Present on ``/.well-known/cinna-desktop`` and ``/.well-known/cinna-app`` only
while the relay is on with a usable provider whose key loads; discovery never
fails because of it.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.utils.voice_transcription import (
    API,
    create_ai_provider,
    enable_voice_transcription,
    put_server_config,
)

PATHS = ("/.well-known/cinna-desktop", "/.well-known/cinna-app")


def _blocks(client: TestClient) -> list[dict | None]:
    out = []
    for path in PATHS:
        r = client.get(path)
        assert r.status_code == 200, r.text
        out.append(r.json().get("voice_transcription"))
    return out


def test_discovery_block_follows_the_relay_state(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    assert _blocks(client) == [None, None]

    # Selected but switched off: absent.
    provider = create_ai_provider(client, su)
    assert put_server_config(client, su, voice_transcription_provider_id=provider["id"]).status_code == 200
    assert _blocks(client) == [None, None]

    # On: present on both, ws(s) URL of the stream.
    enable_voice_transcription(client, su, provider=provider)
    for block in _blocks(client):
        assert block is not None and block["version"] == 1
        assert block["url"].startswith(("ws://", "wss://"))
        assert block["url"].endswith("/api/v1/speech/stream")
        assert set(block) == {"version", "url"}

    # Off again: absent.
    put_server_config(client, su, voice_transcription_enabled=False)
    assert _blocks(client) == [None, None]


def test_discovery_omits_block_when_the_key_cannot_be_loaded_and_survives_errors(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    enable_voice_transcription(client, superuser_token_headers)
    assert all(b is not None for b in _blocks(client))

    # A provider whose key no longer decrypts is unusable.
    with patch(
        "app.services.credentials.ai_providers_service.AIProvidersService.fixed_key_api_key",
        return_value=None,
    ):
        assert _blocks(client) == [None, None]
    assert all(b is not None for b in _blocks(client))

    # A lookup that raises never takes discovery down.
    with patch(
        "app.services.voice_transcription.config_service."
        "VoiceTranscriptionConfigService.discovery_block",
        side_effect=RuntimeError("boom"),
    ):
        for path in PATHS:
            r = client.get(path)
            assert r.status_code == 200
            assert "voice_transcription" not in r.json()
            assert "token_endpoint" in r.json()
