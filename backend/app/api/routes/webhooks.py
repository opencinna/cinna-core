"""
Webhook execution endpoint — public, no JWT auth.

Token validation is performed by the service layer.
"""
from typing import Any

from fastapi import APIRouter, HTTPException, Header, Query, Request
from sqlmodel import Session

from app.core.db import engine
from app.services.task_trigger_service import (
    TaskTriggerService,
    TriggerError,
    TriggerNotFoundError,
    WebhookTokenInvalidError,
)

router = APIRouter(tags=["webhooks"])

# Max webhook payload size: 64KB
MAX_PAYLOAD_SIZE = 64 * 1024


@router.post("/{webhook_id}")
async def execute_webhook(
    webhook_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> dict[str, Any]:
    """
    Execute a webhook trigger.

    Accepts token via:
    - Authorization: Bearer <token> header
    - ?token=<token> query parameter
    """
    # Extract token from header or query
    provided_token = None
    if authorization and authorization.startswith("Bearer "):
        provided_token = authorization[7:]
    elif token:
        provided_token = token

    if not provided_token:
        raise HTTPException(status_code=401, detail="Token required")

    # Check payload size
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_PAYLOAD_SIZE:
        raise HTTPException(
            status_code=413, detail="Payload exceeds maximum size of 64KB"
        )

    # Read body payload
    payload_text: str | None = None
    try:
        body = await request.body()
        if body:
            if len(body) > MAX_PAYLOAD_SIZE:
                raise HTTPException(
                    status_code=413, detail="Payload exceeds maximum size of 64KB"
                )
            payload_text = body.decode("utf-8", errors="replace")
    except HTTPException:
        raise
    except Exception:
        pass  # No body is fine

    # Validate token and fire trigger
    with Session(engine) as db_session:
        try:
            trigger = TaskTriggerService.validate_webhook_token(
                db_session=db_session,
                webhook_id=webhook_id,
                provided_token=provided_token,
            )
        except TriggerNotFoundError:
            raise HTTPException(status_code=404, detail="Webhook not found")
        except WebhookTokenInvalidError:
            raise HTTPException(
                status_code=401, detail="Invalid or expired token"
            )
        except TriggerError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)

        try:
            await TaskTriggerService.fire_trigger(
                db_session=db_session,
                trigger=trigger,
                payload=payload_text,
            )
        except Exception as e:
            # Log but return success — the webhook was received
            import logging
            logging.getLogger(__name__).error(
                f"Webhook {webhook_id} trigger fire error: {e}", exc_info=True
            )

    return {"success": True, "message": "Task execution triggered"}
