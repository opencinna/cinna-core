# Claude Development Guide

This document provides context and instructions for LLM assistants working on this Full Stack FastAPI + React project.

## Project Overview

This is a **Full Stack Web Application** with:
- **Backend**: FastAPI (Python) with PostgreSQL database
- **Frontend**: React + TypeScript with TanStack Router & Query
- **Authentication**: JWT tokens + Google OAuth
- **ORM**: SQLModel (combines SQLAlchemy + Pydantic)
- **Database Migrations**: Alembic
- **Package Manager**: Backend uses `uv`, Frontend uses `npm`

## Project Structure

```
workflow-runner-core/
├── backend/                      # FastAPI backend
│   ├── app/
│   │   ├── main.py              # FastAPI app entry point
│   │   ├── models.py            # SQLModel database models
│   │   ├── crud.py              # Database CRUD operations
│   │   ├── api/
│   │   │   ├── main.py          # API router registration
│   │   │   ├── deps.py          # Dependency injection (auth, db)
│   │   │   └── routes/          # API endpoints by domain
│   │   │       ├── login.py     # Password authentication
│   │   │       ├── oauth.py     # OAuth authentication
│   │   │       ├── users.py     # User management
│   │   │       ├── items.py     # Items CRUD
│   │   │       ├── utils.py     # Utility endpoints
│   │   │       └── private.py   # Dev/testing endpoints
│   │   ├── core/
│   │   │   ├── config.py        # Settings (Pydantic Settings)
│   │   │   ├── security.py      # JWT, password hashing, OAuth verification
│   │   │   └── db.py            # Database connection
│   │   ├── alembic/
│   │   │   └── versions/        # Database migrations
│   │   ├── utils.py             # Utilities (email, tokens, etc.)
│   │   └── initial_data.py      # DB seeding (creates superuser)
│   ├── pyproject.toml           # Python dependencies (uv format)
│   ├── .venv/                   # Python virtual environment
│   └── tests/                   # Backend tests
│
├── frontend/                    # React + TypeScript frontend
│   ├── src/
│   │   ├── main.tsx             # App entry point
│   │   ├── routes/              # TanStack Router routes
│   │   │   ├── __root.tsx       # Root layout
│   │   │   ├── login.tsx        # Login page
│   │   │   ├── signup.tsx       # Signup page
│   │   │   └── _layout/         # Protected routes
│   │   │       ├── index.tsx    # Dashboard
│   │   │       ├── settings.tsx # User settings
│   │   │       ├── items.tsx    # Items list
│   │   │       └── admin.tsx    # Admin panel
│   │   ├── components/
│   │   │   ├── Auth/            # Auth components
│   │   │   ├── UserSettings/    # Settings components
│   │   │   ├── Common/          # Shared components
│   │   │   └── ui/              # shadcn/ui components
│   │   ├── hooks/
│   │   │   └── useAuth.ts       # Auth state management
│   │   ├── client/              # AUTO-GENERATED OpenAPI client
│   │   │   ├── sdk.gen.ts       # Service classes
│   │   │   ├── types.gen.ts     # TypeScript types
│   │   │   └── schemas.gen.ts   # Zod schemas
│   │   └── utils.ts             # Utility functions
│   ├── package.json             # Node dependencies
│   └── openapi.json             # Generated OpenAPI spec
│
├── scripts/
│   └── generate-client.sh       # Regenerates frontend OpenAPI client
│
├── .env                         # Environment variables (backend & shared)
└── docker-compose.yml           # Docker setup
```

## Key Concepts

### Backend Architecture

**Framework**: FastAPI with async support

**Models**: SQLModel (combines SQLAlchemy ORM + Pydantic validation)
- Database models defined in `backend/app/models.py`
- Models with `table=True` are database tables
- Models without `table=True` are Pydantic schemas (API request/response)

**Authentication**:
- JWT tokens (HS256, 8-day expiry by default)
- Password hashing with bcrypt
- Google OAuth via Authlib library
- Tokens stored in frontend localStorage

**Dependency Injection** (in `api/deps.py`):
- `SessionDep` - Database session
- `TokenDep` - Extracted JWT token
- `CurrentUser` - Authenticated user object
- `get_current_active_superuser` - Admin-only guard

**Database**:
- PostgreSQL with UUID primary keys
- Alembic for migrations
- Connection via SQLModel engine

### Frontend Architecture

**Framework**: React 18+ with TypeScript

**Routing**: TanStack Router (file-based routing)
- Routes defined in `src/routes/`
- `_layout` prefix = protected routes (requires auth)
- `beforeLoad` = route guards

**State Management**:
- TanStack React Query (no Redux/Zustand)
- Query keys: `["currentUser"]`, etc.
- Mutations for API calls

**API Client**:
- AUTO-GENERATED from backend OpenAPI spec
- Located in `src/client/`
- **DO NOT manually edit** - regenerate instead
- Services: `LoginService`, `UsersService`, `OauthService`, `ItemsService`

**Styling**: Tailwind CSS + shadcn/ui components

**Auth Flow**:
1. User logs in → JWT token returned
2. Token stored in `localStorage` key: `access_token`
3. OpenAPI client automatically includes token in requests
4. Protected routes check `isLoggedIn()` in `beforeLoad`

## Common Development Tasks

### 1. Adding a New API Endpoint

**Backend Steps**:

1. Define Pydantic models in `backend/app/models.py`:
```python
class MyRequest(SQLModel):
    field: str

class MyResponse(SQLModel):
    result: str
```

2. Add CRUD function in `backend/app/crud.py` (if needed):
```python
def my_operation(*, session: Session, data: MyRequest) -> MyResponse:
    # Database logic
    return MyResponse(result="done")
```

3. Create endpoint in `backend/app/api/routes/[domain].py`:
```python
@router.post("/my-endpoint", response_model=MyResponse)
def my_endpoint(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    data: MyRequest
) -> MyResponse:
    return crud.my_operation(session=session, data=data)
```

4. Register router in `backend/app/api/main.py` (if new file):
```python
from app.api.routes import my_new_router
api_router.include_router(my_new_router.router)
```

5. **CRITICAL**: Regenerate frontend client:
```bash
cd /path/to/workflow-runner-core
source backend/.venv/bin/activate
bash scripts/generate-client.sh
```

**Frontend Steps**:

6. Use the auto-generated service:
```typescript
import { MyDomainService } from "@/client"

const mutation = useMutation({
  mutationFn: (data: MyRequest) =>
    MyDomainService.myEndpoint({ requestBody: data }),
  onSuccess: (result) => console.log(result)
})
```

### 2. Database Schema Changes

**Always use Alembic migrations**:

1. Modify models in `backend/app/models.py`

2. Activate virtual environment:
```bash
cd backend
source .venv/bin/activate
```

3. Generate migration:
```bash
alembic revision --autogenerate -m "description of changes"
```

4. Review generated migration in `backend/app/alembic/versions/`

5. Apply migration:
```bash
alembic upgrade head
```

6. For downgrades:
```bash
alembic downgrade -1  # Rollback one migration
```

### 3. Adding a New React Component

1. Create component in appropriate directory:
   - `src/components/Auth/` - Authentication components
   - `src/components/UserSettings/` - Settings pages
   - `src/components/Common/` - Shared components
   - `src/components/ui/` - shadcn/ui components (don't modify)

2. Use TypeScript with proper types from `@/client`

3. Follow existing patterns:
   - Forms: `react-hook-form` + `zod` validation
   - API calls: `useMutation` or `useQuery` from React Query
   - Routing: `useNavigate` from TanStack Router

### 4. Adding a New Route

1. Create file in `src/routes/`:
   - Public route: `src/routes/my-route.tsx`
   - Protected route: `src/routes/_layout/my-route.tsx`

2. Use TanStack Router file-based conventions:
```typescript
import { createFileRoute } from "@tanstack/react-router"

export const Route = createFileRoute("/_layout/my-route")({
  component: MyComponent,
  beforeLoad: async () => {
    // Optional: route guard logic
  },
})

function MyComponent() {
  return <div>My Route</div>
}
```

3. Route will be auto-discovered (no manual registration needed)

## Critical Commands Reference

### Backend

**Activate Virtual Environment** (ALWAYS do this first):
```bash
cd backend
source .venv/bin/activate
```

**Install Dependencies**:
```bash
cd backend
uv sync
```

**Run Development Server**:
```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload
```

**Database Migrations**:
```bash
cd backend
source .venv/bin/activate
alembic upgrade head                    # Apply all migrations
alembic revision --autogenerate -m "msg"  # Create new migration
alembic downgrade -1                    # Rollback one migration
alembic current                         # Show current version
```

**Run Tests**:
```bash
cd backend
source .venv/bin/activate
pytest
```

### Frontend

**Install Dependencies**:
```bash
cd frontend
npm install
```

**Run Development Server**:
```bash
cd frontend
npm run dev
```

**Build for Production**:
```bash
cd frontend
npm run build
```

**Regenerate OpenAPI Client** (after backend changes):
```bash
# From project root
cd /path/to/workflow-runner-core
source backend/.venv/bin/activate
bash scripts/generate-client.sh
```

**What this script does**:
1. Starts in project root
2. Generates OpenAPI spec from backend (`openapi.json`)
3. Moves spec to `frontend/openapi.json`
4. Runs `npm run generate-client` in frontend
5. Regenerates `frontend/src/client/` directory

### Docker

**Start All Services**:
```bash
docker-compose up -d
```

**View Logs**:
```bash
docker-compose logs -f backend
docker-compose logs -f frontend
```

**Rebuild**:
```bash
docker-compose up -d --build
```

## Environment Variables

### Backend (`.env` in project root)

```env
# Domain & Frontend
DOMAIN=localhost
FRONTEND_HOST=http://localhost:5173
ENVIRONMENT=local  # local | staging | production

# Backend
SECRET_KEY=changethis  # CHANGE IN PRODUCTION
FIRST_SUPERUSER=admin@example.com
FIRST_SUPERUSER_PASSWORD=changethis  # CHANGE IN PRODUCTION

# Database
POSTGRES_SERVER=localhost
POSTGRES_PORT=5432
POSTGRES_DB=app
POSTGRES_USER=postgres
POSTGRES_PASSWORD=changethis

# Email (SMTP)
SMTP_HOST=
SMTP_USER=
SMTP_PASSWORD=
EMAILS_FROM_EMAIL=info@example.com
SMTP_TLS=True
SMTP_PORT=587

# Google OAuth
GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET="GOCSPX-your-client-secret"
GOOGLE_REDIRECT_URI="http://localhost:5173/auth/google/callback"

# Sentry (optional)
SENTRY_DSN=
```

### Frontend (`frontend/.env`)

```env
VITE_API_URL=http://localhost:8000
VITE_GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
MAILCATCHER_HOST=http://localhost:1080
```

## Authentication System

### Password Authentication

**Login Flow**:
1. `POST /api/v1/login/access-token` with `username` (email) + `password`
2. Returns JWT token
3. Frontend stores in `localStorage["access_token"]`
4. Token auto-included in requests via OpenAPI client

**Endpoints**:
- `POST /api/v1/login/access-token` - Login
- `POST /api/v1/users/signup` - Signup
- `POST /api/v1/password-recovery/{email}` - Request reset
- `POST /api/v1/reset-password/` - Reset with token

### Google OAuth

**Login Flow**:
1. Frontend: `GET /api/v1/auth/google/authorize` → Get auth URL + state
2. User redirected to Google
3. Google redirects back with `code` + `state`
4. `POST /api/v1/auth/google/callback` with code → JWT token
5. Auto-creates user or auto-links to existing email

**Endpoints**:
- `GET /auth/oauth/config` - Check if OAuth enabled
- `GET /auth/google/authorize` - Get authorization URL
- `POST /auth/google/callback` - Handle callback, return JWT
- `POST /auth/google/link` - Link Google to existing user
- `DELETE /auth/google/unlink` - Unlink Google account

**User Model**:
```python
class User(UserBase, table=True):
    id: uuid.UUID
    email: EmailStr
    hashed_password: str | None  # Nullable for OAuth-only users
    google_id: str | None        # Google user ID
    is_active: bool
    is_superuser: bool
    full_name: str | None
```

## Important Patterns & Conventions

### Backend

**Dependency Injection**:
```python
# Use annotated types for dependencies
from app.api.deps import SessionDep, CurrentUser

@router.get("/endpoint")
def my_endpoint(session: SessionDep, current_user: CurrentUser):
    # session and current_user are auto-injected
    pass
```

**Error Handling**:
```python
from fastapi import HTTPException

raise HTTPException(status_code=400, detail="Error message")
```

**CRUD Pattern**:
- Always use `crud.py` functions for database operations
- Don't write raw SQL in routes
- Use SQLModel's `session.exec(select(...))` pattern

**Model Validation**:
- Request models: inherit from `SQLModel` without `table=True`
- Response models: specified in `response_model=` decorator
- Database models: inherit from base + `table=True`

### Frontend

**API Calls**:
```typescript
// Queries (GET)
const { data } = useQuery({
  queryKey: ["items"],
  queryFn: ItemsService.readItems,
})

// Mutations (POST/PUT/DELETE)
const mutation = useMutation({
  mutationFn: (data) => ItemsService.createItem({ requestBody: data }),
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ["items"] }),
})
```

**Auth State**:
```typescript
import useAuth from "@/hooks/useAuth"

const { user, loginMutation, logoutMutation } = useAuth()

if (!user) return <div>Not logged in</div>
```

**Route Guards**:
```typescript
export const Route = createFileRoute("/_layout/admin")({
  component: Admin,
  beforeLoad: async () => {
    if (!isLoggedIn()) {
      throw redirect({ to: "/login" })
    }
  },
})
```

## Common Issues & Solutions

### Issue: TypeScript errors in frontend after backend changes

**Solution**: Regenerate the OpenAPI client:
```bash
cd /path/to/workflow-runner-core
source backend/.venv/bin/activate
bash scripts/generate-client.sh
```

### Issue: Database schema mismatch

**Solution**:
1. Check current migration version: `alembic current`
2. Apply pending migrations: `alembic upgrade head`
3. If needed, create new migration: `alembic revision --autogenerate -m "msg"`

### Issue: CORS errors in browser

**Solution**: Check `BACKEND_CORS_ORIGINS` in `.env` includes your frontend URL:
```env
BACKEND_CORS_ORIGINS="http://localhost:5173"
```

### Issue: Import errors in Python

**Solution**: Always activate virtual environment first:
```bash
cd backend
source .venv/bin/activate
```

### Issue: Frontend client has wrong service names

**Symptom**: `AuthService` doesn't exist, should be `OauthService`

**Solution**: The OpenAPI client auto-generates service names from route tags. Check:
1. Backend route has correct `tags=["oauth"]` parameter
2. Regenerate client after any backend changes
3. Use the generated service names (check `frontend/src/client/sdk.gen.ts`)

## Testing

IMPORTANT!
Testing Framework is not configured yet. DOT NOT RUN TESTS!

### Backend Tests

Located in `backend/tests/`

**Run all tests**:
```bash
cd backend
source .venv/bin/activate
pytest
```

**Run specific test**:
```bash
pytest tests/api/routes/test_login.py::test_get_access_token
```

**Test utilities** in `backend/tests/utils/`:
- `user.py` - User creation, authentication helpers
- `utils.py` - Random data generators

### Frontend Tests

**Run tests** (if configured):
```bash
cd frontend
npm test
```

## Security Considerations

**Secrets Management**:
- `.env` files are gitignored
- Never commit secrets to git
- Change default passwords in production
- Use strong `SECRET_KEY` (32+ random bytes)

**Authentication**:
- JWT tokens expire after 8 days by default
- Passwords hashed with bcrypt (cost factor 12)
- OAuth state tokens prevent CSRF
- Google ID tokens verified with Google's public keys

**Database**:
- Use parameterized queries (SQLModel handles this)
- Foreign key constraints with CASCADE delete
- Unique constraints on email and google_id

## Additional Resources

**Backend**:
- FastAPI docs: https://fastapi.tiangolo.com/
- SQLModel docs: https://sqlmodel.tiangolo.com/
- Alembic docs: https://alembic.sqlalchemy.org/
- Authlib docs: https://docs.authlib.org/

**Frontend**:
- TanStack Router: https://tanstack.com/router
- TanStack Query: https://tanstack.com/query
- shadcn/ui: https://ui.shadcn.com/
- React Hook Form: https://react-hook-form.com/

## Quick Reference Checklist

When working on this project:

- [ ] Activate virtual environment before backend work
- [ ] Regenerate frontend client after backend API changes
- [ ] Create Alembic migration after model changes
- [ ] Test both backend and frontend after changes
- [ ] Check `.env` files are configured correctly
- [ ] Never commit `.env` files
- [ ] Use TypeScript types from `@/client` in frontend
- [ ] Follow dependency injection pattern in backend
- [ ] Use React Query for all API calls in frontend
- [ ] Update this guide if project structure changes

---

**Last Updated**: 2025-12-21
**Project Version**: Full Stack FastAPI + React with Google OAuth
