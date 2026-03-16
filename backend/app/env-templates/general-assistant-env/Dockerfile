FROM python:3.11-slim

LABEL maintainer="workflow-runner"
LABEL env.name="general-assistant-env"
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

# Set working directory
WORKDIR /app

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# Compile bytecode
ENV UV_COMPILE_BYTECODE=1

# uv Cache
ENV UV_LINK_MODE=copy

# Copy project files
COPY pyproject.toml uv.lock* ./

# Install Python dependencies (including claude-agent-sdk)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Copy core application files (system files baked into image)
COPY app/core /app/core

ENV PYTHONPATH=/app

# Default port (will be overridden by AGENT_PORT environment variable at runtime)
ENV AGENT_PORT=8000

# Start FastAPI server on the port specified by AGENT_PORT
CMD fastapi run --host 0.0.0.0 --port ${AGENT_PORT} core/main.py
