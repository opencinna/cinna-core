# Backend Development - LLM Quick Reference

## Project Structure
- **Models**: `backend/app/models/` (organized by domain subfolder, e.g., `models/agents/agent.py`, `models/users/user.py`)
- **Routes**: `backend/app/api/routes/` (one file per domain)
- **Services**: `backend/app/services/` (organized by domain subfolder, e.g., `services/agents/agent_service.py`, `services/environments/environment_service.py`)
- **Migrations**: `backend/app/alembic/versions/`

## Database Models (SQLModel)
```python
# backend/app/models/agents/agent.py

# Base schema (shared properties)
class AgentBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)

# Database table (table=True is REQUIRED)
class Agent(AgentBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    ui_color_preset: str | None = Field(default="slate")  # nullable with default
    created_at: datetime = Field(default_factory=datetime.utcnow)

# API response model (no table=True)
class AgentPublic(SQLModel):
    id: uuid.UUID
    name: str
    ui_color_preset: str | None
    created_at: datetime

# Update model (all fields optional)
class AgentUpdate(SQLModel):
    name: str | None = None
    ui_color_preset: str | None = None
```

## Database Migrations (Alembic)

### ALWAYS activate venv first:
```bash
cd backend
source .venv/bin/activate
```

### Generate migration:
```bash
alembic revision --autogenerate -m "add ui_color_preset to agent"
```

### Review & edit migration file:
- Located in `backend/app/alembic/versions/`
- Add data migrations if needed (e.g., set defaults for existing rows)
```python
def upgrade():
    op.add_column('agent', sa.Column('ui_color_preset', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    # Set default for existing rows
    op.execute("UPDATE agent SET ui_color_preset = 'slate' WHERE ui_color_preset IS NULL")
```

### Apply migration:
```bash
alembic upgrade head
```

### Check current version:
```bash
alembic current
```

### Rollback one migration:
```bash
alembic downgrade -1
```

## API Routes Pattern
```python
# backend/app/api/routes/agents.py
from app.api.deps import CurrentUser, SessionDep

@router.put("/{id}", response_model=AgentPublic)
def update_agent(
    *,
    session: SessionDep,          # DB session (auto-injected)
    current_user: CurrentUser,    # Authenticated user (auto-injected)
    id: uuid.UUID,
    agent_in: AgentUpdate,
) -> Any:
    """Update an agent."""
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Permission check
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Use service layer
    updated_agent = AgentService.update_agent(
        session=session, agent_id=id, data=agent_in
    )
    return updated_agent
```

## Services Pattern
```python
# backend/app/services/agents/agent_service.py
from sqlmodel import Session, select

class AgentService:
    @staticmethod
    def update_agent(session: Session, agent_id: UUID, data: AgentUpdate) -> Agent | None:
        agent = session.get(Agent, agent_id)
        if not agent:
            return None

        # model_dump(exclude_unset=True) only updates provided fields
        update_dict = data.model_dump(exclude_unset=True)
        agent.sqlmodel_update(update_dict)

        session.add(agent)
        session.commit()
        session.refresh(agent)
        return agent
```

## Frontend Client Regeneration

### CRITICAL: Run after ANY backend model/route changes:
```bash
cd /path/to/workflow-runner-core
source backend/.venv/bin/activate
bash scripts/generate-client.sh
```

### What it does:
1. Generates OpenAPI spec from backend
2. Moves to `frontend/openapi.json`
3. Runs `npm run generate-client`
4. Updates `frontend/src/client/` (types, services, schemas)

### Files auto-generated (DO NOT EDIT):
- `frontend/src/client/types.gen.ts` - TypeScript types
- `frontend/src/client/sdk.gen.ts` - Service classes
- `frontend/src/client/schemas.gen.ts` - Zod schemas

## Dependency Injection (FastAPI)
- `SessionDep` - Database session
- `CurrentUser` - Authenticated user (requires valid JWT)
- `TokenDep` - Raw JWT token
- `get_current_active_superuser` - Admin-only guard

## Common Patterns

### Update model fields:
1. Edit model in `backend/app/models/<domain>/` subfolder <!-- nocheck -->
2. Add field to `Agent`, `AgentPublic`, `AgentUpdate` classes
3. Generate migration: `alembic revision --autogenerate -m "..."`
4. Review and edit migration file if needed
5. Apply: `alembic upgrade head`
6. Regenerate client: `bash scripts/generate-client.sh`

### Add new endpoint:
1. Add route in `backend/app/api/routes/[domain].py`
2. Use service layer for business logic
3. Regenerate client: `bash scripts/generate-client.sh`
4. Use in frontend: `import { ServiceName } from "@/client"`

## Package Management
- **Backend**: `uv` (not pip)
- Install: `cd backend && uv sync`
- Add package: `uv add package-name`

## Environment Variables
- `.env` in project root
- Loaded by `backend/app/core/config.py`
- Access via `settings` object from `app.core.config`
