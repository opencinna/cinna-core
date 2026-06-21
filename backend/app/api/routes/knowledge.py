import logging
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import AgentEnvContextDep, SessionDep
from app.models import ArticleListItem, ArticleContent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class KnowledgeQueryRequest(BaseModel):
    """Request model for querying integration knowledge."""
    query: str
    article_ids: list[uuid.UUID] | None = None


class KnowledgeQueryResponseDiscovery(BaseModel):
    """Response for discovery step (article list)."""
    type: str = "article_list"
    articles: list[ArticleListItem]


class KnowledgeQueryResponseRetrieval(BaseModel):
    """Response for retrieval step (full articles)."""
    type: str = "full_articles"
    articles: list[ArticleContent]


@router.post("/query")
async def query_knowledge(
    request: KnowledgeQueryRequest,
    session: SessionDep,
    ctx: AgentEnvContextDep,
) -> KnowledgeQueryResponseDiscovery | KnowledgeQueryResponseRetrieval:
    """
    Query the integration knowledge base with two-step discovery/retrieval.

    **Step 1: Discovery (no article_ids):**
    - Generate embedding for query
    - Search for relevant article chunks
    - Return list of matching articles with metadata

    **Step 2: Retrieval (with article_ids):**
    - Retrieve full content for specified articles
    - Validate access permissions
    - Return full article content

    Args:
        request: Query request with search string and optional article IDs
        session: Database session
        environment: Authenticated environment (injected by dependency)

    Returns:
        Discovery response (article list) or retrieval response (full articles)
    """
    from app.services.knowledge.embedding_service import generate_query_embedding, DEFAULT_EMBEDDING_MODEL
    from app.services.knowledge.vector_search_service import (
        search_knowledge,
        get_articles_by_ids,
        get_accessible_source_ids,
        VectorSearchError
    )

    environment = ctx.environment
    logger.info(f"Knowledge query from environment {environment.id}: {request.query}")

    # Agent + owner are already resolved (and scope-verified) by the dep.
    agent = ctx.agent
    user_id = agent.owner_id
    workspace_id = agent.user_workspace_id

    # Step 2: Retrieval - return full articles
    if request.article_ids:
        logger.info(f"Retrieval request for {len(request.article_ids)} articles")

        try:
            # Get accessible sources for permission check
            source_ids = get_accessible_source_ids(
                session=session,
                user_id=user_id,
                workspace_id=workspace_id
            )

            # Get full article content
            articles = get_articles_by_ids(
                session=session,
                article_ids=request.article_ids,
                source_ids=source_ids
            )

            return KnowledgeQueryResponseRetrieval(
                type="full_articles",
                articles=articles
            )

        except VectorSearchError as e:
            logger.error(f"Access denied for articles: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(e)
            )
        except Exception as e:
            logger.error(f"Error retrieving articles: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to retrieve articles: {str(e)}"
            )

    # Step 1: Discovery - search and return article list
    logger.info(f"Discovery request for query: {request.query}")

    try:
        # Generate query embedding
        query_embedding, dimensions = generate_query_embedding(
            query=request.query,
            model=DEFAULT_EMBEDDING_MODEL
        )

        logger.debug(f"Generated query embedding with {dimensions} dimensions")

        # Search for articles
        articles = search_knowledge(
            session=session,
            query_embedding=query_embedding,
            user_id=user_id,
            workspace_id=workspace_id,
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            limit=10
        )

        logger.info(f"Discovery found {len(articles)} articles")

        return KnowledgeQueryResponseDiscovery(
            type="article_list",
            articles=articles
        )

    except Exception as e:
        logger.error(f"Error during knowledge search: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Knowledge search failed: {str(e)}"
        )
