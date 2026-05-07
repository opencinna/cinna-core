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
