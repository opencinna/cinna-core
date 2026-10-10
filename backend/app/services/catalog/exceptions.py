"""Domain exceptions for the catalog layer.

Routes catch ``CatalogError`` and translate to HTTP via ``http_status``; the
message is user-facing.
"""


class CatalogError(Exception):
    """Base class for every catalog-layer domain error."""

    http_status: int = 500


class CatalogNotFoundError(CatalogError):
    http_status = 404


class CatalogAccessDeniedError(CatalogError):
    http_status = 403


class CatalogConflictError(CatalogError):
    http_status = 409


class CatalogValidationError(CatalogError):
    http_status = 422


CLOUD_ONLY_CODE = "cloud_only"
CLOUD_ONLY_MESSAGE = "This agent runs in Cinna."


class CloudOnlyError(CatalogConflictError):
    """A desktop-only path (local installs) was asked to act on a cloud agent:
    the inverse of ``desktop_only``. Answered as 409 with a structured
    ``detail`` so clients can match on ``code``."""

    def __init__(self, message: str = CLOUD_ONLY_MESSAGE) -> None:
        super().__init__(message)

    @property
    def detail(self) -> dict:
        return {"code": CLOUD_ONLY_CODE, "message": str(self)}
