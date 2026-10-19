"""
Service layer for knowledge source management.

This service provides CRUD operations for Git-based knowledge repositories.
Includes Git clone/pull operations, article parsing, and database storage.

Knowledge sources are server-wide and admin-managed: every superuser can manage
every source (route layer enforces superuser). ``user_id`` on a source records
its creator only. Consumer (query) access is resolved in
``knowledge_access_service``.
"""

import uuid
import logging
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, func

from app.models.knowledge.knowledge import (
    AIKnowledgeGitRepo,
    AIKnowledgeGitRepoCreate,
    AIKnowledgeGitRepoPublic,
    AIKnowledgeGitRepoGroupShare,
    AIKnowledgeGitRepoUpdate,
    AIKnowledgeGitRepoUserShare,
    KnowledgeArticle,
    KnowledgeArticleDetail,
    KnowledgeSourceSharedGroupPublic,
    KnowledgeSourceSharedUserPublic,
    SourceStatus,
    CheckAccessResponse,
    RefreshKnowledgeResponse,
)
from app.models.users.ssh_key import UserSSHKey
from app.models.users.user import User
from app.models.users.user_group import UserGroup
from app.services.users.ssh_key_service import SSHKeyService
from app.services.users.user_group_access import UserGroupAccess
from app.services.knowledge.git_operations import (
    create_ssh_key_file,
    verify_repository_access,
    clone_repository_context,
    get_current_commit_hash,
    GitOperationError,
    GitAuthenticationError,
    GitConnectionError,
)
from app.services.knowledge.knowledge_article_service import (
    process_repository_articles,
    delete_orphaned_articles,
    parse_settings_json,
    chunk_and_embed_all_articles,
    ParseError,
)

logger = logging.getLogger(__name__)


class KnowledgeSourceError(Exception):
    """Base error for knowledge source management."""


class KnowledgeSourceNotFoundError(KnowledgeSourceError):
    """Knowledge source (or referenced entity) does not exist."""


class KnowledgeSourceValidationError(KnowledgeSourceError):
    """Request is well-formed but not acceptable (e.g. foreign SSH key)."""


def _require_source(*, session: Session, source_id: uuid.UUID) -> AIKnowledgeGitRepo:
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source:
        raise KnowledgeSourceNotFoundError("Knowledge source not found")
    return source


def _validate_ssh_key_ownership(
    *,
    session: Session,
    ssh_key_id: uuid.UUID,
    acting_user_id: uuid.UUID,
) -> None:
    """An admin may only attach one of their own SSH keys to a source.

    Git operations later resolve the key by its id (see ``_load_source_ssh_key``),
    so any admin can refresh a source regardless of who attached the key.
    """
    if SSHKeyService.get_key_by_id(session, ssh_key_id, acting_user_id) is None:
        raise KnowledgeSourceValidationError(
            "SSH key not found. Select one of your own SSH keys."
        )


def _load_source_ssh_key(
    *,
    session: Session,
    source: AIKnowledgeGitRepo,
) -> Optional[tuple[str, Optional[str]]]:
    """Decrypt the SSH key stored on the source, independent of the acting admin.

    The key is resolved by the source's stored ``ssh_key_id`` and decrypted on
    behalf of the key's own owner, so a refresh or access check by any
    superuser uses the key the source was configured with.
    """
    if not source.ssh_key_id:
        return None
    key = session.get(UserSSHKey, source.ssh_key_id)
    if key is None:
        return None
    return SSHKeyService.get_decrypted_private_key(
        session=session, key_id=key.id, user_id=key.user_id
    )


def _to_public_list(
    *,
    session: Session,
    sources: list[AIKnowledgeGitRepo],
) -> list[AIKnowledgeGitRepoPublic]:
    """Convert sources to the public schema with counts and creator info (batched)."""
    if not sources:
        return []
    source_ids = [source.id for source in sources]
    creator_ids = {source.user_id for source in sources if source.user_id}

    article_counts = dict(
        session.exec(
            select(KnowledgeArticle.git_repo_id, func.count())
            .where(KnowledgeArticle.git_repo_id.in_(source_ids))
            .group_by(KnowledgeArticle.git_repo_id)
        ).all()
    )
    share_counts = dict(
        session.exec(
            select(AIKnowledgeGitRepoUserShare.git_repo_id, func.count())
            .where(AIKnowledgeGitRepoUserShare.git_repo_id.in_(source_ids))
            .group_by(AIKnowledgeGitRepoUserShare.git_repo_id)
        ).all()
    )
    group_share_counts = dict(
        session.exec(
            select(AIKnowledgeGitRepoGroupShare.git_repo_id, func.count())
            .where(AIKnowledgeGitRepoGroupShare.git_repo_id.in_(source_ids))
            .group_by(AIKnowledgeGitRepoGroupShare.git_repo_id)
        ).all()
    )
    creators = {
        user.id: user
        for user in session.exec(select(User).where(User.id.in_(creator_ids))).all()
    }

    result = []
    for source in sources:
        creator = creators.get(source.user_id) if source.user_id else None
        result.append(
            AIKnowledgeGitRepoPublic(
                **source.model_dump(),
                created_by_email=creator.email if creator else None,
                created_by_name=creator.full_name if creator else None,
                article_count=article_counts.get(source.id, 0),
                shared_user_count=share_counts.get(source.id, 0),
                shared_group_count=group_share_counts.get(source.id, 0),
            )
        )
    return result


def _to_public(*, session: Session, source: AIKnowledgeGitRepo) -> AIKnowledgeGitRepoPublic:
    return _to_public_list(session=session, sources=[source])[0]


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
        user_id: ID of the admin creating the source (recorded as creator)
        data: Source creation data

    Raises:
        KnowledgeSourceValidationError: if the SSH key is not the creator's own
    """
    if data.ssh_key_id:
        _validate_ssh_key_ownership(
            session=session, ssh_key_id=data.ssh_key_id, acting_user_id=user_id
        )

    source = AIKnowledgeGitRepo(
        user_id=user_id,
        name=data.name,
        description=data.description,
        git_url=data.git_url,
        branch=data.branch,
        ssh_key_id=data.ssh_key_id,
        access_level=data.access_level,
        status=SourceStatus.pending,
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    return _to_public(session=session, source=source)


def list_sources(
    *,
    session: Session,
    skip: int = 0,
    limit: int = 100,
) -> list[AIKnowledgeGitRepoPublic]:
    """List all knowledge sources on the server (admin view)."""
    sources = session.exec(
        select(AIKnowledgeGitRepo)
        .order_by(AIKnowledgeGitRepo.name)
        .offset(skip)
        .limit(limit)
    ).all()
    return _to_public_list(session=session, sources=list(sources))


def get_source_by_id(
    *,
    session: Session,
    source_id: uuid.UUID,
) -> Optional[AIKnowledgeGitRepoPublic]:
    """Get a knowledge source by ID, or None if it does not exist."""
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source:
        return None
    return _to_public(session=session, source=source)


def update_source(
    *,
    session: Session,
    source_id: uuid.UUID,
    acting_user_id: uuid.UUID,
    data: AIKnowledgeGitRepoUpdate,
) -> Optional[AIKnowledgeGitRepoPublic]:
    """
    Update a knowledge source.

    Args:
        session: Database session
        source_id: ID of the source
        acting_user_id: Admin performing the update (for SSH key ownership)
        data: Update data

    Returns:
        Updated knowledge source or None if not found

    Raises:
        KnowledgeSourceValidationError: if a newly attached SSH key is not the
            acting admin's own
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source:
        return None

    if data.ssh_key_id is not None and data.ssh_key_id != source.ssh_key_id:
        _validate_ssh_key_ownership(
            session=session, ssh_key_id=data.ssh_key_id, acting_user_id=acting_user_id
        )

    # Check if Git config changed (requires re-verification)
    git_config_changed = data.branch is not None or data.ssh_key_id is not None

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(source, field, value)

    if git_config_changed:
        source.status = SourceStatus.pending
        source.status_message = "Configuration updated. Please check access."

    source.updated_at = datetime.now(UTC)
    session.commit()
    session.refresh(source)

    return _to_public(session=session, source=source)


def delete_source(
    *,
    session: Session,
    source_id: uuid.UUID,
) -> bool:
    """
    Delete a knowledge source.

    Args:
        session: Database session
        source_id: ID of the source

    Returns:
        True if deleted, False if not found
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source:
        return False

    session.delete(source)
    session.commit()
    return True


def enable_source(
    *,
    session: Session,
    source_id: uuid.UUID,
) -> Optional[AIKnowledgeGitRepoPublic]:
    """
    Enable a knowledge source.

    Args:
        session: Database session
        source_id: ID of the source

    Returns:
        Updated knowledge source or None if not found
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source:
        return None

    source.is_enabled = True
    source.updated_at = datetime.now(UTC)
    session.commit()
    session.refresh(source)

    return _to_public(session=session, source=source)


def disable_source(
    *,
    session: Session,
    source_id: uuid.UUID,
) -> Optional[AIKnowledgeGitRepoPublic]:
    """
    Disable a knowledge source.

    Args:
        session: Database session
        source_id: ID of the source

    Returns:
        Updated knowledge source or None if not found
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source:
        return None

    source.is_enabled = False
    source.updated_at = datetime.now(UTC)
    session.commit()
    session.refresh(source)

    return _to_public(session=session, source=source)


def check_access(
    *,
    session: Session,
    source_id: uuid.UUID,
) -> CheckAccessResponse:
    """
    Check if the Git repository is accessible.

    Verifies repository access using Git ls-remote without cloning.
    Updates source status based on the result.

    Args:
        session: Database session
        source_id: ID of the source

    Returns:
        Access check response with accessibility status and message

    Raises:
        KnowledgeSourceNotFoundError: if the source does not exist
    """
    source = _require_source(session=session, source_id=source_id)

    ssh_key_path = None
    temp_key_context = None

    try:
        # If SSH key is required, decrypt it
        if source.ssh_key_id:
            key_data = _load_source_ssh_key(session=session, source=source)

            if not key_data:
                source.status = SourceStatus.error
                source.status_message = "SSH key attached to this source no longer exists"
                source.last_checked_at = datetime.now(UTC)
                source.updated_at = datetime.now(UTC)
                session.commit()

                return CheckAccessResponse(
                    accessible=False,
                    message="SSH key attached to this source no longer exists"
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
) -> RefreshKnowledgeResponse:
    """
    Trigger knowledge refresh from Git repository.

    Clones the repository, parses .ai-knowledge/settings.json,
    and stores articles in the database. Embeddings will be generated in a later phase.

    Args:
        session: Database session
        source_id: ID of the source

    Returns:
        Refresh response with status and details

    Raises:
        KnowledgeSourceNotFoundError: if the source does not exist
    """
    source = _require_source(session=session, source_id=source_id)

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
            key_data = _load_source_ssh_key(session=session, source=source)

            if not key_data:
                source.status = SourceStatus.error
                source.status_message = "SSH key attached to this source no longer exists"
                source.updated_at = datetime.now(UTC)
                session.commit()

                return RefreshKnowledgeResponse(
                    status="error",
                    message="SSH key attached to this source no longer exists"
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
                from app.services.knowledge.embedding_service import DEFAULT_EMBEDDING_MODEL

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
    skip: int = 0,
    limit: int = 100,
) -> Optional[list[KnowledgeArticle]]:
    """
    Get articles for a knowledge source.

    Args:
        session: Database session
        source_id: ID of the source
        skip: Number of records to skip
        limit: Maximum number of records to return

    Returns:
        List of articles or None if source not found
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source:
        return None

    articles = session.exec(
        select(KnowledgeArticle)
        .where(KnowledgeArticle.git_repo_id == source_id)
        .offset(skip)
        .limit(limit)
    ).all()

    return list(articles)


def get_article_content(
    *,
    session: Session,
    source_id: uuid.UUID,
    article_id: uuid.UUID,
) -> Optional[KnowledgeArticleDetail]:
    """Return one article's full content (incl. Markdown body).

    Returns None if the source does not exist OR the article does not belong to it.
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source:
        return None
    article = session.get(KnowledgeArticle, article_id)
    if not article or article.git_repo_id != source_id:
        return None
    return KnowledgeArticleDetail(
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
        content=article.content,
        commit_hash=article.commit_hash,
    )


def export_source_markdown(
    *,
    session: Session,
    source_id: uuid.UUID,
) -> Optional[str]:
    """Return all of a source's articles concatenated into one Markdown document.

    Returns None if the source does not exist. An empty source returns a valid
    (header-only) document.
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source:
        return None
    articles = session.exec(
        select(KnowledgeArticle)
        .where(KnowledgeArticle.git_repo_id == source_id)
        .order_by(KnowledgeArticle.file_path)
    ).all()
    parts = [f"# {source.name}\n"]
    if source.description:
        parts.append(f"{source.description}\n")
    for a in articles:
        parts.append(f"\n---\n\n## {a.title}\n")
        parts.append(f"*Source file: `{a.file_path}`*\n")
        if a.description:
            parts.append(f"\n> {a.description}\n")
        parts.append(f"\n{a.content}\n")
    return "\n".join(parts)


# ── User shares (access_level == shared) ─────────────────────────────────────


def list_shared_users(
    *,
    session: Session,
    source_id: uuid.UUID,
) -> list[KnowledgeSourceSharedUserPublic]:
    """List users on the source's share list (ordered by email).

    Raises:
        KnowledgeSourceNotFoundError: if the source does not exist
    """
    _require_source(session=session, source_id=source_id)
    rows = session.exec(
        select(AIKnowledgeGitRepoUserShare, User)
        .join(User, AIKnowledgeGitRepoUserShare.user_id == User.id)
        .where(AIKnowledgeGitRepoUserShare.git_repo_id == source_id)
        .order_by(User.email)
    ).all()
    return [
        KnowledgeSourceSharedUserPublic(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            created_at=share.created_at,
        )
        for share, user in rows
    ]


def add_shared_user(
    *,
    session: Session,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
) -> KnowledgeSourceSharedUserPublic:
    """Add a user to the source's share list. Idempotent: an existing share is returned.

    Raises:
        KnowledgeSourceNotFoundError: if the source or the user does not exist
    """
    _require_source(session=session, source_id=source_id)
    user = session.get(User, user_id)
    if not user:
        raise KnowledgeSourceNotFoundError("User not found")

    share = session.exec(
        select(AIKnowledgeGitRepoUserShare).where(
            AIKnowledgeGitRepoUserShare.git_repo_id == source_id,
            AIKnowledgeGitRepoUserShare.user_id == user_id,
        )
    ).first()
    if share is None:
        share = AIKnowledgeGitRepoUserShare(git_repo_id=source_id, user_id=user_id)
        session.add(share)
        session.commit()
        session.refresh(share)

    return KnowledgeSourceSharedUserPublic(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        created_at=share.created_at,
    )


def remove_shared_user(
    *,
    session: Session,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Remove a user from the source's share list.

    Raises:
        KnowledgeSourceNotFoundError: if the source does not exist or the user
            is not on its share list
    """
    _require_source(session=session, source_id=source_id)
    share = session.exec(
        select(AIKnowledgeGitRepoUserShare).where(
            AIKnowledgeGitRepoUserShare.git_repo_id == source_id,
            AIKnowledgeGitRepoUserShare.user_id == user_id,
        )
    ).first()
    if share is None:
        raise KnowledgeSourceNotFoundError("User is not on this source's share list")
    session.delete(share)
    session.commit()


# ── Group shares (access_level == shared) ────────────────────────────────────
# Members are resolved live (knowledge_access_service); nothing is released
# when a group share goes away: the next query simply excludes the source.


def _group_share_public(
    *,
    share: AIKnowledgeGitRepoGroupShare,
    group: UserGroup,
    member_count: int,
) -> KnowledgeSourceSharedGroupPublic:
    return KnowledgeSourceSharedGroupPublic(
        group_id=group.id,
        name=group.name,
        member_count=member_count,
        created_at=share.created_at,
    )


def list_shared_groups(
    *,
    session: Session,
    source_id: uuid.UUID,
) -> list[KnowledgeSourceSharedGroupPublic]:
    """List user groups on the source's share list (ordered by name).

    Raises:
        KnowledgeSourceNotFoundError: if the source does not exist
    """
    _require_source(session=session, source_id=source_id)
    rows = session.exec(
        select(AIKnowledgeGitRepoGroupShare, UserGroup)
        .join(UserGroup, AIKnowledgeGitRepoGroupShare.group_id == UserGroup.id)
        .where(AIKnowledgeGitRepoGroupShare.git_repo_id == source_id)
        .order_by(func.lower(UserGroup.name), UserGroup.id)
    ).all()
    member_counts = UserGroupAccess.member_counts(
        session, [group.id for _, group in rows]
    )
    return [
        _group_share_public(
            share=share, group=group, member_count=member_counts.get(group.id, 0)
        )
        for share, group in rows
    ]


def add_shared_group(
    *,
    session: Session,
    source_id: uuid.UUID,
    group_id: uuid.UUID,
    granted_by_user_id: uuid.UUID,
) -> KnowledgeSourceSharedGroupPublic:
    """Add a user group to the source's share list. Idempotent: an existing share is returned.

    Raises:
        KnowledgeSourceNotFoundError: if the source or the group does not exist
    """
    _require_source(session=session, source_id=source_id)
    group = session.get(UserGroup, group_id)
    if not group:
        raise KnowledgeSourceNotFoundError("User group not found")

    def find_share() -> AIKnowledgeGitRepoGroupShare | None:
        return session.exec(
            select(AIKnowledgeGitRepoGroupShare).where(
                AIKnowledgeGitRepoGroupShare.git_repo_id == source_id,
                AIKnowledgeGitRepoGroupShare.group_id == group_id,
            )
        ).first()

    share = find_share()
    if share is None:
        share = AIKnowledgeGitRepoGroupShare(
            git_repo_id=source_id,
            group_id=group_id,
            granted_by_user_id=granted_by_user_id,
        )
        session.add(share)
        try:
            session.commit()
            session.refresh(share)
        except IntegrityError:
            # A concurrent add won the unique (source, group) key: return its row.
            # (A source or group deleted meanwhile also lands here.)
            session.rollback()
            share = find_share()
            if share is None:
                raise KnowledgeSourceNotFoundError("Knowledge source or user group not found")

    member_count = UserGroupAccess.member_counts(session, [group.id]).get(group.id, 0)
    return _group_share_public(share=share, group=group, member_count=member_count)


def remove_shared_group(
    *,
    session: Session,
    source_id: uuid.UUID,
    group_id: uuid.UUID,
) -> None:
    """Remove a user group from the source's share list.

    Raises:
        KnowledgeSourceNotFoundError: if the source does not exist or the group
            is not on its share list
    """
    _require_source(session=session, source_id=source_id)
    share = session.exec(
        select(AIKnowledgeGitRepoGroupShare).where(
            AIKnowledgeGitRepoGroupShare.git_repo_id == source_id,
            AIKnowledgeGitRepoGroupShare.group_id == group_id,
        )
    ).first()
    if share is None:
        raise KnowledgeSourceNotFoundError("Group is not on this source's share list")
    session.delete(share)
    session.commit()
