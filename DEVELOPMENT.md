# Development Guide

## Prerequisites

- Docker and Docker Compose
- Node.js 18+
- Python 3.11+ with [uv](https://docs.astral.sh/uv/)

## Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/opencinna/cinna-core.git
   cd cinna-core
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   cp frontend/.env.example frontend/.env
   # Edit .env files with your settings (database, OAuth, etc.)
   ```

3. **Start all services**
   ```bash
   make up
   ```
   This starts the backend (with auto-reload), frontend, PostgreSQL, Adminer, and MailCatcher.

4. **Run initial setup** (first time only)
   ```bash
   make prestart
   ```
   This runs database migrations and creates the initial superuser.

5. **Open the app** at http://localhost:5173

## Service URLs

| Service | URL |
|---------|-----|
| Frontend | http://localhost:5173 |
| Backend API | http://localhost:8000 |
| Swagger UI | http://localhost:8000/docs |
| Adminer (DB UI) | http://localhost:8099 |
| MailCatcher | http://localhost:1080 |

Run `make h` to print these at any time.

## Development Workflow

### Backend

The backend container runs with `--reload` by default (via `docker-compose.override.yml`), so code changes are picked up automatically. Source files are mounted from `backend/app/` into the container.

```bash
make logs-back          # View backend logs
make shell              # Open a shell in the backend container
```

### Frontend

The Docker frontend container serves a static build. For local development with hot-reload:

```bash
make dev-front          # Stops frontend container, starts local Node dev server
```

This runs `npm run dev` from the `frontend/` directory with HMR on http://localhost:5173.

### API Client Generation

After changing backend API endpoints, regenerate the frontend TypeScript client:

```bash
source ./backend/.venv/bin/activate && make gen-client
```

This updates the auto-generated files in `frontend/src/client/` from the backend OpenAPI spec. Never edit these files manually.

### Type Checking

```bash
cd frontend && npx tsc --noEmit 2>&1 | grep -E "(ComponentA|ComponentB)" | head -20
```

Replace `ComponentA|ComponentB` with the files you're checking. Full-codebase typecheck is slow; filter to relevant files.

## Database

### Migrations

```bash
make migrate            # Apply pending migrations (alembic upgrade head)
make migration          # Create a new migration (prompts for name)
```

Migrations run inside the backend container via Alembic. After modifying models in `backend/app/models/`, always create and review a migration before applying.

```bash
docker compose exec backend alembic current    # Check current migration version
docker compose exec backend alembic history    # View migration history
```

## Testing

Tests run inside Docker. See `backend/tests/README.md` for conventions.

```bash
make test-backend                              # Run all backend tests
docker compose exec backend python -m pytest tests/api/agents/webapp/ -v   # Run a topic group
docker compose exec backend python -m pytest tests/api/agents/ -v   # Run a whole domain (610 tests)
```

## Project Structure

```
cinna-core/
├── backend/                 # FastAPI backend
│   ├── app/
│   │   ├── models/          # SQLModel database models (by domain)
│   │   ├── api/routes/      # API endpoints
│   │   ├── services/        # Business logic layer
│   │   └── alembic/         # Database migrations
│   └── tests/               # Backend tests
├── frontend/                # React + TypeScript frontend
│   └── src/
│       ├── routes/          # TanStack Router (file-based)
│       ├── components/      # UI components
│       ├── hooks/           # React hooks
│       └── client/          # Auto-generated OpenAPI client
├── docs/                    # Feature documentation
├── docker-compose.yml       # Production compose
├── docker-compose.override.yml  # Dev overrides (auto-reload, DB, tools)
└── Makefile                 # Common commands (run `make` to see all)
```

## Makefile Reference

Run `make` or `make help` to see all available commands. Key ones:

| Command | Description |
|---------|-------------|
| `make up` | Start all services |
| `make down` | Stop all services |
| `make dev-front` | Stop frontend container, start local Node dev server |
| `make logs-back` | Stream backend logs |
| `make shell` | Open shell in backend container |
| `make migrate` | Apply database migrations |
| `make migration` | Create new migration |
| `make gen-client` | Regenerate frontend API client |
| `make test-backend` | Run backend test suite |
| `make build` | Build Docker images |
| `make dev-tunnel` | Start public tunnel to local backend |

## Tunnels

For testing integrations that require a public URL (OAuth callbacks, webhooks, MCP):

```bash
make dev-tunnel         # General tunnel to local backend
make mcp-tunnel         # MCP-specific tunnel (follow printed instructions)
```
