# LLM Development Guidelines for Workflow Runner Core

This document contains project-specific patterns, commands, and pitfalls for LLM assistants working on this codebase.

## Project Structure

```
workflow-runner-core/
├── backend/           # FastAPI backend (working dir: /Users/evgenyl/dev/ml-llm/workflow-runner-core/backend)
└── frontend/          # React frontend
```

**CRITICAL**: Current working directory is `/Users/evgenyl/dev/ml-llm/workflow-runner-core/backend`

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

### 1. Backend Models (`backend/app/models.py`)

**Pattern to follow**:
```python
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
    owner: User | None = Relationship(back_populates="entities")  # NO QUOTES for User

# Public schema
class EntityPublic(EntityBase):
    id: uuid.UUID
    owner_id: uuid.UUID

class EntitiesPublic(SQLModel):
    data: list[EntityPublic]
    count: int
```

**CRITICAL SQLAlchemy Relationship Pattern**:
- In `User` model (defined first): `entities: list["Entity"] = Relationship(...)` - USE QUOTES (forward reference)
- In `Entity` model (defined later): `owner: User | None = Relationship(...)` - NO QUOTES (User already defined)
- WRONG: `owner: "User | None"` - Will cause SQLAlchemy initialization error
- WRONG: `owner: "User" | None` - Will cause type checker issues

### 2. CRUD Operations (`backend/app/crud.py`)

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
from app.api.routes import entities
api_router.include_router(entities.router)
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

### 1. SQLAlchemy Relationships
❌ `owner: "User | None"` - String with union syntax causes init error
✅ `owner: User | None` - No quotes when User is already defined
✅ `entities: list["Entity"]` - Quotes for forward reference in User model

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
# 1. Edit models.py, crud.py
# 2. Create routes file
# 3. Register in api/main.py
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
