"""
Vector search service for knowledge articles using cosine similarity.

This module provides utilities for:
- Searching article chunks by semantic similarity
- Filtering by workspace permissions
- Ranking results by relevance
"""

import logging
import uuid
from typing import List, Optional, Dict, Any
from sqlmodel import Session, select, and_, or_
from sqlalchemy import func

from app.models import (
    KnowledgeArticle,
    KnowledgeArticleChunk,
    AIKnowledgeGitRepo,
    AIKnowledgeGitRepoWorkspace,
    Agent,
    SourceStatus,
    WorkspaceAccessType,
    ArticleListItem,
    ArticleContent,
)

logger = logging.getLogger(__name__)


class VectorSearchError(Exception):
    """Exception raised when vector search fails."""
    pass


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    Calculate cosine similarity between two vectors.

    Args:
        vec1: First vector
        vec2: Second vector

    Returns:
        Cosine similarity score (0 to 1)
    """
    import math

    if len(vec1) != len(vec2):
        raise ValueError("Vectors must have the same dimensions")

    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))

    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0

    return dot_product / (magnitude1 * magnitude2)


def get_accessible_source_ids(
    *,
    session: Session,
    user_id: uuid.UUID,
    workspace_id: Optional[uuid.UUID] = None
) -> List[uuid.UUID]:
    """
    Get list of knowledge source IDs accessible to the user and workspace.
    Includes both user-owned sources and enabled discoverable sources.

    Args:
        session: Database session
        user_id: User ID
        workspace_id: Optional workspace ID for filtering

    Returns:
        List of accessible source IDs
    """
    from app.models.knowledge import UserEnabledDiscoverableSource

    # Build query for user's own accessible sources
    query = select(AIKnowledgeGitRepo).where(
        and_(
            AIKnowledgeGitRepo.user_id == user_id,
            AIKnowledgeGitRepo.is_enabled == True,
            AIKnowledgeGitRepo.status == SourceStatus.connected
        )
    )

    # Apply workspace filtering if specified
    if workspace_id:
        # Sources with 'all' access OR sources with specific workspace permission
        query = query.where(
            or_(
                AIKnowledgeGitRepo.workspace_access_type == WorkspaceAccessType.all,
                and_(
                    AIKnowledgeGitRepo.workspace_access_type == WorkspaceAccessType.specific,
                    AIKnowledgeGitRepo.id.in_(
                        select(AIKnowledgeGitRepoWorkspace.git_repo_id).where(
                            AIKnowledgeGitRepoWorkspace.user_workspace_id == workspace_id
                        )
                    )
                )
            )
        )

    sources = session.exec(query).all()
    source_ids = [source.id for source in sources]

    # Get enabled discoverable sources
    enabled_discoverable_query = (
        select(AIKnowledgeGitRepo)
        .join(
            UserEnabledDiscoverableSource,
            AIKnowledgeGitRepo.id == UserEnabledDiscoverableSource.git_repo_id
        )
        .where(
            and_(
                UserEnabledDiscoverableSource.user_id == user_id,
                AIKnowledgeGitRepo.public_discovery == True,
                AIKnowledgeGitRepo.status == SourceStatus.connected,
            )
        )
    )

    enabled_discoverable_sources = session.exec(enabled_discoverable_query).all()
    discoverable_ids = [source.id for source in enabled_discoverable_sources]

    # Combine both lists (deduplicate just in case)
    all_source_ids = list(set(source_ids + discoverable_ids))

    logger.info(
        f"Found {len(source_ids)} user sources and {len(discoverable_ids)} "
        f"enabled discoverable sources for user {user_id}"
    )
    return all_source_ids


def search_article_chunks(
    *,
    session: Session,
    query_embedding: List[float],
    source_ids: List[uuid.UUID],
    embedding_model: str,
    limit: int = 10
) -> List[Dict[str, Any]]:
    """
    Search article chunks by semantic similarity.

    Args:
        session: Database session
        query_embedding: Query embedding vector
        source_ids: List of accessible source IDs
        embedding_model: Embedding model name to filter by
        limit: Maximum number of results

    Returns:
        List of chunk results with similarity scores
    """
    if not source_ids:
        logger.warning("No accessible sources, returning empty results")
        return []

    # Get chunks from accessible sources with embeddings
    chunks_query = (
        select(KnowledgeArticleChunk, KnowledgeArticle, AIKnowledgeGitRepo)
        .join(KnowledgeArticle, KnowledgeArticleChunk.article_id == KnowledgeArticle.id)
        .join(AIKnowledgeGitRepo, KnowledgeArticle.git_repo_id == AIKnowledgeGitRepo.id)
        .where(
            and_(
                KnowledgeArticle.git_repo_id.in_(source_ids),
                KnowledgeArticleChunk.embedding_model == embedding_model,
                KnowledgeArticleChunk.embedding.isnot(None)
            )
        )
    )

    chunks = session.exec(chunks_query).all()

    if not chunks:
        logger.warning(f"No chunks found with embedding model {embedding_model}")
        return []

    # Calculate similarity scores
    results = []
    for chunk, article, source in chunks:
        if chunk.embedding:
            similarity = cosine_similarity(query_embedding, chunk.embedding)

            results.append({
                "chunk_id": chunk.id,
                "article_id": article.id,
                "article_title": article.title,
                "article_description": article.description,
                "chunk_text": chunk.chunk_text,
                "chunk_index": chunk.chunk_index,
                "similarity": similarity,
                "source_name": source.name,
                "source_id": source.id,
                "tags": article.tags,
                "features": article.features,
            })

    # Sort by similarity (highest first)
    results.sort(key=lambda x: x["similarity"], reverse=True)

    # Limit results
    return results[:limit]


def get_top_articles_from_chunks(
    chunk_results: List[Dict[str, Any]],
    limit: int = 10
) -> List[ArticleListItem]:
    """
    Group chunk results by article and return top articles.

    Args:
        chunk_results: List of chunk search results with similarity scores
        limit: Maximum number of articles to return

    Returns:
        List of article metadata items
    """
    # Group chunks by article
    article_scores: Dict[uuid.UUID, Dict[str, Any]] = {}

    for chunk in chunk_results:
        article_id = chunk["article_id"]

        if article_id not in article_scores:
            article_scores[article_id] = {
                "id": article_id,
                "title": chunk["article_title"],
                "description": chunk["article_description"],
                "source_name": chunk["source_name"],
                "git_repo_id": chunk["source_id"],
                "tags": chunk["tags"],
                "features": chunk["features"],
                "max_similarity": chunk["similarity"],
                "total_similarity": chunk["similarity"],
                "chunk_count": 1,
            }
        else:
            # Update with higher similarity or accumulate
            article_scores[article_id]["max_similarity"] = max(
                article_scores[article_id]["max_similarity"],
                chunk["similarity"]
            )
            article_scores[article_id]["total_similarity"] += chunk["similarity"]
            article_scores[article_id]["chunk_count"] += 1

    # Sort by max similarity score
    sorted_articles = sorted(
        article_scores.values(),
        key=lambda x: x["max_similarity"],
        reverse=True
    )

    # Convert to ArticleListItem
    results = []
    for article_data in sorted_articles[:limit]:
        results.append(ArticleListItem(
            id=article_data["id"],
            title=article_data["title"],
            description=article_data["description"],
            tags=article_data["tags"],
            features=article_data["features"],
            source_name=article_data["source_name"],
            git_repo_id=article_data["git_repo_id"],
        ))

    return results


def get_articles_by_ids(
    *,
    session: Session,
    article_ids: List[uuid.UUID],
    source_ids: List[uuid.UUID]
) -> List[ArticleContent]:
    """
    Retrieve full article content by IDs.

    Args:
        session: Database session
        article_ids: List of article IDs to retrieve
        source_ids: List of accessible source IDs (for permission check)

    Returns:
        List of full article content

    Raises:
        VectorSearchError: If any article is not accessible
    """
    # Query articles with source information
    query = (
        select(KnowledgeArticle, AIKnowledgeGitRepo)
        .join(AIKnowledgeGitRepo, KnowledgeArticle.git_repo_id == AIKnowledgeGitRepo.id)
        .where(
            and_(
                KnowledgeArticle.id.in_(article_ids),
                KnowledgeArticle.git_repo_id.in_(source_ids)
            )
        )
    )

    results = session.exec(query).all()

    # Check if all requested articles were found
    found_ids = {article.id for article, _ in results}
    missing_ids = set(article_ids) - found_ids

    if missing_ids:
        logger.warning(f"Requested articles not accessible: {missing_ids}")
        raise VectorSearchError(
            f"Some articles are not accessible or do not exist: {missing_ids}"
        )

    # Convert to ArticleContent
    articles = []
    for article, source in results:
        articles.append(ArticleContent(
            id=article.id,
            title=article.title,
            description=article.description,
            content=article.content,
            file_path=article.file_path,
            tags=article.tags,
            features=article.features,
            source_name=source.name,
        ))

    return articles


def search_knowledge(
    *,
    session: Session,
    query_embedding: List[float],
    user_id: uuid.UUID,
    workspace_id: Optional[uuid.UUID] = None,
    embedding_model: str,
    limit: int = 10
) -> List[ArticleListItem]:
    """
    High-level search function for knowledge discovery.

    Args:
        session: Database session
        query_embedding: Query embedding vector
        user_id: User ID for permission checking
        workspace_id: Optional workspace ID for filtering
        embedding_model: Embedding model name
        limit: Maximum number of articles to return

    Returns:
        List of article metadata items
    """
    # Get accessible sources
    source_ids = get_accessible_source_ids(
        session=session,
        user_id=user_id,
        workspace_id=workspace_id
    )

    if not source_ids:
        logger.info("No accessible knowledge sources")
        return []

    # Search chunks
    chunk_results = search_article_chunks(
        session=session,
        query_embedding=query_embedding,
        source_ids=source_ids,
        embedding_model=embedding_model,
        limit=limit * 3  # Get more chunks to ensure we have enough articles
    )

    if not chunk_results:
        logger.info("No matching chunks found")
        return []

    # Group by article and get top results
    articles = get_top_articles_from_chunks(chunk_results, limit=limit)

    logger.info(f"Found {len(articles)} articles matching query")
    return articles
