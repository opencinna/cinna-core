---
feature: knowledge_sources
domain: [application, knowledge]
one_liner: "Lets admins connect Git repositories of documentation that agents and users search semantically, with per-source access control."
docs:
  tech: knowledge_sources_tech.md
---
# Knowledge Sources

## Purpose

Allows platform administrators (superusers) to connect Git repositories containing structured documentation that agents can query during sessions. Repositories are cloned, articles are extracted and indexed with vector embeddings, and agents use a two-step discovery/retrieval flow to find relevant knowledge via semantic search.

Knowledge sources are **server-wide, admin-managed resources**: every superuser can manage every source (create, edit, delete, enable/disable, check access, refresh, preview articles, export). Who may *query* a source's content (via agents, or via the CLI's knowledge search) is a separate, per-source setting: **access level**.

## Core Concepts

- **Knowledge Source** - A Git repository configuration pointing to a documentation repo. Has a status lifecycle and an access level. Managed by every superuser equally; `user_id` records who created it, nothing more
- **Access Level** - Who may *query* a source: `private` (superusers only), `public` (every active user), or `shared` (superusers plus the users on the source's share list)
- **Article** - A single document parsed from the repository's `.ai-knowledge/settings.json` manifest, stored with content and metadata
- **Article Chunk** - A segment of an article (default 1000 chars, 10% overlap) with a vector embedding for semantic search
- **Check Access** - Lightweight verification (`git ls-remote`) that the repository is reachable without cloning
- **Refresh Knowledge** - Full clone + parse + upsert + embed operation that syncs articles from the repository
- **Content Hash** - SHA256 hash of article content used for change detection (skips unchanged articles on refresh)

## Access Control Model

Knowledge Sources is an **admin-only feature**. Only superusers can call any endpoint under `/api/v1/knowledge-sources`; non-superusers receive 403.

Within that, every superuser has full read/write access to **every** source — there is no per-source ownership boundary among admins. Create, edit, delete, enable/disable, check-access, refresh, list/preview articles, and export all work the same for any superuser on any source. `user_id` on a source is metadata (who created it, shown as "Created by" in the UI) and plays no role in authorization.

Regular users do not see Knowledge Sources in the UI and cannot call its API endpoints. Their relationship to a source is entirely mediated by **access level** (below) through the consumer paths: the agent knowledge query tool, the per-agent CLI knowledge search, and the account CLI knowledge search.

The sidebar entry lives inside the **Admin** dropdown (between Users and Plugin Marketplaces), not in the main navigation.

## User Stories / Flows

### Create and Connect a Source (Admin)

1. Admin navigates to Knowledge Sources via the Admin dropdown menu
2. Clicks "Add Source", fills in name, Git URL, branch, optional SSH key, and picks an initial access level (Private / Public / Shared)
3. If an SSH key is selected, it must be one of the admin's own keys (400 otherwise) — the key is later resolved by its stored id, independent of who is operating on the source
4. Source is created with status `pending`
5. Admin clicks "Check Access" — system runs `git ls-remote` to verify connectivity
6. On success, status transitions to `connected`
7. Admin clicks "Refresh Knowledge" — system clones repo, parses articles, generates embeddings
8. Articles appear in the Articles tab with titles, descriptions, tags, and features
9. For a Shared source, the admin adds specific users to the share list from the source's Configuration tab (Access card) after creation

### Update or Remove a Source (Any Admin)

1. Any admin edits source settings (name, description, branch, SSH key) via the Edit button in the source detail header
2. Attaching or changing the SSH key requires the key to belong to the acting admin (400 otherwise) — this holds even when editing a source someone else created
3. Changing branch or SSH key resets status to `pending` (re-verification needed)
4. Git URL cannot be changed after creation (admin must delete and recreate)
5. Deleting a source cascades to its articles and share-list entries. Deleting the *creator's user account* does not delete the source — `user_id` is set to `NULL` and the source keeps running

### Setting a Source's Access Level (Any Admin)

1. Admin opens the source detail page, Configuration tab, Access card
2. Picks **Private** (superusers only), **Public** (every active user), or **Shared** (superusers plus a specific user list)
3. Choosing Shared reveals a user picker (`UserAllowlistPicker`) to add/remove individual users
4. The list page's Access column and the detail page's Access card both show the current level; a Shared source also shows the shared-user count

### Previewing an Article / Exporting a Source (Any Admin)

1. Any admin opens a source detail page and navigates to the Articles tab; clicking a row opens a Dialog with the full article rendered as Markdown
2. Any admin can export a source as a single Markdown file (`.md` file named `knowledge-source-{source_id}.md`) via the page header's ellipsis menu
3. Both operations work identically regardless of who created the source or its access level — read access here is the same server-wide admin access as everything else on this router

### Agent Knowledge Query (Two-Step)

Agents in isolated Docker environments use a **reverse API call pattern** — the agent environment calls back to the main backend to query knowledge. This is implemented as an MCP tool (`mcp__knowledge__query_integration_knowledge`) that is **only available in building mode**.

1. Agent uses the `query_integration_knowledge` tool with a query string (Step 1: Discovery)
2. Agent environment makes an authenticated HTTP call to the backend (`POST /api/v1/knowledge/query`)
3. Backend resolves the agent's owner, generates a query embedding, and searches chunks by cosine similarity across the sources accessible to that owner (see Business Rules below)
4. Returns top matching articles with metadata (title, description, tags, source name)
5. Agent selects relevant articles and requests full content (Step 2: Retrieval)
6. System re-checks access against the same rule and returns full article content

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
- Branch/SSH key changed -> `pending`

### Query Access Rule (Consumers)

This single rule — implemented once in `knowledge_access_service` — governs every consumer path: the agent knowledge query tool, per-agent CLI knowledge search, and account CLI knowledge search. A user may query a source when:

- the source is `is_enabled=true` AND `status=connected`, AND
- the querying user is **active**, AND
- the source is **public**, OR the querying user is a **superuser**, OR (the source is **shared** AND the querying user is on its share list **or** belongs to a [user group](../user_groups/user_groups.md) on it; membership is live)

For agent-driven queries, "the querying user" is resolved as the **agent's owner** — there is no separate agent-level or workspace-level scoping. Inactive users resolve to no accessible sources.

### SSH Key Ownership and Deletion

- Attaching an SSH key to a source (on create or update) requires the key to belong to the **acting admin** — any admin can be refused another admin's key (400), even though they could freely edit every other field on the source
- Once attached, the key is resolved and decrypted **on behalf of its own owner**, not the acting admin, so any superuser's check-access/refresh works regardless of who attached the key
- Deleting an SSH key that is still attached to any knowledge source is rejected (409) rather than silently disconnecting the source — see [SSH Keys](../ssh_keys/ssh_keys.md)
- If a key's owning user account is deleted, the account's SSH keys cascade-delete, and the source's `ssh_key_id` is set to `NULL` (the source itself is not deleted); a subsequent check-access/refresh fails because no key is attached

### Refresh Logic

- Only enabled sources can be refreshed (disabled sources return an error response, not an exception)
- Shallow clone (depth=1) to minimize bandwidth
- Articles identified by `(git_repo_id, file_path)` unique constraint
- Content hash comparison skips unchanged articles (optimization)
- Orphaned articles (removed from `settings.json`) are deleted
- Embeddings regenerated only for new or updated articles
- Source metadata updated: `last_sync_at`, `sync_commit_hash`, `status_message` with statistics

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

Admin management path (any superuser, any source):
```
Admin --> Frontend (Knowledge Sources page, Admin dropdown) --> Backend API (/api/v1/knowledge-sources)
                                                                      |
                                                              KnowledgeSourceService
                                                                   |        |
                                                         GitOperations    KnowledgeArticleService
                                                         (clone/verify)    (parse/upsert/hash)
                                                              |                    |
                                                        SSHKeyService        EmbeddingService
                                                        (decrypt owner's key) (Google Gemini)
                                                              |                    |
                                                         Temp SSH files      VectorSearchService
                                                                             (cosine similarity)
                                                                                   |
                                                                              PostgreSQL
                                                                       (sources, articles, chunks,
                                                                        user shares)
```

Agent query path (reverse API call from Docker container to backend):
```
Agent (building mode)
  --> MCP tool: query_integration_knowledge
    --> Agent-Env HTTP POST /api/v1/knowledge/query
          (Authorization: Bearer <env_token> + X-Agent-Env-Id header)
            --> Backend resolves agent owner
              --> knowledge_access_service (accessible source ids)
                --> EmbeddingService (query embedding)
                  --> VectorSearchService (cosine similarity)
                    --> Article retrieval with access re-check
```

## Integration Points

- **SSH Keys** - Private Git repositories use SSH keys for authentication. A key still attached to a source cannot be deleted (409); attaching/changing a key on a source requires it to belong to the acting admin. See [SSH Keys](../ssh_keys/ssh_keys.md)
- **Agent Environments** - Agents query knowledge via the `/api/v1/knowledge/query` endpoint, authenticated with environment tokens. The knowledge MCP tool is registered only in building mode. See [Agent Environment Core](../../agents/agent_environment_core/agent_environment_core.md)
- **Agent Environment Lifecycle** - `BACKEND_URL`, `AGENT_AUTH_TOKEN`, and `ENV_ID` environment variables are injected into the Docker container's `.env` file during creation/rebuild. See [Agent Environments](../../agents/agent_environments/agent_environments.md)
- **Cinna CLI Integration** - Per-agent and account-level CLI knowledge search both resolve access through the same central rule. See [Cinna CLI Integration](../cinna_cli_integration/cinna_cli_integration.md) and [Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md) (flow 6b)
- **Google Gemini API** - Used for generating embeddings (`gemini-embedding-001` model, 768 dimensions)
- **Admin Panel** - Knowledge Sources is accessible through the Admin dropdown in the sidebar, alongside Users and Plugin Marketplaces
- **Pre-Allowed Tools** - The knowledge query tool is in the pre-allowed list, meaning agents use it without per-call user approval

## Security

- SSH keys decrypted only during Git operations, stored as temp files with `0o600`, cleaned up in `finally` blocks
- All API endpoints require superuser status (`get_current_active_superuser` dependency) — non-admin requests are rejected with 403. Among superusers there is no further authorization boundary: any superuser manages any source
- Agent knowledge endpoint uses **two-factor header auth**: `Authorization: Bearer <env_token>` + `X-Agent-Env-Id` header. Backend validates both against the database record, preventing token reuse across environments
- Query access is enforced once, centrally, in `knowledge_access_service.accessible_sources_filter()` / `get_accessible_source_ids()` — every consumer (agent query, per-agent CLI search, account CLI search) goes through it
- Repository access errors never expose SSH key contents in responses or logs
- `check_access` and `refresh` return 404 (not a generic error) when the source id does not exist, matching the rest of the router's 404-on-missing convention

## Troubleshooting

- **Knowledge tool not available to agent**: Verify the session is in building mode (tool is not registered in conversation mode). Rebuild the environment to get the latest tool definitions. Check agent-env logs for tool import errors
- **Authentication failures**: Verify `ENV_ID` matches the database record. Verify `AGENT_AUTH_TOKEN` matches `environment.config["auth_token"]`. Check backend logs for specific auth failure reason
- **Connection errors from agent-env**: Verify the agent container is on the `agent-bridge` Docker network. Check that the backend service is running
- **Non-admin user cannot access page**: Knowledge Sources is admin-only. The sidebar entry is inside the Admin dropdown and is only visible to superusers
- **Can't delete an SSH key**: The API returns 409 if the key is still attached to one or more knowledge sources; check or change the SSH key on those sources first
- **Can't attach an SSH key to a source**: The key must belong to the admin performing the create/update (400 otherwise) — pick one of your own keys, or ask its owner to attach it

### Sharing with Groups (Admin)

When a source is `shared`, the access card lets an admin add user groups next to individual users (`/knowledge-sources/{id}/shared-groups`, superuser only, idempotent). A group share only takes effect while the access level is `shared`. Removing a member or the group excludes the source from that user's next query; nothing is materialised.

## Changelog

- Shared sources can be shared with user groups.
