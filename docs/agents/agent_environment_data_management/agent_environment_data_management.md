# Environment Data Management

## Purpose

Define how data flows between agents, environments, and installs, ensuring consistent behavior across lifecycle operations (create, activate, rebuild, install, apply-update, environment switch).

## Core Concepts

### Data Locations

- **agent_config** - Database `Agent` model fields and related tables. Persistent, survives environment deletion
- **environment** - Docker container filesystem `/app/workspace/`. Environment-specific, lost if environment deleted

### Data Ownership Levels

| Ownership Level | Description | Examples |
|-----------------|-------------|----------|
| **Bundle-owned** | Snapshotted into the bundle revision at publish time; replaced on apply-update | Workflow prompt, scripts, docs, knowledge, files, workspace_requirements.txt |
| **App Data (persistent)** | Per-user, per-bundle persistent volume; survives uninstall/reinstall; never overwritten by updates | `/app/workspace/app-data/storage/`, `uploads/`, `cache/` |
| **Credentials (synced)** | Linked to the install via `AgentCredentialLink`; synced into `workspace/credentials/` on every start | Integration credentials, service API keys |
| **User** | Data owned by user, shared across their agents | AI credentials |
| **Environment Runtime** | Data generated during execution, never synced | Logs, databases |

### Sync Timing Categories

- **Dynamic** - Synced on every container start (prompts, credentials, plugins)
- **On-Demand** - Synced during install creation, apply-update, or environment switch (bundle workspace files)
- **Persistent (App Data)** - Per-user volume reattached on install/reinstall; never overwritten
- **Never** - Runtime data that stays local to the environment (logs, databases)

## Data Classification Matrix

### Bundle-Owned Data (Replaced on apply-update)

Bundle-owned data is everything under `app/workspace/` **except** the per-user/runtime/secret denylist (`app-data/`, `credentials/`, `logs/`, `databases/`, `uploads/`, and the `__init__.py` template marker). This is a denylist model, not an allowlist — any custom top-level directory or file the agent creates in its workspace is bundle-owned and will be snapshotted at publish time and restored at install/update time.

| Data | Storage | Sync Timing | Notes |
|------|---------|-------------|-------|
| `workflow_prompt` | agent_config + environment | Dynamic | Synced to `/app/workspace/docs/WORKFLOW_PROMPT.md`; snapshotted into bundle revision at publish |
| `entrypoint_prompt` | agent_config + environment | Dynamic | Snapshotted into bundle revision at publish |
| `scripts/` folder | environment | On-Demand (install/update) | Replaced from bundle snapshot on install or apply-update |
| `docs/` folder | environment | On-Demand (install/update) | Replaced from bundle snapshot |
| `knowledge/` folder | environment | On-Demand (install/update) | Replaced from bundle snapshot |
| `files/` folder | environment | On-Demand (install/update) | Replaced from bundle snapshot (static assets shipped with bundle; `uploads/` is separate) |
| `webapp/` folder | environment | On-Demand (install/update) | Web app static files, data endpoints, actions registry; snapshotted in bundle revision |
| `agent_api/` folder | environment | On-Demand (install/update) | REST API source tree built by the agent; snapshotted in bundle revision |
| Any custom top-level dir/file | environment | On-Demand (install/update) | Any path the agent creates at `app/workspace/<name>` that is not in the denylist is bundle-owned and snapshotted |
| `workspace_requirements.txt` | environment | On-Demand (install/update) | Replaced from bundle snapshot |
| `workspace_system_packages.txt` | environment | On-Demand (install/update) | Replaced from bundle snapshot |
| Plugins (LLM tools) | agent_config | Dynamic | Synced via plugin sync operation; `plugins/` is merged on install/update (consumer marketplace plugins survive) |

### App Data (Persistent per user × bundle)

| Data | Storage | Sync Timing | Notes |
|------|---------|-------------|-------|
| `app-data/storage/` | AppDataVolume host path | Persistent | For structured user data (DBs, JSON, CSVs produced at runtime) |
| `app-data/uploads/` | AppDataVolume host path | Persistent | For files the user provides at runtime |
| `app-data/cache/` | AppDataVolume host path | Persistent | For cached downloads and processed files |

App Data is **never** touched by `replace_bundle_content`, rebuild, or apply-update. It survives uninstall and reattaches on reinstall of the same bundle.

### Per-Install Runtime Data (Not Bundle Content, but Env-Migration-Copied)

| Data | Storage | Notes |
|------|---------|-------|
| `uploads/` folder | environment | User-provided files uploaded at runtime. **Not bundle-owned** — excluded from bundle snapshots and never replaced on apply-update. However, it IS copied during environment switch (the `ENV_MIGRATION` profile includes it). For static assets to ship with a bundle, use `files/` instead. |

`uploads/` is the one workspace path whose classification differs between the two copy profiles: excluded from `BUNDLE_OWNED` (publish/seed/apply-update), included in `ENV_MIGRATION` (env switch/rebuild-from-env).

### Install-Specific Data (Credential Ownership)

| Data | Storage | Sync Timing | Notes |
|------|---------|-------------|-------|
| Integration credentials | agent_config | Dynamic | Links via `AgentCredentialLink`, synced to `/app/workspace/credentials/` |
| AI credentials | environment | On Start | Resolved from environment or user profile |

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

### 1b. CLI Push (Prompt Resync)

1. User runs `cinna push` to upload local workspace files to the remote environment
2. Files extracted to `/app/workspace/`
3. Agent prompts (workflow, entrypoint, refiner) resynced from environment back to DB — same as post-building-session resync
4. If workflow prompt changed: A2A skills regeneration and background description update triggered
5. See [Cinna CLI Integration](../../application/cinna_cli_integration/cinna_cli_integration.md)

### 2. Environment Switch (Same Agent)

1. User activates a different environment for the same agent
2. **Source env is resolved synchronously** in the activate handler — *before* `agent.active_environment_id` is flipped to the target — using this priority order (see _Source Environment Selection Priority_ below):
   - Current active environment (if set and different from target)
   - Most recently updated non-target env in `running`/`suspended`/`stopped` status
   - Environment from most recent session for this agent
3. The resolved source env id is passed to the background activation task
4. Workspace data copied from source to target
5. Old environments stopped, target started
6. Dynamic data synced to target

**Copied during switch** (ENV_MIGRATION profile): the full bundle-owned set (`scripts/`, `docs/`, `knowledge/`, `files/`, `webapp/`, `agent_api/`, `plugins/`, any custom top-level dirs, `workspace_requirements.txt`, `workspace_system_packages.txt`) **plus** `credentials/` and `uploads/`. Custom top-level directories created by the agent are included automatically — the ENV_MIGRATION profile is denylist-driven, not allowlist-driven. `plugins/` is a straight copy here (no merge) — env switch is same-user same-agent so there are no foreign consumer plugins to preserve. Top-level and nested symlinks are never followed or copied.

**NOT copied**: `logs/`, `databases/` (runtime data), `app-data/` (bind mount, follows the volume)

### 3. Install Creation

1. User installs a bundle via the catalog
2. New `Agent` (Install) row created from latest revision (prompts, SDK settings)
3. `AgentEnvironment` created; the workspace is seeded from the bundle revision snapshot (`seed_workspace_from_bundle_snapshot`) **inside the background env build** — after the instance dir is materialised from the template and before the container starts (the seed is passed via `create_environment(..., bundle_snapshot_path=...)`, not run in the foreground, to avoid racing the async build)
4. `AppDataVolume` created (or reattached if orphaned from previous install)
5. Credentials: for each `required_credential_spec`, either link an existing user credential or create a placeholder
6. App-data volume bind-mounted at `/app/workspace/app-data` in the generated docker-compose

**Seeded from bundle snapshot** (BUNDLE_OWNED profile, v2 snapshots): the full `app/workspace/` tree minus the denylist — `scripts/`, `docs/`, `knowledge/`, `files/`, `webapp/`, `agent_api/`, any custom top-level dirs the publisher shipped, `workspace_requirements.txt`, `workspace_system_packages.txt`. `plugins/` is merged (consumer's own marketplace plugins survive). For v1 (legacy) snapshots, only the original allowlist (`scripts/`, `docs/`, `knowledge/`, `files/`, the two `.txt` files, `plugins/`) is seeded.

**Persistent (from app-data volume)**: `app-data/storage/`, `app-data/uploads/`, `app-data/cache/`

**NOT copied from snapshot**: `logs/`, `databases/` (runtime), `credentials/` (handled separately via dynamic sync), `uploads/` (per-install runtime data, not bundle-owned)

### 4. Apply Update (Bundle Revision Push)

1. Publisher publishes new revision; all foreign installs receive `INSTALL_UPDATE_AVAILABLE`
2. User reviews release notes and clicks "Apply update" (or automatic mode triggers on next idle)
3. Environment stopped
4. `replace_bundle_content` overwrites bundle-owned folders from the new revision snapshot
5. Install's `workflow_prompt`, `entrypoint_prompt`, `refiner_prompt` synced from revision
6. `installed_revision_id` updated; `pending_update` cleared
7. Environment restarted; `INSTALL_UPDATE_APPLIED` event emitted

**Replaced/updated** (BUNDLE_OWNED profile, v2 snapshots): the full `app/workspace/` tree minus the denylist — every bundle-owned top-level entry in the new snapshot is copied/overwritten into the install workspace.

**Pruned (v2 only)**: any install-workspace top-level entry that (a) is not present in the new snapshot, (b) is not in `BUNDLE_EXCLUDED_TOPLEVEL`, (c) is not runtime-name-denylisted, and (d) is not `plugins/` — is deleted. This removes stale bundle-owned paths (e.g. a directory the old revision shipped but the new one dropped) without touching any user data. The prune pass is best-effort per entry; a failed removal is logged and does not abort the update.

**Preserved**: `app-data/` (AppDataVolume), `credentials/`, `logs/`, `databases/`, `uploads/` (all in `BUNDLE_EXCLUDED_TOPLEVEL`), and the consumer's own `plugins/` marketplace dirs (plugins are merged, never delete-swept).

**Important**: Files an agent wrote directly into a bundle-owned top-level directory (e.g. a file dropped into `scripts/`) will be overwritten or removed on apply-update because that directory is bundle-authoritative. Durable runtime output belongs in `app-data/`, which is never touched by apply-update.

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

When copying workspace between environments (`_find_source_environment_for_workspace_copy` in `backend/app/services/environments/environment_service.py`):

1. Current active environment (if set and different from target)
2. Most recently updated non-target env whose status is `running`, `suspended`, or `stopped` (defense-in-depth so the activate flow still finds a source even if the active-env flip races priority 1). Excludes `creating`/`building`/`error`/`deprecated` — those workspaces may be partial or invalid.
3. Environment from most recent session for this agent (via `Session.updated_at`)

The activate handler resolves the source env synchronously before spawning the activation background task, then passes `source_env_id` through to it. Resolving inside the background task would race the route's `set_active_environment(...)` call, which flips `agent.active_environment_id` to the target and would otherwise hide the previous env from priority 1.

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
├── scripts/                     # Bundle-owned (replaced on update)
├── docs/                        # Bundle-owned
│   ├── WORKFLOW_PROMPT.md
│   └── ENTRYPOINT_PROMPT.md
├── knowledge/                   # Bundle-owned
├── files/                       # Bundle-owned (static assets shipped with bundle)
├── webapp/                      # Bundle-owned — web app files, data endpoints, actions registry
│   ├── index.html
│   ├── api/                     #   Python data endpoint scripts
│   └── WEB_APP_ACTIONS.md       #   Actions registry for chat integration
├── agent_api/                   # Bundle-owned — REST API source tree built by the agent
├── <custom-dir>/                # Bundle-owned — any other top-level dir the agent creates
├── app-data/                    # App Data — persistent per (user × bundle), never bundle-owned
│   ├── storage/                 #   for structured runtime data (DBs, JSON, CSVs)
│   ├── uploads/                 #   for files the user provides at runtime
│   └── cache/                   #   for cached downloads, processed output
├── uploads/                     # Per-install runtime user-provided files — NOT bundle-owned;
│                                #   preserved on apply-update; copied on env migration
├── credentials/                 # Credentials (synced from platform on every start)
├── plugins/                     # LLM plugins (synced dynamically; merged on install/update)
├── logs/                        # Session logs (Runtime — never synced or copied)
├── databases/                   # Runtime databases (Runtime — never synced or copied)
└── workspace_requirements.txt   # Bundle-owned (replaced on update)
```

## Integration Points

- **[Agent Environments](../agent_environments/agent_environments.md)** - Lifecycle operations (start, rebuild, suspend/activate) trigger data sync; two-layer architecture separates system code from workspace data
- **[Agent Bundles & Installs](../agent_bundles/agent_bundles.md)** - Install creation seeds workspace from bundle revision snapshot; apply-update replaces bundle-owned folders while preserving App Data and credentials
- **[Agent App Data](../agent_app_data/agent_app_data.md)** - The persistent `app-data/` volume; lifecycle managed by `AppDataService`
- **[Credential Management](../agent_credentials/agent_credentials.md)** - Integration credentials synced dynamically to `workspace/credentials/` on every start
- **[AI Credentials](../../application/ai_credentials/ai_credentials.md)** - AI provider keys resolved and injected as environment variables during start
- **[Agent Plugins](../agent_plugins/agent_plugins.md)** - Plugins synced dynamically to `workspace/plugins/` on every start
- **[Knowledge Management](../../application/knowledge_sources/knowledge_sources.md)** - Knowledge files synced on-demand during clone/switch, template knowledge updated during rebuild
- **[Agent Webapp](../agent_webapp/agent_webapp.md)** - Webapp folder (`webapp/`) synced on-demand during clone/switch; contains static files, data endpoints, and the actions registry (`WEB_APP_ACTIONS.md`)

