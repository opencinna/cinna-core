"""Helpers for the user-facing channel settings routes (`GET/PUT/DELETE
/users/me/channels`), `backend/app/api/routes/user_channels.py`.

Plain HTTP wrappers — no exemptions needed, unlike the two documented
DB-seam shortcuts (`_scope_list_containing` in
`server_channels_routing_test.py`, `_set_sender_scope` in
`routing_reachability_verdict_test.py`) that predate `PUT` being able to
create a `channel_user_setting` row under these fixtures at all. Those two
may migrate to `update_my_channel` below now that the route works; this
module is what they would migrate to.
"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.core.config import settings

API = settings.API_V1_STR
_BASE = f"{API}/users/me/channels"


def get_my_channels(
    client: TestClient, token_headers: dict[str, str], *, expected_status: int = 200
) -> Any:
    """GET /users/me/channels — every channel this caller may use."""
    r = client.get(_BASE, headers=token_headers)
    assert r.status_code == expected_status, (
        f"List my channels failed: {r.status_code} {r.text}"
    )
    return r.json() if r.status_code == expected_status else None


def find_my_channel(channels: list[dict], channel_id: str) -> dict:
    """Pick one row out of a `get_my_channels` response by channel id."""
    return next(c for c in channels if c["id"] == channel_id)


def update_my_channel(
    client: TestClient,
    token_headers: dict[str, str],
    channel_id: str,
    *,
    expected_status: int = 200,
    **fields: Any,
) -> Any:
    """PUT /users/me/channels/{channel_id} — upsert the caller's settings.

    An omitted field is left unchanged; pass an explicit ``None`` for a field
    to clear it (reverts an inheritable field to the channel default). A call
    with no ``**fields`` sends ``{}``, the documented no-op body.
    """
    r = client.put(f"{_BASE}/{channel_id}", headers=token_headers, json=fields)
    assert r.status_code == expected_status, (
        f"Update my channel failed: {r.status_code} {r.text}"
    )
    return r.json() if r.status_code == expected_status else None


def delete_my_channel(
    client: TestClient,
    token_headers: dict[str, str],
    channel_id: str,
    *,
    expected_status: int = 200,
) -> Any:
    """DELETE /users/me/channels/{channel_id} — revert to pure inheritance."""
    r = client.delete(f"{_BASE}/{channel_id}", headers=token_headers)
    assert r.status_code == expected_status, (
        f"Delete my channel failed: {r.status_code} {r.text}"
    )
    return r.json() if r.status_code == expected_status else None
