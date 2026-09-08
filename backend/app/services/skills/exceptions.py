"""Coded failures of the skills catalog.

One exception with a stable ``code``, because the publish and install dialogs
have to branch: "this skill has a stray ``.env``" and "this agent has no
environment yet" need different copy and different next steps, and a client
cannot get that from a sentence. The route layer owns the code → HTTP status
map (:data:`STATUS_BY_CODE`); the service never mentions HTTP.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class SkillCatalogError(Exception):
    """A refusal from the skills catalog, carrying a machine code.

    ``paths`` is populated for ``skill_contains_secrets`` — the offending files
    relative to the skill folder — so the dialog can name them instead of
    telling the publisher to go hunting.
    """

    def __init__(self, code: str, message: str, *, paths: list[str] | None = None):
        self.code = code
        self.message = message
        self.paths = paths or []
        super().__init__(message)


#: Code → HTTP status. Anything absent is a 400.
STATUS_BY_CODE: dict[str, int] = {
    # Authorization / existence
    "not_accessible": 404,
    "package_not_found": 404,
    "revision_not_found": 404,
    "skill_not_found": 404,
    "not_developer": 403,
    "foreign_install": 403,
    "not_publisher": 403,
    "not_superuser": 403,
    # Publish pre-flight — the environment side
    "no_environment": 409,
    "workspace_unavailable": 409,
    # Publish pre-flight — the content side
    "skill_invalid": 422,
    "skill_contains_secrets": 422,
    "skill_too_large": 422,
    # Identity
    "package_id_invalid": 422,
    "package_id_taken": 409,
    "package_id_immutable": 409,
    "invalid_visibility": 422,
    "invalid_display_name": 422,
    # Install
    "already_installed": 409,
    # A DIFFERENT publisher's package already occupies this skill name in the
    # target agent. Names are unique per publisher, but an agent has one
    # `plugins/cinna-skills/<name>/` directory, so the two cannot coexist.
    "name_conflict": 409,
    "no_revision": 409,
    # Raised when the shared plugin-upgrade route is handed a link that did not
    # come from the catalog — the caller asked the wrong service, which is a
    # request error, not a missing resource.
    "not_a_catalog_link": 400,
    # The snapshot directory is gone. 410, not 503: a revision is immutable, so
    # files that are not there will not reappear on a retry, and "try again
    # later" would send the caller down the wrong path.
    "snapshot_missing": 410,
    "archive_unavailable": 503,
}


def http_error_for(exc: SkillCatalogError) -> HTTPException:
    """Map a coded catalog failure onto its HTTP answer.

    Lives beside the map rather than in a router because two routers raise
    these: the skills routes, and the shared plugin-upgrade route (a catalog
    link upgrades through the catalog service). One refusal must not answer two
    different ways depending on which router the caller happened to reach.

    The code travels in the detail body alongside the sentence so a dialog can
    branch — ``skill_contains_secrets`` lists files, ``no_environment`` offers
    to create one — without parsing prose.
    """
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.paths:
        detail["paths"] = exc.paths
    return HTTPException(
        status_code=STATUS_BY_CODE.get(exc.code, 400), detail=detail
    )
