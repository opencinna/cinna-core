import asyncio
import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute

from core.server.routes import router

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# Workspace files monitored for live-sync resync (relative to workspace root).
# When any of these stabilise after a change (e.g., a Mutagen sync completes),
# env-core fires a single workspace-files-changed callback; the backend emits
# WORKSPACE_FILES_CHANGED and downstream handlers refresh:
#   - prompt files                       → agent.workflow_prompt / entrypoint_prompt / refiner_prompt
#   - docs/CLI_COMMANDS.yaml             → CLICommandsService cached list
#   - app-data/storage/STATUS.md         → AgentStatusService cached snapshot
_WATCHED_FILES = [
    "docs/WORKFLOW_PROMPT.md",
    "docs/ENTRYPOINT_PROMPT.md",
    "docs/REFINER_PROMPT.md",
    "docs/CLI_COMMANDS.yaml",
    "app-data/storage/STATUS.md",
]
# How often to poll for mtime changes (seconds)
_POLL_INTERVAL = 5
# How many consecutive stable polls before firing the callback
_STABLE_POLLS = 1  # 1 × 5s = 5s stable window


async def _workspace_files_watcher() -> None:
    """
    Poll watched workspace file mtimes every _POLL_INTERVAL seconds.

    When any of the files changes and remains stable for _STABLE_POLLS
    polling cycles, POST /api/v1/environments/{env_id}/workspace-files-changed
    to the backend with the list of changed paths; the backend emits a
    WORKSPACE_FILES_CHANGED event whose handlers refresh prompts, CLI
    commands cache, and agent status.

    Uses the same BACKEND_URL + AGENT_AUTH_TOKEN + ENV_ID env vars that the
    security event proxy uses.
    """
    try:
        import httpx as _httpx
    except ImportError:
        logger.warning("httpx not available — workspace file watcher disabled")
        return

    workspace_root = Path(os.getenv("WORKSPACE_ROOT", "/app/workspace"))
    backend_url = os.getenv("BACKEND_URL", "http://host.docker.internal:8000")
    auth_token = os.getenv("AGENT_AUTH_TOKEN", "")
    env_id = os.getenv("ENV_ID", "")

    if not auth_token or not env_id:
        logger.warning("AGENT_AUTH_TOKEN or ENV_ID not set — workspace file watcher disabled")
        return

    callback_url = f"{backend_url}/api/v1/environments/{env_id}/workspace-files-changed"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "X-Agent-Env-Id": env_id,
    }

    # snapshot: path → mtime at last known state
    prev_mtimes: dict[str, float] = {}
    # path → mtime at last change (for debounce)
    pending_change: dict[str, float] = {}
    # path → how many stable polls since change was detected
    stable_count: dict[str, int] = {}

    def _read_mtimes() -> dict[str, float]:
        result = {}
        for rel in _WATCHED_FILES:
            p = workspace_root / rel
            try:
                result[rel] = p.stat().st_mtime
            except FileNotFoundError:
                pass
        return result

    # Seed initial mtimes so we don't fire on startup
    prev_mtimes = _read_mtimes()

    logger.info("Workspace file watcher started")

    while True:
        await asyncio.sleep(_POLL_INTERVAL)

        current_mtimes = _read_mtimes()
        changed_files: list[str] = []

        for rel in _WATCHED_FILES:
            cur = current_mtimes.get(rel)
            prev = prev_mtimes.get(rel)

            if cur is None:
                # File deleted — reset tracking
                pending_change.pop(rel, None)
                stable_count.pop(rel, None)
                continue

            if prev is None or cur != prev:
                # Change detected (new file or mtime updated)
                if pending_change.get(rel) != cur:
                    # Fresh change — reset stability counter
                    pending_change[rel] = cur
                    stable_count[rel] = 0
                    logger.debug(f"Watched file changed: {rel}")
                else:
                    # Same mtime as last poll — increment stability counter
                    stable_count[rel] = stable_count.get(rel, 0) + 1
                    if stable_count[rel] >= _STABLE_POLLS:
                        changed_files.append(rel)
                        # Absorb into prev_mtimes so we don't re-fire
                        prev_mtimes[rel] = cur
                        pending_change.pop(rel, None)
                        stable_count.pop(rel, None)
                        logger.info(f"Watched file stable after change: {rel} — scheduling resync")
            else:
                # No change for this file
                pending_change.pop(rel, None)
                stable_count.pop(rel, None)

        if changed_files:
            try:
                async with _httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        callback_url,
                        headers=headers,
                        json={"changed_files": changed_files},
                    )
                    if resp.status_code == 200:
                        logger.info(
                            f"Workspace-files-changed callback succeeded ({len(changed_files)} files)"
                        )
                    else:
                        logger.warning(
                            f"Workspace-files-changed callback returned {resp.status_code}: "
                            f"{resp.text[:200]}"
                        )
            except Exception as exc:
                logger.warning(f"Workspace-files-changed callback failed (non-fatal): {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background tasks on startup; cancel them on shutdown."""
    watcher_task = asyncio.create_task(_workspace_files_watcher())
    try:
        yield
    finally:
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


app = FastAPI(
    title="Agent Environment Server",
    version="1.0.0",
    generate_unique_id_function=custom_generate_unique_id,
    lifespan=lifespan,
)

# Include API routes
app.include_router(router)
