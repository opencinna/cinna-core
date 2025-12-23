# Agent Sessions Implementation Specification

## 1. Overview

### 1.1 Purpose
Enable users to create and manage conversational sessions with AI agents running in isolated Docker containers. Each agent uses Google's Agent Development Kit (ADK) and maintains persistent file storage while supporting multiple concurrent sessions.

### 1.2 Key Concepts
- **Agent**: A containerized AI assistant with specific capabilities, configuration, and file system
- **Session**: A conversation instance between a user and an agent, with independent message history
- **Container**: Docker environment providing isolation and resource management for agents
- **Message**: Individual communication unit within a session (from user or agent)

---

## 2. Entities & Data Models

### 2.1 Agent (EXISTING MODEL - Extended)
Represents the **logical definition** of an AI agent - its purpose, prompts, and configuration. This is the "what" of the agent, not the "how" or "where" it runs.

**Existing Attributes** (from `models.py`):
- `id` (UUID): Unique identifier
- `name` (string): Human-readable agent name
- `owner_id` (UUID, FK to User): Owner of the agent
- `workflow_prompt` (text, nullable): Agent's workflow instructions
- `entrypoint_prompt` (text, nullable): Agent's entry point behavior
- `credentials` (many-to-many): Linked credentials via `AgentCredentialLink`

**New Attributes to Add:**
- `description` (text, nullable): Agent purpose and capabilities
- `is_active` (boolean, default=True): Whether agent is available for use
- `active_environment_id` (UUID, FK to AgentEnvironment, nullable): Currently active runtime environment
- `created_at` (timestamp)
- `updated_at` (timestamp)

**Business Rules:**
- Each agent belongs to a user
- Agent name must be unique per user
- Agent can have multiple environments (for versioning, rollout, rollback)
- Only one environment is "active" at a time
- Only active agents can create new sessions
- Credentials are defined at Agent level (inherited by all environments)

### 2.2 AgentEnvironment (NEW MODEL)
Represents the **runtime execution environment** for an agent. This is the "how" and "where" - the actual Docker container, remote server, or any other execution platform that follows the EnvironmentAdapter contract.

**Attributes:**
- `id` (UUID): Unique identifier
- `agent_id` (UUID, FK to Agent): Parent agent definition
- `env_name` (string): Environment template name (e.g., "python-env-basic", "nodejs-adk-env")
- `env_version` (string): Environment template version (e.g., "1.0.0", "2.1.3")
- `instance_name` (string): Human-readable instance name (e.g., "Production", "Testing", "Rollback v1")
- `type` (enum): `docker` | `remote_ssh` | `remote_http` | `kubernetes` (extensible)
- `status` (enum): `stopped` | `starting` | `running` | `error` | `deprecated`
- `is_active` (boolean): Whether this is the active environment for the agent
- `created_at` (timestamp)
- `updated_at` (timestamp)
- `last_health_check` (timestamp, nullable): Last successful health check

**Type-Specific Configuration (JSON field: `config`):**

**For Docker type:**
```json
{
  "docker_image": "python-env-basic:1.0.0",
  "container_id": "abc123...",
  "port": 8000,
  "workspace_volume": "/app/workspace",
  "network": "agent-network",
  "resource_limits": {
    "cpu": "1.0",
    "memory": "512M"
  }
}
```

**For Remote SSH type:**
```json
{
  "host": "remote-agent.example.com",
  "port": 22,
  "username": "agent-runner",
  "auth_method": "ssh_key",
  "workspace_path": "/home/agent-runner/workspace",
  "python_path": "/usr/bin/python3"
}
```

**For Remote HTTP type:**
```json
{
  "base_url": "https://agent-api.example.com",
  "api_key": "encrypted...",
  "timeout": 60,
  "max_file_size_mb": 100
}
```

**Environment Name & Version:**
- `env_name` + `env_version` define the **template/blueprint** (e.g., "python-env-basic:1.0.0")
- Each template has an associated `AgentEnvBuilder` that knows how to build/initialize it
- Templates can be versioned independently of agent definitions
- Multiple environments can use the same template (e.g., prod and staging both use "python-env-basic:1.0.0")

**Business Rules:**
- Each agent can have multiple environments
- Only one environment per agent can be `is_active=True` at a time
- Setting a new environment as active automatically deactivates the previous one
- Environments can be deprecated (soft delete) but not hard deleted (for audit trail)
- Sessions are created against a specific environment (allows rollback to previous version)
- Environment must be in `running` status to create new sessions

**Use Cases:**
1. **Version Management**: Create new environment with updated Docker image, test it, then switch active flag
2. **Blue-Green Deployment**: Run two environments, switch active flag for instant rollback
3. **Multi-Platform Support**: Future support for non-Docker environments (cloud functions, remote servers)

### 2.3 Session (UPDATED)
Represents a conversation between a user and an agent **environment**. Sessions belong to a specific environment, not directly to an agent.

**Attributes:**
- `id` (UUID): Unique identifier
- `environment_id` (UUID, FK to AgentEnvironment): Which environment is used
- `user_id` (UUID, FK to User): Session owner
- `title` (string, nullable): User-defined or auto-generated title
- `mode` (enum): `building` | `conversation` - Session interaction mode (see Section 4.5)
- `status` (enum): `active` | `paused` | `completed` | `error`
- `created_at` (timestamp)
- `updated_at` (timestamp)
- `last_message_at` (timestamp, nullable): Last activity timestamp
- `metadata` (JSON, nullable): Session-specific data (context, state)

**Business Rules:**
- Session can only be created if environment is `running`
- Multiple sessions can exist for same environment (shared file system, separate history)
- Only session owner can interact with it
- Sessions maintain independent message histories
- Sessions persist even if environment is later deprecated (historical record)
- Session mode determines the agent's capabilities and context (building vs conversation)
- Mode can be switched during session lifecycle (e.g., build → conversation)

### 2.4 Message
Represents a single message in a conversation.

**Attributes:**
- `id` (UUID): Unique identifier
- `session_id` (UUID, FK to Session): Parent session
- `role` (enum): `user` | `agent` | `system`
- `content` (text): Message body
- `timestamp` (timestamp): When message was created
- `metadata` (JSON, nullable): Additional data (token count, attachments, etc.)
- `sequence_number` (integer): Order within session

**Business Rules:**
- Messages are immutable after creation
- Sequence numbers ensure ordering
- System messages can be used for notifications/errors

### 2.5 Credential (EXISTING MODEL - No Changes Needed)
Already implemented in `models.py` with proper encryption and many-to-many relationship with Agent.

**Key Points:**
- Type-safe credentials (EMAIL_IMAP, ODOO, GMAIL_OAUTH)
- Encrypted data stored in `encrypted_data` field
- Many-to-many relationship with Agent via `AgentCredentialLink`
- Credentials are defined at **Agent level**, not Environment level
- All environments of an agent inherit the same credentials

**Usage in Environments:**
- When environment starts, it receives credentials from its parent Agent
- Agent Communication Protocol includes credential data in initialization
- Environment uses credentials according to its type (Docker: env vars, Remote: API headers)

---

## 3. Architecture & Components

### 3.1 System Architecture (Complete Abstraction Stack)

```
┌─────────────────────────────────────────────────────────────┐
│                    Frontend UI (React)                      │
│   Agent Mgmt | Env Mgmt | Session List | Chat | Files      │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP/WebSocket/File Upload
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   Backend API (FastAPI)                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │Agent Manager │  │Session Mgr   │  │Env Lifecycle │     │
│  │   Service    │  │   Service    │  │   Manager    │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │       Environment Service (Orchestration)            │  │
│  │  - get_adapter(env) -> EnvironmentAdapter           │  │
│  │  - get_builder(name, version) -> AgentEnvBuilder    │  │
│  │  - build_environment(env, agent)                    │  │
│  │  - send_message(env, request)                       │  │
│  │  - upload_file(env, file)                           │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────┬───────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────┐
│              ABSTRACTION LAYER (The Contract)               │
│                                                             │
│  ┌────────────────────────────────────────────────────┐    │
│  │  EnvironmentAdapter (Abstract Interface)          │    │
│  │  - initialize()    - set_prompts()                │    │
│  │  - send_message()  - set_config()                 │    │
│  │  - upload_file()   - set_credentials()            │    │
│  │  - download_file() - execute_command()            │    │
│  │  - health_check()  - get_logs()                   │    │
│  └────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌────────────────────────────────────────────────────┐    │
│  │  AgentEnvBuilder (Template Builder Registry)      │    │
│  │  - PythonEnvBasicBuilder_v1_0                     │    │
│  │  - PythonEnvBasicBuilder_v1_1                     │    │
│  │  - NodeJSADKBuilder_v1_0                          │    │
│  │  Each knows how to build specific env version     │    │
│  └────────────────────────────────────────────────────┘    │
└─────────┬───────────────────────────────────────────────────┘
          │
          │ Concrete Implementations (Transport Layer)
          │
    ┌─────┴──────┬─────────────┬──────────────┐
    │            │             │              │
    ▼            ▼             ▼              ▼
┌─────────┐  ┌─────────┐  ┌─────────┐  ┌──────────┐
│ Docker  │  │   SSH   │  │  HTTP   │  │Kubernetes│
│ Adapter │  │ Adapter │  │ Adapter │  │ Adapter  │
└────┬────┘  └────┬────┘  └────┬────┘  └────┬─────┘
     │            │            │            │
     ▼            ▼            ▼            ▼
┌─────────┐  ┌─────────┐  ┌─────────┐  ┌──────────┐
│ Docker  │  │ Remote  │  │ Remote  │  │   K8s    │
│Container│  │  SSH    │  │   API   │  │   Pod    │
│         │  │ Server  │  │ Server  │  │          │
│  ADK    │  │  ADK    │  │  ADK    │  │   ADK    │
│ Agent   │  │ Agent   │  │ Agent   │  │  Agent   │
│         │  │         │  │         │  │          │
│/workspace│ │/home/..│  │ S3/GCS  │  │ PV/PVC   │
└─────────┘  └─────────┘  └─────────┘  └──────────┘
```

**Layered Architecture (Updated):**

1. **Data Layer** (PostgreSQL)
   - Agent (logical definition)
   - AgentEnvironment (runtime instance with env_name + env_version)
   - Session (conversation)
   - Message (messages in session)
   - Credential (encrypted, linked to Agent)

2. **Service Layer** (Backend API)
   - Agent Manager: CRUD for agent definitions
   - Environment Lifecycle Manager: Start/stop environments
   - Session Manager: Session and message management
   - Environment Service: Orchestrates adapter + builder

3. **Abstraction Layer** (THE KEY INNOVATION)
   - **EnvironmentAdapter**: Transport-agnostic interface for ALL env operations
     - File operations (upload/download/list/delete)
     - Configuration (prompts, config, credentials)
     - Communication (send_message, stream, execute_command)
     - Lifecycle (initialize, shutdown, health_check)
   - **AgentEnvBuilder**: Template-specific build logic
     - Versioned builders for each environment template
     - Handles initialization sequences specific to env version

4. **Transport Layer** (Concrete Implementations)
   - DockerEnvironmentAdapter: Local Docker via Docker SDK + HTTP
   - SSHEnvironmentAdapter: Remote via SSH/SFTP
   - HTTPEnvironmentAdapter: Remote via HTTP API
   - KubernetesEnvironmentAdapter: K8s pods (future)

5. **Runtime Layer** (Actual Execution)
   - Docker containers with mounted volumes
   - Remote SSH servers with workspace directories
   - Remote HTTP APIs with cloud storage
   - Kubernetes pods with persistent volumes

**Key Benefits:**
- ✅ **Transport Agnostic**: File object abstraction works with filesystem, HTTP, SSH, etc.
- ✅ **Version Management**: Different builders for env templates (python-env-basic:1.0.0 vs 1.1.0)
- ✅ **Clean Separation**: Logic (Agent) → Template (Environment) → Transport (Adapter) → Runtime
- ✅ **Extensible**: Add new environment types by implementing adapter + builder

### 3.2 Backend Components

#### 3.2.1 Agent Manager Service
**Responsibilities:**
- CRUD operations for Agent entities (logical definitions)
- Manage agent credentials (link/unlink)
- Validate agent configuration
- Provide agent metadata to frontend

**Key Methods:**
- `create_agent(user_id: UUID, data: AgentCreate) -> Agent`
- `update_agent(agent_id: UUID, data: AgentUpdate) -> Agent`
- `get_agent_with_credentials(agent_id: UUID) -> AgentWithCredentials`
- `set_active_environment(agent_id: UUID, env_id: UUID) -> Agent`
- `delete_agent(agent_id: UUID) -> bool`

#### 3.2.2 Environment Lifecycle Manager (NEW)
**Responsibilities:**
- Create/start/stop/delete AgentEnvironment instances
- Manage environment-specific resources (Docker containers, remote connections)
- Monitor environment health
- Handle environment transitions (blue-green deployments)
- Implement environment-type-specific logic (Docker, Remote, K8s)

**Key Methods:**
- `create_environment(agent_id: UUID, data: EnvCreate) -> AgentEnvironment`
- `start_environment(env_id: UUID) -> bool`
- `stop_environment(env_id: UUID) -> bool`
- `get_environment_status(env_id: UUID) -> EnvironmentStatus`
- `switch_active_environment(agent_id: UUID, new_env_id: UUID) -> bool`
- `health_check(env_id: UUID) -> HealthStatus`
- `get_environment_logs(env_id: UUID) -> list[str]`

**Docker-Specific Methods:**
- `_create_docker_container(env: AgentEnvironment, agent: Agent) -> str`
- `_mount_credentials(container_id: str, credentials: list[Credential])`
- `_configure_network(container_id: str, config: dict)`

#### 3.2.3 Session Manager Service
**Responsibilities:**
- Create and manage sessions (now linked to environments, not agents directly)
- Route messages to appropriate environment
- Store message history in database
- Handle session lifecycle (create, pause, complete)
- Manage session context and metadata

**Key Methods:**
- `create_session(user_id: UUID, environment_id: UUID, title: str) -> Session`
- `send_message(session_id: UUID, content: str, role: MessageRole) -> Message`
- `get_session_history(session_id: UUID, limit: int) -> list[Message]`
- `update_session_status(session_id: UUID, status: SessionStatus)`
- `get_sessions_by_agent(agent_id: UUID) -> list[Session]` (across all environments)
- `get_sessions_by_environment(env_id: UUID) -> list[Session]`

#### 3.2.4 EnvironmentAdapter (THE ABSTRACTION LAYER)
**Responsibilities:**
- Define and implement the **complete abstraction** for ALL environment interactions
- Abstract away transport details (Docker, SSH, HTTP)
- Handle files, configuration, prompts, credentials, messages
- Provide consistent interface regardless of environment type
- Manage timeouts, retries, error handling

**The EnvironmentAdapter Contract** (what all environment types must implement):

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator, BinaryIO

class File:
    """Abstraction for file operations - transport agnostic"""
    path: str
    content: BinaryIO | bytes
    metadata: dict

class EnvironmentAdapter(ABC):
    """Abstract adapter that all environment types must implement"""

    # === Lifecycle Management ===

    @abstractmethod
    async def initialize(self, config: EnvInitConfig) -> InitResponse:
        """
        Initialize the environment
        - config: Environment-specific initialization config
        - Returns: Initialization status and metadata
        """
        pass

    @abstractmethod
    async def shutdown(self) -> bool:
        """Gracefully shutdown the environment"""
        pass

    @abstractmethod
    async def health_check(self) -> HealthResponse:
        """Check if environment is healthy and responsive"""
        pass

    # === Configuration Management ===

    @abstractmethod
    async def set_prompts(self, workflow_prompt: str, entrypoint_prompt: str) -> bool:
        """
        Set or update agent prompts in the environment
        - workflow_prompt: Agent's workflow instructions
        - entrypoint_prompt: Agent's entry point behavior
        """
        pass

    @abstractmethod
    async def set_config(self, config: dict) -> bool:
        """
        Set or update agent configuration
        - config: Key-value configuration (env vars, settings, etc.)
        """
        pass

    @abstractmethod
    async def set_credentials(self, credentials: list[CredentialData]) -> bool:
        """
        Set or update credentials in the environment
        - credentials: List of decrypted credentials to mount/inject
        """
        pass

    # === File Operations ===

    @abstractmethod
    async def upload_file(self, file: File) -> bool:
        """
        Upload a file to the environment's workspace
        - file: File object (path, content, metadata)
        - Implementation: Local filesystem, HTTP upload, SCP, etc.
        """
        pass

    @abstractmethod
    async def download_file(self, path: str) -> File:
        """
        Download a file from the environment's workspace
        - path: Path to file in environment
        - Returns: File object with content
        """
        pass

    @abstractmethod
    async def list_files(self, path: str = "/") -> list[str]:
        """
        List files in environment's workspace
        - path: Directory path to list
        - Returns: List of file paths
        """
        pass

    @abstractmethod
    async def delete_file(self, path: str) -> bool:
        """Delete a file from the environment's workspace"""
        pass

    # === Message Communication ===

    @abstractmethod
    async def send_message(self, request: MessageRequest) -> MessageResponse:
        """
        Send a message to the agent and get response
        - request: session_id, message, history, context
        - Returns: Agent's response message
        """
        pass

    @abstractmethod
    async def stream_message(self, request: MessageRequest) -> AsyncIterator[str]:
        """
        Stream agent response in chunks (for real-time UI)
        - Yields: Response chunks as they're generated
        """
        pass

    # === Command Execution (Optional) ===

    @abstractmethod
    async def execute_command(self, command: str) -> CommandResult:
        """
        Execute a command in the environment
        - command: Shell command to execute
        - Returns: Exit code, stdout, stderr
        - Use case: Debugging, maintenance, custom operations
        """
        pass

    # === Logs & Monitoring ===

    @abstractmethod
    async def get_logs(self, lines: int = 100) -> list[str]:
        """Get recent log lines from the environment"""
        pass
```

**Data Models:**

```python
class EnvInitConfig(SQLModel):
    """Configuration for environment initialization"""
    env_name: str  # e.g., "python-env-basic"
    env_version: str  # e.g., "1.0.0"
    agent_id: UUID
    workspace_id: str  # Unique workspace identifier

class AgentConfig(SQLModel):
    """Agent configuration passed to environment"""
    agent_id: UUID
    workflow_prompt: str | None
    entrypoint_prompt: str | None
    credentials: list[CredentialData]
    files: list[File]  # Files to upload during initialization
    settings: dict  # Environment-specific settings

class MessageRequest(SQLModel):
    session_id: UUID
    message: str
    history: list[HistoryMessage]
    context: dict

class MessageResponse(SQLModel):
    response: str
    metadata: dict | None

class HealthResponse(SQLModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    uptime: int
    message: str | None

class CommandResult(SQLModel):
    exit_code: int
    stdout: str
    stderr: str
```

**Concrete Adapter Implementations:**

```python
class DockerEnvironmentAdapter(EnvironmentAdapter):
    """Adapter for Docker-based environments"""

    def __init__(self, container_id: str, port: int, workspace_path: str):
        self.container_id = container_id
        self.base_url = f"http://localhost:{port}"
        self.workspace_path = workspace_path

    async def upload_file(self, file: File) -> bool:
        # Use Docker SDK to copy file into container
        import docker
        client = docker.from_env()
        container = client.containers.get(self.container_id)

        # Write to temp file, then copy to container
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(file.content)
            tmp.flush()
            container.put_archive(
                path=f"{self.workspace_path}/{os.path.dirname(file.path)}",
                data=open(tmp.name, 'rb')
            )
        return True

    async def set_prompts(self, workflow_prompt: str, entrypoint_prompt: str) -> bool:
        # HTTP POST to container's config endpoint
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/config/prompts",
                json={
                    "workflow_prompt": workflow_prompt,
                    "entrypoint_prompt": entrypoint_prompt
                }
            )
            return response.status_code == 200

class SSHEnvironmentAdapter(EnvironmentAdapter):
    """Adapter for remote SSH-based environments"""

    def __init__(self, host: str, port: int, username: str, key_path: str, workspace_path: str):
        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.connect(host, port, username, key_filename=key_path)
        self.sftp = self.ssh_client.open_sftp()
        self.workspace_path = workspace_path

    async def upload_file(self, file: File) -> bool:
        # Use SFTP to upload file
        remote_path = f"{self.workspace_path}/{file.path}"
        self.sftp.putfo(BytesIO(file.content), remote_path)
        return True

    async def execute_command(self, command: str) -> CommandResult:
        # Execute command over SSH
        stdin, stdout, stderr = self.ssh_client.exec_command(command)
        return CommandResult(
            exit_code=stdout.channel.recv_exit_status(),
            stdout=stdout.read().decode(),
            stderr=stderr.read().decode()
        )

class HTTPEnvironmentAdapter(EnvironmentAdapter):
    """Adapter for HTTP API-based remote environments"""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key

    async def upload_file(self, file: File) -> bool:
        # HTTP multipart upload
        async with httpx.AsyncClient() as client:
            files = {"file": (file.path, file.content)}
            response = await client.post(
                f"{self.base_url}/files",
                files=files,
                headers={"Authorization": f"Bearer {self.api_key}"}
            )
            return response.status_code == 200

    async def send_message(self, request: MessageRequest) -> MessageResponse:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/chat",
                json=request.dict(),
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=120.0
            )
            return MessageResponse(**response.json())
```

---

#### 3.2.5 AgentEnvBuilder (Environment Template Builder)
**Responsibilities:**
- **Build and initialize** specific environment templates
- Process agent configuration (prompts, credentials, files)
- Prepare environment for communication
- Handle version-specific setup logic
- Register and manage environment templates

**The Builder Pattern:**

```python
class AgentEnvBuilder(ABC):
    """Abstract builder for environment templates"""

    env_name: str  # e.g., "python-env-basic"
    env_version: str  # e.g., "1.0.0"

    @abstractmethod
    async def build(
        self,
        environment: AgentEnvironment,
        agent: Agent,
        adapter: EnvironmentAdapter
    ) -> bool:
        """
        Build and configure the environment
        - environment: Environment instance to build
        - agent: Agent definition with prompts, credentials
        - adapter: Transport adapter for this environment
        - Returns: Success/failure
        """
        pass

class PythonEnvBasicBuilder_v1_0(AgentEnvBuilder):
    """Builder for python-env-basic:1.0.0"""

    env_name = "python-env-basic"
    env_version = "1.0.0"

    async def build(
        self,
        environment: AgentEnvironment,
        agent: Agent,
        adapter: EnvironmentAdapter
    ) -> bool:
        # 1. Set prompts
        await adapter.set_prompts(
            workflow_prompt=agent.workflow_prompt,
            entrypoint_prompt=agent.entrypoint_prompt
        )

        # 2. Set credentials
        credentials = decrypt_credentials(agent.credentials)
        await adapter.set_credentials(credentials)

        # 3. Upload initialization files
        init_files = self._prepare_init_files(agent)
        for file in init_files:
            await adapter.upload_file(file)

        # 4. Set environment config
        await adapter.set_config({
            "AGENT_ID": str(agent.id),
            "WORKSPACE_PATH": "/app/workspace",
            "LOG_LEVEL": "INFO"
        })

        # 5. Execute initialization script
        result = await adapter.execute_command("python /app/init_agent.py")
        if result.exit_code != 0:
            raise BuildError(f"Init failed: {result.stderr}")

        return True

    def _prepare_init_files(self, agent: Agent) -> list[File]:
        """Prepare files needed for this environment version"""
        return [
            File(
                path="config/agent.json",
                content=json.dumps({
                    "agent_id": str(agent.id),
                    "workflow_prompt": agent.workflow_prompt
                }).encode()
            )
        ]

# Builder Registry
ENVIRONMENT_BUILDERS = {
    ("python-env-basic", "1.0.0"): PythonEnvBasicBuilder_v1_0,
    ("python-env-basic", "1.1.0"): PythonEnvBasicBuilder_v1_1,
    ("nodejs-adk-env", "1.0.0"): NodeJSADKBuilder_v1_0,
}

def get_builder(env_name: str, env_version: str) -> AgentEnvBuilder:
    """Get builder for specific environment template and version"""
    builder_class = ENVIRONMENT_BUILDERS.get((env_name, env_version))
    if not builder_class:
        raise ValueError(f"No builder for {env_name}:{env_version}")
    return builder_class()
```

**Key Service Methods:**
- `get_adapter(environment: AgentEnvironment) -> EnvironmentAdapter`
- `get_builder(env_name: str, env_version: str) -> AgentEnvBuilder`
- `build_environment(env_id: UUID, agent: Agent) -> bool`
- `send_to_environment(env_id: UUID, request: MessageRequest) -> MessageResponse`
- `upload_file_to_environment(env_id: UUID, file: File) -> bool`

---

## 4. Communication Protocol (Updated for Environment Layer)

### 4.1 User → Backend → Environment → Agent Flow

1. **User sends message** (Frontend)
   - `POST /api/v1/sessions/{session_id}/messages`
   - Body: `{ "content": "user message" }`

2. **Backend validates and prepares** (Session Manager)
   - Validate user owns session
   - Lookup session → get environment_id
   - Validate environment is `running`
   - Create user message in DB
   - Retrieve session history (last N messages)

3. **Backend routes to environment** (Agent Communication Protocol Service)
   - Get protocol implementation for environment type
   - Prepare MessageRequest:
     ```python
     MessageRequest(
       session_id=session.id,
       message="user message",
       history=[...previous messages],
       context=session.metadata
     )
     ```
   - Call protocol's `send_message()` method

4. **Protocol implementation handles transport** (DockerAgentProtocol or RemoteAgentProtocol)
   - Docker: `POST http://localhost:{port}/chat`
   - Remote: `POST {base_url}/api/chat` with auth headers
   - Request body follows MessageRequest schema

5. **Environment receives and processes** (ADK Agent Server in container/remote)
   - Receives HTTP request
   - Extracts message and history
   - Passes to ADK agent with agent configuration (prompts, credentials)
   - ADK generates response
   - Returns MessageResponse

6. **Backend stores and returns** (Session Manager)
   - Create agent message in DB
   - Update session.last_message_at timestamp
   - Return response to user

7. **Frontend displays** (React)
   - Update chat UI with agent response in real-time

### 4.2 Environment Initialization Flow (With Builder Pattern)

When an environment starts, the builder orchestrates initialization through the adapter:

1. **User starts environment** (Frontend)
   - `POST /api/v1/environments/{env_id}/start`

2. **Backend loads environment and agent** (Environment Service)
   - Load `environment` from DB (contains env_name, env_version, type, config)
   - Load parent `agent` from DB (contains prompts, credentials)
   - Decrypt credentials

3. **Backend creates/starts runtime** (Environment Lifecycle Manager)
   - **Docker**: Create container from image `{env_name}:{env_version}`, start it
   - **SSH**: Verify SSH connection, ensure workspace directory exists
   - **HTTP**: Verify remote endpoint is accessible
   - Set environment.status = `starting`

4. **Backend gets adapter for environment** (Environment Service)
   ```python
   adapter = get_adapter(environment)
   # Returns: DockerEnvironmentAdapter, SSHEnvironmentAdapter, or HTTPEnvironmentAdapter
   ```

5. **Backend gets builder for environment template** (Environment Service)
   ```python
   builder = get_builder(environment.env_name, environment.env_version)
   # Returns: PythonEnvBasicBuilder_v1_0, etc.
   ```

6. **Builder initializes environment** (AgentEnvBuilder)
   ```python
   await builder.build(environment, agent, adapter)
   ```
   Builder performs version-specific initialization:
   - Calls `adapter.set_prompts(workflow_prompt, entrypoint_prompt)`
   - Calls `adapter.set_credentials(decrypted_credentials)`
   - Calls `adapter.upload_file(init_file)` for each init file
   - Calls `adapter.set_config(env_vars)`
   - Calls `adapter.execute_command("python /app/init_agent.py")`
   - Each adapter method uses appropriate transport (Docker SDK, SSH, HTTP)

7. **Backend verifies and updates status** (Environment Service)
   - Call `adapter.health_check()` to verify environment is ready
   - Set environment.status = `running`
   - Set environment.last_health_check = now
   - Return success to user

**Example: Docker Adapter Upload File Implementation**
```python
# Inside DockerEnvironmentAdapter
async def upload_file(self, file: File) -> bool:
    # Transport: Docker SDK
    import docker
    client = docker.from_env()
    container = client.containers.get(self.container_id)

    # Create tar archive with file content
    tar_stream = create_tar_archive(file.path, file.content)

    # Copy into container
    container.put_archive(
        path=self.workspace_path,
        data=tar_stream
    )
    return True
```

**Example: SSH Adapter Upload File Implementation**
```python
# Inside SSHEnvironmentAdapter
async def upload_file(self, file: File) -> bool:
    # Transport: SFTP over SSH
    remote_path = f"{self.workspace_path}/{file.path}"

    # Ensure parent directory exists
    remote_dir = os.path.dirname(remote_path)
    self.sftp.mkdir(remote_dir, ignore_existing=True)

    # Upload via SFTP
    self.sftp.putfo(BytesIO(file.content), remote_path)
    return True
```

### 4.3 Environment-to-Backend Communication

Environments may need to:
- **Query database**: Direct PostgreSQL connection via shared network (Docker) or connection string (Remote)
- **Access external APIs**: Via internet (both Docker and Remote)
- **Store files**: Via mounted volumes (Docker) or remote storage (Remote)
- **Log events**: Send logs back to backend for monitoring

**Network Configuration (Docker):**
- Containers join custom Docker network `agent-network`
- Backend exposes PostgreSQL on network at `postgres:5432`
- Containers have internet access (configurable per environment)

**Security:**
- Credentials passed securely during initialization (not stored in container config)
- Docker volumes are user-specific (isolated per agent owner)
- Remote environments use TLS and API key authentication

### 4.4 Real-time Updates (Optional Enhancement)

Use WebSockets for live message streaming:
- User connects to `ws://backend/sessions/{session_id}/stream`
- Backend calls `protocol.stream_message()` instead of `send_message()`
- Environment streams response chunks via AsyncIterator
- Backend forwards chunks to WebSocket
- Frontend updates UI in real-time (typing effect)

### 4.5 Session Modes: Building vs Conversation

Sessions operate in one of two modes that fundamentally change how the agent interacts with the user and what capabilities are available. This separation allows for **efficient context window management** and **role-specific agent behavior**.

#### 4.5.1 Building Mode

**Purpose:** Construct and configure the agent's environment, tools, scripts, and capabilities.

**When to Use:**
- Initial agent setup and configuration
- Creating automation scripts and workflows
- Defining data processing pipelines
- Setting up integrations (email servers, APIs, databases)
- Writing utility functions and helper scripts
- Configuring file structures and templates

**Agent Capabilities in Building Mode:**
- **Full development environment access**: Can create, modify, and delete files
- **Code generation expertise**: Receives comprehensive instructions on:
  - How to write Python/JavaScript/shell scripts
  - How to structure configuration files (JSON, YAML, TOML)
  - How to connect to external services (SMTP, IMAP, APIs)
  - How to work with databases and queues
  - Best practices for error handling and logging
  - How to use the agent's credential system
- **Extended context window**: Can reference extensive documentation and examples
- **Interactive configuration**: Asks clarifying questions about requirements
- **File system operations**: Create directories, organize workspace, manage dependencies

**Example Building Mode Interactions:**
```
User: "Set up email processing capability"
Agent:
  1. Creates email_connector.py with IMAP connection logic
  2. Creates email_queue.py for local DB storage
  3. Creates email_analyzer.py with analysis functions
  4. Sets up configuration file email_config.json
  5. Creates test scripts for validation
  6. Documents the setup in README.md

User: "Add error handling and logging"
Agent:
  - Modifies scripts to add try/catch blocks
  - Adds logging configuration
  - Creates error notification system
```

**System Prompt Characteristics (Building Mode):**
```
You are an expert software engineer helping to build an agent's capabilities.
Your role is to:
- Write production-quality code following best practices
- Create reusable scripts and utilities
- Configure integrations with external services
- Structure the workspace efficiently
- Document everything clearly
- Ask questions when requirements are ambiguous

You have access to:
- File creation and modification tools
- Code execution for testing
- Package installation
- Configuration management
- Credentials for external services

Focus on creating robust, maintainable solutions.
```

#### 4.5.2 Conversation Mode

**Purpose:** Use the pre-built environment to execute tasks and interact naturally.

**When to Use:**
- Regular day-to-day operations
- Executing pre-defined workflows
- Quick task execution
- Natural conversation without configuration
- Production use of the agent

**Agent Capabilities in Conversation Mode:**
- **Execution-focused**: Uses pre-built tools, scripts, and workflows
- **Minimal context**: Does not receive development/coding instructions
- **Task-oriented**: Focuses on using existing capabilities, not building new ones
- **Fast responses**: Smaller context window = faster, more focused responses
- **Limited file operations**: Can read and modify data files, but not core scripts
- **User-friendly**: Natural language interaction for task execution

**Example Conversation Mode Interactions:**
```
User: "Process my emails"
Agent:
  - Runs existing email_connector.py script
  - Processes emails using email_analyzer.py
  - Stores results in database queue
  - Returns summary: "Processed 47 emails, 3 require attention"

User: "Show me the urgent emails"
Agent:
  - Queries local database queue
  - Filters by urgency flag
  - Presents formatted list

User: "Analyze sentiment trends this week"
Agent:
  - Uses pre-built analysis scripts
  - Generates report from stored data
  - Returns insights and visualizations
```

**System Prompt Characteristics (Conversation Mode):**
```
You are a helpful AI assistant with specialized capabilities.

Your environment includes:
- Email processing tools (check_email, analyze_email, queue_email)
- Data analysis scripts (sentiment_analysis, trend_report)
- Database access for queued items
- Configured integrations (IMAP, SMTP)

When users ask you to perform tasks:
- Use the existing tools and scripts in your environment
- Execute workflows efficiently
- Provide clear summaries of results
- Ask for clarification only when necessary

You focus on execution, not development. If a capability doesn't exist,
inform the user to switch to Building mode to create it.
```

#### 4.5.3 Mode Switching

**Workflow:**
1. **Initial Setup**: User creates agent → starts first session in **Building Mode**
2. **Construction Phase**: Build scripts, tools, configurations (Building Mode)
3. **Testing**: Validate that tools work correctly (Building Mode)
4. **Switch to Production**: Change session mode to **Conversation Mode**
5. **Daily Use**: Execute tasks using built capabilities (Conversation Mode)
6. **Maintenance**: Switch back to **Building Mode** when changes needed

**Implementation:**

```python
# Session mode field in database
class Session(SQLModel, table=True):
    # ... other fields ...
    mode: Literal["building", "conversation"] = "conversation"  # Default

# API endpoint to switch modes
@router.patch("/api/v1/sessions/{session_id}/mode")
def switch_session_mode(
    session_id: UUID,
    new_mode: Literal["building", "conversation"],
    current_user: CurrentUser,
    session: SessionDep
) -> Session:
    db_session = get_session_by_id(session_id)
    validate_ownership(db_session, current_user)

    db_session.mode = new_mode
    db_session.metadata["mode_switched_at"] = datetime.utcnow()
    db_session.metadata["mode_switch_count"] =
        db_session.metadata.get("mode_switch_count", 0) + 1

    session.commit()
    return db_session

# Message sending includes mode context
@router.post("/api/v1/sessions/{session_id}/messages")
async def send_message(
    session_id: UUID,
    message: MessageCreate,
    current_user: CurrentUser
) -> Message:
    db_session = get_session_by_id(session_id)
    environment = get_environment(db_session.environment_id)

    # Get mode-specific system prompt
    system_prompt = get_system_prompt_for_mode(
        mode=db_session.mode,
        agent=environment.agent,
        workspace_files=list_workspace_files(environment)
    )

    # Send to agent with mode context
    request = MessageRequest(
        session_id=session_id,
        message=message.content,
        history=get_message_history(session_id),
        context={
            "mode": db_session.mode,
            "system_prompt": system_prompt,
            "available_tools": get_tools_for_mode(db_session.mode)
        }
    )

    adapter = get_adapter(environment)
    response = await adapter.send_message(request)

    # Store message and return
    return store_message(session_id, response)
```

#### 4.5.4 Context Window Management

**Building Mode:**
- **Larger context**: Includes comprehensive development documentation
  - Code examples (Python, JavaScript, shell scripting)
  - API documentation for external services
  - Configuration file format specifications
  - Error handling patterns
  - Testing best practices
- **Token allocation**: ~50-70% for system instructions, 30-50% for conversation

**Conversation Mode:**
- **Smaller context**: Focused on task execution
  - List of available tools and scripts (names + brief descriptions)
  - Current workspace structure overview
  - Recent conversation history
  - Task-specific context only
- **Token allocation**: ~10-20% for system instructions, 80-90% for conversation
- **Result**: Faster responses, lower cost, more conversation history

#### 4.5.5 UI/UX Considerations

**Frontend Indicators:**
- Mode badge on chat interface: 🔨 "Building Mode" or 💬 "Conversation Mode"
- Different color schemes (orange for building, blue for conversation)
- Mode switch toggle in session header
- Confirmation dialog when switching modes
- Quick tips based on current mode

**Example UI Layout:**
```
┌─────────────────────────────────────────────┐
│ Agent: Email Assistant  [🔨 Building Mode]  │
│ [Switch to Conversation Mode]               │
├─────────────────────────────────────────────┤
│ 💬 Building Mode: You can create scripts,   │
│    modify files, and configure integrations │
├─────────────────────────────────────────────┤
│ User: Set up email processing               │
│                                             │
│ Agent: I'll create the email processing     │
│ system. I'll need to:                       │
│ 1. Create email_connector.py for IMAP...   │
│                                             │
└─────────────────────────────────────────────┘

(After switching to Conversation Mode)

┌─────────────────────────────────────────────┐
│ Agent: Email Assistant [💬 Conversation Mode]│
│ [Switch to Building Mode]                   │
├─────────────────────────────────────────────┤
│ 💬 Conversation Mode: Use built-in tools    │
│    for quick task execution                 │
├─────────────────────────────────────────────┤
│ User: Process my emails                     │
│                                             │
│ Agent: ✓ Processed 47 emails               │
│        • 3 urgent items                     │
│        • 12 require follow-up               │
│                                             │
└─────────────────────────────────────────────┘
```

#### 4.5.6 Benefits of Mode Separation

1. **Context Efficiency**:
   - Building mode: Comprehensive but used infrequently
   - Conversation mode: Lean and fast for daily use

2. **Cost Optimization**:
   - Conversation mode uses 50-70% fewer tokens per message
   - Reduced latency (smaller prompts = faster processing)

3. **Role Clarity**:
   - Users know when they're "building" vs "using"
   - Agent behavior is predictable and appropriate

4. **Better User Experience**:
   - Building mode: Patient, detailed, educational
   - Conversation mode: Fast, concise, task-focused

5. **Maintenance Workflow**:
   - Clear separation between development and production
   - Easy to iterate on capabilities without cluttering conversation

6. **Workspace Management**:
   - Building mode creates organized structure
   - Conversation mode respects structure and focuses on data

---

## 5. Container Configuration

### 5.1 Base Docker Image

**Pre-installed:**
- Python 3.11+
- Google ADK (`google-genai`)
- FastAPI (for exposing HTTP API inside container)
- Common ML libraries (numpy, pandas, etc.)
- Database drivers (psycopg2)

**Structure:**
```dockerfile
FROM python:3.11-slim
RUN pip install google-genai fastapi uvicorn psycopg2-binary
COPY agent_server.py /app/
WORKDIR /app
CMD ["uvicorn", "agent_server:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 5.2 Container Startup

**Volumes:**
- `/app/workspace`: Agent's persistent file storage
- `/app/credentials`: Mounted credentials (read-only)
- `/app/config`: Agent configuration files

**Environment Variables:**
- `AGENT_ID`: Unique agent identifier
- `SYSTEM_PROMPT`: Agent's system prompt
- `DATABASE_URL`: PostgreSQL connection string
- Custom credentials (from AgentCredential)

**Networking:**
- Join `agent-network` (custom bridge network)
- Expose port 8000 (agent HTTP API)
- Access to PostgreSQL at `postgres:5432`

### 5.3 Agent Server (Running in Container)

Python FastAPI server that:
1. Initializes ADK agent with system prompt
2. Exposes HTTP endpoint for chat
3. Manages ADK session state
4. Handles file system operations

**Example Structure:**
```python
# agent_server.py (inside container)
from fastapi import FastAPI
from google import genai

app = FastAPI()
agent = genai.Agent(system_prompt=os.getenv("SYSTEM_PROMPT"))

@app.post("/chat")
async def chat(request: ChatRequest):
    response = agent.chat(
        message=request.message,
        history=request.history
    )
    return {"response": response}

@app.get("/health")
async def health():
    return {"status": "healthy"}
```

---

## 6. Business Logic (Updated)

### 6.1 Agent Lifecycle (Simplified - No Runtime State)

Agents are **logical definitions** with no runtime state.

**States:**
- `is_active=True`: Agent available for creating sessions (default)
- `is_active=False`: Agent disabled, cannot create new sessions

**Operations:**
- User creates agent → stored in DB with prompts and credentials
- User updates prompts/credentials → affects all future environment initializations
- User deletes agent → cascades to all environments and sessions (soft delete recommended)
- User sets active environment → `agent.active_environment_id` updated

**No container management at this level** - that's handled by AgentEnvironment.

### 6.2 AgentEnvironment Lifecycle

**States:**
- `stopped`: Environment exists but not running (no container/no connection)
- `starting`: Environment being initialized
- `running`: Environment healthy and accepting requests
- `error`: Environment failed or unhealthy
- `deprecated`: Environment soft-deleted (kept for audit trail)

**Transitions:**
- `stopped → starting`: User starts environment
- `starting → running`: Initialization successful, health check passes
- `starting → error`: Initialization failed
- `running → stopped`: User manually stops OR auto-shutdown (inactivity timeout)
- `running → error`: Health check fails
- `error → starting`: User retries start
- Any state → `deprecated`: User creates new environment and deprecates old one

**Blue-Green Deployment Example:**
1. Agent has Environment A (`is_active=True`, `status=running`)
2. User creates Environment B with new Docker image (`is_active=False`, `status=stopped`)
3. User starts Environment B → becomes `running`
4. User tests Environment B (creates test sessions)
5. User activates Environment B → `agent.active_environment_id` switches
6. Environment A automatically set `is_active=False` (but still running)
7. If issues: user switches back to Environment A instantly
8. If stable: user stops/deprecates Environment A

**Auto-shutdown (Optional):**
- Stop environments after N minutes of inactivity (no new messages in any session)
- Configurable per environment or globally
- Only affects non-active environments (active stays running)

### 6.3 Session Lifecycle (Unchanged)

**States:**
- `active`: Currently in use, can send/receive messages
- `paused`: User paused, no new messages
- `completed`: User ended session (archived)
- `error`: Session failed (environment error, timeout, etc.)

**Transitions:**
- Created → `active`: Session starts when created
- `active ↔ paused`: User can pause/resume
- `active → completed`: User ends session
- `active → error`: Environment failure or timeout
- `error → active`: User retries (if environment recovers)

**Important:** Sessions are tied to specific environment, not agent. If environment is deprecated, sessions persist for historical record.

### 6.4 Message Processing Rules

1. **Ordering**: Messages processed in sequence (no concurrent processing per session)
2. **Timeout**: Agent must respond within configurable timeout (default 60s)
3. **Retry**: On agent failure, retry up to 3 times with exponential backoff
4. **Context Window**: Include last N messages in context (configurable, default 20)
5. **Rate Limiting**: Max M messages per minute per user (prevent abuse)

### 6.5 Permissions & Access Control

**Agent Access:**
- Users can only interact with their own agents
- Admins can view all agents

**Environment Access:**
- Users can only manage environments for their own agents
- Admins can view/manage all environments

**Session Access:**
- Users can only view/interact with their own sessions
- Sessions are private by default
- (Future) Share sessions with other users

**Credential Access:**
- Credentials encrypted in database
- Only accessible during environment initialization
- Never exposed via API (only metadata)

---

## 7. API Endpoints (Updated for Environment Layer)

### 7.1 Agent Management (Logical Layer)

```
POST   /api/v1/agents                           # Create new agent (logical definition)
GET    /api/v1/agents                           # List user's agents
GET    /api/v1/agents/{agent_id}                # Get agent details
PATCH  /api/v1/agents/{agent_id}                # Update agent (name, prompts, description)
DELETE /api/v1/agents/{agent_id}                # Delete agent (cascades to environments)
GET    /api/v1/agents/{agent_id}/credentials    # Get linked credentials
POST   /api/v1/agents/{agent_id}/credentials    # Link credential to agent
DELETE /api/v1/agents/{agent_id}/credentials/{credential_id}  # Unlink credential
```

### 7.2 Environment Management (Runtime Layer)

```
POST   /api/v1/agents/{agent_id}/environments               # Create new environment for agent
GET    /api/v1/agents/{agent_id}/environments               # List agent's environments
GET    /api/v1/environments/{env_id}                        # Get environment details
PATCH  /api/v1/environments/{env_id}                        # Update environment config
DELETE /api/v1/environments/{env_id}                        # Delete environment (stops if running)

POST   /api/v1/environments/{env_id}/start                  # Start environment
POST   /api/v1/environments/{env_id}/stop                   # Stop environment
POST   /api/v1/environments/{env_id}/restart                # Restart environment
GET    /api/v1/environments/{env_id}/status                 # Get current status
GET    /api/v1/environments/{env_id}/health                 # Health check
GET    /api/v1/environments/{env_id}/logs                   # Get logs

POST   /api/v1/agents/{agent_id}/environments/{env_id}/activate  # Set as active environment
```

### 7.3 Session Management (Conversation Layer)

```
POST   /api/v1/sessions                         # Create new session (requires agent_id, uses active env, optional mode)
GET    /api/v1/sessions                         # List user's sessions
GET    /api/v1/agents/{agent_id}/sessions       # List sessions for an agent (all environments)
GET    /api/v1/environments/{env_id}/sessions   # List sessions for specific environment
GET    /api/v1/sessions/{session_id}            # Get session details
DELETE /api/v1/sessions/{session_id}            # Delete session
PATCH  /api/v1/sessions/{session_id}            # Update (title, status, mode)
PATCH  /api/v1/sessions/{session_id}/mode       # Switch session mode (building <-> conversation)
```

### 7.4 Message Management

```
GET    /api/v1/sessions/{session_id}/messages              # Get message history
POST   /api/v1/sessions/{session_id}/messages              # Send message to agent
GET    /api/v1/sessions/{session_id}/messages/{msg_id}    # Get specific message
WS     /api/v1/sessions/{session_id}/stream                # WebSocket for real-time streaming
```

### 7.5 Credentials Management (EXISTING - No Changes)

Already implemented in `backend/app/api/routes/` - credentials are managed separately and linked to agents.

---

## 8. Implementation Phases (Revised for Environment Architecture)

### Phase 1: Core Infrastructure & Data Models
**Goal:** Extend existing Agent model, add new models for AgentEnvironment, Session, Message

**Deliverables:**
1. **Database Migrations:**
   - Extend `Agent` model: add `description`, `is_active`, `active_environment_id`, timestamps
   - Create `AgentEnvironment` model with all attributes (type, status, config, etc.)
   - Create `Session` model (linked to environment, not agent directly, with `mode` field)
   - Create `Message` model with sequence numbers
2. **Pydantic Schemas:**
   - AgentCreate/Update schemas (extend existing)
   - EnvironmentCreate/Update/Public schemas
   - SessionCreate/Update/Public schemas (include mode field)
   - MessageCreate/Public schemas
   - SessionMode enum: `building` | `conversation`
3. **Basic CRUD in `crud.py`:**
   - Agent CRUD (extend existing)
   - Environment CRUD operations
   - Session CRUD operations
   - Message CRUD operations
4. **API Routes (basic, no Docker yet):**
   - `/api/v1/agents/*` (extend existing routes)
   - `/api/v1/environments/*` (new routes)
   - `/api/v1/sessions/*` (new routes)
   - `/api/v1/sessions/{id}/messages` (new routes)
5. **Authorization:**
   - Ensure users can only access their own agents/environments/sessions
   - Dependency injection for ownership validation

**Validation:**
- Can create/update agents with new fields via API
- Can create environments linked to agents via API
- Can create sessions linked to environments via API
- Can create messages in sessions via API
- Data persists correctly in PostgreSQL
- Authorization prevents cross-user access
- Alembic migrations apply cleanly

---

### Phase 2: Agent Communication Protocol (The Contract)
**Goal:** Define and implement the protocol/contract for environment communication

**Deliverables:**
1. **Protocol Definition:**
   - Create `app/services/agent_protocol.py`
   - Define abstract `AgentCommunicationProtocol` class
   - Define data models: `AgentConfig`, `MessageRequest`, `MessageResponse`, `HealthResponse`, `InitResponse`
   - Add mode context to `MessageRequest` (includes session mode)
2. **Mock Implementation (for testing):**
   - `MockAgentProtocol` class that returns canned responses
   - Allows testing without Docker
3. **Protocol Service:**
   - `get_protocol(environment: AgentEnvironment) -> AgentCommunicationProtocol`
   - Factory pattern to return correct protocol based on environment type
4. **Mode-Specific System Prompts:**
   - Create `app/services/mode_prompts.py`
   - `get_system_prompt_for_mode(mode, agent, workspace_files)` function
   - Building mode prompt: comprehensive development instructions
   - Conversation mode prompt: task execution focus
5. **Integration with Session Manager:**
   - Update `/api/v1/sessions/{id}/messages` POST endpoint
   - Include session mode in message context
   - Use mode-specific system prompts
   - Store responses in database
6. **Error Handling:**
   - Timeouts
   - Retries with exponential backoff
   - Environment unreachable errors

**Validation:**
- Can send message to session via API
- Mock protocol returns response
- Response stored in database as agent message
- Error handling works (timeout, unreachable)
- Message history can be retrieved

---

### Phase 3: Docker Environment Implementation
**Goal:** Implement Docker-specific environment lifecycle and ADK agent server

**Deliverables:**
1. **Environment Lifecycle Manager:**
   - Create `app/services/environment_manager.py`
   - Implement Docker SDK integration (container create/start/stop/delete)
   - Implement volume mounting for workspaces
   - Implement credential mounting (as env vars or files)
   - Implement Docker network configuration
   - Health check implementation using `protocol.health_check()`
2. **Base Docker Image:**
   - Create `Dockerfile` for agent base image
   - Install Python, Google ADK, FastAPI, uvicorn, psycopg2
   - Copy `agent_server.py` template
   - Expose port 8000
   - Build and push to registry
3. **ADK Agent Server Template:**
   - Create `agent_server.py` that implements protocol contract
   - `/initialize` endpoint: receive AgentConfig, set up ADK agent
   - `/chat` endpoint: receive MessageRequest, return MessageResponse
   - `/health` endpoint: return HealthResponse
   - ADK agent initialization with prompts
4. **Docker Protocol Implementation:**
   - `DockerAgentProtocol` class in `agent_protocol.py`
   - HTTP client to communicate with container
   - Port mapping and container discovery
5. **Environment API Integration:**
   - Wire up `/api/v1/environments/{id}/start` endpoint
   - Wire up `/api/v1/environments/{id}/stop` endpoint
   - Update environment status in database

**Validation:**
- Can create Docker environment for an agent via API
- Starting environment creates and starts Docker container
- Container initializes ADK agent with prompts and credentials
- Health check returns healthy status
- Can send message to session, routed to Docker container
- ADK agent responds with meaningful answer
- Response flows back to frontend
- Stopping environment stops Docker container
- Environment logs accessible

---

### Phase 4: Frontend UI
**Goal:** Build React interface for agents, environments, and sessions

**Deliverables:**
1. **Regenerate OpenAPI Client:**
   - Run `generate-client.sh` after Phase 1-3 backend changes
   - Verify new services/types generated (including SessionMode enum)
2. **Agent Management UI:**
   - Extend existing agent management (already has CRUD from credentials feature)
   - Add environment list for each agent
   - Add "Create Environment" button
   - Add "Start/Stop Environment" buttons
   - Show environment status badges
3. **Environment Management UI:**
   - Environment detail view (status, logs, config)
   - "Set as Active" button
   - Health status indicator
4. **Dashboard with Session Creation:**
   - Dashboard route at `/` (extend existing)
   - "New Session" button
   - Agent selector dropdown (only active agents)
   - Mode selector: Building or Conversation (default: Conversation)
   - Creates session against agent's active environment with selected mode
5. **Chat Interface:**
   - New route `/sessions/{session_id}`
   - Mode indicator badge (🔨 Building / 💬 Conversation)
   - Mode switch toggle in header
   - Confirmation dialog when switching modes
   - Color scheme changes based on mode (orange/blue)
   - Quick tips panel based on current mode
   - Chat UI with message bubbles (user vs agent)
   - Text input and send button
   - Session title display (editable)
   - Message history loads on mount
   - Real-time message updates
6. **Session List:**
   - Route `/sessions` to list all user sessions
   - Show mode badge for each session
   - Filter by mode (Building / Conversation / All)
   - Group by agent or show flat list
   - Click to navigate to chat interface

**Validation:**
- User can create agent from UI
- User can create environment for agent from UI
- User can start/stop environment from UI
- User can create new session from dashboard with mode selection
- User can switch session mode via toggle in chat interface
- Mode indicator badge displays correctly (🔨 / 💬)
- Color scheme changes when switching modes
- Mode-specific system prompts affect agent behavior
- User can send messages in chat interface
- Chat displays messages correctly with user/agent distinction
- Session list shows all sessions with mode badges
- Can filter sessions by mode
- Can navigate between different sessions

---

### Phase 5: Polish & Production Features
**Goal:** Add production-ready features, monitoring, and enhancements

**Deliverables:**
1. **WebSocket Streaming:**
   - Implement `stream_message()` in protocol
   - WebSocket endpoint `/api/v1/sessions/{id}/stream`
   - Update ADK agent server to support streaming
   - Frontend WebSocket integration for real-time typing
2. **Environment Auto-Shutdown:**
   - Background task to check environment inactivity
   - Configurable timeout per environment
   - Only affects non-active environments
3. **Blue-Green Deployment UI:**
   - "Switch Active Environment" workflow in frontend
   - Visual indicator of active vs inactive environments
   - Rollback feature
4. **Resource Limits:**
   - Docker container CPU/memory limits in environment config
   - UI to configure limits
5. **Monitoring & Logging:**
   - Centralized logging for all environments
   - Metrics: message count, response time, error rate
   - Admin dashboard for monitoring
6. **Rate Limiting:**
   - Per-user message rate limits
   - Per-session concurrency limits
7. **Session Features:**
   - Session search and filtering
   - Export session as JSON/PDF
   - Auto-generate session title from first message
8. **Remote Environment Support (Optional):**
   - Implement `RemoteAgentProtocol`
   - UI to configure remote environment endpoint

**Validation:**
- WebSocket streaming works smoothly
- Inactive environments auto-shutdown
- Can switch between environments without downtime
- Resource limits enforced
- Logs and metrics collected
- Rate limiting prevents abuse
- Session export works
- System handles 100+ concurrent sessions
- Production deployment successful

---

## 9. Open Questions & Decisions Needed

### 9.1 Technical Decisions
- [ ] **Message Storage**: PostgreSQL vs. separate message store (Redis, MongoDB)?
- [ ] **Real-time**: WebSockets, Server-Sent Events, or polling?
- [ ] **Container Orchestration**: Docker SDK vs. Docker Compose vs. Kubernetes (future)?
- [ ] **Credential Encryption**: Which library/algorithm (Fernet, AES-256)?
- [ ] **ADK Version**: Which version of Google ADK to use?

### 9.2 Business Logic
- [ ] **Auto-shutdown**: When to stop inactive agent containers?
- [ ] **Resource Limits**: CPU/memory limits per container?
- [ ] **Concurrency**: Max sessions per agent? Per user?
- [ ] **Rate Limiting**: Messages per minute limits?
- [ ] **File Storage**: Size limits for agent workspace?

### 9.3 User Experience
- [ ] **Session Titles**: Auto-generate from first message or require user input?
- [ ] **Default Agent**: Should users have a default agent selection?
- [ ] **Message Editing**: Can users edit sent messages?
- [ ] **Session Templates**: Pre-configured conversation starters?

---

## 10. Success Criteria

### 10.1 Functional Requirements
✅ Users can create and configure agents
✅ Users can start/stop agent containers
✅ Users can create multiple sessions per agent
✅ Users can create sessions in Building or Conversation mode
✅ Users can switch between Building and Conversation modes mid-session
✅ Agent behavior adapts to session mode (development vs execution)
✅ Building mode provides comprehensive coding/configuration capabilities
✅ Conversation mode focuses on task execution with pre-built tools
✅ Users can send messages and receive agent responses
✅ Message history persisted and retrievable
✅ Credentials securely stored and mounted
✅ Containers properly isolated
✅ Sessions maintain independent histories

### 10.2 Non-Functional Requirements
✅ Response time: < 2s for message round-trip (excluding agent processing)
✅ Conversation mode: 30-50% faster response than Building mode (due to smaller context)
✅ Context efficiency: Conversation mode uses 50-70% fewer tokens than Building mode
✅ Concurrent sessions: Support 100+ active sessions
✅ Uptime: 99%+ availability
✅ Security: Credentials encrypted, proper isolation
✅ Scalability: Can add more agent types without code changes

---

## 11. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Container resource exhaustion | High | Implement CPU/memory limits, auto-shutdown |
| Agent container crashes | Medium | Health checks, automatic restart, error handling |
| Slow agent responses | Medium | Timeouts, async processing, user feedback |
| Credential leakage | High | Encryption at rest, secure mounting, audit logs |
| Docker daemon failure | High | Monitoring, alerting, graceful degradation |
| Database connection limits | Medium | Connection pooling, limit concurrent sessions |

---

## 12. Future Enhancements

### 12.1 Short-term (Next 3-6 months)
- Agent marketplace (share agent configs)
- Voice input/output support
- File upload in chat
- Session export (PDF, JSON)

### 12.2 Long-term (6+ months)
- Multi-agent collaboration (agents talking to each other)
- Fine-tuning custom agents
- Integration with external tools (Zapier, etc.)
- Mobile app
- Agent analytics and insights

---

## 13. Version History

| Version | Date | Changes |
|---------|------|---------|
| v1.0 | 2025-12-22 | Initial specification with environment abstraction layer |
| v1.1 | 2025-12-23 | Added Building vs Conversation session modes for context window optimization |

---

**Document Status**: Draft v1.1
**Last Updated**: 2025-12-23
**Next Review**: After Phase 1 completion
