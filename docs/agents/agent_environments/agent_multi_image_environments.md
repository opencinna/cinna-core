# Multi-Image Agent Environments

## Purpose

Allow agents to run in different Docker base images depending on their workload. Some agents only need Python; others need system-level tools like ffmpeg, imagemagick, or chromium. The template is selected at environment creation time and determines the base OS, available tooling, and image size.

## Available Templates

### `python-env-advanced` (Python)

- **Base image**: `python:3.11-slim` (Debian slim)
- **Image size**: ~200MB built
- **Pre-installed**: curl, git, sqlite3, build-essential, Node.js 20.x, uv, Claude Code CLI
- **Best for**: Pure Python agents — API integrations, data processing, text generation, workflow automation
- **Default**: Yes — selected by default when creating environments

### `general-env` (General Purpose)

- **Base image**: `python:3.11-bookworm` (full Debian)
- **Image size**: ~600MB built
- **Pre-installed**: Same as Python template, plus the full Debian toolchain and package ecosystem
- **Best for**: Agents that need to install system-level tools at runtime (ffmpeg, imagemagick, chromium, pandoc, latex, etc.)
- **Key advantage**: `apt-get install` has access to the full Debian package repository without needing to add package sources

## When to Use Which

| Scenario | Template | Why |
|----------|----------|-----|
| API integrations (REST, GraphQL, webhooks) | Python | No system tools needed, smaller image |
| Data processing (CSV, JSON, databases) | Python | Python libraries handle everything |
| Text/document generation | Python | Python libraries sufficient |
| Audio/video processing | General Purpose | Needs ffmpeg, mediainfo, etc. |
| Image manipulation beyond Python PIL | General Purpose | Needs imagemagick, graphicsmagick |
| PDF generation with LaTeX | General Purpose | Needs texlive packages |
| Web scraping with headless browser | General Purpose | Needs chromium |
| Agents that install system packages at runtime | General Purpose | Full apt-get ecosystem available |
| Unsure / general use | Python | Start lightweight, switch if needed |

## System Package Persistence

Both templates support `workspace_system_packages.txt` — a file in the workspace where agents can list OS packages to install. This file:

- Lives at `workspace/workspace_system_packages.txt`
- Contains one package name per line (lines starting with `#` are comments)
- Is read and installed via `apt-get install -y` on every new container startup (after rebuild or first creation)
- Is **not** re-installed when restarting/activating an existing container (packages already present)
- Is preserved across rebuilds (lives in workspace volume)
- Is included in environment cloning and workspace sync operations

**Example** `workspace_system_packages.txt`:
```
# Media processing
ffmpeg
imagemagick

# Document conversion
pandoc
```

This mirrors the existing `workspace_requirements.txt` pattern for Python packages. The agent writes package names to the file, and they are automatically reinstalled whenever a fresh container is created.

## Template Architecture

Both templates share identical structure:

```
backend/app/env-templates/<template-name>/
├── Dockerfile                    # Only difference: FROM image
├── docker-compose.template.yml   # Only difference: image name
├── pyproject.toml                # Same Python dependencies
├── uv.lock                       # Same lock file
└── app/
    ├── core/                     # Same server code
    │   ├── server/               # FastAPI server, SDK adapters
    │   └── scripts/              # Helper scripts
    └── workspace/                # Same workspace template
        ├── scripts/
        ├── files/
        ├── docs/
        ├── credentials/
        ├── databases/
        ├── knowledge/
        ├── logs/
        └── workspace_requirements.txt
```

The only differences between templates are:
1. **Dockerfile `FROM` line**: `python:3.11-slim` vs `python:3.11-bookworm`
2. **Docker image name**: `agent-python-env-advanced` vs `agent-general-env`

All core server code (`app/core/`), Python dependencies, workspace structure, and docker-compose configuration are identical.

## Template Selection

- Selected via the `env_name` field on the `AgentEnvironment` model (values: `"python-env-advanced"` or `"general-env"`)
- Set at environment creation time through the "Environment Template" dropdown in the Add Environment dialog
- Cannot be changed after creation — to switch templates, create a new environment (workspace data can be synced between environments)
- Template determines which directory under `backend/app/env-templates/` is used as the source for the environment instance

## Integration Points

- **[Agent Environments](./agent_environments.md)** — Parent feature: lifecycle, two-layer architecture, data preservation
- **[Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md)** — Cloning and syncing include `workspace_system_packages.txt`
- **[Agent Sharing](../agent_sharing/agent_sharing.md)** — Cloned agents include system packages file
