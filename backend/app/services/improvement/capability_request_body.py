"""
Capability-request body parsing, shared by every route that files one.

The public ``_cinna/improvement-requests`` route (consumer env) and the account
CLI route read the body raw, after auth, so an oversized payload is refused
(413) before it is parsed and an unauthenticated caller never gets a
validation report. Both apply the same size limit, the same strict model and
the same error report through this module; each route maps
``CapabilityRequestBodyError`` to its own ``detail`` shape.
"""
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from app.models.improvement.agent_improvement_request import (
    MAX_CAPABILITY_REQUEST_BODY_BYTES,
    AgentApiCapabilityRequestCreate,
)

INVALID_BODY_MESSAGE = (
    "Invalid capability request body. Unknown keys are rejected; "
    "send only the fields in `expected`."
)
TOO_LARGE_MESSAGE = "Request body too large"
INVALID_CONTENT_LENGTH_MESSAGE = "Invalid Content-Length"


class CapabilityRequestBodyError(Exception):
    """A refused capability-request body.

    ``status_code`` is 400 (bad ``Content-Length``), 413 (too large) or 422
    (invalid body; ``errors`` holds the JSON-safe pydantic errors, never the
    submitted input).
    """

    def __init__(
        self, status_code: int, message: str, errors: list | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.errors = errors


def inline_json_schema(model: type) -> dict:
    """The model's JSON schema with its ``$defs`` inlined.

    The body is read raw (so FastAPI does not document it); this schema goes
    into ``openapi_extra``, where a ``#/$defs/...`` reference would not resolve.
    The capability-request models are not recursive.
    """
    schema = model.model_json_schema(by_alias=True)
    defs = schema.pop("$defs", {})

    def _resolve(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return _resolve(defs[ref.split("/")[-1]])
            return {k: _resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(v) for v in node]
        return node

    return _resolve(schema)


def capability_request_openapi_extra() -> dict:
    """``openapi_extra`` documenting the raw-read capability-request body."""
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": inline_json_schema(AgentApiCapabilityRequestCreate)
                }
            },
        }
    }


def reject_oversized_content_length(content_length: str | None) -> None:
    """Refuse from the declared length alone, before any body is read."""
    if content_length is None:
        return
    try:
        too_large = int(content_length) > MAX_CAPABILITY_REQUEST_BODY_BYTES
    except ValueError:
        raise CapabilityRequestBodyError(400, INVALID_CONTENT_LENGTH_MESSAGE)
    if too_large:
        raise CapabilityRequestBodyError(413, TOO_LARGE_MESSAGE)


def parse_capability_request_body(raw: bytes) -> AgentApiCapabilityRequestCreate:
    """Size-check and validate the raw body (an empty body reads as ``{}``)."""
    # Also checked on the bytes read: chunked uploads carry no Content-Length.
    if len(raw) > MAX_CAPABILITY_REQUEST_BODY_BYTES:
        raise CapabilityRequestBodyError(413, TOO_LARGE_MESSAGE)
    try:
        return AgentApiCapabilityRequestCreate.model_validate_json(raw or b"{}")
    except ValidationError as e:
        raise CapabilityRequestBodyError(
            422,
            INVALID_BODY_MESSAGE,
            errors=jsonable_encoder(
                e.errors(include_url=False, include_context=False, include_input=False)
            ),
        )
