"""One access model for every catalog kind (agent bundles and skill packages).

An item carries ``visibility`` (``private`` / ``users`` / ``public``) and
``is_listed``; its grant table (``bundle_access_grant`` /
``skill_package_access_grant``) names principals — one person (``user``),
every holder of a role (``role``) or every member of a user group (``group``,
membership resolved live) — with a level:

- ``use`` — see and install (counts only under ``visibility='users'`` while listed)
- ``edit`` — publish new revisions
- ``maintain`` — access, visibility, delete

There is no ``everyone`` principal; that is ``visibility='public'``. The rules
live in :class:`app.services.catalog.catalog_access_service.CatalogAccessService`.
"""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict
from sqlmodel import SQLModel


class CatalogAccess:
    """Access levels, ordered: each level includes the ones before it."""

    USE = "use"
    EDIT = "edit"
    MAINTAIN = "maintain"

    ORDER = (USE, EDIT, MAINTAIN)

    @classmethod
    def rank(cls, access: str | None) -> int:
        """0 for no access, then 1..3 in :attr:`ORDER`."""
        if access is None or access not in cls.ORDER:
            return 0
        return cls.ORDER.index(access) + 1

    @classmethod
    def max(cls, *levels: str | None) -> str | None:
        best: str | None = None
        for level in levels:
            if cls.rank(level) > cls.rank(best):
                best = level
        return best


CatalogAccessLevel = Literal["use", "edit", "maintain"]


class CatalogPrincipalKind:
    """``principal_kind`` values on both grant tables."""

    USER = "user"
    ROLE = "role"
    GROUP = "group"

    ALL = (USER, ROLE, GROUP)


# ── API schemas ─────────────────────────────────────────────────────


class CatalogPrincipalUser(SQLModel):
    # ``forbid`` keeps the union unambiguous: ``{"user_id", "role"}`` is an
    # error, never silently read as one of the two.
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID


class CatalogPrincipalRole(SQLModel):
    model_config = ConfigDict(extra="forbid")

    role: str


class CatalogPrincipalGroup(SQLModel):
    model_config = ConfigDict(extra="forbid")

    group_id: uuid.UUID


CatalogPrincipal = CatalogPrincipalUser | CatalogPrincipalRole | CatalogPrincipalGroup


class CatalogGrantIn(SQLModel):
    """One entry of ``PUT /catalog/items/{uuid}/grants`` (full replace)."""

    principal: CatalogPrincipal
    access: CatalogAccessLevel


class CatalogPerson(SQLModel):
    user_id: uuid.UUID
    display_name: str


class CatalogMaintainer(CatalogPerson):
    """A person holding ``maintain`` on an item."""


class CatalogGrant(SQLModel):
    principal: CatalogPrincipal
    access: CatalogAccessLevel
    # Person's full name or email, the role value, or the group name.
    display_name: str
    # Groups only: how many people belong to the group right now.
    member_count: int | None = None
    granted_by: CatalogPerson | None = None
    created_at: datetime
