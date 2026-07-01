"""
Agent Webhooks API routes — authenticated CRUD + logs + regenerate-token.

Nested under ``/api/v1/agents/{agent_id}/webhooks``.

Public webhook execution lives in ``agent_hooks.py`` (no JWT, token-auth only).
"""
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import CurrentUser, SessionDep, require_developer
from app.models import (
    AgentWebhook,
    AgentWebhookCreateGitSource,
    AgentWebhookCreateScript,
    AgentWebhookCreateSession,
    AgentWebhookLog,
    AgentWebhookLogPublic,
    AgentWebhookLogsPublic,
    AgentWebhookPublic,
    AgentWebhookPublicWithToken,
    AgentWebhookUpdate,
    AgentWebhooksPublic,
)
from app.services.agents.agent_webhook_errors import WebhookError
from app.services.agents.agent_webhook_service import AgentWebhookService

router = APIRouter(tags=["agent-webhooks"])


def _handle_webhook_error(exc: WebhookError) -> None:
    """Translate a service exception into an HTTPException."""
    raise HTTPException(status_code=exc.status_code, detail=exc.message)


def _to_public(webhook: AgentWebhook) -> AgentWebhookPublic:
    """Serialize a webhook row with the computed public URL."""
    data = webhook.model_dump()
    data["webhook_url"] = AgentWebhookService.build_webhook_url(webhook.webhook_id)
    return AgentWebhookPublic(**data)


def _to_public_with_token(
    webhook: AgentWebhook, token: str
) -> AgentWebhookPublicWithToken:
    """Same as ``_to_public`` but includes the plaintext bearer token."""
    data = webhook.model_dump()
    data["webhook_url"] = AgentWebhookService.build_webhook_url(webhook.webhook_id)
    data["webhook_token"] = token
    return AgentWebhookPublicWithToken(**data)


def _log_to_public(log: AgentWebhookLog) -> AgentWebhookLogPublic:
    return AgentWebhookLogPublic(**log.model_dump())


# ==================== Create ====================


@router.post(
    "/agents/{agent_id}/webhooks/session",
    response_model=AgentWebhookPublicWithToken,
)
def create_session_webhook(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    data: AgentWebhookCreateSession,
) -> Any:
    """Create a session-type webhook. Returns the plaintext token ONCE."""
    try:
        webhook, token = AgentWebhookService.create_session_webhook(
            db_session=session,
            agent_id=agent_id,
            user_id=current_user.id,
            data=data,
        )
        return _to_public_with_token(webhook, token)
    except WebhookError as exc:
        _handle_webhook_error(exc)


@router.post(
    "/agents/{agent_id}/webhooks/script",
    response_model=AgentWebhookPublicWithToken,
)
def create_script_webhook(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    data: AgentWebhookCreateScript,
) -> Any:
    """Create a script-type webhook. Returns the plaintext token ONCE."""
    try:
        webhook, token = AgentWebhookService.create_script_webhook(
            db_session=session,
            agent_id=agent_id,
            user_id=current_user.id,
            data=data,
        )
        return _to_public_with_token(webhook, token)
    except WebhookError as exc:
        _handle_webhook_error(exc)


@router.post(
    "/agents/{agent_id}/webhooks/git-source",
    response_model=AgentWebhookPublicWithToken,
    dependencies=[Depends(require_developer)],
)
def create_git_source_webhook(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    data: AgentWebhookCreateGitSource,
) -> Any:
    """Create a git-source (GitOps) webhook. Returns the plaintext token ONCE.

    Firing the webhook triggers the agent's git source ``pull_update``.
    Developer-gated — wiring a GitOps trigger is a developer action.
    """
    try:
        webhook, token = AgentWebhookService.create_git_source_webhook(
            db_session=session,
            agent_id=agent_id,
            user_id=current_user.id,
            data=data,
        )
        return _to_public_with_token(webhook, token)
    except WebhookError as exc:
        _handle_webhook_error(exc)


# ==================== List / Get ====================


@router.get(
    "/agents/{agent_id}/webhooks",
    response_model=AgentWebhooksPublic,
)
def list_webhooks(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
) -> Any:
    """List all webhooks for an agent."""
    try:
        webhooks = AgentWebhookService.list_webhooks(
            db_session=session,
            agent_id=agent_id,
            user_id=current_user.id,
        )
        return AgentWebhooksPublic(
            data=[_to_public(w) for w in webhooks],
            count=len(webhooks),
        )
    except WebhookError as exc:
        _handle_webhook_error(exc)


@router.get(
    "/agents/{agent_id}/webhooks/{webhook_pk}",
    response_model=AgentWebhookPublic,
)
def get_webhook(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    webhook_pk: uuid.UUID,
) -> Any:
    """Fetch a single webhook by its row UUID."""
    try:
        webhook = AgentWebhookService.get_webhook(
            db_session=session,
            agent_id=agent_id,
            webhook_pk=webhook_pk,
            user_id=current_user.id,
        )
        return _to_public(webhook)
    except WebhookError as exc:
        _handle_webhook_error(exc)


# ==================== Update / Delete ====================


@router.patch(
    "/agents/{agent_id}/webhooks/{webhook_pk}",
    response_model=AgentWebhookPublic,
)
def update_webhook(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    webhook_pk: uuid.UUID,
    data: AgentWebhookUpdate,
) -> Any:
    """Partially update a webhook. ``type`` is immutable."""
    try:
        webhook = AgentWebhookService.update_webhook(
            db_session=session,
            agent_id=agent_id,
            webhook_pk=webhook_pk,
            user_id=current_user.id,
            data=data,
        )
        return _to_public(webhook)
    except WebhookError as exc:
        _handle_webhook_error(exc)


@router.delete("/agents/{agent_id}/webhooks/{webhook_pk}")
def delete_webhook(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    webhook_pk: uuid.UUID,
) -> dict:
    """Delete a webhook. Cascades to logs."""
    try:
        AgentWebhookService.delete_webhook(
            db_session=session,
            agent_id=agent_id,
            webhook_pk=webhook_pk,
            user_id=current_user.id,
        )
        return {"success": True}
    except WebhookError as exc:
        _handle_webhook_error(exc)


# ==================== Token regeneration ====================


@router.post(
    "/agents/{agent_id}/webhooks/{webhook_pk}/regenerate-token",
    response_model=AgentWebhookPublicWithToken,
)
def regenerate_token(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    webhook_pk: uuid.UUID,
) -> Any:
    """
    Rotate the bearer token. Same ``webhook_id`` URL, new token — old token
    stops working immediately.
    """
    try:
        webhook, token = AgentWebhookService.regenerate_token(
            db_session=session,
            agent_id=agent_id,
            webhook_pk=webhook_pk,
            user_id=current_user.id,
        )
        return _to_public_with_token(webhook, token)
    except WebhookError as exc:
        _handle_webhook_error(exc)


# ==================== Logs ====================


@router.get(
    "/agents/{agent_id}/webhooks/{webhook_pk}/logs",
    response_model=AgentWebhookLogsPublic,
)
def list_webhook_logs(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    webhook_pk: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
) -> Any:
    """List recent execution logs for a webhook (default 50, max 200)."""
    try:
        logs = AgentWebhookService.get_webhook_logs(
            db_session=session,
            agent_id=agent_id,
            webhook_pk=webhook_pk,
            user_id=current_user.id,
            limit=limit,
        )
        return AgentWebhookLogsPublic(
            data=[_log_to_public(log) for log in logs],
            count=len(logs),
        )
    except WebhookError as exc:
        _handle_webhook_error(exc)
