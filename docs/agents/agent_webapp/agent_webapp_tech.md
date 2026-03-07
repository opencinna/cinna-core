# Agent Webapp - Technical Details

## File Locations

### Backend - Models

- `backend/app/models/agent.py` - `webapp_enabled` field on `Agent` (table model), `AgentUpdate`, and `AgentPublic`
- `backend/app/models/agent_webapp_share.py` - `AgentWebappShare` table model + schema hierarchy (Base, Create, Update, Public, Created, TokenPayload)
- `backend/app/models/__init__.py` - re-exports all webapp share models

### Backend - Routes

- `backend/app/api/routes/webapp.py` - Owner preview routes (auth required), thin controller delegating to `WebappService`
- `backend/app/api/routes/webapp_share.py` - Share CRUD (`router`) + public auth flow (`public_router`)
- `backend/app/api/routes/webapp_public.py` - Token-authenticated content serving (public), uses `WebappService` for status polling and activity tracking
- `backend/app/api/routes/webapp_templates.py` - HTML templates for public error pages and loading screens (extracted from route files)
- `backend/app/api/main.py` - All three routers registered

### Backend - Services

- `backend/app/services/webapp_service.py` - `WebappService` with agent+environment resolution, activity tracking, public status polling logic; exception hierarchy (`WebappError`, `WebappNotFoundError`, `WebappPermissionError`, `WebappNotAvailableError`)
- `backend/app/services/agent_webapp_share_service.py` - `AgentWebappShareService` with CRUD, token validation, security code verification, JWT issuance
- `backend/app/services/adapters/docker_adapter.py` - `get_webapp_status()`, `get_webapp_file()`, `call_webapp_api()` methods
- `backend/app/services/environment_lifecycle.py` - `webapp` added to `dirs_to_copy` in `copy_workspace_between_environments()`

### Backend - Agent-Env Core (inside container)

- `backend/app/env-templates/app_core_base/core/server/routes.py` - `GET /webapp/status`, `GET /webapp/{path}`, `POST /webapp/api/{endpoint}`
- `backend/app/env-templates/app_core_base/core/prompts/WEBAPP_BUILDING.md` - Building mode prompt for webapp development
- `backend/app/env-templates/app_core_base/core/prompts/BUILDING_AGENT.md` - One-liner reference to `WEBAPP_BUILDING.md`

### Frontend

- `frontend/src/routes/webapp/$webappToken.tsx` - Standalone public webapp page (token-based auth, security code entry, iframe rendering)
- `frontend/src/components/Agents/WebappShareCard.tsx` - Share management card in Integrations tab
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` - Updated to include WebappShareCard
- `frontend/src/components/Agents/AgentEnvironmentsTab.tsx` - `webapp_enabled` toggle
- `frontend/src/components/Environment/EnvironmentPanel.tsx` - "Web App" tab in workspace tree
- `frontend/src/components/Environment/TabHeader.tsx` - "Web App Files" dropdown option

### Migrations

- `backend/app/alembic/versions/5332c5643236_add_webapp_enabled_to_agent.py` - Adds `webapp_enabled` column to agent table
- `backend/app/alembic/versions/434f2232d22b_add_agent_webapp_share_table.py` - Creates `agent_webapp_share` table

### Tests

- `backend/tests/api/agents/agents_webapp_test.py` - 19 scenario-based tests covering CRUD, auth, owner preview, public serving
- `backend/tests/api/agents/agents_webapp_command_test.py` - `/webapp` command integration test
- `backend/tests/utils/webapp_share.py` - Test utility helpers
- `backend/tests/stubs/environment_adapter_stub.py` - `get_webapp_status()`, `get_webapp_file()`, `call_webapp_api()` stubs

## Database Schema

### `agent` table (modified)

- `webapp_enabled: bool` (default `False`) - whether webapp feature is active for this agent

### `agent_webapp_share` table (new)

| Column | Type | Description |
|---|---|---|
| `id` | UUID PK | Share ID |
| `agent_id` | UUID FK -> agent | CASCADE delete |
| `owner_id` | UUID FK -> user | CASCADE delete |
| `token` | VARCHAR | Full token (for owner to re-copy) |
| `token_hash` | VARCHAR (indexed, unique) | SHA256 hash for lookup |
| `token_prefix` | VARCHAR(12) | First 8 chars for display |
| `security_code_encrypted` | VARCHAR (nullable) | Fernet-encrypted 4-digit code (null = no code required, default) |
| `label` | VARCHAR(255) (nullable) | User-defined label |
| `is_active` | BOOL (default true) | Soft deactivation |
| `is_code_blocked` | BOOL (default false) | Blocked after 3 failed attempts |
| `failed_code_attempts` | INT (default 0) | Failed code entry counter |
| `allow_data_api` | BOOL (default true) | Whether data API is accessible |
| `expires_at` | TIMESTAMPTZ (nullable) | Expiration time (null = never) |
| `created_at` | TIMESTAMPTZ | Creation timestamp |
| `updated_at` | TIMESTAMPTZ | Last update timestamp |

## API Endpoints

### Owner Preview (`backend/app/api/routes/webapp.py`)

Prefix: `/api/v1/agents/{agent_id}/webapp`

| Method | Path | Purpose |
|---|---|---|
| GET | `/status` | Webapp metadata (exists, size, files, api_endpoints). Does NOT require `webapp_enabled`. |
| GET | `/{path:path}` | Serve static file. Checks size limit on index.html. Updates `last_activity_at`. |
| POST | `/api/{endpoint}` | Execute data script. Updates `last_activity_at`. |

All require owner auth. Returns 400 if `webapp_enabled=false` (except status), 400 if no active running env.

### Share Management (`backend/app/api/routes/webapp_share.py`)

Prefix: `/api/v1/agents/{agent_id}/webapp-shares`

| Method | Path | Purpose |
|---|---|---|
| POST | `/` | Create share. Requires `webapp_enabled`. Returns token, URL; security code only if `require_security_code=true`. |
| GET | `/` | List all shares for agent. Returns decrypted security codes for owner. |
| PATCH | `/{share_id}` | Update label, is_active, allow_data_api, security_code (resets block), remove_security_code (clears code requirement). |
| DELETE | `/{share_id}` | Delete share permanently. |

### Public Auth (`backend/app/api/routes/webapp_share.py`, `public_router`)

Prefix: `/api/v1/webapp-share`

| Method | Path | Purpose |
|---|---|---|
| GET | `/{token}/info` | Check validity, agent name, code requirement, blocked status. |
| POST | `/{token}/auth` | Authenticate with optional security code. Returns JWT. |

### Public Serving (`backend/app/api/routes/webapp_public.py`)

Prefix: `/api/v1/webapp`

| Method | Path | Purpose |
|---|---|---|
| GET | `/{token}/_status` | Env status for loading page polling. Triggers auto-activation. |
| GET | `/{token}/{path:path}` | Serve static file. Returns loading page if env not running, styled error HTML if webapp missing/oversized. |
| POST | `/{token}/api/{endpoint}` | Execute data script. Checks `allow_data_api`. |
| GET | `/{token}/api/{endpoint}` | Returns 405 (POST only). |

### Agent-Env Endpoints (inside container)

| Method | Path | Purpose |
|---|---|---|
| GET | `/webapp/status` | Returns exists, total_size_bytes, file_count, has_index, api_endpoints |
| GET | `/webapp/{path:path}` | Serves file with ETag/Last-Modified/304 support |
| POST | `/webapp/api/{endpoint}` | Executes Python script via stdin/stdout, timeout up to 300s |

## Services & Key Methods

### `WebappService` (`backend/app/services/webapp_service.py`)

- `resolve_agent_environment()` - Resolves agent + active running environment with ownership check; raises domain exceptions for not found, permission denied, webapp disabled, env not running
- `update_last_activity()` - Updates `last_activity_at` on environment to prevent suspension from webapp traffic
- `get_public_status()` - Returns environment readiness status for loading page polling; handles auto-activation of suspended environments

### `AgentWebappShareService` (`backend/app/services/agent_webapp_share_service.py`)

- `create_webapp_share()` - Generates token (secrets.token_urlsafe), hashes it (SHA256), optionally generates and encrypts security code (Fernet) if `require_security_code=True`, creates DB record
- `list_webapp_shares()` - Lists shares for agent, decrypts security codes for owner view
- `update_webapp_share()` - Updates fields; setting new security_code resets failed_code_attempts and is_code_blocked; `remove_security_code` clears the code requirement entirely
- `delete_webapp_share()` - Permanent deletion
- `validate_token()` - Finds share by token hash, checks is_active and expiration
- `authenticate()` - Validates token, verifies security code, issues JWT
- `get_share_info()` - Returns public info (validity, agent name, code requirement)
- `_verify_security_code()` - Decrypts stored code, compares, increments failures, blocks after 3
- `_create_webapp_jwt()` - Creates JWT with role "webapp-viewer", 24h max lifetime

### Docker Adapter (`backend/app/services/adapters/docker_adapter.py`)

- `get_webapp_status()` - `GET {base_url}/webapp/status`
- `get_webapp_file(path, request_headers)` - `GET {base_url}/webapp/{path}` with cache header pass-through
- `call_webapp_api(endpoint, params, timeout)` - `POST {base_url}/webapp/api/{endpoint}`

## Frontend Components

- `WebappShareCard.tsx` - Full share management UI: create dialog (label, expiration, allow_data_api, require_security_code), post-create view (copy URL, security code, embed snippet), share list with status badges, edit dialog (label, allow_data_api, require_security_code toggle with optional code input), delete per share
- `$webappToken.tsx` - Public page with auth state machine (loading -> code_entry -> authenticating -> ready/error), security code input, full-page iframe, `?embed=1` for chromeless mode
- `AgentEnvironmentsTab.tsx` - `webapp_enabled` switch next to inactivity period selector
- `EnvironmentPanel.tsx` - "Web App" tab for webapp files in workspace tree
- `TabHeader.tsx` - "Web App" dropdown link (visible when `webapp_enabled=true` AND `has_index=true`, opens owner preview in new tab)

## Security

- Share tokens stored as SHA256 hashes for lookup; full token stored for owner re-copy
- Security codes encrypted at rest with Fernet (`encrypt_field` / `decrypt_field`)
- 3 failed code attempts blocks the share (`is_code_blocked`)
- JWT tokens capped at 24h or share expiry (whichever is sooner)
- Agent-env path traversal protection: resolved path must stay within `/app/workspace/webapp/`
- Data API endpoint names validated: alphanumeric + underscores only
- Public serving includes `Content-Security-Policy: frame-ancestors *` for iframe embedding
- Data API responses always include `Cache-Control: no-store`

---

*Last updated: 2026-03-07*
