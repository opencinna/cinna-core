"""Skills published from a desktop: preview and upload publish (U5, U8).

A desktop agent's authored skill lives in ``skills/<name>/`` on the person's
computer, not on any workspace core can read, so the desktop sends the folder
as a tar.gz (``tar -C skills -czf x.tgz <name>``, decision P11). Core extracts
it into a temporary directory and runs the same publish core as the agent path
(:meth:`SkillCatalogService._publish_tree`), so both paths produce the same
snapshot, archive, revision row and events.

Order matters as in the agent path: every refusal runs before anything is
written. The archive is untrusted input, so extraction checks each member
while reading the stream (path, type, member count, cumulative size) instead
of extracting first and measuring afterwards.

Any active user may publish this way (U5); the cloud agent path keeps its
developer gate.
"""
from __future__ import annotations

import asyncio
import contextlib
import gzip
import io
import logging
import shutil
import tarfile
import tempfile
import uuid
import zlib
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from fastapi import UploadFile
from sqlmodel import Session

from app.models.catalog.catalog_access import CatalogAccess
from app.models.skills.schemas import (
    SkillDesktopPublishPreviewIn,
    SkillPublishPreview,
    SkillUploadMeta,
)
from app.models.skills.skill_package import SkillPackage, SkillPackageVisibility
from app.models.skills.skill_package_revision import SkillPackageRevision
from app.models.users.user import User
from app.services.agents.skill_manifest import (
    SKILL_NAME_RE,
    coerce_version,
    parse_credential_declarations,
    parse_skill_dir,
)
from app.services.catalog.catalog_access_service import SKILL, CatalogAccessService
from app.services.catalog.catalog_events import CatalogEvents
from app.services.catalog.exceptions import CatalogError
from app.services.skills.exceptions import SkillCatalogError
from app.services.skills.skill_catalog_service import (
    MAX_PACKAGE_BYTES,
    SkillCatalogService,
    _publish_lock_for,
)
from app.services.skills.skill_credential_requirements import (
    SkillCredentialRequirements,
    SkillCredentialResolution,
)

logger = logging.getLogger(__name__)

#: Most members one uploaded skill archive may hold (files and directories).
MAX_ARCHIVE_MEMBERS = 2000

#: Decompressed bytes allowed beyond the file contents: tar headers, padding
#: and pax/long-name records. 2000 members cost about 2 MiB of framing; the
#: rest is headroom. Bounds what a crafted header can make the reader inflate.
_FRAMING_SLACK_BYTES = 4 * 1024 * 1024

_CHUNK_BYTES = 1024 * 1024


class _ArchiveRefused(Exception):
    """Raised inside extraction; turned into ``skill_archive_invalid``."""


class _CappedReader:
    """A read-only stream that refuses to yield more than ``limit`` bytes.

    Wraps the gzip stream so a decompression bomb (or a pax header claiming a
    gigabyte) stops at the cap instead of inflating into memory or disk.
    """

    def __init__(self, raw: BinaryIO, limit: int) -> None:
        self._raw = raw
        self._limit = limit
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._limit - self._read + 1
        data = self._raw.read(size)
        self._read += len(data)
        if self._read > self._limit:
            raise _ArchiveRefused("the archive unpacks to more than the size limit")
        return data


def _archive_invalid(reason: str) -> SkillCatalogError:
    return SkillCatalogError(
        "skill_archive_invalid", f"This skill archive cannot be used: {reason}."
    )


async def read_capped(upload: UploadFile, limit: int = MAX_PACKAGE_BYTES) -> bytes:
    """The uploaded archive, read in chunks and refused past ``limit`` bytes
    (413 ``skill_too_large``)."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise SkillCatalogError(
                "skill_too_large",
                f"This skill archive is over the "
                f"{limit // (1024 * 1024)} MB limit.",
                status=413,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _member_parts(name: str) -> tuple[str, ...]:
    """The path segments of an archive member, or a refusal.

    Refuses absolute paths, ``..``, backslashes and NUL. ``./`` segments are
    dropped, so ``./pdf-report/SKILL.md`` and ``pdf-report/SKILL.md`` read the
    same.
    """
    if not name or "\x00" in name or "\\" in name:
        raise _ArchiveRefused(f"'{name}' is not a usable path")
    if name.startswith("/"):
        raise _ArchiveRefused(f"'{name}' is an absolute path")
    parts = tuple(part for part in PurePosixPath(name).parts if part != ".")
    if ".." in parts:
        raise _ArchiveRefused(f"'{name}' points outside the skill folder")
    return parts


def safe_extract(data: bytes, dest: Path) -> str:
    """Extract a skill tar.gz into ``dest`` and return its one top-level name.

    Runs OFF the event loop. Every member is checked before a byte of it is
    written: a path inside ``dest`` (no absolute path, no ``..``), a regular
    file or a directory (no symlink, hardlink, device, fifo or sparse file),
    one shared top-level directory, at most :data:`MAX_ARCHIVE_MEMBERS`
    members, and a running total of file bytes within
    :data:`MAX_PACKAGE_BYTES`. The decompressed stream itself is capped too.
    ``tarfile.data_filter`` runs as a second opinion where Python has it.
    Files are written by this function, never by ``tarfile.extract``.

    Raises:
        SkillCatalogError: ``skill_archive_invalid`` on any refusal.
    """
    dest_root = dest.resolve()
    has_data_filter = hasattr(tarfile, "data_filter")
    members = 0
    total_bytes = 0
    top: str | None = None
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            reader = _CappedReader(gz, MAX_PACKAGE_BYTES + _FRAMING_SLACK_BYTES)
            with tarfile.open(fileobj=reader, mode="r|") as tar:  # type: ignore[call-overload]
                for member in tar:
                    members += 1
                    if members > MAX_ARCHIVE_MEMBERS:
                        raise _ArchiveRefused(
                            f"it holds more than {MAX_ARCHIVE_MEMBERS} entries"
                        )
                    is_dir = member.isdir()
                    if member.issparse() or not (is_dir or member.isreg()):
                        raise _ArchiveRefused(
                            f"'{member.name}' is a link or special file; only "
                            "files and folders are allowed"
                        )
                    parts = _member_parts(member.name)
                    if not parts:
                        if is_dir:
                            continue  # the archive's own "./" entry
                        raise _ArchiveRefused(f"'{member.name}' has no name")
                    if has_data_filter:
                        try:
                            tarfile.data_filter(member, str(dest_root))
                        except tarfile.FilterError as exc:
                            raise _ArchiveRefused(
                                f"'{member.name}' is not allowed ({exc})"
                            ) from exc
                    if top is None:
                        top = parts[0]
                    elif parts[0] != top:
                        raise _ArchiveRefused(
                            "it must hold exactly one folder, the skill itself"
                        )
                    if len(parts) == 1 and not is_dir:
                        raise _ArchiveRefused(
                            "it must hold the skill folder, not loose files"
                        )

                    target = dest_root.joinpath(*parts)
                    if not target.resolve().is_relative_to(dest_root):
                        raise _ArchiveRefused(
                            f"'{member.name}' points outside the skill folder"
                        )
                    if is_dir:
                        if target.exists() and not target.is_dir():
                            raise _ArchiveRefused(f"'{member.name}' appears twice")
                        target.mkdir(parents=True, exist_ok=True, mode=0o755)
                        continue

                    total_bytes += member.size
                    if total_bytes > MAX_PACKAGE_BYTES:
                        raise _ArchiveRefused(
                            f"it unpacks to more than "
                            f"{MAX_PACKAGE_BYTES // (1024 * 1024)} MB"
                        )
                    if target.exists():
                        raise _ArchiveRefused(f"'{member.name}' appears twice")
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    _write_member(tar, member, target)
    except _ArchiveRefused as exc:
        raise _archive_invalid(str(exc)) from exc
    except (tarfile.TarError, gzip.BadGzipFile, zlib.error, EOFError, OSError) as exc:
        raise _archive_invalid("it is not a readable tar.gz") from exc

    if top is None or not (dest_root / top).is_dir():
        raise _archive_invalid("it is empty")
    return top


def _write_member(tar: tarfile.TarFile, member: tarfile.TarInfo, target: Path) -> None:
    """Copy one regular member to ``target``, never more than its header size."""
    source = tar.extractfile(member)
    if source is None:
        raise _ArchiveRefused(f"'{member.name}' cannot be read")
    remaining = member.size
    with open(target, "xb") as out:
        while remaining > 0:
            chunk = source.read(min(_CHUNK_BYTES, remaining))
            if not chunk:
                raise _ArchiveRefused(f"'{member.name}' is truncated")
            out.write(chunk)
            remaining -= len(chunk)
    target.chmod(0o755 if member.mode & 0o111 else 0o644)


def _user_provided(declarations: list[dict[str, Any]]) -> list[SkillCredentialResolution]:
    """A desktop publish has no agent to resolve slots against: every declared
    slot is filled by each installer (``provided_by='user'``)."""
    return [
        SkillCredentialResolution(
            slot=declaration["slot"],
            type=declaration["type"],
            description=declaration.get("description"),
            provided_by="user",
            credential=None,
            reason=None,
            producer_agent_id=None,
        )
        for declaration in declarations
    ]


def _editable_package(
    session: Session, package_uuid: uuid.UUID, user: User
) -> SkillPackage:
    """The package to publish into: 404 when the caller cannot see it, 403
    without ``edit``."""
    package = session.get(SkillPackage, package_uuid)
    access = (
        SkillCatalogService.access_of(session, package, user)
        if package is not None
        else None
    )
    if package is None or access is None:
        raise SkillCatalogError("package_not_found", "Skill package not found")
    if not CatalogAccessService.at_least(access, CatalogAccess.EDIT):
        raise SkillCatalogError(
            "not_publisher", "You can't publish new versions of this skill."
        )
    return package


def _name_mismatch(package: SkillPackage, name: str) -> SkillCatalogError:
    return SkillCatalogError(
        "skill_name_mismatch",
        f"This package holds the skill '{package.name}', not '{name}'.",
    )


def _package_exists(package: SkillPackage) -> SkillCatalogError:
    return SkillCatalogError(
        "package_exists",
        f"You already publish a skill named '{package.name}'. Publish it as a "
        "new version of that package.",
        extra={"package_uuid": str(package.id)},
    )


class SkillUploadService:
    """Preview and publish of a skill uploaded from a desktop."""

    @staticmethod
    def publish_preview(
        session: Session, *, user: User, body: SkillDesktopPublishPreviewIn
    ) -> SkillPublishPreview:
        """What :meth:`publish` would produce for this skill, from its name and
        frontmatter. Like the agent preview it runs no content refusals; the
        publish does."""
        name = body.name
        if not SKILL_NAME_RE.match(name):
            raise SkillCatalogError(
                "skill_invalid",
                "A skill name uses lowercase letters, digits and single hyphens.",
            )
        if body.package_uuid is not None:
            existing: SkillPackage | None = _editable_package(
                session, body.package_uuid, user
            )
            if existing.name != name:
                raise _name_mismatch(existing, name)
        else:
            existing = SkillCatalogService._find_publisher_package(
                session, publisher_user_id=user.id, entry_name=name
            )

        if existing is not None:
            package_id, disambiguated = existing.package_id, False
            next_number, published_versions, latest_version = (
                SkillCatalogService._version_history(session, existing.id)
            )
        else:
            package_id, disambiguated = SkillCatalogService.derive_package_id(
                session, publisher_user_id=user.id, skill_name=name
            )
            next_number, published_versions, latest_version = 1, set(), None

        frontmatter = body.frontmatter or {}
        header_version = coerce_version(frontmatter.get("version"))
        declarations, problem = parse_credential_declarations(
            frontmatter.get("credentials")
        )
        credentials = (
            SkillCredentialRequirements.to_publish_preview(_user_provided(declarations))
            if problem is None
            else []
        )
        return SkillPublishPreview(
            credentials=credentials,
            version=SkillCatalogService.resolve_version(
                requested=None,
                frontmatter_version=header_version,
                published_versions=published_versions,
                latest_version=latest_version,
            ),
            header_version=header_version,
            latest_published_version=latest_version,
            package_id=package_id,
            package_id_disambiguated=disambiguated,
            is_republish=existing is not None,
            package_uuid=existing.id if existing is not None else None,
            next_revision_number=next_number,
        )

    @staticmethod
    async def publish(
        session: Session, *, user: User, archive: UploadFile, meta: SkillUploadMeta
    ) -> SkillPackageRevision:
        """Publish an uploaded ``skills/<name>/`` archive as a new revision.

        Raises:
            SkillCatalogError: ``skill_too_large`` (413 before parsing, 422
                after), ``skill_archive_invalid``, ``skill_invalid``,
                ``skill_contains_secrets``, ``package_not_found``,
                ``not_publisher``, ``skill_name_mismatch``,
                ``package_exists``, ``grants_require_users_visibility``,
                ``invalid_grants``, ``invalid_visibility`` and the package-id
                codes of a first publish.
        """
        # 1. The compressed bytes, capped while reading.
        data = await read_capped(archive)

        tmp_root = Path(tempfile.mkdtemp(prefix="skill-upload-"))
        try:
            # 2. Safe extraction into a private temporary directory.
            name = await asyncio.to_thread(safe_extract, data, tmp_root)
            del data
            skill_dir = tmp_root / name

            # 3. The same content refusals as the agent path.
            entry = await asyncio.to_thread(
                parse_skill_dir, skill_dir, rel_path=f"skills/{name}"
            )
            SkillCatalogService.validate_entry(entry)
            visibility = SkillCatalogService._normalise_visibility(meta.visibility)

            # 4. Which package this publishes into.
            if meta.package_uuid is not None:
                existing: SkillPackage | None = _editable_package(
                    session, meta.package_uuid, user
                )
                if existing.name != entry.name:
                    raise _name_mismatch(existing, entry.name)
            else:
                existing = SkillCatalogService._find_publisher_package(
                    session, publisher_user_id=user.id, entry_name=entry.name
                )
                if existing is not None:
                    raise _package_exists(existing)

            lock_key = (
                f"package:{existing.id}"
                if existing is not None
                else f"{user.id}:{entry.name}"
            )
            async with _publish_lock_for(lock_key):
                if existing is None:
                    # A concurrent first publish may have created it while
                    # this one waited for the lock.
                    raced = SkillCatalogService._find_publisher_package(
                        session, publisher_user_id=user.id, entry_name=entry.name
                    )
                    if raced is not None:
                        raise _package_exists(raced)

                # 5. Sharing changes on an existing package need ``maintain``.
                SkillUploadService._check_sharing(
                    session, user=user, existing=existing, visibility=visibility,
                    meta=meta,
                )

                # 6. Every declared slot is filled by each installer.
                credential_specs = SkillCredentialRequirements.build_specs(
                    session, _user_provided(entry.credentials)
                )

                if existing is not None:
                    package, created = existing, False
                else:
                    package, created = SkillCatalogService._resolve_package(
                        session,
                        user=user,
                        source_agent_id=None,
                        entry_name=entry.name,
                        description=entry.description,
                        requested_package_id=None,
                    )

                # 7. The shared publish core. A package created just now is
                #    visible by uuid the moment it commits, so its own
                #    ``package:{id}`` lock is taken too (an existing package's is
                #    already held): a concurrent publish naming that uuid must
                #    not compute the same next revision number. The version
                #    write-back lands in the temporary copy, so the snapshot
                #    carries it.
                package_lock = (
                    _publish_lock_for(f"package:{package.id}")
                    if created
                    else contextlib.nullcontext()
                )
                async with package_lock:
                    revision, gained = await SkillCatalogService._publish_tree(
                        session,
                        user=user,
                        package=package,
                        created=created,
                        skill_dir=skill_dir,
                        entry=entry,
                        version=meta.version,
                        release_notes=meta.release_notes,
                        visibility=visibility,
                        credential_specs=credential_specs,
                        source_agent_id=None,
                        catalog_grants=meta.grants or None,
                    )
        finally:
            await asyncio.to_thread(shutil.rmtree, tmp_root, True)

        logger.info(
            "skill_published_upload package_id=%s revision=%s user_id=%s size=%s",
            package.package_id, revision.revision_number, user.id,
            revision.size_bytes,
        )
        await CatalogEvents.revision_published(
            session, SKILL.wire_kind, package, revision.revision_number
        )
        if gained:
            await CatalogEvents.granted(SKILL.wire_kind, package.id, gained)
        return revision

    @staticmethod
    def _check_sharing(
        session: Session,
        *,
        user: User,
        existing: SkillPackage | None,
        visibility: str | None,
        meta: SkillUploadMeta,
    ) -> None:
        """Step 5: refusals over ``visibility`` and ``grants``. Read-only."""
        changes_visibility = (
            existing is not None
            and visibility is not None
            and visibility != existing.visibility
        )
        if (
            existing is not None
            and (changes_visibility or meta.grants)
            and not SkillCatalogService.user_can_manage(session, existing, user)
        ):
            raise SkillCatalogError(
                "not_publisher",
                "Only a maintainer of this skill can change its visibility or "
                "who it is shared with.",
            )
        if not meta.grants:
            return
        effective_visibility = visibility or (
            existing.visibility
            if existing is not None
            else SkillPackageVisibility.PRIVATE
        )
        if effective_visibility != SkillPackageVisibility.USERS and any(
            grant.access == CatalogAccess.USE for grant in meta.grants
        ):
            raise SkillCatalogError(
                "grants_require_users_visibility",
                "Sharing a skill for use with named people only takes effect "
                "when its visibility is 'users', and this publish would leave "
                f"it '{effective_visibility}'. Set the visibility to 'users', "
                "or publish without the use grants.",
            )
        try:
            SkillCatalogService.validate_catalog_grants(
                session, existing, user, meta.grants
            )
        except CatalogError as exc:
            raise SkillCatalogError(
                "invalid_grants", str(exc), status=exc.http_status
            ) from exc


__all__ = [
    "MAX_ARCHIVE_MEMBERS",
    "SkillUploadService",
    "read_capped",
    "safe_extract",
]
