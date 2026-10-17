"""Resolving and securing ``/a/`` content requests.

``resolve`` turns ``(cap, path)`` into a file on disk or None — and None is
always a 404, never 401/403, so the route is no oracle for which artifacts or
caps exist. Per request (D1): verify the cap's signature and expiry, load the
artifact and the revision by id, re-check the principal the cap was minted for
(``u:`` — that user can still view; ``l:`` — the link is still live and, when
pinned, pinned to this revision), then look the path up in the revision
manifest. Only the immutable manifest is cached (LRU by revision id).

``build_headers`` is the security envelope every ``/a/`` response carries.
The sandbox has no ``allow-same-origin``, ever: the artifact runs as an opaque
origin, so it cannot read the core's storage, cookies or DOM, nor call the
API with the user's credentials.
"""
import os
import posixpath
import threading
import unicodedata
import uuid
import zipfile
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from sqlmodel import Session, select

from app.core.config import settings
from app.models.artifacts.artifact import Artifact
from app.models.artifacts.artifact_link import ArtifactLink
from app.models.artifacts.artifact_revision import ArtifactRevision
from app.models.users.user import User
from app.services.artifacts import artifact_cap
from app.services.artifacts.artifact_access import ArtifactAccess
from app.services.artifacts.artifact_cap import CapClaims
from app.services.artifacts.artifact_storage import ArtifactStorage

MANIFEST_CACHE_SIZE = 256
EXPOSED_HEADERS = "Content-Length, Content-Range, Accept-Ranges"

_SANDBOX = (
    "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox "
    "allow-downloads allow-modals allow-forms"
)


def frame_ancestor() -> str:
    """Origin of the core SPA (``FRONTEND_HOST``) — the only page allowed to frame content."""
    parts = urlsplit(settings.FRONTEND_HOST)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return "'none'"


def content_csp(base: str, ancestor: str) -> str:
    """The exact CSP of core.md §4, with every source scoped to ``base`` (``B``)."""
    b = base
    return (
        f"{_SANDBOX}; "
        "default-src 'none'; "
        f"script-src {b} 'unsafe-inline' 'wasm-unsafe-eval' blob:; "
        f"style-src {b} 'unsafe-inline'; "
        f"img-src {b} data: blob:; "
        f"font-src {b} data:; "
        f"connect-src {b} data: blob:; "
        f"media-src {b} data: blob:; "
        "worker-src blob:; "
        f"frame-src {b}; "
        "form-action 'none'; "
        "base-uri 'none'; "
        f"frame-ancestors {ancestor}"
    )


def error_csp(ancestor: str) -> str:
    """CSP for error responses: same sandbox, nothing may load."""
    return (
        f"{_SANDBOX}; default-src 'none'; form-action 'none'; base-uri 'none'; "
        f"frame-ancestors {ancestor}"
    )


def _common_headers() -> dict[str, str]:
    return {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": EXPOSED_HEADERS,
    }


def build_headers(cap: str, *, etag: str | None = None) -> dict[str, str]:
    """Headers for a served file (``cap`` must be a verified cap)."""
    headers = {
        "Content-Security-Policy": content_csp(artifact_cap.content_base(cap), frame_ancestor()),
        "Cache-Control": "private, max-age=3600",
        **_common_headers(),
    }
    if etag:
        headers["ETag"] = f'"{etag}"'
    return headers


def error_headers() -> dict[str, str]:
    """Headers for any non-success ``/a/`` response (D19: never cached)."""
    return {
        "Content-Security-Policy": error_csp(frame_ancestor()),
        "Cache-Control": "no-store",
        **_common_headers(),
    }


# ── Manifest cache ──────────────────────────────────────────────────


class _ManifestCache:
    """Bounded LRU of revision id → {path: file entry}. Revisions are immutable."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._data: OrderedDict[uuid.UUID, dict[str, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, revision_id: uuid.UUID) -> dict[str, dict] | None:
        with self._lock:
            manifest = self._data.get(revision_id)
            if manifest is not None:
                self._data.move_to_end(revision_id)
            return manifest

    def put(self, revision_id: uuid.UUID, manifest: dict[str, dict]) -> None:
        with self._lock:
            self._data[revision_id] = manifest
            self._data.move_to_end(revision_id)
            while len(self._data) > self._size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


manifest_cache = _ManifestCache(MANIFEST_CACHE_SIZE)


# ── Resolution ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedRevision:
    claims: CapClaims
    artifact: Artifact
    revision_number: int
    manifest: dict[str, dict]

    @property
    def revision_dir(self) -> Path:
        return ArtifactStorage.revision_dir(self.artifact.id, self.claims.revision_id)


@dataclass(frozen=True)
class ResolvedFile:
    revision: ResolvedRevision
    abs_path: Path
    entry: dict


def _principal_allowed(db: Session, claims: CapClaims, artifact: Artifact) -> bool:
    if claims.via_kind == "u":
        return ArtifactAccess.can_view(db, artifact, db.get(User, claims.via_id))
    link = db.get(ArtifactLink, claims.via_id)
    if link is None or not ArtifactAccess.link_serves(db, link, artifact):
        return False
    return link.pinned_revision_id in (None, claims.revision_id)


def _load_manifest(db: Session, revision_id: uuid.UUID) -> dict[str, dict]:
    manifest = manifest_cache.get(revision_id)
    if manifest is None:
        files = db.exec(
            select(ArtifactRevision.files).where(ArtifactRevision.id == revision_id)
        ).first() or []
        manifest = {f["path"]: f for f in files}
        manifest_cache.put(revision_id, manifest)
    return manifest


class ArtifactContentService:
    @staticmethod
    def resolve_revision(db: Session, cap: str) -> ResolvedRevision | None:
        claims = artifact_cap.verify(cap)
        if claims is None:
            return None
        artifact = db.get(Artifact, claims.artifact_id)
        if artifact is None:
            return None
        number = db.exec(
            select(ArtifactRevision.number).where(
                ArtifactRevision.id == claims.revision_id,
                ArtifactRevision.artifact_id == artifact.id,
            )
        ).first()
        if number is None:
            return None
        if not _principal_allowed(db, claims, artifact):
            return None
        return ResolvedRevision(
            claims=claims,
            artifact=artifact,
            revision_number=number,
            manifest=_load_manifest(db, claims.revision_id),
        )

    @staticmethod
    def lookup(revision: ResolvedRevision, path: str) -> ResolvedFile | None:
        """Manifest lookup with ``""`` / ``dir/`` ⇒ ``index.html``, contained under the revision dir."""
        path = unicodedata.normalize("NFC", path)
        if path == "" or path.endswith("/"):
            path = f"{path}index.html"
        entry = revision.manifest.get(path)
        if entry is None:
            return None
        root = revision.revision_dir.resolve()
        candidate = (root / path).resolve()
        if root not in candidate.parents or not candidate.is_file():
            return None
        return ResolvedFile(revision=revision, abs_path=candidate, entry=entry)

    @staticmethod
    def is_directory(revision: ResolvedRevision, path: str) -> bool:
        """``path`` (no trailing slash) is a folder with an ``index.html`` in the manifest."""
        path = unicodedata.normalize("NFC", path)
        return bool(path) and not path.endswith("/") and f"{path}/index.html" in revision.manifest

    @classmethod
    def resolve(cls, db: Session, cap: str, path: str) -> ResolvedFile | None:
        revision = cls.resolve_revision(db, cap)
        if revision is None:
            return None
        return cls.lookup(revision, path)

    @classmethod
    def revision_zip(
        cls, artifact_id: uuid.UUID, revision_id: uuid.UUID, paths: Iterable[str]
    ) -> Path:
        """The revision's zip, built once and cached beside the revision folder.

        Built into scratch and renamed into place (atomic; concurrent builders
        just race to an identical file). Removed with the revision / artifact.
        """
        target = ArtifactStorage.revision_zip(artifact_id, revision_id)
        if target.is_file():
            return target
        scratch = ArtifactStorage.tmp_dir()
        try:
            built = scratch / "artifact.zip"
            cls.build_zip(ArtifactStorage.revision_dir(artifact_id, revision_id), paths, built)
            os.replace(built, target)
        finally:
            ArtifactStorage.discard_tmp(scratch)
        return target

    @staticmethod
    def build_zip(revision_dir: Path, paths: Iterable[str], target: Path) -> None:
        """Write the manifest ``paths`` under ``revision_dir`` into a zip at ``target``."""
        root = revision_dir.resolve()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(paths):
                source = (root / path).resolve()
                if root in source.parents and source.is_file():
                    zf.write(source, arcname=path)


def download_name(title: str, suffix: str = "") -> str:
    """A filesystem-safe download name derived from a title."""
    safe = "".join(ch if ch.isalnum() or ch in "-_. " else "_" for ch in title).strip(" .")
    return f"{(safe or 'artifact')[:100]}{suffix}"


def file_basename(path: str) -> str:
    return posixpath.basename(path) or "file"
