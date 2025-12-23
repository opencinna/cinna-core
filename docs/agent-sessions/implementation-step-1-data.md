# Agent Sessions - Step 1: Data Entities Implementation

## 0. Preparatory Refactoring: Split Models into Domain Modules

**Rationale:** Current `backend/app/models.py` is monolithic. Split into domain-based modules similar to `backend/app/api/routes/` structure for better maintainability.

### 0.1 New Structure
```
backend/app/models/
├── __init__.py           # Re-export all models for backward compatibility
├── user.py               # User, UserCreate, UserRegister, UserUpdate, UserPublic, etc.
├── agent.py              # Agent, AgentCreate, AgentUpdate, AgentPublic, AgentCredentialLink
├── credential.py         # Credential, CredentialCreate, CredentialUpdate, CredentialPublic, CredentialType
├── item.py               # Item, ItemCreate, ItemUpdate, ItemPublic
├── environment.py        # AgentEnvironment (NEW models)
├── session.py            # Session, Message (NEW models)
```

### 0.2 Migration Steps

1. **Create `backend/app/models/` directory**
2. **Split existing models:**
   - Move User-related models → `models/user.py`
   - Move Agent-related models → `models/agent.py`
   - Move Credential-related models → `models/credential.py`
   - Move Item-related models → `models/item.py`
3. **Create `models/__init__.py`:**
   ```python
   # Re-export all models for backward compatibility
   from .user import (
       User, UserCreate, UserRegister, UserUpdate, UserUpdateMe,
       UserPublic, UsersPublic, UpdatePassword, Message, NewPassword, Token
   )
   from .agent import (
       Agent, AgentCreate, AgentUpdate, AgentPublic, AgentsPublic,
       AgentCredentialLink
   )
   from .credential import (
       Credential, CredentialCreate, CredentialUpdate, CredentialPublic,
       CredentialsPublic, CredentialType, EncryptedData
   )
   from .item import (
       Item, ItemCreate, ItemUpdate, ItemPublic, ItemsPublic
   )

   __all__ = [
       # Users
       "User", "UserCreate", "UserRegister", "UserUpdate", "UserUpdateMe",
       "UserPublic", "UsersPublic", "UpdatePassword", "Message", "NewPassword", "Token",
       # Agents
       "Agent", "AgentCreate", "AgentUpdate", "AgentPublic", "AgentsPublic",
       "AgentCredentialLink",
       # Credentials
       "Credential", "CredentialCreate", "CredentialUpdate", "CredentialPublic",
       "CredentialsPublic", "CredentialType", "EncryptedData",
       # Items
       "Item", "ItemCreate", "ItemUpdate", "ItemPublic", "ItemsPublic",
   ]
   ```
4. **Verify imports still work:**
   - All existing code uses `from app.models import ...`
   - Should work unchanged due to `__init__.py` re-exports
5. **Run tests to ensure no breakage**

### 0.3 Add New Models in Separate Files

After refactoring, add new models:
- `models/environment.py` - AgentEnvironment and schemas
- `models/session.py` - Session, Message and schemas

## 1. Database Models (New Files in `backend/app/models/`)

### 1.1 Extend Agent Model in `models/agent.py`
```python
# File: backend/app/models/agent.py

class Agent(SQLModel, table=True):
    # EXISTING FIELDS (keep as-is):
    # id, name, owner_id, workflow_prompt, entrypoint_prompt, credentials (via AgentCredentialLink)

    # ADD NEW FIELDS:
    description: str | None = None
    is_active: bool = Field(default=True)
    active_environment_id: uuid.UUID | None = Field(default=None, foreign_key="agent_environment.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
```

### 1.2 New AgentEnvironment Model in `models/environment.py`
```python
# File: backend/app/models/environment.py

from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON
import uuid
from datetime import datetime

class AgentEnvironment(SQLModel, table=True):
    __tablename__ = "agent_environment"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")
    env_name: str  # e.g., "python-env-basic"
    env_version: str  # e.g., "1.0.0"
    instance_name: str  # e.g., "Production", "Testing"
    type: str  # "docker" | "remote_ssh" | "remote_http" | "kubernetes"
    status: str = "stopped"  # "stopped" | "starting" | "running" | "error" | "deprecated"
    is_active: bool = Field(default=False)
    config: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_health_check: datetime | None = None
```

### 1.3 New Session & Message Models in `models/session.py`
```python
# File: backend/app/models/session.py

from sqlmodel import SQLModel, Field, Column, UniqueConstraint
from sqlalchemy import JSON
import uuid
from datetime import datetime

class Session(SQLModel, table=True):
    __tablename__ = "session"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    environment_id: uuid.UUID = Field(foreign_key="agent_environment.id", ondelete="CASCADE")
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    title: str | None = None
    mode: str = "conversation"  # "building" | "conversation"
    status: str = "active"  # "active" | "paused" | "completed" | "error"
    metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_message_at: datetime | None = None


class Message(SQLModel, table=True):
    __tablename__ = "message"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID = Field(foreign_key="session.id", ondelete="CASCADE")
    role: str  # "user" | "agent" | "system"
    content: str
    sequence_number: int
    metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (UniqueConstraint("session_id", "sequence_number"),)
```

## 2. Pydantic Schemas (in respective model files)

### 2.1 Agent Schemas in `models/agent.py` (extend existing)
```python
# File: backend/app/models/agent.py

class AgentUpdate(SQLModel):
    name: str | None = None
    description: str | None = None
    workflow_prompt: str | None = None
    entrypoint_prompt: str | None = None
    is_active: bool | None = None

class AgentPublic(SQLModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_active: bool
    active_environment_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
```

### 2.2 Environment Schemas in `models/environment.py`
```python
# File: backend/app/models/environment.py

class AgentEnvironmentCreate(SQLModel):
    env_name: str
    env_version: str
    instance_name: str
    type: str  # "docker" | "remote_ssh" | "remote_http"
    config: dict = {}

class AgentEnvironmentUpdate(SQLModel):
    instance_name: str | None = None
    config: dict | None = None

class AgentEnvironmentPublic(SQLModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    env_name: str
    env_version: str
    instance_name: str
    type: str
    status: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_health_check: datetime | None

class AgentEnvironmentsPublic(SQLModel):
    data: list[AgentEnvironmentPublic]
    count: int
```

### 2.3 Session Schemas in `models/session.py`
```python
# File: backend/app/models/session.py

class SessionCreate(SQLModel):
    agent_id: uuid.UUID  # Will use active environment
    title: str | None = None
    mode: str = "conversation"  # "building" | "conversation"

class SessionUpdate(SQLModel):
    title: str | None = None
    status: str | None = None
    mode: str | None = None

class SessionPublic(SQLModel):
    id: uuid.UUID
    environment_id: uuid.UUID
    user_id: uuid.UUID
    title: str | None
    mode: str
    status: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None

class SessionsPublic(SQLModel):
    data: list[SessionPublic]
    count: int
```

### 2.4 Message Schemas in `models/session.py`
```python
# File: backend/app/models/session.py

class MessageCreate(SQLModel):
    content: str

class MessagePublic(SQLModel):
    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    sequence_number: int
    timestamp: datetime
    metadata: dict

class MessagesPublic(SQLModel):
    data: list[MessagePublic]
    count: int
```

### 2.5 Update `models/__init__.py` with new exports
```python
# File: backend/app/models/__init__.py

# ... existing imports ...

# Add new imports
from .environment import (
    AgentEnvironment,
    AgentEnvironmentCreate,
    AgentEnvironmentUpdate,
    AgentEnvironmentPublic,
    AgentEnvironmentsPublic,
)
from .session import (
    Session,
    SessionCreate,
    SessionUpdate,
    SessionPublic,
    SessionsPublic,
    Message,
    MessageCreate,
    MessagePublic,
    MessagesPublic,
)

__all__ = [
    # ... existing exports ...
    # Environments
    "AgentEnvironment",
    "AgentEnvironmentCreate",
    "AgentEnvironmentUpdate",
    "AgentEnvironmentPublic",
    "AgentEnvironmentsPublic",
    # Sessions
    "Session",
    "SessionCreate",
    "SessionUpdate",
    "SessionPublic",
    "SessionsPublic",
    # Messages
    "Message",
    "MessageCreate",
    "MessagePublic",
    "MessagesPublic",
]
```

## 3. Service Classes (`backend/app/services/`)

### 3.1 AgentService (`backend/app/services/agent_service.py`)
```python
class AgentService:
    @staticmethod
    def create_agent(session: Session, user_id: UUID, data: AgentCreate) -> Agent:
        """Create new agent"""
        pass  # Skeleton

    @staticmethod
    def get_agent_with_environment(session: Session, agent_id: UUID) -> Agent:
        """Get agent with active environment details"""
        pass

    @staticmethod
    def update_agent(session: Session, agent_id: UUID, data: AgentUpdate) -> Agent:
        """Update agent"""
        pass

    @staticmethod
    def set_active_environment(session: Session, agent_id: UUID, env_id: UUID) -> Agent:
        """Set active environment for agent"""
        pass

    @staticmethod
    def delete_agent(session: Session, agent_id: UUID) -> bool:
        """Delete agent (cascades to environments)"""
        pass
```

### 3.2 EnvironmentService (`backend/app/services/environment_service.py`)
```python
class EnvironmentService:
    @staticmethod
    def create_environment(
        session: Session,
        agent_id: UUID,
        data: AgentEnvironmentCreate
    ) -> AgentEnvironment:
        """Create environment for agent"""
        pass

    @staticmethod
    def get_environment(session: Session, env_id: UUID) -> AgentEnvironment:
        """Get environment by ID"""
        pass

    @staticmethod
    def update_environment(
        session: Session,
        env_id: UUID,
        data: AgentEnvironmentUpdate
    ) -> AgentEnvironment:
        """Update environment config"""
        pass

    @staticmethod
    def delete_environment(session: Session, env_id: UUID) -> bool:
        """Delete environment"""
        pass

    @staticmethod
    def list_agent_environments(session: Session, agent_id: UUID) -> list[AgentEnvironment]:
        """List all environments for an agent"""
        pass
```

### 3.3 SessionService (`backend/app/services/session_service.py`)
```python
class SessionService:
    @staticmethod
    def create_session(
        session: Session,
        user_id: UUID,
        data: SessionCreate
    ) -> Session:
        """Create session using agent's active environment"""
        pass

    @staticmethod
    def get_session(session: Session, session_id: UUID) -> Session:
        """Get session by ID"""
        pass

    @staticmethod
    def update_session(
        session: Session,
        session_id: UUID,
        data: SessionUpdate
    ) -> Session:
        """Update session (title, status, mode)"""
        pass

    @staticmethod
    def switch_mode(session: Session, session_id: UUID, new_mode: str) -> Session:
        """Switch session mode (building <-> conversation)"""
        pass

    @staticmethod
    def list_user_sessions(session: Session, user_id: UUID) -> list[Session]:
        """List all sessions for user"""
        pass

    @staticmethod
    def list_agent_sessions(session: Session, agent_id: UUID) -> list[Session]:
        """List all sessions for agent (across all environments)"""
        pass
```

### 3.4 MessageService (`backend/app/services/message_service.py`)
```python
class MessageService:
    @staticmethod
    def create_message(
        session: Session,
        session_id: UUID,
        role: str,
        content: str,
        metadata: dict = None
    ) -> Message:
        """Create message in session with auto-incremented sequence"""
        pass

    @staticmethod
    def get_session_messages(
        session: Session,
        session_id: UUID,
        limit: int = 100,
        offset: int = 0
    ) -> list[Message]:
        """Get messages for session ordered by sequence"""
        pass

    @staticmethod
    def get_last_n_messages(
        session: Session,
        session_id: UUID,
        n: int = 20
    ) -> list[Message]:
        """Get last N messages for context window"""
        pass
```

## 4. API Routes (Skeleton)

### 4.1 Agent Routes (`backend/app/api/routes/agents.py`)
```python
# Extend existing agent routes

@router.post("/{agent_id}/environments", response_model=AgentEnvironmentPublic)
def create_agent_environment(
    agent_id: UUID,
    data: AgentEnvironmentCreate,
    session: SessionDep,
    current_user: CurrentUser
) -> AgentEnvironment:
    """Create new environment for agent"""
    # Validate ownership
    # Call EnvironmentService.create_environment()
    pass

@router.get("/{agent_id}/environments", response_model=list[AgentEnvironmentPublic])
def list_agent_environments(
    agent_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
) -> list[AgentEnvironment]:
    """List agent's environments"""
    pass

@router.post("/{agent_id}/environments/{env_id}/activate", response_model=AgentPublic)
def activate_environment(
    agent_id: UUID,
    env_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
) -> Agent:
    """Set environment as active for agent"""
    pass
```

### 4.2 Environment Routes (`backend/app/api/routes/environments.py` - NEW)
```python
@router.get("/{env_id}", response_model=AgentEnvironmentPublic)
def get_environment(
    env_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
) -> AgentEnvironment:
    """Get environment details"""
    pass

@router.patch("/{env_id}", response_model=AgentEnvironmentPublic)
def update_environment(
    env_id: UUID,
    data: AgentEnvironmentUpdate,
    session: SessionDep,
    current_user: CurrentUser
) -> AgentEnvironment:
    """Update environment config"""
    pass

@router.delete("/{env_id}")
def delete_environment(
    env_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
):
    """Delete environment"""
    pass

# Lifecycle endpoints (stub for now)
@router.post("/{env_id}/start")
def start_environment(env_id: UUID, session: SessionDep, current_user: CurrentUser):
    """Start environment (stub)"""
    raise HTTPException(501, "Not implemented in Step 1")

@router.post("/{env_id}/stop")
def stop_environment(env_id: UUID, session: SessionDep, current_user: CurrentUser):
    """Stop environment (stub)"""
    raise HTTPException(501, "Not implemented in Step 1")
```

### 4.3 Session Routes (`backend/app/api/routes/sessions.py` - NEW)
```python
@router.post("/", response_model=SessionPublic)
def create_session(
    data: SessionCreate,
    session: SessionDep,
    current_user: CurrentUser
) -> Session:
    """Create new session using agent's active environment"""
    pass

@router.get("/", response_model=list[SessionPublic])
def list_sessions(
    session: SessionDep,
    current_user: CurrentUser
) -> list[Session]:
    """List user's sessions"""
    pass

@router.get("/{session_id}", response_model=SessionPublic)
def get_session(
    session_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
) -> Session:
    """Get session details"""
    pass

@router.patch("/{session_id}", response_model=SessionPublic)
def update_session(
    session_id: UUID,
    data: SessionUpdate,
    session: SessionDep,
    current_user: CurrentUser
) -> Session:
    """Update session"""
    pass

@router.patch("/{session_id}/mode", response_model=SessionPublic)
def switch_session_mode(
    session_id: UUID,
    new_mode: str,
    session: SessionDep,
    current_user: CurrentUser
) -> Session:
    """Switch session mode (building <-> conversation)"""
    pass

@router.delete("/{session_id}")
def delete_session(
    session_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
):
    """Delete session"""
    pass
```

### 4.4 Message Routes (`backend/app/api/routes/messages.py` - NEW)
```python
@router.get("/sessions/{session_id}/messages", response_model=list[MessagePublic])
def get_messages(
    session_id: UUID,
    limit: int = 100,
    offset: int = 0,
    session: SessionDep,
    current_user: CurrentUser
) -> list[Message]:
    """Get session messages"""
    pass

@router.post("/sessions/{session_id}/messages", response_model=MessagePublic)
def send_message(
    session_id: UUID,
    data: MessageCreate,
    session: SessionDep,
    current_user: CurrentUser
) -> Message:
    """Send message to agent (stub - no actual agent communication yet)"""
    # For Step 1: Just store user message, return mock agent response
    pass
```

## 5. Database Migration

### Alembic Migration Script
```bash
cd backend
source .venv/bin/activate
alembic revision --autogenerate -m "Add agent sessions: extend agent, add environment, session, message"
alembic upgrade head
```

**Expected Changes:**
- ALTER TABLE agent: add description, is_active, active_environment_id, created_at, updated_at
- CREATE TABLE agent_environment
- CREATE TABLE session
- CREATE TABLE message
- CREATE INDEX on message(session_id, sequence_number)
- CREATE FOREIGN KEY constraints with CASCADE delete

## 6. Register New Routes (`backend/app/api/main.py`)

```python
from app.api.routes import environments, sessions, messages

api_router.include_router(environments.router, prefix="/environments", tags=["environments"])
api_router.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
api_router.include_router(messages.router, prefix="/messages", tags=["messages"])
```

## 7. Validation Checklist

### 7.1 Refactoring Validation
- [ ] `backend/app/models/` directory created with all files
- [ ] Existing models split into domain files (user, agent, credential, item)
- [ ] `models/__init__.py` re-exports all models
- [ ] All existing imports (`from app.models import ...`) still work
- [ ] Backend tests pass after refactoring
- [ ] No import errors in routes/crud/services

### 7.2 New Models Validation
- [ ] Alembic migration runs without errors
- [ ] Can create agent via API (with new fields: description, is_active, timestamps)
- [ ] Can create environment for agent via API
- [ ] Can create session linked to environment via API
- [ ] Can create message in session via API
- [ ] Can retrieve messages ordered by sequence
- [ ] Authorization prevents cross-user access
- [ ] Cascade delete works (agent → environments → sessions → messages)
- [ ] Can set active environment for agent
- [ ] Can switch session mode
- [ ] OpenAPI spec regenerates correctly with new models

## Implementation Notes

- **Refactoring first** - Split models into domain files before adding new models
- **No Docker logic yet** - environment lifecycle endpoints are stubs
- **No agent communication** - message sending just stores user message
- **Focus on data layer** - proper relationships, constraints, indexes
- **Service abstraction** - business logic separated from routes
- **Authorization** - validate user owns agent/session before operations
- **JSON columns** - for flexible config/metadata without schema changes
- **Similar refactoring for crud.py** - Consider splitting `crud.py` into `crud/agents.py`, `crud/items.py`, etc. in future (not in this step)
