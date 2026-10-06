"""
Cinna Task Protocol (CTP) v1 — agent-environment endpoints.

Called by the ``tasks_*`` tools on an agent environment's ``cinna`` MCP server
(Claude Code in-process server, OpenCode stdio bridge). Authentication and
scoping are those of ``task_agent_api``: the scoped agent-environment token
(``AgentEnvContextDep``), every session checked against the env's owner and
agent, every task against the owner. Business rules live in
``TaskProtocolService``.

Registered before ``task_agent_api``: its ``/agent/tasks/{task_id}/comment``
pattern would otherwise capture ``/agent/tasks/protocol/comment``.

Routes:
  GET   /agent/tasks/protocol/targets  — tasks_targets
  GET   /agent/tasks/protocol/list     — tasks_list
  GET   /agent/tasks/protocol/get      — tasks_get
  POST  /agent/tasks/protocol/create   — tasks_create
  POST  /agent/tasks/protocol/update   — tasks_update
  POST  /agent/tasks/protocol/comment  — tasks_comment
  POST  /agent/tasks/protocol/report   — tasks_report
  POST  /agent/tasks/protocol/reply    — tasks_reply
"""
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import AgentEnvContextDep, SessionDep
from app.models.tasks.task_protocol import (
    TaskTargetsPublic,
    TasksCommentRequest,
    TasksCreateRequest,
    TasksReplyRequest,
    TasksReportRequest,
    TasksUpdateRequest,
)
from app.services.tasks.input_task_service import InputTaskError
from app.services.tasks.task_protocol_service import TaskProtocolService

router = APIRouter(prefix="/agent/tasks/protocol", tags=["agent-tasks"])


def _handle_error(error: InputTaskError) -> None:
    raise HTTPException(status_code=error.status_code, detail=error.message)


@router.get("/targets", response_model=TaskTargetsPublic)
def tasks_targets(
    db_session: SessionDep, ctx: AgentEnvContextDep, source_session_id: uuid.UUID,
) -> Any:
    try:
        return TaskProtocolService.targets(db_session, ctx, source_session_id)
    except InputTaskError as error:
        _handle_error(error)


@router.get("/list")
def tasks_list(
    db_session: SessionDep,
    ctx: AgentEnvContextDep,
    source_session_id: uuid.UUID,
    relation: Literal["assigned", "created", "delegated", "all"] = "all",
    category: Literal["backlog", "ready", "active", "waiting", "done"] | None = None,
    include_closed: bool = False,
) -> Any:
    try:
        return TaskProtocolService.list_tasks(
            db_session, ctx, source_session_id, relation, category, include_closed,
        )
    except InputTaskError as error:
        _handle_error(error)


@router.get("/get")
async def tasks_get(
    db_session: SessionDep,
    ctx: AgentEnvContextDep,
    source_session_id: uuid.UUID,
    ref: str = Query(min_length=1, max_length=128),
) -> Any:
    try:
        return await TaskProtocolService.get_task(db_session, ctx, source_session_id, ref)
    except InputTaskError as error:
        _handle_error(error)


@router.post("/create")
async def tasks_create(
    data: TasksCreateRequest, db_session: SessionDep, ctx: AgentEnvContextDep,
) -> Any:
    try:
        return await TaskProtocolService.create(db_session, ctx, data)
    except InputTaskError as error:
        _handle_error(error)


@router.post("/update")
def tasks_update(
    data: TasksUpdateRequest, db_session: SessionDep, ctx: AgentEnvContextDep,
) -> Any:
    try:
        return TaskProtocolService.update(db_session, ctx, data)
    except InputTaskError as error:
        _handle_error(error)


@router.post("/comment")
def tasks_comment(
    data: TasksCommentRequest, db_session: SessionDep, ctx: AgentEnvContextDep,
) -> Any:
    try:
        return TaskProtocolService.comment(db_session, ctx, data)
    except InputTaskError as error:
        _handle_error(error)


@router.post("/report")
def tasks_report(
    data: TasksReportRequest, db_session: SessionDep, ctx: AgentEnvContextDep,
) -> Any:
    try:
        return TaskProtocolService.report(db_session, ctx, data)
    except InputTaskError as error:
        _handle_error(error)


@router.post("/reply")
async def tasks_reply(
    data: TasksReplyRequest, db_session: SessionDep, ctx: AgentEnvContextDep,
) -> Any:
    try:
        return await TaskProtocolService.reply(db_session, ctx, data)
    except InputTaskError as error:
        _handle_error(error)
