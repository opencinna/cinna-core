FROM python:3.11-slim

LABEL maintainer="workflow-runner"
LABEL env.name="python-env-advanced"
LABEL env.version="1.0.0"

ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
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

# Set working directory
WORKDIR /app

# Place executables in the environment at the front of the path
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#using-the-environment
ENV PATH="/app/.venv/bin:$PATH"

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

# Copy core application files (system files baked into image)
COPY app/core /app/core

ENV PYTHONPATH=/app

# Note: app/workspace/ directory will be mounted as a volume at runtime
# This allows user files to persist across rebuilds
# Core files are baked into the image for easy updates

# Default port (will be overridden by AGENT_PORT environment variable at runtime)
ENV AGENT_PORT=8000

# No EXPOSE directive - ports are managed dynamically via AGENT_PORT env var
# Communication happens via agent-bridge Docker network, not published ports

# Note: HEALTHCHECK is defined in docker-compose.yml to use runtime AGENT_PORT value

# Start FastAPI server on the port specified by AGENT_PORT
# Using shell form to allow environment variable expansion
CMD fastapi run --host 0.0.0.0 --port ${AGENT_PORT} core/main.py
