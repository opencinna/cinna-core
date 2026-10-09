"""GitHttpService — git smart HTTP for the team library.

Serves ``git upload-pack`` / ``git receive-pack`` in ``--stateless-rpc`` mode
(what ``git http-backend`` does) from async subprocesses, so a large clone or
push never blocks the event loop:

- the request body is streamed into git's stdin by a feeder task (gzip bodies
  inflated on the fly, size-capped by ``GIT_MAX_PUSH_BYTES``);
- git's stdout is yielded back in 64 KiB chunks concurrently, so a big pack
  cannot deadlock the pipes;
- the whole exchange is bounded by ``GIT_HTTP_TIMEOUT_SECONDS``; on timeout,
  client disconnect or any error the process is stopped. receive-pack gets
  SIGTERM first (git removes its ref locks and quarantine on it), SIGKILL only
  if it does not exit within a few seconds.

Every spawn passes ``-c core.hooksPath=<GIT_HOOKS_DIR>`` and the hardened env of
:class:`GitRepoStorage`; receive-pack adds ``receive.fsckObjects`` and the
``CINNA_*`` variables the ``pre-receive`` policy hook reads, and is refused
outright (503) when the hook is missing or not executable — git would silently
skip it. Nothing from a pushed tree is ever executed or checked out.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import uuid
import zlib
from collections.abc import AsyncIterator
from pathlib import Path

from sqlmodel import Session
from starlette.requests import ClientDisconnect

from app.core.config import settings
from app.models.library.git_repo import GitRepo, GitRepoKind, LibraryAccess
from app.models.users.user import User
from app.services.cli.platform_knowledge_assets import local_kit_dir
from app.services.library.exceptions import LibraryAccessDeniedError
from app.services.library.git_repo_storage import MAIN_REF, GitRepoStorage
from app.services.library.library_access_service import LibraryAccessService

logger = logging.getLogger(__name__)

UPLOAD_PACK = "git-upload-pack"
RECEIVE_PACK = "git-receive-pack"
SERVICES = (UPLOAD_PACK, RECEIVE_PACK)

READ_CHUNK_BYTES = 65536
#: Upper bound for one inflate step, so a gzip bomb cannot balloon memory.
INFLATE_STEP_BYTES = 1024 * 1024
#: The receive-pack command list precedes the pack; stop looking past this.
MAX_COMMAND_SECTION_BYTES = 1024 * 1024
#: Seconds receive-pack gets to exit on SIGTERM before SIGKILL.
TERMINATE_GRACE_SECONDS = 5
REPO_FILE_SUFFIX = ".git"
PRE_RECEIVE_HOOK = "pre-receive"
_GIT_PROTOCOL_RE = re.compile(r"^[A-Za-z0-9=:._-]{1,256}$")

HOOK_UNAVAILABLE_DETAIL = (
    "The team library can't accept changes right now. "
    "Ask your admin to look at the Cinna server."
)

#: Reaper tasks kept referenced until done (asyncio holds only weak refs).
_REAPERS: set[asyncio.Task[None]] = set()


class GitHttpError(Exception):
    """A transport-level failure with the HTTP status to answer."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitPushTooLargeError(GitHttpError):
    def __init__(self) -> None:
        super().__init__(413, "This change is too big to share with the team.")


def advertisement_content_type(service: str) -> str:
    return f"application/x-{service}-advertisement"


def result_content_type(service: str) -> str:
    return f"application/x-{service}-result"


def _pkt_line(text: str) -> bytes:
    data = text.encode("utf-8")
    return f"{len(data) + 4:04x}".encode("ascii") + data


class ReceiveCommands:
    """The ``<old> <new> <ref>`` commands at the head of a receive-pack body.

    Fed the (inflated) request bytes by the feeder; stops at the first flush
    packet. ``main_new`` is the SHA the client asked ``refs/heads/main`` to
    become, so bookkeeping can tell this push's result from a racing one's.
    """

    def __init__(self) -> None:
        self.main_new: str | None = None
        self.done = False
        self._buffer = b""
        self._seen = 0

    def feed(self, data: bytes) -> None:
        if self.done or not data:
            return
        self._seen += len(data)
        self._buffer += data
        while not self.done:
            if len(self._buffer) < 4:
                break
            try:
                length = int(self._buffer[:4], 16)
            except ValueError:
                self.done = True
                break
            if length < 4:  # flush / delim: end of the command list
                self.done = True
                break
            if len(self._buffer) < length:
                break
            line = self._buffer[4:length]
            self._buffer = self._buffer[length:]
            self._parse_command(line)
        if self._seen > MAX_COMMAND_SECTION_BYTES:
            self.done = True
        if self.done:
            self._buffer = b""

    def _parse_command(self, line: bytes) -> None:
        command = line.split(b"\0", 1)[0].strip().decode("ascii", "replace")
        parts = command.split(" ")
        if len(parts) == 3 and parts[2] == MAIN_REF:
            self.main_new = parts[1]


class GitHttpService:
    """Stateless helpers; one call per HTTP request."""

    @staticmethod
    def parse_repo_file(repo_file: str) -> uuid.UUID | None:
        """``<uuid>.git`` → the repo id, else ``None`` (the route answers 404)."""
        if not repo_file.endswith(REPO_FILE_SUFFIX):
            return None
        try:
            return uuid.UUID(repo_file[: -len(REPO_FILE_SUFFIX)])
        except ValueError:
            return None

    @staticmethod
    def clean_git_protocol(value: str | None) -> str | None:
        """The ``Git-Protocol`` header when well-formed, else ``None`` (dropped)."""
        if value and _GIT_PROTOCOL_RE.match(value):
            return value
        return None

    @staticmethod
    def hook_path() -> Path:
        return Path(settings.GIT_HOOKS_DIR) / PRE_RECEIVE_HOOK

    @staticmethod
    def hook_ready() -> bool:
        """True when the policy hook exists and is executable (git skips it otherwise)."""
        hook = GitHttpService.hook_path()
        try:
            mode = hook.stat().st_mode
        except OSError:
            return False
        # Check the mode bits too: ``os.access(X_OK)`` is True for root on some
        # filesystems even when no execute bit is set.
        executable_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        return stat.S_ISREG(mode) and bool(mode & executable_bits) and os.access(hook, os.X_OK)

    @staticmethod
    def log_hook_status() -> None:
        """Startup check: shout when shares would be refused for a missing hook."""
        if not GitHttpService.hook_ready():
            logger.error(
                "Team library pre-receive hook missing or not executable at %s; "
                "every share (git push) will be refused with 503",
                GitHttpService.hook_path(),
            )

    @staticmethod
    def authorize(session: Session, repo: GitRepo, user: User, service: str) -> None:
        """Grant decision for one transport call; raises ``LibraryAccessDeniedError``.

        ``read`` for upload-pack (any superuser may clone, for oversight),
        ``write`` for receive-pack; mirrors are never writable.
        """
        access = LibraryAccessService.effective_access(session, repo, user)
        if service == RECEIVE_PACK:
            if repo.kind == GitRepoKind.MIRROR or LibraryAccess.rank(access) < LibraryAccess.rank(
                LibraryAccess.WRITE
            ):
                raise LibraryAccessDeniedError("You can't share changes to this agent.")
        elif access is None and not user.is_superuser:
            raise LibraryAccessDeniedError("You don't have access to this agent.")

    @staticmethod
    def spawn_env(
        service: str,
        git_protocol: str | None,
        *,
        repo_kind: str | None = None,
        pusher_id: uuid.UUID | None = None,
    ) -> dict[str, str]:
        env = GitRepoStorage.git_env()
        git_protocol = GitHttpService.clean_git_protocol(git_protocol)
        if git_protocol:
            env["GIT_PROTOCOL"] = git_protocol
        if service == RECEIVE_PACK:
            env.update(
                {
                    "CINNA_REPO_KIND": repo_kind or "",
                    "CINNA_KIT_DIR": str(local_kit_dir().resolve()),  # hook cwd is the repo
                    "CINNA_GIT_MAX_BLOB_BYTES": str(settings.GIT_MAX_BLOB_BYTES),
                    "CINNA_GIT_MAX_TREE_BYTES": str(settings.GIT_MAX_TREE_BYTES),
                    "CINNA_PUSHER_ID": str(pusher_id or ""),
                }
            )
        return env

    @staticmethod
    def _args(service: str, repo_id: uuid.UUID, *extra: str) -> list[str]:
        subcommand = service.removeprefix("git-")  # upload-pack / receive-pack
        receive_only = ["-c", "receive.fsckObjects=true"] if service == RECEIVE_PACK else []
        return [
            *GitRepoStorage.git_base_args(),
            *receive_only,
            subcommand,
            "--stateless-rpc",
            *extra,
            str(GitRepoStorage.repo_path(repo_id)),
        ]

    @staticmethod
    def _require_hook(service: str) -> None:
        if service == RECEIVE_PACK and not GitHttpService.hook_ready():
            logger.error(
                "Refusing git-receive-pack: pre-receive hook missing or not executable at %s",
                GitHttpService.hook_path(),
            )
            raise GitHttpError(503, HOOK_UNAVAILABLE_DETAIL)

    # ── info/refs ──────────────────────────────────────────────────

    @staticmethod
    async def advertise(
        repo_id: uuid.UUID,
        service: str,
        git_protocol: str | None,
        *,
        repo_kind: str | None = None,
    ) -> bytes:
        """Body of ``GET info/refs?service=...``.

        Protocol v0/v1 answers start with the ``# service=`` pkt-line and a
        flush; protocol v2 answers do not (as ``http-backend.c``).
        """
        GitHttpService._require_hook(service)
        git_protocol = GitHttpService.clean_git_protocol(git_protocol)
        env = GitHttpService.spawn_env(service, git_protocol, repo_kind=repo_kind)
        proc = await asyncio.create_subprocess_exec(
            *GitHttpService._args(service, repo_id, "--advertise-refs"),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            async with asyncio.timeout(settings.GIT_HTTP_TIMEOUT_SECONDS):
                stdout, stderr = await proc.communicate()
        except TimeoutError as exc:
            _kill(proc)
            raise GitHttpError(504, "Timed out") from exc
        finally:
            if proc.returncode is None:
                _kill(proc)
        if proc.returncode != 0:
            logger.warning(
                "git %s --advertise-refs failed for %s: %s",
                service,
                repo_id,
                stderr.decode("utf-8", "replace").strip(),
            )
            raise GitHttpError(500, "The team library is unavailable")
        if git_protocol and "version=2" in git_protocol:
            return stdout
        return _pkt_line(f"# service={service}\n") + b"0000" + stdout

    # ── RPC ────────────────────────────────────────────────────────

    @staticmethod
    async def run_rpc(
        repo_id: uuid.UUID,
        service: str,
        body: AsyncIterator[bytes],
        *,
        gzip: bool,
        git_protocol: str | None,
        repo_kind: str | None = None,
        pusher_id: uuid.UUID | None = None,
        commands: ReceiveCommands | None = None,
    ) -> AsyncIterator[bytes]:
        """Run one stateless RPC; yields git's stdout.

        Raises :class:`GitHttpError`: 503 when the policy hook is not ready
        (receive-pack), 413 past ``GIT_MAX_PUSH_BYTES``, 400 for a bad or
        abandoned request body, 504 on timeout. The caller should pull the first
        chunk before starting its response so those can still set the status.
        ``commands`` (receive-pack) is filled with the pushed ref updates.
        """
        GitHttpService._require_hook(service)
        graceful = service == RECEIVE_PACK
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.GIT_HTTP_TIMEOUT_SECONDS
        env = GitHttpService.spawn_env(
            service, git_protocol, repo_kind=repo_kind, pusher_id=pusher_id
        )
        proc = await asyncio.create_subprocess_exec(
            *GitHttpService._args(service, repo_id),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin and proc.stdout and proc.stderr
        stderr_task = asyncio.create_task(proc.stderr.read())
        feeder = asyncio.create_task(
            _feed_stdin(
                proc,
                body,
                gzip=gzip,
                max_bytes=settings.GIT_MAX_PUSH_BYTES,
                commands=commands,
            )
        )
        # A failed feed (too large, body error, client gone) stops git so
        # stdout hits EOF.
        feeder.add_done_callback(
            lambda task: _stop(proc, graceful)
            if not task.cancelled() and task.exception()
            else None
        )
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                chunk = await asyncio.wait_for(proc.stdout.read(READ_CHUNK_BYTES), remaining)
                if not chunk:
                    break
                yield chunk
            try:
                await asyncio.wait_for(feeder, max(deadline - loop.time(), 0.001))
            except (GitHttpError, TimeoutError):
                raise
            except Exception as exc:
                logger.warning("git %s request body failed for %s: %r", service, repo_id, exc)
                raise GitHttpError(400, "The request could not be read") from exc
            await asyncio.wait_for(proc.wait(), max(deadline - loop.time(), 0.001))
        except TimeoutError as exc:
            raise GitHttpError(504, "Timed out") from exc
        finally:
            if not feeder.done():
                feeder.cancel()
            if proc.returncode is None:
                _stop(proc, graceful)
            # Reap (and escalate to SIGKILL if needed) off the request path;
            # never await here (a cancelled stream would re-raise in ``finally``).
            reaper = loop.create_task(_reap(proc, stderr_task, service, repo_id))
            _REAPERS.add(reaper)
            reaper.add_done_callback(_REAPERS.discard)


async def _feed_stdin(
    proc: asyncio.subprocess.Process,
    body: AsyncIterator[bytes],
    *,
    gzip: bool,
    max_bytes: int,
    commands: ReceiveCommands | None = None,
) -> None:
    """Stream the request body into git's stdin, inflating gzip, capped."""
    assert proc.stdin is not None
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS) if gzip else None
    total = 0

    async def write(data: bytes) -> None:
        nonlocal total
        if not data:
            return
        total += len(data)
        if total > max_bytes:
            raise GitPushTooLargeError()
        if commands is not None:
            commands.feed(data)
        proc.stdin.write(data)
        await proc.stdin.drain()

    try:
        async for chunk in body:
            if inflater is None:
                await write(chunk)
                continue
            try:
                data = inflater.decompress(chunk, INFLATE_STEP_BYTES)
                await write(data)
                while inflater.unconsumed_tail:
                    data = inflater.decompress(inflater.unconsumed_tail, INFLATE_STEP_BYTES)
                    await write(data)
            except zlib.error as exc:
                raise GitHttpError(400, "Malformed gzip request body") from exc
        if inflater is not None:
            await write(inflater.flush())
    except (BrokenPipeError, ConnectionResetError):
        return  # git stopped reading; its exit status and output tell the rest
    except ClientDisconnect as exc:
        raise GitHttpError(400, "The client went away") from exc
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _stop(proc: asyncio.subprocess.Process, graceful: bool) -> None:
    """SIGTERM for receive-pack (lets git drop ref locks / quarantine; the
    reaper escalates), SIGKILL otherwise."""
    if proc.returncode is not None:
        return
    if not graceful:
        _kill(proc)
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        pass


async def _reap(
    proc: asyncio.subprocess.Process,
    stderr_task: asyncio.Task[bytes],
    service: str,
    repo_id: uuid.UUID,
) -> None:
    try:
        try:
            await asyncio.wait_for(proc.wait(), TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            if proc.returncode is None:
                _kill(proc)
            await asyncio.wait_for(proc.wait(), 30)
        stderr = await asyncio.wait_for(stderr_task, 5)
    except (TimeoutError, asyncio.CancelledError):
        return
    except Exception:  # noqa: BLE001 — logging only
        logger.exception("Failed to reap git %s for %s", service, repo_id)
        return
    if proc.returncode not in (0, None) and stderr:
        logger.info(
            "git %s for %s exited %s: %s",
            service,
            repo_id,
            proc.returncode,
            stderr.decode("utf-8", "replace").strip()[:2000],
        )
