"""Brand theme storage and API shapes.

A server has at most one **published** brand theme (README B1). Every upload
that passes the validator becomes a ``draft`` row; publishing one demotes the
current published row back to ``draft``. A new look is a new upload; the only
content change a row ever sees is at publish, when re-validation stores what
the *current* sanitiser and normaliser produce for the same package, so a
published revision always describes bytes validated by today's rules.

Assets are content-addressed (``sha256`` of the stored bytes; SVGs are the
sanitised bytes) and shared between drafts that use the same file.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField
from sqlalchemy import Column, Index, Integer, LargeBinary, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

BRAND_THEME_STATUS_DRAFT = "draft"
BRAND_THEME_STATUS_PUBLISHED = "published"

# Upper bound on stored drafts. A package can carry up to ~8 MB of assets, so
# the cap keeps a superuser's experiments from growing the table without end;
# deleting a draft frees its slot (and any asset no other draft uses).
BRAND_THEME_MAX_DRAFTS = 20

# Postgres sequence the publish revision is drawn from. A sequence rather
# than ``max(revision) + 1``: deleting every row must not let a later publish
# reuse a revision a desktop still holds as its ETag (it would get a 304 and
# keep the old theme).
BRAND_THEME_REVISION_SEQUENCE = "brand_theme_revision_seq"


def _now() -> datetime:
    return datetime.now(UTC)


class BrandTheme(SQLModel, table=True):
    __tablename__ = "brand_theme"
    __table_args__ = (
        # At most one published theme per server (B1).
        Index(
            "uq_brand_theme_one_published",
            "status",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    status: str = Field(default=BRAND_THEME_STATUS_DRAFT, max_length=16, nullable=False)
    # Denormalised from theme_json for listing.
    name: str = Field(max_length=64, nullable=False)
    # The validator's normalised theme.json: colors as #rrggbb, asset values
    # still package paths ("assets/logo-dark.svg").
    theme_json: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    # Package path → sha256 of the stored bytes, for every referenced path.
    asset_map: dict[str, str] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}")
    )
    # Drawn from BRAND_THEME_REVISION_SEQUENCE on every publish; None until
    # the draft is first published.
    revision: int | None = Field(default=None)
    created_by_id: uuid.UUID | None = Field(default=None, foreign_key="user.id", ondelete="SET NULL")
    published_by_id: uuid.UUID | None = Field(default=None, foreign_key="user.id", ondelete="SET NULL")
    published_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class BrandThemeAsset(SQLModel, table=True):
    """Content-addressed, immutable asset bytes."""

    __tablename__ = "brand_theme_asset"

    sha256: str = Field(primary_key=True, min_length=64, max_length=64)
    mime: str = Field(max_length=32, nullable=False)
    # Column ``bytes`` (contract name); attribute ``size`` because a field
    # named ``bytes`` would shadow the type of ``data`` below.
    size: int = Field(sa_column=Column("bytes", Integer, nullable=False))
    width: int | None = Field(default=None)
    height: int | None = Field(default=None)
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    created_at: datetime = Field(default_factory=_now)


# ── Admin API shapes ───────────────────────────────────────────────────

class BrandThemeIssue(SQLModel):
    """One validator finding. ``path`` is dotted (``modes.dark.text.muted``),
    ``code`` is stable, ``message`` says what to change."""

    path: str
    code: str
    message: str


class BrandThemeUploadResult(SQLModel):
    """``id`` is null when the package was refused; ``errors`` then says why.
    ``warnings`` never block (what the SVG sanitiser removed, unreferenced files)."""

    id: uuid.UUID | None = None
    errors: list[BrandThemeIssue] = []
    warnings: list[BrandThemeIssue] = []


class BrandThemePublishErrors(SQLModel):
    """409 body when a draft no longer passes the (possibly tightened) rules."""

    errors: list[BrandThemeIssue]


class BrandThemeDraftPublic(SQLModel):
    id: uuid.UUID
    status: str
    name: str
    revision: int | None
    created_at: datetime
    created_by_email: str | None = None
    published_at: datetime | None = None
    published_by_email: str | None = None
    # The served projection of this draft (asset URLs included), so the admin
    # page previews exactly what desktops would receive.
    document: dict[str, Any]


class BrandThemeDraftsPublic(SQLModel):
    data: list[BrandThemeDraftPublic]
    count: int
    max_drafts: int = BRAND_THEME_MAX_DRAFTS


# ── Public served shape (README §3) — OpenAPI documentation and typing ──
#
# The endpoint returns ``served.served_document`` verbatim; these models only
# describe it. Built by hand, not from these models, because SVG assets omit
# width/height while a ``null`` background is meaningful and must be kept.

class BrandThemeAssetRef(BaseModel):
    url: str
    type: str
    bytes: int
    sha256: str
    width: int | None = None
    height: int | None = None


class BrandThemeLogoRefs(BaseModel):
    dark: BrandThemeAssetRef
    light: BrandThemeAssetRef


class BrandThemeBackgroundRefs(BaseModel):
    dark: BrandThemeAssetRef | None
    light: BrandThemeAssetRef | None


class BrandThemeCopy(BaseModel):
    newChatHeading: str | None = None


class BrandThemeContent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = PydanticField(alias="schema")
    name: str
    # mode ("dark" | "light") → role → "#rrggbb"
    modes: dict[str, dict[str, str]]
    logo: BrandThemeLogoRefs | None = None
    background: BrandThemeBackgroundRefs | None = None
    copy_: BrandThemeCopy | None = PydanticField(default=None, alias="copy")


class BrandThemePublic(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = PydanticField(alias="schema")
    revision: int
    theme: BrandThemeContent
    # "logo.dark", "logo.light", "background.dark", "background.light"
    assets: dict[str, BrandThemeAssetRef]
