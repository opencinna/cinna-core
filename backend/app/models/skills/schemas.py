"""Wire shapes for the skills catalog (no tables).

Everything a client needs to browse the catalog, read a revision, publish a
skill from an agent and install one into another agent. Kept separate from the
table modules so the API contract can grow projections (publisher display name,
install count, "which of my agents already have this") that no column backs.
"""
import uuid
from datetime import datetime

from pydantic import field_validator
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
    #: Whether this row is visible to the caller *because someone granted it*
    #: — a ``users``-visibility package they were named on. False for the
    #: publisher's own row and for every public package, so the card can say
    #: "Shared with you" only where that is actually the reason it is there.
    is_granted: bool = False


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

    #: Free text, but **one line**: it is written into the skill's own
    #: ``SKILL.md`` frontmatter, where a newline would inject top-level keys
    #: into the author's header. Refused at the boundary as well as normalised
    #: in ``coerce_version``, so a client gets a 422 naming the field rather
    #: than a silently-dropped version.
    version: str | None = Field(default=None, max_length=64)
    release_notes: str | None = None
    #: ``private`` (default), ``users`` or ``public``. Only honoured on the
    #: first publish and on an explicit change — a re-publish that omits it
    #: leaves the package's current visibility alone.
    visibility: str | None = None
    #: Email addresses to grant catalog visibility to, in the same transaction
    #: as the publish. **Additive**: a re-publish adds the emails it names and
    #: never revokes the ones it omits — revoking is its own verb
    #: (``DELETE /skills/packages/{id}/grants/{user_id}``), so an out-of-date
    #: dialog cannot silently take access away. An unknown address fails the
    #: whole publish rather than half-sharing it.
    #:
    #: Only meaningful together with an effective visibility of ``users`` —
    #: grants are consulted under that visibility and no other. Naming an
    #: address **other than the publisher's own** while the package would stay
    #: ``private`` or ``public`` is refused (409
    #: ``grants_require_users_visibility``) rather than written as rows that do
    #: nothing until somebody changes the visibility. The publisher's own
    #: address resolves to no grant row at all, so it stays the no-op it is
    #: under every visibility.
    #:
    #: Bounded because each address costs one case-insensitive user lookup with
    #: no functional index behind it, and this list arrives from a request body:
    #: an unbounded one turns a publish into an arbitrary number of sequential
    #: scans. Fifty is well past the size of a dialog anybody fills in by hand,
    #: and a wider audience is what ``visibility="public"`` is for.
    grant_emails: list[str] = Field(default=[], max_length=50)
    #: Reverse-DNS id for the FIRST publish of a skill. On a re-publish it may
    #: only repeat the package's existing id — a mismatch is refused with
    #: ``package_id_immutable`` (409) rather than silently ignored, because
    #: every install and every container manifest references that id.
    package_id: str | None = Field(default=None, max_length=255)

    @field_validator("version")
    @classmethod
    def _version_is_one_line(cls, value: str | None) -> str | None:
        """Refuse a version carrying a newline or any other control character.

        The boundary check, paired with the normaliser in ``coerce_version``:
        this value is written verbatim into a ``SKILL.md`` frontmatter block as
        ``version: <value>``, so a newline injects top-level keys into the
        author's own header — and the published revision is immutable, so it
        ships. A 422 naming the field beats a version the server quietly
        dropped.
        """
        if value is None:
            return None
        if any(ch == "\r" or ch == "\n" or ch < " " for ch in value):
            raise ValueError(
                "A version must be a single line with no control characters."
            )
        return value


class SkillPublishPreview(SQLModel):
    """Response of ``GET /agents/{agent_id}/skills/{name}/publish-preview``.

    Everything the Share dialog needs to show the publisher what pressing the
    button will do, computed by the same code that will do it. Nothing here is
    a promise: the values are re-derived inside the publish lock, so a
    concurrent publish of the same skill can still move the revision number
    between this call and the next one.
    """

    #: The version this publish would use. Never null — a skill with no
    #: version anywhere starts at ``1.0.0``.
    version: str
    #: ``version:`` as it stands in ``SKILL.md`` right now, if the author (or
    #: an earlier publish) put one there.
    header_version: str | None = None
    #: The newest version this package has already released.
    latest_published_version: str | None = None
    #: The id this publish would use: the package's existing id on a
    #: re-publish, otherwise a derived and verified-free one.
    package_id: str
    #: True when the plain ``<host>.skill.<name>`` id was already taken on this
    #: instance and the publisher's own slug had to be appended. Surfaced so
    #: the dialog can say so rather than showing an id whose shape the
    #: publisher has no explanation for.
    package_id_disambiguated: bool = False
    #: Whether this skill already has a package behind it.
    is_republish: bool = False
    next_revision_number: int = 1


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


# =============================================================================
# Access grants (``visibility='users'``)
# =============================================================================


class SkillPackageAccessGrantPublic(SQLModel):
    """Response schema for one skill package access grant.

    ``user_email`` is resolved from the ``User`` row rather than stored: the
    grant is keyed on the user id, and an email the publisher typed months ago
    must not outlive a change of address.
    """

    id: uuid.UUID
    package_id: uuid.UUID
    user_id: uuid.UUID
    user_email: str | None = None
    granted_by_user_id: uuid.UUID | None = None
    created_at: datetime


class SkillPackageAccessGrantsPublic(SQLModel):
    """List response for a package's grants."""

    data: list[SkillPackageAccessGrantPublic]
    count: int


class SkillPackageAccessGrantCreate(SQLModel):
    """Body of ``POST /skills/packages/{package_id}/grants``."""

    email: str
