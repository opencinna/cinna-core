# Knowledge Query Tool (Agent-Env Side)

## Purpose

MCP tool running inside agent Docker containers that enables agents to query indexed knowledge sources via a reverse API call to the backend. Available only in **building mode** — agents in conversation mode cannot access this tool.

## How It Works

The tool implements a **reverse API call pattern**: instead of the backend pushing knowledge into the environment, the agent environment calls back to the backend when it needs knowledge.

1. Agent invokes `query_integration_knowledge` tool with a query string
2. Tool reads `BACKEND_URL`, `AGENT_AUTH_TOKEN`, and `ENV_ID` from environment variables
3. Makes authenticated HTTP POST to `POST /api/v1/knowledge/query` on the backend
4. Backend validates two-factor auth (bearer token + environment ID), runs vector search
5. Returns matching articles (discovery) or full content (retrieval)
6. Tool formats the response for agent consumption

### Two-Step Flow

- **Discovery** (query only): Returns article metadata (title, description, tags, source name) ranked by semantic similarity
- **Retrieval** (query + article_ids): Returns full article content for selected articles, with access control validation

## Registration

- Registered in the Claude Code adapter as an MCP server: `create_sdk_mcp_server(name="knowledge", tools=[query_integration_knowledge])`
- Full tool name: `mcp__knowledge__query_integration_knowledge`
- Only loaded when `mode == "building"` — skipped for conversation mode
- Import failure is handled gracefully (logged, tool not registered)
- Listed in backend's pre-allowed tools — agents can invoke without per-call user approval

## Authentication

Two-factor header-based auth (separate from user JWT):

1. `Authorization: Bearer <AGENT_AUTH_TOKEN>` - Token generated at environment creation, stored in `environment.config["auth_token"]`
2. `X-Agent-Env-Id: <ENV_ID>` - Environment UUID header

Backend validates both match the database record, preventing token reuse across environments.

## Environment Variables

| Variable | Source | Purpose |
|----------|--------|---------|
| `BACKEND_URL` | `http://backend:8000` (Docker network) | Backend API base URL |
| `AGENT_AUTH_TOKEN` | Generated at env creation | Bearer token for auth |
| `ENV_ID` | Environment UUID | Identifies the calling environment |

Set in the container `.env` file by `backend/app/services/environment_lifecycle.py:_generate_env_file()`.

## Error Handling

- **Timeout**: Returns descriptive timeout error
- **Connection error**: Reports backend unreachable
- **Auth failure (401)**: Reports invalid token or environment ID
- **Validation**: UUID format validation for article_ids, supports CSV/list/single UUID input formats

## File References

- **Tool implementation**: `backend/app/env-templates/app_core_base/core/server/tools/knowledge_query.py`
- **Adapter registration**: `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py`
- **Backend endpoint**: `backend/app/api/routes/knowledge.py` - `query_knowledge()` + `verify_agent_auth_token()` dependency
- **Pre-allowed list**: `backend/app/services/message_service.py`
- **Frontend rendering**: `frontend/src/components/Chat/ToolCallBlock.tsx` - `KnowledgeQueryToolBlock` component

## Related Docs

- [Knowledge Sources](../../application/knowledge_sources/knowledge_sources.md) - Full feature documentation (sources, articles, embeddings, discovery)
- [Knowledge Sources Tech](../../application/knowledge_sources/knowledge_sources_tech.md) - Backend services, database schema, API endpoints
- [Agent Environment Core](agent_environment_core.md) - Parent feature: server running inside Docker containers

