"""
Service layer for knowledge source management.

This service provides CRUD operations for Git-based knowledge repositories.
Includes Git clone/pull operations, article parsing, and database storage.
"""

import uuid
import logging
from datetime import UTC, datetime
from typing import Optional

from sqlmodel import Session, select, func

from app.models.knowledge import (
    AIKnowledgeGitRepo,
    AIKnowledgeGitRepoCreate,
    AIKnowledgeGitRepoPublic,
    AIKnowledgeGitRepoUpdate,
    AIKnowledgeGitRepoWorkspace,
    KnowledgeArticle,
    SourceStatus,
    WorkspaceAccessType,
    CheckAccessResponse,
    RefreshKnowledgeResponse,
)
from app.services.ssh_key_service import SSHKeyService
from app.services.git_operations import (
    create_ssh_key_file,
    verify_repository_access,
    clone_repository_context,
    get_current_commit_hash,
    GitOperationError,
    GitAuthenticationError,
    GitConnectionError,
)
from app.services.knowledge_article_service import (
    process_repository_articles,
    delete_orphaned_articles,
    parse_settings_json,
    chunk_and_embed_all_articles,
    ParseError,
)

logger = logging.getLogger(__name__)


def create_source(
    *,
    session: Session,
    user_id: uuid.UUID,
    data: AIKnowledgeGitRepoCreate,
) -> AIKnowledgeGitRepoPublic:
    """
    Create a new knowledge source.

    Args:
        session: Database session
        user_id: ID of the user creating the source
        data: Source creation data

    Returns:
        Created knowledge source with public schema
    """
    # Create the main source record
    source = AIKnowledgeGitRepo(
        user_id=user_id,
        name=data.name,
        description=data.description,
        git_url=data.git_url,
        branch=data.branch,
        ssh_key_id=data.ssh_key_id,
        workspace_access_type=data.workspace_access_type,
        status=SourceStatus.pending,
    )
    session.add(source)
    session.flush()  # Get the source ID

    # Create workspace permissions if specific workspaces selected
    if data.workspace_access_type == WorkspaceAccessType.specific and data.workspace_ids:
        for workspace_id in data.workspace_ids:
            permission = AIKnowledgeGitRepoWorkspace(
                git_repo_id=source.id,
                user_workspace_id=workspace_id,
            )
            session.add(permission)

    session.commit()
    session.refresh(source)

    # Get article count (will be 0 initially)
    article_count = get_article_count(session=session, source_id=source.id)

    return AIKnowledgeGitRepoPublic(
        **source.model_dump(),
        article_count=article_count,
    )


def get_user_sources(
    *,
    session: Session,
    user_id: uuid.UUID,
    workspace_id: Optional[uuid.UUID] = None,
    skip: int = 0,
    limit: int = 100,
) -> list[AIKnowledgeGitRepoPublic]:
    """
    Get all knowledge sources for a user, optionally filtered by workspace.

    Args:
        session: Database session
        user_id: ID of the user
        workspace_id: Optional workspace ID to filter by
        skip: Number of records to skip
        limit: Maximum number of records to return

    Returns:
        List of knowledge sources
    """
    query = select(AIKnowledgeGitRepo).where(
        AIKnowledgeGitRepo.user_id == user_id
    )

    if workspace_id:
        # Filter by workspace permissions
        query = query.where(
            (AIKnowledgeGitRepo.workspace_access_type == WorkspaceAccessType.all)
            | (
                AIKnowledgeGitRepo.id.in_(
                    select(AIKnowledgeGitRepoWorkspace.git_repo_id).where(
                        AIKnowledgeGitRepoWorkspace.user_workspace_id == workspace_id
                    )
                )
            )
        )

    query = query.offset(skip).limit(limit)
    sources = session.exec(query).all()

    # Add article counts
    result = []
    for source in sources:
        article_count = get_article_count(session=session, source_id=source.id)
        result.append(
            AIKnowledgeGitRepoPublic(
                **source.model_dump(),
                article_count=article_count,
            )
        )

    return result


def get_source_by_id(
    *,
    session: Session,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[AIKnowledgeGitRepoPublic]:
    """
    Get a knowledge source by ID.

    Args:
        session: Database session
        source_id: ID of the source
        user_id: ID of the user (for ownership check)

    Returns:
        Knowledge source or None if not found or not owned by user
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source or source.user_id != user_id:
        return None

    article_count = get_article_count(session=session, source_id=source.id)
    return AIKnowledgeGitRepoPublic(
        **source.model_dump(),
        article_count=article_count,
    )


def update_source(
    *,
    session: Session,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
    data: AIKnowledgeGitRepoUpdate,
) -> Optional[AIKnowledgeGitRepoPublic]:
    """
    Update a knowledge source.

    Args:
        session: Database session
        source_id: ID of the source
        user_id: ID of the user (for ownership check)
        data: Update data

    Returns:
        Updated knowledge source or None if not found or not owned by user
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source or source.user_id != user_id:
        return None

    # Check if Git config changed (requires re-verification)
    git_config_changed = False
    if data.branch is not None or data.ssh_key_id is not None:
        git_config_changed = True

    # Update fields
    update_data = data.model_dump(exclude_unset=True, exclude={"workspace_ids"})
    for field, value in update_data.items():
        setattr(source, field, value)

    # If Git config changed, mark as pending
    if git_config_changed:
        source.status = SourceStatus.pending
        source.status_message = "Configuration updated. Please check access."

    source.updated_at = datetime.now(UTC)

    # Update workspace permissions if changed
    if data.workspace_ids is not None and data.workspace_access_type == WorkspaceAccessType.specific:
        # Delete existing permissions
        session.exec(
            select(AIKnowledgeGitRepoWorkspace).where(
                AIKnowledgeGitRepoWorkspace.git_repo_id == source_id
            )
        )
        for perm in session.exec(
            select(AIKnowledgeGitRepoWorkspace).where(
                AIKnowledgeGitRepoWorkspace.git_repo_id == source_id
            )
        ).all():
            session.delete(perm)

        # Create new permissions
        for workspace_id in data.workspace_ids:
            permission = AIKnowledgeGitRepoWorkspace(
                git_repo_id=source_id,
                user_workspace_id=workspace_id,
            )
            session.add(permission)

    session.commit()
    session.refresh(source)

    article_count = get_article_count(session=session, source_id=source.id)
    return AIKnowledgeGitRepoPublic(
        **source.model_dump(),
        article_count=article_count,
    )


def delete_source(
    *,
    session: Session,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """
    Delete a knowledge source.

    Args:
        session: Database session
        source_id: ID of the source
        user_id: ID of the user (for ownership check)

    Returns:
        True if deleted, False if not found or not owned by user
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source or source.user_id != user_id:
        return False

    session.delete(source)
    session.commit()
    return True


def enable_source(
    *,
    session: Session,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[AIKnowledgeGitRepoPublic]:
    """
    Enable a knowledge source.

    Args:
        session: Database session
        source_id: ID of the source
        user_id: ID of the user (for ownership check)

    Returns:
        Updated knowledge source or None if not found or not owned by user
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source or source.user_id != user_id:
        return None

    source.is_enabled = True
    source.updated_at = datetime.now(UTC)
    session.commit()
    session.refresh(source)

    article_count = get_article_count(session=session, source_id=source.id)
    return AIKnowledgeGitRepoPublic(
        **source.model_dump(),
        article_count=article_count,
    )


def disable_source(
    *,
    session: Session,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[AIKnowledgeGitRepoPublic]:
    """
    Disable a knowledge source.

    Args:
        session: Database session
        source_id: ID of the source
        user_id: ID of the user (for ownership check)

    Returns:
        Updated knowledge source or None if not found or not owned by user
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source or source.user_id != user_id:
        return None

    source.is_enabled = False
    source.updated_at = datetime.now(UTC)
    session.commit()
    session.refresh(source)

    article_count = get_article_count(session=session, source_id=source.id)
    return AIKnowledgeGitRepoPublic(
        **source.model_dump(),
        article_count=article_count,
    )


def check_access(
    *,
    session: Session,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
) -> CheckAccessResponse:
    """
    Check if the Git repository is accessible.

    Verifies repository access using Git ls-remote without cloning.
    Updates source status based on the result.

    Args:
        session: Database session
        source_id: ID of the source
        user_id: ID of the user (for ownership check)

    Returns:
        Access check response with accessibility status and message
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source or source.user_id != user_id:
        return CheckAccessResponse(
            accessible=False,
            message="Source not found or access denied",
        )

    ssh_key_path = None
    temp_key_context = None

    try:
        # If SSH key is required, decrypt it
        if source.ssh_key_id:
            key_data = SSHKeyService.get_decrypted_private_key(
                session=session,
                key_id=source.ssh_key_id,
                user_id=user_id
            )

            if not key_data:
                source.status = SourceStatus.error
                source.status_message = "SSH key not found or access denied"
                source.last_checked_at = datetime.now(UTC)
                source.updated_at = datetime.now(UTC)
                session.commit()

                return CheckAccessResponse(
                    accessible=False,
                    message="SSH key not found or access denied"
                )

            private_key, passphrase = key_data

            # Create temporary SSH key file
            temp_key_context = create_ssh_key_file(private_key, passphrase)
            ssh_key_path = temp_key_context.__enter__()

        # Verify repository access
        accessible, message = verify_repository_access(
            git_url=source.git_url,
            branch=source.branch,
            ssh_key_path=ssh_key_path
        )

        # Update source status
        if accessible:
            source.status = SourceStatus.connected
            source.status_message = message
        else:
            source.status = SourceStatus.error
            source.status_message = message

        source.last_checked_at = datetime.now(UTC)
        source.updated_at = datetime.now(UTC)
        session.commit()

        logger.info(f"Access check for source {source_id}: {accessible}")

        return CheckAccessResponse(
            accessible=accessible,
            message=message
        )

    except Exception as e:
        logger.error(f"Unexpected error checking access for source {source_id}: {e}")

        source.status = SourceStatus.error
        source.status_message = f"Unexpected error: {str(e)}"
        source.last_checked_at = datetime.now(UTC)
        source.updated_at = datetime.now(UTC)
        session.commit()

        return CheckAccessResponse(
            accessible=False,
            message=f"Unexpected error: {str(e)}"
        )

    finally:
        # Clean up temporary SSH key file
        if temp_key_context:
            try:
                temp_key_context.__exit__(None, None, None)
            except Exception as e:
                logger.warning(f"Failed to clean up SSH key file: {e}")


def refresh_knowledge(
    *,
    session: Session,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
) -> RefreshKnowledgeResponse:
    """
    Trigger knowledge refresh from Git repository.

    Clones the repository, parses .ai-knowledge/settings.json,
    and stores articles in the database. Embeddings will be generated in a later phase.

    Args:
        session: Database session
        source_id: ID of the source
        user_id: ID of the user (for ownership check)

    Returns:
        Refresh response with status and details
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source or source.user_id != user_id:
        return RefreshKnowledgeResponse(
            status="error",
            message="Source not found or access denied",
        )

    if not source.is_enabled:
        return RefreshKnowledgeResponse(
            status="error",
            message="Source is disabled. Enable it first to refresh knowledge.",
        )

    ssh_key_path = None
    temp_key_context = None

    try:
        # If SSH key is required, decrypt it
        if source.ssh_key_id:
            key_data = SSHKeyService.get_decrypted_private_key(
                session=session,
                key_id=source.ssh_key_id,
                user_id=user_id
            )

            if not key_data:
                source.status = SourceStatus.error
                source.status_message = "SSH key not found or access denied"
                source.updated_at = datetime.now(UTC)
                session.commit()

                return RefreshKnowledgeResponse(
                    status="error",
                    message="SSH key not found or access denied"
                )

            private_key, passphrase = key_data

            # Create temporary SSH key file
            temp_key_context = create_ssh_key_file(private_key, passphrase)
            ssh_key_path = temp_key_context.__enter__()

        # Clone repository and process articles
        with clone_repository_context(
            git_url=source.git_url,
            branch=source.branch,
            ssh_key_path=ssh_key_path
        ) as (repo_path, repo):
            # Get current commit hash
            commit_hash = get_current_commit_hash(repo)

            logger.info(f"Processing articles from commit {commit_hash}")

            # Parse settings.json to get list of current articles
            settings = parse_settings_json(repo_path)
            current_file_paths = [article.path for article in settings.static_articles]

            # Process all articles
            results = process_repository_articles(
                session=session,
                git_repo_id=str(source.id),
                repo_path=repo_path,
                commit_hash=commit_hash
            )

            # Delete orphaned articles (removed from settings.json)
            deleted_count = delete_orphaned_articles(
                session=session,
                git_repo_id=str(source.id),
                current_file_paths=current_file_paths
            )

            # Generate embeddings for all articles
            logger.info(f"Starting embedding generation for source {source_id}")
            try:
                from app.services.embedding_service import DEFAULT_EMBEDDING_MODEL

                embedding_results = chunk_and_embed_all_articles(
                    session=session,
                    git_repo_id=str(source.id),
                    embedding_model=DEFAULT_EMBEDDING_MODEL
                )

                logger.info(
                    f"Embedding generation complete: "
                    f"{embedding_results['articles_processed']} articles processed, "
                    f"{embedding_results['total_chunks_created']} chunks created, "
                    f"{embedding_results['total_chunks_updated']} chunks updated, "
                    f"{embedding_results['articles_failed']} failed"
                )

            except Exception as e:
                # Log error but don't fail the entire refresh
                # Articles are still usable without embeddings
                logger.error(f"Failed to generate embeddings: {str(e)}", exc_info=True)
                embedding_results = {
                    "articles_processed": 0,
                    "articles_failed": 0,
                    "total_chunks_created": 0,
                    "total_chunks_updated": 0,
                    "errors": [{"error": str(e)}]
                }

            # Update source metadata
            source.status = SourceStatus.connected
            source.last_sync_at = datetime.now(UTC)
            source.sync_commit_hash = commit_hash
            source.updated_at = datetime.now(UTC)

            # Build status message
            message_parts = [
                f"Successfully processed {results['total']} articles:",
                f"{results['created']} created",
                f"{results['updated']} updated",
                f"{results['skipped']} unchanged"
            ]

            if deleted_count > 0:
                message_parts.append(f"{deleted_count} deleted")

            # Add embedding information
            if embedding_results['articles_processed'] > 0 or embedding_results.get('articles_skipped', 0) > 0:
                embed_parts = []
                if embedding_results['articles_processed'] > 0:
                    embed_parts.append(f"{embedding_results['articles_processed']} embedded")
                if embedding_results.get('articles_skipped', 0) > 0:
                    embed_parts.append(f"{embedding_results['articles_skipped']} skipped (up-to-date)")
                if embedding_results['total_chunks_created'] > 0:
                    embed_parts.append(f"{embedding_results['total_chunks_created']} chunks created")
                if embedding_results['total_chunks_updated'] > 0:
                    embed_parts.append(f"{embedding_results['total_chunks_updated']} chunks updated")

                message_parts.append(f"Embeddings: {', '.join(embed_parts)}")

            if embedding_results.get('articles_failed', 0) > 0:
                message_parts.append(f"{embedding_results['articles_failed']} articles failed embedding")

            if results['errors']:
                message_parts.append(f"{len(results['errors'])} article parse errors")
                source.status_message = "; ".join(message_parts) + ". Check logs for error details."
            else:
                source.status_message = "; ".join(message_parts)

            session.commit()

            logger.info(f"Knowledge refresh complete for source {source_id}")

            return RefreshKnowledgeResponse(
                status="success",
                message=source.status_message
            )

    except GitAuthenticationError as e:
        logger.error(f"Authentication error refreshing source {source_id}: {e}")

        source.status = SourceStatus.error
        source.status_message = f"Authentication failed: {str(e)}"
        source.updated_at = datetime.now(UTC)
        session.commit()

        return RefreshKnowledgeResponse(
            status="error",
            message=source.status_message
        )

    except GitConnectionError as e:
        logger.error(f"Connection error refreshing source {source_id}: {e}")

        source.status = SourceStatus.error
        source.status_message = f"Connection failed: {str(e)}"
        source.updated_at = datetime.now(UTC)
        session.commit()

        return RefreshKnowledgeResponse(
            status="error",
            message=source.status_message
        )

    except ParseError as e:
        logger.error(f"Parse error refreshing source {source_id}: {e}")

        source.status = SourceStatus.error
        source.status_message = f"Parse error: {str(e)}"
        source.updated_at = datetime.now(UTC)
        session.commit()

        return RefreshKnowledgeResponse(
            status="error",
            message=source.status_message
        )

    except GitOperationError as e:
        logger.error(f"Git operation error refreshing source {source_id}: {e}")

        source.status = SourceStatus.error
        source.status_message = f"Git error: {str(e)}"
        source.updated_at = datetime.now(UTC)
        session.commit()

        return RefreshKnowledgeResponse(
            status="error",
            message=source.status_message
        )

    except Exception as e:
        logger.error(f"Unexpected error refreshing source {source_id}: {e}", exc_info=True)

        source.status = SourceStatus.error
        source.status_message = f"Unexpected error: {str(e)}"
        source.updated_at = datetime.now(UTC)
        session.commit()

        return RefreshKnowledgeResponse(
            status="error",
            message=source.status_message
        )

    finally:
        # Clean up temporary SSH key file
        if temp_key_context:
            try:
                temp_key_context.__exit__(None, None, None)
            except Exception as e:
                logger.warning(f"Failed to clean up SSH key file: {e}")


def get_article_count(
    *,
    session: Session,
    source_id: uuid.UUID,
) -> int:
    """
    Get the count of articles for a knowledge source.

    Args:
        session: Database session
        source_id: ID of the source

    Returns:
        Number of articles
    """
    count = session.exec(
        select(func.count()).select_from(KnowledgeArticle).where(
            KnowledgeArticle.git_repo_id == source_id
        )
    ).one()
    return count


def get_source_articles(
    *,
    session: Session,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
    skip: int = 0,
    limit: int = 100,
) -> Optional[list[KnowledgeArticle]]:
    """
    Get articles for a knowledge source.

    Args:
        session: Database session
        source_id: ID of the source
        user_id: ID of the user (for ownership check)
        skip: Number of records to skip
        limit: Maximum number of records to return

    Returns:
        List of articles or None if source not found or not owned by user
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source or source.user_id != user_id:
        return None

    articles = session.exec(
        select(KnowledgeArticle)
        .where(KnowledgeArticle.git_repo_id == source_id)
        .offset(skip)
        .limit(limit)
    ).all()

    return list(articles)


def get_discoverable_sources(
    *,
    session: Session,
    user_id: uuid.UUID,
    skip: int = 0,
    limit: int = 100,
) -> list:
    """
    Get all public knowledge sources from other admins (read-only view).

    Args:
        session: Database session
        user_id: ID of the current user (to exclude their own sources)
        skip: Number of records to skip
        limit: Maximum number of records to return

    Returns:
        List of public knowledge sources with owner info
    """
    from app.models.knowledge import DiscoverableSourcePublic
    from app.models.user import User

    query = (
        select(AIKnowledgeGitRepo, User)
        .join(User, AIKnowledgeGitRepo.user_id == User.id)
        .where(
            AIKnowledgeGitRepo.public_discovery == True,
            AIKnowledgeGitRepo.user_id != user_id,
            AIKnowledgeGitRepo.status == SourceStatus.connected,
        )
        .order_by(AIKnowledgeGitRepo.name)
        .offset(skip)
        .limit(limit)
    )

    sources_with_users = session.exec(query).all()

    result = []
    for source, owner in sources_with_users:
        article_count = get_article_count(session=session, source_id=source.id)
        result.append(
            DiscoverableSourcePublic(
                id=source.id,
                name=source.name,
                description=source.description,
                status=source.status,
                article_count=article_count,
                owner_username=owner.username,
            )
        )

    return result
