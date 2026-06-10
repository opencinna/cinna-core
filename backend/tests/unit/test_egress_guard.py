"""
Unit tests for the MCP-provider SSRF egress guard.

Pure predicate logic — ``validate_external_endpoint_url`` and ``is_host_blocked``
operate on URLs/hostnames with no DB or HTTP. The literal private-IP / loopback /
bad-scheme cases are additionally covered end-to-end through the connect endpoint
in ``tests/api/mcp_integration/test_a2a_connector_consumer.py`` (the SSRF-guard
scenarios); this file keeps the DNS-resolution cases and the static checks that
have no end-to-end equivalent (link-local, missing host, public passthrough, and
the allow-private-hosts override).
"""
from unittest.mock import patch

import pytest


def test_egress_guard_link_local_blocked() -> None:
    """Link-local addresses (169.254.x.x) are blocked."""
    from app.services.mcp_providers.egress_guard import (
        EgressBlockedError,
        validate_external_endpoint_url,
    )
    with patch("app.core.config.settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS", False):
        with pytest.raises(EgressBlockedError):
            validate_external_endpoint_url("http://169.254.169.254/latest/meta-data")


def test_egress_guard_missing_host_blocked() -> None:
    """URL with no host component raises EgressBlockedError."""
    from app.services.mcp_providers.egress_guard import (
        EgressBlockedError,
        validate_external_endpoint_url,
    )
    with pytest.raises(EgressBlockedError, match="host"):
        validate_external_endpoint_url("http:///mcp")


def test_egress_guard_valid_public_url_allowed() -> None:
    """A well-formed public HTTPS URL passes validation."""
    from app.services.mcp_providers.egress_guard import validate_external_endpoint_url
    with patch("app.core.config.settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS", False):
        result = validate_external_endpoint_url("https://api.example.com/mcp")
    assert result == "https://api.example.com/mcp"


def test_egress_guard_allow_private_hosts_override() -> None:
    """MCP_PROVIDER_ALLOW_PRIVATE_HOSTS=True disables all guards."""
    from app.services.mcp_providers.egress_guard import validate_external_endpoint_url
    with patch("app.core.config.settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS", True):
        # Would normally raise — but the override disables the guard
        result = validate_external_endpoint_url("http://192.168.0.1/mcp")
    assert result == "http://192.168.0.1/mcp"


def test_is_host_blocked_private_resolution() -> None:
    """is_host_blocked returns True when DNS resolves to a private address."""
    import socket

    from app.services.mcp_providers.egress_guard import is_host_blocked

    # Patch getaddrinfo to return a private IP
    private_addr = ("192.168.1.1", 80, 0, "")
    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, None, None, None, private_addr)]):
        with patch("app.core.config.settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS", False):
            assert is_host_blocked("someinternal.corp") is True


def test_is_host_blocked_public_resolution() -> None:
    """is_host_blocked returns False when DNS resolves to a public address."""
    import socket

    from app.services.mcp_providers.egress_guard import is_host_blocked

    public_addr = ("8.8.8.8", 443, 0, "")
    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, None, None, None, public_addr)]):
        with patch("app.core.config.settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS", False):
            assert is_host_blocked("dns.google") is False
