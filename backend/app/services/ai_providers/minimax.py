"""MiniMax provider adapter.

Live in the backend and deliberately absent from every frontend provider list.
The registry covers it regardless: a type that exists in the enum but has no
adapter is a hole, and "which providers does the UI offer" is a separate
question from "which providers does the backend understand".
"""
from __future__ import annotations

from app.models.credentials.ai_credential import AICredentialType
from app.services.ai_providers.base import (
    NO_LIST_ENDPOINT,
    BaseProviderAdapter,
    ProbeResult,
)


class MiniMaxAdapter(BaseProviderAdapter):
    type = AICredentialType.MINIMAX
    label = "MiniMax"
    account_config_display_name = "MiniMax"
    account_config_slug = "minimax"
    sdk_engine = "claude-code/minimax"
    bag_key_api_key = "minimax_api_key"
    # No public model-list endpoint — the catalog is the only source, so a
    # credential with no discovered models is expected rather than unverified.
    supports_model_listing = False
    key_provisioner = None

    async def list_models(
        self, api_key: str, base_url: str | None = None
    ) -> ProbeResult:
        return ProbeResult(ok=True, models=[], reason=NO_LIST_ENDPOINT)
