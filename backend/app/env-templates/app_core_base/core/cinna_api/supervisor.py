"""
Agent REST API supervisor (env-core, in-container).

Owns the lifecycle of the uvicorn child that serves the agent's REST API:

- **Lazy spawn-on-first-call:** the child is NOT started on env start. It is
  spawned only when a proxied request first reaches env-core, so a chat-only
  session pays zero API overhead (no child, no bound port).
- **Idle reap:** the child is stopped after ~5 minutes without API traffic — a
  within-container analogue of env suspension.
- **Health checks + restart:** mirrors the ``opencode serve`` supervision in
  ``opencode_sdk_adapter.py``.
- **Boot-error capture:** the child's stderr tail + the last import traceback
  are kept so ``_status`` can surface boot failures.
- **Import-only spec harvest:** the OpenAPI spec is harvested by running
  ``python -m core.cinna_api.harvest`` in a short-lived subprocess — it does
  NOT require the serving child to be up.

The child binds ``127.0.0.1:<internal_port>`` (localhost-only); it is reachable
only through env-core's proxy route.
"""
import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Internal port the child binds to (localhost-only). Distinct from env-core's
# own port and the OpenCode serve ports (4096/4097).
INTERNAL_PORT = int(os.getenv("AGENT_API_INTERNAL_PORT", "9100"))
INTERNAL_HOST = "127.0.0.1"
BASE_URL = f"http://{INTERNAL_HOST}:{INTERNAL_PORT}"

# How long to wait for the child to become healthy after spawning (seconds).
STARTUP_TIMEOUT = 20
# Stop the child after this much idle time without an API request (seconds).
IDLE_REAP_SECONDS = 300  # ~5 min
# How often the reaper checks for idleness (seconds).
REAP_CHECK_INTERVAL = 30
# Spec harvest subprocess timeout (seconds).
HARVEST_TIMEOUT = 30

AGENT_API_DIR = Path(os.getenv("AGENT_API_DIR", "/app/workspace/agent_api"))
SDK_PARENT_DIR = os.getenv("CINNA_API_SDK_PARENT", "/app/core")

# Optional isolated venv for agent-supplied dependencies (plan §3.4, §9).
# ZERO-INSTALL is the fast path: if there is no agent_api/requirements.txt, no
# venv is created and the child runs on the base image's interpreter. When a
# requirements.txt IS present, its deps are installed into a SEPARATE venv
# (agent_api/.venv) via ``uv`` so they cannot clobber env-core's own runtime.
REQUIREMENTS_FILE = AGENT_API_DIR / "requirements.txt"
AGENT_API_VENV = AGENT_API_DIR / ".venv"
# Bound the install so a slow/huge requirements file can't hang a consumer's
# first call indefinitely.
VENV_INSTALL_TIMEOUT = 180  # seconds
# Marker storing the hash of the requirements.txt the venv was built from, so we
# only reinstall when the file actually changes.
_VENV_REQ_HASH_FILE = AGENT_API_VENV / ".cinna_req_sha256"

# Backend re-cache notification (after a reload / boot-error change).
_BACKEND_URL = os.getenv("BACKEND_URL", "http://host.docker.internal:8000")
_AUTH_TOKEN = os.getenv("AGENT_AUTH_TOKEN", "")
_ENV_ID = os.getenv("ENV_ID", "")


def _import_httpx():
    try:
        import httpx
        return httpx
    except ImportError:
        return None


class AgentApiSupervisor:
    """Supervises the lazily-spawned uvicorn child serving the agent REST API."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._start_lock = asyncio.Lock()
        self._last_activity: float = 0.0
        self._reaper_task: asyncio.Task | None = None
        self._boot_error: str | None = None
        self._stderr_tail: list[str] = []
        self._stderr_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    @property
    def child_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def boot_error(self) -> str | None:
        return self._boot_error

    def mark_activity(self) -> None:
        """Record an API request time so the idle reaper holds the child open."""
        self._last_activity = time.monotonic()

    async def ensure_running(self) -> bool:
        """
        Ensure the child is up and healthy, spawning it on first call.

        Returns True if the child is healthy and ready to receive proxied
        requests; False if it failed to become healthy within the budget (the
        caller should return 503 + Retry-After).
        """
        async with self._start_lock:
            if self.child_running and await self._is_healthy():
                self.mark_activity()
                return True
            # (Re)spawn.
            await self._spawn()
            ok = await self._wait_for_health()
            if ok:
                self.mark_activity()
                self._ensure_reaper()
            return ok

    async def get_status(self) -> dict:
        """Build/run status dict for the ``/agent-api/_status`` route."""
        from .discovery import has_agent_api

        has_app = has_agent_api()
        healthy = self.child_running and await self._is_healthy() if self.child_running else False
        if self._boot_error:
            state = "error"
        elif healthy:
            state = "running"
        elif has_app:
            state = "stopped"  # built but child not currently spawned (lazy)
        else:
            state = "empty"
        return {
            "state": state,
            "child_running": self.child_running,
            "has_app": has_app,
            "spec_available": has_app and self._boot_error is None,
            "internal_port": INTERNAL_PORT,
            "last_error": self._boot_error,
        }

    async def harvest_spec(self) -> dict:
        """
        Harvest the OpenAPI spec import-only (no serving child).

        Runs ``python -m core.cinna_api.harvest`` in a short-lived subprocess.
        On success returns the spec dict and clears any prior boot error. On
        failure records the boot error and raises RuntimeError with the captured
        traceback.
        """
        env = {
            **os.environ,
            "CINNA_API_SDK_PARENT": SDK_PARENT_DIR,
        }

        # Use the isolated venv's interpreter for the harvest too, so the
        # import-only harvest can import the agent's extra deps. Zero-install
        # falls back to the base interpreter. A venv-install failure here is
        # recorded as the boot error and surfaced to the owner.
        python_cmd = "python"
        try:
            venv_python = await self._ensure_venv()
        except Exception as exc:
            self._boot_error = f"agent_api venv setup failed: {exc}"
            raise RuntimeError(self._boot_error) from exc
        if self._boot_error:
            raise RuntimeError(self._boot_error)
        if venv_python is not None:
            python_cmd = str(venv_python)
            env["VIRTUAL_ENV"] = str(AGENT_API_VENV)
            env["PATH"] = f"{AGENT_API_VENV / 'bin'}:{env.get('PATH', '')}"

        try:
            proc = await asyncio.create_subprocess_exec(
                python_cmd, "-m", "core.cinna_api.harvest",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd="/app",
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=HARVEST_TIMEOUT
            )
        except asyncio.TimeoutError:
            self._boot_error = f"spec harvest timed out after {HARVEST_TIMEOUT}s"
            raise RuntimeError(self._boot_error)
        except Exception as exc:
            self._boot_error = f"spec harvest failed to launch: {exc}"
            raise RuntimeError(self._boot_error) from exc

        raw = stdout.decode("utf-8", errors="replace").strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            tail = stderr.decode("utf-8", errors="replace")[-2000:]
            self._boot_error = f"spec harvest produced no JSON. stderr: {tail}"
            raise RuntimeError(self._boot_error)

        if result.get("ok"):
            self._boot_error = None
            return result["spec"]

        self._boot_error = result.get("error") or "unknown harvest error"
        tb = result.get("traceback", "")
        raise RuntimeError(f"{self._boot_error}\n{tb}")

    async def shutdown(self) -> None:
        """Stop the child and cancel background tasks (called on env-core shutdown)."""
        await self._stop_child()
        if self._reaper_task is not None:
            self._reaper_task.cancel()

    # ------------------------------------------------------------------ #
    # Process management                                                  #
    # ------------------------------------------------------------------ #

    async def _spawn(self) -> None:
        """Launch the uvicorn child bound to localhost on the internal port."""
        await self._stop_child()  # idempotent — clean up a dead/zombie child
        self._boot_error = None
        self._stderr_tail = []

        env = {
            **os.environ,
            "CINNA_API_SDK_PARENT": SDK_PARENT_DIR,
        }

        # Optional isolated venv: install agent_api/requirements.txt into
        # agent_api/.venv (zero-install fast path when absent). On install
        # failure we record the boot error and bail — the child is NOT spawned,
        # so a broken requirements file surfaces as an error state rather than a
        # crashing child.
        uvicorn_cmd = ["uvicorn"]
        try:
            venv_python = await self._ensure_venv()
        except Exception as exc:  # defensive — never let venv setup crash env-core
            self._boot_error = f"agent_api venv setup failed: {exc}"
            logger.error(self._boot_error)
            return
        if self._boot_error:
            # _ensure_venv recorded an install failure.
            return
        if venv_python is not None:
            # Run the child from the venv's interpreter so agent deps are
            # importable. The venv is built with --system-site-packages so
            # fastapi/uvicorn/httpx/pydantic from the base image stay available.
            venv_bin = str(AGENT_API_VENV / "bin")
            env["VIRTUAL_ENV"] = str(AGENT_API_VENV)
            env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
            uvicorn_cmd = [str(AGENT_API_VENV / "bin" / "python"), "-m", "uvicorn"]

        logger.info("agent_api: spawning uvicorn child on %s", BASE_URL)
        self._process = await asyncio.create_subprocess_exec(
            *uvicorn_cmd, "core.cinna_api.serve:app",
            "--host", INTERNAL_HOST,
            "--port", str(INTERNAL_PORT),
            "--reload",
            "--reload-dir", str(AGENT_API_DIR),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd="/app",
            env=env,
        )
        # Drain stderr into a bounded tail so boot errors are visible.
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _ensure_venv(self) -> Path | None:
        """
        Ensure the isolated agent-deps venv exists and is up to date.

        Returns the venv's python path when a venv is in use, or None for the
        zero-install fast path (no requirements.txt). On install failure, records
        ``self._boot_error`` and returns None (caller bails without spawning).

        The venv is built with ``--system-site-packages`` so the base image's
        fastapi/uvicorn/httpx/pydantic remain importable; only the agent's extra
        deps are layered on top, in an isolated site so they cannot clobber
        env-core's own runtime.
        """
        if not REQUIREMENTS_FILE.is_file():
            # Zero-install fast path. If a stale venv exists from a previous
            # requirements.txt that was since deleted, leave it (harmless) but
            # don't use it.
            return None

        req_text = REQUIREMENTS_FILE.read_bytes()
        req_hash = hashlib.sha256(req_text).hexdigest()

        venv_python = AGENT_API_VENV / "bin" / "python"
        # Reuse the venv if it exists and was built from the same requirements.
        if venv_python.is_file() and _VENV_REQ_HASH_FILE.is_file():
            try:
                if _VENV_REQ_HASH_FILE.read_text().strip() == req_hash:
                    return venv_python
            except OSError:
                pass  # fall through and rebuild

        # (Re)create the venv and install deps via uv, time-bounded.
        ok = await self._run_bounded(
            ["uv", "venv", "--system-site-packages", str(AGENT_API_VENV)],
            "create venv",
        )
        if not ok:
            return None
        ok = await self._run_bounded(
            [
                "uv", "pip", "install",
                "--python", str(venv_python),
                "-r", str(REQUIREMENTS_FILE),
            ],
            "install requirements",
        )
        if not ok:
            return None

        try:
            _VENV_REQ_HASH_FILE.write_text(req_hash)
        except OSError as exc:
            logger.warning("agent_api: could not write venv req-hash marker: %s", exc)
        logger.info("agent_api: isolated venv ready (%s)", AGENT_API_VENV)
        return venv_python

    async def _run_bounded(self, cmd: list[str], what: str) -> bool:
        """Run a venv/install command with a hard timeout; record boot error on failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(AGENT_API_DIR),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=VENV_INSTALL_TIMEOUT
            )
        except asyncio.TimeoutError:
            self._boot_error = (
                f"agent_api dependency {what} timed out after {VENV_INSTALL_TIMEOUT}s"
            )
            logger.error(self._boot_error)
            return False
        except FileNotFoundError:
            self._boot_error = "agent_api venv requires 'uv' which is not available"
            logger.error(self._boot_error)
            return False
        except Exception as exc:
            self._boot_error = f"agent_api dependency {what} failed to launch: {exc}"
            logger.error(self._boot_error)
            return False

        if proc.returncode != 0:
            tail = stderr.decode("utf-8", errors="replace")[-2000:]
            self._boot_error = f"agent_api dependency {what} failed:\n{tail}"
            logger.error(self._boot_error)
            return False
        return True

    async def _drain_stderr(self) -> None:
        """Keep a bounded tail of the child's stderr for boot-error capture."""
        proc = self._process
        if proc is None or proc.stderr is None:
            return
        try:
            async for line_bytes in proc.stderr:
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                self._stderr_tail.append(line)
                if len(self._stderr_tail) > 100:
                    self._stderr_tail.pop(0)
                # Heuristic: capture the most recent traceback-ish line.
                if "Error" in line or "Traceback" in line or "Exception" in line:
                    self._boot_error = "\n".join(self._stderr_tail[-20:])
        except Exception:
            pass

    async def _stop_child(self) -> None:
        """SIGTERM then SIGKILL the child. Idempotent."""
        proc = self._process
        self._process = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        except Exception as exc:
            logger.warning("agent_api: error stopping child: %s", exc)

    async def _wait_for_health(self) -> bool:
        """Poll the child's health until ready or STARTUP_TIMEOUT elapses."""
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            # If the child died during boot, surface its error and stop.
            if self._process is not None and self._process.returncode is not None:
                if not self._boot_error:
                    self._boot_error = "\n".join(self._stderr_tail[-20:]) or "child exited during startup"
                logger.error("agent_api: child exited during startup: %s", self._boot_error)
                return False
            if await self._is_healthy():
                return True
            await asyncio.sleep(0.5)
        self._boot_error = self._boot_error or f"child did not become healthy within {STARTUP_TIMEOUT}s"
        return False

    async def _is_healthy(self) -> bool:
        """Return True if the child responds to a request on the internal port."""
        httpx = _import_httpx()
        if httpx is None:
            # Without httpx we can't check; assume alive if process is running.
            return self.child_running
        try:
            async with httpx.AsyncClient() as client:
                # /openapi.json always exists on a FastAPI app.
                resp = await client.get(f"{BASE_URL}/openapi.json", timeout=2.0)
                return resp.status_code < 500
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Idle reaper                                                         #
    # ------------------------------------------------------------------ #

    def _ensure_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        """Stop the child after IDLE_REAP_SECONDS without API traffic."""
        try:
            while True:
                await asyncio.sleep(REAP_CHECK_INTERVAL)
                if not self.child_running:
                    return  # nothing to reap; exit until next spawn re-arms us
                idle = time.monotonic() - self._last_activity
                if idle >= IDLE_REAP_SECONDS:
                    logger.info(
                        "agent_api: reaping idle child after %.0fs idle", idle
                    )
                    await self._stop_child()
                    return
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------ #
    # Backend re-cache notification                                       #
    # ------------------------------------------------------------------ #

    async def notify_backend_reload(self) -> None:
        """
        Tell the backend to re-harvest + re-cache the spec after a reload.

        Best-effort; never raises. Uses the same BACKEND_URL + AGENT_AUTH_TOKEN
        + ENV_ID that the security-event proxy / file watcher use.
        """
        httpx = _import_httpx()
        if httpx is None or not _AUTH_TOKEN or not _ENV_ID:
            return
        url = f"{_BACKEND_URL}/api/v1/environments/{_ENV_ID}/agent-api-reloaded"
        headers = {
            "Authorization": f"Bearer {_AUTH_TOKEN}",
            "X-Agent-Env-Id": _ENV_ID,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, headers=headers)
        except Exception as exc:
            logger.debug("agent_api: backend reload notify failed (non-fatal): %s", exc)


# Module-level singleton — env-core routes import this.
agent_api_supervisor = AgentApiSupervisor()
