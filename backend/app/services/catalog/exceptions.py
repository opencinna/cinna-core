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
