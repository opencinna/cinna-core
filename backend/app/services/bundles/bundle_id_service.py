"""Bundle ID generation — Phase 1 portion of ``BundleService``.

The full ``BundleService`` (CRUD on ``AgentBundle`` rows) lands in Phase 2.
For Phase 1 we only need the algorithm that produces a stable reverse-DNS
identifier from the configured frontend host + a short slug derived from the
agent UUID. Every ``Agent`` row gets one of these on creation.

Format: ``<reversed-host>.<short-uuid>``
  - ``reversed-host`` — ``settings.FRONTEND_HOST`` parsed → host → split on
    ``.`` → reversed → joined (e.g. ``cinna.opencinna.io`` → ``io.opencinna.cinna``).
    Falls back to ``localhost`` (single-component) when the host cannot be
    parsed (tests / local dev with custom hosts).
  - ``short-uuid`` — first 8 hex chars of the agent UUID.

Final example: ``io.opencinna.cinna.a1b2c3d4``.

The DNS-like format check ``^[a-zA-Z0-9]([a-zA-Z0-9.\\-]{1,253})$`` is the
contract for *user-supplied* edits in Phase 2; auto-generated values always
satisfy it because they only contain ``[a-z0-9.]``.
"""
import re
import uuid
from urllib.parse import urlparse

from app.core.config import settings


# Reverse-DNS validation regex used by Phase 2's edit endpoint. Exposed here
# so Phase 1 callers (migration, model defaults) can sanity-check generated
# values during tests.
BUNDLE_ID_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-]{1,253}$")

# Reserved prefix — Phase 2 bundle-id editor must reject these.
RESERVED_BUNDLE_ID_PREFIXES: tuple[str, ...] = ("io.opencinna.system.",)


class BundleIdService:
    """Algorithms for deriving and validating reverse-DNS bundle identifiers."""

    @staticmethod
    def reversed_host_prefix() -> str:
        """Return the reversed-DNS prefix derived from ``settings.FRONTEND_HOST``.

        Examples:
            FRONTEND_HOST="https://cinna.opencinna.io"  → "io.opencinna.cinna"
            FRONTEND_HOST="http://localhost:5173"        → "localhost"
            FRONTEND_HOST="http://192.168.1.5"           → "5.1.168.192"

        IPv4 addresses are reversed component-wise like any other host. IPv6
        is not separately handled — it would round-trip through the regex
        validator unchanged, but in practice nobody runs Cinna on a raw IPv6
        host. The fallback for an empty/unparseable host is ``"localhost"``.
        """
        raw = (settings.FRONTEND_HOST or "").strip()
        if not raw:
            return "localhost"

        # urlparse is liberal: it accepts "host", "host:port", and full URLs.
        # The hostname helper drops port + scheme for us.
        parsed = urlparse(raw if "://" in raw else f"//{raw}", scheme="http")
        host = (parsed.hostname or raw).lower()

        # Split + reverse + rejoin. Single-component hosts (e.g. "localhost")
        # round-trip unchanged.
        parts = [p for p in host.split(".") if p]
        if not parts:
            return "localhost"
        return ".".join(reversed(parts))

    @classmethod
    def generate_bundle_id(cls, agent_id: uuid.UUID) -> str:
        """Generate a default bundle id for a freshly-created agent.

        Args:
            agent_id: The agent's UUID. Only the first 8 hex chars are used.

        Returns:
            A reverse-DNS string e.g. ``io.opencinna.cinna.a1b2c3d4``.
        """
        prefix = cls.reversed_host_prefix()
        suffix = agent_id.hex[:8]
        return f"{prefix}.{suffix}"

    @staticmethod
    def is_valid_format(bundle_id: str) -> bool:
        """Return True if ``bundle_id`` matches the DNS-like format contract."""
        return bool(BUNDLE_ID_REGEX.match(bundle_id or ""))

    @staticmethod
    def is_reserved(bundle_id: str) -> bool:
        """Return True if ``bundle_id`` starts with a reserved system prefix."""
        if not bundle_id:
            return False
        return any(bundle_id.startswith(p) for p in RESERVED_BUNDLE_ID_PREFIXES)
