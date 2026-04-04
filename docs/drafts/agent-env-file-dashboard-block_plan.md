# Agent Env File Dashboard Block — Implementation Plan

## Overview

`agent_env_file` extends the user dashboard block system with a new view type that renders a
workspace file from an agent environment directly inside a dashboard block. The user selects
which environment (from the block's agent) and which workspace file to display. The same
pure-content viewer components used by the environment panel (CSVViewer, MarkdownViewer,
JSONViewer, TextViewer) are reused inside the block.

**Core capabilities:**
- Dashboard blocks support a new `view_type = "agent_env_file"`
- Block `config` stores `{env_id, file_path}` — no new DB migration needed
- File content is served via a new API endpoint on the dashboards router
- DockerEnvironmentAdapter reads files directly from local disk without requiring the
  container to be running (via `LocalFilesAccessInterface`)
- Fallback to container HTTP proxy if the adapter does not support local file access
- Auto-refreshes every 30 seconds like all other block views

**High-level flow:**
```
User adds block → selects "Agent Env File" → picks env + file path → block saved
Dashboard view → AgentEnvFileView fetches /dashboards/{id}/blocks/{bid}/env-file?path=...
  → backend: local file access (no container needed) → returns file bytes as text
  → frontend renders with appropriate viewer (CSV/Markdown/JSON/Text)
```

---

## Architecture Overview

```
UserDashboardBlock (view_type="agent_env_file", config={env_id, file_path})
  │
  ├── AgentEnvFileView (frontend component)
  │       └── GET /api/v1/dashboards/{id}/blocks/{bid}/env-file?path=...
  │                  │
  │                  ├── LocalFilesAccessInterface (supported by DockerEnvironmentAdapter)
  │                  │       └── reads workspace file directly from disk
  │                  │
  │                  └── Fallback: adapter.download_workspace_item()
  │                              (requires environment to be running)
  │
  └── AddBlockDialog / EditBlockDialog (env selector + file path input)
```

**Integration points:**
- `backend/app/services/environments/adapters/base.py` — add `LocalFilesAccessInterface`
- `backend/app/services/environments/adapters/docker_adapter.py` — implement `LocalFilesAccessInterface`
- `backend/app/api/routes/user_dashboards.py` — add `env-file` endpoint
- `backend/app/models/users/user_dashboard.py` — update Literal type to include `"agent_env_file"`
- `backend/app/services/users/user_dashboard_service.py` — update `add_block()` validation
- `frontend/src/components/Dashboard/UserDashboards/views/AgentEnvFileView.tsx` — new component
- `frontend/src/components/Dashboard/UserDashboards/AddBlockDialog.tsx` — env/file selectors
- `frontend/src/components/Dashboard/UserDashboards/EditBlockDialog.tsx` — env/file edit fields
- `frontend/src/components/Dashboard/UserDashboards/DashboardBlock.tsx` — renderView() case

---

## Data Models

### No new database migration required

The `user_dashboard_block` table already has a `config: JSON | null` column. For
`view_type = "agent_env_file"`, the block's `config` stores:

```json
{
  "env_id": "uuid-string",
  "file_path": "files/report.csv"
}
```

Both fields are required when `view_type = "agent_env_file"`.

### Schema changes (no migration)

**File:** `backend/app/models/users/user_dashboard.py`

Update `view_type` Literal in `UserDashboardBlockBase` and `UserDashboardBlockUpdate`:

```python
# Before:
view_type: Literal["webapp", "latest_session", "latest_tasks"] = Field(default="latest_session")

# After:
view_type: Literal["webapp", "latest_session", "latest_tasks", "agent_env_file"] = Field(default="latest_session")
```

Apply in both `UserDashboardBlockBase` and `UserDashboardBlockUpdate`. The DB model (`UserDashboardBlock`) keeps `view_type: str` unchanged.

---

## Security Architecture

- **Ownership chain**: dashboard → block → agent → env. Each step verified:
  1. Dashboard owned by `current_user` (via `UserDashboardService.get_dashboard()`)
  2. Block belongs to dashboard (via `UserDashboardService._get_block()`)
  3. Block view_type is `"agent_env_file"` (else 400)
  4. `env.agent_id == block.agent_id` (else 400)
  5. `agent.owner_id == current_user.id` (else 403)
- **Path traversal prevention**: `LocalFilesAccessInterface.get_local_workspace_file_path()`
  resolves the path and checks it remains inside `{env_dir}/app/workspace/`
- **No credential exposure**: only file content is returned, not any credentials or config
- **File size consideration**: files could be large; stream response rather than buffering
- **Supported file types**: same as environment panel — CSV, Markdown, JSON, TXT, LOG;
  other types return content too (frontend will show unsupported message)

---

## Backend Implementation

### 1. LocalFilesAccessInterface

**File:** `backend/app/services/environments/adapters/base.py`

Add after the existing class definitions, before `EnvironmentAdapter`:

```python
class LocalFilesAccessInterface(ABC):
    """
    Optional mixin for adapters that can provide direct local filesystem access
    to workspace files without requiring the container to be running.

    Adapters that implement this interface allow features like dashboard blocks
    to read files directly from disk. This is an optional capability — adapters
    that do NOT implement it will fall back to the standard download_workspace_item()
    path, which requires the container to be running.

    In distributed or cloud environments, adapters may still implement this interface
    if they auto-sync workspace files to a local cache directory.
    """

    @abstractmethod
    def get_local_workspace_file_path(self, relative_path: str) -> Path | None:
        """
        Return the absolute local filesystem path for a workspace file,
        or None if the file is not accessible locally.

        Args:
            relative_path: Path relative to the workspace root (e.g., "files/data.csv").
                           Must not contain absolute paths or directory traversal sequences.

        Returns:
            Absolute Path object if the file exists and is safely accessible.
            None if the file does not exist, is outside the workspace boundary,
            or if the relative_path contains traversal sequences.

        Security: Implementations MUST validate that the resolved path stays within
                  the workspace directory to prevent directory traversal attacks.
        """
        pass
```

Import `Path` from `pathlib` is already present in `base.py`.

### 2. DockerEnvironmentAdapter implements LocalFilesAccessInterface

**File:** `backend/app/services/environments/adapters/docker_adapter.py`

Change class signature:
```python
from .base import (
    EnvironmentAdapter,
    LocalFilesAccessInterface,  # add this import
    EnvInitConfig,
    ...
)

class DockerEnvironmentAdapter(EnvironmentAdapter, LocalFilesAccessInterface):
```

Add method:
```python
def get_local_workspace_file_path(self, relative_path: str) -> Path | None:
    """
    Return the absolute path to a workspace file on the local filesystem.

    DockerEnvironmentAdapter stores workspace files at {env_dir}/app/workspace/,
    which is mounted as a Docker volume and accessible directly on the host.

    Security: resolves symlinks and checks the result stays within workspace_dir.
    """
    workspace_dir = self.env_dir / "app" / "workspace"
    # Reject obvious traversal early
    if ".." in relative_path or relative_path.startswith("/"):
        logger.warning(f"Path traversal attempt rejected: {relative_path}")
        return None
    try:
        candidate = (workspace_dir / relative_path).resolve()
        workspace_resolved = workspace_dir.resolve()
        if not str(candidate).startswith(str(workspace_resolved) + "/") and candidate != workspace_resolved:
            logger.warning(f"Path {relative_path!r} resolved outside workspace: {candidate}")
            return None
        if not candidate.exists() or not candidate.is_file():
            return None
        return candidate
    except Exception as e:
        logger.warning(f"Error resolving workspace path {relative_path!r}: {e}")
        return None
```

Place this method in the `# === File Operations ===` section, before `upload_file`.

### 3. API Endpoint

**File:** `backend/app/api/routes/user_dashboards.py`

Add import at top of file:
```python
import uuid
from pathlib import Path
from fastapi.responses import StreamingResponse
from app.models import AgentEnvironment, Agent
from app.services.environment_service import EnvironmentService
from app.services.adapters.base import LocalFilesAccessInterface
```

Add the following endpoint. Register it **before** the `/{dashboard_id}/blocks/{block_id}` PUT/DELETE routes to avoid path conflicts (add after the `latest-session` endpoint):

```python
@router.get("/{dashboard_id}/blocks/{block_id}/env-file")
async def get_block_env_file(
    session: SessionDep,
    current_user: CurrentUser,
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    path: str,  # query parameter: relative path within workspace
) -> StreamingResponse:
    """
    Stream the content of a workspace file referenced by an agent_env_file block.

    Uses local filesystem access when supported by the adapter (no container needed).
    Falls back to HTTP proxy when the adapter does not support local access (requires
    environment to be running).

    Args:
        dashboard_id: Dashboard UUID
        block_id: Block UUID
        path: Relative path within workspace (e.g., "files/report.csv")

    Returns:
        StreamingResponse with text/plain content

    Raises:
        404: Dashboard, block, or file not found
        400: Block is not agent_env_file type, or env/agent mismatch, or env not running (fallback)
        403: Dashboard not owned by current user
    """
    # 1. Verify dashboard ownership and get block
    dashboard = UserDashboardService.get_dashboard(
        session=session, dashboard_id=dashboard_id, owner_id=current_user.id
    )
    block = UserDashboardService._get_block(
        session=session, dashboard_id=dashboard_id, block_id=block_id
    )

    # 2. Validate block type
    if block.view_type != "agent_env_file":
        raise HTTPException(
            status_code=400,
            detail="Block is not of type agent_env_file"
        )

    # 3. Read config
    config = block.config or {}
    env_id_str = config.get("env_id")
    if not env_id_str:
        raise HTTPException(status_code=400, detail="Block config missing env_id")

    try:
        env_id = uuid.UUID(env_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Block config has invalid env_id")

    # 4. Verify environment exists and belongs to this block's agent
    environment = session.get(AgentEnvironment, env_id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    if environment.agent_id != block.agent_id:
        raise HTTPException(
            status_code=400,
            detail="Environment does not belong to this block's agent"
        )

    # 5. Verify agent ownership
    agent = session.get(Agent, environment.agent_id)
    if not agent or agent.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    # 6. Validate path (basic check; deeper validation in adapter)
    if not path or ".." in path or path.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid file path")

    # 7. Get adapter and attempt local file access first
    lifecycle_manager = EnvironmentService.get_lifecycle_manager()
    adapter = lifecycle_manager.get_adapter(environment)

    if isinstance(adapter, LocalFilesAccessInterface):
        # Local file access path — no container needed
        file_path = adapter.get_local_workspace_file_path(path)
        if file_path is None:
            raise HTTPException(status_code=404, detail="File not found")

        def stream_local_file():
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(
            stream_local_file(),
            media_type="text/plain; charset=utf-8",
            headers={"X-Accel-Buffering": "no"},
        )

    else:
        # Fallback: requires container to be running
        if environment.status != "running":
            raise HTTPException(
                status_code=400,
                detail=f"Environment must be running to access this file (current status: {environment.status})"
            )

        async def stream_remote_file():
            async for chunk in adapter.download_workspace_item(path):
                yield chunk

        return StreamingResponse(
            stream_remote_file(),
            media_type="text/plain; charset=utf-8",
            headers={"X-Accel-Buffering": "no"},
        )
```

**Note:** `_get_block` is already a private method on `UserDashboardService`. The route accesses it directly as a class method. If access from route level is needed, consider making it a module-level helper or exposing it. Currently, the route can call `UserDashboardService._get_block(session, dashboard_id, block_id)` — this is consistent with how the other routes work in this file (they already use `UserDashboardService` class methods).

**Route registration order** in `user_dashboards.py`: the env-file endpoint path is
`/{dashboard_id}/blocks/{block_id}/env-file`. FastAPI resolves this before
`/{dashboard_id}/blocks/{block_id}` (exact match takes priority) so no registration order
change is required beyond ensuring it's above the `/{action_id}` prompt-action routes (which
it naturally is, since those are under a different sub-path).

### 4. Service Layer Changes

**File:** `backend/app/services/users/user_dashboard_service.py`

Update `add_block()` to include `"agent_env_file"` in the valid view types set:

```python
# Before:
VALID_VIEW_TYPES = {"webapp", "latest_session", "latest_tasks"}

# After:
VALID_VIEW_TYPES = {"webapp", "latest_session", "latest_tasks", "agent_env_file"}
```

Also add config validation in `add_block()` for the new type:
```python
if view_type == "agent_env_file":
    config = data.config or {}
    if not config.get("env_id") or not config.get("file_path"):
        raise HTTPException(
            status_code=422,
            detail="agent_env_file blocks require config with env_id and file_path"
        )
```

Similarly in `update_block()` — if view_type is being changed to `"agent_env_file"`, validate config.

### 5. Model Exports

**File:** `backend/app/models/__init__.py`

Add `AgentEnvironment` and `Agent` to imports used in the routes file if not already present.
These are already exported — verify they're importable from `app.models`.

---

## Frontend Implementation

### 1. AgentEnvFileView Component

**File:** `frontend/src/components/Dashboard/UserDashboards/views/AgentEnvFileView.tsx`

```typescript
import { useQuery } from "@tanstack/react-query"
import { DashboardsService } from "@/client"
import { CSVViewer } from "@/components/Environment/CSVViewer"
import { MarkdownViewer } from "@/components/Environment/MarkdownViewer"
import { JSONViewer } from "@/components/Environment/JSONViewer"
import { TextViewer } from "@/components/Environment/TextViewer"
import { Loader2, AlertCircle } from "lucide-react"

interface AgentEnvFileViewProps {
  dashboardId: string
  blockId: string
  filePath: string
}

export function AgentEnvFileView({ dashboardId, blockId, filePath }: AgentEnvFileViewProps) {
  const filename = filePath.split("/").pop() || filePath
  const fileExtension = filename.split(".").pop()?.toLowerCase()

  const { data: fileContent, isLoading, error } = useQuery({
    queryKey: ["dashboardBlockEnvFile", dashboardId, blockId, filePath],
    queryFn: async () => {
      const response = await DashboardsService.getBlockEnvFile({
        dashboardId,
        blockId,
        path: filePath,
      })
      return response as unknown as string
    },
    enabled: !!dashboardId && !!blockId && !!filePath,
    refetchInterval: 30000,
    staleTime: 15000,
  })

  if (!filePath) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
        No file configured
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full gap-2 text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        <span className="text-sm">Loading file...</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-full gap-2 text-muted-foreground">
        <AlertCircle className="h-4 w-4 text-destructive" />
        <span className="text-sm">Failed to load file</span>
      </div>
    )
  }

  if (!fileContent) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
        Empty file
      </div>
    )
  }

  if (fileExtension === "csv") return <CSVViewer content={fileContent} />
  if (fileExtension === "md") return <MarkdownViewer content={fileContent} />
  if (fileExtension === "json") return <JSONViewer content={fileContent} />
  if (fileExtension === "txt" || fileExtension === "log") return <TextViewer content={fileContent} />

  return <TextViewer content={fileContent} />
}
```

**React Query key:** `["dashboardBlockEnvFile", dashboardId, blockId, filePath]`

### 2. DashboardBlock.tsx Changes

**File:** `frontend/src/components/Dashboard/UserDashboards/DashboardBlock.tsx`

Import the new component and add a case to `renderView()`:

```typescript
import { AgentEnvFileView } from "./views/AgentEnvFileView"

// Inside renderView():
if (block.view_type === "agent_env_file") {
  return (
    <AgentEnvFileView
      dashboardId={dashboardId}
      blockId={block.id}
      filePath={block.config?.file_path || ""}
    />
  )
}
```

The `dashboardId` prop is already available on `DashboardBlock` (verify it's passed or use
the dashboard context). If `dashboardId` is not a prop, it should be threaded from the parent
(`DashboardGrid` → `DashboardBlock`).

### 3. AddBlockDialog.tsx Changes

**File:** `frontend/src/components/Dashboard/UserDashboards/AddBlockDialog.tsx`

Changes needed:
1. Add `"agent_env_file"` option to the view type radio group with label "Agent Env File" and an appropriate icon (e.g., `FileText` from lucide-react)
2. When `viewType === "agent_env_file"`, show two additional fields:
   - **Environment selector**: dropdown populated from `EnvironmentsService.readEnvironments({ agentId: selectedAgent.id })` — only shown after agent is selected; label "Environment"
   - **File path input**: text input, label "File Path", placeholder "files/report.csv"
3. These fields must be populated before the "Add" button is enabled
4. On submit with `view_type === "agent_env_file"`: include `config: { env_id: selectedEnvId, file_path: filePath }` in the `UserDashboardBlockCreate` payload

**State additions:**
```typescript
const [selectedEnvId, setSelectedEnvId] = useState<string>("")
const [filePath, setFilePath] = useState<string>("")
```

**Query for environments** (conditional on agent selection + view type):
```typescript
const { data: environmentsData } = useQuery({
  queryKey: ["agentEnvironments", selectedAgentId],
  queryFn: () => EnvironmentsService.readEnvironments({ agentId: selectedAgentId }),
  enabled: !!selectedAgentId && viewType === "agent_env_file",
})
```

**Block create payload:**
```typescript
const blockPayload: UserDashboardBlockCreate = {
  agent_id: selectedAgentId,
  view_type: viewType,
  ...(viewType === "agent_env_file" && {
    config: { env_id: selectedEnvId, file_path: filePath }
  }),
}
```

### 4. EditBlockDialog.tsx Changes

**File:** `frontend/src/components/Dashboard/UserDashboards/EditBlockDialog.tsx`

When `block.view_type === "agent_env_file"` or when the user switches to that type:
1. Show the same Environment selector and File Path input (same logic as AddBlockDialog)
2. Pre-populate from `block.config?.env_id` and `block.config?.file_path`
3. Include config in the `UserDashboardBlockUpdate` payload when saving:
   ```typescript
   config: viewType === "agent_env_file"
     ? { env_id: selectedEnvId, file_path: filePath }
     : block.config
   ```

### 5. Client Regeneration

After backend changes, regenerate the frontend client:
```bash
source ./backend/.venv/bin/activate && make gen-client
```

This will:
- Add `getBlockEnvFile` method to `DashboardsService` in `sdk.gen.ts`
- Add "agent_env_file" to the view_type Literal in `types.gen.ts`
- Update `schemas.gen.ts` with the new view_type option

---

## Error Handling & Edge Cases

| Condition | Backend Response | Frontend Display |
|-----------|-----------------|------------------|
| `block.config` is null | 400 "Block config missing env_id" | "No file configured" placeholder |
| `env_id` not found | 404 "Environment not found" | Error state in component |
| env.agent_id != block.agent_id | 400 "Environment does not belong..." | Error state |
| Agent not owned by user | 403 "Not enough permissions" | Error state |
| Path traversal attempt | 400 "Invalid file path" | Error state |
| File not found (local) | 404 "File not found" | Error state |
| Fallback + env not running | 400 "Environment must be running..." | Error state with hint |
| Empty filePath prop | — | "No file configured" placeholder |
| Large file (>10MB) | Streams normally | Rendered; perf may be poor |

---

## UI/UX Considerations

- **Block header icon**: Use `FileText` icon from lucide-react for the `agent_env_file` view type in block headers
- **Loading state**: Spinner + "Loading file..." text (matches other view patterns)
- **Error state**: `AlertCircle` icon + short error text — same style as other error states
- **Auto-refresh**: 30-second interval keeps dashboards fresh for agents writing output files
- **File path display**: Consider showing the file path in small text below the view type in block header when `show_header` is true (optional enhancement)
- **Unsupported extensions**: Fall back to `TextViewer` (display raw content) rather than blocking — many agent output files are text-like
- **Environment selector in AddBlockDialog**: Only show environments for the selected agent. Display environment name if available, otherwise show truncated ID. Environments in any status can be selected (file access doesn't need them running)

---

## Integration Points

- **UserDashboardService._get_block()**: Already exists as a private class method; used by route handler
- **EnvironmentService.get_lifecycle_manager()**: Already used in `workspace.py`; same pattern applies
- **LocalFilesAccessInterface**: New in `base.py`; DockerEnvironmentAdapter is the only current implementer
- **Frontend client**: Must be regenerated after backend changes; the `DashboardsService` will gain the `getBlockEnvFile` method
- **React Query invalidation**: No invalidation needed for env-file reads (it's a read-only streaming endpoint); the `refetchInterval: 30000` handles freshness
- **AddBlockDialog / EditBlockDialog**: Must import `EnvironmentsService` if not already imported

---

## Future Enhancements (Out of Scope)

- **File picker UI**: Instead of a text input for file path, show a tree picker populated from workspace tree (requires env to be running for initial setup, or local file tree reading)
- **Multiple files**: Support array of file_paths to cycle through or display as tabs
- **Auto-detect file type**: Use MIME type detection instead of extension
- **Write support**: Allow updating a file from the dashboard block
- **Cloud adapter support**: Cloud adapters that sync to local cache would implement `LocalFilesAccessInterface` automatically
- **File size limit**: Warn or truncate very large files before streaming
- **Database file support**: Similar to remote database viewer, support SQLite files in the block

---

## Summary Checklist

### Backend Tasks

- [ ] Add `LocalFilesAccessInterface` ABC to `backend/app/services/environments/adapters/base.py`
  - Place it before `EnvironmentAdapter` class definition
  - Add `get_local_workspace_file_path(self, relative_path: str) -> Path | None` abstract method
  - Add docstring explaining the optional capability pattern

- [ ] Update `DockerEnvironmentAdapter` in `backend/app/services/environments/adapters/docker_adapter.py`
  - Change class signature to `class DockerEnvironmentAdapter(EnvironmentAdapter, LocalFilesAccessInterface)`
  - Add import for `LocalFilesAccessInterface` from `.base`
  - Implement `get_local_workspace_file_path()` method with path traversal prevention
  - Place in the `# === File Operations ===` section

- [ ] Update `backend/app/models/users/user_dashboard.py`
  - Add `"agent_env_file"` to Literal in `UserDashboardBlockBase.view_type`
  - Add `"agent_env_file"` to Literal in `UserDashboardBlockUpdate.view_type`

- [ ] Update `backend/app/services/users/user_dashboard_service.py`
  - Add `"agent_env_file"` to `VALID_VIEW_TYPES` (or equivalent validation set)
  - Add config validation in `add_block()`: require `env_id` and `file_path` when `view_type == "agent_env_file"`

- [ ] Add `get_block_env_file` endpoint to `backend/app/api/routes/user_dashboards.py`
  - Route: `GET /{dashboard_id}/blocks/{block_id}/env-file`
  - Query param: `path: str`
  - Add imports: `StreamingResponse`, `AgentEnvironment`, `Agent`, `EnvironmentService`, `LocalFilesAccessInterface`
  - Implement ownership chain verification
  - Implement local file access path (via `LocalFilesAccessInterface`)
  - Implement fallback path (requires env running)

- [ ] Regenerate frontend client: `source ./backend/.venv/bin/activate && make gen-client`

### Frontend Tasks

- [ ] Create `frontend/src/components/Dashboard/UserDashboards/views/AgentEnvFileView.tsx`
  - Props: `dashboardId`, `blockId`, `filePath`
  - Fetch via `DashboardsService.getBlockEnvFile()`
  - Render with correct viewer based on file extension
  - Loading, error, empty states
  - `refetchInterval: 30000`

- [ ] Update `frontend/src/components/Dashboard/UserDashboards/DashboardBlock.tsx`
  - Import `AgentEnvFileView`
  - Add `"agent_env_file"` case to `renderView()`
  - Pass `dashboardId`, `blockId`, `block.config?.file_path`

- [ ] Update `frontend/src/components/Dashboard/UserDashboards/AddBlockDialog.tsx`
  - Add "Agent Env File" to view type radio options (with `FileText` icon)
  - Add environment selector (conditional on `viewType === "agent_env_file"` and agent selected)
  - Add file path text input
  - Include `config: { env_id, file_path }` in block create payload
  - Add `useQuery` for agent environments

- [ ] Update `frontend/src/components/Dashboard/UserDashboards/EditBlockDialog.tsx`
  - Add environment selector and file path fields for `agent_env_file` blocks
  - Pre-populate from `block.config`
  - Include config in update payload

- [ ] Verify TypeScript types compile:
  ```bash
  cd frontend && npx tsc --noEmit 2>&1 | grep -E "(AgentEnvFileView|AddBlockDialog|EditBlockDialog|DashboardBlock)" | head -20
  ```

### Testing Tasks

- [ ] Add tests to `backend/tests/api/dashboards/test_dashboards.py`:
  - Create block with `view_type="agent_env_file"` and valid config → 200
  - Create block with `view_type="agent_env_file"` missing `env_id` → 422
  - Create block with `view_type="agent_env_file"` missing `file_path` → 422
  - `GET .../env-file?path=...` with valid block (mock local file) → 200 with content
  - `GET .../env-file?path=...` with path traversal `../secret` → 400
  - `GET .../env-file?path=...` from different user → 403
  - `GET .../env-file?path=...` with wrong block type → 400
  - `GET .../env-file?path=...` with env not belonging to block's agent → 400

- [ ] Add test utility helpers in `backend/tests/utils/dashboard.py`:
  - `create_agent_env_file_block(client, dashboard_id, agent_id, env_id, file_path)` helper

- [ ] Run full test suite: `make test-backend`

### Documentation Tasks

- [ ] Update `docs/application/user_dashboards/user_dashboards.md`
  - Add `agent_env_file` to Block View Types table
  - Add user flow: Adding an Agent Env File block
  - Add error states for new type

- [ ] Update `docs/application/user_dashboards/user_dashboards_tech.md`
  - Document new Pydantic Literal update
  - Document `config` schema for `agent_env_file`
  - Document new API endpoint `GET .../env-file`
  - Document `AgentEnvFileView` component
  - Add React Query key `["dashboardBlockEnvFile", ...]`

- [ ] Update `docs/agents/agent_environments/agent_environments_tech.md`
  - Document `LocalFilesAccessInterface` in the adapters section
  - Note which adapters implement it and the encapsulation principle
