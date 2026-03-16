# Agent Workspace

This directory is the workspace where your agent operates.

## Build Mode

When you start a session in "building" mode, Claude Code SDK has access to this workspace with the following tools:

- **Read**: Read files and explore code
- **Edit**: Modify existing files
- **Write**: Create new files
- **Glob**: Find files by pattern
- **Grep**: Search code content
- **Bash**: Execute commands

Claude will help you:
- Write scripts and utilities
- Set up configurations
- Create project structures
- Debug and fix code

## Conversation Mode

In "conversation" mode, the agent uses pre-built tools and scripts from this workspace to help with tasks.

## Directory Structure

- `/credentials/` - Service credentials (read-only)
- `/databases/` - SQLite or other local databases
- `/files/` - User-uploaded files
- `/logs/` - Agent logs
- `/scripts/` - Custom scripts
- `/server/` - Optional: Custom API endpoints
- `/docs/` - Documentation and specifications
- `/knowledge/` - Integration-specific knowledge base (API docs, schemas)

## Python Packages

### Template Dependencies

The environment comes with pre-installed Python packages defined in the template's `pyproject.toml`:
- `fastapi` - Web framework
- `uvicorn` - ASGI server
- `pydantic` - Data validation
- `httpx` - Async HTTP client
- `requests` - HTTP library
- `claude-agent-sdk` - Claude Agent SDK
- `python-dotenv` - Environment variables

These are baked into the Docker image and updated when the environment is rebuilt.

### Custom Dependencies

For integration-specific packages (e.g., `odoo-rpc-client`, `salesforce-api`, `stripe`), use the **workspace requirements file**:

**File**: `/workspace/workspace_requirements.txt`

**How it works**:
1. Add package names to `workspace_requirements.txt` (one per line)
2. Packages are automatically installed when the container starts
3. The file persists across environment rebuilds
4. Template dependencies and custom dependencies are kept separate

**Example workflow**:
```python
# Install a package immediately
import subprocess
subprocess.run(["uv", "pip", "install", "odoo-rpc-client"], check=True)

# Add to workspace_requirements.txt for persistence
with open("/app/workspace/workspace_requirements.txt", "a") as f:
    f.write("odoo-rpc-client>=0.8.0\n")
```

**Why this separation?**:
- Template dependencies are system-level (updated via rebuild)
- Custom dependencies are workflow-specific (persist across rebuilds)
- Environment rebuilds can update template packages without losing your custom packages
