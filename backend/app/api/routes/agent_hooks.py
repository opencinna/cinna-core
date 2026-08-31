"""
Public agent webhook execution endpoint — no JWT auth.

Mounted at the app root (no ``/api/v1`` prefix) so the public URL reads
``{host}/agent-hooks/{webhook_id}``, mirroring the task-trigger ``/hooks/{id}``
convention.

Token validation is done by the service layer via ``hmac.compare_digest`` on
the decrypted Fernet ciphertext. Pre-validation errors (unknown webhook,
disabled webhook, bad/missing token, payload too large) return 4xx. Any
failure after validation still returns 200 with ``log_id`` — the caller knows
the webhook was received and can correlate in the UI.
"""
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from app.core.db import create_session
from app.services.agents.agent_webhook_errors import (
    WebhookError,
    WebhookNotFoundError,
    WebhookTokenInvalidError,
)
from app.services.agents.agent_webhook_service import AgentWebhookService
from app.utils import client_ip

router = APIRouter(tags=["agent-hooks"])

logger = logging.getLogger(__name__)

# Max webhook payload size: 64KB
_MAX_PAYLOAD_SIZE = 64 * 1024


@router.post("/{webhook_id}")
async def execute_agent_webhook(
    webhook_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> dict[str, Any]:
    """
    Fire an agent webhook.

    Accepts the bearer token via:
    - ``Authorization: Bearer <token>`` header, OR
    - ``?token=<token>`` query parameter.

    Response (HTTP 200):
        {
            "success": true,
            "webhook_type": "session" | "script",
            "log_id": "<uuid>"
        }
    """
    # ---- Token extraction ----
    provided_token: str | None = None
    if authorization and authorization.startswith("Bearer "):
        provided_token = authorization[7:]
    elif token:
        provided_token = token
    if not provided_token:
        raise HTTPException(status_code=401, detail="Token required")

    # ---- Payload size check (fast path via Content-Length) ----
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_PAYLOAD_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail="Payload exceeds maximum size of 64KB",
                )
        except ValueError:
            # Malformed Content-Length — ignore, we'll check the real body below
            pass

    # ---- Body read + hard cap ----
    payload_text: str | None = None
    try:
        body = await request.body()
    except Exception:
        body = b""
    if body:
        if len(body) > _MAX_PAYLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail="Payload exceeds maximum size of 64KB",
            )
        payload_text = body.decode("utf-8", errors="replace")

    payload_content_type = request.headers.get("content-type")
    remote_ip = client_ip(request)
    # Snapshot headers as a plain dict — the FastAPI/Starlette Headers object
    # isn't directly serializable.
    headers_snapshot: dict[str, str] = dict(request.headers)

    # ---- Token validation + dispatch ----
    # ``create_session()`` (not ``Session(engine)``) so the public endpoint is
    # patchable in tests and participates in the rolled-back test transaction.
    with create_session() as db_session:
        try:
            webhook = AgentWebhookService.validate_webhook_token(
                db_session=db_session,
                webhook_id=webhook_id,
                provided_token=provided_token,
            )
        except WebhookNotFoundError:
            raise HTTPException(status_code=404, detail="Webhook not found")
        except WebhookTokenInvalidError:
            raise HTTPException(
                status_code=401, detail="Invalid or expired token"
            )
        except WebhookError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

        # Post-validation: any failure is logged with status="error" and we
        # still return 200 with the log_id so the caller can correlate.
        try:
            log = await AgentWebhookService.fire_webhook(
                db_session=db_session,
                webhook=webhook,
                payload_text=payload_text,
                payload_content_type=payload_content_type,
                headers=headers_snapshot,
                remote_ip=remote_ip,
            )
        except Exception as exc:
            # fire_webhook is expected to catch internally, but keep this as
            # a final safety net so we never 500 a post-auth webhook call.
            logger.error(
                f"agent-hook {webhook_id}: fire_webhook raised unexpectedly: {exc}",
                exc_info=True,
            )
            return {
                "success": True,
                "webhook_type": webhook.type,
                "log_id": None,
            }

    return {
        "success": True,
        "webhook_type": log.webhook_type,
        "log_id": str(log.id),
    }
