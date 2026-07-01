"""
SSRF / egress hygiene for backend-initiated outbound calls.

Arbitrary external URLs/hosts are an SSRF surface. Every backend-initiated
network call to a user-supplied target — MCP-provider DCR/OAuth/probe calls,
git clone/pull/push/ls-remote — must validate the target first. This module is
the single, reusable chokepoint.

Originally lived in ``app.services.mcp_providers.egress_guard`` (keyed off
``MCP_PROVIDER_ALLOW_PRIVATE_HOSTS``). It was promoted here so non-MCP callers
(git sources) can reuse the exact same range checks. The private-host policy is
now a per-call argument so each caller honors its own setting:

- MCP callers pass nothing → default to ``MCP_PROVIDER_ALLOW_PRIVATE_HOSTS``
  (behavior unchanged).
- Git-source callers pass ``allow_private_hosts=settings.GIT_SOURCE_ALLOW_PRIVATE_HOSTS``.

``validate_external_endpoint_url`` is the static (no-DNS) scheme/host-shape
check. ``is_host_blocked`` resolves a host and checks every resolved address
(DNS-rebind defence). ``assert_url_allowed`` combines both for ``http(s)`` URLs;
``assert_host_allowed`` is the host-only variant for schemeless SSH git URLs.
"""
import ipaddress
import socket
from urllib.parse import urlsplit

from app.core.config import settings

# Only these schemes are ever allowed for an external http(s) endpoint.
ALLOWED_SCHEMES = ("http", "https")


class EgressBlockedError(Exception):
    """Raised when a target URL/host is rejected by the egress guard."""


def _resolve_allow_private(allow_private_hosts: bool | None) -> bool:
    """Resolve the effective private-host policy for a call.

    ``None`` (the default for legacy MCP callers) falls back to the MCP
    setting so existing behavior is preserved. Git-source callers pass an
    explicit bool sourced from ``GIT_SOURCE_ALLOW_PRIVATE_HOSTS``.
    """
    if allow_private_hosts is None:
        return settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS
    return allow_private_hosts


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


def validate_external_endpoint_url(
    url: str, *, allow_private_hosts: bool | None = None
) -> str:
    """
    Static validation of a user-entered external endpoint URL.

    Checks scheme + host shape only (no DNS resolution — that happens at network
    time in the live paths). If the host is a literal IP in a private/loopback/
    link-local range it is rejected here too. Returns the normalised URL.

    Raises :class:`EgressBlockedError` on any violation.
    """
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise EgressBlockedError(
            f"Unsupported URL scheme '{parts.scheme}'. Use http or https."
        )
    host = parts.hostname
    if not host:
        raise EgressBlockedError("URL has no host.")

    if _resolve_allow_private(allow_private_hosts):
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


def is_host_blocked(host: str, *, allow_private_hosts: bool | None = None) -> bool:
    """
    Resolve ``host`` and return True if ANY resolved address is in a blocked
    range. Network-time check (DNS-rebind defence).
    """
    if _resolve_allow_private(allow_private_hosts):
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


def assert_url_allowed(url: str, *, allow_private_hosts: bool | None = None) -> str:
    """
    Network-time guard for every backend-initiated ``http(s)`` call. The single
    chokepoint those paths must pass through before opening any connection.

    Combines the static scheme/host-shape check (:func:`validate_external_endpoint_url`)
    with a DNS-resolving range check (:func:`is_host_blocked`) so a hostname that
    resolves to a private/loopback/link-local address is rejected too (DNS-rebind
    defence). Returns the normalised URL; raises :class:`EgressBlockedError` on
    any violation.
    """
    normalised = validate_external_endpoint_url(
        url, allow_private_hosts=allow_private_hosts
    )
    host = urlsplit(normalised).hostname
    if host and is_host_blocked(host, allow_private_hosts=allow_private_hosts):
        raise EgressBlockedError(
            "Refusing to connect: the target host resolves to a private / "
            "loopback / link-local address."
        )
    return normalised


def assert_host_allowed(host: str, *, allow_private_hosts: bool | None = None) -> str:
    """
    Host-only egress guard for schemeless targets (e.g. SSH git URLs of the form
    ``git@host:owner/repo.git`` which carry no scheme and so cannot go through
    :func:`assert_url_allowed`).

    Runs the same DNS-resolving range check on the resolved host. Returns the
    host; raises :class:`EgressBlockedError` if it is blocked.
    """
    if not host:
        raise EgressBlockedError("No host to validate.")
    if is_host_blocked(host, allow_private_hosts=allow_private_hosts):
        raise EgressBlockedError(
            "Refusing to connect: the target host is / resolves to a private / "
            "loopback / link-local address."
        )
    return host
