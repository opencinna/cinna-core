"""Anthropic provider adapter."""
from __future__ import annotations

from app.models.credentials.ai_credential import AICredentialType
from app.services.ai_providers import _http
from app.services.ai_providers.base import (
    OAUTH_TOKEN_UNSUPPORTED,
    BaseProviderAdapter,
    KeyClassification,
    ProbeResult,
)

_MODELS_URL = "https://api.anthropic.com/v1/models"
_API_VERSION = "2023-06-01"

# Anthropic issues two kinds of secret under one credential type. The prefix is
# the only way to tell them apart, and the container environment variable a key
# must be delivered in differs by kind — an OAuth token set as ANTHROPIC_API_KEY
# is simply rejected. This is THE declaration of that rule; five other modules
# used to repeat the prefix test independently.
_OAUTH_TOKEN_PREFIX = "sk-ant-oat"
_API_KEY_PREFIX = "sk-ant-api"

ANTHROPIC_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
ANTHROPIC_OAUTH_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def _list_models_blocking(api_key: str) -> list[str]:
    """List Anthropic models via GET /v1/models.

    ``httpx`` rather than a vendor SDK: the ``anthropic`` package is not a
    project dependency.
    """
    payload = _http.get_json(
        _MODELS_URL,
        {"x-api-key": api_key, "anthropic-version": _API_VERSION},
    )
    return _http.ids_from_openai_shape(payload)


class AnthropicAdapter(BaseProviderAdapter):
    type = AICredentialType.ANTHROPIC
    label = "Anthropic"
    account_config_display_name = "Claude"
    account_config_slug = "claude"
    sdk_engine = "claude-code/anthropic"
    bag_key_api_key = "anthropic_api_key"
    # The only provider that issues two kinds of secret under one type.
    issues_oauth_tokens = True

    # Anthropic's Admin API lists and updates keys but cannot create them, in
    # the API or in any SDK. There is no provisioner to write.
    key_provisioner = None

    def classify_key(self, api_key: str | None) -> KeyClassification:
        if not api_key:
            return KeyClassification(
                is_oauth_token=False,
                env_var_name=ANTHROPIC_API_KEY_ENV_VAR,
                label="API Key (Empty)",
            )
        if api_key.startswith(_OAUTH_TOKEN_PREFIX):
            return KeyClassification(
                is_oauth_token=True,
                env_var_name=ANTHROPIC_OAUTH_TOKEN_ENV_VAR,
                label="OAuth Token",
            )
        if api_key.startswith(_API_KEY_PREFIX):
            return KeyClassification(
                is_oauth_token=False,
                env_var_name=ANTHROPIC_API_KEY_ENV_VAR,
                label="API Key",
            )
        return KeyClassification(
            is_oauth_token=False,
            env_var_name=ANTHROPIC_API_KEY_ENV_VAR,
            label="API Key (Unknown Format)",
        )

    async def list_models(
        self, api_key: str, base_url: str | None = None
    ) -> ProbeResult:
        # An OAuth token cannot call /v1/models. The credential is fine; the
        # listing is simply unavailable for it.
        if self.classify_key(api_key).is_oauth_token:
            return ProbeResult(ok=True, models=[], reason=OAUTH_TOKEN_UNSUPPORTED)
        return await _http.run_listing(_list_models_blocking, api_key)
