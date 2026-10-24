"""The core MCP directory: superuser CRUD, ban / unban, opencinna index import.

Every write bumps the addons directory version in the same transaction, so
``GET /addons/directory`` stops serving its cached index for the change.

The opencinna index is the public MCP server list Cinna Desktop ships as its
"Cinna Official" registry. Rows imported from it are ``source='opencinna'``;
an import overwrites the index-owned fields of those rows, never touches a
manual row (a manual row with the same name blocks the import entry), never
changes a ban, and never deletes: an imported row the index no longer lists is
reported ``stale`` for an administrator to decide on.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import settings
from app.models.addons.directory import (
    DirectoryEntry,
    DirectoryMcpInstall,
    DirectoryOrigin,
    DirectoryPublisher,
    DirectoryRef,
    McpConnectDescriptor,
    McpEnvVar,
    McpPackage,
)
from app.models.mcp.mcp_directory_entry import (
    MCP_DIRECTORY_NAME_PATTERN,
    McpDirectoryAccessUpdate,
    McpDirectoryEntriesPublic,
    McpDirectoryEntry,
    McpDirectoryEntryCreate,
    McpDirectoryEntryPublic,
    McpDirectoryEntryUpdate,
    McpDirectoryImportResult,
    McpDirectoryImportSkipped,
)
from app.models.users.user import User
from app.services.addons.directory_version import AddonsDirectoryVersion
from app.services.addons.summary import summary_line

logger = logging.getLogger(__name__)

ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
INDEX_TIMEOUT_SECONDS = 10.0
INDEX_MAX_BYTES = 1024 * 1024
#: Fields an import owns on a ``source='opencinna'`` row.
_INDEX_FIELDS = (
    "title",
    "description",
    "homepage",
    "transport",
    "url",
    "package",
    "auth",
    "env",
    "publisher_name",
    "verified",
    "source_ref",
)
#: Columns an update may change but never clear (explicit ``null`` → 422).
_NOT_NULL_FIELDS = ("title", "transport", "auth", "categories", "env", "verified")
INDEX_DEADLINE_SECONDS = 20.0
_ENV_ITEM_KEYS = frozenset({"name", "secret", "description"})
_INDEX_REMOTE_TRANSPORTS = {"streamable-http": "http", "sse": "sse"}


class McpDirectoryError(Exception):
    """A refusal with a machine code; the route maps it to ``status``."""

    def __init__(self, code: str, message: str, status: int = 422):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


def _is_https(url: str | None) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(parts.netloc)


def _now() -> datetime:
    return datetime.now(UTC)


class McpDirectoryService:
    # ── Read ───────────────────────────────────────────────────────────

    @staticmethod
    def list_entries(session: Session) -> McpDirectoryEntriesPublic:
        """Every entry, banned ones included (the admin view)."""
        rows = session.exec(
            select(McpDirectoryEntry).order_by(
                func.lower(McpDirectoryEntry.title), McpDirectoryEntry.id
            )
        ).all()
        return McpDirectoryEntriesPublic(
            data=[McpDirectoryService.to_public(r) for r in rows], count=len(rows)
        )

    @staticmethod
    def visible_entries(session: Session) -> list[McpDirectoryEntry]:
        """What every account sees: everything not banned."""
        return list(
            session.exec(
                select(McpDirectoryEntry).where(McpDirectoryEntry.access != "banned")
            ).all()
        )

    @staticmethod
    def get_entry(session: Session, entry_id: uuid.UUID) -> McpDirectoryEntry:
        row = session.get(McpDirectoryEntry, entry_id)
        if row is None:
            raise McpDirectoryError("entry_not_found", "No such MCP directory entry.", 404)
        return row

    # ── Write ──────────────────────────────────────────────────────────

    @staticmethod
    def create_entry(
        session: Session, data: McpDirectoryEntryCreate, user: User
    ) -> McpDirectoryEntry:
        fields = data.model_dump(mode="json")
        McpDirectoryService._validate(fields)
        McpDirectoryService._assert_name_free(session, data.name)
        row = McpDirectoryEntry(
            **fields, source="manual", created_by_id=user.id
        )
        session.add(row)
        AddonsDirectoryVersion.bump(session)
        try:
            session.commit()
        except IntegrityError as exc:
            # Two creates of one name raced past the read above.
            session.rollback()
            raise McpDirectoryError(
                "name_taken", f"An MCP directory entry named '{data.name}' already exists.", 409
            ) from exc
        session.refresh(row)
        logger.info("mcp_directory_created name=%s by=%s", row.name, user.id)
        return row

    @staticmethod
    def update_entry(
        session: Session, entry_id: uuid.UUID, data: McpDirectoryEntryUpdate
    ) -> McpDirectoryEntry:
        row = McpDirectoryService.get_entry(session, entry_id)
        changes = data.model_dump(mode="json", exclude_unset=True)
        cleared = sorted(k for k in _NOT_NULL_FIELDS if k in changes and changes[k] is None)
        if cleared:
            raise McpDirectoryError(
                "invalid_entry", f"{', '.join(cleared)} cannot be null.", 422
            )
        merged = McpDirectoryService._fields_of(row) | changes
        # Switching transport drops the other transport's half, so an edit
        # from http to stdio does not have to null the url explicitly.
        if "transport" in changes:
            if merged["transport"] == "stdio" and "url" not in changes:
                merged["url"] = None
            if merged["transport"] != "stdio" and "package" not in changes:
                merged["package"] = None
        McpDirectoryService._validate(merged)
        for key, value in merged.items():
            setattr(row, key, value)
        row.updated_at = _now()
        session.add(row)
        AddonsDirectoryVersion.bump(session)
        session.commit()
        session.refresh(row)
        return row

    @staticmethod
    def delete_entry(session: Session, entry_id: uuid.UUID) -> None:
        row = McpDirectoryService.get_entry(session, entry_id)
        name = row.name
        session.delete(row)
        AddonsDirectoryVersion.bump(session)
        session.commit()
        logger.info("mcp_directory_deleted name=%s", name)

    @staticmethod
    def set_access(
        session: Session,
        entry_id: uuid.UUID,
        data: McpDirectoryAccessUpdate,
        user: User,
    ) -> McpDirectoryEntry:
        row = McpDirectoryService.get_entry(session, entry_id)
        row.access = data.access
        row.access_reason = (data.reason or None) if data.access == "banned" else None
        row.access_changed_by_id = user.id
        row.access_changed_at = _now()
        row.updated_at = _now()
        session.add(row)
        AddonsDirectoryVersion.bump(session)
        session.commit()
        session.refresh(row)
        logger.info(
            "mcp_directory_access name=%s access=%s by=%s", row.name, row.access, user.id
        )
        return row

    # ── Import ─────────────────────────────────────────────────────────

    @staticmethod
    def fetch_index(url: str | None = None) -> dict:
        """GET the opencinna index: HTTPS only, 10 s, at most 1 MB."""
        url = url or settings.MCP_DIRECTORY_INDEX_URL
        if not _is_https(url):
            raise McpDirectoryError(
                "index_url_invalid", "The opencinna index URL must be HTTPS.", 500
            )
        try:
            with httpx.Client(timeout=INDEX_TIMEOUT_SECONDS, follow_redirects=False) as client:
                with client.stream("GET", url, headers={"Accept": "application/json"}) as resp:
                    if resp.status_code != 200:
                        raise McpDirectoryError(
                            "index_fetch_failed",
                            f"The opencinna index answered HTTP {resp.status_code}.",
                            502,
                        )
                    body = bytearray()
                    deadline = time.monotonic() + INDEX_DEADLINE_SECONDS
                    for chunk in resp.iter_bytes():
                        if time.monotonic() > deadline:
                            # The per-read timeout alone lets a slow trickle
                            # hold the request open indefinitely.
                            raise McpDirectoryError(
                                "index_fetch_failed",
                                "The opencinna index took longer than 20 s to download.",
                                502,
                            )
                        body.extend(chunk)
                        if len(body) > INDEX_MAX_BYTES:
                            raise McpDirectoryError(
                                "index_too_large",
                                "The opencinna index is larger than 1 MB.",
                                502,
                            )
        except httpx.HTTPError as exc:
            raise McpDirectoryError(
                "index_fetch_failed", f"Could not fetch the opencinna index: {exc}", 502
            ) from exc
        try:
            payload = json.loads(bytes(body))
        except ValueError as exc:
            raise McpDirectoryError(
                "index_invalid", "The opencinna index is not valid JSON.", 502
            ) from exc
        if not isinstance(payload, dict):
            raise McpDirectoryError("index_invalid", "The opencinna index is not an object.", 502)
        return payload

    @staticmethod
    def import_index(session: Session, user: User) -> McpDirectoryImportResult:
        return McpDirectoryService.apply_index(
            session, McpDirectoryService.fetch_index(), user
        )

    @staticmethod
    def apply_index(
        session: Session, payload: dict, user: User
    ) -> McpDirectoryImportResult:
        if payload.get("version") != 1:
            raise McpDirectoryError(
                "index_version_unsupported",
                f"opencinna index version {payload.get('version')!r} is not supported (expected 1).",
                422,
            )
        servers = payload.get("servers")
        if not isinstance(servers, list):
            raise McpDirectoryError("index_invalid", "The index has no servers list.", 502)

        result = McpDirectoryImportResult()
        existing = {
            row.name: row for row in session.exec(select(McpDirectoryEntry)).all()
        }
        seen: set[str] = set()
        #: Index ids that still map to an importable entry; stored opencinna
        #: rows outside this set are ``stale``.
        listed: set[str] = set()
        changed = False
        for position, server in enumerate(servers):
            if not isinstance(server, dict):
                result.skipped.append(
                    McpDirectoryImportSkipped(id=f"#{position}", reason="invalid_entry")
                )
                continue
            ref = server.get("id")
            label = ref if isinstance(ref, str) and ref else f"#{position}"
            if not isinstance(ref, str) or not re.fullmatch(MCP_DIRECTORY_NAME_PATTERN, ref):
                result.skipped.append(McpDirectoryImportSkipped(id=label, reason="invalid_entry"))
                continue
            if ref in seen:
                result.skipped.append(McpDirectoryImportSkipped(id=ref, reason="duplicate"))
                continue
            seen.add(ref)
            fields = McpDirectoryService._map_index_server(server)
            if fields is None:
                # A stored row whose entry lost its https remote is not listed
                # any more: it falls through to ``stale`` below.
                result.skipped.append(McpDirectoryImportSkipped(id=ref, reason="no_remote"))
                continue
            row = existing.get(ref)
            if row is not None and row.source != "opencinna":
                result.skipped.append(McpDirectoryImportSkipped(id=ref, reason="name_taken"))
                continue
            if row is None:
                # ON CONFLICT instead of a savepoint: a manual create of the
                # same name racing this import loses one row, not the import.
                inserted = session.execute(
                    pg_insert(McpDirectoryEntry.__table__)
                    .values(
                        id=uuid.uuid4(),
                        name=ref,
                        source="opencinna",
                        created_by_id=user.id,
                        icon_url=None,
                        categories=[],
                        access="default",
                        created_at=_now(),
                        updated_at=_now(),
                        **fields,
                    )
                    .on_conflict_do_nothing(index_elements=["name"])
                    .returning(McpDirectoryEntry.__table__.c.id)
                ).first()
                if inserted is None:
                    result.skipped.append(McpDirectoryImportSkipped(id=ref, reason="name_taken"))
                    continue
                listed.add(ref)
                result.created += 1
                changed = True
                continue
            listed.add(ref)
            current = {key: getattr(row, key) for key in _INDEX_FIELDS}
            if current == fields:
                result.unchanged += 1
                continue
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = _now()
            session.add(row)
            result.updated += 1
            changed = True

        result.stale = sorted(
            name
            for name, row in existing.items()
            if row.source == "opencinna" and name not in listed
        )
        if changed:
            AddonsDirectoryVersion.bump(session)
        session.commit()
        logger.info(
            "mcp_directory_import created=%s updated=%s unchanged=%s skipped=%s stale=%s by=%s",
            result.created,
            result.updated,
            result.unchanged,
            len(result.skipped),
            len(result.stale),
            user.id,
        )
        return result

    @staticmethod
    def _map_index_server(server: dict) -> dict[str, Any] | None:
        """One opencinna server → the index-owned fields; None without an https remote."""
        remote = None
        for candidate in server.get("remotes") or []:
            if (
                isinstance(candidate, dict)
                and candidate.get("type") in _INDEX_REMOTE_TRANSPORTS
                and _is_https(candidate.get("url"))
            ):
                remote = candidate
                break
        if remote is None:
            return None

        def text(key: str, limit: int) -> str | None:
            value = server.get(key)
            if not isinstance(value, str):
                return None
            return value.strip()[:limit] or None

        website = server.get("websiteUrl")
        return {
            "title": text("title", 255) or text("name", 255) or server["id"],
            "description": text("description", 20000),
            "homepage": website[:2048] if _is_https(website) else None,
            "transport": _INDEX_REMOTE_TRANSPORTS[remote["type"]],
            "url": remote["url"][:2048],
            "package": None,
            "auth": "oauth" if remote.get("requiresAuth") is True else "none",
            "env": [],
            "publisher_name": "Cinna",
            "verified": True,
            "source_ref": server["id"],
        }

    # ── Validation ─────────────────────────────────────────────────────

    @staticmethod
    def _fields_of(row: McpDirectoryEntry) -> dict[str, Any]:
        return {
            key: getattr(row, key)
            for key in McpDirectoryEntryCreate.model_fields
            if key != "name"
        }

    @staticmethod
    def _assert_name_free(session: Session, name: str) -> None:
        taken = session.exec(
            select(McpDirectoryEntry.id).where(McpDirectoryEntry.name == name)
        ).first()
        if taken is not None:
            raise McpDirectoryError(
                "name_taken", f"An MCP directory entry named '{name}' already exists.", 409
            )

    @staticmethod
    def _validate(fields: dict[str, Any]) -> None:
        """The one rule set for create and update (on the merged result)."""

        def refuse(message: str) -> None:
            raise McpDirectoryError("invalid_entry", message, 422)

        transport = fields.get("transport")
        if transport in ("http", "sse"):
            if not fields.get("url"):
                refuse(f"A {transport} server needs a url.")
            if not _is_https(fields["url"]):
                refuse("The url must be an https:// address.")
            if fields.get("package"):
                refuse(f"A {transport} server takes a url, not a package.")
        elif transport == "stdio":
            if not fields.get("package"):
                refuse("A stdio server needs a package (registry, name).")
            if fields.get("url"):
                refuse("A stdio server takes a package, not a url.")
        else:
            refuse("transport must be stdio, http or sse.")
        for key in ("homepage", "icon_url"):
            if fields.get(key) and not _is_https(fields[key]):
                refuse(f"{key} must be an https:// address.")
        names: set[str] = set()
        for item in fields.get("env") or []:
            name = item.get("name") if isinstance(item, dict) else None
            if isinstance(item, dict) and set(item) - _ENV_ITEM_KEYS:
                # Never echo the value: it is probably the secret itself.
                refuse(
                    f"Environment item {name!r} may only carry name, secret and "
                    "description. Values (secrets) are supplied by each user, "
                    "never stored in the directory."
                )
            if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
                refuse(
                    f"Environment name {name!r} must be upper case letters, digits "
                    "and '_', not starting with a digit."
                )
            if name in names:
                refuse(f"Environment name {name} is listed twice.")
            names.add(name)
        categories = fields.get("categories") or []
        if any(not isinstance(c, str) or not c.strip() or len(c) > 64 for c in categories):
            refuse("Each category is a non-empty label of at most 64 characters.")

    # ── Projections ────────────────────────────────────────────────────

    @staticmethod
    def to_public(row: McpDirectoryEntry) -> McpDirectoryEntryPublic:
        return McpDirectoryEntryPublic.model_validate(row, from_attributes=True)

    @staticmethod
    def descriptor(row: McpDirectoryEntry) -> McpConnectDescriptor:
        return McpConnectDescriptor(
            transport=row.transport,
            url=row.url,
            package=McpPackage.model_validate(row.package) if row.package else None,
            auth=row.auth,
            env=[McpEnvVar.model_validate(e) for e in row.env or []],
        )

    @staticmethod
    def to_directory_entry(row: McpDirectoryEntry) -> DirectoryEntry:
        categories = [c for c in row.categories or [] if isinstance(c, str)]
        package = row.package or {}
        return DirectoryEntry(
            ref=DirectoryRef(source="core-mcp-directory", id=str(row.id)),
            kind="mcp",
            name=row.name,
            title=row.title,
            summary=summary_line(row.description),
            description=row.description,
            category=categories[0] if categories else None,
            tags=categories,
            icon_url=row.icon_url,
            publisher=DirectoryPublisher(name=row.publisher_name, verified=row.verified),
            origin=DirectoryOrigin(
                label="opencinna index" if row.source == "opencinna" else "MCP directory",
                kind="directory",
                repository_url=None,
                homepage=row.homepage,
            ),
            version=package.get("version") if row.transport == "stdio" else None,
            digest=None,
            components=None,
            runs_code=row.transport == "stdio",
            status="ready",
            unsupported_reason=None,
            install=DirectoryMcpInstall(mcp=McpDirectoryService.descriptor(row)),
        )

