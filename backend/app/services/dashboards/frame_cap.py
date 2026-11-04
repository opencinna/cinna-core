"""Frame capabilities for dashboard component pages (D-C2).

A frame cap is a SECRET_KEY-signed HS256 JWT naming one owner and one tile
target, modelled on ``services/artifacts/artifact_cap.py``:

- ``sub`` — the owner's user id,
- ``tgt`` — ``b:<block_id>`` (a dashboard block) or
  ``p:<agent_id>:<skill|->:<component>[:<tile_key>]`` (an agent-scoped preview,
  or a desktop remote tile when ``tile_key`` is given),
- ``aud`` = ``dashboard_frame``, ``exp`` = mint time + ``DASHBOARD_FRAME_CAP_TTL_SECONDS``.

The ``aud`` claim keeps a cap from ever authenticating as a user
(``get_current_user`` decodes without an audience, so PyJWT rejects it) and
from being accepted as an artifact cap, and vice versa. The signature alone
grants nothing: every content request re-checks the owner against the block
or agent.
"""
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from jwt import InvalidTokenError

from app.core import security
from app.core.config import settings

CAP_AUDIENCE = "dashboard_frame"
# base64url segments of a compact JWS; anything else is not a cap and must
# never be interpolated into a header.
_CAP_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NO_SKILL = "-"


@dataclass(frozen=True)
class FrameTarget:
    """What a frame shows: a block, or an agent's component (``block_id`` None)."""

    block_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    skill: str | None = None
    component: str | None = None
    tile_key: str | None = None

    @property
    def is_block(self) -> bool:
        return self.block_id is not None


@dataclass(frozen=True)
class FrameCapClaims:
    user_id: uuid.UUID
    target: FrameTarget
    expires_at: datetime


def block_target(block_id: uuid.UUID) -> str:
    return f"b:{block_id}"


def component_target(
    agent_id: uuid.UUID, skill: str | None, component: str, tile_key: str | None = None
) -> str:
    target = f"p:{agent_id}:{skill or _NO_SKILL}:{component}"
    return f"{target}:{tile_key}" if tile_key else target


def parse_target(raw: str) -> FrameTarget | None:
    """Parse a ``tgt`` claim; None for anything malformed."""
    kind, _, rest = raw.partition(":")
    try:
        if kind == "b":
            return FrameTarget(block_id=uuid.UUID(rest))
        if kind != "p":
            return None
        parts = rest.split(":")
        if len(parts) not in (3, 4):
            return None
        agent_id = uuid.UUID(parts[0])
    except ValueError:
        return None
    skill, component = parts[1], parts[2]
    tile_key = parts[3] if len(parts) == 4 else None
    if skill != _NO_SKILL and not _NAME.match(skill):
        return None
    if not _NAME.match(component) or (tile_key is not None and not _NAME.match(tile_key)):
        return None
    return FrameTarget(
        agent_id=agent_id,
        skill=None if skill == _NO_SKILL else skill,
        component=component,
        tile_key=tile_key,
    )


def is_cap_shaped(cap: str) -> bool:
    return bool(cap) and len(cap) <= 2048 and _CAP_SHAPE.match(cap) is not None


def mint(user_id: uuid.UUID, target: str) -> tuple[str, datetime]:
    """Sign a frame cap for ``target`` (see ``block_target``/``component_target``)."""
    ttl = timedelta(seconds=settings.DASHBOARD_FRAME_CAP_TTL_SECONDS)
    expires_at = datetime.now(UTC) + ttl
    cap = security.create_access_token(
        subject=str(user_id),
        expires_delta=ttl,
        extra_claims={"aud": CAP_AUDIENCE, "tgt": target},
    )
    return cap, expires_at


def verify(cap: str) -> FrameCapClaims | None:
    """Decode and validate a cap; None for anything that is not a live, well-formed frame cap."""
    if not is_cap_shaped(cap):
        return None
    try:
        payload = jwt.decode(
            cap,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
            audience=CAP_AUDIENCE,
            options={"require": ["exp", "sub", "aud"]},
        )
        target = parse_target(str(payload["tgt"]))
        if target is None:
            return None
        return FrameCapClaims(
            user_id=uuid.UUID(str(payload["sub"])),
            target=target,
            expires_at=datetime.fromtimestamp(int(payload["exp"]), UTC),
        )
    except (InvalidTokenError, KeyError, ValueError, TypeError):
        return None


def content_base(cap: str) -> str:
    """``<content origin>/d/v/<cap>/`` — the CSP's ``<src>`` and every file URL's prefix."""
    return f"{settings.artifacts_content_origin}/d/v/{cap}/"


def frame_url(cap: str, entry: str) -> str:
    """The URL of a component's entry page under its cap."""
    return content_base(cap) + entry.lstrip("/")
