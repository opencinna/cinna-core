"""
Attachment Materialization Service — turns agent-declared workspace file paths
into durable platform attachments.

An agent declares an attachment by emitting an absolute container path inside a
``<cinna_attach>`` tag (see MessageService._extract_attachments). This service is
the finalize-time worker that, for each declared path:

  1. validates the path is an absolute path rooted at the workspace root
     (``/app/workspace``) and re-asserts the resolved path stays inside the root,
  2. pulls the file bytes from the agent environment (Docker volume read or the
     remote ``GET /workspace/download/{path}`` endpoint),
  3. sniffs the MIME type server-side and enforces the MIME whitelist, the
     per-file size cap, the per-message count / aggregate-byte caps and the owner
     storage quota,
  4. stores the bytes via FileStorageService and creates the FileUpload +
     MessageFile records (``origin='agent'`` / ``source='agent_attachment'``).

Only successfully materialised junctions are returned; rejected paths are
reported via ``rejections`` so the caller can surface a single
``attachment_error`` notice. No partial records are ever created.
"""
import logging
import mimetypes
import os
import uuid
from dataclasses import dataclass, field

from sqlmodel import Session

from app.core.config import settings
from app.models.environments.environment import AgentEnvironment
from app.models.files.file_upload import FileUpload, MessageFile
from app.models.sessions.session import Session as ChatSession, SessionMessage
from app.services.files.attachment_limits import (
    REASON_AGGREGATE_LIMIT,
    REASON_QUOTA_EXCEEDED,
    REASON_TOO_LARGE,
    REASON_TYPE_NOT_ALLOWED,
    validate_attachment_bytes,
)
from app.services.files.file_service import FileService
from app.services.files.file_storage_service import FileStorageService

logger = logging.getLogger(__name__)

# Workspace root inside the agent container. The agent always emits an absolute
# path rooted here; anything else is rejected.
WORKSPACE_ROOT = "/app/workspace"

# Per-message limits (in addition to the per-file size cap and owner quota).
MAX_ATTACHMENTS_PER_MESSAGE = 10
MAX_AGGREGATE_BYTES_PER_MESSAGE = 100 * 1024 * 1024  # 100 MB

# Explicit extension -> MIME fallback for documented previewable types that
# ``mimetypes.guess_type`` returns None / unreliable values for on the target
# environment (notably ``.md``). Each entry MUST resolve to a type inside
# ``settings.allowed_mime_types`` so the attachment actually materialises.
_EXTENSION_MIME_FALLBACK = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".xml": "application/xml",
    ".pdf": "application/pdf",
}


@dataclass
class MaterializedAttachment:
    """A successfully materialised attachment and its event metadata."""

    message_file: MessageFile
    file_upload: FileUpload

    def event_metadata(self) -> dict:
        """Metadata block for the `attachment` streaming event."""
        return {
            "file_id": str(self.file_upload.id),
            "filename": self.file_upload.filename,
            "mime_type": self.file_upload.mime_type,
            "size": self.file_upload.file_size,
            "agent_env_path": self.message_file.agent_env_path,
        }


@dataclass
class MaterializationResult:
    """Outcome of materialising a message's declared attachment paths."""

    # Materialised attachments, keyed by the ORIGINAL absolute path the agent
    # declared (a path that appeared twice maps to the same entry).
    by_path: dict[str, MaterializedAttachment] = field(default_factory=dict)
    # Human-readable rejection reasons (one per rejected/failed path).
    rejections: list[str] = field(default_factory=list)


class AttachmentMaterializationService:
    """Materialise agent-declared workspace files into platform attachments."""

    @staticmethod
    async def materialize_attachments(
        db: Session,
        session: ChatSession,
        message: SessionMessage,
        paths: list[str],
    ) -> MaterializationResult:
        """
        Validate, pull, store and record each declared attachment path.

        Args:
            db: Database session.
            session: The chat session the message belongs to.
            message: The agent message the attachments hang off.
            paths: Absolute container paths (each rooted at ``/app/workspace``).

        Returns:
            MaterializationResult with the materialised junctions (by original
            path) and any rejection reasons. Never raises for per-path failures.
        """
        result = MaterializationResult()
        if not paths:
            return result

        owner_id = session.user_id

        # Resolve the environment + adapter once for the whole message.
        adapter = AttachmentMaterializationService._get_adapter(db, session)
        if adapter is None:
            result.rejections.append(
                "attachment skipped: agent environment unavailable"
            )
            return result

        # Track running storage usage and per-message aggregate as we go, so
        # later attachments in the same message see earlier ones' bytes.
        running_usage = FileService.get_user_storage_usage(
            session=db, user_id=owner_id
        )
        aggregate_bytes = 0
        materialized_count = 0
        # Map resolved-relative-path -> already-materialised attachment so a path
        # declared twice stores once and both tags reference one file_id.
        seen_by_relative: dict[str, MaterializedAttachment] = {}

        for raw_path in paths:
            relative = AttachmentMaterializationService._to_workspace_relative(raw_path)
            if relative is None:
                result.rejections.append(
                    f"attachment skipped: path is not inside {WORKSPACE_ROOT}"
                )
                continue

            # De-dupe: same file declared twice → one record, two cards.
            if relative in seen_by_relative:
                result.by_path[raw_path] = seen_by_relative[relative]
                continue

            if materialized_count >= MAX_ATTACHMENTS_PER_MESSAGE:
                result.rejections.append(
                    f"attachment skipped: max {MAX_ATTACHMENTS_PER_MESSAGE} "
                    "attachments per message reached"
                )
                continue

            pulled = await AttachmentMaterializationService._pull_workspace_bytes(
                adapter, relative
            )
            if pulled is None:
                result.rejections.append(
                    f"attachment skipped: could not read '{os.path.basename(raw_path)}'"
                )
                continue
            content, sniffed_mime = pulled
            size = len(content)

            reject = validate_attachment_bytes(
                size=size,
                mime_type=sniffed_mime,
                max_file_bytes=settings.upload_max_file_size_bytes,
                aggregate_so_far=aggregate_bytes,
                max_aggregate_bytes=MAX_AGGREGATE_BYTES_PER_MESSAGE,
                running_usage=running_usage,
                max_user_storage_bytes=settings.upload_max_user_storage_bytes,
            )
            if reject is not None:
                result.rejections.append(
                    AttachmentMaterializationService._rejection_text(
                        reject, sniffed_mime
                    )
                )
                continue

            try:
                materialized = AttachmentMaterializationService._store_and_record(
                    db=db,
                    owner_id=owner_id,
                    session_id=session.id,
                    message_id=message.id,
                    abs_path=raw_path,
                    content=content,
                    mime_type=sniffed_mime,
                )
            except Exception as e:  # pragma: no cover - storage failure path
                logger.error(
                    "Failed to store agent attachment (size=%d mime=%s): %s",
                    size, sniffed_mime, e, exc_info=True,
                )
                result.rejections.append(
                    f"attachment skipped: storage error for "
                    f"'{os.path.basename(raw_path)}'"
                )
                continue

            running_usage += size
            aggregate_bytes += size
            materialized_count += 1
            seen_by_relative[relative] = materialized
            result.by_path[raw_path] = materialized

            logger.info(
                "Materialised agent attachment file_id=%s filename=%s size=%d mime=%s",
                materialized.file_upload.id,
                materialized.file_upload.filename,
                size,
                sniffed_mime,
            )

        return result

    # ------------------------------------------------------------------
    # Path normalisation / boundary checks
    # ------------------------------------------------------------------

    @staticmethod
    def _to_workspace_relative(abs_path: str) -> str | None:
        """
        Validate an absolute container path and return the workspace-relative
        remainder, or None if the path is invalid / escapes the workspace root.

        Rejects: non-absolute paths, paths not rooted at WORKSPACE_ROOT, and any
        path that — after normalising ``..`` segments — resolves outside the root.
        """
        if not abs_path or not abs_path.startswith("/"):
            logger.debug("Attachment path not absolute: %r", abs_path)
            return None

        # Normalise ``..``/``.`` segments WITHOUT touching the filesystem so a
        # crafted path cannot escape the logical root.
        normalized = os.path.normpath(abs_path)
        root = os.path.normpath(WORKSPACE_ROOT)

        if normalized != root and not normalized.startswith(root + "/"):
            logger.warning(
                "Attachment path escapes workspace root: %r -> %r", abs_path, normalized
            )
            return None

        relative = os.path.relpath(normalized, root)
        if relative == "." or relative.startswith(".."):
            logger.warning("Attachment path resolves to root or outside: %r", abs_path)
            return None
        return relative

    # ------------------------------------------------------------------
    # Byte retrieval
    # ------------------------------------------------------------------

    @staticmethod
    async def _pull_workspace_bytes(
        adapter,
        relative_path: str,
    ) -> tuple[bytes, str] | None:
        """
        Read the file bytes for a workspace-relative path and sniff its MIME.

        Docker adapters expose the *main* workspace as a host volume, so we try
        to read the host Path directly first (fast, no container round-trip;
        boundary-checked inside ``get_local_workspace_file_path``). That
        optimization only covers the workspace volume itself — sub-paths that are
        backed by a *separate* bind mount (notably ``app-data/``, the per-user App
        Data volume mounted at ``/app/workspace/app-data``) do not exist under the
        workspace host dir, so the host read misses them. In that case we fall
        back to the in-container ``GET /workspace/download/{path}`` endpoint via
        ``fetch_workspace_item_with_meta`` (the same path remote adapters use and
        what ``AgentStatusService`` uses to read ``app-data/storage/STATUS.md``),
        which sees every mount the container sees.

        Returns ``(content, mime)`` or None on any failure (missing file,
        boundary rejection, transport error). The remote path retries once.
        """
        # 1. Docker host-volume fast path (main workspace only).
        get_local = getattr(adapter, "get_local_workspace_file_path", None)
        if callable(get_local):
            host_path = get_local(relative_path)
            if host_path is not None:
                try:
                    content = host_path.read_bytes()
                    return content, AttachmentMaterializationService._sniff_mime(relative_path)
                except OSError as e:
                    logger.warning("Failed reading attachment %r: %s", relative_path, e)
                    # Fall through to the container fetch below.
            else:
                # Not present under the workspace host dir — likely a separately
                # mounted sub-volume (e.g. app-data/). Fall back to reading it
                # through the container, which sees that mount.
                logger.debug(
                    "Attachment %r not under workspace host dir; trying container fetch",
                    relative_path,
                )

        # 2. Container/remote fetch over HTTP (works for app-data and remote
        #    adapters). Retry once on transient transport errors.
        fetch = getattr(adapter, "fetch_workspace_item_with_meta", None)
        if not callable(fetch):
            logger.warning(
                "Attachment not found and adapter has no container fetch: %r",
                relative_path,
            )
            return None

        for attempt in (1, 2):
            try:
                meta, stream = await fetch(relative_path)
                if not getattr(meta, "exists", False):
                    logger.warning("Attachment not found: %r", relative_path)
                    return None
                chunks = [chunk async for chunk in stream]
                content = b"".join(chunks)
                # MIME is derived from the basename (same as the Docker path) so
                # it stays untrusted-from-the-agent. The server-supplied
                # Content-Type header is treated as a non-authoritative hint only.
                mime = AttachmentMaterializationService._sniff_mime(relative_path)
                return content, mime
            except ValueError:
                # Invalid path rejected by the env-core boundary check.
                logger.warning("Remote rejected attachment path: %r", relative_path)
                return None
            except Exception as e:
                logger.warning(
                    "Remote attachment fetch failed (attempt %d) %r: %s",
                    attempt, relative_path, e,
                )
                continue
        return None

    @staticmethod
    def _sniff_mime(path: str) -> str:
        """Derive MIME type from the file basename (extension), server-side.

        Falls back to an explicit extension map for documented previewable types
        that ``mimetypes.guess_type`` mishandles on the target env (notably
        ``.md``), then to ``application/octet-stream``.
        """
        guessed, _ = mimetypes.guess_type(path)
        if guessed:
            return guessed
        ext = os.path.splitext(path)[1].lower()
        return _EXTENSION_MIME_FALLBACK.get(ext, "application/octet-stream")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _rejection_text(reason: str, mime: str) -> str:
        """Render a shared validator code into this path's rejection sentence.

        The limits themselves live in ``attachment_limits`` and are shared with
        the channel-attachment path; only the *wording* is local, because the
        two paths report to different audiences (this one to the platform user
        who owns the session, via the ``attachment_error`` notice).
        """
        if reason == REASON_TYPE_NOT_ALLOWED:
            return f"attachment rejected: file type not allowed ({mime})"
        if reason == REASON_TOO_LARGE:
            return (
                "attachment rejected: file exceeds "
                f"{settings.UPLOAD_MAX_FILE_SIZE_MB}MB limit"
            )
        if reason == REASON_AGGREGATE_LIMIT:
            return (
                "attachment rejected: total attachment size per message exceeds "
                f"{MAX_AGGREGATE_BYTES_PER_MESSAGE // (1024 * 1024)}MB"
            )
        if reason == REASON_QUOTA_EXCEEDED:
            return "attachment rejected: storage quota exceeded"
        return f"attachment rejected: {reason}"

    # ------------------------------------------------------------------
    # Storage + records
    # ------------------------------------------------------------------

    @staticmethod
    def _store_and_record(
        db: Session,
        owner_id: uuid.UUID,
        session_id: uuid.UUID,
        message_id: uuid.UUID,
        abs_path: str,
        content: bytes,
        mime_type: str,
    ) -> MaterializedAttachment:
        """Persist bytes to storage and create the FileUpload + MessageFile rows."""
        # Display name = on-disk basename, sanitised. No agent-supplied caption.
        filename = FileStorageService.sanitize_filename(os.path.basename(abs_path))

        file_id = uuid.uuid4()
        file_path = FileStorageService.store_file(
            user_id=str(owner_id),
            file_id=str(file_id),
            filename=filename,
            content=content,
        )

        file_upload = FileUpload(
            id=file_id,
            user_id=owner_id,
            filename=filename,
            file_path=file_path,
            file_size=len(content),
            mime_type=mime_type,
            origin="agent",
            session_id=session_id,
            status="attached",
        )
        db.add(file_upload)

        message_file = MessageFile(
            message_id=message_id,
            file_id=file_id,
            agent_env_path=abs_path,
            source="agent_attachment",
        )
        db.add(message_file)
        db.commit()
        db.refresh(file_upload)
        db.refresh(message_file)

        return MaterializedAttachment(
            message_file=message_file, file_upload=file_upload
        )

    # ------------------------------------------------------------------
    # Adapter resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _get_adapter(db: Session, session: ChatSession):
        """Resolve the environment adapter for a session, or None if unavailable."""
        if session.environment_id is None:
            return None
        environment = db.get(AgentEnvironment, session.environment_id)
        if environment is None:
            return None
        try:
            from app.services.environments.environment_service import EnvironmentService

            lifecycle_manager = EnvironmentService.get_lifecycle_manager()
            return lifecycle_manager.get_adapter(environment)
        except Exception as e:  # pragma: no cover - defensive
            logger.error(
                "Failed to resolve adapter for env %s: %s",
                session.environment_id, e, exc_info=True,
            )
            return None
