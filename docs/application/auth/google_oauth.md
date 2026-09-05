# Google OAuth

## Purpose

Enables users to authenticate with their Google account as an alternative to password-based login. Supports account creation from Google, linking/unlinking Google to existing accounts, and access-policy-restricted registration.

## Core Concepts

| Concept | Definition |
|---------|-----------|
| **Authorization Code Flow** | OAuth flow where Google popup returns a short-lived code exchanged server-side for tokens |
| **ID Token** | JWT signed by Google containing user claims (email, name, google_id) |
| **Google ID** | Google's unique user identifier (`sub` claim), stored on the user for future logins |
| **Account Linking** | Associating a Google account with an existing platform user |
| **OAuth State** | CSRF protection token generated before the OAuth flow, validated on callback |
| **Google Auto-Register** | The `google_auto_register` switch on the [access policy](../server_configuration/access_policy.md): whether a Google sign-in on an unknown email may create an account. Ignored in invite-only mode |
| **Google Usable** | Google sign-in needs *two* independently configured facts: the backend's client id **and** secret (`google_auth_enabled`), and the frontend build's `VITE_GOOGLE_CLIENT_ID`. The button renders only when both hold |

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
   - **Unknown Google ID, unknown email** -> creates a new user (no password) **only if the access policy allows it**: open registration, `google_auto_register` on, and a match against the allowed email patterns. Otherwise 403 with the reason code
8. Backend returns JWT access token
9. Frontend stores token and navigates to the post-login target — the validated `?redirect=` URL param (stashed in `sessionStorage` before Google's popup opened) if present, otherwise the dashboard. See [Post-Login Redirect](auth.md#post-login-redirect)

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
3. If the user doesn't exist, the backend creates a new user from Google claims (no password), with the access policy's default role
4. The access policy is enforced on the Google account's email — registration mode, `google_auto_register`, and the allowed email patterns
5. In invite-only mode no account is ever created here. The Google button is still rendered on `/signup`, because it is how an already-existing (e.g. invited) account signs in

## Business Rules

### Account Resolution on Login
- First lookup by `google_id` (direct match)
- If no match, lookup by email - if found, auto-link Google to existing account
- If neither match, create a new user — subject to the access policy. The auto-link branch is **not** gated: linking is not registration, and the account already exists

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

### Access Policy on Google Registration
- Google registration is gated by `AccessPolicyService.can_register(origin="google")`. All three of open registration, `google_auto_register`, and a pattern match must hold
- **In `invite_only` mode Google never creates an account**, whatever `google_auto_register` says. The admin card greys the switch out and says so
- Refusals return 403 with a reason code — `registration_closed`, `google_auto_register_disabled`, or `email_not_allowed` — the same status and body shape as password signup, so neither path reveals whether an address already has an account
- Existing users are not affected by policy changes: patterns and registration mode gate account *creation* only
- Google first-login accounts are auto-confirmed (`email_confirmed=True`) and get the policy's `default_user_role`

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
- **[Access Policy](../server_configuration/access_policy.md)** - `google_auto_register` and `registration_mode` decide whether a Google first login creates an account; `google_auth_enabled` in the public projection tells the login page whether the backend has Google configured
- **[User Workspaces](../user_workspaces/user_workspaces.md)** - New Google-created users get default workspace
- **Frontend Settings** - Google link/unlink available in user profile settings
