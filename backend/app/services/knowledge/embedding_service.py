"""
Embedding generation service using Google Gemini.

This module provides utilities for:
- Generating embeddings for text using Google Gemini API
- Batch processing multiple texts
- Chunking text into optimal sizes for embedding
"""

import os
import logging
from typing import List, Dict, Any, Optional
import re

logger = logging.getLogger(__name__)

# Configuration
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"  # Google's latest embedding model
DEFAULT_EMBEDDING_DIMENSIONS = 768
CHUNK_SIZE = 1000  # Characters per chunk
CHUNK_OVERLAP_PERCENT = 0.10  # 10% overlap between chunks


class EmbeddingError(Exception):
    """Exception raised when embedding generation fails."""
    pass


def get_embedding_client():
    """
    Get Google GenAI client.

    Returns:
        Configured Google GenAI client

    Raises:
        EmbeddingError: If API key is not configured
    """
    try:
        from google import genai

        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise EmbeddingError("GOOGLE_API_KEY environment variable not set")

        client = genai.Client(api_key=api_key)
        return client

    except ImportError:
        raise EmbeddingError(
            "google-genai package not installed. "
            "Install with: pip install google-genai"
        )
    except Exception as e:
        raise EmbeddingError(f"Failed to initialize embedding client: {str(e)}")


def chunk_text(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    overlap_percent: float = CHUNK_OVERLAP_PERCENT
) -> List[str]:
    """
    Split text into chunks with overlap for better context preservation.

    Args:
        text: Text to chunk
        chunk_size: Maximum characters per chunk
        overlap_percent: Percentage of chunk_size to overlap (default 0.10 = 10%)

    Returns:
        List of text chunks
    """
    if len(text) <= chunk_size:
        return [text]

    # Calculate overlap as percentage of chunk size
    overlap = int(chunk_size * overlap_percent)

    chunks = []
    start = 0

    while start < len(text):
        # Get chunk
        end = start + chunk_size

        # If not at the end, try to break at a sentence or word boundary
        if end < len(text):
            # Look for sentence boundary (. ! ?)
            sentence_end = max(
                text.rfind('. ', start, end),
                text.rfind('! ', start, end),
                text.rfind('? ', start, end),
                text.rfind('\n\n', start, end)
            )

            if sentence_end > start + chunk_size // 2:
                end = sentence_end + 1
            else:
                # No sentence boundary, look for word boundary
                word_end = text.rfind(' ', start, end)
                if word_end > start + chunk_size // 2:
                    end = word_end

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        # Move start with overlap
        start = end - overlap

        # Prevent infinite loop
        if start >= len(text):
            break

    return chunks


def generate_embedding(
    text: str,
    model: str = DEFAULT_EMBEDDING_MODEL
) -> tuple[List[float], int]:
    """
    Generate embedding for a single text.

    Args:
        text: Text to embed
        model: Embedding model to use

    Returns:
        Tuple of (embedding vector, dimensions)

    Raises:
        EmbeddingError: If embedding generation fails
    """
    try:
        client = get_embedding_client()

        # Generate embedding
        response = client.models.embed_content(
            model=model,
            contents=text,
        )

        # Extract embedding from response
        embedding = response.embeddings[0].values
        dimensions = len(embedding)

        logger.debug(f"Generated embedding with {dimensions} dimensions")
        return embedding, dimensions

    except Exception as e:
        logger.error(f"Failed to generate embedding: {str(e)}")
        raise EmbeddingError(f"Embedding generation failed: {str(e)}")


def generate_embeddings_batch(
    texts: List[str],
    model: str = DEFAULT_EMBEDDING_MODEL
) -> List[tuple[List[float], int]]:
    """
    Generate embeddings for multiple texts in batch.

    The new API supports batch processing natively.

    Args:
        texts: List of texts to embed
        model: Embedding model to use

    Returns:
        List of (embedding vector, dimensions) tuples

    Raises:
        EmbeddingError: If embedding generation fails
    """
    try:
        client = get_embedding_client()

        embeddings = []

        # Process in batches to respect rate limits
        batch_size = 100
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]

            # New API supports batch processing natively
            response = client.models.embed_content(
                model=model,
                contents=batch,
            )

            # Extract embeddings from response
            for emb in response.embeddings:
                embedding = emb.values
                dimensions = len(embedding)
                embeddings.append((embedding, dimensions))

            logger.debug(f"Processed batch {i//batch_size + 1}, total embeddings: {len(embeddings)}")

        return embeddings

    except Exception as e:
        logger.error(f"Failed to generate embeddings batch: {str(e)}")
        raise EmbeddingError(f"Batch embedding generation failed: {str(e)}")


def generate_query_embedding(
    query: str,
    model: str = DEFAULT_EMBEDDING_MODEL
) -> tuple[List[float], int]:
    """
    Generate embedding for a search query.

    Note: The new API doesn't require separate task types.

    Args:
        query: Search query text
        model: Embedding model to use

    Returns:
        Tuple of (embedding vector, dimensions)

    Raises:
        EmbeddingError: If embedding generation fails
    """
    try:
        client = get_embedding_client()

        # Generate embedding
        response = client.models.embed_content(
            model=model,
            contents=query,
        )

        # Extract embedding from response
        embedding = response.embeddings[0].values
        dimensions = len(embedding)

        logger.debug(f"Generated query embedding with {dimensions} dimensions")
        return embedding, dimensions

    except Exception as e:
        logger.error(f"Failed to generate query embedding: {str(e)}")
        raise EmbeddingError(f"Query embedding generation failed: {str(e)}")


def prepare_article_for_embedding(title: str, description: str, content: str) -> str:
    """
    Prepare article text for embedding by combining title, description, and content.

    Args:
        title: Article title
        description: Article description
        content: Article content

    Returns:
        Combined text for embedding
    """
    parts = []

    if title:
        parts.append(f"Title: {title}")

    if description:
        parts.append(f"Description: {description}")

    if content:
        parts.append(f"Content: {content}")

    return "\n\n".join(parts)
