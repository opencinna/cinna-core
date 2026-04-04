import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Header, status, Depends
from pydantic import BaseModel

from app.api.deps import SessionDep
from app.models import AgentEnvironment, Agent, ArticleListItem, ArticleContent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


async def verify_agent_auth_token(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
    x_agent_env_id: Annotated[str | None, Header()] = None
) -> AgentEnvironment:
    """
    Verify the Authorization header and X-Agent-Env-Id header match a valid environment.

    This validates that:
    1. The environment ID exists in the database
    2. The auth token matches the environment's stored token
    3. The environment belongs to a valid agent

    Args:
        session: Database session
        authorization: Authorization header value (e.g., "Bearer <token>")
        x_agent_env_id: Environment ID header

    Returns:
        The verified AgentEnvironment object

    Raises:
        HTTPException: If authentication fails
    """
    # Validate Authorization header is present
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header"
        )

    # Validate X-Agent-Env-Id header is present
    if not x_agent_env_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Agent-Env-Id header"
        )

    # Parse Authorization header (format: "Bearer <token>")
    try:
        scheme, token = authorization.split(" ", 1)
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme. Expected 'Bearer'"
            )

        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication token"
            )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected 'Bearer <token>'"
        )

    # Parse environment ID
    try:
        env_id = uuid.UUID(x_agent_env_id)
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid environment ID format"
        )

    # Look up environment in database
    environment = session.get(AgentEnvironment, env_id)
    if not environment:
        logger.warning(f"Authentication failed: Environment {env_id} not found")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid environment ID"
        )

    # Verify token matches the environment's auth token
    stored_token = environment.config.get("auth_token")
    if not stored_token or stored_token != token:
        logger.warning(f"Authentication failed: Token mismatch for environment {env_id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token"
        )

    logger.info(f"Successfully authenticated environment {env_id}")
    return environment


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
    environment: Annotated[AgentEnvironment, Depends(verify_agent_auth_token)]
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

    logger.info(f"Knowledge query from environment {environment.id}: {request.query}")

    # Get agent and user information
    agent = session.get(Agent, environment.agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )

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
