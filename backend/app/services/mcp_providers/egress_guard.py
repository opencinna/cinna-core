"""
SSRF / egress hygiene for backend-initiated MCP-provider calls (RD-6).

Arbitrary external MCP URLs are an SSRF surface. Every backend-initiated call to
an external MCP server — DCR registration, OAuth refresh, the connectivity probe
— must validate the target host first. This module is the single chokepoint.

Phase 3 uses :func:`validate_external_endpoint_url` to reject malformed / unsafe
URLs at credential-create time (a static check on the user-entered URL). The
DNS-resolving network-time guard used by the live DCR/refresh/probe paths
(Phase 4/5) builds on :func:`is_host_blocked` so the policy stays in one place.

The ``MCP_PROVIDER_ALLOW_PRIVATE_HOSTS`` setting (default false) is a self-hosted
override that disables the private-range block entirely.
"""
import ipaddress
import socket
from urllib.parse import urlsplit

from app.core.config import settings

# Only these schemes are ever allowed for an external MCP endpoint.
ALLOWED_SCHEMES = ("http", "https")


class EgressBlockedError(Exception):
    """Raised when a target URL/host is rejected by the egress guard."""


def _ip_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if the IP is in a range we must never let the backend reach."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_external_endpoint_url(url: str) -> str:
    """
    Static validation of a user-entered external MCP endpoint URL.

    Checks scheme + host shape only (no DNS resolution — that happens at network
    time in the live paths). If the host is a literal IP in a private/loopback/
    link-local range it is rejected here too. Returns the normalised URL.

    Raises :class:`EgressBlockedError` on any violation. Honors
    ``MCP_PROVIDER_ALLOW_PRIVATE_HOSTS``.
    """
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise EgressBlockedError(
            f"Unsupported URL scheme '{parts.scheme}'. Use http or https."
        )
    host = parts.hostname
    if not host:
        raise EgressBlockedError("URL has no host.")

    if settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS:
        return url.strip()

    # If the host is a literal IP, block private ranges immediately. Hostnames
    # are resolved + re-checked at network time by the live paths (is_host_blocked).
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and _ip_is_private(ip):
        raise EgressBlockedError(
            "Refusing to connect to a private / loopback / link-local address."
        )
    return url.strip()


def is_host_blocked(host: str) -> bool:
    """
    Resolve ``host`` and return True if ANY resolved address is in a blocked
    range. Network-time check for the live DCR/refresh/probe paths (Phase 4/5).

    Honors ``MCP_PROVIDER_ALLOW_PRIVATE_HOSTS`` (returns False when set).
    """
    if settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS:
        return False
    try:
        ip = ipaddress.ip_address(host)
        return _ip_is_private(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable host — let the live caller surface the connection error.
        return False
    for info in infos:
        addr = info[4][0]
        try:
            if _ip_is_private(ipaddress.ip_address(addr)):
                return True
        except ValueError:
            continue
    return False


def assert_url_allowed(url: str) -> str:
    """
    Network-time guard for every backend-initiated MCP call (DCR registration,
    OAuth token exchange / refresh, the ``/test`` probe). The single chokepoint
    those paths must pass through before opening any connection (RD-6).

    Combines the static scheme/host-shape check (:func:`validate_external_endpoint_url`)
    with a DNS-resolving range check (:func:`is_host_blocked`) so a hostname that
    resolves to a private/loopback/link-local address is rejected too (DNS-rebind
    defence). Returns the normalised URL; raises :class:`EgressBlockedError` on
    any violation. Honors ``MCP_PROVIDER_ALLOW_PRIVATE_HOSTS``.
    """
    normalised = validate_external_endpoint_url(url)
    host = urlsplit(normalised).hostname
    if host and is_host_blocked(host):
        raise EgressBlockedError(
            "Refusing to connect: the target host resolves to a private / "
            "loopback / link-local address."
        )
    return normalised
