# LLM Development Guidelines for Workflow Runner Core

This document contains project-specific patterns, commands, and pitfalls for LLM assistants working on this codebase.

## Project Structure

```
workflow-runner-core/
├── backend/                      # FastAPI backend
│   ├── app/
│   │   ├── models/              # Domain-based model files (NEW structure)
│   │   │   ├── __init__.py      # Re-exports all models
│   │   │   ├── user.py
│   │   │   ├── item.py
│   │   │   ├── agent.py
│   │   │   ├── credential.py
│   │   │   ├── link_models.py   # Many-to-many link tables
│   │   │   ├── environment.py
│   │   │   └── session.py
│   │   ├── services/            # Business logic layer (NEW)
│   │   │   ├── agent_service.py
│   │   │   ├── environment_service.py
│   │   │   ├── session_service.py
│   │   │   └── message_service.py
│   │   ├── api/
│   │   │   ├── routes/
│   │   │   │   ├── agents.py
│   │   │   │   ├── credentials.py
│   │   │   │   ├── environments.py  # NEW
│   │   │   │   ├── sessions.py      # NEW
│   │   │   │   └── messages.py      # NEW
│   │   │   └── main.py          # Router registration
│   │   ├── crud.py              # DEPRECATED for new code
│   │   └── core/
│   │       ├── config.py
│   │       ├── security.py
│   │       └── db.py
│   └── .venv/
└── frontend/                     # React frontend
    └── src/
        ├── client/              # Auto-generated OpenAPI client
        └── routes/
```

**CRITICAL**: Current working directory is `/Users/evgenyl/dev/ml-llm/workflow-runner-core/backend`

**Architecture Pattern**:
- **Models** in `app/models/` - Database entities and Pydantic schemas (domain-based files)
- **Services** in `app/services/` - Business logic (NEW: use for complex operations)
- **Routes** in `app/api/routes/` - HTTP endpoints (lightweight, delegate to services)
- **CRUD** in `app/crud.py` - DEPRECATED for new code (use services instead)

**MIGRATION NOTE (Dec 2023)**: Models refactored from single `models.py` to `models/` directory. All imports remain backward compatible via `__init__.py`. No code changes required for existing imports like `from app.models import User, Agent`.

## Command Execution Patterns

### Backend Commands
```bash
# ALWAYS activate venv first
source .venv/bin/activate

# Test imports (verify no SQLAlchemy errors)
python -c "from app.main import app; print('✓ Import successful')"

# Migrations
alembic revision --autogenerate -m "description"
alembic upgrade head
alembic current  # check status
```

### Frontend Commands
```bash
# From project root (NOT from backend/)
cd /Users/evgenyl/dev/ml-llm/workflow-runner-core
source backend/.venv/bin/activate
bash scripts/generate-client.sh  # Regenerate OpenAPI client

# From frontend/
npm run build  # Check TypeScript errors
```

### Common Pitfall: Directory Context
- `pwd` shows `/Users/evgenyl/dev/ml-llm/workflow-runner-core/backend`
- To run frontend commands, use absolute paths or `cd` to project root first
- Don't use `cd backend` from backend/ (already there)

## Adding New Entities (Full Stack)

### 1. Backend Models (`backend/app/models/`)

**IMPORTANT**: Models are now organized in domain-based files, not a single monolithic file.

**Structure**:
```
backend/app/models/
├── __init__.py           # Re-exports all models for backward compatibility
├── user.py               # User, auth, OAuth models
├── item.py               # Item models
├── agent.py              # Agent models
├── credential.py         # Credential models
├── link_models.py        # Many-to-many link tables (e.g., AgentCredentialLink)
├── environment.py        # AgentEnvironment models
└── session.py            # Session, SessionMessage models
```

**When adding a new entity**:
1. Create a new file `backend/app/models/your_entity.py`
2. Define models following the pattern below
3. Export models in `backend/app/models/__init__.py`

**Pattern in `backend/app/models/entity.py`**:
```python
import uuid
from sqlmodel import Field, Relationship, SQLModel
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .user import User

# Shared properties
class EntityBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)

# Create schema
class EntityCreate(EntityBase):
    pass

# Update schema
class EntityUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)

# Database model
class Entity(EntityBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, ondelete="CASCADE")
    owner: "User | None" = Relationship(back_populates="entities")  # Use quotes with TYPE_CHECKING import

# Public schema
class EntityPublic(EntityBase):
    id: uuid.UUID
    owner_id: uuid.UUID

class EntitiesPublic(SQLModel):
    data: list[EntityPublic]
    count: int
```

**Then export in `backend/app/models/__init__.py`**:
```python
from .entity import (
    Entity,
    EntityCreate,
    EntityUpdate,
    EntityPublic,
    EntitiesPublic,
)

__all__ = [
    # ... existing exports ...
    # Entities
    "Entity",
    "EntityCreate",
    "EntityUpdate",
    "EntityPublic",
    "EntitiesPublic",
]
```

**CRITICAL SQLAlchemy Relationship Pattern with Domain Files**:
- ✅ Use `TYPE_CHECKING` import to avoid circular imports
- ✅ Use quoted type hints for relationships: `owner: "User | None"`
- ✅ Import actual model only in `TYPE_CHECKING` block
- ✅ In both models use quotes: `entities: list["Entity"]` and `owner: "User | None"`
- ❌ NEVER import models at module level if they create circular dependencies
- ❌ WRONG: `owner: User | None` without TYPE_CHECKING - causes import errors

**For many-to-many relationships**, create link model in `backend/app/models/link_models.py`:
```python
import uuid
from sqlmodel import Field, SQLModel

class EntityCategoryLink(SQLModel, table=True):
    __tablename__ = "entity_category_link"
    entity_id: uuid.UUID = Field(foreign_key="entity.id", primary_key=True, ondelete="CASCADE")
    category_id: uuid.UUID = Field(foreign_key="category.id", primary_key=True, ondelete="CASCADE")
```

Then import in both related models:
```python
from .link_models import EntityCategoryLink

categories: list["Category"] = Relationship(back_populates="entities", link_model=EntityCategoryLink)
```

### 2. Service Layer (`backend/app/services/`)

**NEW**: Business logic should be in service classes, not routes or CRUD.

**Pattern in `backend/app/services/entity_service.py`**:
```python
from uuid import UUID
from sqlmodel import Session, select
from app.models import Entity, EntityCreate, EntityUpdate

class EntityService:
    @staticmethod
    def create_entity(session: Session, user_id: UUID, data: EntityCreate) -> Entity:
        """Create new entity"""
        entity = Entity.model_validate(data, update={"owner_id": user_id})
        session.add(entity)
        session.commit()
        session.refresh(entity)
        return entity

    @staticmethod
    def get_entity(session: Session, entity_id: UUID) -> Entity | None:
        """Get entity by ID"""
        return session.get(Entity, entity_id)

    @staticmethod
    def update_entity(session: Session, entity_id: UUID, data: EntityUpdate) -> Entity | None:
        """Update entity"""
        entity = session.get(Entity, entity_id)
        if not entity:
            return None

        update_dict = data.model_dump(exclude_unset=True)
        entity.sqlmodel_update(update_dict)

        session.add(entity)
        session.commit()
        session.refresh(entity)
        return entity

    @staticmethod
    def delete_entity(session: Session, entity_id: UUID) -> bool:
        """Delete entity"""
        entity = session.get(Entity, entity_id)
        if not entity:
            return False

        session.delete(entity)
        session.commit()
        return True
```

**Call services from routes, NOT CRUD directly**:
```python
from app.services.entity_service import EntityService

@router.post("/", response_model=EntityPublic)
def create_entity(*, session: SessionDep, current_user: CurrentUser, entity_in: EntityCreate):
    entity = EntityService.create_entity(session=session, user_id=current_user.id, data=entity_in)
    return entity
```

### 3. CRUD Operations (`backend/app/crud.py`) - DEPRECATED FOR NEW CODE

```python
def create_entity(*, session: Session, entity_in: EntityCreate, owner_id: uuid.UUID) -> Entity:
    db_entity = Entity.model_validate(entity_in, update={"owner_id": owner_id})
    session.add(db_entity)
    session.commit()
    session.refresh(db_entity)
    return db_entity
```

### 3. API Routes (`backend/app/api/routes/entities.py`)

**Standard CRUD pattern**:
```python
router = APIRouter(prefix="/entities", tags=["entities"])

@router.get("/", response_model=EntitiesPublic)
def read_entities(session: SessionDep, current_user: CurrentUser, skip: int = 0, limit: int = 100):
    # Superuser sees all, regular users see only their own
    if current_user.is_superuser:
        count_statement = select(func.count()).select_from(Entity)
        statement = select(Entity).offset(skip).limit(limit)
    else:
        count_statement = select(func.count()).select_from(Entity).where(Entity.owner_id == current_user.id)
        statement = select(Entity).where(Entity.owner_id == current_user.id).offset(skip).limit(limit)

    count = session.exec(count_statement).one()
    entities = session.exec(statement).all()
    return EntitiesPublic(data=entities, count=count)

@router.post("/", response_model=EntityPublic)
def create_entity(*, session: SessionDep, current_user: CurrentUser, entity_in: EntityCreate):
    entity = Entity.model_validate(entity_in, update={"owner_id": current_user.id})
    session.add(entity)
    session.commit()
    session.refresh(entity)
    return entity

# PUT, DELETE follow same permission pattern (check owner_id or is_superuser)
```

**Register in `backend/app/api/main.py`**:
```python
from app.api.routes import entities, environments, sessions, messages  # Add all new routes
api_router.include_router(entities.router)
api_router.include_router(environments.router)  # If environment-related
api_router.include_router(sessions.router)      # If session-related
api_router.include_router(messages.router)      # If message-related
```

### 4. Database Migration

```bash
source .venv/bin/activate
alembic revision --autogenerate -m "Add entities table"
alembic upgrade head
```

### 5. Regenerate Frontend Client

```bash
cd /Users/evgenyl/dev/ml-llm/workflow-runner-core
source backend/.venv/bin/activate
bash scripts/generate-client.sh
```

### 6. Frontend Components

**Directory structure**:
```
frontend/src/components/Entities/
├── columns.tsx           # DataTable columns
├── AddEntity.tsx         # Create dialog
├── EditEntity.tsx        # Edit dialog
├── DeleteEntity.tsx      # Delete confirmation
└── EntityActionsMenu.tsx # Dropdown menu
```

**columns.tsx pattern**:
```typescript
export const columns: ColumnDef<EntityPublic>[] = [
  {
    accessorKey: "id",
    header: "ID",
    cell: ({ row }) => <CopyId id={row.original.id} />,
  },
  {
    accessorKey: "name",
    header: "Name",
    cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
  },
  {
    id: "actions",
    cell: ({ row }) => <EntityActionsMenu entity={row.original} />,
  },
]
```

**Form handling (AddEntity.tsx)**:
```typescript
const mutation = useMutation({
  mutationFn: (data: EntityCreate) =>
    EntitiesService.createEntity({ requestBody: data }),
  onSuccess: () => {
    showSuccessToast("Entity created successfully")
    form.reset()
    setIsOpen(false)
  },
  onError: handleError.bind(showErrorToast),  // CORRECT binding
  onSettled: () => {
    queryClient.invalidateQueries({ queryKey: ["entities"] })
  },
})
```

**TypeScript Form Field Type Assertions** (when using dynamic fields):
```typescript
// For Input components with dynamic credential_data fields
<Input {...field} value={field.value as string} />
<Input type="number" {...field} value={field.value as number} />
<Checkbox checked={field.value as boolean} />
<Textarea {...field} value={field.value as string} />
```

### 7. Frontend Route (`frontend/src/routes/_layout/entities.tsx`)

```typescript
import { useSuspenseQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Icon } from "lucide-react"
import { Suspense } from "react"

import { EntitiesService } from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import AddEntity from "@/components/Entities/AddEntity"
import { columns } from "@/components/Entities/columns"
import PendingItems from "@/components/Pending/PendingItems"

function getEntitiesQueryOptions() {
  return {
    queryFn: () => EntitiesService.readEntities({ skip: 0, limit: 100 }),
    queryKey: ["entities"],
  }
}

export const Route = createFileRoute("/_layout/entities")({
  component: Entities,
})

function Entities() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Entities</h1>
          <p className="text-muted-foreground">Description</p>
        </div>
        <AddEntity />
      </div>
      <Suspense fallback={<PendingItems />}>
        <EntitiesTableContent />
      </Suspense>
    </div>
  )
}
```

### 8. Add Menu Item (`frontend/src/components/Sidebar/AppSidebar.tsx`)

```typescript
import { Icon } from "lucide-react"

const baseItems: Item[] = [
  { icon: Home, title: "Dashboard", path: "/" },
  { icon: Icon, title: "Entities", path: "/entities" },
]
```

## Encryption Pattern (for sensitive fields)

### Backend Setup

**1. Add encryption utilities (`backend/app/core/security.py`)**:
```python
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # CORRECT import

def _get_cipher() -> Fernet:
    key_bytes = settings.ENCRYPTION_KEY.encode()
    kdf = PBKDF2HMAC(  # NOT PBKDF2
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"credentials_salt",
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(key_bytes))
    return Fernet(key)

def encrypt_field(value: str) -> str:
    if not value:
        return value
    cipher = _get_cipher()
    return cipher.encrypt(value.encode()).decode()

def decrypt_field(encrypted_value: str) -> str:
    if not encrypted_value:
        return encrypted_value
    cipher = _get_cipher()
    return cipher.decrypt(encrypted_value.encode()).decode()
```

**2. Model with encrypted field**:
```python
from sqlmodel import Column, Text

class SecureEntity(EntityBase, table=True):
    encrypted_data: str = Field(sa_column=Column(Text, nullable=False))
```

**3. CRUD with encryption**:
```python
import json

def create_secure_entity(*, session: Session, entity_in: SecureEntityCreate, owner_id: uuid.UUID):
    data_json = json.dumps(entity_in.sensitive_data)
    encrypted = encrypt_field(data_json)

    db_entity = SecureEntity(
        name=entity_in.name,
        encrypted_data=encrypted,
        owner_id=owner_id,
    )
    session.add(db_entity)
    session.commit()
    session.refresh(db_entity)
    return db_entity

def get_with_decrypted_data(*, session: Session, entity: SecureEntity) -> dict:
    decrypted_json = decrypt_field(entity.encrypted_data)
    return json.loads(decrypted_json)
```

**4. API endpoint for decrypted data**:
```python
@router.get("/{id}/with-data", response_model=EntityWithData)
def read_with_data(session: SessionDep, current_user: CurrentUser, id: uuid.UUID):
    entity = session.get(Entity, id)
    # ... permission checks ...
    data = crud.get_with_decrypted_data(session=session, entity=entity)
    return EntityWithData(..., sensitive_data=data)
```

## Common Mistakes to Avoid

### 1. SQLAlchemy Relationships with Domain-Based Models
❌ `owner: User | None` without TYPE_CHECKING import - causes circular import errors
✅ `owner: "User | None"` with TYPE_CHECKING import - correct pattern for domain files
❌ Importing models at module level between domain files - causes circular dependencies
✅ Using `from typing import TYPE_CHECKING` and importing models only inside the block
✅ Using quoted type hints for all relationship types: `list["Entity"]`, `"User | None"`

### 2. Cryptography Import
❌ `from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2`
✅ `from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC`

### 3. Form Error Handling
❌ `onError: (error) => handleError(error, showErrorToast)`
✅ `onError: handleError.bind(showErrorToast)`

### 4. Directory Navigation
❌ `cd backend` when already in backend/
✅ Use absolute paths or check `pwd` first
✅ For frontend: `cd /Users/evgenyl/dev/ml-llm/workflow-runner-core`

### 5. Foreign Key Constraints
✅ Always use `ondelete="CASCADE"` in Field definition
✅ Match relationship `cascade_delete=True` in parent model

### 6. Permission Checks
✅ Always check `current_user.is_superuser OR entity.owner_id == current_user.id`
✅ Use same pattern in list endpoints (filter by owner_id for non-superusers)

### 7. Reserved SQLModel Field Names
❌ `metadata` - Reserved by SQLAlchemy/SQLModel
✅ `session_metadata`, `message_metadata`, `entity_metadata` - Use prefixed names
❌ `type` as a model name - Reserved Python keyword
✅ Use `entity_type`, `credential_type`, etc.

## Testing Checklist

After implementing a new entity:

1. ✅ Backend imports successfully: `python -c "from app.main import app"`
2. ✅ Migration applied: `alembic current` shows latest
3. ✅ OpenAPI client regenerated: Check `frontend/src/client/sdk.gen.ts` has new service
4. ✅ TypeScript compiles: `npm run build` (from frontend/)
5. ✅ Menu item visible in sidebar
6. ✅ Can create, read, update, delete entity via UI

## Quick Reference Commands

```bash
# Start from backend/
source .venv/bin/activate

# Backend: Add entity
# 1. Create backend/app/models/entity.py with models
# 2. Export models in backend/app/models/__init__.py
# 3. Create backend/app/services/entity_service.py with business logic
# 4. Create backend/app/api/routes/entities.py with endpoints
# 5. Register router in backend/app/api/main.py
alembic revision --autogenerate -m "Add entity"
alembic upgrade head
python -c "from app.main import app"  # Verify

# Frontend: Regenerate client
cd /Users/evgenyl/dev/ml-llm/workflow-runner-core
source backend/.venv/bin/activate
bash scripts/generate-client.sh

# Frontend: Create components
# 4. Create components/Entity/ directory
# 5. Create route in routes/_layout/entity.tsx
# 6. Add menu item in Sidebar/AppSidebar.tsx

# Verify
cd frontend && npm run build
```

## File Naming Conventions

- Backend routes: `snake_case.py` (e.g., `credentials.py`)
- Frontend components: `PascalCase.tsx` (e.g., `AddCredential.tsx`)
- Frontend routes: `lowercase.tsx` (e.g., `credentials.tsx`)
- Model classes: `PascalCase` (e.g., `Credential`, `CredentialCreate`)
- Route prefixes: `/lowercase` (e.g., `/credentials`)
- Service names: `PascalCase` (e.g., `CredentialsService`)

## OpenAPI Client Auto-Generation

**NEVER manually edit** `frontend/src/client/` - it's auto-generated.

Changes flow: Backend routes → OpenAPI spec → Frontend client

After ANY backend API changes:
```bash
cd /Users/evgenyl/dev/ml-llm/workflow-runner-core
source backend/.venv/bin/activate
bash scripts/generate-client.sh
```

## UI Implementation Patterns: Cards vs Tables

### When to Use Each Pattern

**Use Card-Based Grid Layout**:
- Visual, browsable content (credentials, agents, projects)
- Emphasis on individual items
- Less than 50-100 items typically
- Rich metadata per item (icons, badges, descriptions)
- Mobile-friendly experience needed

**Use DataTable Layout**:
- Large datasets (100+ items)
- Emphasis on sorting, filtering, searching
- Tabular data with many columns
- Need for bulk operations
- Export/reporting functionality

### Card-Based UI Implementation (Reference: Credentials, Agents)

#### 1. Route Structure

**CRITICAL Pattern**: Use nested route structure for detail pages

```
frontend/src/routes/_layout/
├── entities.tsx                  # List page (card grid)
└── entity/
    └── $entityId.tsx             # Detail page
```

❌ **WRONG**: `entities.$id.tsx` - Causes routing issues with TanStack Router
✅ **CORRECT**: `entity/$entityId.tsx` - Works properly with TanStack Router

**Example from credentials implementation**:
```
frontend/src/routes/_layout/
├── credentials.tsx               # Card grid
└── credential/
    └── $credentialId.tsx         # Detail form
```

#### 2. Card Component Pattern

**File**: `frontend/src/components/Entities/EntityCard.tsx`

```typescript
import { Link } from "@tanstack/react-router"
import { Icon } from "lucide-react"

import type { EntityPublic } from "@/client"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"

interface EntityCardProps {
  entity: EntityPublic
}

export function EntityCard({ entity }: EntityCardProps) {
  return (
    <Card className="relative transition-all hover:shadow-md hover:-translate-y-0.5">
      <Link
        to="/entity/$entityId"
        params={{ entityId: entity.id }}
        className="block"
      >
        <CardHeader className="pb-3">
          <div className="flex items-start gap-3 mb-2">
            <div className="rounded-lg bg-primary/10 p-2 text-primary">
              <Icon className="h-5 w-5" />
            </div>
            <div className="flex-1 min-w-0">
              <CardTitle className="text-lg break-words">
                {entity.name}
              </CardTitle>
            </div>
          </div>
          {entity.description && (
            <CardDescription className="line-clamp-2 min-h-[2.5rem]">
              {entity.description}
            </CardDescription>
          )}
        </CardHeader>

        <CardContent>
          <div className="flex items-center gap-2">
            <Badge variant="secondary">{entity.type}</Badge>
          </div>
        </CardContent>
      </Link>
    </Card>
  )
}
```

**Key patterns**:
- ✅ Entire card wrapped in `<Link>` for clickability
- ✅ `break-words` on title (not `truncate`) - allows text to wrap
- ✅ Conditional rendering for optional fields (no "No description" text)
- ✅ Hover effects: `hover:shadow-md hover:-translate-y-0.5`
- ✅ Icon with colored background: `bg-primary/10 p-2 text-primary`
- ✅ `line-clamp-2` for descriptions with min height
- ❌ NO dropdown menu on cards - use detail page for actions

#### 3. List Page with Card Grid

**File**: `frontend/src/routes/_layout/entities.tsx`

```typescript
import { useQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Icon } from "lucide-react"

import { EntitiesService } from "@/client"
import AddEntity from "@/components/Entities/AddEntity"
import { EntityCard } from "@/components/Entities/EntityCard"
import PendingItems from "@/components/Pending/PendingItems"

export const Route = createFileRoute("/_layout/entities")({
  component: Entities,
})

function EntitiesGrid() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["entities"],
    queryFn: async () => {
      const response = await EntitiesService.readEntities({
        skip: 0,
        limit: 100,
      })
      return response
    },
  })

  if (isLoading) {
    return <PendingItems />
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">
          Error loading entities: {(error as Error).message}
        </p>
      </div>
    )
  }

  const entities = data?.data || []

  if (entities.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center text-center py-12">
        <div className="rounded-full bg-muted p-4 mb-4">
          <Icon className="h-8 w-8 text-muted-foreground" />
        </div>
        <h3 className="text-lg font-semibold">
          You don't have any entities yet
        </h3>
        <p className="text-muted-foreground">Add a new entity to get started</p>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
      {entities.map((entity) => (
        <EntityCard key={entity.id} entity={entity} />
      ))}
    </div>
  )
}

function Entities() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Entities</h1>
          <p className="text-muted-foreground">
            Manage your entities
          </p>
        </div>
        <AddEntity />
      </div>
      <EntitiesGrid />
    </div>
  )
}
```

**Key patterns**:
- ✅ Use `useQuery` (NOT `useSuspenseQuery`) for better control
- ✅ Explicit loading, error, and empty states
- ✅ Responsive grid: `grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4`
- ✅ Separate `EntitiesGrid` component for data fetching
- ✅ Empty state with icon and helpful message

#### 4. Detail Page Pattern

**File**: `frontend/src/routes/_layout/entity/$entityId.tsx`

```typescript
import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { ArrowLeft, Trash2 } from "lucide-react"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { EntitiesService } from "@/client"
import PendingItems from "@/components/Pending/PendingItems"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import DeleteEntity from "@/components/Entities/DeleteEntity"

const formSchema = z.object({
  name: z.string().min(1, { message: "Name is required" }),
  description: z.string().optional(),
})

type FormData = z.infer<typeof formSchema>

export const Route = createFileRoute("/_layout/entity/$entityId")({
  component: EntityDetail,
})

function EntityDetail() {
  const { entityId } = Route.useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [isDeleteOpen, setIsDeleteOpen] = useState(false)

  const { data: entity, isLoading, error } = useQuery({
    queryKey: ["entity", entityId],
    queryFn: () => EntitiesService.readEntity({ id: entityId }),
    enabled: !!entityId,
  })

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      name: "",
      description: "",
    },
  })

  useEffect(() => {
    if (entity) {
      form.reset({
        name: entity.name,
        description: entity.description ?? undefined,
      })
    }
  }, [entity, form])

  const mutation = useMutation({
    mutationFn: (data: FormData) =>
      EntitiesService.updateEntity({ id: entityId, requestBody: data }),
    onSuccess: () => {
      showSuccessToast("Entity updated successfully")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["entities"] })
      queryClient.invalidateQueries({ queryKey: ["entity", entityId] })
    },
  })

  const onSubmit = (data: FormData) => {
    mutation.mutate(data)
  }

  const handleDeleteSuccess = () => {
    navigate({ to: "/entities" })
  }

  if (isLoading) {
    return <PendingItems />
  }

  if (error || !entity) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading entity details</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => navigate({ to: "/entities" })}
          >
            <ArrowLeft className="h-5 w-5" />
          </Button>
          <div>
            <h1 className="text-2xl font-bold tracking-tight">
              {entity.name}
            </h1>
            <p className="text-muted-foreground">{entity.type}</p>
          </div>
        </div>
        <DeleteEntity
          entity={entity}
          onSuccess={handleDeleteSuccess}
          isOpen={isDeleteOpen}
          setIsOpen={setIsDeleteOpen}
        >
          <Button variant="destructive" size="sm">
            <Trash2 className="mr-2 h-4 w-4" />
            Delete
          </Button>
        </DeleteEntity>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Entity Details</CardTitle>
          <CardDescription>
            Update your entity information below.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
              <FormField
                control={form.control}
                name="name"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>
                      Name <span className="text-destructive">*</span>
                    </FormLabel>
                    <FormControl>
                      <Input placeholder="My Entity" type="text" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="description"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Description</FormLabel>
                    <FormControl>
                      <Input placeholder="Description..." {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <div className="flex justify-end gap-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => navigate({ to: "/entities" })}
                  disabled={mutation.isPending}
                >
                  Cancel
                </Button>
                <LoadingButton type="submit" loading={mutation.isPending}>
                  Save Changes
                </LoadingButton>
              </div>
            </form>
          </Form>
        </CardContent>
      </Card>
    </div>
  )
}
```

**Key patterns**:
- ✅ Use `useQuery` (NOT `useSuspenseQuery`)
- ✅ Use `Route.useParams()` to get route parameters
- ✅ `enabled: !!entityId` to prevent query when param is undefined
- ✅ Header with back button, title, and delete button
- ✅ Form inside Card component
- ✅ `useEffect` to reset form when data loads
- ✅ Navigate to list page after deletion
- ✅ Invalidate both list and detail queries on update
- ✅ Explicit loading and error states

#### 5. Navigation After Creation

**Pattern for AddEntity dialog**:

```typescript
const mutation = useMutation({
  mutationFn: (data: EntityCreate) =>
    EntitiesService.createEntity({ requestBody: data }),
  onSuccess: (entity) => {
    showSuccessToast("Entity created successfully")
    form.reset()
    setIsOpen(false)
    // Navigate to detail page
    navigate({ to: "/entity/$entityId", params: { entityId: entity.id } })
  },
  onError: handleError.bind(showErrorToast),
  onSettled: () => {
    queryClient.invalidateQueries({ queryKey: ["entities"] })
  },
})
```

**Key pattern**:
- ✅ After creation, navigate to detail page (not back to list)
- ✅ Detail page shows empty form ready for user to fill
- ✅ Matches flow: Create placeholder → Fill details

### useQuery vs useSuspenseQuery Decision Matrix

**Use `useQuery`**:
- ✅ Card grid list pages
- ✅ Detail pages
- ✅ When you need explicit control over loading/error states
- ✅ When route parameters might be undefined
- ✅ When you want to show custom loading UI

**Use `useSuspenseQuery`**:
- ✅ DataTable list pages (with Suspense boundary)
- ✅ When using React Suspense pattern
- ❌ NOT for detail pages with route params (causes routing issues)

### Common Pitfalls

❌ **Route structure**: `entities.$id.tsx`
- Causes issues where URL changes but component doesn't render
- List page remains visible instead of detail page

✅ **Correct route structure**: `entity/$entityId.tsx`
- Clean separation between list and detail routes
- TanStack Router handles navigation properly

❌ **useSuspenseQuery in detail pages**
- Can cause routing and rendering issues
- Harder to control loading states

✅ **useQuery in detail pages**
- Explicit loading/error handling
- Works reliably with route parameters

❌ **Dropdown menu on cards**
- Cluttered UI
- Not mobile-friendly
- Redundant with detail page

✅ **Actions on detail page only**
- Cleaner card design
- All actions in one place
- Better mobile experience

❌ **"No description" placeholder text**
```typescript
{entity.notes || "No description provided"}
```

✅ **Conditional rendering**
```typescript
{entity.notes && (
  <CardDescription>{entity.notes}</CardDescription>
)}
```

❌ **Truncated card titles**
```typescript
<CardTitle className="text-lg truncate">
```

✅ **Wrapping card titles**
```typescript
<CardTitle className="text-lg break-words">
```

### Component Structure Checklist

For card-based UI implementation:

```
components/Entities/
├── EntityCard.tsx          # ✅ Card component (clickable, no menu)
├── AddEntity.tsx           # ✅ Create dialog (navigates to detail after)
├── DeleteEntity.tsx        # ✅ Delete confirmation (used in detail page)
└── EditEntity.tsx          # ❌ NOT NEEDED (use detail page form)

routes/_layout/
├── entities.tsx            # ✅ Card grid with useQuery
└── entity/
    └── $entityId.tsx       # ✅ Detail form with useQuery
```

### Responsive Grid Configuration

```typescript
// Adjust columns based on content size
<div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">

// For larger cards
<div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">

// For very detailed cards
<div className="grid grid-cols-1 md:grid-cols-2 gap-4">
```

### Summary: Card-Based UI Recipe

1. **List page** (`entities.tsx`):
   - Use `useQuery` for data fetching
   - Responsive grid layout
   - Card components for each item
   - Add button in header

2. **Card component** (`EntityCard.tsx`):
   - Wrapped in `<Link>` to detail page
   - Icon, title (with `break-words`), optional description
   - Badges for metadata
   - Hover effects
   - NO dropdown menu

3. **Detail page** (`entity/$entityId.tsx`):
   - Use `useQuery` with `enabled: !!entityId`
   - Header with back button, title, delete button
   - Form in Card component
   - Cancel/Save buttons
   - Navigate to list after delete

4. **Create flow**:
   - Dialog with name and type only
   - Navigate to detail page after creation
   - User fills remaining fields on detail page

This pattern ensures clean, mobile-friendly UI with proper routing behavior.
