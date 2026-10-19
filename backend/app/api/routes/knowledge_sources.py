"""
API routes for knowledge source management.

Admin-only feature: only superusers can create, manage, and configure
knowledge sources, and every superuser manages every source (the creator is
recorded as metadata only). Who may *query* a source is governed by its
``access_level`` (private / public / shared) and share list; see
``knowledge_access_service``.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from app.api.deps import SessionDep, get_current_active_superuser
from app.models import User
from app.models.knowledge.knowledge import (
    AIKnowledgeGitRepoCreate,
    AIKnowledgeGitRepoPublic,
    AIKnowledgeGitRepoUpdate,
    CheckAccessResponse,
    KnowledgeArticleDetail,
    KnowledgeArticlePublic,
    KnowledgeSourceGroupShareCreate,
    KnowledgeSourceSharedGroupPublic,
    KnowledgeSourceSharedUserPublic,
    KnowledgeSourceShareCreate,
    RefreshKnowledgeResponse,
)
from app.services.knowledge import knowledge_source_service
from app.services.knowledge.knowledge_source_service import (
    KnowledgeSourceNotFoundError,
    KnowledgeSourceValidationError,
)

router = APIRouter(prefix="/knowledge-sources", tags=["knowledge-sources"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


@router.get("/", response_model=list[AIKnowledgeGitRepoPublic])
def list_knowledge_sources(
    session: SessionDep,
    current_user: SuperUser,
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """
    List all knowledge sources on the server. Admin only.
    """
    return knowledge_source_service.list_sources(
        session=session, skip=skip, limit=limit
    )


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
    try:
        return knowledge_source_service.create_source(
            session=session,
            user_id=current_user.id,
            data=source_in,
        )
    except KnowledgeSourceValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
    try:
        source = knowledge_source_service.update_source(
            session=session,
            source_id=source_id,
            acting_user_id=current_user.id,
            data=source_in,
        )
    except KnowledgeSourceValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
    try:
        return knowledge_source_service.check_access(
            session=session,
            source_id=source_id,
        )
    except KnowledgeSourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
    try:
        return knowledge_source_service.refresh_knowledge(
            session=session,
            source_id=source_id,
        )
    except KnowledgeSourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


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


@router.get(
    "/{source_id}/articles/{article_id}",
    response_model=KnowledgeArticleDetail,
)
def get_knowledge_article(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
    article_id: uuid.UUID,
) -> Any:
    """
    Get a single article's full content.

    Admin only.
    """
    article = knowledge_source_service.get_article_content(
        session=session,
        source_id=source_id,
        article_id=article_id,
    )
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return article


@router.get("/{source_id}/export", response_class=PlainTextResponse)
def export_knowledge_source(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
) -> Any:
    """
    Export all articles of a knowledge source as a single Markdown document.

    Admin only.
    """
    doc = knowledge_source_service.export_source_markdown(
        session=session,
        source_id=source_id,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    return PlainTextResponse(
        content=doc,
        media_type="text/markdown",
        headers={
            "Content-Disposition": (
                f'attachment; filename="knowledge-source-{source_id}.md"'
            )
        },
    )


# Share list endpoints (effective when access_level == shared)


@router.get(
    "/{source_id}/shared-users",
    response_model=list[KnowledgeSourceSharedUserPublic],
)
def list_knowledge_source_shared_users(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
) -> Any:
    """
    List users a knowledge source is shared with. Admin only.
    """
    try:
        return knowledge_source_service.list_shared_users(
            session=session, source_id=source_id
        )
    except KnowledgeSourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post(
    "/{source_id}/shared-users",
    response_model=KnowledgeSourceSharedUserPublic,
)
def add_knowledge_source_shared_user(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
    share_in: KnowledgeSourceShareCreate,
) -> Any:
    """
    Share a knowledge source with a user (idempotent). Admin only.

    The share takes effect while the source's access level is ``shared``.
    """
    try:
        return knowledge_source_service.add_shared_user(
            session=session, source_id=source_id, user_id=share_in.user_id
        )
    except KnowledgeSourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{source_id}/shared-users/{user_id}")
def remove_knowledge_source_shared_user(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Any:
    """
    Remove a user from a knowledge source's share list. Admin only.
    """
    try:
        knowledge_source_service.remove_shared_user(
            session=session, source_id=source_id, user_id=user_id
        )
    except KnowledgeSourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.get(
    "/{source_id}/shared-groups",
    response_model=list[KnowledgeSourceSharedGroupPublic],
)
def list_knowledge_source_shared_groups(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
) -> Any:
    """
    List user groups a knowledge source is shared with. Admin only.
    """
    try:
        return knowledge_source_service.list_shared_groups(
            session=session, source_id=source_id
        )
    except KnowledgeSourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post(
    "/{source_id}/shared-groups",
    response_model=KnowledgeSourceSharedGroupPublic,
)
def add_knowledge_source_shared_group(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
    share_in: KnowledgeSourceGroupShareCreate,
) -> Any:
    """
    Share a knowledge source with a user group (idempotent). Admin only.

    Members are resolved live; the share takes effect while the source's
    access level is ``shared``.
    """
    try:
        return knowledge_source_service.add_shared_group(
            session=session,
            source_id=source_id,
            group_id=share_in.group_id,
            granted_by_user_id=current_user.id,
        )
    except KnowledgeSourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{source_id}/shared-groups/{group_id}")
def remove_knowledge_source_shared_group(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
    group_id: uuid.UUID,
) -> Any:
    """
    Remove a user group from a knowledge source's share list. Admin only.
    """
    try:
        knowledge_source_service.remove_shared_group(
            session=session, source_id=source_id, group_id=group_id
        )
    except KnowledgeSourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}
