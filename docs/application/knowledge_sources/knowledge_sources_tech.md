# Knowledge Sources - Technical Details

## File Locations

### Backend

- **Models**: `backend/app/models/knowledge.py` - `AIKnowledgeGitRepo`, `AIKnowledgeGitRepoWorkspace`, `KnowledgeArticle`, `KnowledgeArticleChunk`, `UserEnabledDiscoverableSource`, enums (`SourceStatus`, `WorkspaceAccessType`), request/response schemas
- **Routes (user CRUD)**: `backend/app/api/routes/knowledge_sources.py` - Source management, articles, discovery endpoints
- **Routes (agent query)**: `backend/app/api/routes/knowledge.py` - Knowledge query endpoint for agents
- **Source service**: `backend/app/services/knowledge_source_service.py` - `KnowledgeSourceService` (CRUD, check-access, refresh, discovery)
- **Article service**: `backend/app/services/knowledge_article_service.py` - Article parsing, upserting, content hashing, embedding orchestration
- **Git operations**: `backend/app/services/git_operations.py` - Clone, verify, SSH key file management, URL conversion
- **Embedding service**: `backend/app/services/embedding_service.py` - Google Gemini embeddings, text chunking
- **Vector search**: `backend/app/services/vector_search_service.py` - Cosine similarity, access control, article retrieval

### Migrations

- `backend/app/alembic/versions/240176144d01_add_knowledge_management_tables.py` - Core tables (git_repo, workspaces, articles)
- `backend/app/alembic/versions/0df6011bb22d_add_public_discovery_and_user_enabled.py` - Public discovery, user enablement, username column
- `backend/app/alembic/versions/f8a9c3d1e4b2_add_knowledge_article_chunks_table.py` - Article chunks for semantic search

### Agent Environment (Docker Container)

- **Knowledge query tool**: `backend/app/env-templates/app_core_base/core/server/tools/knowledge_query.py` - MCP tool `query_integration_knowledge`, two-step discovery/retrieval, reads `BACKEND_URL`/`AGENT_AUTH_TOKEN`/`ENV_ID` env vars, UUID validation, error handling
- **Claude Code adapter**: `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py` - Registers knowledge MCP server in building mode only: `create_sdk_mcp_server(name="knowledge", tools=[query_integration_knowledge])` → tool name `mcp__knowledge__query_integration_knowledge`
- **Tools package**: `backend/app/env-templates/app_core_base/core/server/tools/__init__.py`

### Frontend

- **Sources list page**: `frontend/src/routes/_layout/knowledge-sources.tsx` - My sources + discoverable sources
- **Source detail page**: `frontend/src/routes/_layout/knowledge-source/$sourceId.tsx` - Tabs for configuration and articles
- **Add modal**: `frontend/src/components/KnowledgeSources/AddSourceModal.tsx` - Create source with SSH key and workspace selection
- **Edit modal**: `frontend/src/components/KnowledgeSources/EditSourceModal.tsx` - Update source settings (Git URL read-only)
- **Configuration tab**: `frontend/src/components/KnowledgeSources/KnowledgeSourceConfigurationTab.tsx` - Status display, enable/disable, check access, refresh
- **Articles tab**: `frontend/src/components/KnowledgeSources/KnowledgeSourceArticlesTab.tsx` - Article list with tags and features
- **Knowledge tool render**: `frontend/src/components/Chat/ToolCallBlock.tsx` - Detects `mcp__knowledge__query_integration_knowledge` tool calls, renders via `KnowledgeQueryToolBlock` component
- **API client**: `frontend/src/client/sdk.gen.ts` - `KnowledgeSourcesService`

## Database Schema

### Table: `ai_knowledge_git_repo`

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| user_id | UUID | FK -> user(id) ON DELETE CASCADE, indexed |
| name | VARCHAR | NOT NULL, indexed |
| description | TEXT | nullable |
| git_url | VARCHAR | NOT NULL |
| branch | VARCHAR | NOT NULL, default "main" |
| ssh_key_id | UUID | FK -> user_ssh_keys(id), nullable |
| is_enabled | BOOLEAN | NOT NULL, default true, indexed |
| status | VARCHAR | NOT NULL, default "pending", indexed (enum: pending/connected/error/disconnected) |
| status_message | TEXT | nullable |
| last_checked_at | DATETIME | nullable |
| last_sync_at | DATETIME | nullable |
| sync_commit_hash | VARCHAR | nullable |
| workspace_access_type | VARCHAR | NOT NULL, default "all" (enum: all/specific) |
| public_discovery | BOOLEAN | NOT NULL, default false, indexed |
| created_at | DATETIME | NOT NULL |
| updated_at | DATETIME | NOT NULL |

### Table: `ai_knowledge_git_repo_workspaces`

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| git_repo_id | UUID | FK -> ai_knowledge_git_repo(id) ON DELETE CASCADE, indexed |
| user_workspace_id | UUID | FK -> user_workspace(id) ON DELETE CASCADE, indexed |
| created_at | DATETIME | NOT NULL |

Unique: `idx_git_repo_workspace_unique` on `(git_repo_id, user_workspace_id)`

### Table: `knowledge_articles`

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| git_repo_id | UUID | FK -> ai_knowledge_git_repo(id) ON DELETE CASCADE, indexed |
| title | VARCHAR | NOT NULL |
| description | TEXT | NOT NULL |
| tags | JSON | default [] |
| features | JSON | default [] |
| file_path | VARCHAR | NOT NULL |
| content | TEXT | NOT NULL |
| content_hash | VARCHAR | NOT NULL |
| embedding | JSON | nullable (article-level, future use) |
| embedding_model | VARCHAR | nullable, indexed |
| embedding_dimensions | INTEGER | nullable |
| commit_hash | VARCHAR | nullable |
| created_at | DATETIME | NOT NULL |
| updated_at | DATETIME | NOT NULL |

Unique: `idx_article_repo_path_unique` on `(git_repo_id, file_path)`

### Table: `knowledge_article_chunks`

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| article_id | UUID | FK -> knowledge_articles(id) ON DELETE CASCADE, indexed |
| chunk_index | INTEGER | NOT NULL |
| chunk_text | TEXT | NOT NULL |
| embedding | JSON | nullable (vector data) |
| embedding_model | VARCHAR | nullable, indexed |
| embedding_dimensions | INTEGER | nullable |
| created_at | DATETIME | NOT NULL |

Unique: `idx_chunk_article_idx_unique` on `(article_id, chunk_index)`

### Table: `user_enabled_discoverable_sources`

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| user_id | UUID | FK -> user(id) ON DELETE CASCADE, indexed |
| git_repo_id | UUID | FK -> ai_knowledge_git_repo(id) ON DELETE CASCADE, indexed |
| created_at | DATETIME | NOT NULL |

Unique: `idx_user_source_unique` on `(user_id, git_repo_id)`

## Environment Variables (Agent Container)

Injected into the agent container's `.env` file by `backend/app/services/environment_lifecycle.py:_generate_env_file()`:

| Variable | Value | Purpose |
|----------|-------|---------|
| `BACKEND_URL` | `http://backend:8000` | Backend API URL via Docker network service name |
| `AGENT_AUTH_TOKEN` | Generated UUID | Bearer token for knowledge query auth |
| `ENV_ID` | Environment UUID | Identifies the agent environment |

## Pre-Allowed Tools

`backend/app/services/message_service.py` - `mcp__knowledge__query_integration_knowledge` is in the pre-allowed tools list, meaning agents can invoke it without per-call user approval. Other pre-allowed tools: `mcp__task__create_agent_task`, `mcp__task__update_session_state`, `mcp__task__respond_to_task`.

## API Endpoints

### User Source Management

**Route file**: `backend/app/api/routes/knowledge_sources.py`
**Prefix**: `/api/v1/knowledge-sources` | **Tag**: `knowledge-sources`

| Method | Path | Description | Request | Response |
|--------|------|-------------|---------|----------|
| GET | `/` | List user's sources | `?workspace_id&skip&limit` | `list[AIKnowledgeGitRepoPublic]` |
| POST | `/` | Create source | `AIKnowledgeGitRepoCreate` | `AIKnowledgeGitRepoPublic` |
| GET | `/{source_id}` | Get source by ID | - | `AIKnowledgeGitRepoPublic` |
| PUT | `/{source_id}` | Update source | `AIKnowledgeGitRepoUpdate` | `AIKnowledgeGitRepoPublic` |
| DELETE | `/{source_id}` | Delete source (cascades) | - | `{"ok": true}` |
| POST | `/{source_id}/enable` | Enable source | - | `AIKnowledgeGitRepoPublic` |
| POST | `/{source_id}/disable` | Disable source | - | `AIKnowledgeGitRepoPublic` |
| POST | `/{source_id}/check-access` | Verify Git access (ls-remote) | - | `CheckAccessResponse` |
| POST | `/{source_id}/refresh` | Clone + parse + embed articles | - | `RefreshKnowledgeResponse` |
| GET | `/{source_id}/articles` | List articles | `?skip&limit` | `list[KnowledgeArticlePublic]` |

### Discovery Endpoints

| Method | Path | Description | Response |
|--------|------|-------------|----------|
| GET | `/discoverable/list` | List public sources from other users | `list[DiscoverableSourcePublic]` |
| POST | `/discoverable/{source_id}/enable` | Enable discoverable source for user | `{"ok": true}` |
| POST | `/discoverable/{source_id}/disable` | Disable discoverable source for user | `{"ok": true}` |

### Agent Knowledge Query

**Route file**: `backend/app/api/routes/knowledge.py`
**Prefix**: `/api/v1/knowledge`

| Method | Path | Description | Auth | Response |
|--------|------|-------------|------|----------|
| POST | `/query` | Two-step knowledge query | `Authorization: Bearer <env_token>` + `X-Agent-Env-Id` header | `KnowledgeQueryResponseDiscovery` or `KnowledgeQueryResponseRetrieval` |

Request body: `{ "query": "string", "article_ids": ["uuid"] }` - omit `article_ids` for discovery step, include for retrieval step.

## Services & Key Methods

### `backend/app/services/knowledge_source_service.py` - KnowledgeSourceService

| Method | Purpose |
|--------|---------|
| `create_source(session, user_id, data)` | Creates source with `pending` status, sets up workspace permissions if `specific` |
| `get_user_sources(session, user_id, workspace_id, skip, limit)` | Lists sources with computed `article_count` |
| `get_source_by_id(session, source_id, user_id)` | Gets source with ownership check |
| `update_source(session, source_id, user_id, data)` | Updates fields, resets status to `pending` if branch or SSH key changed |
| `delete_source(session, source_id, user_id)` | Deletes source (cascades to articles and permissions) |
| `enable_source(session, source_id, user_id)` | Sets `is_enabled=true` |
| `disable_source(session, source_id, user_id)` | Sets `is_enabled=false` |
| `check_access(session, source_id, user_id)` | Decrypts SSH key, runs `git ls-remote`, updates status |
| `refresh_knowledge(session, source_id, user_id)` | Full clone + parse + embed workflow |
| `get_source_articles(session, source_id, user_id, skip, limit)` | Lists articles for a source |
| `get_discoverable_sources(session, user_id, skip, limit)` | Lists public sources from other users with enablement status |
| `enable_discoverable_source(session, source_id, user_id)` | Creates `UserEnabledDiscoverableSource` record |
| `disable_discoverable_source(session, source_id, user_id)` | Removes enablement record |

### `backend/app/services/knowledge_article_service.py`

| Method | Purpose |
|--------|---------|
| `parse_settings_json(repo_path)` | Parses `.ai-knowledge/settings.json` into `KnowledgeSettings` |
| `calculate_content_hash(content)` | SHA256 hex digest for change detection |
| `read_article_file(repo_path, file_path)` | Reads article content from cloned repo |
| `upsert_article(session, git_repo_id, config, content, commit_hash)` | Insert or update based on content hash, returns `(article, is_new)` |
| `process_repository_articles(session, git_repo_id, repo_path, commit_hash)` | Batch process all articles in settings.json |
| `delete_orphaned_articles(session, git_repo_id, current_paths)` | Removes articles no longer in settings.json |
| `chunk_and_embed_article(session, article_id, model)` | Chunks article text and generates embeddings |
| `chunk_and_embed_all_articles(session, git_repo_id, model)` | Smart batch: only processes new/updated articles |

### `backend/app/services/git_operations.py`

| Method | Purpose |
|--------|---------|
| `create_ssh_key_file(private_key, passphrase)` | Context manager: temp file with `0o600` permissions, auto-cleanup |
| `create_git_ssh_command(ssh_key_path)` | Returns SSH command string with `StrictHostKeyChecking=no` |
| `verify_repository_access(git_url, branch, ssh_key_path)` | `git ls-remote` without cloning, returns `(accessible, message)` |
| `clone_repository(git_url, destination, branch, ssh_key_path, depth)` | Shallow clone (default depth=1) |
| `clone_repository_context(git_url, branch, ssh_key_path)` | Context manager: clone to temp dir, yields `(path, repo)`, auto-cleanup |
| `convert_https_to_ssh_url(git_url)` | Converts HTTPS to SSH format for key-based auth |
| `convert_ssh_to_https_url(git_url)` | Converts SSH to HTTPS format |
| `get_current_commit_hash(repo)` | Returns HEAD SHA |

Custom exceptions: `GitAuthenticationError`, `GitConnectionError`, `GitOperationError`

### `backend/app/services/embedding_service.py`

| Method | Purpose |
|--------|---------|
| `chunk_text(text, chunk_size, overlap_percent)` | Splits text at sentence/word boundaries, 1000 char chunks, 10% overlap |
| `generate_embedding(text, model)` | Single text embedding via Google Gemini |
| `generate_embeddings_batch(texts, model)` | Batch embedding (up to 100 per API call) |
| `generate_query_embedding(query, model)` | Query embedding for search |
| `prepare_article_for_embedding(title, description, content)` | Combines fields: "Title: ...\n\nDescription: ...\n\nContent: ..." |

Default config: model `gemini-embedding-001`, 768 dimensions, 1000 char chunks, 10% overlap, batch size 100

### `backend/app/services/vector_search_service.py`

| Method | Purpose |
|--------|---------|
| `get_accessible_source_ids(session, user_id, workspace_id)` | Returns source IDs: owned+enabled+connected + discoverable enabled |
| `cosine_similarity(vec1, vec2)` | In-memory cosine similarity calculation |
| `search_article_chunks(session, query_embedding, source_ids, model, limit)` | Finds most similar chunks across sources |
| `get_top_articles_from_chunks(chunk_results, limit)` | Groups by article, ranks by max similarity, deduplicates |
| `get_articles_by_ids(session, article_ids, source_ids)` | Full content retrieval with access validation |
| `search_knowledge(session, query_embedding, user_id, workspace_id, model, limit)` | High-level search combining access control + similarity |

## Frontend Components

### `frontend/src/routes/_layout/knowledge-sources.tsx`

- Two sections: "My Knowledge Sources" (own sources) + "Discoverable Sources" (public sources from others)
- Status badges (green=connected, yellow=pending, red=error, gray=disconnected)
- Article count badges, last sync timestamp
- Search and workspace filter for own sources
- Toggle switches for discoverable source enablement

### `frontend/src/routes/_layout/knowledge-source/$sourceId.tsx`

- Source detail page with two tabs (Configuration, Articles)
- Header with source name, edit/delete dropdown
- Back navigation to sources list

### `frontend/src/components/KnowledgeSources/AddSourceModal.tsx`

- Multi-field form: name, description, Git URL, branch, SSH key dropdown, workspace access type
- SSH key dropdown populated from user's SSH keys
- Workspace selection checkboxes (shown when access type is `specific`)
- After creation, allows immediate check-access

### `frontend/src/components/KnowledgeSources/EditSourceModal.tsx`

- Same fields as Add modal except Git URL is read-only with explanation text
- Public discovery toggle

### `frontend/src/components/KnowledgeSources/KnowledgeSourceConfigurationTab.tsx`

- Displays: Git URL (monospace), branch, SSH key status, connection status badge, last sync time
- Enable/disable toggle
- "Check Access" button (when not connected)
- "Refresh Knowledge" button (when enabled and connected)
- Status message display (error details, sync statistics)

### `frontend/src/components/KnowledgeSources/KnowledgeSourceArticlesTab.tsx`

- Alert if source is disabled (directs to Configuration tab)
- Table: title (bold), description, tags (first 3 + overflow), features (first 2 + overflow)
- Empty state with instructions about `.ai-knowledge/settings.json`
- Skeleton loaders during fetch

## Configuration

| Setting | Source | Purpose |
|---------|--------|---------|
| `ENCRYPTION_KEY` | `.env` | Decrypting SSH keys for Git operations |
| `GOOGLE_API_KEY` | `.env` | Google Gemini API for embedding generation |

## Dependencies

- `gitpython>=3.1.43` - Git clone, verify, pull operations
- `google-genai` - Google Gemini embedding API client
- `cryptography` - SSH key encryption/decryption (shared with SSH keys feature)

## Security

- **Source ownership**: All CRUD operations verify `user_id` before access
- **Agent auth**: Knowledge query uses two-factor header-based auth (`Authorization: Bearer <env_token>` + `X-Agent-Env-Id`), separate from user JWT. Backend validates both match the database record via `verify_agent_auth_token()` dependency in `backend/app/api/routes/knowledge.py`
- **Access filtering**: `backend/app/services/vector_search_service.py:get_accessible_source_ids()` enforces ownership, enablement, status, and workspace constraints
- **Article access**: Retrieval step validates all requested articles belong to accessible sources (403 if not)
- **SSH key handling**: Decrypted in-memory only, temp files `0o600`, cleanup in `finally`
- **Discovery opt-in**: Users must explicitly enable discoverable sources before agents can access them

## Related Aspect Docs

- [Agent-Env Knowledge Tool](../../agents/agent_environment_core/knowledge_tool.md) - MCP tool implementation running inside agent Docker containers (building mode only)
