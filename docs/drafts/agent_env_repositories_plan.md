# Plan: Agent Repositories Feature

## Overview

Add a new "Repositories" entity that allows users to attach Git repositories to agents. Repositories are managed similarly to credentials (checkbox toggle in the agent-env panel). SSH keys provide authentication. Repos are NOT auto-cloned -- only config/folders are created; the agent clones on-demand.

---

## Phase 1: Backend Model & Migration

### 1.1 Create Model: `backend/app/models/repository.py`

- `AgentRepositoryBase` - shared fields: `name`, `git_url`, `branch`, `instructions`
- `AgentRepository(table=True)` - DB table with: `id`, `owner_id` (FK user), `ssh_key_id` (FK user_ssh_keys, SET NULL), `user_workspace_id` (FK user_workspace, SET NULL), `auto_clone`, `created_at`, `updated_at`
- `AgentRepositoryPublic` - API response (includes resolved `ssh_key_name`)
- `AgentRepositoriesPublic` - list wrapper with `data` + `count`
- `AgentRepositoryCreate` - create request
- `AgentRepositoryUpdate` - update request
- Unique constraint: `(owner_id, name)`

### 1.2 Create Link Model: add to `backend/app/models/link_models.py`

- `AgentRepositoryLink(table=True)` - composite PK: `agent_id` (FK agent, CASCADE) + `repository_id` (FK agent_repository, CASCADE)

### 1.3 Register in `backend/app/models/__init__.py`

### 1.4 Alembic Migration

```bash
make migration  # "add agent_repository and agent_repository_link tables"
```

---

## Phase 2: Backend Service

### 2.1 Create: `backend/app/services/repository_service.py`

Follow `credentials_service.py` pattern:

- `create_repository(session, repository_in, owner_id)`
- `update_repository(session, repository_id, repository_in, owner_id)` - triggers sync
- `delete_repository(session, repository_id, owner_id)` - triggers sync to affected agents
- `get_user_repositories(session, owner_id, user_workspace_id=None)`
- `get_agent_repositories(session, agent_id)`
- `link_repository_to_agent(session, agent_id, repository_id, owner_id)` - triggers sync
- `unlink_repository_from_agent(session, agent_id, repository_id, owner_id)` - triggers sync
- `prepare_repositories_for_environment(session, agent_id)` - returns `{"repos_config": {...}, "ssh_keys": {...}, "repos_readme": "..."}`
- `generate_repos_config(repositories, session, owner_id)` - builds the JSON config
- `generate_repositories_readme(repositories)` - markdown for agent context
- `sync_repositories_to_agent_environments(session, agent_id)` - syncs to all running envs
- `get_affected_agents(session, repository_id)` - find agents with this repo linked

---

## Phase 3: Backend API Routes

### 3.1 Create: `backend/app/api/routes/repositories.py`

CRUD endpoints (tag: `repositories`):
- `GET /repositories/` - list user's repos (optional workspace filter)
- `GET /repositories/{id}` - get single repo
- `POST /repositories/` - create repo
- `PUT /repositories/{id}` - update repo (triggers sync)
- `DELETE /repositories/{id}` - delete repo (triggers sync)

### 3.2 Modify: `backend/app/api/routes/agents.py`

Add agent-repository link endpoints (following credential pattern):
- `GET /agents/{id}/repositories` - list repos linked to agent
- `POST /agents/{id}/repositories` - link repo to agent (body: `{repository_id}`)
- `DELETE /agents/{id}/repositories/{repository_id}` - unlink repo from agent

### 3.3 Register in `backend/app/api/main.py`

---

## Phase 4: Backend Environment Integration

### 4.1 Modify: `backend/app/services/adapters/base.py`

Add abstract method:
```python
async def set_repositories(self, repositories_data: dict) -> bool:
```

### 4.2 Modify: `backend/app/services/adapters/docker_adapter.py`

Implement `set_repositories()` - POST to `/config/repositories` on agent-env container.

### 4.3 Modify: `backend/app/services/environment_lifecycle.py`

**In `_sync_dynamic_data()`** - add after credentials/plugins sync:
```python
repositories_data = RepositoryService.prepare_repositories_for_environment(session, agent.id)
await adapter.set_repositories(repositories_data)
```

**In `copy_workspace_between_environments()`** - add `"app/workspace/repositories"` to `dirs_to_copy` list. This preserves cloned repos, local branches, and uncommitted changes when switching environments.

---

## Phase 5: Agent-Env Implementation

### 5.1 Workspace Structure

```
/app/workspace/repositories/
  repos_config.json          # Config with repo list
  README.md                  # Agent-readable instructions
  .ssh/                      # SSH keys (mode 600)
    repo_key_<short_uuid>    # One key per repo that needs it
  my-docs-repo/              # Blank folder (agent clones here)
  api-service/               # Blank folder (agent clones here)
```

### 5.2 `repos_config.json` Format

```json
{
  "repositories": [
    {
      "name": "my-docs-repo",
      "git_url": "git@github.com:org/repo.git",
      "branch": "main",
      "path": "./my-docs-repo",
      "ssh_key_path": "./.ssh/repo_key_6a32aeb0",
      "instructions": "Documentation repo. Commit to feature/agent-updates branch.",
      "auto_clone": false
    }
  ]
}
```

### 5.3 New Endpoint in Agent-Env Core

**Modify**: `backend/app/env-templates/app_core_base/core/server/routes.py`

Add `POST /config/repositories`:
1. Write `repos_config.json` to `/app/workspace/repositories/`
2. Write `README.md` to `/app/workspace/repositories/`
3. Create `.ssh/` directory, write SSH keys with permissions `600`
4. Create empty folder per repository (if not already existing - don't delete existing cloned repos)
5. Remove SSH keys that are no longer referenced

**Modify**: `backend/app/env-templates/app_core_base/core/server/agent_env_service.py`

Add methods:
- `set_repositories(repos_config, ssh_keys, repos_readme)` - business logic for writing config/keys
- `_write_ssh_keys(ssh_keys_dir, ssh_keys_dict)` - write keys with proper permissions
- `_create_repo_directories(repos_dir, repositories)` - create blank folders

**Modify**: `backend/app/env-templates/app_core_base/core/server/models.py`

Add `RepositoriesConfigRequest` Pydantic model.

### 5.4 Prompt Integration

**Modify**: `backend/app/env-templates/app_core_base/core/server/prompt_generator.py`

Add `_get_repositories_context()` method:
- Read `repositories/repos_config.json`
- Generate prompt section listing available repos with clone instructions
- Include in both building and conversation mode prompts

Prompt addition example:
```
## Git Repositories

The following repositories are available in `./repositories/`:

| Name | Branch | Instructions |
|------|--------|--------------|
| my-docs-repo | main | Documentation repo. Commit to feature/agent-updates. |

To work with a repository:
1. Read `./repositories/repos_config.json` for full configuration
2. Clone: `GIT_SSH_COMMAND="ssh -i repositories/{ssh_key_path} -o StrictHostKeyChecking=no" git clone {git_url} repositories/{path}`
3. Or if already cloned, `cd repositories/{name}` and use git normally

IMPORTANT: Use the SSH key path from repos_config.json for all git operations with that repository.
```

---

## Phase 6: Frontend

### 6.1 Create: `frontend/src/components/Environment/RepositoriesTabContent.tsx`

Checkbox list component (same pattern as `CredentialsTabContent.tsx`):
- Shows each user repository with toggle checkbox
- Displays: name, git_url (truncated), branch badge
- Loading spinner during mutation

### 6.2 Modify: `frontend/src/components/Environment/EnvironmentPanel.tsx`

- Add "Repositories" tab (value: `"repositories"`)
- Add queries: `useQuery(["agent-repositories", agentId])` and `useQuery(["repositories"])`
- Add mutations for link/unlink
- Render `<RepositoriesTabContent>`

### 6.3 Create: `frontend/src/routes/_layout/repositories.tsx`

Repository management page:
- List of user's repositories with cards
- Create/Edit modal with fields: name, git_url, branch, ssh_key (dropdown), instructions (textarea), auto_clone (toggle)
- Delete with confirmation

### 6.4 Regenerate API Client

```bash
source backend/.venv/bin/activate && make gen-client
```

---

## Implementation Order

1. Model + Migration (Phase 1)
2. Service layer (Phase 2)
3. API routes (Phase 3)
4. Environment integration - adapter + lifecycle (Phase 4)
5. Agent-env endpoint + prompt (Phase 5)
6. Frontend (Phase 6)

---

## Verification Plan

1. **Backend API**: Create repository, link to agent, verify via GET endpoints
2. **Sync**: Start an environment, verify `repos_config.json` and SSH keys appear in `workspace/repositories/`
3. **Agent prompt**: Check building/conversation mode prompts include repos section
4. **Agent clone**: In a building session, ask agent to clone a configured repo using the SSH key - verify it works
5. **Environment switch**: Clone a repo in env A, switch to env B, verify cloned repo (with local changes) is copied
6. **Rebuild**: Rebuild environment, verify repositories folder preserved
7. **Unlink**: Unlink a repo, verify SSH key removed from environment, config updated
8. **Multi-repo**: Link 2+ repos to same agent, verify both appear in config and agent can work with both
9. **Frontend**: Toggle checkbox in panel, verify API calls succeed and sync triggers

---

## Key Files to Modify/Create

**Create:**
- `backend/app/models/repository.py`
- `backend/app/services/repository_service.py`
- `backend/app/api/routes/repositories.py`
- `backend/app/alembic/versions/<hash>_add_agent_repository.py` (auto-generated)
- `frontend/src/components/Environment/RepositoriesTabContent.tsx`
- `frontend/src/routes/_layout/repositories.tsx`

**Modify:**
- `backend/app/models/link_models.py` - add AgentRepositoryLink
- `backend/app/models/__init__.py` - register new models
- `backend/app/api/main.py` - register route
- `backend/app/api/routes/agents.py` - add agent-repo endpoints
- `backend/app/services/adapters/base.py` - add set_repositories abstract method
- `backend/app/services/adapters/docker_adapter.py` - implement set_repositories
- `backend/app/services/environment_lifecycle.py` - sync in _sync_dynamic_data + copy in workspace copy
- `backend/app/env-templates/app_core_base/core/server/routes.py` - POST /config/repositories
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` - repository file ops
- `backend/app/env-templates/app_core_base/core/server/models.py` - request model
- `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` - repos context
- `frontend/src/components/Environment/EnvironmentPanel.tsx` - add tab + queries