"""Wire shapes of the unified catalog (``/api/v1/catalog/items``, README §5.1).

One ``CatalogItem`` per agent bundle (cloud or desktop) or skill package,
keyed on the item's uuid. Field names are a contract shared with Cinna
Desktop; do not rename them. Deviations from §5.1 (plan §0.3): ``latest.
published_by`` is ``{user_id, display_name} | null``; ``name`` is the display
name for a cloud bundle, the slug for a desktop agent and the folder name for
a skill; ``skills[]`` entries also carry ``package_uuid``; ``?scope=all``.
"""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import StringConstraints
from sqlmodel import Field, SQLModel

from app.models.catalog.catalog_access import (
    CatalogAccessLevel,
    CatalogGrantIn,
    CatalogMaintainer,
    CatalogPerson,
)
from app.models.library.git_repo import KIT_SLUG_PATTERN

CatalogKind = Literal["agent", "skill"]
CatalogRunsOn = Literal["cloud", "desktop"]
CatalogVisibility = Literal["private", "users", "public"]


class CatalogLatest(SQLModel):
    """The item's newest version."""

    revision_number: int
    version: str | None = None
    published_at: datetime
    published_by: CatalogPerson | None = None
    release_notes: str | None = None


class CatalogInstall(SQLModel):
    """One of the caller's installs of the item. C2 reports cloud installs
    only: an ``Agent`` built from a bundle (``install_id`` = the agent) or a
    skill link on one of the caller's agents (``install_id`` = that agent)."""

    where: Literal["cloud", "local"]
    install_id: UUID | None = None
    device_id: str | None = None
    host_manifest_id: str | None = None
    revision_number: int | None = None


class CatalogItemSkill(SQLModel):
    """A skill an agent comes with: authored in its ``skills/`` or pinned from
    the catalog."""

    name: str
    description: str | None = None
    source: Literal["authored", "catalog"]
    package_id: str | None = None
    package_uuid: UUID | None = None


class CatalogDesktop(SQLModel):
    """Where Cinna Desktop gets a desktop agent."""

    repo_id: UUID
    clone_url: str
    default_branch: str
    head_sha: str | None = None
    manifest_id: UUID
    slug: str
    engine: str | None = None
    contract_version: str | None = None


class CatalogSkillContent(SQLModel):
    """A skill package's latest content, as its archive."""

    frontmatter: dict = {}
    archive_sha256: str | None = None
    size_bytes: int = 0


class CatalogItem(SQLModel):
    kind: CatalogKind
    #: Agents only.
    runs_on: CatalogRunsOn | None = None
    uuid: UUID
    #: ``bundle_id`` or ``package_id``.
    id: str
    name: str
    display_name: str
    description: str | None = None
    publisher: CatalogPerson | None = None
    maintainers: list[CatalogMaintainer] = []
    access: CatalogAccessLevel
    visibility: str
    is_listed: bool
    latest: CatalogLatest | None = None
    install_count: int = 0
    my_installs: list[CatalogInstall] = []
    required_credential_specs: list[dict] = []
    #: Agents only.
    skills: list[CatalogItemSkill] | None = None
    #: Desktop agents only.
    desktop: CatalogDesktop | None = None
    #: Skills only.
    skill: CatalogSkillContent | None = None


class CatalogRevision(SQLModel):
    revision_number: int
    version: str | None = None
    published_at: datetime
    published_by: CatalogPerson | None = None
    release_notes: str | None = None
    #: ``publish`` (cloud bundle), ``share`` (desktop agent) or ``skill``.
    origin: str
    git_commit_sha: str | None = None


class CatalogItemDetail(CatalogItem):
    """``GET /catalog/items/{uuid}``: the item and its versions, newest first."""

    revisions: list[CatalogRevision] = []


class CatalogItemsPublic(SQLModel):
    data: list[CatalogItem]
    count: int


class CatalogItemUpdate(SQLModel):
    """Body of ``PATCH /catalog/items/{uuid}`` (maintainer or superuser)."""

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    visibility: CatalogVisibility | None = None
    is_listed: bool | None = None


class DesktopAgentCreate(SQLModel):
    """Body of ``POST /catalog/desktop-agents``."""

    # sqlmodel drops ``regex=`` silently, so the pattern rides on the type.
    slug: Annotated[str, StringConstraints(pattern=KIT_SLUG_PATTERN)]
    display_name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=10_000)
    #: The ``id`` of the agent's ``cinna-agent.json``; bound for good.
    manifest_id: UUID
    visibility: CatalogVisibility = "private"
    #: Who else may reach it; the creator is always ``maintain``.
    grants: list[CatalogGrantIn] = []
