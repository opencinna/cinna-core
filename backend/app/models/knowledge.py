"""
Knowledge management models for Git-based knowledge repositories.

This module defines models for managing knowledge sources (Git repositories),
workspace permissions, and knowledge articles with embeddings.
"""

import uuid
from datetime import datetime, UTC
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, Column, ForeignKey, Index, Text
from sqlmodel import Field, Relationship, SQLModel


class SourceStatus(str, Enum):
    """Status of a knowledge source."""

    pending = "pending"
    connected = "connected"
    error = "error"
    disconnected = "disconnected"


class WorkspaceAccessType(str, Enum):
    """Type of workspace access for a knowledge source."""

    all = "all"
    specific = "specific"


# Knowledge Git Repository Model
class AIKnowledgeGitRepoBase(SQLModel):
    """Base model for knowledge git repository."""

    name: str = Field(index=True)
    description: Optional[str] = None
    git_url: str
    branch: str = Field(default="main")
    ssh_key_id: Optional[uuid.UUID] = Field(default=None, foreign_key="user_ssh_keys.id")
    is_enabled: bool = Field(default=True, index=True)
    status: SourceStatus = Field(default=SourceStatus.pending)
    status_message: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    sync_commit_hash: Optional[str] = None
    workspace_access_type: WorkspaceAccessType = Field(default=WorkspaceAccessType.all)
    public_discovery: bool = Field(default=False, index=True)


class AIKnowledgeGitRepo(AIKnowledgeGitRepoBase, table=True):
    """Knowledge git repository table."""

    __tablename__ = "ai_knowledge_git_repo"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Relationships
    # user: Optional["User"] = Relationship(back_populates="knowledge_repos")
    workspace_permissions: list["AIKnowledgeGitRepoWorkspace"] = Relationship(
        back_populates="git_repo", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    articles: list["KnowledgeArticle"] = Relationship(
        back_populates="git_repo", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class AIKnowledgeGitRepoPublic(AIKnowledgeGitRepoBase):
    """Public schema for knowledge git repository."""

    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    article_count: int = 0


class AIKnowledgeGitRepoCreate(SQLModel):
    """Schema for creating a knowledge git repository."""

    name: str
    description: Optional[str] = None
    git_url: str
    branch: str = "main"
    ssh_key_id: Optional[uuid.UUID] = None
    workspace_access_type: WorkspaceAccessType = WorkspaceAccessType.all
    workspace_ids: Optional[list[uuid.UUID]] = None


class AIKnowledgeGitRepoUpdate(SQLModel):
    """Schema for updating a knowledge git repository."""

    name: Optional[str] = None
    description: Optional[str] = None
    branch: Optional[str] = None
    ssh_key_id: Optional[uuid.UUID] = None
    is_enabled: Optional[bool] = None
    workspace_access_type: Optional[WorkspaceAccessType] = None
    workspace_ids: Optional[list[uuid.UUID]] = None
    public_discovery: Optional[bool] = None


# Workspace Permissions Model
class AIKnowledgeGitRepoWorkspace(SQLModel, table=True):
    """Link table for knowledge git repository workspace permissions."""

    __tablename__ = "ai_knowledge_git_repo_workspaces"
    __table_args__ = (
        Index("idx_git_repo_workspace_unique", "git_repo_id", "user_workspace_id", unique=True),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    git_repo_id: uuid.UUID = Field(foreign_key="ai_knowledge_git_repo.id", ondelete="CASCADE", index=True)
    user_workspace_id: uuid.UUID = Field(foreign_key="user_workspace.id", ondelete="CASCADE", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Relationships
    git_repo: Optional[AIKnowledgeGitRepo] = Relationship(back_populates="workspace_permissions")


# User Enabled Discoverable Sources Model
class UserEnabledDiscoverableSource(SQLModel, table=True):
    """Link table for users who enabled discoverable knowledge sources."""

    __tablename__ = "user_enabled_discoverable_sources"
    __table_args__ = (
        Index("idx_user_source_unique", "user_id", "git_repo_id", unique=True),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    git_repo_id: uuid.UUID = Field(foreign_key="ai_knowledge_git_repo.id", ondelete="CASCADE", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Discoverable Source Public Schema
class DiscoverableSourcePublic(SQLModel):
    """Public schema for discoverable knowledge sources (read-only admin view)."""

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    status: SourceStatus
    article_count: int = 0
    owner_username: Optional[str] = None


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
