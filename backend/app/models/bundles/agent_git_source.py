"""AgentGitSource — git-backed versioning link for a single agent install.

Git is an external storage/transport/interchange backend for the thing the
platform already versions: an ``AgentBundleRevision``. An ``AgentGitSource``
row binds one ``Agent`` install to one remote git repo (optionally a ``subdir``
within it) so the install can be checked out, pulled, and pushed against that
remote. It is the git analog of ``Agent.bundle_uuid`` + ``installed_revision_id``
and is modeled on ``AIKnowledgeGitRepo`` (the existing git-source model used by
knowledge sources), reusing the same ``git_operations.py`` plumbing.

Owner/workspace-scoped: ``owner_id`` is the per-agent ownership scope. Exactly
one git source is allowed per install (unique ``agent_id``); multi-remote is an
explicit non-goal for now.

Enum-like columns (``sync_direction``, ``status``) are stored as plain strings
with app-level validation — consistent with ``AgentWebhook.type`` — to avoid pg
enum migration churn.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, Index, Text
from sqlmodel import Field, SQLModel


class GitSyncDirection:
    """Allowed sync directions for an agent git source.

    Governs which git operations are permitted against the source:
    - ``pull``: checkout/pull only (read from remote).
    - ``push``: push only (write to remote).
    - ``bidirectional``: both.
    """

    PULL = "pull"
    PUSH = "push"
    BIDIRECTIONAL = "bidirectional"


class GitSourceStatus:
    """Lifecycle status of an agent git source.

    Mirrors the knowledge ``SourceStatus`` shape (pending / connected / error /
    disconnected) but kept local to the bundles domain to avoid cross-domain
    coupling.
    """

    PENDING = "pending"
    CONNECTED = "connected"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class AgentGitSourceBase(SQLModel):
    """Shared, user-editable fields for an agent git source."""

    # HTTPS or SSH; normalized by the git_operations URL converters.
    repo_url: str = Field(max_length=2048)
    # Path within the repo (several agents may live in one repo); NULL = root.
    subdir: str | None = Field(default=None, max_length=1024)
    # Branch or tag to track.
    ref: str = Field(default="main", max_length=255)
    # Private-repo auth (host-side decrypted key only); NULL = public repo.
    ssh_key_id: uuid.UUID | None = Field(
        default=None, foreign_key="user_ssh_keys.id", ondelete="SET NULL"
    )
    # One of GitSyncDirection.* — governs which ops are allowed.
    sync_direction: str = Field(default=GitSyncDirection.BIDIRECTIONAL, max_length=32)


class AgentGitSource(AgentGitSourceBase, table=True):
    """Database model: one git source backing one agent install."""

    __tablename__ = "agent_git_source"
    __table_args__ = (
        # One git source per install (unique) + the per-agent lookup index.
        Index("ix_agent_git_source_agent_id", "agent_id", unique=True),
        Index("ix_agent_git_source_owner_id", "owner_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # The install this source backs.
    agent_id: uuid.UUID = Field(
        foreign_key="agent.id", nullable=False, ondelete="CASCADE"
    )
    # Per-agent ownership scope.
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    # Git analog of Agent.bundle_uuid; the bundle this git source mirrors.
    bundle_uuid: uuid.UUID | None = Field(
        default=None, foreign_key="agent_bundle.id", ondelete="SET NULL"
    )

    # SHA git analog of installed_revision_id; idempotency pin for pull/push.
    last_synced_commit: str | None = Field(default=None, max_length=64)
    last_sync_at: datetime | None = Field(default=None)
    # One of GitSourceStatus.*
    status: str = Field(default=GitSourceStatus.PENDING, max_length=32)
    # Last failure detail (free-form).
    last_error: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentGitSourcePublic(AgentGitSourceBase):
    """API response schema. Never includes SSH key material."""

    id: uuid.UUID
    agent_id: uuid.UUID
    owner_id: uuid.UUID
    bundle_uuid: uuid.UUID | None = None
    status: str
    last_synced_commit: str | None = None
    last_sync_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    # Set by the route from a cheap ls-remote when requested; default False.
    update_available: bool = False
    # Browser URL to the remote's commit history for this agent's subdir, set by
    # the route layer. Only populated for hosts with a known web layout (GitHub
    # today); ``None`` otherwise so the UI can hide the "View history" link.
    web_history_url: str | None = None
    # Browser URL to the repo tree at this agent's branch + subdir (the "open the
    # repo" link on the repo name). Same provider gating as ``web_history_url``.
    web_tree_url: str | None = None


class AgentGitSourceCreate(AgentGitSourceBase):
    """API input for creating a git source (e.g. the checkout body, Phase 3).

    Inherits the Base fields verbatim so the ``max_length`` validation on
    ``repo_url`` / ``subdir`` / ``ref`` applies to user input too.
    """


class AgentGitSourceUpdate(SQLModel):
    """API input for updating a git source — all fields optional.

    Mirrors the Base ``max_length`` constraints on the optional variants.
    """

    repo_url: str | None = Field(default=None, max_length=2048)
    subdir: str | None = Field(default=None, max_length=1024)
    ref: str | None = Field(default=None, max_length=255)
    ssh_key_id: uuid.UUID | None = None
    sync_direction: str | None = Field(default=None, max_length=32)
