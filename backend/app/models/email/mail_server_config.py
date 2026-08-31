import uuid
from datetime import datetime, UTC
from enum import Enum

from sqlmodel import Field, SQLModel, Column, Text


class MailServerType(str, Enum):
    IMAP = "imap"
    SMTP = "smtp"


class EncryptionType(str, Enum):
    SSL = "ssl"
    TLS = "tls"
    STARTTLS = "starttls"
    NONE = "none"


# Shared properties
class MailServerConfigBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    server_type: MailServerType
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    encryption_type: EncryptionType = Field(default=EncryptionType.SSL)
    username: str = Field(min_length=1, max_length=255)


# Properties to receive on creation
class MailServerConfigCreate(MailServerConfigBase):
    password: str = Field(min_length=1)


# Properties to receive on update (all optional)
class MailServerConfigUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    encryption_type: EncryptionType | None = None
    username: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1)


# Database model
class MailServerConfig(MailServerConfigBase, table=True):
    """A server-owned IMAP/SMTP endpoint, referenced by email channels.

    Server-scoped, not user-scoped: a mail server is infrastructure an admin
    configures once, the way a Google Chat channel's service account is. Email
    ``ServerChannel`` rows point at one of these by id in their ``config``;
    nothing else may reference them, and only superusers may see or edit them.
    """

    __tablename__ = "mail_server_config"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    encrypted_password: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Properties to return via API (password redacted)
class MailServerConfigPublic(MailServerConfigBase):
    id: uuid.UUID
    has_password: bool = True
    created_at: datetime
    updated_at: datetime


class MailServerConfigsPublic(SQLModel):
    data: list[MailServerConfigPublic]
    count: int


class MailServerChannelUsage(SQLModel):
    """One channel that references a mail server, and in which role."""

    channel_id: uuid.UUID
    channel_name: str
    #: ``"incoming"`` (IMAP) or ``"outgoing"`` (SMTP).
    role: str


class MailServerDeletionImpact(SQLModel):
    """Blast radius of deleting a mail server.

    A channel references a mail server as a plain id inside
    ``ServerChannel.config`` — a JSON column, with no foreign key behind it.
    Nothing at the database level notices the delete: the row goes away and the
    channel is left holding an id that resolves to nothing. Nothing is nulled,
    nothing errors, and the symptom surfaces later as mail that stops arriving.
    Deletion is therefore **blocked** (HTTP 409) whenever ``channel_usages`` is
    non-empty.
    There is no ``force``: unlike a credential, nothing legitimate is served by
    breaking a live channel, and detaching the server from the channel first is
    a one-click action.
    """

    channel_usages: list[MailServerChannelUsage] = []
