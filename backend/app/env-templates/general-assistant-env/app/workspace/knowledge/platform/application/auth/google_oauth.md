# Google OAuth

## Purpose

Enables users to authenticate with their Google account as an alternative to password-based login. Supports account creation from Google, linking/unlinking Google to existing accounts, and domain-restricted access.

## Core Concepts

| Concept | Definition |
|---------|-----------|
| **Authorization Code Flow** | OAuth flow where Google popup returns a short-lived code exchanged server-side for tokens |
| **ID Token** | JWT signed by Google containing user claims (email, name, google_id) |
| **Google ID** | Google's unique user identifier (`sub` claim), stored on the user for future logins |
| **Account Linking** | Associating a Google account with an existing platform user |
| **OAuth State** | CSRF protection token generated before the OAuth flow, validated on callback |

## User Stories / Flows

### Login with Google

1. User clicks "Continue with Google" on login page
2. Google popup opens, user selects Google account
3. Google returns authorization code to frontend
4. Frontend sends code + state to backend callback endpoint
5. Backend exchanges code for Google tokens (server-to-server)
6. Backend verifies ID token signature and claims
7. Backend resolves user:
   - **Known Google ID** -> existing user found
   - **Unknown Google ID, known email** -> auto-links Google to existing user
   - **Unknown Google ID, unknown email** -> creates new user (no password)
8. Backend returns JWT access token
9. Frontend stores token and redirects to dashboard

### Link Google Account

1. Authenticated user initiates Google OAuth from settings
2. Same popup flow produces an authorization code
3. Frontend sends code to link endpoint
4. Backend verifies Google ID is not already linked to another user
5. Backend sets `google_id` on the current user

### Unlink Google Account

1. User requests Google unlink from settings
2. Backend verifies user has a password set (prevents lockout)
3. Backend clears `google_id` from the user

### Sign Up with Google

1. User clicks "Continue with Google" on signup page
2. Same flow as Login with Google
3. If user doesn't exist, backend creates a new user from Google claims (no password)
4. Domain whitelist is enforced on the Google account's email

## Business Rules

### Account Resolution on Login
- First lookup by `google_id` (direct match)
- If no match, lookup by email - if found, auto-link Google to existing account
- If neither match, create new user (subject to domain whitelist)

### Linking Constraints
- A Google ID can only be linked to one platform user
- Attempting to link a Google ID already used by another user raises an error
- Only the authenticated user can link/unlink their own Google account

### Unlinking Protection
- Cannot unlink Google if the user has no password set (would cause lockout)
- After unlinking, user must use password or re-link Google to log in

### State Token Management
- State tokens are stored in-memory with 10-minute expiry
- Expired states are cleaned up on each new state generation
- Popup flow has built-in CSRF protection via browser same-origin policy

### Domain Whitelist
- Google OAuth registration respects `AUTH_WHITELIST_USER_DOMAINS`
- Users with non-whitelisted email domains are rejected during account creation
- Existing users are not affected by whitelist changes

## Architecture Overview

```
Google Login Button ──→ Google Popup ──→ Authorization Code
                                              │
                                              ▼
Frontend ──→ POST /auth/google/callback ──→ AuthService.authenticate_with_google()
                                              │
                                              ├── exchange_google_code() ──→ Google Token URL
                                              ├── verify_and_decode_google_token() ──→ Claims
                                              ├── Resolve user (find/link/create)
                                              └── Return JWT Token
```

## Integration Points

- **[Authentication](auth.md)** - Parent feature; Google OAuth produces the same JWT tokens
- **[User Workspaces](../user_workspaces/user_workspaces.md)** - New Google-created users get default workspace
- **Frontend Settings** - Google link/unlink available in user profile settings
