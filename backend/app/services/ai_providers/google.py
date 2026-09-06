"""Google (Gemini) provider adapter."""
from __future__ import annotations

from app.models.credentials.ai_credential import AICredentialType
from app.services.ai_providers import _http
from app.services.ai_providers.base import BaseProviderAdapter, ProbeResult


def _list_models_blocking(api_key: str) -> list[str]:
    """List Google models via the google-genai client (ListModels).

    The one adapter that does not go through ``httpx``: ``google-genai`` is a
    declared project dependency and its client is the shape this listing has
    always used.
    """
    from google import genai

    client = genai.Client(api_key=api_key)
    ids: list[str] = []
    for model in client.models.list():
        name = getattr(model, "name", None)
        if name:
            # Names come back as "models/gemini-2.5-pro"; strip the prefix.
            ids.append(name.split("/", 1)[1] if "/" in name else name)
    return ids


class GoogleAdapter(BaseProviderAdapter):
    type = AICredentialType.GOOGLE
    label = "Google"
    # The native/desktop bundle has always called this provider "Gemini". The
    # browser calls it "Google" in one place and "Google AI" in two others;
    # reconciling those three is the follow-up that consumes this field, and it
    # must not be done by quietly changing what the desktop client receives.
    account_config_display_name = "Gemini"
    account_config_slug = "gemini"
    sdk_engine = "opencode/google"
    bag_key_api_key = "google_api_key"
    key_provisioner = None

    async def list_models(
        self, api_key: str, base_url: str | None = None
    ) -> ProbeResult:
        # NOTE: ``run_listing`` maps only ``httpx.HTTPStatusError`` to
        # ``invalid_key``, and ``google-genai`` raises its own
        # ``google.genai.errors.ClientError`` instead — so a rejected Google key
        # propagates rather than mapping. Pre-existing (the old single dispatch
        # had the identical hole) and preserved here deliberately; see
        # ``_http.run_listing``.
        return await _http.run_listing(_list_models_blocking, api_key)
