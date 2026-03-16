# Authentication

## Purpose

Provides user identity and session management for the platform. Users authenticate via password or Google OAuth, receive a JWT token, and use it for all subsequent API requests. Supports dual authentication (both methods on one account), domain-restricted registration, and password recovery.

## Core Concepts

| Concept | Definition |
|---------|-----------|
| **JWT Token** | HS256-signed token containing user ID and expiration. Used as bearer token for all API requests |
| **Password Auth** | Email + password login using bcrypt-hashed passwords |
| **Google OAuth** | Authorization code flow via Google popup. See [Google OAuth](google_oauth.md) |
| **Domain Whitelist** | Optional restriction limiting new user registration to specific email domains |
| **Access Token** | The JWT stored in frontend `localStorage` and auto-included in API requests |
| **Guest Token** | Special JWT with `role=chat-guest` for unauthenticated agent chat access. See [Guest Sharing](../../agents/agent_sharing/guest_sharing.md) |

## User Stories / Flows

### Password Login

1. User enters email and password on the login page
2. Frontend submits credentials as OAuth2PasswordRequestForm
3. Backend validates credentials (bcrypt comparison)
4. Backend returns JWT access token
5. Frontend stores token in `localStorage` and redirects to dashboard

### User Registration (Signup)

1. User fills signup form (full name, email, password, confirm password)
2. Frontend validates locally (email format, password >= 8 chars, passwords match)
3. Backend checks domain whitelist (if configured)
4. Backend checks email uniqueness
5. Backend creates user with hashed password
6. Welcome email sent (if SMTP configured)
7. User redirected to login page

### Password Recovery

1. User enters email on recovery page
2. Backend generates time-limited reset token
3. Backend sends email with reset link containing token
4. User clicks link, enters new password
5. Backend validates token, hashes and saves new password
6. User redirected to login page

### Set Password (OAuth Users)

1. OAuth-only user (no password set) navigates to profile settings
2. User enters desired password
3. Backend sets password, enabling dual authentication

### Logout

1. User clicks logout
2. Frontend removes `access_token` from localStorage
3. Frontend redirects to login page

## Business Rules

### Token Lifecycle
- JWT tokens expire after 8 days (configurable via `ACCESS_TOKEN_EXPIRE_MINUTES`)
- Tokens contain only user ID (`sub`) and expiration (`exp`)
- Invalid/expired tokens return 403; missing users return 404
- Frontend clears token and redirects to login on 401/404 errors

### Registration Restrictions
- When `AUTH_WHITELIST_USER_DOMAINS` is set, only emails from listed domains can register
- Whitelist applies to both password signup and OAuth registration
- Admin-created users (`POST /api/v1/users/`) bypass the whitelist
- Email-integration-created users bypass the whitelist
- When whitelist is active, users cannot change their email address

### Account Protection
- Cannot unlink Google OAuth if no password is set (prevents lockout)
- Cannot delete superuser account via self-service
- Inactive users are blocked from authentication
- Password changes require current password verification
- Cannot set password if one already exists (use change password instead)

### Dual Authentication
- Users can have both password and Google OAuth linked
- `has_password` and `has_google_account` booleans exposed in user profile
- Either method produces the same JWT token

## Architecture Overview

```
Login Page ──→ POST /login/access-token ──→ UserService.authenticate() ──→ JWT Token
                                                                              │
Signup Page ──→ POST /users/signup ──→ UserService.register_user() ──→ User Created
                                                                              │
Google Button ──→ OAuth Flow ──→ AuthService.authenticate_with_google() ──→ JWT Token
                                                                              │
                                                                              ▼
Frontend (localStorage) ──→ Authorization Header ──→ deps.get_current_user() ──→ CurrentUser
```

## Integration Points

- **[Google OAuth](google_oauth.md)** - Alternative authentication method via Google popup flow
- **[Guest Sharing](../../agents/agent_sharing/guest_sharing.md)** - Special guest JWT tokens for unauthenticated agent chat access
- **[User Workspaces](../user_workspaces/user_workspaces.md)** - Workspace context applied after authentication
- **[AI Credentials](../ai_credentials/ai_credentials.md)** - User model stores encrypted AI credentials
- **Route Protection** - All `/_layout/*` frontend routes require valid authentication
