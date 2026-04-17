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

Core server code is maintained in a single shared location and overlaid onto each template during environment creation and rebuild:

```
backend/app/env-templates/
├── app_core_base/                # Shared across ALL templates
│   └── core/                     # FastAPI server, SDK adapters, prompts, tools
│       ├── server/               # HTTP API, SDK manager, adapters
│       ├── prompts/              # BUILDING_AGENT.md, WEBAPP_BUILDING.md
│       └── scripts/              # Helper scripts
│
├── python-env-advanced/          # Template-specific files only
│   ├── Dockerfile                # FROM python:3.11-slim; no COPY app/core (ro bind-mount)
│   ├── docker-compose.template.yml  # image: ${TEMPLATE_IMAGE_TAG}, no build: block
│   ├── pyproject.toml
│   ├── uv.lock
│   └── app/
│       └── workspace/            # Workspace template
│
└── general-env/                  # Template-specific files only
    ├── Dockerfile                # FROM python:3.11-bookworm; no COPY app/core (ro bind-mount)
    ├── docker-compose.template.yml  # image: ${TEMPLATE_IMAGE_TAG}, no build: block
    ├── pyproject.toml
    ├── uv.lock
    └── app/
        └── workspace/            # Workspace template
```

Each template produces **one shared Docker image** per unique set of build inputs (`Dockerfile` + `pyproject.toml` + `uv.lock`). The image is built and cached by `TemplateImageService` — all environments using the same template share that image as long as the inputs have not changed.

Image tag format: `cinna-agent-<env_name>:<sha256[:12]>` — e.g. `cinna-agent-python-env-advanced:a1b2c3d4e5f6`.

`Dockerfile`, `pyproject.toml`, and `uv.lock` are **not** copied into per-environment instance directories. They live only in the template directory and are consumed exclusively by `TemplateImageService`.

The main differences between templates are:
1. **Dockerfile `FROM` line**: `python:3.11-slim` vs `python:3.11-bookworm`
2. **Python dependencies** (`pyproject.toml`/`uv.lock`): may differ between templates

All core server code (`app/core/`) is shared via `app_core_base` — changes to routes, models, adapters, or prompts apply to all templates automatically. Core is bind-mounted read-only into each container at runtime, not baked into the image.

## Template Selection

- Selected via the `env_name` field on the `AgentEnvironment` model (values: `"python-env-advanced"` or `"general-env"`)
- Set at environment creation time through the "Environment Template" dropdown in the Add Environment dialog
- Cannot be changed after creation — to switch templates, create a new environment (workspace data can be synced between environments)
- Template determines which directory under `backend/app/env-templates/` is used as the source for the environment instance

## Integration Points

- **[Agent Environments](./agent_environments.md)** — Parent feature: lifecycle, two-layer architecture, data preservation
- **[Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md)** — Cloning and syncing include `workspace_system_packages.txt`
- **[Agent Sharing](../agent_sharing/agent_sharing.md)** — Cloned agents include system packages file
