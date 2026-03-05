"""
Integration test for POST /api/v1/knowledge/query.

This endpoint is called by agent Docker containers via the knowledge MCP tool
(reverse API call pattern). It uses two-factor auth: a Bearer token + an
X-Agent-Env-Id header, both validated against the AgentEnvironment DB record.

Two-step flow under test:
  - Discovery (no article_ids): generates query embedding, runs cosine-similarity
    search across article chunks, returns ranked article metadata.
  - Retrieval (with article_ids): returns full article content with access validation.

Setup approach
--------------
The endpoint requires an AgentEnvironment with a known auth_token, and articles
with pre-computed chunk embeddings. Neither is reachable through the public API, so
both are inserted directly via the `db` fixture (same session the TestClient uses).

Google Gemini embedding generation is mocked with pre-defined unit vectors from
tests/stubs/knowledge_embeddings.py. The vector search service (cosine similarity)
runs unpatched, exercising the real ranking logic.
"""

import hashlib
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models import Agent, AgentEnvironment, KnowledgeArticle, KnowledgeArticleChunk
from tests.stubs.knowledge_embeddings import (
    ARTICLE_IRRELEVANT,
    ARTICLE_RELEVANT_A,
    ARTICLE_RELEVANT_B,
    EMBEDDING_DIMS,
    EMBEDDING_MODEL,
    IRRELEVANT_EMBEDDING,
    QUERY_EMBEDDING,
    RELEVANT_EMBEDDING,
)
from tests.utils.knowledge_source import create_knowledge_source

_QUERY_URL = f"{settings.API_V1_STR}/knowledge/query"
_KSBASE = f"{settings.API_V1_STR}/knowledge-sources"

# Fixed token used for all auth header assertions in this test
_TEST_AUTH_TOKEN = "test-knowledge-query-token-abc123-fixture"


# ---------------------------------------------------------------------------
# Local helper
# ---------------------------------------------------------------------------

def _insert_article(
    db: Session,
    source_id: uuid.UUID,
    article_data: dict,
    embedding: list[float],
) -> KnowledgeArticle:
    """Insert a KnowledgeArticle + one KnowledgeArticleChunk with the given embedding.

    Uses db.flush() so records are visible within the same session (and therefore
    to the TestClient) without committing the outer savepoint transaction.
    """
    content_hash = hashlib.sha256(article_data["content"].encode()).hexdigest()
    article = KnowledgeArticle(
        git_repo_id=source_id,
        title=article_data["title"],
        description=article_data["description"],
        tags=article_data["tags"],
        features=article_data["features"],
        file_path=article_data["file_path"],
        content=article_data["content"],
        content_hash=content_hash,
    )
    db.add(article)
    db.flush()

    chunk = KnowledgeArticleChunk(
        article_id=article.id,
        chunk_index=0,
        chunk_text=article_data["content"],
        embedding=embedding,
        embedding_model=EMBEDDING_MODEL,
        embedding_dimensions=EMBEDDING_DIMS,
    )
    db.add(chunk)
    db.flush()
    return article


# ---------------------------------------------------------------------------
# Scenario test
# ---------------------------------------------------------------------------

def test_knowledge_query_two_step_flow(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Full knowledge query flow as exercised by the agent MCP tool:
      1.  Insert Agent + AgentEnvironment with known auth_token into the test DB
      2.  Create a knowledge source via API and mark it as connected
      3.  Insert 2 relevant articles and 1 irrelevant article with pre-computed embeddings
      4.  Auth guards: missing token, wrong token, unknown env, malformed env ID all → 401
      5.  Discovery (query only): relevant articles appear first, irrelevant last
      6.  Retrieval (query + article_ids): full article content returned
      7.  Access control: retrieval for an inaccessible article_id → 403
    """

    # ── Phase 1: Create agent environment with known auth_token ───────────
    # The knowledge query endpoint resolves the agent owner from the environment,
    # so owner_id must match the user who owns the knowledge source.
    me_r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert me_r.status_code == 200
    user_id = uuid.UUID(me_r.json()["id"])

    # auth_token is stored in config — not exposed via any API endpoint.
    agent = Agent(name="Knowledge Query Test Agent", owner_id=user_id)
    db.add(agent)
    db.flush()

    env = AgentEnvironment(
        agent_id=agent.id,
        env_name=settings.DEFAULT_AGENT_ENV_NAME,
        status="running",
        is_active=True,
        config={"auth_token": _TEST_AUTH_TOKEN},
    )
    db.add(env)
    db.flush()
    env_id = str(env.id)

    auth_headers = {
        "Authorization": f"Bearer {_TEST_AUTH_TOKEN}",
        "X-Agent-Env-Id": env_id,
    }

    # ── Phase 2: Create knowledge source and mark it as connected ─────────
    source = create_knowledge_source(client, superuser_token_headers)
    source_id = uuid.UUID(source["id"])

    with patch(
        "app.services.knowledge_source_service.verify_repository_access",
        return_value=(True, "Repository accessible"),
    ):
        r = client.post(
            f"{_KSBASE}/{source_id}/check-access", headers=superuser_token_headers
        )
        assert r.status_code == 200
        assert r.json()["accessible"] is True

    # ── Phase 3: Insert articles with pre-computed embeddings ─────────────
    # RELEVANT_EMBEDDING is a unit vector in dimension 0.
    # IRRELEVANT_EMBEDDING is a unit vector in dimension 1 (orthogonal).
    # QUERY_EMBEDDING matches RELEVANT_EMBEDDING exactly, so:
    #   cosine_similarity(query, relevant)   = 1.0
    #   cosine_similarity(query, irrelevant) = 0.0
    article_a = _insert_article(db, source_id, ARTICLE_RELEVANT_A, RELEVANT_EMBEDDING)
    article_b = _insert_article(db, source_id, ARTICLE_RELEVANT_B, RELEVANT_EMBEDDING)
    article_irr = _insert_article(db, source_id, ARTICLE_IRRELEVANT, IRRELEVANT_EMBEDDING)

    # ── Phase 4: Auth guard checks ─────────────────────────────────────────
    payload = {"query": "python api integration"}

    # No Authorization header at all
    r = client.post(_QUERY_URL, json=payload)
    assert r.status_code == 401

    # Valid env_id, wrong bearer token
    r = client.post(
        _QUERY_URL,
        headers={"Authorization": "Bearer wrong-token", "X-Agent-Env-Id": env_id},
        json=payload,
    )
    assert r.status_code == 401

    # Valid token, valid UUID format but no matching environment record
    r = client.post(
        _QUERY_URL,
        headers={
            "Authorization": f"Bearer {_TEST_AUTH_TOKEN}",
            "X-Agent-Env-Id": str(uuid.uuid4()),
        },
        json=payload,
    )
    assert r.status_code == 401

    # Valid token, malformed env ID (not a UUID)
    r = client.post(
        _QUERY_URL,
        headers={
            "Authorization": f"Bearer {_TEST_AUTH_TOKEN}",
            "X-Agent-Env-Id": "not-a-valid-uuid",
        },
        json=payload,
    )
    assert r.status_code == 401

    # ── Phase 5: Discovery — ranked semantic search ────────────────────────
    # Mock Gemini so no real API call is made. The vector search service (cosine
    # similarity) runs unpatched, exercising the real ranking logic.
    with patch(
        "app.services.embedding_service.generate_query_embedding",
        return_value=(QUERY_EMBEDDING, EMBEDDING_DIMS),
    ):
        r = client.post(_QUERY_URL, headers=auth_headers, json=payload)

    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "article_list"

    articles = body["articles"]
    returned_ids = [a["id"] for a in articles]

    # Both relevant articles must appear
    assert str(article_a.id) in returned_ids, "Relevant article A missing from discovery"
    assert str(article_b.id) in returned_ids, "Relevant article B missing from discovery"
    assert str(article_irr.id) in returned_ids, "Irrelevant article should still be returned (low score)"

    # Relevant articles (cosine = 1.0) must rank above the irrelevant one (cosine = 0.0)
    irr_pos = returned_ids.index(str(article_irr.id))
    for rel_article in (article_a, article_b):
        rel_pos = returned_ids.index(str(rel_article.id))
        assert rel_pos < irr_pos, (
            f"Relevant article '{rel_article.title}' (pos {rel_pos}) "
            f"should rank above irrelevant (pos {irr_pos})"
        )

    # Every returned item must carry the required metadata fields
    for item in articles:
        assert "id" in item
        assert "title" in item
        assert "description" in item
        assert "tags" in item
        assert "features" in item
        assert "source_name" in item
        assert "git_repo_id" in item

    # ── Phase 6: Retrieval — full article content ──────────────────────────
    # No embedding is generated in retrieval — the mock is not needed here.
    r = client.post(
        _QUERY_URL,
        headers=auth_headers,
        json={"query": payload["query"], "article_ids": [str(article_a.id)]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "full_articles"
    assert len(body["articles"]) == 1

    full = body["articles"][0]
    assert full["id"] == str(article_a.id)
    assert full["title"] == ARTICLE_RELEVANT_A["title"]
    assert full["description"] == ARTICLE_RELEVANT_A["description"]
    assert full["content"] == ARTICLE_RELEVANT_A["content"]
    assert full["file_path"] == ARTICLE_RELEVANT_A["file_path"]
    assert full["tags"] == ARTICLE_RELEVANT_A["tags"]
    assert "source_name" in full

    # ── Phase 7: Access control — inaccessible article → 403 ──────────────
    # A random UUID that has no matching article in any accessible source.
    r = client.post(
        _QUERY_URL,
        headers=auth_headers,
        json={"query": payload["query"], "article_ids": [str(uuid.uuid4())]},
    )
    assert r.status_code == 403
