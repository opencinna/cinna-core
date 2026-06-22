"""User's Details service — current-user context for credentials.json.

Holds three concerns for the "User's Details" feature:

1. The pure parser/normalizer (``parse_user_details`` / ``format_user_details``)
   that turns free-text env-file content into a normalized
   ``{UPPER_SNAKE: "value"}`` map (and back to a display view).
2. The synthetic ``current_user`` block builder (``build_current_user_block``)
   injected into each agent environment's credentials.json.
3. The per-user re-sync fan-out (``event_user_details_updated``) that refreshes
   every running environment of every agent the user owns.

Kept in the user-domain service folder (per conventions) so
``credentials_service.py`` only imports the block builder, and so the
fan-out's import of ``CredentialsService`` is done locally to avoid an
import cycle.
"""

import logging
import re
import uuid

from sqlmodel import Session, select

from app.models import Agent
from app.models.users.user import User

logger = logging.getLogger(__name__)


# ── Parsing limits ──────────────────────────────────────────────────────
MAX_RAW_BYTES = 10 * 1024  # 10 KB of raw text
MAX_KEYS = 100
MAX_KEY_LENGTH = 64
MAX_VALUE_LENGTH = 1024  # 1 KB

_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_NON_ALNUM_RUN = re.compile(r"[^A-Z0-9]+")


def _normalize_key(raw_key: str) -> str:
    """Normalize a raw key to UPPER_SNAKE form.

    trim → uppercase → replace any run of non-alphanumeric chars with a
    single ``_`` → strip leading/trailing ``_``. May return an empty string
    (caller treats that as a parse error).
    """
    upper = raw_key.strip().upper()
    collapsed = _NON_ALNUM_RUN.sub("_", upper)
    return collapsed.strip("_")


def _strip_surrounding_quotes(value: str) -> str:
    """Strip exactly one layer of matching surrounding quotes, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_user_details(raw: str) -> dict[str, str]:
    """Parse env-file style text into a normalized ``{UPPER_SNAKE: value}`` map.

    Pure function. Raises ``ValueError`` with a human-readable,
    line-referencing message on any rule violation. Empty input is valid and
    returns an empty dict.

    See the plan's "Parsing & Normalization Contract" for the full rule set.
    """
    if raw is None:
        return {}

    # Limit: raw text size (measured in bytes for a true byte cap).
    if len(raw.encode("utf-8")) > MAX_RAW_BYTES:
        raise ValueError(
            f"Details are too large (max {MAX_RAW_BYTES // 1024} KB)."
        )

    parsed: dict[str, str] = {}

    for index, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()

        # Blank lines are ignored.
        if not stripped:
            continue

        # Comment lines (first non-whitespace char is '#') are ignored.
        if stripped.startswith("#"):
            continue

        # A line with no '=' is a parse error.
        if "=" not in line:
            raise ValueError(f"Line {index}: expected 'key = value'")

        # Split on the FIRST '=' only (values may contain '=').
        raw_key, raw_value = line.split("=", 1)

        key = _normalize_key(raw_key)
        if not key:
            raise ValueError(
                f"Line {index}: key is empty after normalization"
            )

        if len(key) > MAX_KEY_LENGTH:
            raise ValueError(
                f"Line {index}: key '{key}' exceeds {MAX_KEY_LENGTH} characters"
            )

        if not _KEY_PATTERN.match(key):
            raise ValueError(
                f"Line {index}: invalid key '{key}' "
                f"(must match {_KEY_PATTERN.pattern})"
            )

        value = _strip_surrounding_quotes(raw_value.strip())

        if len(value) > MAX_VALUE_LENGTH:
            raise ValueError(
                f"Line {index}: value for '{key}' exceeds "
                f"{MAX_VALUE_LENGTH} characters"
            )

        # Duplicate keys (after normalization) are an error — do not last-wins.
        if key in parsed:
            raise ValueError(f"Duplicate key: {key}")

        if len(parsed) >= MAX_KEYS:
            raise ValueError(f"Too many keys (max {MAX_KEYS}).")

        parsed[key] = value

    return parsed


def format_user_details(parsed: dict[str, str] | None) -> str:
    """Render a normalized map as ``KEY="value"`` lines for the editor view.

    Values are always double-quoted; inner ``"`` are escaped as ``\\"``.
    Keys are emitted in their stored (normalized) order. Returns ``""`` when
    there are no details.
    """
    if not parsed:
        return ""

    lines: list[str] = []
    for key, value in parsed.items():
        escaped = str(value).replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    return "\n".join(lines)


# Fixed marker constants for the synthetic credentials.json entry.
CURRENT_USER_ID = "current_user"
CURRENT_USER_TYPE = "current_user"
CURRENT_USER_NAME = "Current User"
CURRENT_USER_NOTES = (
    "Auto-generated identity & details of the agent owner. "
    "Not a real credential."
)


def build_current_user_block(user: User) -> dict:
    """Build the synthetic ``current_user`` list entry for credentials.json.

    Mirrors the shape of a real credential entry
    (``{id, name, type, notes, credential_data}``) but with reserved sentinel
    ``id``/``type`` markers. Carries the owner's public identity plus their
    self-authored ``custom_details`` map. No secrets.
    """
    return {
        "id": CURRENT_USER_ID,
        "name": CURRENT_USER_NAME,
        "type": CURRENT_USER_TYPE,
        "notes": CURRENT_USER_NOTES,
        "credential_data": {
            "username": user.username,
            "full_name": user.full_name,
            "email": user.email,
            "email_confirmed": user.email_confirmed,
            "timezone": user.timezone,
            "language": user.language,
            "locale": user.locale,
            "conversation_style": user.conversation_style,
            "custom_details": user.details_parsed or {},
        },
    }


async def event_user_details_updated(
    session: Session,
    user_id: uuid.UUID,
) -> None:
    """Re-sync all running environments of all agents owned by the user.

    The current-user/details block is per-user (not per-credential), so a
    details change must refresh every environment the user owns. Enumerates
    ``Agent.owner_id == user_id`` and calls
    ``CredentialsService.sync_credentials_to_agent_environments`` per agent
    (which itself filters to running envs and swallows per-env errors).

    Mirrors ``CredentialsService.event_credential_updated`` with a broader
    enumeration root. Imports ``CredentialsService`` locally to avoid an
    import cycle.
    """
    from app.services.credentials.credentials_service import CredentialsService

    logger.info(f"User {user_id} details updated, syncing owned agents")

    agent_ids = session.exec(
        select(Agent.id).where(Agent.owner_id == user_id)
    ).all()

    if not agent_ids:
        logger.info(f"User {user_id} owns no agents; nothing to sync")
        return

    logger.info(f"User {user_id} details update affects {len(agent_ids)} agent(s)")

    for agent_id in agent_ids:
        await CredentialsService.sync_credentials_to_agent_environments(
            session=session,
            agent_id=agent_id,
        )
