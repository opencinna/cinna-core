# Cinna CLI Integration

## Purpose

Enables local development of remote agents using the `cinna` CLI tool. Users develop agent scripts, prompts, and webapps using local editors and AI coding tools (Claude Code, opencode, Cursor), while the platform handles production execution — sessions, scheduling, triggers, and integrations.

## Core Concepts

| Concept | Description |
|---------|-------------|
| **Bootstrap Script** | Python script served by the platform at `GET /api/cli-setup/{token}`. Checks if `cinna` is installed — if yes, delegates to `cinna setup`; if no, prints install instructions |
| **Setup Token** | Short-lived (15min), single-use token embedded in the bootstrap URL. Generated from the UI, consumed when `cinna setup` exchanges it for a CLI token |
| **CLI Token** | Long-lived JWT (7-day rolling expiry) stored on the user's machine. Created by exchanging a setup token. Supports revocation from the UI |
| **Build Context** | Tarball containing the agent's Docker build context (Dockerfile, dependencies, app core) — used to replicate the production container locally |
| **Workspace Sync** | Push/pull mechanism to move workspace files between the local machine and the remote environment |
| **MCP Proxy** | Local MCP server (stdio) that forwards knowledge queries from local AI tools to the platform's knowledge search API |
| **Building Context** | The assembled building-mode prompt pulled from the env core — makes local AI tools behave like the platform's building agent |

## User Stories / Flows

### 1. Setting Up Local Development

1. User navigates to agent's **Integrations** tab
2. Clicks **Setup** in the Local Development card
3. Platform generates a setup token and displays a `curl | python3` oneliner with inline **Copy** and **Regenerate** buttons
4. User copies the command and runs it in their terminal
5. Platform serves a bootstrap script (`GET /api/cli-setup/{token}`) that checks if `cinna` CLI is installed:
   - **If installed**: runs `cinna setup <url>` which exchanges the token, downloads build context, builds container, clones workspace, configures MCP proxy
   - **If not installed**: prints install instructions (`uv tool install cinna-cli` or `pip install cinna-cli`) and exits
6. User opens the agent directory in their editor/AI tool and starts developing

### 2. Local Development Workflow

1. User edits scripts and prompts locally in their editor
2. Runs scripts via `cinna exec python scripts/main.py` (executes inside the local container)
3. Container has the exact same runtime as production — same Python packages, system deps, credentials
4. Local AI tools (Claude Code, Cursor) read `CLAUDE.md` and `BUILDING_AGENT.md` for agent context
5. MCP proxy provides `knowledge_query` tool for searching the agent's knowledge base

### 3. Syncing with Remote Environment

1. `cinna push` — uploads local workspace changes to the remote environment
2. After a successful push, the platform automatically resyncs agent prompts (workflow, entrypoint, refiner) from the environment back to the database — the same resync that runs when a building session ends
3. If pushed files changed the workflow prompt, side effects fire: A2A skills regeneration and background description update
4. `cinna pull` — downloads remote workspace changes + refreshes credentials and building context
5. Manifest-based diffing determines which files changed
6. Conflict detection: warns if both local and remote changed the same file

### 4. Managing Active Sessions (UI)

1. Integrations tab shows list of active CLI sessions (machine name, last used time)
2. User can **Disconnect** a session — revokes the CLI token immediately
3. Next CLI API call with the revoked token returns 401
4. Local files remain intact — only the authentication is invalidated

### 5. Token Lifecycle

1. Setup token expires after 15 minutes or first use (whichever comes first)
2. CLI token expires after 7 days of **inactivity** (rolling window renewed on each API call)
3. User can regenerate setup tokens at any time (previous unused ones still work until they expire)
4. Expired setup tokens are cleaned up automatically by a background scheduler (hourly)

## Business Rules

### Authentication

- Setup tokens are single-use — re-exchange returns 400
- CLI tokens are scoped to exactly one agent and one user
- CLI JWT includes `token_type: "cli"` to distinguish from regular user JWTs
- Token hash (SHA-256) stored in DB — actual token value shown only once at creation
- Rolling expiry: every successful API call renews the 7-day window
- Agent deletion cascades to CLI token deletion

### Authorization

| Resource | Rule |
|----------|------|
| Setup token creation | Authenticated user who owns the agent |
| Build context | CLI token owner must own the agent |
| Workspace (read/write) | CLI token owner must own the agent |
| Credentials | CLI token owner must own the agent |
| Knowledge search | CLI token owner must own the agent |
| Token revocation | Token owner only |

### Workspace Sync

- File paths validated against directory traversal (`../` rejected)
- Files > 100MB skipped during push/pull
- Push/pull only needed when moving work to/from production — not during local development (workspace is volume-mounted)
- After a successful push, agent prompts are resynced from the environment to the database (same as post-building-session resync). If the resync fails, the push still succeeds — a warning is logged

## Architecture Overview

```
User's IDE          cinna CLI           Local Container         Platform Backend
    |                   |                     |                       |
    |-- edits files --> workspace/ (volume mount)                     |
    |                   |                     |                       |
    |                   |-- exec ----------->| runs scripts           |
    |                   |                     |                       |
    |                   |-- push/pull ------->|<------- workspace --->|
    |                   |                     |                       |
    |                   |-- credentials ----->|<------- creds ------->|
    |                   |                     |                       |
    |              MCP proxy (stdio)          |                       |
    |<-- knowledge_query --|-- HTTP --------->|--- knowledge search ->|
```

## Integration Points

- **Agent Management** — CLI tokens are scoped per-agent, linked via FK. See [agent_management](../agent_management/agent_management.md)
- **Agent Environments** — Build context downloaded from the same environment templates used by the platform. See [agent_environments](../../agents/agent_environments/agent_environments.md)
- **Credentials** — Reuses `CredentialsService.prepare_credentials_for_environment()` for credential pull. See [ai_credentials](../ai_credentials/ai_credentials.md)
- **Knowledge Sources** — MCP proxy calls existing vector search infrastructure. See [knowledge_sources](../knowledge_sources/knowledge_sources.md)
- **Frontend Integrations Tab** — LocalDevCard sits alongside A2A, MCP, Access Token cards in the agent detail page

## Aspects

- [Local CLI Development](local_cli_development.md) — How to develop and test the `cinna` CLI tool itself against a local platform instance (editable install from source)
