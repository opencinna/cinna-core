# Claude Development Guide

This document provides context and instructions for LLM assistants working on this Full Stack FastAPI + React project.

**Quick References:**
- Feature map & business context: `docs/README.md` ← start here when planning or exploring unfamiliar features
- Detailed backend patterns: `docs/development/backend/backend_development_llm.md`
- Common operations: See `Makefile` in project root

## User Shortcuts

- **"read core ..."** — When the user says "read core" (optionally followed by a topic), read `docs/README.md` first. This is the base for documentation discovery; use it to find the relevant feature docs for the topic mentioned.

## Documentation Strategy

`docs/README.md` is the entrypoint to all feature documentation. When planning a task or exploring unfamiliar territory, start there to identify which features are involved and how they relate.

From the README, pick the most relevant feature docs and read those business logic files first. If something remains unclear, follow the integration point links to adjacent features and read those next. Expand only as far as needed — not every feature requires full coverage.

Each feature has two doc types:
- **Business logic file** (`feature_name.md`) — what the feature does, user flows, business rules, integration points
- **Tech file** (`feature_name_tech.md`) — models, routes, service layer, implementation details

Prefer business logic files. Go into tech files only when you already understand what needs to happen and need to know how it is implemented.

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
│   │   ├── models/              # SQLModel database models (organized by domain)
│   │   │   ├── __init__.py      # Re-exports all models for package-level imports
│   │   │   ├── agents/          # Agent, AgentSchedule, AgentHandover
│   │   │   ├── environments/    # AgentEnvironment
│   │   │   ├── sessions/        # Session, Activity
│   │   │   ├── tasks/           # InputTask, TaskComment, TaskAttachment, TaskTrigger, etc.
│   │   │   ├── credentials/     # Credential, AICredential, shares, link_models
│   │   │   ├── a2a/             # AgentAccessToken
│   │   │   ├── mcp/             # MCPConnector, MCPToken, MCPOAuthClient, etc.
│   │   │   ├── knowledge/       # AIKnowledgeGitRepo, KnowledgeArticle
│   │   │   ├── sharing/         # AgentShare, AgentGuestShare, CloneUpdateRequest
│   │   │   ├── agentic_teams/   # AgenticTeam, nodes, connections
│   │   │   ├── webapp/          # AgentWebappShare, AgentWebappInterfaceConfig
│   │   │   ├── files/           # FileUpload
│   │   │   ├── plugins/         # LLMPluginMarketplace, AgentPluginLink
│   │   │   ├── users/           # User, UserWorkspace, UserDashboard, SSHKey
│   │   │   ├── events/          # Event, SecurityEvent
│   │   │   └── email/           # AgentEmailIntegration, MailServerConfig, EmailMessage
│   │   ├── crud.py              # Database CRUD operations
│   │   ├── api/
│   │   │   ├── main.py          # API router registration
│   │   │   ├── deps.py          # Dependency injection (auth, db)
│   │   │   └── routes/          # API endpoints by domain
│   │   ├── core/
│   │   │   ├── config.py        # Settings (Pydantic Settings)
│   │   │   ├── security.py      # JWT, password hashing, OAuth verification
│   │   │   └── db.py            # Database connection
│   │   ├── alembic/
│   │   │   └── versions/        # Database migrations
│   │   ├── services/            # Business logic layer (organized by domain)
│   │   │   ├── agents/          # Agent CRUD, handover, scheduling, commands
│   │   │   ├── environments/    # Lifecycle, status, adapters, connectors
│   │   │   ├── sessions/        # Session, message, streaming
│   │   │   ├── tasks/           # Input tasks, triggers, comments, attachments
│   │   │   ├── credentials/     # Credentials, AI credentials, OAuth, sharing
│   │   │   ├── a2a/             # A2A protocol, request handling, tokens
│   │   │   ├── mcp/             # MCP connectors, OAuth, consent
│   │   │   ├── knowledge/       # Knowledge sources, articles, embeddings, search
│   │   │   ├── sharing/         # Agent cloning, sharing, guest access
│   │   │   ├── agentic_teams/   # Team, node, connection services
│   │   │   ├── webapp/          # Webapp serving, sharing, chat
│   │   │   ├── files/           # File upload, storage, cleanup
│   │   │   ├── plugins/         # Plugin marketplace
│   │   │   ├── users/           # Auth, user, workspace, dashboard, SSH keys
│   │   │   ├── events/          # Event bus, SocketIO, security events, activities
│   │   │   ├── ai_functions/    # AI utility functions (LLM cascade)
│   │   │   └── email/           # IMAP/SMTP, polling, sending, processing
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
- Database models in `backend/app/models/` organized by domain subfolder (e.g., `agents/agent.py`, `users/user.py`)
- `__init__.py` re-exports all models, so `from app.models import Agent` still works
- Direct imports also work: `from app.models.agents.agent import Agent`
- Models with `table=True` are database tables
- Models without `table=True` are Pydantic schemas (API request/response)
- See `docs/development/backend/backend_development_llm.md` for detailed patterns

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

### Adding API Endpoint
1. Add models in `backend/app/models/[domain]/[entity].py` (Base, Public, Update, Create) and re-export in `models/__init__.py`
2. Add route in `backend/app/api/routes/[domain].py` using `SessionDep`, `CurrentUser`
3. Use service layer for business logic (`backend/app/services/[domain]/`)
4. Regenerate client: `bash scripts/generate-client.sh`
5. Use in frontend: `import { ServiceName } from "@/client"`

**Details**: See `docs/development/backend/backend_development_llm.md`

### Database Schema Changes
1. Modify models in `backend/app/models/[domain]/[entity].py`
2. Generate migration: `make migration` or `docker compose exec backend alembic revision --autogenerate -m "desc"`
3. Review & edit migration in `backend/app/alembic/versions/`
4. Apply: `make migrate` or `docker compose exec backend alembic upgrade head`
5. Regenerate frontend client

**Details**: See `Makefile` and `docs/development/backend/backend_development_llm.md`

### Frontend Component Organization
- `src/components/Auth/` - Authentication
- `src/components/UserSettings/` - Settings
- `src/components/Common/` - Shared components
- `src/components/ui/` - shadcn/ui (auto-generated, don't modify)
- Use Card components for grouping (see `AgentPromptsTab.tsx`)
- Forms: `react-hook-form` + `zod`, API: React Query

### Routes
- Public: `src/routes/my-route.tsx`
- Protected: `src/routes/_layout/my-route.tsx`
- File-based routing (TanStack Router) - no manual registration

## Critical Commands

### Migrations (via Docker)
```bash
make migrate                                                 # Apply migrations
make migration                                               # Create new migration
docker compose exec backend alembic current                  # Check version
```

### Client Generation (after backend changes)
```bash
source ./backend/.venv/bin/activate && make gen-client       # From project root
```
Regenerates `frontend/src/client/` from backend OpenAPI spec

### Type Checking (Frontend)
No `npm run typecheck` command exists. Use `npx tsc --noEmit` with grep to check specific files:
```bash
cd frontend && npx tsc --noEmit 2>&1 | grep -E "(ComponentA|ComponentB)" | head -20
```
Replace `ComponentA|ComponentB` with the actual component/file names you're checking. Avoid running full typecheck on entire codebase.

### Development
```bash
# Backend
cd backend && source .venv/bin/activate && uvicorn app.main:app --reload

# Frontend
cd frontend && npm run dev

# Docker
docker compose up -d                                         # Start all
docker compose logs -f backend                               # View logs
```

### Makefile Commands
See `Makefile` for: `make prestart`, `make migrate`, `make migration`, `make dev-tunnel`

## Environment Variables
- Backend `.env` in project root: `SECRET_KEY`, `POSTGRES_*`, `GOOGLE_CLIENT_ID/SECRET`, `FRONTEND_HOST`
- Frontend `frontend/.env`: `VITE_API_URL`, `VITE_GOOGLE_CLIENT_ID`
- Never commit `.env` files

## Authentication
- JWT tokens in `localStorage["access_token"]`, auto-included by OpenAPI client
- Password auth: `/api/v1/login/access-token`, `/api/v1/users/signup`
- Google OAuth: `/auth/google/authorize`, `/auth/google/callback`
- User model: `email`, `hashed_password` (nullable), `google_id` (nullable)

## Important Patterns

### Backend
- Use `SessionDep`, `CurrentUser` for dependency injection
- Service layer (`backend/app/services/`) for business logic
- Models in `backend/app/models/[domain]/` subfolders; re-exported via `__init__.py`
- Services in `backend/app/services/[domain]/` subfolders
- Models: Base (shared), Database (`table=True`), Public (API response), Update/Create (API input)
- See `docs/development/backend/backend_development_llm.md` for detailed patterns

### Frontend
- React Query: `useQuery` for GET, `useMutation` for POST/PUT/DELETE
- Auth: `useAuth()` hook from `@/hooks/useAuth`
- Routes: Protected routes in `_layout/`, use `beforeLoad` for guards
- Types: Always use auto-generated types from `@/client`

## Common Issues

- **TypeScript errors after backend changes**: Regenerate client (`bash scripts/generate-client.sh`)
- **Database schema mismatch**: Check version (`docker compose exec backend alembic current`), apply migrations (`make migrate`)
- **CORS errors**: Add frontend URL to `BACKEND_CORS_ORIGINS` in `.env`
- **Wrong service names**: Check route `tags` parameter, service names auto-generated from tags

## Testing

Tests run inside Docker. **Before writing any backend tests, read `backend/tests/README.md`** for architecture, fixtures, conventions, and rules (API-only tests, no direct DB access, scenario-based structure, test utility patterns). Also check for domain-specific `README.md` files in the target test directory (e.g., `tests/api/agents/README.md`).

```bash
# Run all backend tests
make test-backend

# Run a specific test file or directory
docker compose exec backend python -m pytest tests/api/agents/agents_email_integration_test.py -v
docker compose exec backend python -m pytest tests/api/agents/ -v
```

**Prerequisites**: Docker services must be running (`make up` or `docker compose up -d`).

## Security
- `.env` files gitignored, never commit secrets
- JWT tokens (8-day expiry), bcrypt password hashing
- OAuth with state tokens, Google ID verification
- SQLModel handles parameterized queries

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

- [ ] Database operations require Docker (`docker compose up -d db`)
- [ ] Use `make migrate` or Docker commands for migrations (not direct alembic)
- [ ] Models are in `backend/app/models/` (separate files per entity)
- [ ] Regenerate frontend client after backend API changes (`bash scripts/generate-client.sh`)
- [ ] Create Alembic migration after model changes (via Docker)
- [ ] Test both backend and frontend after changes
- [ ] Check `.env` files are configured correctly
- [ ] Never commit `.env` files
- [ ] Use TypeScript types from `@/client` in frontend
- [ ] Follow dependency injection pattern in backend
- [ ] Use React Query for all API calls in frontend
- [ ] Reference `Makefile` for common operations
- [ ] See `docs/development/backend/backend_development_llm.md` for detailed patterns
- [ ] See `docs/development/frontend/frontend_development_llm.md` for detailed frontend development patterns
- [ ] See `backend/tests/README.md` for detailed backend test writing patterns and recommendations

---

**Last Updated**: 2026-02-26
**Project Version**: Full Stack FastAPI + React with Google OAuth
