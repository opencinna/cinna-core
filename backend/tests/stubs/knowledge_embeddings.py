"""
Pre-defined synthetic embedding vectors for knowledge query tests.

Uses 768-dimensional unit vectors (matching gemini-embedding-001 dimensions):

  RELEVANT_EMBEDDING   — points in dimension 0: cosine similarity with QUERY = 1.0
  IRRELEVANT_EMBEDDING — points in dimension 1: cosine similarity with QUERY = 0.0
  QUERY_EMBEDDING      — same direction as RELEVANT_EMBEDDING

These are pure math — no Gemini API needed. The knowledge query endpoint's
`generate_query_embedding` call is mocked to return QUERY_EMBEDDING, while the
article chunks stored in the DB carry these pre-defined embeddings. The vector
search service computes cosine similarity in Python, so the test exercises the
real ranking logic without any external calls.
"""

EMBEDDING_DIMS = 768
EMBEDDING_MODEL = "gemini-embedding-001"

# Unit vector in dimension 0: perfectly aligned with QUERY_EMBEDDING (cosine = 1.0)
RELEVANT_EMBEDDING: list[float] = [1.0] + [0.0] * (EMBEDDING_DIMS - 1)

# Unit vector in dimension 1: orthogonal to QUERY_EMBEDDING (cosine = 0.0)
IRRELEVANT_EMBEDDING: list[float] = [0.0, 1.0] + [0.0] * (EMBEDDING_DIMS - 2)

# Query vector — same direction as RELEVANT_EMBEDDING
QUERY_EMBEDDING: list[float] = [1.0] + [0.0] * (EMBEDDING_DIMS - 1)


# ---------------------------------------------------------------------------
# Article fixture data
# ---------------------------------------------------------------------------

ARTICLE_RELEVANT_A: dict = {
    "title": "Python Integration Guide",
    "description": "How to integrate Python applications with external REST APIs",
    "tags": ["python", "integration", "api"],
    "features": ["sdk", "requests"],
    "file_path": "articles/python-integration.md",
    "content": (
        "# Python Integration Guide\n\n"
        "This guide covers essential patterns for integrating Python applications "
        "with external REST APIs. Topics include authentication, error handling, "
        "rate limiting, and SDK usage.\n\n"
        "## Authentication\n\nUse Bearer tokens or API keys passed in request headers. "
        "Always store secrets in environment variables, never in source code.\n\n"
        "## Error Handling\n\nWrap API calls in try/except blocks. Implement "
        "exponential backoff for rate limit errors (HTTP 429).\n\n"
        "## SDK Usage\n\nPrefer official SDKs over raw HTTP when available — they "
        "handle authentication, retries, and response parsing automatically.\n"
    ),
}

ARTICLE_RELEVANT_B: dict = {
    "title": "REST API Authentication Patterns",
    "description": "JWT and OAuth2 authentication for REST API integrations",
    "tags": ["auth", "jwt", "oauth2", "api"],
    "features": ["security", "tokens"],
    "file_path": "articles/api-auth.md",
    "content": (
        "# REST API Authentication Patterns\n\n"
        "Secure your API integrations using JWT tokens and OAuth2 flows.\n\n"
        "## JWT Tokens\n\nGenerate short-lived access tokens signed with a secret key. "
        "Include the token in the Authorization header: `Bearer <token>`.\n\n"
        "## OAuth2\n\nUse the authorization code flow for user-facing integrations. "
        "Client credentials flow is suitable for server-to-server API calls.\n\n"
        "## API Keys\n\nFor simpler integrations, API keys provide straightforward "
        "authentication. Rotate keys regularly and scope them to minimum permissions.\n"
    ),
}

ARTICLE_IRRELEVANT: dict = {
    "title": "Company HR Policies",
    "description": "Internal HR guidelines covering leave, benefits, and workplace conduct",
    "tags": ["hr", "internal", "policy", "benefits"],
    "features": ["leave", "conduct"],
    "file_path": "articles/hr-policies.md",
    "content": (
        "# Company HR Policies\n\n"
        "This document outlines the company's human resources policies and procedures.\n\n"
        "## Annual Leave\n\nAll full-time employees are entitled to 25 days of paid "
        "annual leave per calendar year. Leave must be approved by your line manager "
        "at least two weeks in advance.\n\n"
        "## Benefits\n\nThe company provides health insurance, pension contributions "
        "(5% employer match), and an annual gym membership allowance.\n\n"
        "## Workplace Conduct\n\nEmployees are expected to maintain a professional "
        "and respectful environment. See the Code of Conduct for full details.\n"
    ),
}
