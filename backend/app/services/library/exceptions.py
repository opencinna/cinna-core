"""Domain exceptions for the git transport of desktop agents.

The message is user-facing (S9 vocabulary) and is sent as the plain-text body
of the git smart-HTTP answer.
"""


class GitAccessDeniedError(Exception):
    """The caller may not run this git service on this repo (HTTP 403)."""

    http_status: int = 403
