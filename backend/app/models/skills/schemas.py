"""Wire shapes for the skills catalog (no tables).

Everything a client needs to browse the catalog, read a revision, publish a
skill from an agent and install one into another agent. Kept separate from the
table modules so the API contract can grow projections (publisher display name,
install count, "which of my agents already have this") that no column backs.
"""
import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel


class SkillPackageRevisionPublic(SQLModel):
    """One published revision of a skill package.

    Carries every field the package-detail revision rows render: ``version``,
    ``published_at``, ``size_bytes``, ``release_notes`` and ``content_hash``.
    ``frontmatter`` rides along because the catalog card shows the skill's own
    description, which lives there and not on the package when a publisher has
    edited the package blurb.
    """

    id: uuid.UUID
    package_id: uuid.UUID
    revision_number: int
    version: str | None = None
    frontmatter: dict = {}
    content_hash: str = ""
    size_bytes: int = 0
    release_notes: str | None = None
    published_by_user_id: uuid.UUID | None = None
    published_at: datetime


class SkillPackagePublic(SQLModel):
    """Identity of a catalog skill package."""

    id: uuid.UUID
    package_id: str
    name: str
    display_name: str
    description: str | None = None
    publisher_user_id: uuid.UUID | None = None
    publisher_name: str | None = None
    publisher_email: str | None = None
    publisher_email_confirmed: bool = False
    source_agent_id: uuid.UUID | None = None
    latest_revision_id: uuid.UUID | None = None
    latest_revision_number: int | None = None
    latest_version: str | None = None
    visibility: str
    is_listed: bool
    created_at: datetime
    updated_at: datetime


class SkillPackageEntry(SkillPackagePublic):
    """A catalog row, resolved for the calling user."""

    latest_revision: SkillPackageRevisionPublic | None = None
    #: Distinct consumer installs across the instance. Excludes the publisher's
    #: own agents, so a publisher dogfooding their skill does not inflate it.
    install_count: int = 0
    #: The caller's own agents that already carry this package. Ids only — a
    #: grid of cards cannot afford a name lookup per card (§ UI spec gap 7).
    installed_in_agent_ids: list[uuid.UUID] = []
    #: Whether the caller may edit this package (rename, re-describe, change
    #: visibility). A capability reply, so the client never has to reproduce
    #: the publisher rule — delisting is a separate, admin-only verb.
    can_manage: bool = False


class SkillPackagesPublic(SQLModel):
    """List response for the skills catalog."""

    data: list[SkillPackageEntry]
    count: int


class SkillPackageDetailPublic(SkillPackageEntry):
    """A package plus its full revision history, newest first."""

    revisions: list[SkillPackageRevisionPublic] = []


class SkillPackageUpdate(SQLModel):
    """Publisher-editable fields on a package."""

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    visibility: str | None = None
    is_listed: bool | None = None


class SkillPublishRequest(SQLModel):
    """Body of ``POST /agents/{agent_id}/skills/{name}/publish``."""

    version: str | None = Field(default=None, max_length=64)
    release_notes: str | None = None
    #: ``private`` (default) or ``public``. Only honoured on the first publish
    #: and on an explicit change — a re-publish that omits it leaves the
    #: package's current visibility alone.
    visibility: str | None = None
    #: Reverse-DNS id for the FIRST publish of a skill. On a re-publish it may
    #: only repeat the package's existing id — a mismatch is refused with
    #: ``package_id_immutable`` (409) rather than silently ignored, because
    #: every install and every container manifest references that id.
    package_id: str | None = Field(default=None, max_length=255)


class SkillInstallRequest(SQLModel):
    """Body of ``POST /agents/{agent_id}/skills/install``."""

    package_id: uuid.UUID
    #: Defaults to the package's latest revision.
    revision_number: int | None = None
    conversation_mode: bool = True
    building_mode: bool = True


class SkillRevisionContentPublic(SQLModel):
    """The ``SKILL.md`` of one published revision — the catalog preview."""

    package_id: uuid.UUID
    revision_number: int
    name: str
    content: str
    truncated: bool = False
