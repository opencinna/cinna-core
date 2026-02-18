import uuid
from datetime import datetime
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
    __tablename__ = "mail_server_config"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    encrypted_password: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# Properties to return via API (password redacted)
class MailServerConfigPublic(MailServerConfigBase):
    id: uuid.UUID
    user_id: uuid.UUID
    has_password: bool = True
    created_at: datetime
    updated_at: datetime


class MailServerConfigsPublic(SQLModel):
    data: list[MailServerConfigPublic]
    count: int
