"""
Unit tests for the ``mcp_provider`` credential-type constants on
``CredentialsService``.

Pure constant assertions — the MCP_PROVIDER credential must be excluded from
``credentials.json`` (empty allow-list) and its token/OAuth secrets must be
redacted (present in SENSITIVE_FIELDS). The end-to-end exclusion + injection
behavior is exercised in
``tests/api/mcp_integration/test_a2a_connector_consumer.py``.
"""
from app.services.credentials.credentials_service import CredentialsService


def test_mcp_provider_excluded_from_credentials_json_whitelist() -> None:
    """
    AGENT_ENV_ALLOWED_FIELDS["mcp_provider"] must be an empty list so the
    credential is never written to credentials.json.
    """
    allowed = CredentialsService.AGENT_ENV_ALLOWED_FIELDS.get("mcp_provider")
    assert allowed == [], (
        f"MCP_PROVIDER must have empty whitelist, got {allowed!r}"
    )


def test_mcp_provider_sensitive_fields_includes_token() -> None:
    """
    SENSITIVE_FIELDS["mcp_provider"] must include token, oauth_client_secret,
    and oauth_refresh_token so they are redacted in prompts.
    """
    sensitive = CredentialsService.SENSITIVE_FIELDS.get("mcp_provider", [])
    for field in ("token", "oauth_client_secret", "oauth_refresh_token"):
        assert field in sensitive, (
            f"'{field}' must be in SENSITIVE_FIELDS['mcp_provider'], got {sensitive}"
        )
