"""
API routes for knowledge source management.

Admin-only feature: only superusers can create, manage, and configure
knowledge sources. Public sources are automatically available to all users
via the knowledge query tool.
"""

import uuid
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import SessionDep, get_current_active_superuser
from app.models import User
from app.models.knowledge import (
    AIKnowledgeGitRepoCreate,
    AIKnowledgeGitRepoPublic,
    AIKnowledgeGitRepoUpdate,
    CheckAccessResponse,
    DiscoverableSourcePublic,
    KnowledgeArticlePublic,
    RefreshKnowledgeResponse,
)
from app.services import knowledge_source_service

router = APIRouter(prefix="/knowledge-sources", tags=["knowledge-sources"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


@router.get("/", response_model=list[AIKnowledgeGitRepoPublic])
def list_knowledge_sources(
    session: SessionDep,
    current_user: SuperUser,
    workspace_id: Optional[uuid.UUID] = Query(None),
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """
    Retrieve knowledge sources for the current admin user.

    Only superusers can manage knowledge sources.
    """
    sources = knowledge_source_service.get_user_sources(
        session=session,
        user_id=current_user.id,
        workspace_id=workspace_id,
        skip=skip,
        limit=limit,
    )
    return sources


@router.post("/", response_model=AIKnowledgeGitRepoPublic)
def create_knowledge_source(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_in: AIKnowledgeGitRepoCreate,
) -> Any:
    """
    Create a new knowledge source. Admin only.
    """
    source = knowledge_source_service.create_source(
        session=session,
        user_id=current_user.id,
        data=source_in,
    )
    return source


@router.get("/{source_id}", response_model=AIKnowledgeGitRepoPublic)
def get_knowledge_source(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
) -> Any:
    """
    Get a knowledge source by ID. Admin only.
    """
    source = knowledge_source_service.get_source_by_id(
        session=session,
        source_id=source_id,
        user_id=current_user.id,
    )
    if not source:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    return source


@router.put("/{source_id}", response_model=AIKnowledgeGitRepoPublic)
def update_knowledge_source(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
    source_in: AIKnowledgeGitRepoUpdate,
) -> Any:
    """
    Update a knowledge source. Admin only.
    """
    source = knowledge_source_service.update_source(
        session=session,
        source_id=source_id,
        user_id=current_user.id,
        data=source_in,
    )
    if not source:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    return source


@router.delete("/{source_id}")
def delete_knowledge_source(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
) -> Any:
    """
    Delete a knowledge source. Admin only.
    """
    deleted = knowledge_source_service.delete_source(
        session=session,
        source_id=source_id,
        user_id=current_user.id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    return {"ok": True}


@router.post("/{source_id}/enable", response_model=AIKnowledgeGitRepoPublic)
def enable_knowledge_source(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
) -> Any:
    """
    Enable a knowledge source. Admin only.
    """
    source = knowledge_source_service.enable_source(
        session=session,
        source_id=source_id,
        user_id=current_user.id,
    )
    if not source:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    return source


@router.post("/{source_id}/disable", response_model=AIKnowledgeGitRepoPublic)
def disable_knowledge_source(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
) -> Any:
    """
    Disable a knowledge source. Admin only.
    """
    source = knowledge_source_service.disable_source(
        session=session,
        source_id=source_id,
        user_id=current_user.id,
    )
    if not source:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    return source


@router.post("/{source_id}/check-access", response_model=CheckAccessResponse)
def check_knowledge_source_access(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
) -> Any:
    """
    Check if the Git repository is accessible. Admin only.
    """
    response = knowledge_source_service.check_access(
        session=session,
        source_id=source_id,
        user_id=current_user.id,
    )
    return response


@router.post("/{source_id}/refresh", response_model=RefreshKnowledgeResponse)
def refresh_knowledge_source(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
) -> Any:
    """
    Trigger knowledge refresh from Git repository. Admin only.
    """
    response = knowledge_source_service.refresh_knowledge(
        session=session,
        source_id=source_id,
        user_id=current_user.id,
    )
    return response


@router.get("/{source_id}/articles", response_model=list[KnowledgeArticlePublic])
def list_knowledge_articles(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """
    List articles for a knowledge source. Admin only.
    """
    articles = knowledge_source_service.get_source_articles(
        session=session,
        source_id=source_id,
        user_id=current_user.id,
        skip=skip,
        limit=limit,
    )
    if articles is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")

    # Convert to public schema
    return [
        KnowledgeArticlePublic(
            id=article.id,
            git_repo_id=article.git_repo_id,
            title=article.title,
            description=article.description,
            tags=article.tags,
            features=article.features,
            file_path=article.file_path,
            embedding_model=article.embedding_model,
            embedding_dimensions=article.embedding_dimensions,
            updated_at=article.updated_at,
        )
        for article in articles
    ]


# Discoverable Sources Endpoints

@router.get("/discoverable/list", response_model=list[DiscoverableSourcePublic])
def list_discoverable_sources(
    session: SessionDep,
    current_user: SuperUser,
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """
    List public knowledge sources from other admins. Admin only, read-only.

    Shows sources marked as public_discovery=True by other admins,
    for cross-admin visibility of globally available knowledge.
    """
    sources = knowledge_source_service.get_discoverable_sources(
        session=session,
        user_id=current_user.id,
        skip=skip,
        limit=limit,
    )
    return sources
