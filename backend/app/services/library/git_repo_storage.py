"""GitRepoStorage — the bare repositories behind desktop agents.

One bare repo per :class:`~app.models.library.git_repo.GitRepo` row, at
``<GIT_STORAGE_DIR>/<repo_id>.git``. Paths are derived from the row's UUID only,
never from a user-supplied string.

Every git call runs as a subprocess with a timeout and a hardened environment:
no system or global git config is read (``GIT_CONFIG_NOSYSTEM``,
``GIT_CONFIG_GLOBAL=/dev/null``), ``HOME`` is the storage dir, and the hooks
directory is always passed as ``-c core.hooksPath=<GIT_HOOKS_DIR>`` so a path
stored in a repo's config can never point at hooks inside the data volume.

Sync on purpose: callers on the event loop wrap these in a threadpool. The
smart-HTTP transport (later phase) spawns its own async subprocesses and only
uses :meth:`repo_path` / :meth:`git_env` from here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import settings
from app.models.library.git_repo import GitRepoKind

logger = logging.getLogger(__name__)

#: The library's single branch.
DEFAULT_BRANCH = "main"
MAIN_REF = f"refs/heads/{DEFAULT_BRANCH}"
#: The kit manifest at the root of a tree-v3 agent folder.
MANIFEST_PATH = "cinna-agent.json"
#: Removed repos are moved here, not deleted; the operator purges.
DELETED_DIRNAME = ".deleted"
#: Timeout (seconds) for the short local git calls made from this module.
GIT_CALL_TIMEOUT_SECONDS = 30
#: A manifest larger than this is not parsed for the listing cache.
MAX_MANIFEST_BYTES = 1024 * 1024
#: Release notes built from commit messages are capped at this many characters.
MAX_COMMIT_MESSAGES_CHARS = 10_000
_CLONE_URL_SUFFIX_RE = re.compile(
    r"/git/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:\.git)?/?$"
)


class GitRepoStorageError(Exception):
    """A local git operation on a library repo failed."""


class GitRepoStorage:
    """Static helpers over the bare repos in ``settings.GIT_STORAGE_DIR``."""

    # ── Paths and environment ──────────────────────────────────────

    @staticmethod
    def storage_dir() -> Path:
        return Path(settings.GIT_STORAGE_DIR)

    @staticmethod
    def repo_path(repo_id: uuid.UUID | str) -> Path:
        """``<GIT_STORAGE_DIR>/<repo_id>.git``; raises ``ValueError`` for a non-UUID."""
        canonical = uuid.UUID(str(repo_id))  # ValueError on anything else
        return GitRepoStorage.storage_dir() / f"{canonical}.git"

    @staticmethod
    def clone_url(repo_id: uuid.UUID) -> str:
        """The URL Cinna Desktop clones a desktop agent from."""
        return f"{settings.backend_base_url}/git/{repo_id}.git"

    @staticmethod
    def repo_id_in_url(url: str) -> uuid.UUID | None:
        """The core repo a git URL points at, matched on the
        ``/git/<repo_id>[.git][/]`` suffix so a changed ``backend_base_url``
        (or an api./bare-domain origin) still matches."""
        match = _CLONE_URL_SUFFIX_RE.search(url or "")
        if not match:
            return None
        try:
            return uuid.UUID(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def git_env() -> dict[str, str]:
        """Environment for every git subprocess touching library repos."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(GitRepoStorage.storage_dir()),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C.UTF-8",
        }
        # The pre-receive hook is Python and imports ``app``; keep the image's
        # interpreter settings when they are set.
        for key in ("PYTHONPATH", "VIRTUAL_ENV"):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    @staticmethod
    def git_base_args() -> list[str]:
        """``git -c core.hooksPath=<GIT_HOOKS_DIR>`` — the prefix of every spawn."""
        return ["git", "-c", f"core.hooksPath={settings.GIT_HOOKS_DIR}"]

    @staticmethod
    def _run(
        args: list[str], *, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                [*GitRepoStorage.git_base_args(), *args],
                cwd=str(cwd) if cwd else None,
                env=GitRepoStorage.git_env(),
                capture_output=True,
                timeout=GIT_CALL_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitRepoStorageError(f"git {args[0]} timed out") from exc
        if check and result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise GitRepoStorageError(f"git {args[0]} failed: {stderr}")
        return result

    # ── Lifecycle ──────────────────────────────────────────────────

    @staticmethod
    def init_bare(repo_id: uuid.UUID, kind: str) -> Path:
        """Create the bare repo for ``repo_id`` with the library's receive rules."""
        if kind not in GitRepoKind.ALL:
            raise ValueError(f"unknown git repo kind {kind!r}")
        path = GitRepoStorage.repo_path(repo_id)
        if path.exists():
            raise GitRepoStorageError(f"repository {repo_id} already exists")
        GitRepoStorage.storage_dir().mkdir(parents=True, exist_ok=True, mode=0o755)
        try:
            GitRepoStorage._run(
                ["init", "--bare", f"--initial-branch={DEFAULT_BRANCH}", str(path)]
            )
            config = {
                "receive.denyNonFastForwards": "true",
                "receive.denyDeletes": "true",
                "receive.fsckObjects": "true",
                "core.hooksPath": settings.GIT_HOOKS_DIR,
                "cinna.kind": kind,
                "uploadpack.allowFilter": "true",
            }
            for key, value in config.items():
                GitRepoStorage._run(["config", key, value], cwd=path)
        except Exception:
            shutil.rmtree(path, ignore_errors=True)
            raise
        path.chmod(0o755)
        return path

    @staticmethod
    def remove(repo_id: uuid.UUID) -> Path | None:
        """Move the repo to ``<GIT_STORAGE_DIR>/.deleted/<repo_id>-<utc>-<suffix>``.

        Kept, not deleted, so a mistaken delete is recoverable until the
        operator purges. Returns the new path, or ``None`` when the repo was
        already gone.
        """
        path = GitRepoStorage.repo_path(repo_id)
        if not path.exists():
            return None
        trash = GitRepoStorage.storage_dir() / DELETED_DIRNAME
        trash.mkdir(parents=True, exist_ok=True, mode=0o755)
        # Microseconds plus a short random suffix: two removals of the same
        # repo id within one second must never collide.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = trash / f"{uuid.UUID(str(repo_id))}-{stamp}-{uuid.uuid4().hex[:8]}"
        path.rename(target)
        return target

    # ── Reads ──────────────────────────────────────────────────────

    @staticmethod
    def head_sha(repo_id: uuid.UUID) -> str | None:
        """SHA of ``refs/heads/main``, or ``None`` before the first push."""
        path = GitRepoStorage.repo_path(repo_id)
        if not path.exists():
            return None
        result = GitRepoStorage._run(
            ["rev-parse", "--verify", "-q", f"{MAIN_REF}^{{commit}}"],
            cwd=path,
            check=False,
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.decode("ascii", "replace").strip()
        return sha or None

    @staticmethod
    def tree_id(repo_id: uuid.UUID, sha: str) -> str | None:
        """The git tree id of commit ``sha`` (a desktop revision's content hash)."""
        path = GitRepoStorage.repo_path(repo_id)
        if not path.exists() or not _is_hex_sha(sha):
            return None
        result = GitRepoStorage._run(
            ["rev-parse", "--verify", "-q", f"{sha}^{{tree}}"], cwd=path, check=False
        )
        if result.returncode != 0:
            return None
        return result.stdout.decode("ascii", "replace").strip() or None

    @staticmethod
    def commit_messages(repo_id: uuid.UUID, old_sha: str | None, new_sha: str) -> str | None:
        """Messages of the commits in ``old_sha..new_sha`` (all of ``new_sha``'s
        history when ``old_sha`` is None), newest first, trimmed, joined by blank
        lines and capped at :data:`MAX_COMMIT_MESSAGES_CHARS`. ``None`` when
        there are none."""
        path = GitRepoStorage.repo_path(repo_id)
        if not path.exists() or not _is_hex_sha(new_sha):
            return None
        span = f"{old_sha}..{new_sha}" if old_sha and _is_hex_sha(old_sha) else new_sha
        result = GitRepoStorage._run(
            ["log", "--format=%B%x00", span], cwd=path, check=False
        )
        if result.returncode != 0 and span != new_sha:
            # The previous version is gone from the history; take the tip only.
            result = GitRepoStorage._run(
                ["log", "-1", "--format=%B%x00", new_sha], cwd=path, check=False
            )
        if result.returncode != 0:
            return None
        messages = [
            m.strip()
            for m in result.stdout.decode("utf-8", "replace").split("\x00")
            if m.strip()
        ]
        if not messages:
            return None
        text = "\n\n".join(messages)
        if len(text) > MAX_COMMIT_MESSAGES_CHARS:
            text = text[: MAX_COMMIT_MESSAGES_CHARS - 1] + "…"
        return text

    @staticmethod
    def export_paths(
        repo_id: uuid.UUID, sha: str, paths: list[str], dest: Path
    ) -> Path:
        """Extract ``paths`` of commit ``sha`` into ``dest`` (``git archive``,
        hardened env). Paths missing from the tree are skipped. Symlinks and
        anything resolving outside ``dest`` are never written."""
        repo = GitRepoStorage.repo_path(repo_id)
        dest.mkdir(parents=True, exist_ok=True)
        if not repo.exists() or not _is_hex_sha(sha):
            return dest
        present = GitRepoStorage._run(
            ["ls-tree", "--name-only", sha, "--", *paths], cwd=repo, check=False
        )
        wanted = [
            line
            for line in present.stdout.decode("utf-8", "replace").splitlines()
            if line
        ]
        if present.returncode != 0 or not wanted:
            return dest
        archive = dest / ".export.tar"
        try:
            with archive.open("wb") as handle:
                try:
                    proc = subprocess.run(
                        [*GitRepoStorage.git_base_args(), "archive", "--format=tar", sha, "--", *wanted],
                        cwd=str(repo),
                        env=GitRepoStorage.git_env(),
                        stdout=handle,
                        stderr=subprocess.PIPE,
                        timeout=GIT_CALL_TIMEOUT_SECONDS,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise GitRepoStorageError("git archive timed out") from exc
            if proc.returncode != 0:
                stderr = proc.stderr.decode("utf-8", "replace").strip()
                raise GitRepoStorageError(f"git archive failed: {stderr}")
            root = dest.resolve()
            with tarfile.open(archive) as tar:
                safe = [
                    m
                    for m in tar.getmembers()
                    if (m.isfile() or m.isdir())
                    and (root / m.name).resolve().is_relative_to(root)
                ]
                tar.extractall(dest, members=safe, filter="data")
        finally:
            archive.unlink(missing_ok=True)
        return dest

    @staticmethod
    def read_manifest_at(repo_id: uuid.UUID, sha: str) -> dict | None:
        """``cinna-agent.json`` at ``sha``, projected for ``git_repo.manifest_cache``.

        Returns ``None`` for a missing, oversized or unparseable manifest — the
        listing shows no manifest rather than failing.
        """
        path = GitRepoStorage.repo_path(repo_id)
        if not path.exists() or not _is_hex_sha(sha):
            return None
        size = GitRepoStorage._run(
            ["cat-file", "-s", f"{sha}:{MANIFEST_PATH}"], cwd=path, check=False
        )
        if size.returncode != 0:
            return None
        try:
            if int(size.stdout.strip()) > MAX_MANIFEST_BYTES:
                return None
        except ValueError:
            return None
        result = GitRepoStorage._run(
            ["cat-file", "blob", f"{sha}:{MANIFEST_PATH}"], cwd=path, check=False
        )
        if result.returncode != 0:
            return None
        try:
            document = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return project_manifest(document)


def project_manifest(document: object) -> dict | None:
    """``{id, name, description, runtime: {engine}, contract_version}`` or ``None``.

    Only string values are kept; anything else becomes ``None`` so a hostile
    manifest cannot push arbitrary JSON into the listing.
    """
    if not isinstance(document, dict):
        return None

    def text(value: object) -> str | None:
        return value if isinstance(value, str) else None

    runtime = document.get("runtime")
    engine = text(runtime.get("engine")) if isinstance(runtime, dict) else None
    return {
        "id": text(document.get("id")),
        "name": text(document.get("name")),
        "description": text(document.get("description")),
        "runtime": {"engine": engine},
        "contract_version": text(document.get("contract_version")),
    }


def _is_hex_sha(value: str) -> bool:
    return 4 <= len(value) <= 64 and all(c in "0123456789abcdef" for c in value.lower())
