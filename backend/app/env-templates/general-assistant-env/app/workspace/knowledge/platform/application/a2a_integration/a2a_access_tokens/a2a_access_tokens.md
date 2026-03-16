# A2A Access Tokens

## Purpose

Provides scoped JWT-based authentication tokens for external A2A clients to access agents without full user credentials. Tokens control which modes and sessions the external client can access.

## Core Concepts

| Concept | Description |
|---------|-------------|
| **Access Token** | JWT-based credential scoped to a specific agent with mode and scope restrictions |
| **Mode** | Controls which agent modes the token can access (conversation or building) |
| **Scope** | Controls session visibility (limited to own sessions or all sessions) |
| **Token Prefix** | First 8 characters of the token, stored for user identification |
| **Token Hash** | SHA256 hash of the token, used for verification (actual token never stored) |

## User Stories / Flows

### 1. Creating an Access Token

1. User navigates to agent's **Integrations** tab (A2A must be enabled)
2. Clicks "Create Token" in the Access Tokens card
3. Selects mode: `conversation` (default) or `building`
4. Selects scope: `limited` (default) or `general`
5. Enters a descriptive name for the token
6. Token is generated and displayed **once** with a copy button
7. User copies token - it cannot be retrieved again

### 2. External Client Authentication

1. Client includes token in `Authorization: Bearer <token>` header
2. Backend decodes JWT and checks `token_type='agent'`
3. Token validated against database (exists, not revoked, hash matches)
4. `A2AAuthContext` created with token payload and token ID
5. Mode and scope enforced on subsequent operations

### 3. Token Lifecycle Management

1. User can revoke a token (soft disable - keeps for audit trail)
2. User can restore a revoked token
3. User can delete a token permanently
4. Token expiration: 5 years from creation
5. `last_used_at` updated on each successful validation

## Business Rules

### Mode

| Mode | Description | Allowed Operations |
|------|-------------|-------------------|
| `conversation` | Default. Chat-only access | message/send, message/stream in conversation mode |
| `building` | Full access (includes conversation) | All modes including building |

### Scope

| Scope | Description | Session Access |
|-------|-------------|----------------|
| `limited` | Default. Isolated access | Only sessions created by this token |
| `general` | Full access | All sessions for the agent (including UI-created) |

**Use Cases:**
- `limited` - Share agent with external users who shouldn't see your conversations
- `general` - Your own external UI that needs access to all sessions

### Token States

| State | Description |
|-------|-------------|
| Active | Token is valid and usable |
| Revoked | Token marked as revoked, cannot be used |
| Expired | Token past expiration date (5 years from creation) |
| Deleted | Token removed from database |

### Session-Token Relationship

- Sessions store `access_token_id` when created via A2A token
- `limited` scope tokens can only access sessions where `session.access_token_id` matches
- `general` scope tokens can access all sessions for the agent
- If token is deleted, sessions retain `access_token_id` (SET NULL on delete)

### Token Security Rules

- Token value returned only once on creation (never stored in DB)
- Only SHA256 hash stored in database (not reversible)
- Token prefix (first 8 chars) stored for user identification
- Hash verification performed on every use
- Owner must own the agent for token CRUD operations

## Architecture Overview

```
External Client --> A2A Router --> Auth Context --> A2A Request Handler --> Session/Message Services
                       |
                Access Token Validation
                       |
                Mode/Scope Enforcement
```

### Authentication Flow

```
Token in Header --> get_a2a_auth_context() --> Decode JWT
                            |
                    Check token_type='agent'
                            |
              +-------------+-------------+
              |                           |
       A2A Token                   User JWT Token
              |                           |
       Validate against DB         Standard user auth
              |                           |
       Return A2AAuthContext       Return A2AAuthContext
       (with payload+token_id)     (with user)
```

### Scope Enforcement Flow

1. If accessing existing session with A2A token:
   - Get session from database
   - Check `AccessTokenService.can_access_session()`
   - Deny if limited scope and `session.access_token_id` doesn't match
2. If creating new session:
   - Pass `access_token_id` to SessionService
   - Session is linked to this token

## Integration Points

- **[A2A Protocol](../a2a_protocol/a2a_protocol.md)** - Access tokens are used for A2A authentication
- **[Agent Sessions](../../agent_sessions/agent_sessions.md)** - Sessions track `access_token_id` for scope enforcement

---

*Last updated: 2026-03-02*
