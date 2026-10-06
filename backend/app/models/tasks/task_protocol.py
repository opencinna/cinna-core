"""Wire models for the Cinna Task Protocol (CTP) v1 agent-environment binding.

The eight ``tasks_*`` tools on an agent environment's ``cinna`` MCP server post
these to ``/api/v1/agent/tasks/protocol/*``. Field names and limits follow
``docs/local_agent_kit/protocols/tasks.schema.json``; every request carries the
calling ``source_session_id`` the env-side tool reads from its session context.
"""
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1
# The MCP server name every binding exposes the tasks_* tools under.
SERVER_NAME = "cinna"

TaskPriority = Literal["low", "normal", "high", "urgent"]
TaskCategory = Literal["backlog", "ready", "active", "waiting", "done"]


class _ProtocolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_session_id: UUID


class TasksCreateRequest(_ProtocolRequest):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=100000)
    priority: TaskPriority | None = None
    category: Literal["backlog", "ready", "draft"] | None = None
    parent: str | None = Field(default=None, min_length=1, max_length=128)
    assignee: str | None = Field(default=None, max_length=256)
    id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    execution: Literal["ask", "auto"] | None = None
    group: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    custom_fields: dict[str, Any] | None = None


class TasksUpdateRequest(_ProtocolRequest):
    ref: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=100000)
    priority: TaskPriority | None = None
    custom_fields: dict[str, Any] | None = None
    category: Literal["backlog", "ready", "done", "cancelled"] | None = None


class TasksCommentRequest(_ProtocolRequest):
    ref: str | None = Field(default=None, min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=8000)
    files: list[str] | None = Field(default=None, max_length=20)


class TasksReportRequest(_ProtocolRequest):
    ref: str | None = Field(default=None, min_length=1, max_length=128)
    status: Literal["in_progress", "blocked", "done", "failed"]
    summary: str = Field(min_length=1, max_length=1000)
    question: str | None = Field(default=None, max_length=4000)
    audience: Literal["requester", "user"] | None = None
    # A path or URL string, or {kind?, name?, ref}; normalised by the service.
    artifacts: list[str | dict[str, Any]] = Field(default_factory=list, max_length=100)
    body: str = Field(default="", max_length=100000)


class TasksReplyRequest(_ProtocolRequest):
    ref: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=64000)


class TaskTargetCapabilities(BaseModel):
    structured_report: bool = True
    reply: bool = True
    priority: bool = True
    custom_fields: bool = False
    attachments: bool = True


class TaskTarget(BaseModel):
    target: str
    name: str
    kind: Literal["folder", "kit", "cinna", "desktop", "external"] = "cinna"
    location: Literal["local", "remote"] = "remote"
    description: str | None = None
    capabilities: TaskTargetCapabilities = Field(default_factory=TaskTargetCapabilities)
    protocol_version: int = PROTOCOL_VERSION
    requires_approval: bool = False


class TaskTargetsPublic(BaseModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    targets: list[TaskTarget]
