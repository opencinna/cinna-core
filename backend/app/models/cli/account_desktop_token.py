"""
Account-CLI → desktop token exchange schemas (handover §8.2 / D12).

Request + response models for ``POST /api/v1/cli/account/desktop-token``, the
endpoint Cinna Desktop calls when it finds a CLI account token in a workshop
tree and wants a desktop session without sending the user through a browser
consent.

Pydantic models only — no ``table=True``. The rows this exchange touches are the
existing ``desktop_oauth_client`` / ``desktop_refresh_token`` tables, written by
``DesktopAuthService``.
"""
from sqlmodel import Field, SQLModel


class AccountDesktopTokenBody(SQLModel):
    """Body of ``POST /cli/account/desktop-token``.

    ``client_id`` is the desktop's own client id when it has one. A desktop that
    has never authenticated against this instance has none, so it sends the same
    three display fields the browser consent flow accepts (``device_name``,
    ``platform``, ``app_version``) and a client is registered for it — the
    identical lazy-registration path, not a copy of it.

    There is deliberately no field for the client's *origin*: provenance is set
    by the server, never claimed by the caller.
    """

    client_id: str | None = Field(default=None, max_length=64)
    device_name: str | None = Field(default=None, max_length=200)
    platform: str | None = Field(default=None, max_length=50)
    app_version: str | None = Field(default=None, max_length=50)


class AccountDesktopTokenResponse(SQLModel):
    """Desktop tokens issued from a CLI account token.

    Mirrors the ``/desktop-auth/token`` response the desktop already knows how to
    consume — same field names, same semantics, same refresh endpoint — plus
    ``email``, which lets the desktop name the profile it is about to create
    without a second round-trip to ``/desktop-auth/userinfo``.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    client_id: str
    email: str
