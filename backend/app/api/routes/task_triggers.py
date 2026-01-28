"""
Task Triggers API routes.

Provides CRUD operations for task trigger management.
Nested under tasks: /tasks/{task_id}/triggers
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.models import (
    TaskTriggerCreateSchedule,
    TaskTriggerCreateExactDate,
    TaskTriggerCreateWebhook,
    TaskTriggerUpdate,
    TaskTriggerPublic,
    TaskTriggerPublicWithToken,
    TaskTriggersPublic,
)
from app.services.task_trigger_service import (
    TaskTriggerService,
    TriggerError,
)

router = APIRouter(tags=["task-triggers"])


def _handle_trigger_error(e: TriggerError) -> None:
    """Convert service exceptions to HTTP exceptions."""
    raise HTTPException(status_code=e.status_code, detail=e.message)


def _trigger_to_public(trigger) -> TaskTriggerPublic:
    """Convert a TaskTrigger to TaskTriggerPublic with webhook_url."""
    data = trigger.model_dump()
    if trigger.webhook_id:
        data["webhook_url"] = TaskTriggerService._build_webhook_url(trigger.webhook_id)
    return TaskTriggerPublic(**data)


def _trigger_to_public_with_token(trigger, token: str) -> TaskTriggerPublicWithToken:
    """Convert a TaskTrigger to TaskTriggerPublicWithToken."""
    data = trigger.model_dump()
    if trigger.webhook_id:
        data["webhook_url"] = TaskTriggerService._build_webhook_url(trigger.webhook_id)
    data["webhook_token"] = token
    return TaskTriggerPublicWithToken(**data)


# ==================== Schedule Triggers ====================

@router.post(
    "/{task_id}/triggers/schedule",
    response_model=TaskTriggerPublic,
)
def create_schedule_trigger(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    task_id: uuid.UUID,
    data: TaskTriggerCreateSchedule,
) -> Any:
    """Create a schedule trigger with AI natural language parsing."""
    try:
        trigger = TaskTriggerService.create_schedule_trigger(
            db_session=session,
            task_id=task_id,
            user_id=current_user.id,
            data=data,
        )
        return _trigger_to_public(trigger)
    except TriggerError as e:
        _handle_trigger_error(e)


# ==================== Exact Date Triggers ====================

@router.post(
    "/{task_id}/triggers/exact-date",
    response_model=TaskTriggerPublic,
)
def create_exact_date_trigger(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    task_id: uuid.UUID,
    data: TaskTriggerCreateExactDate,
) -> Any:
    """Create a one-time exact date trigger."""
    try:
        trigger = TaskTriggerService.create_exact_date_trigger(
            db_session=session,
            task_id=task_id,
            user_id=current_user.id,
            data=data,
        )
        return _trigger_to_public(trigger)
    except TriggerError as e:
        _handle_trigger_error(e)


# ==================== Webhook Triggers ====================

@router.post(
    "/{task_id}/triggers/webhook",
    response_model=TaskTriggerPublicWithToken,
)
def create_webhook_trigger(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    task_id: uuid.UUID,
    data: TaskTriggerCreateWebhook,
) -> Any:
    """Create a webhook trigger. Returns the full token ONCE."""
    try:
        trigger, token = TaskTriggerService.create_webhook_trigger(
            db_session=session,
            task_id=task_id,
            user_id=current_user.id,
            data=data,
        )
        return _trigger_to_public_with_token(trigger, token)
    except TriggerError as e:
        _handle_trigger_error(e)


# ==================== List / Get ====================

@router.get(
    "/{task_id}/triggers",
    response_model=TaskTriggersPublic,
)
def list_triggers(
    session: SessionDep,
    current_user: CurrentUser,
    task_id: uuid.UUID,
) -> Any:
    """List all triggers for a task."""
    try:
        triggers = TaskTriggerService.list_triggers(
            db_session=session,
            task_id=task_id,
            user_id=current_user.id,
        )
        return TaskTriggersPublic(
            data=[_trigger_to_public(t) for t in triggers],
            count=len(triggers),
        )
    except TriggerError as e:
        _handle_trigger_error(e)


@router.get(
    "/{task_id}/triggers/{trigger_id}",
    response_model=TaskTriggerPublic,
)
def get_trigger(
    session: SessionDep,
    current_user: CurrentUser,
    task_id: uuid.UUID,
    trigger_id: uuid.UUID,
) -> Any:
    """Get a single trigger."""
    try:
        trigger = TaskTriggerService.get_trigger(
            db_session=session,
            trigger_id=trigger_id,
            task_id=task_id,
            user_id=current_user.id,
        )
        return _trigger_to_public(trigger)
    except TriggerError as e:
        _handle_trigger_error(e)


# ==================== Update / Delete ====================

@router.patch(
    "/{task_id}/triggers/{trigger_id}",
    response_model=TaskTriggerPublic,
)
def update_trigger(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    task_id: uuid.UUID,
    trigger_id: uuid.UUID,
    data: TaskTriggerUpdate,
) -> Any:
    """Update a trigger."""
    try:
        trigger = TaskTriggerService.update_trigger(
            db_session=session,
            trigger_id=trigger_id,
            task_id=task_id,
            user_id=current_user.id,
            data=data,
        )
        return _trigger_to_public(trigger)
    except TriggerError as e:
        _handle_trigger_error(e)


@router.delete("/{task_id}/triggers/{trigger_id}")
def delete_trigger(
    session: SessionDep,
    current_user: CurrentUser,
    task_id: uuid.UUID,
    trigger_id: uuid.UUID,
) -> dict:
    """Delete a trigger."""
    try:
        TaskTriggerService.delete_trigger(
            db_session=session,
            trigger_id=trigger_id,
            task_id=task_id,
            user_id=current_user.id,
        )
        return {"success": True}
    except TriggerError as e:
        _handle_trigger_error(e)


# ==================== Token Regeneration ====================

@router.post(
    "/{task_id}/triggers/{trigger_id}/regenerate-token",
    response_model=TaskTriggerPublicWithToken,
)
def regenerate_token(
    session: SessionDep,
    current_user: CurrentUser,
    task_id: uuid.UUID,
    trigger_id: uuid.UUID,
) -> Any:
    """Regenerate webhook token. Returns the new full token ONCE."""
    try:
        trigger, token = TaskTriggerService.regenerate_webhook_token(
            db_session=session,
            trigger_id=trigger_id,
            task_id=task_id,
            user_id=current_user.id,
        )
        return _trigger_to_public_with_token(trigger, token)
    except TriggerError as e:
        _handle_trigger_error(e)
