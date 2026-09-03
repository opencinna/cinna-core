"""Desktop OAuth client model — represents a registered desktop app installation."""
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel

# ── Client origin ───────────────────────────────────────────────────
#
# ``origin`` answers: **how did this client's most recent grant of tokens come
# about?** It is deliberately *not* "how was the row first created". A client id
# registered months ago through a browser consent can later be handed to
# ``POST /cli/account/desktop-token``, and if ``origin`` froze at registration
# time that exchange would produce a session indistinguishable from the browser
# one — which would defeat the whole point of showing it. So every grant path
# stamps it, both through ``DesktopAuthService._stamp_grant``: ``exchange_code``
# (the browser consent flow's token endpoint, shared with the mobile
# ``/app-auth`` surface) and ``issue_tokens_for_cli_exchange``. Row creation
# seeds it with the origin of the path that created the row, so it is never null
# before the first grant. Refresh rotation carries it forward unchanged —
# rotating is not a new grant.
#
# A scalar can only describe one grant, so ``_stamp_grant`` also collapses the
# client to a single live refresh family. Without that the column would be
# accurate only about the *most recent* grant while older ones stayed live, and
# the field would be readable in one direction only. See ``_stamp_grant``.
#
# Provenance, not display text. The server sets it on the granting path; it is
# never read from a request body — which is why it lives on the table model +
# ``DesktopOAuthClientPublic`` and NOT on ``DesktopOAuthClientBase`` /
# ``DesktopOAuthClientCreate`` (those carry the user-supplied display fields,
# which a caller may set freely). Do not fold it into ``device_name`` either:
# that field is caller-supplied display text, so provenance encoded there would
# be a label the caller could forge.
CLIENT_ORIGIN_BROWSER_CONSENT = "browser_consent"
"""Tokens last granted through the browser consent flow.

``/desktop-auth/authorize`` for the desktop and ``/app-auth/authorize`` for
Cinna Mobile — one value covers both, since the mobile surface shares this
service, these tables and the same consent semantics.
"""

CLIENT_ORIGIN_CLI_EXCHANGE = "cli_exchange"
"""Tokens last granted by exchanging a CLI account token.

``POST /cli/account/desktop-token``. Distinguished from a browser consent
because no human approved *that* session in a browser: a credential on disk did.
A user who never ran that exchange and sees one of these in
Settings → App Sessions is looking at a session someone minted from their
``account.json``.

The property users actually lean on is the contrapositive — **no badge means no
live CLI-minted session** — and it holds only because ``_stamp_grant`` retires
the previous grant's tokens. The one bounded exception is an access token issued
by a superseded grant, which survives until it expires
(``DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES``); revoking the client, or the account
token behind it, closes even that on the next request.
"""


@dataclass(frozen=True)
class GrantProvenance:
    """Where one grant of tokens came from: the pair, never either field alone.

    Both columns describe a single grant, so they are written together and
    compared together. Making that a value rather than two assignments is the
    point: ``__eq__`` is derived from the fields, so a provenance field
    *declared* here necessarily enters the comparison that decides whether a
    grant supersedes the previous one.

    **What that couples is field-to-compared, not written-to-compared, and the
    distinction is the whole of the safety margin — do not read it as the
    stronger claim.** ``of`` and ``apply`` are hand-written enumerations, so a
    new field has to be added in three places, not one. Exactly one omission
    shape is loud, and it is the narrowest of the three:

    * Declared with **no default** and forgotten in ``of`` (the read) — ``of``'s
      constructor call raises ``TypeError`` on the first grant. Loud.
    * Declared **with a default** and forgotten in ``of`` — **silent.** The read
      yields the default while the caller supplies the real value.
    * Forgotten in ``apply`` (the write), **with or without a default** —
      **silent.** The column is never written, so ``of`` keeps returning the
      stale value.

    Both silent shapes have the same consequence: an identical row compares
    unequal and is treated as superseded, retiring the client's live refresh
    tokens on **every** grant. That runs in the fail-safe direction, and **"it
    fails safe" is not an adequate defence of it** — over-revoking is precisely
    what the conditional in ``_stamp_grant`` exists to prevent, so the safe
    direction is the cost here, not the excuse. **So: give a new provenance
    field no default, and add it to ``of`` and ``apply`` in the same edit;
    neither the dataclass nor the type checker will tell you if you don't.**

    **What this does not do**, stated so nobody reads more into it: it cannot
    stop someone adding a provenance column and writing it outside
    ``DesktopAuthService._stamp_grant``. That exposure is exactly the same with
    or without this class; the guarantee is only that fields carried *here* stay
    in sync between writing and comparing.
    """

    origin: str
    minted_by_account_token_id: UUID | None

    @classmethod
    def of(cls, client: "DesktopOAuthClient") -> "GrantProvenance":
        """Read the provenance currently recorded on a client row."""
        return cls(
            origin=client.origin,
            minted_by_account_token_id=client.minted_by_account_token_id,
        )

    def apply(self, client: "DesktopOAuthClient") -> None:
        """Write this provenance onto a client row. Does not commit."""
        client.origin = self.origin
        client.minted_by_account_token_id = self.minted_by_account_token_id


class DesktopOAuthClientBase(SQLModel):
    device_name: str = Field(max_length=200)
    platform: str | None = Field(default=None, max_length=50)
    app_version: str | None = Field(default=None, max_length=50)


class DesktopOAuthClient(DesktopOAuthClientBase, table=True):
    __tablename__ = "desktop_oauth_client"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    client_id: str = Field(
        max_length=64,
        sa_column_kwargs={"unique": True, "index": True, "name": "client_id"},
    )
    user_id: UUID = Field(
        foreign_key="user.id",
        ondelete="CASCADE",
        sa_column_kwargs={"index": True},
    )
    is_revoked: bool = Field(default=False)
    # No default on either side, deliberately, and the DB is what enforces it.
    # The reassuring value must never be the one a new creation path gets for
    # free: a row created without stating its provenance would read as a
    # human-approved browser consent, which is the exact direction the badge is
    # trusted in ("no cli_exchange ⇒ no CLI-minted session").
    #
    # Dropping the Python-side default alone does NOT achieve that — SQLModel
    # skips validation on ``table=True`` models, so the attribute is just
    # ``None`` and a server default would fill it in silently on INSERT. That
    # was measured, not assumed. Migration c9a2f5b1d604 therefore backfills the
    # existing rows and removes the standing server default, so an insert that
    # omits ``origin`` raises a NOT NULL violation instead of being quietly
    # reassured.
    origin: str = Field(max_length=32)
    # Provenance for the CLI-exchange grant path: the account CLI token that
    # bought this client's current session, or NULL when a browser consent did.
    # Written and cleared together with ``origin`` (see ``_stamp_grant``) — the
    # pair describes ONE grant, and a row where they disagree is a bug.
    #
    # It exists so revoking that account token also disconnects the desktop
    # session it minted, mirroring the child-CLI-token cascade in
    # ``AccountCLIService.revoke_account_token``. SET NULL rather than CASCADE,
    # for the same reason ``cli_device_login_request.minted_token_id`` uses it:
    # deleting a token row must not silently delete the session record a user
    # may still need to see.
    minted_by_account_token_id: UUID | None = Field(
        default=None,
        foreign_key="cli_token.id",
        nullable=True,
        index=True,
        ondelete="SET NULL",
    )
    last_used_at: datetime | None = Field(
        default=None, nullable=True, sa_type=DateTime(timezone=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=DateTime(timezone=True),
    )


class DesktopOAuthClientCreate(SQLModel):
    device_name: str = Field(max_length=200)
    platform: str | None = None
    app_version: str | None = None


class DesktopOAuthClientPublic(SQLModel):
    client_id: str
    device_name: str
    platform: str | None = None
    app_version: str | None = None
    # Required for the same reason the column is: a projection that forgot to
    # pass it would report every session as a browser consent.
    origin: str
    # ``minted_by_account_token_id`` is deliberately NOT projected: ``origin``
    # already carries everything a user needs to judge the session, and the
    # token id it references is an internal identifier of a *different*
    # credential. Exposing it would leak account-token ids into a response that
    # a desktop client also reads.
    last_used_at: datetime | None = None
    created_at: datetime
    is_revoked: bool
