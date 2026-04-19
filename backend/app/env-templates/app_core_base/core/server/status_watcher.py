"""
STATUS.md mtime watcher — notifies the backend when the agent updates its status.

Polls /app/workspace/STATUS.md every STATUS_POLL_INTERVAL seconds. When the
file's mtime changes (or the file is created/deleted), waits STATUS_DEBOUNCE_SECONDS
then POSTs to the backend's status-updated push endpoint.

Environment variables (same as other tools in this template):
  BACKEND_URL        — e.g. http://backend:8000 (default)
  AGENT_AUTH_TOKEN   — Bearer token for backend auth
  ENV_ID             — this environment's UUID

The watcher runs as a daemon thread so it never blocks FastAPI startup or shutdown.
If BACKEND_URL, AGENT_AUTH_TOKEN, or ENV_ID are missing the watcher logs a warning
and exits silently — it is purely best-effort.
"""
import logging
import os
import threading
import time

import httpx

logger = logging.getLogger(__name__)

STATUS_FILE = "/app/workspace/STATUS.md"
STATUS_POLL_INTERVAL = 5      # seconds between mtime checks
STATUS_DEBOUNCE_SECONDS = 2   # wait after detecting change before notifying


def _notify_backend(backend_url: str, env_id: str, token: str) -> None:
    """POST to the backend status-updated endpoint (best-effort, never raises)."""
    url = f"{backend_url}/api/v1/internal/environments/{env_id}/status-updated"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.post(url, headers=headers, timeout=10.0)
        if resp.status_code == 429:
            logger.debug("status_watcher: rate limited by backend (env=%s)", env_id)
        elif resp.status_code not in (200, 204):
            logger.warning(
                "status_watcher: unexpected response %s from backend (env=%s)",
                resp.status_code, env_id,
            )
        else:
            logger.debug("status_watcher: notified backend (env=%s)", env_id)
    except Exception as exc:
        logger.debug("status_watcher: failed to notify backend: %s", exc)


def _watch_loop(backend_url: str, env_id: str, token: str) -> None:
    """Main polling loop. Runs forever in a daemon thread."""
    last_mtime: float | None = None
    last_exists: bool = False

    while True:
        try:
            exists = os.path.exists(STATUS_FILE)
            mtime = os.stat(STATUS_FILE).st_mtime if exists else None

            changed = (exists != last_exists) or (mtime != last_mtime)

            if changed:
                last_exists = exists
                last_mtime = mtime

                # Debounce: wait a moment for the write to settle
                time.sleep(STATUS_DEBOUNCE_SECONDS)

                # Notify backend (fire-and-forget)
                _notify_backend(backend_url, env_id, token)

        except Exception as exc:
            logger.debug("status_watcher: poll error: %s", exc)

        time.sleep(STATUS_POLL_INTERVAL)


def start_status_watcher() -> None:
    """
    Start the STATUS.md mtime watcher in a background daemon thread.

    Safe to call at FastAPI lifespan startup. If required env vars are absent
    the function logs a warning and returns without starting the thread.
    """
    backend_url = os.getenv("BACKEND_URL", "http://backend:8000")
    env_id = os.getenv("ENV_ID")
    token = os.getenv("AGENT_AUTH_TOKEN")

    if not env_id:
        logger.warning("status_watcher: ENV_ID not set — watcher disabled")
        return
    if not token:
        logger.warning("status_watcher: AGENT_AUTH_TOKEN not set — watcher disabled")
        return

    thread = threading.Thread(
        target=_watch_loop,
        args=(backend_url, env_id, token),
        daemon=True,
        name="status-watcher",
    )
    thread.start()
    logger.info("status_watcher: started (env=%s, poll=%ds)", env_id, STATUS_POLL_INTERVAL)
