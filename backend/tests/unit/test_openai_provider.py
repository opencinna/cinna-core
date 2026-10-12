"""
OpenAIProvider (AI-functions) request-shape tests.

Covers the Chat Completions payload the provider sends:
1. ``max_completion_tokens`` is sent, ``max_tokens`` is not (gpt-5+ rejects it)
2. ``model`` falls back to ``settings.OPENAI_MODEL`` when no model is passed
3. An explicit ``model`` argument wins over the setting

``httpx.post`` is patched in the provider module; no network, no DB.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.agents.providers.openai_provider import OpenAIProvider

_POST_TARGET = "app.agents.providers.openai_provider.httpx.post"


def _ok_response(text: str = "hello") -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"choices": [{"message": {"content": text}}]}
    return response


@pytest.fixture
def post_mock():
    with patch(_POST_TARGET, return_value=_ok_response()) as mocked:
        yield mocked


def _sent_payload(post_mock: MagicMock) -> dict:
    post_mock.assert_called_once()
    return post_mock.call_args.kwargs["json"]


def test_payload_uses_max_completion_tokens_not_max_tokens(post_mock):
    OpenAIProvider(api_key="sk-test").generate_content("hi")

    payload = _sent_payload(post_mock)
    assert "max_completion_tokens" in payload
    assert "max_tokens" not in payload


def test_model_falls_back_to_settings_openai_model(post_mock):
    with patch(
        "app.agents.providers.openai_provider.settings.OPENAI_MODEL",
        "configured-model",
    ):
        result = OpenAIProvider(api_key="sk-test").generate_content("hi")

    assert _sent_payload(post_mock)["model"] == "configured-model"
    assert result.model == "configured-model"


def test_explicit_model_wins_over_setting(post_mock):
    with patch(
        "app.agents.providers.openai_provider.settings.OPENAI_MODEL",
        "configured-model",
    ):
        result = OpenAIProvider(api_key="sk-test").generate_content(
            "hi", model="explicit-model"
        )

    assert _sent_payload(post_mock)["model"] == "explicit-model"
    assert result.model == "explicit-model"
