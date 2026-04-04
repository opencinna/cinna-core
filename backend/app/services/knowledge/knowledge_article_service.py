"""
Knowledge article service for parsing and storing articles.

This module provides utilities for:
- Parsing .ai-knowledge/settings.json
- Validating article schemas
- Calculating content hashes
- Upserting articles to database
"""

import os
import json
import hashlib
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import UTC, datetime

from sqlmodel import Session, select
from pydantic import BaseModel, Field, ValidationError

from app.models.knowledge.knowledge import KnowledgeArticle

logger = logging.getLogger(__name__)


class ArticleConfig(BaseModel):
    """Schema for an article configuration in settings.json."""

    title: str = Field(..., min_length=1, description="Article title")
    description: str = Field(..., min_length=1, description="Article description")
    tags: List[str] = Field(default_factory=list, description="Article tags")
    features: List[str] = Field(default_factory=list, description="Feature categories")
    path: str = Field(..., min_length=1, description="Relative path to article file")


class KnowledgeSettings(BaseModel):
    """Schema for .ai-knowledge/settings.json."""

    static_articles: List[ArticleConfig] = Field(
        default_factory=list,
        description="List of static article configurations"
    )


class ParseError(Exception):
    """Exception raised when parsing fails."""
    pass


def parse_settings_json(repo_path: str) -> KnowledgeSettings:
    """
    Parse .ai-knowledge/settings.json from a Git repository.

    Args:
        repo_path: Path to the cloned repository

    Returns:
        KnowledgeSettings object

    Raises:
        ParseError: If settings.json is not found, invalid, or malformed
    """
    settings_path = Path(repo_path) / ".ai-knowledge" / "settings.json"

    if not settings_path.exists():
        raise ParseError(
            "Knowledge configuration not found. "
            "Repository must contain .ai-knowledge/settings.json"
        )

    try:
        with open(settings_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Validate against schema
        settings = KnowledgeSettings(**data)

        logger.info(f"Successfully parsed settings.json with {len(settings.static_articles)} articles")
        return settings

    except json.JSONDecodeError as e:
        raise ParseError(f"Invalid JSON format: {str(e)}") from e

    except ValidationError as e:
        raise ParseError(f"Invalid settings schema: {str(e)}") from e

    except Exception as e:
        raise ParseError(f"Failed to parse settings.json: {str(e)}") from e


def calculate_content_hash(content: str) -> str:
    """
    Calculate SHA256 hash of content.

    Args:
        content: Content string

    Returns:
        SHA256 hash in hexadecimal
    """
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def read_article_file(repo_path: str, file_path: str) -> str:
    """
    Read article content from file.

    Args:
        repo_path: Path to the cloned repository
        file_path: Relative path to the article file

    Returns:
        Article content

    Raises:
        ParseError: If file is not found or cannot be read
    """
    full_path = Path(repo_path) / file_path

    if not full_path.exists():
        raise ParseError(f"Article file not found: {file_path}")

    if not full_path.is_file():
        raise ParseError(f"Article path is not a file: {file_path}")

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()

        return content

    except UnicodeDecodeError as e:
        raise ParseError(f"Failed to decode article file {file_path}: {str(e)}") from e

    except Exception as e:
        raise ParseError(f"Failed to read article file {file_path}: {str(e)}") from e


def upsert_article(
    *,
    session: Session,
    git_repo_id: str,
    article_config: ArticleConfig,
    content: str,
    commit_hash: str
) -> tuple[KnowledgeArticle, bool]:
    """
    Insert or update an article in the database.

    Args:
        session: Database session
        git_repo_id: ID of the Git repository
        article_config: Article configuration from settings.json
        content: Article content
        commit_hash: Git commit hash

    Returns:
        Tuple of (article, created) where created is True if new article was created
    """
    # Calculate content hash
    content_hash = calculate_content_hash(content)

    # Check if article already exists
    existing = session.exec(
        select(KnowledgeArticle).where(
            KnowledgeArticle.git_repo_id == git_repo_id,
            KnowledgeArticle.file_path == article_config.path
        )
    ).first()

    if existing:
        # Check if content has changed
        if existing.content_hash == content_hash and existing.commit_hash == commit_hash:
            logger.debug(f"Article {article_config.path} unchanged, skipping")
            return existing, False

        # Update existing article
        existing.title = article_config.title
        existing.description = article_config.description
        existing.tags = article_config.tags
        existing.features = article_config.features
        existing.content = content
        existing.content_hash = content_hash
        existing.commit_hash = commit_hash
        existing.updated_at = datetime.now(UTC)

        # Note: Embeddings will be regenerated in a later phase
        # For now, we're just storing the content

        session.add(existing)
        session.commit()
        session.refresh(existing)

        logger.info(f"Updated article: {article_config.path}")
        return existing, False

    else:
        # Create new article
        article = KnowledgeArticle(
            git_repo_id=git_repo_id,
            title=article_config.title,
            description=article_config.description,
            tags=article_config.tags,
            features=article_config.features,
            file_path=article_config.path,
            content=content,
            content_hash=content_hash,
            commit_hash=commit_hash,
            embedding_model=None,  # Will be set when embeddings are generated
            embedding_dimensions=None,
            embedding=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC)
        )

        session.add(article)
        session.commit()
        session.refresh(article)

        logger.info(f"Created new article: {article_config.path}")
        return article, True


def process_repository_articles(
    *,
    session: Session,
    git_repo_id: str,
    repo_path: str,
    commit_hash: str
) -> Dict[str, Any]:
    """
    Process all articles from a repository.

    Args:
        session: Database session
        git_repo_id: ID of the Git repository
        repo_path: Path to the cloned repository
        commit_hash: Git commit hash

    Returns:
        Dictionary with processing results:
        {
            "total": int,
            "created": int,
            "updated": int,
            "skipped": int,
            "errors": List[Dict[str, str]]
        }

    Raises:
        ParseError: If settings.json cannot be parsed
    """
    # Parse settings.json
    settings = parse_settings_json(repo_path)

    results = {
        "total": len(settings.static_articles),
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": []
    }

    # Process each article
    for article_config in settings.static_articles:
        try:
            # Read article content
            content = read_article_file(repo_path, article_config.path)

            # Upsert article
            article, created = upsert_article(
                session=session,
                git_repo_id=git_repo_id,
                article_config=article_config,
                content=content,
                commit_hash=commit_hash
            )

            if created:
                results["created"] += 1
            elif article.updated_at > article.created_at:
                results["updated"] += 1
            else:
                results["skipped"] += 1

        except ParseError as e:
            error_msg = f"Error processing {article_config.path}: {str(e)}"
            logger.warning(error_msg)
            results["errors"].append({
                "file": article_config.path,
                "error": str(e)
            })

        except Exception as e:
            error_msg = f"Unexpected error processing {article_config.path}: {str(e)}"
            logger.error(error_msg)
            results["errors"].append({
                "file": article_config.path,
                "error": str(e)
            })

    logger.info(
        f"Processing complete: {results['created']} created, "
        f"{results['updated']} updated, {results['skipped']} skipped, "
        f"{len(results['errors'])} errors"
    )

    return results


def delete_orphaned_articles(
    *,
    session: Session,
    git_repo_id: str,
    current_file_paths: List[str]
) -> int:
    """
    Delete articles that no longer exist in settings.json.

    Args:
        session: Database session
        git_repo_id: ID of the Git repository
        current_file_paths: List of file paths from current settings.json

    Returns:
        Number of articles deleted
    """
    # Get all existing articles for this repo
    existing_articles = session.exec(
        select(KnowledgeArticle).where(
            KnowledgeArticle.git_repo_id == git_repo_id
        )
    ).all()

    deleted_count = 0

    for article in existing_articles:
        if article.file_path not in current_file_paths:
            session.delete(article)
            deleted_count += 1
            logger.info(f"Deleted orphaned article: {article.file_path}")

    if deleted_count > 0:
        session.commit()
        logger.info(f"Deleted {deleted_count} orphaned articles")

    return deleted_count


def chunk_and_embed_article(
    *,
    session: Session,
    article_id: str,
    embedding_model: str = "text-embedding-004"
) -> Dict[str, Any]:
    """
    Chunk an article and generate embeddings for each chunk.

    Args:
        session: Database session
        article_id: Article ID to process
        embedding_model: Embedding model to use

    Returns:
        Dictionary with processing results:
        {
            "chunks_created": int,
            "chunks_updated": int,
            "total_chunks": int
        }

    Raises:
        ValueError: If article not found
    """
    from app.models.knowledge.knowledge import KnowledgeArticleChunk
    from app.services.knowledge.embedding_service import (
        chunk_text,
        prepare_article_for_embedding,
        generate_embedding,
        EmbeddingError
    )

    # Get article
    article = session.get(KnowledgeArticle, article_id)
    if not article:
        raise ValueError(f"Article not found: {article_id}")

    # Prepare text for embedding
    full_text = prepare_article_for_embedding(
        title=article.title,
        description=article.description,
        content=article.content
    )

    # Split into chunks
    text_chunks = chunk_text(full_text)

    logger.info(f"Article {article.title} split into {len(text_chunks)} chunks")

    results = {
        "chunks_created": 0,
        "chunks_updated": 0,
        "total_chunks": len(text_chunks)
    }

    # Process each chunk
    for idx, chunk_text in enumerate(text_chunks):
        try:
            # Generate embedding for chunk
            embedding, dimensions = generate_embedding(
                text=chunk_text,
                model=embedding_model
            )

            # Check if chunk already exists
            existing_chunk = session.exec(
                select(KnowledgeArticleChunk).where(
                    KnowledgeArticleChunk.article_id == article_id,
                    KnowledgeArticleChunk.chunk_index == idx
                )
            ).first()

            if existing_chunk:
                # Update existing chunk
                existing_chunk.chunk_text = chunk_text
                existing_chunk.embedding = embedding
                existing_chunk.embedding_model = embedding_model
                existing_chunk.embedding_dimensions = dimensions

                session.add(existing_chunk)
                results["chunks_updated"] += 1

                logger.debug(f"Updated chunk {idx} for article {article.title}")

            else:
                # Create new chunk
                new_chunk = KnowledgeArticleChunk(
                    article_id=article_id,
                    chunk_index=idx,
                    chunk_text=chunk_text,
                    embedding=embedding,
                    embedding_model=embedding_model,
                    embedding_dimensions=dimensions
                )

                session.add(new_chunk)
                results["chunks_created"] += 1

                logger.debug(f"Created chunk {idx} for article {article.title}")

        except EmbeddingError as e:
            logger.error(f"Failed to generate embedding for chunk {idx}: {str(e)}")
            # Continue with other chunks even if one fails

    # Delete orphaned chunks (if article was re-chunked with fewer chunks)
    orphaned_chunks = session.exec(
        select(KnowledgeArticleChunk).where(
            KnowledgeArticleChunk.article_id == article_id,
            KnowledgeArticleChunk.chunk_index >= len(text_chunks)
        )
    ).all()

    for chunk in orphaned_chunks:
        session.delete(chunk)
        logger.debug(f"Deleted orphaned chunk {chunk.chunk_index}")

    # Commit all changes
    session.commit()

    logger.info(
        f"Chunking complete for article {article.title}: "
        f"{results['chunks_created']} created, {results['chunks_updated']} updated"
    )

    return results


def chunk_and_embed_all_articles(
    *,
    session: Session,
    git_repo_id: str,
    embedding_model: str = "text-embedding-004"
) -> Dict[str, Any]:
    """
    Chunk and embed all articles for a repository.

    Only processes articles that:
    1. Have no chunks (new articles)
    2. Have been updated since chunks were created (content changed)
    3. Have chunks but no embeddings (embedding failed previously)

    Args:
        session: Database session
        git_repo_id: Repository ID
        embedding_model: Embedding model to use

    Returns:
        Dictionary with processing results
    """
    from app.models.knowledge.knowledge import KnowledgeArticleChunk

    # Get all articles for repository
    articles = session.exec(
        select(KnowledgeArticle).where(
            KnowledgeArticle.git_repo_id == git_repo_id
        )
    ).all()

    total_results = {
        "articles_processed": 0,
        "articles_skipped": 0,
        "articles_failed": 0,
        "total_chunks_created": 0,
        "total_chunks_updated": 0,
        "errors": []
    }

    for article in articles:
        try:
            # Check if article needs embedding by examining existing chunks
            existing_chunks = session.exec(
                select(KnowledgeArticleChunk).where(
                    KnowledgeArticleChunk.article_id == article.id
                )
            ).all()

            needs_embedding = False

            if not existing_chunks:
                # No chunks exist - need to embed
                needs_embedding = True
                logger.debug(f"Article {article.title} has no chunks - will embed")
            else:
                # Chunks exist - check if content changed or embeddings missing
                # Get the newest chunk creation time
                newest_chunk_time = max(chunk.created_at for chunk in existing_chunks)

                # If article was updated after chunks were created, content changed
                if article.updated_at > newest_chunk_time:
                    needs_embedding = True
                    logger.debug(
                        f"Article {article.title} updated after chunks "
                        f"(article: {article.updated_at}, chunks: {newest_chunk_time}) - will re-embed"
                    )
                # Check if any chunks are missing embeddings
                elif any(chunk.embedding is None for chunk in existing_chunks):
                    needs_embedding = True
                    logger.debug(f"Article {article.title} has chunks without embeddings - will re-embed")
                else:
                    # Chunks exist and are up to date
                    logger.debug(f"Article {article.title} has up-to-date embeddings - skipping")
                    total_results["articles_skipped"] += 1

            if needs_embedding:
                results = chunk_and_embed_article(
                    session=session,
                    article_id=str(article.id),
                    embedding_model=embedding_model
                )

                total_results["articles_processed"] += 1
                total_results["total_chunks_created"] += results["chunks_created"]
                total_results["total_chunks_updated"] += results["chunks_updated"]

        except Exception as e:
            logger.error(f"Failed to process article {article.title}: {str(e)}")
            total_results["articles_failed"] += 1
            total_results["errors"].append({
                "article_id": str(article.id),
                "article_title": article.title,
                "error": str(e)
            })

    logger.info(
        f"Batch processing complete: {total_results['articles_processed']} processed, "
        f"{total_results['articles_skipped']} skipped, "
        f"{total_results['articles_failed']} failed, "
        f"{total_results['total_chunks_created']} chunks created, "
        f"{total_results['total_chunks_updated']} chunks updated"
    )

    return total_results
