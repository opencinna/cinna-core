# Google OAuth - Technical Details

## File Locations

### Backend - Service
- `backend/app/services/users/auth_service.py` - OAuth flow orchestration, state management, Google API interactions

### Backend - Routes
- `backend/app/api/routes/oauth.py` - OAuth endpoints (config, authorize, callback, link, unlink)

### Backend - Security
- `backend/app/core/security.py` - `verify_google_token()` - RS256 ID token verification with cached Google public keys
- `backend/app/core/config.py` - Google OAuth settings (client ID, secret, redirect URI)

### Frontend - Components
- `frontend/src/components/Auth/GoogleLoginButton.tsx` - Google OAuth button using `@react-oauth/google`

### Frontend - Routes
- `frontend/src/routes/login.tsx` - Login page with Google button
- `frontend/src/routes/signup.tsx` - Signup page with Google button

## API Endpoints

### OAuth Routes (`backend/app/api/routes/oauth.py`)
- `GET /api/v1/auth/oauth/config` - Returns OAuthConfig (google_enabled, allow_email_change)
- `GET /api/v1/auth/google/authorize` - Generates state token, returns Google authorization URL
- `POST /api/v1/auth/google/callback` - Exchanges code for tokens, resolves user, returns JWT
- `POST /api/v1/auth/google/link` - Links Google to authenticated user (requires CurrentUser)
- `DELETE /api/v1/auth/google/unlink` - Unlinks Google from user (requires CurrentUser)

## Services & Key Methods

### AuthService (`backend/app/services/users/auth_service.py`)

**OAuth Flow Methods:**
- `is_google_oauth_enabled()` - Checks if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are configured
- `generate_oauth_state()` - Creates 32-byte URL-safe token, stores in-memory with 10-min TTL
- `consume_oauth_state(state)` - Validates and removes state token (one-time use)
- `build_google_authorization_url(state)` - Constructs Google auth URL with scopes "openid email profile"
- `exchange_google_code(code)` - POST to Google Token URL with `redirect_uri="postmessage"` (popup flow)
- `verify_and_decode_google_token(id_token)` - Delegates to `security.verify_google_token()`

**User Resolution Methods:**
- `get_user_by_google_id(session, google_id)` - Database lookup by Google ID
- `get_user_by_email(session, email)` - Database lookup by email
- `create_user_from_google(session, email, google_id, full_name)` - Creates user with no password, enforces whitelist

**Account Management Methods:**
- `authenticate_with_google(session, code, state)` - Full OAuth flow: state validation -> code exchange -> token verification -> user resolution -> JWT
- `link_google_account_for_user(session, user, code, state)` - Link flow with duplicate check
- `unlink_google_account_for_user(session, user)` - Unlink with password-exists validation
- `link_google_account(session, user, google_id)` - Sets `google_id` on user
- `unlink_google_account(session, user)` - Clears `google_id` from user

### Security (`backend/app/core/security.py`)
- `verify_google_token(token, client_id)` - RS256 verification using Authlib `JsonWebToken`
  - Caches Google public keys for 1 hour
  - Validates issuer: `accounts.google.com`
  - Validates audience: `GOOGLE_CLIENT_ID`
  - Requires `email_verified=true`

## Frontend Components

### GoogleLoginButton (`frontend/src/components/Auth/GoogleLoginButton.tsx`)
- Uses `@react-oauth/google` library (`useGoogleLogin` hook)
- Flow type: `"auth-code"` (server-side exchange)
- State: Generated via `crypto.randomUUID()`, stored in `sessionStorage`
- On success: calls `OauthService.googleCallback()`, stores JWT in localStorage, navigates to /
- On error: clears state from sessionStorage, shows error toast

## Configuration

| Setting | Location | Purpose |
|---------|----------|---------|
| `GOOGLE_CLIENT_ID` | `.env` / `config.py` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | `.env` / `config.py` | Google OAuth client secret |
| `GOOGLE_REDIRECT_URI` | `config.py` | Frontend callback URL (default: `http://localhost:5173/auth/google/callback`) |
| `google_oauth_enabled` | `config.py` (computed) | True if both client ID and secret are configured |

## Security

- **ID Token Verification**: RS256 signature check against Google's public keys (JWK)
- **Key Caching**: Google public keys cached for 1 hour to reduce external calls
- **Claim Validation**: Issuer, audience, and email_verified all verified
- **State Tokens**: In-memory with 10-minute expiry; popup flow adds same-origin policy protection
- **Code Exchange**: Server-side only (authorization code never exposed to client beyond initial receipt)
- **Duplicate Prevention**: Google ID uniqueness enforced at database level and application level
