"""Public artifact links (``/api/v1/artifact-links``), no authentication.

Unknown, revoked or deleted ⇒ 404. ``open`` answers 410 for an expired link
and 403 with ``{message, attempts_left, blocked}`` for a missing or wrong code.
"""
from typing import Any

from fastapi import APIRouter

from app.api.deps import SessionDep
from app.api.routes.artifacts import artifact_errors
from app.models.artifacts.artifact import ArtifactViewPublic
from app.models.artifacts.artifact_link import ArtifactLinkInfo, ArtifactLinkOpen
from app.services.artifacts.artifact_link_service import ArtifactLinkService

router = APIRouter(prefix="/artifact-links", tags=["artifact-links"])


@router.get("/{token}/info", response_model=ArtifactLinkInfo)
def get_artifact_link_info(*, db: SessionDep, token: str) -> Any:
    """What the public page shows before opening: title, owner, code / expiry state."""
    with artifact_errors():
        return ArtifactLinkService.info(db, token)


@router.post("/{token}/open", response_model=ArtifactViewPublic)
def open_artifact_link(*, db: SessionDep, token: str, body: ArtifactLinkOpen | None = None) -> Any:
    """Open the link (with its code, when it has one) → a view capability."""
    with artifact_errors():
        return ArtifactLinkService.open(db, token, body.code if body else None)
