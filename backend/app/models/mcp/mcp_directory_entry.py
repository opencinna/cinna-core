"""The core MCP directory: MCP servers an administrator offers every account.

Rows are either typed in by a superuser (``source='manual'``) or imported from
the opencinna index (``source='opencinna'``), which uses the same entry format,
so the two stay one shape. Every row is visible to every account in
``GET /addons/directory`` unless it is banned (``access='banned'``).

Nothing here holds a secret: ``env`` lists the *names* of values a server needs,
and the user supplies them per computer.
"""
import re
import uuid
from datetime import UTC, datetime
from typing import Literal, Optional

import sqlalchemy as sa
from pydantic import field_validator
from sqlmodel import JSON, Column, Field, SQLModel

from app.models.addons.directory import McpEnvVar, McpPackage

McpTransport = Literal["stdio", "http", "sse"]
McpAuth = Literal["none", "oauth", "bearer", "env"]
McpDirectorySource = Literal["manual", "opencinna"]
McpDirectoryAccess = Literal["default", "banned"]

#: Slug of an entry: the opencinna index ``id`` (stable kebab-case) or a name
#: a superuser typed.
MCP_DIRECTORY_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"


class McpDirectoryEntry(SQLModel, table=True):
    __tablename__ = "mcp_directory_entry"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(max_length=64, unique=True, nullable=False)
    title: str = Field(max_length=255)
    description: Optional[str] = Field(default=None, sa_type=sa.Text)
    icon_url: Optional[str] = Field(default=None, max_length=2048)
    categories: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    #: ``stdio`` | ``http`` | ``sse``
    transport: str = Field(max_length=8)
    #: http / sse only, HTTPS only.
    url: Optional[str] = Field(default=None, max_length=2048)
    #: stdio only: :class:`McpPackage` as JSON.
    package: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    #: ``none`` | ``oauth`` | ``bearer`` | ``env``
    auth: str = Field(default="none", max_length=8)
    #: ``[{name, secret, description}]`` — names only, never values.
    env: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    homepage: Optional[str] = Field(default=None, max_length=2048)
    publisher_name: Optional[str] = Field(default=None, max_length=255)
    verified: bool = Field(default=False)
    #: ``manual`` | ``opencinna``
    source: str = Field(default="manual", max_length=16)
    #: The opencinna index ``id`` the row was imported from.
    source_ref: Optional[str] = Field(default=None, max_length=255)
    #: ``default`` | ``banned``
    access: str = Field(
        default="default", max_length=8, sa_column_kwargs={"server_default": "default"}
    )
    access_reason: Optional[str] = Field(default=None, max_length=500)
    access_changed_by_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    access_changed_at: Optional[datetime] = Field(default=None)
    created_by_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class McpDirectoryEntryCreate(SQLModel):
    """A manual entry. Cross-field rules (transport ⇔ url / package, HTTPS
    only, env names) are checked by the service, so create and update share
    one rule set."""

    name: str = Field(max_length=64)
    title: str = Field(min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=20000)
    icon_url: Optional[str] = Field(default=None, max_length=2048)
    categories: list[str] = Field(default_factory=list, max_length=20)
    transport: McpTransport
    url: Optional[str] = Field(default=None, max_length=2048)
    package: Optional[McpPackage] = None
    auth: McpAuth = "none"
    env: list[McpEnvVar] = Field(default_factory=list, max_length=50)
    homepage: Optional[str] = Field(default=None, max_length=2048)
    publisher_name: Optional[str] = Field(default=None, max_length=255)
    verified: bool = False

    @field_validator("name")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not re.fullmatch(MCP_DIRECTORY_NAME_PATTERN, value):
            raise ValueError(
                "A name is 1-64 lowercase letters, digits, '.', '_' or '-', "
                "starting with a letter or digit."
            )
        return value


class McpDirectoryEntryUpdate(SQLModel):
    """Partial update; an omitted field is unchanged, an explicit ``null``
    clears it. ``name`` is immutable (it is the entry's slug)."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=20000)
    icon_url: Optional[str] = Field(default=None, max_length=2048)
    categories: Optional[list[str]] = Field(default=None, max_length=20)
    transport: Optional[McpTransport] = None
    url: Optional[str] = Field(default=None, max_length=2048)
    package: Optional[McpPackage] = None
    auth: Optional[McpAuth] = None
    env: Optional[list[McpEnvVar]] = Field(default=None, max_length=50)
    homepage: Optional[str] = Field(default=None, max_length=2048)
    publisher_name: Optional[str] = Field(default=None, max_length=255)
    verified: Optional[bool] = None


class McpDirectoryAccessUpdate(SQLModel):
    access: McpDirectoryAccess
    reason: Optional[str] = Field(default=None, max_length=500)


class McpDirectoryEntryPublic(SQLModel):
    id: uuid.UUID
    name: str
    title: str
    description: Optional[str] = None
    icon_url: Optional[str] = None
    categories: list[str] = Field(default_factory=list)
    transport: str
    url: Optional[str] = None
    package: Optional[McpPackage] = None
    auth: str
    env: list[McpEnvVar] = Field(default_factory=list)
    homepage: Optional[str] = None
    publisher_name: Optional[str] = None
    verified: bool
    source: str
    source_ref: Optional[str] = None
    access: str
    access_reason: Optional[str] = None
    access_changed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class McpDirectoryEntriesPublic(SQLModel):
    data: list[McpDirectoryEntryPublic]
    count: int


class McpDirectoryImportSkipped(SQLModel):
    #: The index entry's ``id`` (or its position when it has none).
    id: str
    #: ``no_remote`` | ``name_taken`` | ``invalid_entry`` | ``duplicate``
    reason: str


class McpDirectoryImportResult(SQLModel):
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: list[McpDirectoryImportSkipped] = Field(default_factory=list)
    #: Names of imported rows the index no longer lists. Never deleted.
    stale: list[str] = Field(default_factory=list)
