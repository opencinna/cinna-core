"""View capabilities for artifact content (D1, D3).

A cap is a SECRET_KEY-signed HS256 JWT that names one revision of one artifact
and the principal it was minted for:

- ``sub`` — the artifact id, ``rev`` — the revision id,
- ``via`` — ``u:<user_id>`` (minted by ``POST /artifacts/{id}/view``) or
  ``l:<link_id>`` (minted by opening a public link),
- ``aud`` = ``artifact_view``, ``exp`` = mint time + ``ARTIFACT_VIEW_CAP_TTL_SECONDS``.

The ``aud`` claim is what keeps a cap from ever authenticating as a user:
``get_current_user`` decodes without an audience, so PyJWT rejects any
aud-bearing token. The signature alone is not trusted for access — every
content request re-checks ``via`` against the database (D1).

The cap travels in the URL path (``/a/v/<cap>/<file>``), so relative URLs in
an artifact resolve under it and the CSP can scope sources to that path.
"""
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from jwt import InvalidTokenError

from app.core import security
from app.core.config import settings

CAP_AUDIENCE = "artifact_view"
# base64url segments of a compact JWS; anything else is not a cap and must
# never be interpolated into a header.
_CAP_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")

ViaKind = Literal["u", "l"]


@dataclass(frozen=True)
class CapClaims:
    artifact_id: uuid.UUID
    revision_id: uuid.UUID
    via_kind: ViaKind
    via_id: uuid.UUID
    expires_at: datetime


def via_user(user_id: uuid.UUID) -> str:
    return f"u:{user_id}"


def via_link(link_id: uuid.UUID) -> str:
    return f"l:{link_id}"


def is_cap_shaped(cap: str) -> bool:
    return bool(cap) and len(cap) <= 2048 and _CAP_SHAPE.match(cap) is not None


def mint(artifact_id: uuid.UUID, revision_id: uuid.UUID, via: str) -> tuple[str, datetime]:
    """Sign a cap; returns ``(cap, expires_at)``."""
    ttl = timedelta(seconds=settings.ARTIFACT_VIEW_CAP_TTL_SECONDS)
    expires_at = datetime.now(UTC) + ttl
    cap = security.create_access_token(
        subject=str(artifact_id),
        expires_delta=ttl,
        extra_claims={"aud": CAP_AUDIENCE, "rev": str(revision_id), "via": via},
    )
    return cap, expires_at


def verify(cap: str) -> CapClaims | None:
    """Decode and validate a cap; None for anything that is not a live, well-formed cap."""
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
        via = str(payload["via"])
        kind, _, via_id = via.partition(":")
        if kind not in ("u", "l"):
            return None
        return CapClaims(
            artifact_id=uuid.UUID(str(payload["sub"])),
            revision_id=uuid.UUID(str(payload["rev"])),
            via_kind=kind,  # type: ignore[arg-type]
            via_id=uuid.UUID(via_id),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), UTC),
        )
    except (InvalidTokenError, KeyError, ValueError, TypeError):
        return None


def content_base(cap: str) -> str:
    """``<content origin>/a/v/<cap>/`` — the CSP's ``B`` and every file URL's prefix."""
    return f"{settings.artifacts_content_origin}/a/v/{cap}/"


def zip_url(cap: str) -> str:
    """Zip download of the cap's revision (D18)."""
    return f"{settings.artifacts_content_origin}/a/z/{cap}"
