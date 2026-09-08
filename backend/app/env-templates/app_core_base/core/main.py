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
#   - skills/                            → AgentSkillsService cached index
#
# An entry ending in "/" is a DIRECTORY: it is watched by its skill-manifest
# tree hash (path + size + mtime of every file beneath it) instead of one
# file's mtime, because a skill is a folder and an edit anywhere inside it —
# including adding or deleting a file — has to count as a change.
_WATCHED_FILES = [
    "docs/WORKFLOW_PROMPT.md",
    "docs/ENTRYPOINT_PROMPT.md",
    "docs/REFINER_PROMPT.md",
    "docs/CLI_COMMANDS.yaml",
    "app-data/storage/STATUS.md",
    "skills/",
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

    # The same hash the projection and the backend cache compare on, so a
    # directory the watcher calls "changed" is exactly a directory the index
    # would report differently.
    from core.server.skill_manifest import tree_hash as _tree_hash

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

    # snapshot: path → change token at last known state (mtime for a file,
    # tree hash for a directory entry — both are just "did this move?" values)
    prev_mtimes: dict[str, float | str] = {}
    # path → change token at last change (for debounce)
    pending_change: dict[str, float | str] = {}
    # path → how many stable polls since change was detected
    stable_count: dict[str, int] = {}

    def _read_mtimes() -> dict[str, float | str]:
        result: dict[str, float | str] = {}
        for rel in _WATCHED_FILES:
            p = workspace_root / rel
            if rel.endswith("/"):
                # Directory entry: hash the tree. Unlike a missing FILE, a
                # missing directory is still recorded — `tree_hash` of an
                # absent root is the empty digest, which is also what an empty
                # directory hashes to. Recording it is what makes DELETING the
                # whole folder a change the backend hears about; omitting it
                # (the file rule) would leave the cache advertising skills that
                # no longer exist. Creating an empty folder still fires
                # nothing, because nothing about the index moved.
                try:
                    result[rel] = _tree_hash(p)
                except OSError:
                    continue
                continue
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


async def _agent_api_reload_watcher() -> None:
    """
    Watch ``/app/workspace/agent_api/`` for changes and notify the backend so it
    re-harvests + re-caches the OpenAPI spec.

    This is intentionally SEPARATE from ``_workspace_files_watcher`` (plan §3.4):
    that watcher tracks a fixed file list to refresh prompts / CLI commands. This
    one tracks the whole agent_api directory tree purely to drive the agent_api
    spec re-cache. The serving child (when running) hot-reloads via uvicorn's own
    ``--reload``; this watcher only handles the backend re-cache notification,
    which must work even when no child is running (lazy spawn).
    """
    try:
        from core.cinna_api.supervisor import agent_api_supervisor, AGENT_API_DIR
    except Exception as exc:
        logger.warning("agent_api reload watcher disabled: %s", exc)
        return

    def _dir_signature() -> tuple:
        """Coarse signature of the agent_api tree (paths + mtimes)."""
        try:
            if not AGENT_API_DIR.is_dir():
                return ()
            entries = []
            for p in sorted(AGENT_API_DIR.rglob("*.py")):
                try:
                    entries.append((str(p), p.stat().st_mtime))
                except FileNotFoundError:
                    continue
            return tuple(entries)
        except Exception:
            return ()

    prev = _dir_signature()
    pending = False  # a change is observed but writes may still be in flight
    logger.info("Agent API reload watcher started")

    while True:
        await asyncio.sleep(_POLL_INTERVAL)
        current = _dir_signature()
        if current != prev:
            # Still changing — DEBOUNCE: defer the re-harvest until writes
            # settle. A multi-step local edit synced over Mutagen lands as a
            # burst of intermediate states (sometimes transiently broken); we
            # don't want to harvest a half-written tree. Keep deferring while
            # the signature keeps moving; only notify once it holds steady for a
            # full poll cycle.
            prev = current
            pending = True
            continue
        if pending:
            pending = False
            logger.info("agent_api/ settled — notifying backend to re-cache spec")
            await agent_api_supervisor.notify_backend_reload()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background tasks on startup; cancel them on shutdown."""
    watcher_task = asyncio.create_task(_workspace_files_watcher())
    agent_api_watcher_task = asyncio.create_task(_agent_api_reload_watcher())
    try:
        yield
    finally:
        for task in (watcher_task, agent_api_watcher_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Stop the lazily-spawned agent API child if it is running.
        try:
            from core.cinna_api.supervisor import agent_api_supervisor
            await agent_api_supervisor.shutdown()
        except Exception:
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
