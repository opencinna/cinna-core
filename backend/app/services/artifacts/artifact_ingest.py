"""Turn an upload into a validated, extracted revision tree.

One service for every publisher (desktop, CLI, browser upload, core agent).
The input is either a zip (:class:`ZipSource`) or a list of named files
(:class:`FilesSource`); the output is a scratch folder holding exactly the
files that will be served, plus their manifest. The caller promotes the
scratch folder with :meth:`ArtifactStorage.promote` and writes the DB rows
last; on any error here the scratch folder is already gone.

Rules, in order:

1. Names: ``\\`` is rejected, the name is NFC-normalised and a leading ``./``
   stripped; absolute paths, ``..`` / ``.`` / empty segments and control
   characters are rejected, as are symlink, device / fifo / socket,
   encrypted and unsupported-compression zip entries, and duplicate paths
   after normalisation (case-insensitive).
2. A10: anything under ``.git``, ``.claude``, ``credentials/``, ``app-data/``,
   ``node_modules/``, any ``.env*``, ``*.pem``, ``*.key`` and ``.DS_Store`` is
   dropped and recorded in ``excluded`` (a dropped folder is recorded once, as
   ``<prefix>/``). Directory entries are dropped silently.
3. Limits: file count, per-file and per-revision bytes (declared sizes first,
   then the **actual** decompressed bytes while extracting — headers are never
   trusted), and a per-entry compression ratio (zip bombs).
4. Every kept file gets its sha256 and its MIME type (by extension).
5. Kind: ``site`` when ``entry_path`` is given (it must be a kept ``.html``
   file), else when ``index.html`` is at the root, else when the only file is
   an ``.html``; otherwise ``files``.
"""
import hashlib
import os
import stat
import unicodedata
import zipfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from app.core.config import settings
from app.models.artifacts.artifact import ArtifactKind
from app.services.artifacts.artifact_mime import is_html_path, mime_for_path
from app.services.artifacts.artifact_storage import ArtifactStorage

_CHUNK = 64 * 1024
MAX_PATH_LENGTH = 1024
# A zip may carry many more entries than it publishes (dropped folders such as
# node_modules), but not unboundedly many.
MAX_ENTRIES_FACTOR = 10

# Path segments that drop everything at and below them (file or folder).
# All matching is on casefolded names (``.ENV`` and ``Credentials/`` count too).
_EXCLUDED_ANY_SEGMENT = frozenset({".git", ".claude"})
# Folder names (never matched against the final segment of a file path).
_EXCLUDED_DIR_SEGMENTS = frozenset({"credentials", "app-data", "node_modules"})
_EXCLUDED_FILE_NAMES = frozenset({".ds_store"})
_EXCLUDED_SUFFIXES = (".pem", ".key")
# ``excluded`` lists at most this many records, then one "…and N more".
MAX_EXCLUDED_LISTED = 500
_SUPPORTED_COMPRESSION = frozenset(
    {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
)


class ArtifactIngestError(Exception):
    """A rejected upload: ``status`` is 400 (malformed) or 413 (too large)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class ZipSource:
    """A zip archive: a path on disk or a seekable binary file object."""

    file: str | Path | BinaryIO


@dataclass(frozen=True)
class SourceFile:
    """One uploaded file; ``name`` is its relative path in the artifact."""

    name: str
    file: BinaryIO


@dataclass(frozen=True)
class FilesSource:
    files: list[SourceFile]


@dataclass
class IngestResult:
    tmp_dir: Path
    files: list[dict] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    kind: str = ArtifactKind.FILES
    entry_path: str | None = None
    size_bytes: int = 0
    file_count: int = 0


@dataclass
class _Entry:
    """A file selected for extraction."""

    path: str
    open: Callable[[], AbstractContextManager[BinaryIO]]
    declared_size: int | None = None
    compressed_size: int | None = None


@dataclass
class _Limits:
    max_revision_bytes: int
    max_revision_files: int
    max_file_bytes: int
    max_ratio: int

    @classmethod
    def from_settings(cls) -> "_Limits":
        return cls(
            max_revision_bytes=settings.ARTIFACT_MAX_REVISION_BYTES,
            max_revision_files=settings.ARTIFACT_MAX_REVISION_FILES,
            max_file_bytes=settings.ARTIFACT_MAX_FILE_BYTES,
            max_ratio=settings.ARTIFACT_MAX_COMPRESSION_RATIO,
        )


def _bad(message: str) -> ArtifactIngestError:
    return ArtifactIngestError(400, message)


def _too_large(message: str) -> ArtifactIngestError:
    return ArtifactIngestError(413, message)


def normalize_path(raw: str) -> tuple[str, bool]:
    """Validate one entry name; returns ``(path, is_dir)`` or raises 400.

    ``is_dir`` is True for a name ending in ``/`` (a zip directory entry).
    """
    if not isinstance(raw, str) or raw == "":
        raise _bad("Empty file name in upload")
    if "\\" in raw:
        raise _bad(f"Backslash in path is not allowed: {raw!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise _bad(f"Control character in path: {raw!r}")
    name = unicodedata.normalize("NFC", raw)
    while name.startswith("./"):
        name = name[2:]
    if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        raise _bad(f"Absolute path is not allowed: {raw!r}")
    is_dir = name.endswith("/")
    if is_dir:
        name = name[:-1]
    if not name:
        raise _bad(f"Empty path in upload: {raw!r}")
    if len(name) > MAX_PATH_LENGTH:
        raise _bad(f"Path longer than {MAX_PATH_LENGTH} characters: {raw[:80]!r}…")
    for segment in name.split("/"):
        if segment in ("", ".", ".."):
            raise _bad(f"Invalid path segment in {raw!r}")
    return name, is_dir


def excluded_prefix(path: str) -> str | None:
    """The A10 record for ``path`` (``<folder>/`` or the file path), or None to keep it."""
    segments = path.split("/")
    last = len(segments) - 1
    for i, raw_segment in enumerate(segments):
        segment = raw_segment.casefold()
        is_final = i == last
        if (
            segment in _EXCLUDED_ANY_SEGMENT
            or segment.startswith(".env")
            or (not is_final and segment in _EXCLUDED_DIR_SEGMENTS)
        ):
            return path if is_final else "/".join(segments[: i + 1]) + "/"
    name = segments[-1].casefold()
    if name in _EXCLUDED_FILE_NAMES or name.endswith(_EXCLUDED_SUFFIXES):
        return path
    return None


def _zip_entry_kind_error(info: zipfile.ZipInfo) -> str | None:
    """Why a zip entry is not a regular file or directory, if it is not."""
    mode = info.external_attr >> 16
    if not mode:
        return None
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device file"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    return None


class ArtifactIngestService:
    @staticmethod
    def ingest(
        source: ZipSource | FilesSource, entry_path: str | None = None
    ) -> IngestResult:
        """Validate and extract ``source`` into a fresh scratch folder.

        Raises :class:`ArtifactIngestError`; on error no scratch folder remains.
        """
        ArtifactStorage.sweep_stale_tmp()
        limits = _Limits.from_settings()
        if isinstance(source, ZipSource):
            try:
                with zipfile.ZipFile(source.file) as zf:
                    entries, excluded = _select_zip_entries(zf, limits)
                    return _extract(entries, excluded, entry_path, limits)
            except zipfile.BadZipFile as exc:
                raise _bad(f"Not a valid zip archive: {exc}") from exc
            except zipfile.LargeZipFile as exc:
                raise _too_large("Zip archive is too large") from exc
        if isinstance(source, FilesSource):
            entries, excluded = _select_file_entries(source, limits)
            return _extract(entries, excluded, entry_path, limits)
        raise TypeError(f"Unsupported ingest source: {type(source).__name__}")


def _select(
    candidates: list[tuple[str, bool, _Entry | None]], limits: _Limits
) -> tuple[list[_Entry], list[str]]:
    """Dedupe, apply A10 and the declared-size limits to normalised candidates.

    Duplicates are detected case-insensitively: on a case-insensitive
    filesystem ``A.txt`` and ``a.txt`` (or ``Foo/x`` and ``foo``) collide.
    """
    seen: dict[str, str] = {}  # casefolded path -> path as uploaded
    dirs: set[str] = set()
    kept: list[_Entry] = []
    excluded: set[str] = set()
    for path, is_dir, entry in candidates:
        if is_dir:
            dirs.add(path.casefold())
            continue
        key = path.casefold()
        if key in seen:
            if seen[key] == path:
                raise _bad(f"Duplicate path in upload: {path!r}")
            raise _bad(
                f"Duplicate path (case-insensitive) in upload: {seen[key]!r} and {path!r}"
            )
        seen[key] = path
        record = excluded_prefix(path)
        if record is not None:
            excluded.add(record)
            continue
        assert entry is not None
        kept.append(entry)

    for key, path in seen.items():
        parts = key.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in seen:
                raise _bad(f"Path {path!r} is nested under a file of the same name")
    for d in dirs:
        if d in seen:
            raise _bad(f"Path {seen[d]!r} is both a file and a folder")

    if not kept:
        raise _bad("Nothing to publish: the upload has no files")
    if len(kept) > limits.max_revision_files:
        raise _too_large(
            f"Too many files: {len(kept)} (limit {limits.max_revision_files})"
        )
    declared_total = 0
    for e in kept:
        if e.declared_size is None:
            continue
        if e.declared_size > limits.max_file_bytes:
            raise _too_large(
                f"File {e.path!r} is larger than {limits.max_file_bytes} bytes"
            )
        if e.compressed_size is not None and e.declared_size > 0:
            if (
                e.compressed_size == 0
                or e.declared_size / e.compressed_size > limits.max_ratio
            ):
                raise _bad(
                    f"Entry {e.path!r} exceeds the compression ratio limit "
                    f"({limits.max_ratio})"
                )
        declared_total += e.declared_size
    if declared_total > limits.max_revision_bytes:
        raise _too_large(
            f"Upload is larger than {limits.max_revision_bytes} bytes"
        )
    kept.sort(key=lambda e: e.path)
    listed = sorted(excluded)
    if len(listed) > MAX_EXCLUDED_LISTED:
        rest = len(listed) - MAX_EXCLUDED_LISTED
        listed = listed[:MAX_EXCLUDED_LISTED] + [f"…and {rest} more"]
    return kept, listed


def _select_zip_entries(
    zf: zipfile.ZipFile, limits: _Limits
) -> tuple[list[_Entry], list[str]]:
    infos = zf.infolist()
    if len(infos) > limits.max_revision_files * MAX_ENTRIES_FACTOR:
        raise _too_large(f"Too many entries in the archive: {len(infos)}")
    candidates: list[tuple[str, bool, _Entry | None]] = []
    for info in infos:
        kind_error = _zip_entry_kind_error(info)
        if kind_error:
            raise _bad(f"Archive entry {info.filename!r} is a {kind_error}")
        if info.flag_bits & 0x1:
            raise _bad(f"Archive entry {info.filename!r} is encrypted")
        if info.compress_type not in _SUPPORTED_COMPRESSION:
            raise _bad(
                f"Archive entry {info.filename!r} uses an unsupported compression "
                f"method ({info.compress_type})"
            )
        path, is_dir = normalize_path(info.filename)
        if is_dir or info.is_dir():
            candidates.append((path, True, None))
            continue
        candidates.append(
            (
                path,
                False,
                _Entry(
                    path=path,
                    open=lambda info=info: zf.open(info, "r"),
                    declared_size=info.file_size,
                    compressed_size=info.compress_size,
                ),
            )
        )
    return _select(candidates, limits)


def _select_file_entries(
    source: FilesSource, limits: _Limits
) -> tuple[list[_Entry], list[str]]:
    if len(source.files) > limits.max_revision_files * MAX_ENTRIES_FACTOR:
        raise _too_large(f"Too many files in the upload: {len(source.files)}")
    candidates: list[tuple[str, bool, _Entry | None]] = []
    for item in source.files:
        path, is_dir = normalize_path(item.name)
        if is_dir:
            raise _bad(f"File name ends with '/': {item.name!r}")
        candidates.append(
            (path, False, _Entry(path=path, open=lambda f=item.file: _rewound(f)))
        )
    return _select(candidates, limits)


class _NoClose:
    """Context-manager wrapper that leaves the caller's file open."""

    def __init__(self, f: BinaryIO) -> None:
        self._f = f

    def __enter__(self) -> BinaryIO:
        return self._f

    def __exit__(self, *exc: object) -> None:
        return None


def _rewound(f: BinaryIO) -> _NoClose:
    if f.seekable():
        f.seek(0)
    return _NoClose(f)


def _extract(
    entries: list[_Entry],
    excluded: list[str],
    entry_path: str | None,
    limits: _Limits,
) -> IngestResult:
    # Resolve the kind before touching the disk: a bad entry_path is cheap to reject.
    paths = [e.path for e in entries]
    kind, entry = _detect_kind(paths, entry_path)

    tmp = ArtifactStorage.tmp_dir()
    try:
        manifest: list[dict] = []
        total = 0
        tmp_resolved = tmp.resolve()
        for e in entries:
            target = tmp / e.path
            if tmp_resolved not in target.resolve().parents:
                raise _bad(f"Path escapes the artifact: {e.path!r}")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            size, digest = _copy_entry(e, target, limits, total)
            total += size
            manifest.append(
                {"path": e.path, "size": size, "sha256": digest, "mime": mime_for_path(e.path)}
            )
        return IngestResult(
            tmp_dir=tmp,
            files=manifest,
            excluded=excluded,
            kind=kind,
            entry_path=entry,
            size_bytes=total,
            file_count=len(manifest),
        )
    except zipfile.BadZipFile as exc:
        ArtifactStorage.discard_tmp(tmp)
        raise _bad(f"Corrupt archive entry: {exc}") from exc
    except (FileExistsError, IsADirectoryError, NotADirectoryError) as exc:
        # Defensive: the case-insensitive dedupe above should make this unreachable.
        ArtifactStorage.discard_tmp(tmp)
        raise _bad(f"Conflicting paths in upload: {exc.filename or exc}") from exc
    except BaseException:
        ArtifactStorage.discard_tmp(tmp)
        raise


def _copy_entry(
    e: _Entry, target: Path, limits: _Limits, total_so_far: int
) -> tuple[int, str]:
    """Stream one entry to ``target`` counting the actual bytes; returns (size, sha256)."""
    sha = hashlib.sha256()
    size = 0
    max_by_ratio = (
        e.compressed_size * limits.max_ratio if e.compressed_size is not None else None
    )
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as out, e.open() as src:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > limits.max_file_bytes:
                raise _too_large(
                    f"File {e.path!r} is larger than {limits.max_file_bytes} bytes"
                )
            if total_so_far + size > limits.max_revision_bytes:
                raise _too_large(
                    f"Upload is larger than {limits.max_revision_bytes} bytes"
                )
            if max_by_ratio is not None and size > max_by_ratio:
                raise _bad(
                    f"Entry {e.path!r} exceeds the compression ratio limit "
                    f"({limits.max_ratio})"
                )
            sha.update(chunk)
            out.write(chunk)
    return size, sha.hexdigest()


def _detect_kind(paths: list[str], entry_path: str | None) -> tuple[str, str | None]:
    if entry_path is not None and entry_path.strip():
        entry, is_dir = normalize_path(entry_path.strip())
        if is_dir or entry not in paths:
            raise _bad(f"Entry path {entry_path!r} is not a file of this upload")
        if not is_html_path(entry):
            raise _bad(f"Entry path {entry_path!r} must be an .html file")
        return ArtifactKind.SITE, entry
    if "index.html" in paths:
        return ArtifactKind.SITE, "index.html"
    if len(paths) == 1 and is_html_path(paths[0]):
        return ArtifactKind.SITE, paths[0]
    return ArtifactKind.FILES, None
