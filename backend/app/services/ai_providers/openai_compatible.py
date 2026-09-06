"""OpenAI-compatible endpoint adapter.

A shape rather than a vendor: any endpoint speaking the OpenAI REST dialect.
That is why it is the only adapter requiring a base URL and a model, and why its
account-config display name is the empty-string sentinel — the credential's own
free-form name is the only accurate label for it.
"""
from __future__ import annotations

from app.models.credentials.ai_credential import AICredentialType
from app.services.ai_providers import _http
from app.services.ai_providers.base import (
    NO_BASE_URL,
    BaseProviderAdapter,
    ProbeResult,
)

_MODELS_PATH = "/models"


def _list_models_blocking(api_key: str, base_url: str) -> list[str]:
    """List models via GET ``{base_url}/models``.

    Assumes the OpenAI response shape. Endpoints that differ or are unreachable
    raise, and the caller records an error + skips.
    """
    url = base_url.rstrip("/") + _MODELS_PATH
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = _http.get_json(url, headers)
    return _http.ids_from_openai_shape(payload)


class OpenAICompatibleAdapter(BaseProviderAdapter):
    type = AICredentialType.OPENAI_COMPATIBLE
    label = "OpenAI Compatible"
    account_config_display_name = ""  # sentinel: use the credential's own name
    account_config_slug = "openai-compatible"
    sdk_engine = "opencode/openai_compatible"
    bag_key_api_key = "openai_compatible_api_key"
    bag_key_base_url = "openai_compatible_base_url"
    bag_key_model = "openai_compatible_model"
    requires_base_url = True
    requires_model = True
    key_provisioner = None

    async def list_models(
        self, api_key: str, base_url: str | None = None
    ) -> ProbeResult:
        if not base_url:
            return ProbeResult(ok=True, models=[], reason=NO_BASE_URL)
        return await _http.run_listing(_list_models_blocking, api_key, base_url)
