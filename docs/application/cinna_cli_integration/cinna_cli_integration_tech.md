# Cinna CLI Integration — Technical Details

## File Locations

### Backend — Models

- `backend/app/models/cli/__init__.py` — Re-exports all CLI models
- `backend/app/models/cli/cli_setup_token.py` — CLISetupToken (table), CLISetupTokenBase, CLISetupTokenPublic, CLISetupTokenCreate, CLISetupTokenCreated
- `backend/app/models/cli/cli_token.py` — CLIToken (table), CLITokenBase, CLITokenPublic, CLITokenCreate, CLITokenCreated, CLITokensPublic, CLITokenPayload
- `backend/app/models/__init__.py` — Re-exports CLI models at package level

### Backend — Routes

- `backend/app/api/routes/cli.py` — Two routers: `setup_router` (`/api/cli-setup`) and `router` (under `/api/v1/cli`). Also contains `_verify_cli_agent_scope()` helper and `get_bootstrap_script()` endpoint

### Backend — Services

- `backend/app/services/cli/cli_service.py` — CLIService: setup token lifecycle, CLI token management, build context assembly, workspace sync, credentials, building context, knowledge search. Also contains `_get_platform_url()` and `_ensure_utc()` helpers
- `backend/app/services/cli/cli_auth.py` — CLIAuthService: JWT create/decode, token hashing
- `backend/app/services/cli/cli_setup_token_scheduler.py` — Background scheduler for expired token cleanup (hourly)

### Backend — Dependencies

- `backend/app/api/deps.py` — `CLIContext`, `CLIContextDep`, `get_cli_context()`, `_ensure_utc()`

### Backend — App Registration

- `backend/app/main.py` — `setup_router` registered at app level (prefix `/api/cli-setup`); cleanup scheduler started/stopped in lifespan
- `backend/app/api/main.py` — `router` registered under api_router

### Frontend — Components

- `frontend/src/components/Agents/LocalDevCard.tsx` — Setup command display, copy button, expiry countdown, active sessions list, disconnect dialog
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` — LocalDevCard added to integrations grid

### Frontend — Generated Client

- `frontend/src/client/sdk.gen.ts` — `CliService` with methods: `createSetupToken`, `listCliTokens`, `revokeCliToken`, `exchangeSetupToken`, `getBuildContext`, `getCredentials`, `getBuildingContext`, `getWorkspace`, `uploadWorkspace`, `getWorkspaceManifest`, `searchKnowledge`
- `frontend/src/client/types.gen.ts` — `CLISetupTokenCreated`, `CLITokenPublic`, `CLITokensPublic`, etc.

### Migrations

- `backend/app/alembic/versions/51014db83e57_add_cli_tokens.py` — Creates `cli_setup_token` and `cli_token` tables with indexes

### Tests

- `backend/tests/api/cli/test_cli.py` — Two scenario tests covering full lifecycle and CLI-authenticated endpoints
- `backend/tests/api/cli/conftest.py` — Patches Docker adapter for test environment
- `backend/tests/utils/cli.py` — Reusable test helpers

## Database Schema

### cli_setup_token

| Field | Type | Constraints |
|-------|------|-------------|
| id | UUID | PK |
| token | VARCHAR(64) | unique, indexed |
| agent_id | UUID | FK -> agent.id, CASCADE |
| environment_id | UUID | FK -> agent_environment.id, SET NULL, nullable |
| owner_id | UUID | FK -> user.id, CASCADE |
| is_used | BOOLEAN | default false |
| expires_at | TIMESTAMP WITH TZ | |
| created_at | TIMESTAMP WITH TZ | |

Indexes: `ix_cli_setup_token_token` (unique), `ix_cli_setup_token_owner_agent` (composite)

### cli_token

| Field | Type | Constraints |
|-------|------|-------------|
| id | UUID | PK |
| agent_id | UUID | FK -> agent.id, CASCADE |
| owner_id | UUID | FK -> user.id, CASCADE |
| name | VARCHAR(100) | |
| token_hash | VARCHAR | unique, indexed |
| prefix | VARCHAR(12) | |
| is_revoked | BOOLEAN | default false |
| last_used_at | TIMESTAMP WITH TZ | nullable |
| machine_info | VARCHAR(200) | nullable |
| expires_at | TIMESTAMP WITH TZ | |
| created_at | TIMESTAMP WITH TZ | |

Indexes: `ix_cli_token_token_hash` (unique), `ix_cli_token_owner_agent` (composite)

## API Endpoints

### Setup Bootstrap (no auth, under /api)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/cli-setup/{token}` | Serve bootstrap Python script (checks for `cinna`, delegates or shows install instructions) |
| POST | `/api/cli-setup/{token}` | Exchange setup token for CLI token + bootstrap payload (called by `cinna setup`) |

### CLI Management (user JWT auth)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/cli/setup-tokens` | Generate a setup token for an agent |
| GET | `/api/v1/cli/tokens` | List active CLI tokens (optionally filtered by agent_id) |
| DELETE | `/api/v1/cli/tokens/{token_id}` | Revoke a CLI token |

### Agent-scoped (CLI JWT auth)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/cli/agents/{agent_id}/build-context` | Download Docker build context tarball |
| GET | `/api/v1/cli/agents/{agent_id}/credentials` | Pull decrypted credentials |
| GET | `/api/v1/cli/agents/{agent_id}/building-context` | Get assembled building-mode prompt + settings |
| GET | `/api/v1/cli/agents/{agent_id}/workspace` | Download remote workspace tarball |
| POST | `/api/v1/cli/agents/{agent_id}/workspace` | Upload local workspace tarball |
| GET | `/api/v1/cli/agents/{agent_id}/workspace/manifest` | Get file manifest for diff |
| POST | `/api/v1/cli/agents/{agent_id}/knowledge/search` | Search agent's knowledge sources |

## Services & Key Methods

### CLIService (`backend/app/services/cli/cli_service.py`)

- `create_setup_token()` — Verifies agent ownership, generates random token, stores in DB, returns setup command
- `exchange_setup_token()` — Validates token (not used, not expired), creates CLIToken with JWT, marks setup token as used
- `cleanup_expired_setup_tokens()` — Deletes used tokens >24h old and expired unused tokens
- `list_tokens()` — Returns active (non-revoked, non-expired) tokens for a user, optionally filtered by agent
- `revoke_token()` — Soft-revokes a token (sets is_revoked=True), verified by ownership
- `get_build_context()` — Assembles tarball from environment template (Dockerfile, pyproject.toml, uv.lock, app/core/, generated docker-compose.yml)
- `get_workspace_tarball()` — Proxies to env core HTTP API to download workspace
- `upload_workspace()` — Proxies tarball upload to env core, then calls `EnvironmentService.sync_agent_prompts_from_environment()` to resync prompts back to DB (failure logged as warning, doesn't break push)
- `get_workspace_manifest()` — Proxies manifest request to env core
- `get_credentials_for_cli()` — Delegates to `CredentialsService.prepare_credentials_for_environment()`
- `get_building_context()` — Proxies to env core prompt generator; falls back to minimal context if env unavailable
- `search_knowledge()` — Generates query embedding, searches accessible knowledge sources via vector search

### CLIAuthService (`backend/app/services/cli/cli_auth.py`)

- `create_cli_jwt()` — Creates JWT with sub=token_id, agent_id, owner_id, token_type="cli"
- `decode_cli_jwt()` — Decodes and validates JWT, checks token_type=="cli"
- `hash_token()` — SHA-256 hash for secure storage

### get_cli_context (`backend/app/api/deps.py`)

- Decodes CLI JWT and verifies token_type
- Looks up CLIToken by ID, checks revocation and expiry
- Loads agent, verifies ownership match
- Loads active environment for the agent
- Updates last_used_at and renews expires_at (rolling 7-day window)
- Returns CLIContext (user, agent, environment, cli_token)

## Frontend Components

### LocalDevCard (`frontend/src/components/Agents/LocalDevCard.tsx`)

- **Setup button** — Triggers `CliService.createSetupToken`, displays curl command
- **Command display** — Readonly input with two inline buttons forming a button group: Copy (clipboard icon, 2s check feedback) and Regenerate (refresh icon, spins while loading)
- **Expiry countdown** — `useEffect` + `setInterval`, shows "Expires in Xm Ys" or "Expired"
- **Active sessions list** — `useQuery` with key `["cli-tokens", agentId]`, shows machine_info/name/prefix + relative last-used time
- **Disconnect dialog** — AlertDialog confirmation, calls `revokeCliToken`, invalidates query on success

### AgentIntegrationsTab (`frontend/src/components/Agents/AgentIntegrationsTab.tsx`)

- LocalDevCard added as the last card in the integrations grid

## Security

- Setup tokens: 15-minute TTL, single-use, token value is a 32-char URL-safe random string
- CLI tokens: JWT with HS256, 7-day rolling expiry, hash-stored in DB
- Token value shown only once at creation (same pattern as A2A access tokens)
- Every CLI API call validates: JWT signature, DB lookup, revocation check, ownership check
- Agent scope enforced per-endpoint via `_verify_cli_agent_scope()` helper
- Workspace file paths validated against directory traversal
- Credentials returned with decrypted values — user accepts local exposure
