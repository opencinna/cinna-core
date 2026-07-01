"""
SSRF / egress hygiene for backend-initiated MCP-provider calls (RD-6).

The implementation was promoted to the neutral, reusable module
:mod:`app.services.common.egress_guard` so non-MCP callers (git sources) share
the exact same range checks. This module re-exports the public surface so the
existing MCP-provider imports keep working unchanged.

MCP callers invoke ``assert_url_allowed`` / ``validate_external_endpoint_url`` /
``is_host_blocked`` with no private-host argument, which defaults to
``MCP_PROVIDER_ALLOW_PRIVATE_HOSTS`` — behavior is identical to before.
"""

from app.services.common.egress_guard import (  # noqa: F401
    ALLOWED_SCHEMES,
    EgressBlockedError,
    assert_host_allowed,
    assert_url_allowed,
    is_host_blocked,
    validate_external_endpoint_url,
)

__all__ = [
    "ALLOWED_SCHEMES",
    "EgressBlockedError",
    "assert_host_allowed",
    "assert_url_allowed",
    "is_host_blocked",
    "validate_external_endpoint_url",
]
