"""Reading a brand theme package: a zip, a folder, or theme.json plus files.

Produces the ``(theme_json_bytes, {"assets/<name>": bytes})`` pair the
validator takes. The zip reader is defensive (entry count, per-file and total
uncompressed size, no absolute or ``..`` names) because the upload is
untrusted; it never touches the filesystem.

Pure module: standard library only.
"""
from __future__ import annotations

import io
import os
import zipfile
from collections.abc import Iterable

from .schema import ASSET_DIR, BACKGROUND_RASTER_MAX_BYTES, MIB

THEME_FILE = "theme.json"
MAX_ENTRIES = 64
# Largest single file the rules can accept, plus slack for the error message
# to name its real size rather than a truncated one.
MAX_FILE_BYTES = BACKGROUND_RASTER_MAX_BYTES + MIB
MAX_TOTAL_BYTES = 16 * MIB
THEME_MAX_BYTES = 256 * 1024

_IGNORED_PARTS = ("__MACOSX",)
_IGNORED_NAMES = (".DS_Store", "Thumbs.db")


class PackageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _ignored(name: str) -> bool:
    parts = name.split("/")
    return any(p in _IGNORED_PARTS for p in parts) or parts[-1] in _IGNORED_NAMES or parts[-1].startswith("._")


def read_zip(data: bytes) -> tuple[bytes, dict[str, bytes]]:
    """Read a zipped package. ``theme.json`` may sit at the root or inside a
    single top-level folder (``acme/theme.json``), as zipping a folder gives."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PackageError("invalid_package", "the upload is not a valid zip file.") from None
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir() and not _ignored(i.filename)]
        if len(infos) > MAX_ENTRIES:
            raise PackageError("invalid_package", f"the zip has {len(infos)} files; at most {MAX_ENTRIES} are allowed.")
        total = 0
        for info in infos:
            name = info.filename
            if name.startswith("/") or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")):
                raise PackageError("invalid_package", f"zip entry {name!r} has an unsafe path.")
            if info.file_size > MAX_FILE_BYTES:
                raise PackageError("invalid_package", f"{name} is {info.file_size // MIB} MB uncompressed; too large.")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise PackageError("invalid_package", f"the package is over {MAX_TOTAL_BYTES // MIB} MB uncompressed.")
        names = [i.filename for i in infos]
        prefix = ""
        if THEME_FILE not in names:
            nested = [n for n in names if n.count("/") == 1 and n.endswith("/" + THEME_FILE)]
            if len(nested) != 1:
                raise PackageError("missing_theme", "the zip has no theme.json at its root (or in one top-level folder).")
            prefix = nested[0][: -len(THEME_FILE)]
        theme: bytes | None = None
        assets: dict[str, bytes] = {}
        for info in infos:
            if not info.filename.startswith(prefix):
                continue
            rel = info.filename[len(prefix):]
            # Read with a hard cap: the header's file_size is attacker-controlled.
            with archive.open(info) as fh:
                content = fh.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise PackageError("invalid_package", f"{info.filename} is larger than its zip header says.")
            if rel == THEME_FILE:
                theme = content
            elif rel.startswith(ASSET_DIR):
                assets[rel] = content
            # Anything else (README, previews) is not part of the package.
    if theme is None:
        raise PackageError("missing_theme", "the zip has no theme.json.")
    if len(theme) > THEME_MAX_BYTES:
        raise PackageError("invalid_package", "theme.json is too large.")
    return theme, assets


def assets_from_files(files: Iterable[tuple[str, bytes]]) -> dict[str, bytes]:
    """Loose uploads keyed by file name: ``logo-dark.svg`` → ``assets/logo-dark.svg``
    (a browser sends base names only; a leading ``assets/`` is accepted too).
    Same entry and total-size caps as a zip; two files with one name are
    refused rather than one silently replacing the other."""
    assets: dict[str, bytes] = {}
    total = 0
    for name, content in files:
        base = name.replace("\\", "/").split("/")[-1]
        if not base or base in (".", "..") or _ignored(base):
            continue
        key = ASSET_DIR + base
        if key in assets:
            raise PackageError("invalid_package", f"two uploaded files are named {base!r}; rename one.")
        if len(assets) >= MAX_ENTRIES:
            raise PackageError("invalid_package", f"at most {MAX_ENTRIES} asset files are allowed.")
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise PackageError("invalid_package", f"the package is over {MAX_TOTAL_BYTES // MIB} MB.")
        assets[key] = content
    return assets


def read_folder(path: str) -> tuple[bytes, dict[str, bytes]]:
    """Read an unpacked package folder (used by the authoring skill's CLI)."""
    with open(os.path.join(path, THEME_FILE), "rb") as fh:
        theme = fh.read()
    assets: dict[str, bytes] = {}
    asset_root = os.path.join(path, ASSET_DIR.rstrip("/"))
    if os.path.isdir(asset_root):
        for root, _dirs, files in os.walk(asset_root):
            for name in files:
                if _ignored(name):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, path).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    assets[rel] = fh.read()
    return theme, assets
