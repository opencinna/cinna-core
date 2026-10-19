"""
Knowledge management models for Git-based knowledge repositories.

This module defines models for managing knowledge sources (Git repositories),
their access level / user shares, and knowledge articles with embeddings.

Knowledge sources are server-wide and managed by superusers only. ``user_id``
on a source records the admin who created it; it does not scope management.
"""

import uuid
from datetime import datetime, UTC
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, Text, UniqueConstraint, Uuid
from sqlmodel import Field, Relationship, SQLModel


class SourceStatus(str, Enum):
    """Status of a knowledge source."""

    pending = "pending"
    connected = "connected"
    error = "error"
    disconnected = "disconnected"


class KnowledgeSourceAccessLevel(str, Enum):
    """Who may query a knowledge source (via agents or CLI knowledge search).

    - private: superusers only
    - public: every user on the server
    - shared: superusers plus the users listed in the source's share list
    """

    private = "private"
    public = "public"
    shared = "shared"


# Knowledge Git Repository Model
class AIKnowledgeGitRepoBase(SQLModel):
    """Base model for knowledge git repository."""

    name: str = Field(index=True)
    description: Optional[str] = None
    git_url: str
    branch: str = Field(default="main")
    # SET NULL: deleting the key's owner (cascades their keys) keeps the source;
    # the next check/refresh of a private repo then fails. Direct key deletion
    # is guarded with a 409 in SSHKeyService.delete_key.
    # DEFERRABLE INITIALLY DEFERRED: deleting an admin who is both creator and
    # key owner reaches the row via two cascade paths (user_id SET NULL and
    # user_ssh_keys CASCADE -> ssh_key_id SET NULL); an immediate FK check on
    # the intermediate row version fails, so the check runs at commit instead.
    ssh_key_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(
            Uuid,
            ForeignKey(
                "user_ssh_keys.id",
                ondelete="SET NULL",
                deferrable=True,
                initially="DEFERRED",
            ),
            nullable=True,
        ),
    )
    is_enabled: bool = Field(default=True, index=True)
    status: SourceStatus = Field(default=SourceStatus.pending)
    status_message: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    sync_commit_hash: Optional[str] = None
    access_level: KnowledgeSourceAccessLevel = Field(
        default=KnowledgeSourceAccessLevel.private, index=True
    )


class AIKnowledgeGitRepo(AIKnowledgeGitRepoBase, table=True):
    """Knowledge git repository table."""

    __tablename__ = "ai_knowledge_git_repo"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Creator (metadata only). SET NULL: deleting the admin keeps the server-wide source.
    user_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL", nullable=True, index=True
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Relationships
    articles: list["KnowledgeArticle"] = Relationship(
        back_populates="git_repo", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class AIKnowledgeGitRepoPublic(AIKnowledgeGitRepoBase):
    """Public schema for knowledge git repository."""

    id: uuid.UUID
    # Creator of the source (metadata only; every superuser manages every source).
    # None once the creating admin has been deleted.
    user_id: Optional[uuid.UUID] = None
    created_by_email: Optional[str] = None
    created_by_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    article_count: int = 0
    shared_user_count: int = 0
    shared_group_count: int = 0


class AIKnowledgeGitRepoCreate(SQLModel):
    """Schema for creating a knowledge git repository."""

    name: str
    description: Optional[str] = None
    git_url: str
    branch: str = "main"
    ssh_key_id: Optional[uuid.UUID] = None
    access_level: KnowledgeSourceAccessLevel = KnowledgeSourceAccessLevel.private


class AIKnowledgeGitRepoUpdate(SQLModel):
    """Schema for updating a knowledge git repository."""

    name: Optional[str] = None
    description: Optional[str] = None
    branch: Optional[str] = None
    ssh_key_id: Optional[uuid.UUID] = None
    is_enabled: Optional[bool] = None
    access_level: Optional[KnowledgeSourceAccessLevel] = None


# User Share Model (access_level == shared)
class AIKnowledgeGitRepoUserShare(SQLModel, table=True):
    """Link table: users granted query access to a ``shared`` knowledge source."""

    __tablename__ = "ai_knowledge_git_repo_user_shares"
    __table_args__ = (
        UniqueConstraint("git_repo_id", "user_id", name="uq_knowledge_repo_user_share"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    git_repo_id: uuid.UUID = Field(foreign_key="ai_knowledge_git_repo.id", ondelete="CASCADE", index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AIKnowledgeGitRepoGroupShare(SQLModel, table=True):
    """Link table: user groups granted query access to a ``shared`` knowledge source.

    Members are resolved live through ``user_group_member``.
    """

    __tablename__ = "ai_knowledge_git_repo_group_share"
    __table_args__ = (
        UniqueConstraint("git_repo_id", "group_id", name="uq_knowledge_repo_group_share"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    git_repo_id: uuid.UUID = Field(
        foreign_key="ai_knowledge_git_repo.id", ondelete="CASCADE", index=True
    )
    group_id: uuid.UUID = Field(foreign_key="user_group.id", ondelete="CASCADE", index=True)
    # SET NULL DEFERRABLE INITIALLY DEFERRED: sources outlive their admins, and
    # an admin deleted alongside other cascades must not trip an immediate check.
    granted_by_user_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(
            Uuid,
            ForeignKey(
                "user.id",
                ondelete="SET NULL",
                deferrable=True,
                initially="DEFERRED",
            ),
            nullable=True,
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=DateTime(timezone=True),
        nullable=False,
    )


class KnowledgeSourceShareCreate(SQLModel):
    """Request body for adding a user to a knowledge source's share list."""

    user_id: uuid.UUID


class KnowledgeSourceSharedUserPublic(SQLModel):
    """A user on a knowledge source's share list."""

    user_id: uuid.UUID
    email: str
    full_name: Optional[str] = None
    created_at: datetime


class KnowledgeSourceGroupShareCreate(SQLModel):
    """Request body for adding a user group to a knowledge source's share list."""

    group_id: uuid.UUID


class KnowledgeSourceSharedGroupPublic(SQLModel):
    """A user group on a knowledge source's share list."""

    group_id: uuid.UUID
    name: str
    member_count: int = 0
    created_at: datetime


# Knowledge Article Model
class KnowledgeArticleBase(SQLModel):
    """Base model for knowledge article."""

    title: str
    description: str
    tags: list[str] = Field(default=[], sa_column=Column(JSON))
    features: list[str] = Field(default=[], sa_column=Column(JSON))
    file_path: str
    content: str
    content_hash: str
    embedding_model: Optional[str] = None
    embedding_dimensions: Optional[int] = None
    commit_hash: Optional[str] = None


class KnowledgeArticle(KnowledgeArticleBase, table=True):
    """Knowledge article table with vector embeddings."""

    __tablename__ = "knowledge_articles"
    __table_args__ = (
        Index("idx_article_repo_path_unique", "git_repo_id", "file_path", unique=True),
        Index("idx_articles_repo", "git_repo_id"),
        Index("idx_articles_model", "embedding_model"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    git_repo_id: uuid.UUID = Field(foreign_key="ai_knowledge_git_repo.id", ondelete="CASCADE", index=True)
    # NOTE: Using JSON for now, will be migrated to pgvector when embedding functionality is implemented
    embedding: Optional[list[float]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Relationships
    git_repo: Optional[AIKnowledgeGitRepo] = Relationship(back_populates="articles")


class KnowledgeArticlePublic(SQLModel):
    """Public schema for knowledge article."""

    id: uuid.UUID
    git_repo_id: uuid.UUID
    title: str
    description: str
    tags: list[str]
    features: list[str]
    file_path: str
    embedding_model: Optional[str]
    embedding_dimensions: Optional[int]
    updated_at: datetime


class KnowledgeArticleDetail(KnowledgeArticlePublic):
    """Detailed schema for knowledge article including content."""

    content: str
    commit_hash: Optional[str]


# Knowledge Article Chunk Model
class KnowledgeArticleChunkBase(SQLModel):
    """Base model for knowledge article chunk."""

    chunk_index: int
    chunk_text: str = Field(sa_column=Column(Text))
    embedding_model: Optional[str] = None
    embedding_dimensions: Optional[int] = None


class KnowledgeArticleChunk(KnowledgeArticleChunkBase, table=True):
    """Knowledge article chunk table with vector embeddings for semantic search."""

    __tablename__ = "knowledge_article_chunks"
    __table_args__ = (
        Index("idx_chunk_article_idx_unique", "article_id", "chunk_index", unique=True),
        Index("idx_chunks_article", "article_id"),
        Index("idx_chunks_model", "embedding_model"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    article_id: uuid.UUID = Field(foreign_key="knowledge_articles.id", ondelete="CASCADE", index=True)
    # Using pgvector type for vector storage
    # Will store embeddings as vector type with dynamic dimensions
    embedding: Optional[list[float]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Relationships
    article: Optional[KnowledgeArticle] = Relationship()


class KnowledgeArticleChunkPublic(SQLModel):
    """Public schema for knowledge article chunk."""

    id: uuid.UUID
    article_id: uuid.UUID
    chunk_index: int
    chunk_text: str
    embedding_model: Optional[str]
    embedding_dimensions: Optional[int]


# Response schemas
class CheckAccessResponse(SQLModel):
    """Response for check access endpoint."""

    accessible: bool
    message: str


class RefreshKnowledgeResponse(SQLModel):
    """Response for refresh knowledge endpoint."""

    status: str
    message: str
    task_id: Optional[str] = None


# Knowledge Query Response Schemas
class ArticleListItem(SQLModel):
    """Article metadata for discovery step."""

    id: uuid.UUID
    title: str
    description: str
    tags: list[str]
    features: list[str]
    source_name: str
    git_repo_id: uuid.UUID


class ArticleContent(SQLModel):
    """Full article content for retrieval step."""

    id: uuid.UUID
    title: str
    description: str
    content: str
    file_path: str
    tags: list[str]
    features: list[str]
    source_name: str
