"""Server-hosted git repositories: the content store of desktop agents.

A ``GitRepo`` is one bare repository on the instance's git storage
(``settings.GIT_STORAGE_DIR``), served over smart HTTP at
``/git/<repo_id>.git``. ``kind='agent'`` repos hold one desktop agent folder
(tree v3, the kit's agent folder as-is); ``kind='mirror'`` repos mirror an
external marketplace (later phase).

Who a desktop agent is and who may reach it lives on the catalog side: each
agent repo backs exactly one ``AgentBundle(runs_on='desktop')``
(``agent_bundle.git_repo_id``), whose ``visibility`` and
``bundle_access_grant`` rows decide access (see
:class:`app.services.catalog.catalog_access_service.CatalogAccessService`).
"""
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import JSON, Column, DateTime
from sqlmodel import Field, SQLModel


class GitRepoKind:
    """``git_repo.kind`` values (plain strings, not a pg enum)."""

    AGENT = "agent"
    MIRROR = "mirror"

    ALL = (AGENT, MIRROR)


#: Kit slug pattern (``schema/cinna-agent.schema.json``), unique per instance
#: among desktop bundles.
KIT_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GitRepo(SQLModel, table=True):
    """One bare repository: the content behind one desktop agent."""

    __tablename__ = "git_repo"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    kind: str = Field(max_length=16, nullable=False)
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
    # Moves on every accepted push.
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
