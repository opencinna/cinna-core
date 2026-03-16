# `/files` and `/files-all` Commands

## Purpose

List workspace files with clickable links, giving users and A2A clients instant access to files produced by an agent without navigating the UI manually.

## Core Concepts

- **`/files`** — Shows only the `files` section of the workspace: user-facing data files (reports, CSVs, exports)
- **`/files-all`** — Shows all workspace sections: `files`, `scripts`, `logs`, `docs`, `uploads`
- **Workspace view token** — Short-lived JWT generated per command invocation for A2A clients; allows opening file links in a browser without user authentication
- **Context-aware links** — The same command produces different link formats based on the caller (UI user vs A2A client)

## User Stories / Flows

**UI user lists files:**
1. User types `/files` in the chat
2. Backend verifies the environment is running
3. Workspace tree is fetched from the agent environment
4. A markdown list is returned with clickable filename links (pointing to the frontend FileViewer route)
5. User clicks a link → opens the in-app FileViewer

**A2A client lists files:**
1. Client sends `/files` via `message/send`
2. Backend generates a workspace view token (1 hour, bound to the environment)
3. Markdown is returned with `[filename](backend-url?token=...)` links
4. Client embeds links in a UI or opens them directly in a browser — no authentication required

**Empty workspace:**
- Response: "No files found in workspace"

**Environment not running:**
- Response: error message indicating the environment is not active

## Business Rules

- Both commands share the same execution logic; they differ only in which workspace sections are included
- One workspace view token is generated per command invocation and reused across all links in the response
- UI links use the frontend FileViewer route; A2A links use the public shared workspace endpoint
- The presence of `access_token_id` in `CommandContext` is the discriminator for UI vs A2A context
- File sizes are displayed in human-readable format (bytes, KB, MB)
- Workspace tree is fetched via the existing `DockerAdapter.get_workspace_tree()` method — no new agent-env API needed

## Architecture Overview

```
/files command received
        │
        ▼
FilesCommandHandler.execute(context, args)
        │
        ├── Verify environment is running  (error if not)
        ├── adapter.get_workspace_tree()   (reuses existing DockerAdapter method)
        │
        ├── A2A context? (access_token_id present)
        │       YES → AgentWorkspaceTokenService.create_workspace_view_token()
        │       NO  → no token needed
        │
        ├── Collect files from requested sections only
        ├── Build markdown: filename links with human-readable sizes
        └── Return CommandResult(content=markdown)
```

## Link Formats

**UI context** (`access_token_id` is `None`):
```
{FRONTEND_HOST}/environment/{envId}/file?path={encoded_path}
```
Uses the frontend FileViewer route; user's browser session handles authentication.

**A2A context** (`access_token_id` is present):
```
{backend_base_url}/api/v1/shared/workspace/{env_id}/view/{path}?token={workspace_view_token}
```
Uses the public shared workspace endpoint. Token expires after 1 hour.

## Workspace View Tokens

Short-lived JWTs allowing A2A clients to open agent workspace file links in a browser without regular user authentication.

- **Token payload:** `type="workspace_view"`, `env_id`, `agent_id`, `exp` (now + 1 hour)
- **Signed with:** `settings.SECRET_KEY` using HS256
- **Validation:** self-contained (no DB lookup); `env_id` in URL must match token's `env_id`
- **Scope:** one token per command invocation, shared across all links in the response

## Integration Points

- **[Agent Environments](../agent_environments/agent_environments.md)** — Environment must be running; workspace tree fetched via `DockerAdapter`
- **[Agent File Management](../agent_file_management/agent_file_management.md)** — Public file endpoint (`shared_workspace.py`) serves file content using workspace view tokens
- **[A2A Protocol](../../application/a2a_integration/a2a_protocol/a2a_protocol.md)** — A2A callers receive completed tasks with the markdown file listing
