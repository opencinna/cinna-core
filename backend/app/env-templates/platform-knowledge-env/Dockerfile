FROM python:3.11-slim

LABEL maintainer="cinna"
LABEL env.name="platform-knowledge-env"
LABEL env.version="1.0.0"

ENV PYTHONUNBUFFERED=1

# Install system dependencies.
# `openssh-client` provides `ssh`, `ssh-keyscan`, and `scp` — required by the
# ssh_key credential feature so agents can `git clone git@host:repo` and talk
# to any SSH endpoint using keys materialized into ~/.ssh/.
RUN apt-get update && apt-get install -y \
    curl \
    git \
    openssh-client \
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

# Install mutagen-agent (used by cinna-cli live sync — /sync/exec spawns this binary)
ARG MUTAGEN_VERSION=0.18.1
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
      x86_64)           mutarch=linux_amd64 ;; \
      aarch64|arm64)    mutarch=linux_arm64 ;; \
      *) echo "Unsupported arch for mutagen-agent: $arch" >&2; exit 1 ;; \
    esac; \
    tmpdir="$(mktemp -d)"; \
    curl -fsSL -o "$tmpdir/m.tgz" \
      "https://github.com/mutagen-io/mutagen/releases/download/v${MUTAGEN_VERSION}/mutagen_${mutarch}_v${MUTAGEN_VERSION}.tar.gz"; \
    tar -xzf "$tmpdir/m.tgz" -C "$tmpdir" mutagen-agents.tar.gz; \
    tar -xzf "$tmpdir/mutagen-agents.tar.gz" -C "$tmpdir"; \
    mv "$tmpdir/${mutarch}" /usr/local/bin/mutagen-agent; \
    chmod +x /usr/local/bin/mutagen-agent; \
    sha256sum /usr/local/bin/mutagen-agent | awk '{print $1}' > /etc/mutagen-agent.sha256; \
    echo "$MUTAGEN_VERSION" > /etc/mutagen-agent.version; \
    rm -rf "$tmpdir"; \
    /usr/local/bin/mutagen-agent version

# Set working directory
WORKDIR /app

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:/root/.opencode/bin:$PATH"

# Compile bytecode
ENV UV_COMPILE_BYTECODE=1

# uv Cache
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

# Start FastAPI server on the port specified by AGENT_PORT
CMD fastapi run --host 0.0.0.0 --port ${AGENT_PORT} core/main.py
