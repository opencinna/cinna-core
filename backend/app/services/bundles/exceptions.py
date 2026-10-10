"""Domain exceptions for the bundles layer.

Routes catch ``BundleError`` and translate to HTTP via the per-subclass
``http_status`` attribute. Internal callers can match on subclasses.
"""


class BundleError(Exception):
    """Base class — every bundle-layer domain error inherits from this."""

    http_status: int = 500


class BundleNotFoundError(BundleError):
    http_status = 404


class BundleAccessDeniedError(BundleError):
    http_status = 403


class BundleConflictError(BundleError):
    http_status = 409


class BundleValidationError(BundleError):
    http_status = 400


# ── More specific subclasses (still safe to catch the base above) ──────


class RevisionNotFoundError(BundleNotFoundError):
    pass


class RevisionInUseError(BundleConflictError):
    pass


class GrantNotFoundError(BundleNotFoundError):
    pass


DESKTOP_ONLY_CODE = "desktop_only"
DESKTOP_ONLY_MESSAGE = "This agent runs on your computer in Cinna Desktop."


class DesktopOnlyError(BundleConflictError):
    """A cloud path was asked to act on a desktop agent (``runs_on='desktop'``).

    Answered as 409 with the structured ``detail`` below, so clients can match
    on ``code`` rather than on the sentence.
    """

    def __init__(self, message: str = DESKTOP_ONLY_MESSAGE) -> None:
        super().__init__(message)

    @property
    def detail(self) -> dict:
        return {"code": DESKTOP_ONLY_CODE, "message": str(self)}
