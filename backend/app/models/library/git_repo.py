"""Team library: server-hosted git repositories and who may reach them.

A ``GitRepo`` is one bare repository on the instance's git storage
(``settings.GIT_STORAGE_DIR``), served over smart HTTP at
``/git/<repo_id>.git``. ``kind='agent'`` repos hold one shared agent folder
(tree v3, the kit's agent folder as-is); ``kind='mirror'`` repos mirror an
external marketplace (later phase).

Access is never implied by ownership: a ``GitRepoGrant`` row names a principal
(one user, every user of a role, or everyone) and an access level. Effective
access is the maximum over the grants that match the caller; see
:class:`app.services.library.library_access_service.LibraryAccessService`.

The API schemas at the bottom are the wire contract of ``/api/v1/library``
(cinna-desktop ``plans/team-sharing/README.md`` §4.2). Field names are part of
that contract; do not rename them.
"""
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

import sqlalchemy as sa
from pydantic import ConfigDict, StringConstraints
from sqlalchemy import JSON, Column, DateTime, Index, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class GitRepoKind:
    """``git_repo.kind`` values (plain strings, not a pg enum)."""

    AGENT = "agent"
    MIRROR = "mirror"

    ALL = (AGENT, MIRROR)


class LibraryPrincipalKind:
    """``git_repo_grant.principal_kind`` values."""

    USER = "user"
    ROLE = "role"
    EVERYONE = "everyone"

    ALL = (USER, ROLE, EVERYONE)


#: ``principal_id`` stored for an ``everyone`` grant (NOT NULL so the unique
#: key ``(repo_id, principal_kind, principal_id)`` holds for it too).
EVERYONE_PRINCIPAL_ID = "*"


class LibraryAccess:
    """Access levels, ordered: each level includes the ones before it."""

    READ = "read"
    WRITE = "write"
    MAINTAIN = "maintain"

    ORDER = (READ, WRITE, MAINTAIN)

    @classmethod
    def rank(cls, access: str | None) -> int:
        """0 for no access, then 1..3 in :attr:`ORDER`."""
        if access is None or access not in cls.ORDER:
            return 0
        return cls.ORDER.index(access) + 1

    @classmethod
    def max(cls, *levels: str | None) -> str | None:
        best: str | None = None
        for level in levels:
            if cls.rank(level) > cls.rank(best):
                best = level
        return best


LibraryAccessLevel = Literal["read", "write", "maintain"]

#: Kit slug pattern (``schema/cinna-agent.schema.json``), unique per instance
#: among ``kind='agent'`` repos.
LIBRARY_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ── Tables ──────────────────────────────────────────────────────────


class GitRepo(SQLModel, table=True):
    """One bare repository in the team library."""

    __tablename__ = "git_repo"
    __table_args__ = (
        Index(
            "uq_git_repo_agent_slug",
            "slug",
            unique=True,
            postgresql_where=sa.text("kind = 'agent'"),
        ),
        Index("ix_git_repo_owner_user_id", "owner_user_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    kind: str = Field(max_length=16, nullable=False)
    slug: str = Field(max_length=64, nullable=False)
    name: str = Field(max_length=255, nullable=False)
    description: str = Field(
        default="",
        sa_column=Column(Text, nullable=False, server_default=""),
    )
    # Creator. The repo survives the user's deletion (SET NULL).
    owner_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL", nullable=True
    )
    # Mirror repos only (later phase).
    marketplace_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="llm_plugin_marketplace.id",
        ondelete="SET NULL",
        nullable=True,
    )
    default_branch: str = Field(
        default="main",
        max_length=64,
        sa_column_kwargs={"server_default": "main"},
    )
    # NULL until the first accepted push.
    head_sha: str | None = Field(default=None, max_length=64, nullable=True)
    # Who made the last accepted push; NULL before it or once they are deleted.
    last_pushed_by_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL", nullable=True
    )
    # ``{id, name, description, runtime: {engine}, contract_version}`` read from
    # ``cinna-agent.json`` at HEAD; NULL until the first accepted push.
    manifest_cache: dict | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Moves on every accepted push and every metadata edit.
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class GitRepoGrant(SQLModel, table=True):
    """One principal's access to one library repo."""

    __tablename__ = "git_repo_grant"
    __table_args__ = (
        UniqueConstraint(
            "repo_id",
            "principal_kind",
            "principal_id",
            name="uq_git_repo_grant_principal",
        ),
        Index("ix_git_repo_grant_repo_id", "repo_id"),
        Index("ix_git_repo_grant_user_id", "user_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    repo_id: uuid.UUID = Field(
        foreign_key="git_repo.id", nullable=False, ondelete="CASCADE"
    )
    principal_kind: str = Field(max_length=16, nullable=False)
    # User uuid as text / role value / ``*`` for everyone.
    principal_id: str = Field(max_length=64, nullable=False)
    # Set only for ``principal_kind='user'`` so deleting the user drops the grant.
    user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="CASCADE", nullable=True
    )
    access: str = Field(max_length=16, nullable=False)
    granted_by_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL", nullable=True
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


# ── API schemas (README §4.2) ───────────────────────────────────────


class LibraryPrincipalUser(SQLModel):
    # ``forbid`` keeps the union unambiguous: ``{"user_id", "role"}`` is an
    # error, never silently read as one of the two.
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID


class LibraryPrincipalRole(SQLModel):
    model_config = ConfigDict(extra="forbid")

    role: str


LibraryPrincipal = LibraryPrincipalUser | LibraryPrincipalRole | Literal["everyone"]


class LibraryGrantIn(SQLModel):
    """One entry of ``PUT /library/agents/{repo_id}/grants`` (full replace)."""

    principal: LibraryPrincipal
    access: LibraryAccessLevel


class LibraryGrantPublic(SQLModel):
    principal: LibraryPrincipal
    access: LibraryAccessLevel
    # Person's full name or email, the role value, or "Everyone".
    display_name: str
    granted_by: str | None = None
    created_at: datetime


class LibraryMaintainer(SQLModel):
    user_id: uuid.UUID
    display_name: str


class LibraryPusher(SQLModel):
    """The person behind the last accepted push."""

    user_id: uuid.UUID
    display_name: str


class LibraryManifestRuntime(SQLModel):
    engine: str | None = None


class LibraryManifest(SQLModel):
    """The projection of ``cinna-agent.json`` at HEAD the library shows."""

    id: str | None = None
    name: str | None = None
    description: str | None = None
    runtime: LibraryManifestRuntime = Field(default_factory=LibraryManifestRuntime)
    contract_version: str | None = None


class LibraryAgentBase(SQLModel):
    repo_id: uuid.UUID
    slug: str
    name: str
    description: str
    clone_url: str
    default_branch: str
    head_sha: str | None = None
    updated_at: datetime
    # NULL until the first accepted push, or once its pusher is deleted.
    pushed_by: LibraryPusher | None = None
    maintainers: list[LibraryMaintainer] = []
    # NULL until the first accepted push.
    manifest: LibraryManifest | None = None
    # The caller's own cloud agent bound to this repo, if any.
    cloud_install_id: uuid.UUID | None = None


class LibraryAgent(LibraryAgentBase):
    access: LibraryAccessLevel


class LibraryMirrorEntry(SQLModel):
    name: str
    kind: Literal["agent", "plugin", "skill"]
    path: str
    description: str | None = None
    version: str | None = None
    supported: bool
    unsupported_reason: str | None = None


class LibraryMirror(SQLModel):
    marketplace_id: uuid.UUID
    name: str
    clone_url: str
    head_sha: str | None = None
    format: Literal["claude", "codex", "skills", "cinna"]
    entries: list[LibraryMirrorEntry] = []


class LibraryListing(SQLModel):
    """Response of ``GET /library``."""

    agents: list[LibraryAgent] = []
    mirrors: list[LibraryMirror] = []


class LibraryAgentCreate(SQLModel):
    """Body of ``POST /library/agents``."""

    # sqlmodel 0.0.27 drops ``regex=`` silently, so the pattern rides on the type.
    slug: Annotated[str, StringConstraints(pattern=LIBRARY_SLUG_PATTERN)]
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=10_000)
    # Who else may reach it, validated like ``PUT …/grants``. The creator is
    # always added as ``maintain``; omitted means creator only.
    grants: list[LibraryGrantIn] | None = None


class LibraryOwner(SQLModel):
    user_id: uuid.UUID
    display_name: str


class LibraryAgentAdmin(LibraryAgentBase):
    """Admin listing row: a library agent without the caller's ``access``,
    plus its creator and every grant."""

    owner: LibraryOwner | None = None
    grants: list[LibraryGrantPublic] = []
