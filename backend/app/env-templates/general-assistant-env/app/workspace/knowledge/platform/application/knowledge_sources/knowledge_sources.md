# Knowledge Sources

## Purpose

Allows platform administrators (superusers) to connect Git repositories containing structured documentation that agents can query during sessions. Repositories are cloned, articles are extracted and indexed with vector embeddings, and agents use a two-step discovery/retrieval flow to find relevant knowledge via semantic search.

Public sources are automatically available to all users' agents — no per-user opt-in is required.

## Core Concepts

- **Knowledge Source** - A Git repository configuration pointing to a documentation repo. Has a status lifecycle and workspace access rules. Managed exclusively by superusers
- **Article** - A single document parsed from the repository's `.ai-knowledge/settings.json` manifest, stored with content and metadata
- **Article Chunk** - A segment of an article (default 1000 chars, 10% overlap) with a vector embedding for semantic search
- **Check Access** - Lightweight verification (`git ls-remote`) that the repository is reachable without cloning
- **Refresh Knowledge** - Full clone + parse + upsert + embed operation that syncs articles from the repository
- **Content Hash** - SHA256 hash of article content used for change detection (skips unchanged articles on refresh)
- **Public Discovery** - Flag that makes a source's articles automatically available to all users' agents, without any per-user opt-in

## Access Control Model

Knowledge Sources is an **admin-only feature**. Only superusers can:
- Create, edit, and delete knowledge sources
- Trigger check-access and refresh operations
- View the discoverable sources list from other admins

Regular users do not see Knowledge Sources in the UI and cannot call its API endpoints.

The sidebar entry lives inside the **Admin** dropdown (between Users and Plugin Marketplaces), not in the main navigation.

## User Stories / Flows

### Create and Connect a Source (Admin)

1. Admin navigates to Knowledge Sources via the Admin dropdown menu
2. Clicks "Add Source", fills in name, Git URL, branch, optional SSH key, workspace access
3. Source is created with status `pending`
4. Admin clicks "Check Access" — system runs `git ls-remote` to verify connectivity
5. On success, status transitions to `connected`
6. Admin clicks "Refresh Knowledge" — system clones repo, parses articles, generates embeddings
7. Articles appear in the Articles tab with titles, descriptions, tags, and features

### Update or Remove a Source (Admin)

1. Admin edits source settings (name, description, branch, SSH key, workspace access) via the Edit button in the source detail header
2. Changing branch or SSH key resets status to `pending` (re-verification needed)
3. Git URL cannot be changed after creation (admin must delete and recreate)
4. Deleting a source cascades to all articles and workspace permissions
5. Public discovery toggle and enabled toggle are in the Configuration tab footer (not in the edit modal)

### Making a Source Public or Private (Admin)

1. Admin opens the source detail page and goes to the Configuration tab
2. Toggles the **Public** switch in the Configuration tab footer
3. When public (`public_discovery=True`): all users' agents can query articles from this source automatically
4. When private: only the owning admin's agents can access the source
5. The Visibility column on the list page shows a **Public** badge (blue, with globe icon) or **Private** label for each source

### Viewing Other Admins' Public Sources (Admin)

1. Admin opens the Knowledge Sources page
2. The **Discoverable Sources** section at the bottom lists public sources from other admins
3. The list is read-only — no enable/disable controls — it exists for cross-admin visibility
4. These sources are automatically included in all users' queries; no action is required

### Agent Knowledge Query (Two-Step)

Agents in isolated Docker environments use a **reverse API call pattern** — the agent environment calls back to the main backend to query knowledge. This is implemented as an MCP tool (`mcp__knowledge__query_integration_knowledge`) that is **only available in building mode**.

1. Agent uses the `query_integration_knowledge` tool with a query string (Step 1: Discovery)
2. Agent environment makes an authenticated HTTP call to the backend (`POST /api/v1/knowledge/query`)
3. Backend validates environment token, generates a query embedding, searches chunks by cosine similarity across accessible sources
4. Returns top matching articles with metadata (title, description, tags, source name)
5. Agent selects relevant articles and requests full content (Step 2: Retrieval)
6. System validates access permissions and returns full article content

The knowledge tool is **pre-allowed** — agents can use it without requiring user approval for each call.

## Business Rules

### Status Lifecycle

- `pending` - Newly created, not yet verified. Initial state
- `connected` - Repository accessible, ready for refresh or already synced
- `error` - Verification or refresh failed (message stored in `status_message`)
- `disconnected` - Previously connected but lost access (e.g., SSH key deleted)

Transitions:
- Create -> `pending`
- Check Access success -> `connected`
- Check Access failure -> `error`
- SSH key deleted -> `disconnected`
- Branch/SSH key changed -> `pending`

### Workspace Access Control

- **All workspaces** (`all`) - Source available to agents in any of the owner's workspaces
- **Specific workspaces** (`specific`) - Source only available to agents in selected workspaces (managed via link table)

Workspace filtering applies only to the owning admin's sources. Public sources from other admins are not workspace-filtered when queried by agents.

### Refresh Logic

- Only enabled sources with `connected` status can be refreshed
- Shallow clone (depth=1) to minimize bandwidth
- Articles identified by `(git_repo_id, file_path)` unique constraint
- Content hash comparison skips unchanged articles (optimization)
- Orphaned articles (removed from `settings.json`) are deleted
- Embeddings regenerated only for new or updated articles
- Source metadata updated: `last_sync_at`, `sync_commit_hash`, `status_message` with statistics

### Agent Access Rules

The access model is simplified: no per-user opt-in table is consulted.

Agents can query knowledge from:
- Sources owned by the agent's owner, where `is_enabled=true` and `status=connected`
- Workspace filtering applied to own sources (if source has `specific` access type)
- All public sources (`public_discovery=True`, `is_enabled=true`, `status=connected`) from any admin — automatically, without any user enablement step

### Repository Format

Repositories must contain `.ai-knowledge/settings.json` at root:

```
my-docs-repo/
  .ai-knowledge/
    settings.json       # Required: defines articles
  articles/
    getting-started.md
    api-reference.md
```

Settings structure: `static_articles[]` array, each with `title`, `description`, `tags[]`, `features[]`, `path` (relative to repo root)

## Architecture Overview

Admin management path:
```
Admin --> Frontend (Knowledge Sources page, Admin dropdown) --> Backend API (/api/v1/knowledge-sources)
                                                                      |
                                                              KnowledgeSourceService
                                                                   |        |
                                                         GitOperations    KnowledgeArticleService
                                                         (clone/verify)    (parse/upsert/hash)
                                                              |                    |
                                                        SSHKeyService        EmbeddingService
                                                        (decrypt keys)      (Google Gemini)
                                                              |                    |
                                                         Temp SSH files      VectorSearchService
                                                                             (cosine similarity)
                                                                                   |
                                                                              PostgreSQL
                                                                       (sources, articles, chunks)
```

Agent query path (reverse API call from Docker container to backend):
```
Agent (building mode)
  --> MCP tool: query_integration_knowledge
    --> Agent-Env HTTP POST /api/v1/knowledge/query
          (Authorization: Bearer <env_token> + X-Agent-Env-Id header)
            --> Backend validates env token
              --> EmbeddingService (query embedding)
                --> VectorSearchService (cosine similarity)
                  --> Article retrieval with access control
```

## Integration Points

- **SSH Keys** - Private Git repositories use SSH keys for authentication. Deleting an SSH key disconnects associated sources. See [SSH Keys](../ssh_keys/ssh_keys.md)
- **Agent Environments** - Agents query knowledge via the `/api/v1/knowledge/query` endpoint, authenticated with environment tokens. The knowledge MCP tool is registered only in building mode. See [Agent Environment Core](../../agents/agent_environment_core/agent_environment_core.md)
- **Agent Environment Lifecycle** - `BACKEND_URL`, `AGENT_AUTH_TOKEN`, and `ENV_ID` environment variables are injected into the Docker container's `.env` file during creation/rebuild. See [Agent Environments](../../agents/agent_environments/agent_environments.md)
- **Workspaces** - Source access can be restricted to specific workspaces via the link table. See [User Workspaces](../user_workspaces/user_workspaces.md)
- **Google Gemini API** - Used for generating embeddings (`gemini-embedding-001` model, 768 dimensions)
- **Admin Panel** - Knowledge Sources is accessible through the Admin dropdown in the sidebar, alongside Users and Plugin Marketplaces
- **Pre-Allowed Tools** - The knowledge query tool is in the pre-allowed list, meaning agents use it without per-call user approval

## Security

- SSH keys decrypted only during Git operations, stored as temp files with `0o600`, cleaned up in `finally` blocks
- All API endpoints require superuser status (`get_current_active_superuser` dependency) — non-admin requests are rejected with 403
- Agent knowledge endpoint uses **two-factor header auth**: `Authorization: Bearer <env_token>` + `X-Agent-Env-Id` header. Backend validates both against the database record, preventing token reuse across environments
- Article access verified against source ownership and workspace permissions
- Public sources are automatically accessible to all users' agents — there is no mechanism for users to disable their own access to public sources
- Repository access errors never expose SSH key contents in responses or logs

## Troubleshooting

- **Knowledge tool not available to agent**: Verify the session is in building mode (tool is not registered in conversation mode). Rebuild the environment to get the latest tool definitions. Check agent-env logs for tool import errors
- **Authentication failures**: Verify `ENV_ID` matches the database record. Verify `AGENT_AUTH_TOKEN` matches `environment.config["auth_token"]`. Check backend logs for specific auth failure reason
- **Connection errors from agent-env**: Verify the agent container is on the `agent-bridge` Docker network. Check that the backend service is running
- **Non-admin user cannot access page**: Knowledge Sources is admin-only. The sidebar entry is inside the Admin dropdown and is only visible to superusers
