import uuid
from datetime import datetime, UTC
from sqlmodel import Field, Relationship, SQLModel, Column, Text

from app.models.users.user import User


# Shared properties for SSH keys
class SSHKeyBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)


# Properties to receive on SSH key generation
class SSHKeyGenerate(SQLModel):
    name: str = Field(min_length=1, max_length=255)


# Properties to receive on SSH key import
class SSHKeyImport(SSHKeyBase):
    public_key: str
    private_key: str
    passphrase: str | None = None


# Database model
class UserSSHKey(SSHKeyBase, table=True):
    __tablename__ = "user_ssh_keys"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    public_key: str = Field(sa_column=Column(Text, nullable=False))
    private_key_encrypted: str = Field(sa_column=Column(Text, nullable=False))
    passphrase_encrypted: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    fingerprint: str = Field(max_length=255, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Relationships
    owner: User | None = Relationship()


# Properties to return via API (without sensitive data)
class SSHKeyPublic(SSHKeyBase):
    id: uuid.UUID
    public_key: str
    fingerprint: str
    created_at: datetime
    updated_at: datetime


# Properties to return via API with list of keys
class SSHKeysPublic(SQLModel):
    data: list[SSHKeyPublic]
    count: int


# Properties for updating SSH key (only name can be updated)
class SSHKeyUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
