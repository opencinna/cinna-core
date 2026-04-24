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

# Prompt files monitored for live-sync resync (relative to workspace root)
_PROMPT_FILES = [
    "docs/WORKFLOW_PROMPT.md",
    "docs/ENTRYPOINT_PROMPT.md",
    "docs/REFINER_PROMPT.md",
]
# How often to poll for mtime changes (seconds)
_POLL_INTERVAL = 5
# How many consecutive stable polls before firing the callback
_STABLE_POLLS = 1  # 1 × 5s = 5s stable window


async def _prompt_file_watcher() -> None:
    """
    Poll prompt file mtimes every _POLL_INTERVAL seconds.

    When any of the prompt files changes and remains stable for _STABLE_POLLS
    polling cycles, POST /api/v1/environments/{env_id}/prompt-file-changed to
    the backend so it can resync agent prompts from the environment.

    Uses the same BACKEND_URL + AGENT_AUTH_TOKEN + ENV_ID env vars that the
    security event proxy uses.
    """
    try:
        import httpx as _httpx
    except ImportError:
        logger.warning("httpx not available — prompt file watcher disabled")
        return

    workspace_root = Path(os.getenv("WORKSPACE_ROOT", "/app/workspace"))
    backend_url = os.getenv("BACKEND_URL", "http://host.docker.internal:8000")
    auth_token = os.getenv("AGENT_AUTH_TOKEN", "")
    env_id = os.getenv("ENV_ID", "")

    if not auth_token or not env_id:
        logger.warning("AGENT_AUTH_TOKEN or ENV_ID not set — prompt file watcher disabled")
        return

    callback_url = f"{backend_url}/api/v1/environments/{env_id}/prompt-file-changed"
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
        for rel in _PROMPT_FILES:
            p = workspace_root / rel
            try:
                result[rel] = p.stat().st_mtime
            except FileNotFoundError:
                pass
        return result

    # Seed initial mtimes so we don't fire on startup
    prev_mtimes = _read_mtimes()

    logger.info("Prompt file watcher started")

    while True:
        await asyncio.sleep(_POLL_INTERVAL)

        current_mtimes = _read_mtimes()
        fire_callback = False

        for rel in _PROMPT_FILES:
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
                    logger.debug(f"Prompt file changed: {rel}")
                else:
                    # Same mtime as last poll — increment stability counter
                    stable_count[rel] = stable_count.get(rel, 0) + 1
                    if stable_count[rel] >= _STABLE_POLLS:
                        fire_callback = True
                        # Absorb into prev_mtimes so we don't re-fire
                        prev_mtimes[rel] = cur
                        pending_change.pop(rel, None)
                        stable_count.pop(rel, None)
                        logger.info(f"Prompt file stable after change: {rel} — scheduling resync")
            else:
                # No change for this file
                pending_change.pop(rel, None)
                stable_count.pop(rel, None)

        if fire_callback:
            try:
                async with _httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(callback_url, headers=headers)
                    if resp.status_code == 200:
                        logger.info("Prompt resync callback succeeded")
                    else:
                        logger.warning(
                            f"Prompt resync callback returned {resp.status_code}: {resp.text[:200]}"
                        )
            except Exception as exc:
                logger.warning(f"Prompt resync callback failed (non-fatal): {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background tasks on startup; cancel them on shutdown."""
    watcher_task = asyncio.create_task(_prompt_file_watcher())
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
