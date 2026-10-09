"""Domain exceptions for the team library.

Routes catch ``LibraryError`` and translate to HTTP via ``http_status``; the
message is user-facing and uses the library's vocabulary (S9).
"""


class LibraryError(Exception):
    """Base class for every library-layer domain error."""

    http_status: int = 500


class LibraryNotFoundError(LibraryError):
    http_status = 404


class LibraryAccessDeniedError(LibraryError):
    http_status = 403


class LibraryConflictError(LibraryError):
    http_status = 409


class LibraryValidationError(LibraryError):
    http_status = 422
