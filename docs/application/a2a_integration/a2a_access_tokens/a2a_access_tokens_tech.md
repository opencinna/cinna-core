# A2A Access Tokens - Technical Details

## File Locations

### Backend - API Layer
- `backend/app/api/routes/access_tokens.py` - Token CRUD endpoints
- `backend/app/api/routes/a2a.py` - Updated with dual auth support (`get_a2a_auth_context()`, `A2AAuthContext`)
- `backend/app/api/main.py` - Router registration

### Backend - Services
- `backend/app/services/a2a/access_token_service.py` - Token operations and validation
- `backend/app/services/a2a/a2a_request_handler.py` - Updated with scope enforcement
- `backend/app/services/sessions/session_service.py` - Updated with `access_token_id` param

### Backend - Models
- `backend/app/models/a2a/agent_access_token.py` - Token model and schemas
- `backend/app/models/sessions/session.py` - Added `access_token_id` field
- `backend/app/models/__init__.py` - Exports

### Backend - Migrations
- `backend/app/alembic/versions/f6a7b8c9d0e1_add_agent_access_tokens.py` - Token table and session FK

### Frontend
- `frontend/src/components/Agents/AccessTokensCard.tsx` - Token management UI
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` - Shows AccessTokensCard when A2A is enabled

## Database Schema

**Migration:** `backend/app/alembic/versions/f6a7b8c9d0e1_add_agent_access_tokens.py`

**Tables:**
- `agent_access_tokens` - Token metadata (name, mode, scope, hash, prefix, expiration, revoked status)

**Models:** `backend/app/models/a2a/agent_access_token.py`
- `AgentAccessToken` (table model)
- `AgentAccessTokenPublic`, `AgentAccessTokenCreated`, `AgentAccessTokenCreate`, `AgentAccessTokenUpdate` (schemas)
- `AccessTokenMode`, `AccessTokenScope` (enums)
- `A2ATokenPayload` (JWT payload structure)

**Updated:** `backend/app/models/sessions/session.py`
- `Session.access_token_id` - Foreign key to `agent_access_tokens.id` (nullable, SET NULL on delete)

### JWT Token Payload Structure

`A2ATokenPayload` fields:
- `sub` - Token ID (UUID)
- `agent_id` - Agent UUID this token is for
- `mode` - "conversation" or "building"
- `scope` - "limited" or "general"
- `token_type` - Always "agent" (distinguishes from user tokens)
- `exp` - Expiration timestamp (5 years from creation)

## API Endpoints

### Token Management
**File:** `backend/app/api/routes/access_tokens.py`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/agents/{agent_id}/access-tokens/` | GET | List all tokens for agent |
| `/api/v1/agents/{agent_id}/access-tokens/` | POST | Create new token (returns JWT once) |
| `/api/v1/agents/{agent_id}/access-tokens/{token_id}` | GET | Get token details |
| `/api/v1/agents/{agent_id}/access-tokens/{token_id}` | PUT | Update name or revoke |
| `/api/v1/agents/{agent_id}/access-tokens/{token_id}` | DELETE | Delete token |

### A2A Auth Context
**File:** `backend/app/api/routes/a2a.py`

- `get_a2a_auth_context()` - Dependency handling both user JWT and A2A token auth
- `get_optional_a2a_auth_context()` - Optional version for public card endpoints
- `A2AAuthContext` - Dataclass holding user or A2A token context

## Services & Key Methods

### AccessTokenService
**File:** `backend/app/services/a2a/access_token_service.py`

- `create_token()` - Generate JWT, hash it, store in DB, return JWT once
- `verify_a2a_token()` - Decode JWT, check `token_type='agent'`
- `validate_token_for_agent()` - Verify token exists, not revoked, hash matches
- `get_agent_tokens()` - List tokens for agent
- `update_token()` - Update name or revoked status
- `delete_token()` - Remove from database
- `can_access_session()` - Check scope allows session access
- `can_use_mode()` - Check mode allows requested operation

### A2ARequestHandler (token integration)
**File:** `backend/app/services/a2a/a2a_request_handler.py`

- Constructor accepts `a2a_token_payload` and `access_token_id`
- `_parse_and_validate_session_id()` - Stores `access_token_id` on new sessions, checks scope on existing
- `handle_tasks_get()` - Checks scope before returning task
- `handle_tasks_cancel()` - Checks scope before canceling

### SessionService (token integration)
**File:** `backend/app/services/sessions/session_service.py`

- `create_session()` - Accepts optional `access_token_id` parameter
- `list_environment_sessions()` - Filters by access_token_id for limited scope

## Frontend Components

### AccessTokensCard
**File:** `frontend/src/components/Agents/AccessTokensCard.tsx`

- Lists tokens with mode/scope badges
- Create dialog with mode and scope selection
- Shows token once on creation with copy button
- Revoke/restore toggle
- Delete with confirmation dialog

### AgentIntegrationsTab (token integration)
**File:** `frontend/src/components/Agents/AgentIntegrationsTab.tsx`

- Shows AccessTokensCard when A2A is enabled
- Token management only available when A2A integration is active

### State Management

- `useQuery` for token list (`["access-tokens", agentId]`)
- `useMutation` for create, update, delete operations
- Local state for create dialog form

## Security

### Token Validation Chain

1. JWT signature verification
2. Token type check (`token_type='agent'`)
3. Database existence check
4. Revoked status check
5. Hash verification (prevents token forgery)
6. Expiration check

### Access Control

- Token can only access its associated agent
- Mode enforced on message operations (`can_use_mode()`)
- Scope enforced on session access (`can_access_session()`)
- Owner verification for token CRUD (must own the agent)

### Token Storage

- SHA256 hash only (not reversible)
- Prefix (first 8 chars) for user identification
- Soft revoke option (keeps for audit trail)
- `last_used_at` updated on successful validation

---

*Last updated: 2026-03-02*
