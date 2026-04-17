FROM python:3.11-bookworm

LABEL maintainer="cinna"
LABEL env.name="general-env"
LABEL env.version="1.0.0"

ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    sqlite3 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20.x (required for Claude Code)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install uv (Python package manager)
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install OpenCode binary (multi-provider AI agent)
RUN curl -fsSL https://opencode.ai/install | bash

# Set working directory
WORKDIR /app

# Place executables in the environment at the front of the path
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#using-the-environment
ENV PATH="/app/.venv/bin:/root/.opencode/bin:$PATH"

# Compile bytecode
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#compiling-bytecode
ENV UV_COMPILE_BYTECODE=1

# uv Cache
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#caching
ENV UV_LINK_MODE=copy

# Copy project files
COPY pyproject.toml uv.lock* ./

# Install Python dependencies (including claude-agent-sdk)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

ENV PYTHONPATH=/app

# Note: app/workspace/ and app/core/ directories are mounted as volumes at runtime.
# app/core is bind-mounted read-only from the per-env instance directory — it is NOT
# baked into the image. This allows all environments sharing the same template to use
# a single shared image while receiving individual core updates via bind-mount.

# Default port (will be overridden by AGENT_PORT environment variable at runtime)
ENV AGENT_PORT=8000

# No EXPOSE directive - ports are managed dynamically via AGENT_PORT env var
# Communication happens via agent-bridge Docker network, not published ports

# Note: HEALTHCHECK is defined in docker-compose.yml to use runtime AGENT_PORT value

# Start FastAPI server on the port specified by AGENT_PORT
# Using shell form to allow environment variable expansion
CMD fastapi run --host 0.0.0.0 --port ${AGENT_PORT} core/main.py
