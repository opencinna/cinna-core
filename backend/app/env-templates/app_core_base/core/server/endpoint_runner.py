"""Run one data script the way every dashboards host does.

Shared by the webapp data route (``POST /webapp/api/{endpoint}``) and the
dashboards component route (``POST /dashboards/run``). The contract a script
sees is the kit's (``protocols/dashboards.schema.json`` §5.3): params as one
JSON value on stdin, one JSON value on stdout, logs on stderr, cwd the agent
root.

Hardening, each item a failure seen on a real host:

* the script runs in its own session / process group, and that whole group is
  SIGKILLed when the run ends (timeout, oversize output, or a normal exit), so a
  grandchild it left behind can neither outlive the run nor hold the pipes open;
* stdin is written and closed at once on a separate thread; a script that never
  reads it (``BrokenPipeError`` / ``ConnectionResetError``) is allowed;
* stdout is read incrementally and capped at ``MAX_STDOUT_BYTES``: past it the
  group is killed and the run reports ``too_large`` instead of buffering it;
* only the last ``STDERR_TAIL_CHARS`` characters of stderr are kept.

Params and stdout can carry user data: this module never logs either.

The process is driven by ``subprocess.Popen`` on worker threads rather than
``asyncio`` subprocesses on purpose: ``asyncio``'s ``Process.wait()`` may not
return until every pipe is closed, so a grandchild holding stdout would turn a
finished run into a timeout. ``Popen.wait()`` is a plain ``waitpid``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 300
MAX_STDOUT_BYTES = 5 * 1024 * 1024  # 5,242,880
STDERR_TAIL_CHARS = 2000

#: Script suffix → runtime. Lowercase only, like the kit's checker.
RUNTIMES: dict[str, str] = {".py": "python", ".js": "node", ".mjs": "node", ".cjs": "node"}

#: Exported to every script so a page's tooling can find Node the same way the
#: desktop host advertises it.
NODE_ENV_VAR = "CINNA_NODE"

_READ_CHUNK = 64 * 1024
#: Bytes of stderr kept while reading — enough for STDERR_TAIL_CHARS of UTF-8.
_STDERR_KEEP_BYTES = STDERR_TAIL_CHARS * 4
#: How long the reader threads get to drain after the group is killed.
_READER_JOIN_S = 5.0

#: Runs in flight at once. Each one holds a worker thread for up to its timeout,
#: so runs get their own pool rather than the loop's default executor (which
#: the SDK manager and the MCP tool bridges share).
MAX_CONCURRENT_RUNS = 8
#: Seconds past ``timeout_s`` after which ``run_script`` stops waiting for the
#: worker at all — a backstop for a run the worker cannot finish cleanly.
BACKSTOP_GRACE_S = 10

_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_RUNS, thread_name_prefix="endpoint-run")
#: One slot per worker, held until the worker really finishes (not merely until
#: the backstop gives up on it), so a stuck run keeps its slot and queued runs
#: never start their backstop clock while waiting for a worker.
_slots: dict[int, asyncio.Semaphore] = {}

RunReason = Literal["ok", "timeout", "too_large", "failed", "bad_json"]


class UnsupportedScriptError(ValueError):
    """The script's extension has no runtime (only .py, .js, .mjs, .cjs run)."""


@dataclass(frozen=True)
class RunResult:
    """Outcome of one run.

    ``value_bytes`` is stdout as printed (empty for ``timeout``/``too_large``);
    when ``reason`` is ``ok`` it is one valid JSON document. ``exit_code`` is
    ``None`` when the process never started or was killed at the timeout.
    ``start_error`` is set only when the process could not be started at all.
    """

    ok: bool
    value_bytes: bytes
    exit_code: int | None
    stderr_tail: str
    reason: RunReason
    start_error: str | None = None


def clamp_timeout(value: Any, default: int = DEFAULT_TIMEOUT_S) -> int:
    """A whole number of seconds in ``1..MAX_TIMEOUT_S``; ``default`` when unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(1, min(int(value), MAX_TIMEOUT_S))


def command_for(script: Path, cwd: Path, use_uv: bool = True) -> list[str]:
    """The argv a host runs ``script`` with.

    ``.py`` runs through ``uv run`` when ``use_uv`` is set and the agent root
    (``cwd``) carries a ``pyproject.toml``, so the agent's declared
    dependencies are importable; otherwise with the image's ``python``.
    ``.js/.mjs/.cjs`` run with ``node``. The webapp data route passes
    ``use_uv=False``: its scripts have always run on the image's ``python``.

    Raises:
        UnsupportedScriptError: for any other extension.
    """
    runtime = RUNTIMES.get(script.suffix)
    if runtime == "python":
        if use_uv and (cwd / "pyproject.toml").is_file():
            return ["uv", "run", "--quiet", "python", str(script)]
        return ["python", str(script)]
    if runtime == "node":
        return ["node", str(script)]
    raise UnsupportedScriptError(f"no runtime for {script.suffix or 'a file without extension'}")


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the script's process group (its own session, so pgid == pid)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class _Capture:
    def __init__(self) -> None:
        self.stdout = bytearray()
        self.stderr_tail = b""
        self.overflow = False


def _read_stdout(proc: subprocess.Popen, capture: _Capture) -> None:
    stream = proc.stdout
    assert stream is not None
    while True:
        chunk = stream.read1(_READ_CHUNK)
        if not chunk:
            return
        if len(capture.stdout) + len(chunk) > MAX_STDOUT_BYTES:
            capture.overflow = True
            _kill_group(proc)
            return
        capture.stdout.extend(chunk)


def _read_stderr(proc: subprocess.Popen, capture: _Capture) -> None:
    stream = proc.stderr
    assert stream is not None
    while True:
        chunk = stream.read1(_READ_CHUNK)
        if not chunk:
            return
        capture.stderr_tail = (capture.stderr_tail + chunk)[-_STDERR_KEEP_BYTES:]


def _feed_stdin(proc: subprocess.Popen, payload: bytes) -> None:
    stream = proc.stdin
    assert stream is not None
    try:
        stream.write(payload)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass  # the script does not read its params; that is allowed
    finally:
        try:
            stream.close()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def _tail(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:]


def _run_blocking(command: list[str], payload: bytes, cwd: Path, timeout_s: int) -> RunResult:
    env = {**os.environ, NODE_ENV_VAR: "node"}
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        return RunResult(
            ok=False, value_bytes=b"", exit_code=None, stderr_tail="",
            reason="failed", start_error=exc.strerror or type(exc).__name__,
        )

    capture = _Capture()
    threads = [
        threading.Thread(target=_read_stdout, args=(proc, capture), daemon=True),
        threading.Thread(target=_read_stderr, args=(proc, capture), daemon=True),
        threading.Thread(target=_feed_stdin, args=(proc, payload), daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        exit_code: int | None = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = None
    # Whatever happened, the run is over: end everything the script started,
    # which also releases any pipe a leftover grandchild still holds.
    _kill_group(proc)
    if timed_out:
        proc.wait()
    for thread in threads:
        thread.join(timeout=_READER_JOIN_S)
    # Close a pipe only once its reader is done. A descendant that left the
    # group (setsid) can keep a pipe open; its reader is then still blocked in
    # read1() holding the buffer lock, and close() would block on that lock
    # forever. The stuck daemon reader keeps owning that fd instead.
    for thread, stream in ((threads[0], proc.stdout), (threads[1], proc.stderr)):
        if thread.is_alive() or stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass

    stderr_tail = _tail(capture.stderr_tail)
    if capture.overflow:
        return RunResult(False, b"", exit_code, stderr_tail, "too_large")
    if timed_out:
        return RunResult(False, b"", None, stderr_tail, "timeout")
    stdout = bytes(capture.stdout)
    if exit_code != 0:
        return RunResult(False, stdout, exit_code, stderr_tail, "failed")
    try:
        json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return RunResult(False, stdout, exit_code, stderr_tail, "bad_json")
    return RunResult(True, stdout, exit_code, stderr_tail, "ok")


async def run_script(
    script: Path,
    params: dict,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    cwd: Path | None = None,
    use_uv: bool = True,
) -> RunResult:
    """Run ``script`` with ``params`` on stdin and return its outcome.

    Args:
        script: the script file (its containment is the caller's job).
        params: sent to the script as one JSON object on stdin.
        timeout_s: wall-clock limit, clamped to ``1..MAX_TIMEOUT_S``.
        cwd: working directory — the agent root. Also where ``pyproject.toml``
            is looked for. Defaults to the script's folder.
        use_uv: allow ``uv run`` for ``.py`` (see :func:`command_for`).

    Raises:
        UnsupportedScriptError: the extension has no runtime.
    """
    workdir = cwd if cwd is not None else script.parent
    command = command_for(script, workdir, use_uv=use_uv)
    payload = json.dumps(params).encode("utf-8")
    timeout = clamp_timeout(timeout_s)

    loop = asyncio.get_running_loop()
    slots = _slots.setdefault(id(loop), asyncio.Semaphore(MAX_CONCURRENT_RUNS))
    await slots.acquire()
    try:
        future = loop.run_in_executor(_executor, _run_blocking, command, payload, workdir, timeout)
    except BaseException:
        slots.release()
        raise
    future.add_done_callback(lambda _: slots.release())
    try:
        # shield: giving up on the worker must not cancel the future, whose
        # completion is what frees the slot.
        return await asyncio.wait_for(asyncio.shield(future), timeout + BACKSTOP_GRACE_S)
    except asyncio.TimeoutError:
        return RunResult(False, b"", None, "", "timeout")
