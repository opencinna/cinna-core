# Authentication

## Overview

The application supports two authentication methods: password-based login and Google OAuth. Users can have one or both methods enabled. JWT tokens are used for session management.

## Authentication Methods

### Password Authentication

- **Endpoint**: `POST /api/v1/login/access-token`
- **Route**: `backend/app/api/routes/login.py`
- **Flow**: Email + password → JWT token

### Google OAuth

- **Service**: `backend/app/services/auth_service.py` → `AuthService`
- **Routes**: `backend/app/api/routes/oauth.py`
- **Flow**: Google popup → authorization code → token exchange → JWT token

## AuthService

Central service for OAuth and Google-related authentication logic.

**Location**: `backend/app/services/auth_service.py`

### Key Methods

| Method | Purpose |
|--------|---------|
| `is_google_oauth_enabled()` | Check if Google OAuth is configured |
| `is_email_domain_allowed()` | Check if email domain is in whitelist |
| `generate_oauth_state()` | Create CSRF state token for OAuth flow |
| `build_google_authorization_url()` | Build Google OAuth redirect URL |
| `exchange_google_code()` | Exchange authorization code for tokens |
| `verify_and_decode_google_token()` | Verify Google ID token |
| `authenticate_with_google()` | Complete OAuth flow: code → user + JWT |
| `link_google_account_for_user()` | Link Google to existing user |
| `unlink_google_account_for_user()` | Remove Google link from user |

### User Operations

| Method | Purpose |
|--------|---------|
| `get_user_by_google_id()` | Find user by Google ID |
| `get_user_by_email()` | Find user by email |
| `create_user_from_google()` | Create new user from OAuth (no password) |
| `link_google_account()` | Set Google ID on user |
| `unlink_google_account()` | Clear Google ID from user |

## OAuth Flow

### Login with Google

1. Frontend calls `GET /auth/google/authorize`
2. `AuthService.generate_oauth_state()` creates CSRF token
3. `AuthService.build_google_authorization_url()` returns redirect URL
4. User authenticates with Google (popup flow)
5. Frontend receives authorization code
6. Frontend calls `POST /auth/google/callback` with code + state
7. `AuthService.authenticate_with_google()`:
   - Exchanges code for tokens via `exchange_google_code()`
   - Verifies ID token via `verify_and_decode_google_token()`
   - Finds or creates user (auto-links if email matches existing user)
   - Returns JWT access token

### Link Google Account

1. Authenticated user initiates Google OAuth
2. Frontend calls `POST /auth/google/link` with code + state
3. `AuthService.link_google_account_for_user()`:
   - Verifies Google ID not already linked to another user
   - Links Google ID to current user

### Unlink Google Account

1. User calls `DELETE /auth/google/unlink`
2. `AuthService.unlink_google_account_for_user()`:
   - Validates user has password set (prevents lockout)
   - Removes Google ID from user

## State Management

OAuth state tokens prevent CSRF attacks. Stored in-memory with 10-minute expiry.

- `_oauth_states`: In-memory dict (use Redis in production)
- `_cleanup_expired_states()`: Removes expired tokens on each new state generation

## JWT Tokens

- **Creation**: `AuthService.create_access_token()` → `backend/app/core/security.py`
- **Expiry**: Configured via `ACCESS_TOKEN_EXPIRE_MINUTES` setting
- **Storage**: Frontend localStorage (`access_token` key)
- **Validation**: `backend/app/api/deps.py` → `get_current_user()`

## User Model Fields

Relevant fields in `backend/app/models/user.py`:

| Field | Purpose |
|-------|---------|
| `email` | Unique identifier, used for login |
| `hashed_password` | Nullable - OAuth-only users have no password |
| `google_id` | Nullable - Google's unique user ID |
| `is_active` | Must be true to authenticate |

## Configuration

Settings in `backend/app/core/config.py`:

| Setting | Purpose |
|---------|---------|
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `GOOGLE_REDIRECT_URI` | OAuth callback URL |
| `google_oauth_enabled` | Computed property - true if client ID/secret configured |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | JWT token lifetime |
| `AUTH_WHITELIST_USER_DOMAINS` | Comma-separated allowed domains for registration (e.g., `"example.com,company.org"`) |
| `auth_whitelist_domains` | Computed property - parsed list of allowed domains |
| `allow_user_email_change` | Computed property - false when whitelist is active |

## Domain Whitelist

Restrict new user registration to specific email domains.

**Configuration**: Set `AUTH_WHITELIST_USER_DOMAINS` in `.env`:
```
AUTH_WHITELIST_USER_DOMAINS=example.com,company.org
```

**Behavior**:
- New users (signup or OAuth) must have email from whitelisted domain
- Existing users are not affected
- Admin-created users (`POST /api/v1/users/`) bypass whitelist
- When whitelist is active, users cannot change their email (`PATCH /me`)

**Validation**: `AuthService.is_email_domain_allowed(email)` checks domain against whitelist

## Related Files

- `backend/app/api/routes/login.py` - Password login endpoint
- `backend/app/api/routes/users.py` - User signup, password management
- `backend/app/core/security.py` - Password hashing, JWT creation, Google token verification
- `backend/app/api/deps.py` - Authentication dependencies (`CurrentUser`, `SessionDep`)
