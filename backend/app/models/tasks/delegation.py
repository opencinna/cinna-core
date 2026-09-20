"""Validated wire contract for durable cross-agent delegations."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DelegationMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str = Field(min_length=1, max_length=128)
    requester_key: str = Field(min_length=1, max_length=128)
    origin_kind: Literal['local_chat', 'local_task', 'remote_task', 'external']
    origin_agent_id: str | None = Field(default=None, max_length=128)
    origin_chat_id: str | None = Field(default=None, max_length=128)
    origin_task_id: str | None = Field(default=None, max_length=128)
    depth: int = Field(ge=1, le=2)
    root: str = Field(min_length=1, max_length=128)
    group: str | None = Field(default=None, max_length=128)


class DelegationArtifact(BaseModel):
    model_config = ConfigDict(extra='forbid')

    kind: Literal['file', 'link']
    name: str = Field(min_length=1, max_length=500)
    ref: str = Field(min_length=1, max_length=4096)

    @model_validator(mode='after')
    def portable_reference(self):
        if not self.ref.startswith(('https://', 'http://')):
            raise ValueError('Artifacts require portable HTTP(S) references; upload local files first')
        return self


class DelegationReport(BaseModel):
    model_config = ConfigDict(extra='forbid')

    status: Literal['in_progress', 'blocked', 'done', 'failed']
    summary: str = Field(min_length=1, max_length=1000)
    question: str | None = Field(default=None, max_length=10000)
    # A question with no stated audience goes to a person: the requester agent
    # must not be the default answerer of something it may not know.
    audience: Literal['requester', 'user'] = 'user'
    artifacts: list[DelegationArtifact] = Field(default_factory=list, max_length=100)
    body: str = Field(default='', max_length=100000)

    @model_validator(mode='after')
    def blocked_question(self):
        if self.status == 'blocked' and not (self.question or '').strip():
            raise ValueError('A blocked report must contain a question')
        return self


class AgentDelegationReport(DelegationReport):
    source_session_id: UUID


class DelegationReply(BaseModel):
    model_config = ConfigDict(extra='forbid')

    result_id: UUID
    message: str = Field(min_length=1, max_length=10000)

    @model_validator(mode='after')
    def nonempty_message(self):
        if not self.message.strip():
            raise ValueError('A reply must contain a message')
        return self


class DelegationResultPublic(BaseModel):
    """The stored structured result of a delegated task, as the requester reads it."""
    id: UUID
    session_id: UUID | None = None
    status: Literal['in_progress', 'blocked', 'done', 'failed']
    summary: str
    question: str | None = None
    audience: Literal['requester', 'user'] = 'user'
    artifacts: list[DelegationArtifact] = Field(default_factory=list)
    body: str = ''
    # Report fields above are stored exactly as reported; a delivered reply does
    # not rewrite status. None until the owner replies to a blocked question;
    # 'sending' while the reply is being handed to the session, 'delivered'
    # once it was accepted (the task itself then moves blocked -> in_progress).
    reply_state: Literal['sending', 'delivered'] | None = None


class DelegationCapabilities(BaseModel):
    """Which parts of the delegation contract this server supports."""
    version: int
    metadata: bool
    structured_result: bool
    reply: bool


class DelegationReplyResult(BaseModel):
    delivered: bool
    # True when an earlier attempt's acknowledgement was lost mid-send: the
    # reply may or may not have reached the session, and is not re-sent.
    uncertain: bool = False
