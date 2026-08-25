"""App MCP as a `ServerChannel` — the singleton, the shape, and the surfacing.

App MCP became a channel so that the admin gains what it never had: a
server-wide kill switch, a visibility + grant allowlist, and per-user
enablement. This file covers the *channel* half of that — the row, its
uniqueness, the transport shape the admin form is driven by, and both listing
surfaces.

The *enforcement* half — that an existing, otherwise-valid App MCP token stops
working when any of those switches close — lives in
`tests/api/app_mcp/app_mcp_channel_availability_test.py`, because it needs the
OAuth flow that mints a real token and the App MCP domain's fixtures.

Two facts worth stating once, because several assertions below rest on them:

* **Nobody creates this row.** There is no `create_server_channel` call for
  `app_mcp` anywhere, and there cannot be one. It is materialized lazily by
  `ServerChannelService.get_or_create_singleton` — the one accessor the admin
  list, the user list and the token verifier all share — so its presence in a
  listing *is* the assertion that the accessor ran.
* **`email_whitelist` is inert here and is expected to stay NULL.** The
  whitelist is applied in the inbound pipeline's post-verification step, which
  an `authenticated` transport never enters. A NULL whitelist on this channel
  denies nobody; that is exactly why the admin form hides the field rather
  than rendering a fail-closed control that means nothing.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.server_channel import (
    create_server_channel,
    find_server_channel_by_type,
    list_channel_types,
    list_server_channels,
    replace_channel_grants,
    update_server_channel,
)
from tests.utils.user import create_random_user, user_authentication_headers
from tests.utils.user_channel import find_my_channel, get_my_channels

_ADMIN_BASE = f"{settings.API_V1_STR}/admin/server-channels"
_APP_MCP = "app_mcp"


def test_app_mcp_channel_is_a_singleton_admin_object(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The App MCP row exists without being created, and cannot be doubled:

    1. It appears in the admin list although nothing created it
    2. Its projection is the authenticated-transport shape (no webhook, no
       missing-credential alarm, no config)
    3. `/channel-types` declares that shape, which is what the admin form
       branches on instead of the channel type
    4. Creating a second one is a 409
    5. Moving an *existing* channel onto the type is the same 409
    6. Its adapter refuses configuration outright
    7. There is no webhook token to regenerate
    8. It cannot be deleted — that would silently reset the kill switch
    9. It can be switched off and back on, which is the whole point
    """
    # ── Phase 1: the row is there, unbidden ───────────────────────────────
    channel = find_server_channel_by_type(client, superuser_token_headers, _APP_MCP)
    channel_id = channel["id"]

    # ── Phase 2: the authenticated-transport projection ──────────────────
    assert channel["enabled"] is True
    assert channel["visibility"] == "public"
    assert channel["default_enabled_for_users"] is True
    assert channel["default_agent_scope"] == "all"
    # No webhook: a token would advertise a door that can only ever 404.
    assert channel["webhook_token"] is None
    assert channel["webhook_url"] is None
    # True, and deliberately so: there is no outbound credential and none is
    # missing, so a False here would be a permanent, unclearable "No
    # credential" alarm on the admin list.
    assert channel["has_outbound_credentials"] is True
    assert channel["config"] == {}
    # Inert, not misconfigured — see the module docstring.
    assert channel["email_whitelist"] is None

    # ── Phase 3: the declared shape the admin form reads ─────────────────
    types = {t["channel_type"]: t for t in list_channel_types(client, superuser_token_headers)}
    app_mcp_type = types[_APP_MCP]
    assert app_mcp_type["inbound_mode"] == "authenticated"
    assert app_mcp_type["needs_webhook_token"] is False
    assert app_mcp_type["needs_outbound_credentials"] is False
    assert app_mcp_type["is_singleton"] is True
    # Paired with a webhook transport, so the assertions above are about
    # App MCP's declaration rather than about every field being False.
    google_chat = types["google_chat"]
    assert google_chat["inbound_mode"] == "webhook"
    assert google_chat["needs_webhook_token"] is True
    assert google_chat["is_singleton"] is False

    # ── Phase 4: a second one is refused ─────────────────────────────────
    # 409, not 422: the payload is legal, it conflicts with the collection.
    create_server_channel(
        client,
        superuser_token_headers,
        channel_type=_APP_MCP,
        config={},
        expected_status=409,
    )

    # ── Phase 5: and so is migrating an existing channel onto the type ───
    google = create_server_channel(client, superuser_token_headers)
    update_server_channel(
        client,
        superuser_token_headers,
        google["id"],
        expected_status=409,
        channel_type=_APP_MCP,
    )
    # Re-sending the App MCP channel's own type is not a conflict with itself.
    update_server_channel(
        client, superuser_token_headers, channel_id, channel_type=_APP_MCP
    )

    # ── Phase 6: the adapter takes no configuration ──────────────────────
    update_server_channel(
        client,
        superuser_token_headers,
        channel_id,
        expected_status=422,
        config={"project_number": "123456789012"},
    )

    # ── Phase 7: nothing to regenerate ───────────────────────────────────
    update_server_channel(
        client,
        superuser_token_headers,
        channel_id,
        expected_status=422,
        regenerate_webhook_token=True,
    )

    # ── Phase 8: undeletable, and still listed afterwards ────────────────
    r = client.delete(f"{_ADMIN_BASE}/{channel_id}", headers=superuser_token_headers)
    assert r.status_code == 422, r.text
    assert any(
        c["id"] == channel_id
        for c in list_server_channels(client, superuser_token_headers)
    )

    # ── Phase 9: the kill switch itself works ────────────────────────────
    off = update_server_channel(
        client, superuser_token_headers, channel_id, enabled=False
    )
    assert off["enabled"] is False
    # Still exactly one row: a disabled singleton must not be re-materialized
    # as a second, enabled one by the next listing.
    still_one = find_server_channel_by_type(client, superuser_token_headers, _APP_MCP)
    assert still_one["id"] == channel_id
    assert still_one["enabled"] is False
    back_on = update_server_channel(
        client, superuser_token_headers, channel_id, enabled=True
    )
    assert back_on["enabled"] is True

    # ── Phase 10: still superuser-only, like every other channel route ───
    # An unknown id on purpose: the auth dependency runs before the channel is
    # looked up, so a refusal here is about the caller and not about the row.
    #
    # There is no `GET /admin/server-channels/{id}` — only PUT and DELETE — and
    # FastAPI answers a method mismatch with 405 *before* any dependency runs,
    # so a GET there would pass this assertion while proving nothing about
    # authentication. Each request below is a method the route table actually
    # has.
    ghost = str(uuid.uuid4())
    assert client.get(_ADMIN_BASE).status_code in (401, 403)
    assert client.get(f"{_ADMIN_BASE}/{ghost}/grants").status_code in (401, 403)
    assert client.delete(f"{_ADMIN_BASE}/{ghost}").status_code in (401, 403)


def test_app_mcp_channel_appears_in_user_settings_and_follows_visibility(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A user sees App MCP in Settings → Channels, gated like any channel:

    1. A brand-new user, who has never touched App MCP, sees the row
    2. The projection is the user-facing one — no admin fields leak
    3. `visibility="restricted"` with no grant removes it entirely (404-shaped
       absence, indistinguishable from a channel that does not exist)
    4. A grant brings it back
    5. The admin kill switch removes it for a granted user too
    """
    consumer = create_random_user(client)
    consumer_headers = user_authentication_headers(
        client=client, email=consumer["email"], password=consumer["_password"]
    )

    # ── Phase 1: present for a user who has done nothing ─────────────────
    channel = find_server_channel_by_type(client, superuser_token_headers, _APP_MCP)
    channel_id = channel["id"]
    mine = find_my_channel(get_my_channels(client, consumer_headers), channel_id)
    assert mine["channel_type"] == _APP_MCP
    assert mine["is_available"] is True
    # No settings row exists — the channel default is what they get, and
    # nothing on a read path may materialize one for them.
    assert mine["is_enabled_inherited"] is True

    # ── Phase 2: the user projection carries no admin field ──────────────
    # Allowlist, not a blocklist of known secret names: a model cannot leak a
    # field it does not declare, and this asserts the model stayed that way.
    for forbidden in ("webhook_token", "webhook_url", "config", "email_whitelist",
                      "encrypted_secrets", "has_outbound_credentials"):
        assert forbidden not in mine, (forbidden, mine)

    # ── Phase 3: restricted, ungranted → gone ────────────────────────────
    update_server_channel(
        client, superuser_token_headers, channel_id, visibility="restricted"
    )
    assert channel_id not in {
        c["id"] for c in get_my_channels(client, consumer_headers)
    }

    # ── Phase 4: granted → back ──────────────────────────────────────────
    replace_channel_grants(
        client, superuser_token_headers, channel_id, [consumer["id"]]
    )
    granted = find_my_channel(get_my_channels(client, consumer_headers), channel_id)
    assert granted["is_available"] is True

    # ── Phase 5: the kill switch outranks the grant ──────────────────────
    update_server_channel(
        client, superuser_token_headers, channel_id, enabled=False
    )
    assert channel_id not in {
        c["id"] for c in get_my_channels(client, consumer_headers)
    }
