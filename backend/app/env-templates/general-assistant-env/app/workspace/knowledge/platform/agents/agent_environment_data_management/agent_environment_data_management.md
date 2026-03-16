# Environment Data Management

## Purpose

Define how data flows between agents, environments, and clones, ensuring consistent behavior across lifecycle operations (create, activate, rebuild, clone, sync updates, environment switch).

## Core Concepts

### Data Locations

- **agent_config** - Database `Agent` model fields and related tables. Persistent, survives environment deletion
- **environment** - Docker container filesystem `/app/workspace/`. Environment-specific, lost if environment deleted

### Data Ownership Levels

| Ownership Level | Description | Examples |
|-----------------|-------------|----------|
| **Original Agent** | Data defined by parent agent; clones receive copies and updates | Workflow prompt, scripts, docs, knowledge, files, plugins |
| **Clone/Instance Agent** | Data owned independently by each agent instance | Integration credential links, agent-specific settings |
| **User** | Data owned by user, shared across their agents | AI credentials, user workspace files |
| **Environment Runtime** | Data generated during execution, never synced | Logs, databases |

### Sync Timing Categories

- **Dynamic** - Synced on every container start (prompts, credentials, plugins)
- **On-Demand** - Synced during clone creation, push updates, or environment switch (workspace files)
- **Static (Clone)** - Copied during clone creation only, not updated afterwards (SDK config, A2A config)
- **Never** - Runtime data that stays local to the environment (logs, databases)

## Data Classification Matrix

### Agent Definition Data (Original Agent Ownership)

| Data | Storage | Sync Timing | Notes |
|------|---------|-------------|-------|
| `workflow_prompt` | agent_config | Dynamic | Synced to `/app/workspace/docs/WORKFLOW_PROMPT.md` |
| `entrypoint_prompt` | agent_config | Dynamic | Synced to `/app/workspace/docs/ENTRYPOINT_PROMPT.md` |
| `scripts/` folder | environment | On-Demand | Agent-created Python scripts |
| `docs/` folder | environment | On-Demand | Prompt files and workflow docs |
| `knowledge/` folder | environment | On-Demand | Integration docs, API guides |
| `files/` folder | environment | On-Demand | Reports, CSV files, SQLite DBs, caches |
| `uploads/` folder | environment | On-Demand | User-uploaded files |
| `webapp/` folder | environment | On-Demand | Web app static files, data endpoints, actions registry |
| `workspace_requirements.txt` | environment | On-Demand | Agent-installed Python packages |
| Plugins (LLM tools) | agent_config | Dynamic | Synced via plugin sync operation |

### Agent Instance Data (Clone/Instance Ownership)

| Data | Storage | Sync Timing | Notes |
|------|---------|-------------|-------|
| Integration credentials | agent_config | Dynamic | Links via `AgentCredentialLink`, synced to `/app/workspace/credentials/` |
| AI credentials | environment | On Start | Resolved from environment or user profile |
| Agent SDK config | agent_config | Static (Clone) | Copied during clone, not updated |
| A2A config | agent_config | Static (Clone) | Agent-to-agent communication settings |

### Environment Runtime Data (Never Synced)

| Data | Storage | Notes |
|------|---------|-------|
| `logs/` folder | environment | Session logs, debug output |
| `databases/` folder | environment | Runtime SQLite DBs, session state |

## User Stories / Flows

### 1. Environment Start (Dynamic Sync)

1. Container starts (new or existing)
2. Dynamic data sync runs:
   - Agent prompts sent to `workspace/docs/`
   - Integration credentials sent to `workspace/credentials/`
   - Plugins sent to `workspace/plugins/`
3. Environment ready for sessions

### 2. Environment Switch (Same Agent)

1. User activates a different environment for the same agent
2. System finds the best source environment (priority order):
   - Current active environment (if set and different from target)
   - Most recently updated suspended environment
   - Environment from most recent session for this agent
3. Workspace data copied from source to target
4. Old environments stopped, target started
5. Dynamic data synced to target

**Copied during switch**: `scripts/`, `docs/`, `knowledge/`, `files/`, `uploads/`, `credentials/`, `plugins/`, `webapp/`, `workspace_requirements.txt`

**NOT copied**: `logs/`, `databases/` (runtime data)

### 3. Clone Creation

1. Clone agent record created with clone fields
2. Environment created for clone
3. Workspace files copied from original agent's environment
4. Credentials linked: shared credentials linked directly, others created as placeholders

**Copied to clone**: `scripts/`, `docs/`, `knowledge/`, `files/`, `uploads/`, `webapp/`, `workspace_requirements.txt`

**NOT copied**: `logs/`, `databases/` (runtime), `credentials/` (handled separately via dynamic sync)

### 4. Clone Update (Push Updates)

1. Original agent owner pushes updates to all clones
2. For each clone: workspace files synced from parent
3. Dynamic data sync runs on next start

**Applied during update**: `scripts/`, `docs/`, `knowledge/`, `files/`, `uploads/`, `webapp/`, `workspace_requirements.txt`

**Not applied**: Integration credentials, runtime data

### 5. Environment Rebuild

1. Infrastructure files updated from template (Dockerfile, pyproject.toml)
2. Core server code replaced from template
3. Knowledge base files synced from template (add/update only, no deletions)

**Preserved**: All workspace data (scripts, files, docs, credentials, webapp, databases, logs)

## Business Rules

### AI Credential Resolution

AI credentials are resolved during environment start/rebuild:

| Scenario | Resolution Behavior |
|----------|---------------------|
| Credentials assigned on environment | Use **only** assigned credentials (supports shared via `AICredentialShare`) |
| Assigned credential not accessible | Warning logged, no fallback (environment may fail to start) |
| No credentials assigned | Fall back to user's default profile credentials |

**Key rule**: When credentials are specifically assigned (e.g., shared credentials for cloned agents), the system does **not** fall back to the user's own credentials. This ensures cloned agents always use the owner's shared credentials.

### Source Environment Selection Priority

When copying workspace between environments:
1. Current active environment (if set and different from target)
2. Most recently updated suspended environment
3. Environment from most recent session for this agent (via `Session.updated_at`)

### Extending the Framework

When adding new data types, determine:
1. **Ownership** - Original agent, clone/instance, user, or runtime
2. **Storage** - agent_config (DB) or environment (filesystem)
3. **Sync timing** - Dynamic (every start), on-demand (clone/switch), static (clone only), never
4. **Conflict resolution** - Overwrite, append, rename, skip

## Architecture Overview

```
Agent Model (DB) → Environment Lifecycle Manager → Docker Adapter → Docker Container → /app/workspace/
                          │                              │
                          ├── _sync_dynamic_data()       ├── set_agent_prompts()
                          ├── _setup_new_container()     ├── set_credentials()
                          └── copy_workspace_between()   └── set_plugins()
```

## Workspace Directory Structure

```
/app/workspace/
├── scripts/                     # Agent scripts (Original Agent)
├── docs/                        # Documentation (Original Agent)
│   ├── WORKFLOW_PROMPT.md
│   └── ENTRYPOINT_PROMPT.md
├── knowledge/                   # Integration docs (Original Agent)
├── files/                       # Reports & caches (Original Agent)
├── uploads/                     # User-uploaded files (Original Agent)
├── credentials/                 # Integration credentials (Clone/Instance)
├── plugins/                     # LLM plugins (Original Agent)
├── webapp/                      # Web app files, data endpoints, actions registry (Original Agent)
│   ├── index.html
│   ├── api/                     # Python data endpoint scripts
│   └── WEB_APP_ACTIONS.md       # Actions registry for chat integration
├── logs/                        # Session logs (Runtime - never synced)
├── databases/                   # Runtime databases (Runtime - never synced)
└── workspace_requirements.txt   # Python packages (Original Agent)
```

## Integration Points

- **[Agent Environments](../agent_environments/agent_environments.md)** - Lifecycle operations (start, rebuild, suspend/activate) trigger data sync; two-layer architecture separates system code from workspace data
- **[Agent Sharing](../agent_sharing/agent_sharing.md)** - Clone creation copies workspace data; push updates sync changes to all clones
- **[Credential Management](../agent_credentials/agent_credentials.md)** - Integration credentials synced dynamically to `workspace/credentials/` on every start
- **[AI Credentials](../../application/ai_credentials/ai_credentials.md)** - AI provider keys resolved and injected as environment variables during start
- **[Agent Plugins](../agent_plugins/agent_plugins.md)** - Plugins synced dynamically to `workspace/plugins/` on every start
- **[Knowledge Management](../../application/knowledge_sources/knowledge_sources.md)** - Knowledge files synced on-demand during clone/switch, template knowledge updated during rebuild
- **[Agent Webapp](../agent_webapp/agent_webapp.md)** - Webapp folder (`webapp/`) synced on-demand during clone/switch; contains static files, data endpoints, and the actions registry (`WEB_APP_ACTIONS.md`)

