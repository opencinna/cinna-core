# Authentication - Technical Details

## File Locations

### Backend - Models
- `backend/app/models/users/user.py` - User model (table), UserBase, UserCreate, UserRegister, UserUpdate, UserUpdateMe, UserPublic, Token, TokenPayload, OAuthConfig, SetPassword, NewPassword, UpdatePassword

### Backend - Routes
- `backend/app/api/routes/login.py` - Password login, test token, password recovery, password reset
- `backend/app/api/routes/oauth.py` - OAuth config, Google authorize/callback/link/unlink
- `backend/app/api/routes/users.py` - Signup, profile, password management, admin user CRUD

### Backend - Services
- `backend/app/services/users/auth_service.py` - OAuth flows, domain whitelist, state management
- `backend/app/services/users/user_service.py` - User CRUD, password hashing, registration, password recovery

### Backend - Core
- `backend/app/core/security.py` - JWT creation, bcrypt hashing, Google token verification, field encryption
- `backend/app/core/config.py` - Auth settings (secrets, token expiry, OAuth config, whitelist)
- `backend/app/api/deps.py` - Auth dependency injection (CurrentUser, TokenDep, guest context)

### Frontend - Hooks
- `frontend/src/hooks/useAuth.ts` - Auth state management (login, logout, signup, current user query)

### Frontend - Routes
- `frontend/src/routes/login.tsx` - Login page with email/password form and Google button
- `frontend/src/routes/signup.tsx` - Registration page with Google button
- `frontend/src/routes/recover-password.tsx` - Password recovery form
- `frontend/src/routes/reset-password.tsx` - Password reset form (token from URL)
- `frontend/src/routes/_layout.tsx` - Protected route guard (`isLoggedIn()` check)

### Frontend - Components
- `frontend/src/components/Auth/GoogleLoginButton.tsx` - Google OAuth button component

## Database Schema

### User Table (`backend/app/models/users/user.py`)

Auth-relevant fields:

| Field | Type | Purpose |
|-------|------|---------|
| `id` | UUID | Primary key, used as JWT `sub` claim |
| `email` | String (unique) | Login identifier |
| `hashed_password` | String (nullable) | Bcrypt hash; null for OAuth-only users |
| `google_id` | String (nullable, unique) | Google's unique user ID for OAuth linking |
| `is_active` | Boolean | Must be true to authenticate |
| `is_superuser` | Boolean | Admin access flag |

## API Endpoints

### Login Routes (`backend/app/api/routes/login.py`)
- `POST /api/v1/login/access-token` - Password login (OAuth2PasswordRequestForm) -> Token
- `POST /api/v1/login/test-token` - Validate JWT -> UserPublic
- `POST /api/v1/password-recovery/{email}` - Initiate password reset -> Message
- `POST /api/v1/reset-password/` - Complete password reset (NewPassword) -> Message

### OAuth Routes (`backend/app/api/routes/oauth.py`)
- `GET /api/v1/auth/oauth/config` - OAuth availability -> OAuthConfig
- `GET /api/v1/auth/google/authorize` - Start OAuth flow -> {authorization_url, state}
- `POST /api/v1/auth/google/callback` - Handle OAuth code (GoogleCallbackRequest) -> Token
- `POST /api/v1/auth/google/link` - Link Google to authenticated user (requires CurrentUser)
- `DELETE /api/v1/auth/google/unlink` - Unlink Google account (requires CurrentUser)

### User Routes (`backend/app/api/routes/users.py`) - Auth-relevant
- `POST /api/v1/users/signup` - Public registration (UserRegister) -> UserPublic
- `GET /api/v1/users/me` - Current user profile -> UserPublic
- `PATCH /api/v1/users/me/password` - Change password (UpdatePassword)
- `POST /api/v1/users/me/set-password` - Set password for OAuth users (SetPassword)

## Services & Key Methods

### UserService (`backend/app/services/users/user_service.py`)
- `authenticate(session, email, password)` - Validate credentials, return User or None
- `register_user(session, email, password, full_name)` - Public signup with whitelist check
- `create_user(session, user_create)` - Admin user creation with password hashing
- `update_password(session, user, current_password, new_password)` - Password change with validation
- `set_password(session, user, new_password)` - Set password for OAuth-only users
- `reset_password(session, token, new_password)` - Token-based password reset
- `recover_password(session, email)` - Generate reset token and send email
- `create_email_user(session, email)` - Create user from email (email integration, bypasses whitelist)

### AuthService (`backend/app/services/users/auth_service.py`)
- `is_email_domain_allowed(email)` - Validate against whitelist
- `create_access_token(user_id)` - Generate JWT via `security.create_access_token()`
- See [Google OAuth Tech](google_oauth_tech.md) for OAuth-specific methods

### Security (`backend/app/core/security.py`)
- `create_access_token(subject, expires_delta)` - JWT creation (PyJWT, HS256, SECRET_KEY)
- `verify_password(plain, hashed)` - Bcrypt comparison via passlib
- `get_password_hash(password)` - Bcrypt hashing via passlib

### Dependencies (`backend/app/api/deps.py`)
- `TokenDep` - OAuth2PasswordBearer extracting JWT from Authorization header
- `get_current_user(session, token)` - Decode JWT, fetch User, validate active status
- `CurrentUser` - Annotated dependency for authenticated user
- `get_current_active_superuser(current_user)` - Admin-only guard (403 if not superuser)
- `get_current_user_or_guest(session, token)` - Resolves both regular user and guest JWT types

## Frontend Components

### useAuth Hook (`frontend/src/hooks/useAuth.ts`)
- `isLoggedIn()` - Checks `localStorage.getItem("access_token") !== null`
- `user` query - `useQuery(["currentUser"], UsersService.readUserMe())` with auto-logout on 401/404
- `loginMutation` - Password login via `LoginService.loginAccessToken()`, stores token
- `signUpMutation` - Registration via `UsersService.registerUser()`
- `logout()` - Removes token from localStorage, navigates to /login

### Route Guard (`frontend/src/routes/_layout.tsx`)
- `beforeLoad` checks `isLoggedIn()`, throws `redirect({ to: "/login" })` if false
- All routes under `/_layout/` are protected

### Login Page (`frontend/src/routes/login.tsx`)
- Zod validation (email, password >= 8 chars)
- Google button above email form with "Or continue with email" divider
- Links to /signup and /recover-password

## Configuration

| Setting | Location | Purpose |
|---------|----------|---------|
| `SECRET_KEY` | `.env` / `config.py` | JWT signing secret (32-byte random) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `config.py` | Token lifetime (default: 8 days) |
| `AUTH_WHITELIST_USER_DOMAINS` | `.env` / `config.py` | Comma-separated allowed email domains |
| `allow_user_email_change` | `config.py` (computed) | False when whitelist is active |
| `EMAILS_ENABLED` | `config.py` | Whether password recovery emails are sent |

## Security

- **JWT**: HS256 with 32-byte random SECRET_KEY, 8-day expiry
- **Passwords**: Bcrypt hashing via passlib CryptContext
- **Token validation**: `get_current_user()` verifies signature, expiration, and user active status
- **Domain whitelist**: Enforced at registration for both password and OAuth paths
- **Lockout prevention**: Cannot unlink Google without password set; cannot delete superuser self
