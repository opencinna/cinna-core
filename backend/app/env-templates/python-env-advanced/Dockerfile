FROM python:3.10

LABEL maintainer="workflow-runner"
LABEL env.name="python-env-basic"
LABEL env.version="1.0.0"

ENV PYTHONUNBUFFERED=1

WORKDIR /app/

# Install uv
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#installing-uv
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

# Place executables in the environment at the front of the path
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#using-the-environment
ENV PATH="/app/.venv/bin:$PATH"

# Compile bytecode
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#compiling-bytecode
ENV UV_COMPILE_BYTECODE=1

# uv Cache
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#caching
ENV UV_LINK_MODE=copy

# Copy dependency files
COPY ./pyproject.toml ./uv.lock /app/

# Install dependencies only (not the project itself, since app/ will be mounted)
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#intermediate-layers
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

ENV PYTHONPATH=/app

# Note: app/ directory will be mounted as a volume at runtime
# This allows for live code updates without rebuilding the image

# Default port (will be overridden by AGENT_PORT environment variable at runtime)
ENV AGENT_PORT=8000

# No EXPOSE directive - ports are managed dynamically via AGENT_PORT env var
# Communication happens via agent-bridge Docker network, not published ports

# Note: HEALTHCHECK is defined in docker-compose.yml to use runtime AGENT_PORT value

# Start FastAPI server on the port specified by AGENT_PORT
# Using shell form to allow environment variable expansion
CMD fastapi run --host 0.0.0.0 --port ${AGENT_PORT} app/main.py
