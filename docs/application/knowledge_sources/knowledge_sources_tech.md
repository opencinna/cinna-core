# Knowledge Sources - Technical Details

## File Locations

### Backend

- **Models**: `backend/app/models/knowledge/knowledge.py` - `AIKnowledgeGitRepo`, `AIKnowledgeGitRepoUserShare`, `KnowledgeArticle`, `KnowledgeArticleChunk`, enums (`SourceStatus`, `KnowledgeSourceAccessLevel`), request/response schemas
- **Routes (admin CRUD)**: `backend/app/api/routes/knowledge_sources.py` - Source management, articles, share-list endpoints. All endpoints use `SuperUser` dependency
- **Routes (agent query)**: `backend/app/api/routes/knowledge.py` - Knowledge query endpoint for agents
- **Source service**: `backend/app/services/knowledge/knowledge_source_service.py` - CRUD, check-access, refresh, share-list management
- **Access service**: `backend/app/services/knowledge/knowledge_access_service.py` - Single source of truth for consumer (query) access resolution
- **Article service**: `backend/app/services/knowledge/knowledge_article_service.py` - Article parsing, upserting, content hashing, embedding orchestration
- **Git operations**: `backend/app/services/knowledge/git_operations.py` - Clone, verify, SSH key file management, URL conversion
- **Embedding service**: `backend/app/services/knowledge/embedding_service.py` - Google Gemini embeddings, text chunking
- **Vector search**: `backend/app/services/knowledge/vector_search_service.py` - Cosine similarity and article retrieval; re-exports `get_accessible_source_ids` from the access service

### Migrations

- `backend/app/alembic/versions/240176144d01_add_knowledge_management_tables.py` - Core tables (git_repo, workspaces, articles) — superseded by the migration below
- `backend/app/alembic/versions/f8a9c3d1e4b2_add_knowledge_article_chunks_table.py` - Article chunks for semantic search
- `backend/app/alembic/versions/a7f55e82a224_knowledge_source_access_level_and_user_.py` - Replaces `public_discovery` with `access_level` (private/public/shared, backfilled from `public_discovery`); adds `ai_knowledge_git_repo_user_shares`; drops workspace scoping (`workspace_access_type` + `ai_knowledge_git_repo_workspaces`) and the dead `user_enabled_discoverable_sources` table; makes `user_id` nullable with `ON DELETE SET NULL`; changes `ssh_key_id` FK to `ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED` (deferred because deleting an admin who is both a source's creator and its key's owner touches the row via two cascade paths)

### Agent Environment (Docker Container)

- **Knowledge query tool**: `backend/app/env-templates/app_core_base/core/server/tools/knowledge_query.py` - MCP tool `query_integration_knowledge`, two-step discovery/retrieval, reads `BACKEND_URL`/`AGENT_AUTH_TOKEN`/`ENV_ID` env vars, UUID validation, error handling
- **Claude Code adapter**: `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py` - Registers knowledge MCP server in building mode only: `create_sdk_mcp_server(name="knowledge", tools=[query_integration_knowledge])` → tool name `mcp__knowledge__query_integration_knowledge`
- **Tools package**: `backend/app/env-templates/app_core_base/core/server/tools/__init__.py`

### Frontend

- **Sources list page**: `frontend/src/routes/_layout/knowledge-sources.tsx` - Single table of every source with Name, Status, Access, Created by, Articles, Last Sync
- **Source detail page**: `frontend/src/routes/_layout/knowledge-source/$sourceId.tsx` - Tabs for configuration and articles
- **Add modal**: `frontend/src/components/KnowledgeSources/AddSourceModal.tsx` - Create source with SSH key and access-level radio group
- **Edit modal**: `frontend/src/components/KnowledgeSources/EditSourceModal.tsx` - Update name, description, branch, SSH key. Git URL is read-only. No access-level control here (lives in the Configuration tab's Access card)
- **Access level UI helpers**: `frontend/src/components/KnowledgeSources/accessLevel.tsx` - `ACCESS_LEVEL_OPTIONS`, `AccessLevelBadge`, `AccessLevelRadioGroup` (shared by the list page, Add modal, and Access card)
- **Access card**: `frontend/src/components/KnowledgeSources/KnowledgeSourceAccessCard.tsx` - Access-level radio group + `UserAllowlistPicker` share-list management (shown only for `shared`), rendered in the Configuration tab below the main config card
- **Configuration tab**: `frontend/src/components/KnowledgeSources/KnowledgeSourceConfigurationTab.tsx` - Status display, check access, refresh, Enabled toggle, Created-by field. Renders `KnowledgeSourceAccessCard` as a second card
- **Articles tab**: `frontend/src/components/KnowledgeSources/KnowledgeSourceArticlesTab.tsx` - Article list with tags and features; article preview dialog (`sm:max-w-4xl`, `max-h-[85vh]`, scrollable)
- **Admin sidebar menu**: `frontend/src/components/Sidebar/AdminMenu.tsx` - Knowledge Sources entry (BookOpen icon) between Users and Plugin Marketplaces in the Admin dropdown
- **Knowledge tool render**: `frontend/src/components/Chat/ToolCallBlock.tsx` - Detects `mcp__knowledge__query_integration_knowledge` tool calls, renders via `KnowledgeQueryToolBlock` component
- **API client**: `frontend/src/client/sdk.gen.ts` - `KnowledgeSourcesService`

## Database Schema

### Table: `ai_knowledge_git_repo`

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| user_id | UUID | FK -> user(id) ON DELETE SET NULL, nullable, indexed — creator only, not an authorization scope |
| name | VARCHAR | NOT NULL, indexed |
| description | TEXT | nullable |
| git_url | VARCHAR | NOT NULL |
| branch | VARCHAR | NOT NULL, default "main" |
| ssh_key_id | UUID | FK -> user_ssh_keys(id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED, nullable |
| is_enabled | BOOLEAN | NOT NULL, default true, indexed |
| status | VARCHAR | NOT NULL, default "pending", indexed (enum: pending/connected/error/disconnected) |
| status_message | TEXT | nullable |
| last_checked_at | DATETIME | nullable |
| last_sync_at | DATETIME | nullable |
| sync_commit_hash | VARCHAR | nullable |
| access_level | VARCHAR | NOT NULL, default "private", indexed (enum: private/public/shared) |
| created_at | DATETIME | NOT NULL |
| updated_at | DATETIME | NOT NULL |

### Table: `ai_knowledge_git_repo_user_shares`

Link table: users granted query access to a `shared` source.

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| git_repo_id | UUID | FK -> ai_knowledge_git_repo(id) ON DELETE CASCADE, indexed |
| user_id | UUID | FK -> user(id) ON DELETE CASCADE, indexed |
| created_at | DATETIME | NOT NULL |

Unique: `uq_knowledge_repo_user_share` on `(git_repo_id, user_id)`

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

### Removed tables (migration `a7f55e82a224`)

- `ai_knowledge_git_repo_workspaces` — workspace-scoping link table; scoped a source to the creating admin's own workspaces, which had no meaning once sources became server-wide
- `user_enabled_discoverable_sources` — per-user opt-in table for discoverable public sources; already dead code before this change, dropped entirely

## Environment Variables (Agent Container)

Injected into the agent container's `.env` file by `backend/app/services/environments/environment_lifecycle.py:_generate_env_file()`:

| Variable | Value | Purpose |
|----------|-------|---------|
| `BACKEND_URL` | `http://backend:8000` | Backend API URL via Docker network service name |
| `AGENT_AUTH_TOKEN` | Generated UUID | Bearer token for knowledge query auth |
| `ENV_ID` | Environment UUID | Identifies the agent environment |

## Pre-Allowed Tools

`backend/app/services/sessions/message_service.py` - `mcp__knowledge__query_integration_knowledge` is in the pre-allowed tools list, meaning agents can invoke it without per-call user approval. Other pre-allowed tools: the Cinna Task Protocol's `mcp__cinna__tasks_*` / `cinna_tasks_*` (plus the deprecated `agent_task` names, kept for environments built before the protocol).

## API Endpoints

### Admin Source Management

**Route file**: `backend/app/api/routes/knowledge_sources.py`
**Prefix**: `/api/v1/knowledge-sources` | **Tag**: `knowledge-sources`
**Auth**: All endpoints require `SuperUser` (`get_current_active_superuser`). Non-superusers receive 403. There is no further per-source ownership check — any superuser can act on any source.

Route-level dependency declaration:
```python
SuperUser = Annotated[User, Depends(get_current_active_superuser)]
```

| Method | Path | Description | Request | Response |
|--------|------|-------------|---------|----------|
| GET | `/` | List all sources on the server | `?skip&limit` | `list[AIKnowledgeGitRepoPublic]` |
| POST | `/` | Create source | `AIKnowledgeGitRepoCreate` | `AIKnowledgeGitRepoPublic`, 400 if `ssh_key_id` isn't the creator's own |
| GET | `/{source_id}` | Get source by ID | - | `AIKnowledgeGitRepoPublic`, 404 |
| PUT | `/{source_id}` | Update source | `AIKnowledgeGitRepoUpdate` | `AIKnowledgeGitRepoPublic`, 400 if a newly attached `ssh_key_id` isn't the acting admin's own, 404 |
| DELETE | `/{source_id}` | Delete source (cascades to articles + shares) | - | `{"ok": true}`, 404 |
| POST | `/{source_id}/enable` | Enable source | - | `AIKnowledgeGitRepoPublic`, 404 |
| POST | `/{source_id}/disable` | Disable source | - | `AIKnowledgeGitRepoPublic`, 404 |
| POST | `/{source_id}/check-access` | Verify Git access (ls-remote) | - | `CheckAccessResponse`, 404 |
| POST | `/{source_id}/refresh` | Clone + parse + embed articles | - | `RefreshKnowledgeResponse`, 404 |
| GET | `/{source_id}/articles` | List articles | `?skip&limit` | `list[KnowledgeArticlePublic]` |
| GET | `/{source_id}/articles/{article_id}` | Get full article content | - | `KnowledgeArticleDetail`, 404 |
| GET | `/{source_id}/export` | Export source as Markdown download | - | `text/markdown` (file download), 404 |
| GET | `/{source_id}/shared-users` | List the source's share list | - | `list[KnowledgeSourceSharedUserPublic]`, 404 |
| POST | `/{source_id}/shared-users` | Add a user to the share list (idempotent) | `KnowledgeSourceShareCreate {user_id}` | `KnowledgeSourceSharedUserPublic`, 404 if source or user missing |
| DELETE | `/{source_id}/shared-users/{user_id}` | Remove a user from the share list | - | `{"ok": true}`, 404 if source missing or user not on the list |

`AIKnowledgeGitRepoPublic` includes `user_id` (creator, nullable), `created_by_email`/`created_by_name` (looked up creator info, `None` once the creator is deleted), `article_count`, and `shared_user_count`.

`KnowledgeArticleDetail` extends `KnowledgeArticlePublic` with `content` (full Markdown body) and `commit_hash` (Git commit that last wrote the article).

The route layer maps `KnowledgeSourceNotFoundError` → 404 and `KnowledgeSourceValidationError` → 400 wherever the service can raise them; endpoints without a `try/except` simply return `None`/falsy and the route raises 404 directly.

### Agent Knowledge Query

**Route file**: `backend/app/api/routes/knowledge.py`
**Prefix**: `/api/v1/knowledge`

| Method | Path | Description | Auth | Response |
|--------|------|-------------|------|----------|
| POST | `/query` | Two-step knowledge query | `Authorization: Bearer <env_token>` + `X-Agent-Env-Id` header | `KnowledgeQueryResponseDiscovery` or `KnowledgeQueryResponseRetrieval` |

Request body: `{ "query": "string", "article_ids": ["uuid"] }` - omit `article_ids` for discovery step, include for retrieval step. The route resolves `user_id = agent.owner_id` from the authenticated environment context (`ctx.agent`) and passes it straight into `get_accessible_source_ids` / `search_knowledge` — no workspace parameter exists anywhere on this path.

## Services & Key Methods

### `backend/app/services/knowledge/knowledge_source_service.py`

Module-level exceptions: `KnowledgeSourceError` (base), `KnowledgeSourceNotFoundError`, `KnowledgeSourceValidationError`.

| Function | Purpose |
|--------|---------|
| `create_source(session, user_id, data)` | Creates source with `pending` status; `user_id` becomes the creator. Raises `KnowledgeSourceValidationError` if `data.ssh_key_id` isn't the creator's own key |
| `list_sources(session, skip, limit)` | Lists every source on the server, ordered by name, with computed `article_count`/`shared_user_count`/creator info |
| `get_source_by_id(session, source_id)` | Gets a source by id, or `None` — no ownership parameter |
| `update_source(session, source_id, acting_user_id, data)` | Updates fields, resets status to `pending` if branch or SSH key changed. Raises `KnowledgeSourceValidationError` if a newly attached `ssh_key_id` isn't `acting_user_id`'s own key |
| `delete_source(session, source_id)` | Deletes source (cascades to articles and shares) |
| `enable_source(session, source_id)` / `disable_source(session, source_id)` | Toggle `is_enabled` |
| `check_access(session, source_id)` | Decrypts the source's stored SSH key (via `_load_source_ssh_key`, resolved by the key's own owner), runs `git ls-remote`, updates status. Raises `KnowledgeSourceNotFoundError` |
| `refresh_knowledge(session, source_id)` | Full clone + parse + embed workflow; returns an error response (not an exception) if the source is disabled. Raises `KnowledgeSourceNotFoundError` |
| `get_source_articles(session, source_id, skip, limit)` | Lists articles for a source |
| `get_article_content(session, source_id, article_id)` | Returns a `KnowledgeArticleDetail`, or `None` if the source or article doesn't exist |
| `export_source_markdown(session, source_id)` | Concatenates all articles for a source into one Markdown string (source header + per-article `##` section ordered by `file_path`); `None` if source missing, header-only document if it has no articles |
| `list_shared_users(session, source_id)` | Lists users on the share list ordered by email. Raises `KnowledgeSourceNotFoundError` |
| `add_shared_user(session, source_id, user_id)` | Idempotent add to the share list. Raises `KnowledgeSourceNotFoundError` if the source or the user doesn't exist |
| `remove_shared_user(session, source_id, user_id)` | Removes a user from the share list. Raises `KnowledgeSourceNotFoundError` if the source is missing or the user isn't on the list |
| `_require_source(session, source_id)` | Internal — `session.get` + raise `KnowledgeSourceNotFoundError` |
| `_validate_ssh_key_ownership(session, ssh_key_id, acting_user_id)` | Internal — raises `KnowledgeSourceValidationError` unless `SSHKeyService.get_key_by_id` finds the key under `acting_user_id` |
| `_load_source_ssh_key(session, source)` | Internal — resolves `source.ssh_key_id` to a `UserSSHKey` row and decrypts it via `SSHKeyService.get_decrypted_private_key(user_id=key.user_id)` (the key's own owner, not the caller) |
| `_to_public(...)` / `_to_public_list(...)` | Internal — batch-build `AIKnowledgeGitRepoPublic` rows with article/share counts and creator email/name in one pass (avoids N+1 queries) |

Removed functions (no longer exist): `get_user_sources`, `get_discoverable_sources`, `enable_discoverable_source`, `disable_discoverable_source`, `get_user_enabled_discoverable_source_ids`.

### `backend/app/services/knowledge/knowledge_access_service.py` - Access Control (single source of truth)

| Function | Purpose |
|--------|---------|
| `accessible_sources_filter(user)` | Returns a SQLAlchemy `ColumnElement[bool]` predicate on `AIKnowledgeGitRepo`: `false()` if `user.is_active` is `False`; otherwise `is_enabled AND status=connected AND (access_level=public OR user.is_superuser OR (access_level=shared AND AIKnowledgeGitRepoUserShare exists for this user)`. Superusers get an unconditional `true()` level clause (see every source regardless of level, still gated by enabled+connected) |
| `get_accessible_source_ids(session, user_id)` | Looks up the user, returns `[]` for unknown/inactive users, otherwise runs `accessible_sources_filter` and returns matching source ids. This is the function every consumer path calls |

`backend/app/services/knowledge/vector_search_service.py` re-exports `get_accessible_source_ids` from this module (`# noqa: F401`) so existing `from app.services.knowledge.vector_search_service import get_accessible_source_ids` call sites keep working.

### `backend/app/services/knowledge/vector_search_service.py`

| Method | Purpose |
|--------|---------|
| `cosine_similarity(vec1, vec2)` | Dot product / magnitude |
| `search_article_chunks(...)` | Chunk-level cosine search |
| `search_knowledge(session, query_embedding, user_id, embedding_model, limit=10)` | Calls `get_accessible_source_ids(session=session, user_id=user_id)` (agent owner for agent-driven queries), then ranks chunks. No `workspace_id` parameter |
| `get_articles_by_ids(...)` | Retrieval-step article lookup, re-checked against `get_accessible_source_ids` |

### `backend/app/services/knowledge/knowledge_article_service.py`

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

### `backend/app/services/knowledge/git_operations.py`

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

### `backend/app/services/knowledge/embedding_service.py`

| Method | Purpose |
|--------|---------|
| `chunk_text(text, chunk_size, overlap_percent)` | Splits text at sentence/word boundaries, 1000 char chunks, 10% overlap |
| `generate_embedding(text, model)` | Single text embedding via Google Gemini |
| `generate_embeddings_batch(texts, model)` | Batch embedding (up to 100 per API call) |
| `generate_query_embedding(query, model)` | Query embedding for search |
| `prepare_article_for_embedding(title, description, content)` | Combines fields: "Title: ...\n\nDescription: ...\n\nContent: ..." |

Default config: model `gemini-embedding-001`, 768 dimensions, 1000 char chunks, 10% overlap, batch size 100

## Frontend Components

### `frontend/src/routes/_layout/knowledge-sources.tsx`

- Single "Knowledge Sources" card listing every source on the server (`KnowledgeSourcesList`, renamed from `MyKnowledgeSourcesList`) — no separate Discoverable Sources section
- Table columns: Name, Status, **Access**, **Created by**, Articles, Last Sync
  - Access column renders `AccessLevelBadge` (Private/Public/Shared, with a share-user count suffix when shared)
  - Created by shows `created_by_name || created_by_email || "Unknown"`, truncated at `max-w-[14rem]`
  - Last Sync uses the shared `RelativeTime` component (was raw `Date#toLocaleDateString`)
- Disabled sources render at 60% opacity

### `frontend/src/routes/_layout/knowledge-source/$sourceId.tsx`

- Source detail page with two tabs (Configuration, Articles)
- Header with source name and a vertical-ellipsis dropdown menu containing three items: **Export as Markdown** (Download icon), **Edit Source** (Edit icon), **Delete Source** (Trash icon, destructive style)
- Back navigation to sources list
- **Export as Markdown**: uses a raw authenticated `fetch` + blob URL download (not the generated SDK client, which cannot stream file downloads). Reads the JWT from `localStorage["access_token"]`, sends `Authorization: Bearer` header. Filename resolved from the `Content-Disposition` response header; falls back to `knowledge-source-{sourceId}.md`

### `frontend/src/components/KnowledgeSources/AddSourceModal.tsx`

- Fields: name, description, Git URL, branch, SSH key dropdown, `AccessLevelRadioGroup` (default `private`)
- No workspace selection (removed along with `workspace_access_type`/`workspace_ids`)
- A helper line under the radio group tells the admin to add specific users on the source page after creation for Shared
- After creation, allows immediate check-access

### `frontend/src/components/KnowledgeSources/EditSourceModal.tsx`

- Fields: name, description, branch, SSH key. Git URL is read-only with explanation text
- No access-level control here — that lives in the Configuration tab's Access card

### `frontend/src/components/KnowledgeSources/accessLevel.tsx`

- `ACCESS_LEVEL_OPTIONS`: `{value, label, description, icon}` for `private` (Lock, "Admins only"), `public` (Globe, "Every user on the server"), `shared` (Users, "Selected users and admins")
- `AccessLevelBadge({level, sharedUserCount})` — outline `Badge` with the option's icon/label; appends `· N user(s)` when `level="shared"` and a count is passed
- `AccessLevelRadioGroup({value, onChange, disabled, idPrefix})` — shared radio group used by the list page's mental model, the Add modal, and the Access card

### `frontend/src/components/KnowledgeSources/KnowledgeSourceAccessCard.tsx`

- `Card` titled "Access" (Shield icon) rendered below the main Configuration card
- `AccessLevelRadioGroup` bound to `PUT /{source_id}` with `{access_level}`; on change, invalidates `["knowledge-source", sourceId]` and `["knowledge-sources"]`
- When `access_level === "shared"`, renders a `UserAllowlistPicker` fed by `GET /{source_id}/shared-users` (query key `["knowledge-source", sourceId, "shared-users"]`, `enabled: isShared`)
  - Add: `POST /{source_id}/shared-users {user_id}` → toast "Shared with {name|email}"
  - Remove: `DELETE /{source_id}/shared-users/{user_id}` → toast "User removed from the share list"
  - Both mutations invalidate the shared-users key plus the source/list keys

### `frontend/src/components/KnowledgeSources/KnowledgeSourceConfigurationTab.tsx`

- Now returns a `space-y-6` stack of two cards instead of one
- Main card: Git URL (monospace), branch, SSH key status, connection status badge, Last Sync (via `RelativeTime`), and a new **Created by** field
- Footer: only the **Enabled** toggle remains (the old Public toggle and its `togglePublicDiscoveryMutation` are gone — access level is now handled entirely by the Access card) + "Check Access" (shown when status is not `connected`) + "Refresh Knowledge" (disabled when source is not enabled)
- Second card: `KnowledgeSourceAccessCard`
- Mutation error handlers switched to the shared `handleError.bind(showErrorToast)` pattern

### `frontend/src/components/KnowledgeSources/KnowledgeSourceArticlesTab.tsx`

- Alert if source is disabled (directs to Configuration tab)
- Table columns: **Title** (~30% width) and **Description** (~70% width), `table-fixed w-full`, wraps instead of horizontal-scrolling
- Description column shows text truncated to 2 lines (`line-clamp-2`) plus tags (first 3 + overflow badge) and features (first 2 + overflow badge)
- Each row is clickable, opening a Dialog with the full article content
- **Article preview Dialog**: widened to `sm:max-w-4xl` (was `max-w-3xl`) and `max-h-[85vh]` (was `80vh`), `overflow-y-auto overflow-x-hidden` content area with `break-words` on both the title and the `MarkdownViewer` body — fixes long unbroken tokens (URLs, identifiers) forcing horizontal scroll
- Loading skeleton (4 lines) while the content is fetching; error message if the fetch fails
- Article content fetched on-demand via `GET /{source_id}/articles/{article_id}`, query key `["knowledge-article", sourceId, selectedArticleId]`, `enabled: !!selectedArticleId`

### `frontend/src/components/Sidebar/AdminMenu.tsx`

- Admin dropdown menu containing: Users, **Knowledge Sources**, Plugin Marketplaces
- Knowledge Sources entry uses `BookOpen` icon, links to `/knowledge-sources`
- Sidebar button shows active state when current path is `/knowledge-sources` or starts with `/knowledge-source/`

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

- **Superuser-only**: All routes use `SuperUser = Annotated[User, Depends(get_current_active_superuser)]`. FastAPI returns 403 for non-superuser requests before the handler runs
- **No inter-admin boundary on management**: every write/read operation in `knowledge_source_service` takes `source_id` (and, for SSH-key validation, `acting_user_id`) but never an ownership-gating `user_id` — any superuser reaches any source
- **SSH key ownership is the one exception**: `_validate_ssh_key_ownership` enforces that attaching/changing a source's key requires the key to belong to the acting admin, via `SSHKeyService.get_key_by_id(session, ssh_key_id, acting_user_id)`. Refresh/check-access then decrypt that key on behalf of *its own* owner (`_load_source_ssh_key`), not the acting admin, so any superuser can operate the source after the key is attached
- **404 not 403 on missing source**: `check_access`, `refresh_knowledge`, article/export/share-list endpoints all treat a nonexistent `source_id` as 404, never a generic 500 or leak
- **Agent auth**: Knowledge query uses two-factor header-based auth (`Authorization: Bearer <env_token>` + `X-Agent-Env-Id`), separate from user JWT. Backend validates both match the database record via `verify_agent_auth_token()` dependency in `backend/app/api/routes/knowledge.py`
- **Query access filtering**: `knowledge_access_service.accessible_sources_filter()` is the single implementation of "who may query this source" — enabled + connected + (public OR superuser OR shared-with-user), gated on the querying user being active. Every consumer (agent query, per-agent CLI search, account CLI search) calls through `get_accessible_source_ids()`
- **Article access (agent retrieval)**: Retrieval step re-validates all requested articles belong to accessible sources (403 if not)
- **SSH key handling**: Decrypted in-memory only, temp files `0o600`, cleanup in `finally`
- **SSH key delete guard**: `SSHKeyService.delete_key` raises `SSHKeyInUseError` (mapped to 409) when any source still references the key, rather than relying on the FK's `ON DELETE SET NULL` to silently disconnect sources
- **Export download**: Implemented as a raw authenticated `fetch` on the frontend (not through the generated SDK). The JWT is read from `localStorage["access_token"]` and sent as an `Authorization: Bearer` header; the backend enforces the same superuser dependency as all other routes

## Related Aspect Docs

- [Agent-Env Knowledge Tool](../../agents/agent_environment_core/knowledge_tool.md) - MCP tool implementation running inside agent Docker containers (building mode only)
