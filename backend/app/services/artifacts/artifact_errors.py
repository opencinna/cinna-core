"""The one error type the artifact services raise; routes map it to HTTP.

``detail`` is usually the message string; a few errors carry a structured
detail the client reads (e.g. attempts left after a wrong link code).
"""
from typing import Any


class ArtifactError(Exception):
    def __init__(self, status: int, message: str, detail: Any | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail if detail is not None else message


def not_found(what: str = "Artifact") -> ArtifactError:
    return ArtifactError(404, f"{what} not found")
