"""Per-user channel settings — `GET/PUT/DELETE /users/me/channels`.

The row these routes own (`channel_user_setting`) is *optional by design*: its
absence means "the channel default applies", and `PUT` is the only thing in the
codebase that ever creates one. That makes lazy creation through the route the
load-bearing behaviour of the whole per-user settings model, and this file is
where it is proved.

Started thin — one existence proof that a row can be created through the route
under these fixtures. Now covers: the inherit-vs-override provenance matrix
(an admin default flip is followed by an inheriting user and not by a user with
an explicit value, in both directions), `DELETE` reverting a user to pure
inheritance (and a later admin default change then being followed again), the
cross-user ownership isolation grid on all three verbs, and that the user
projection carries no secret-adjacent field.

Channel-*policy* scenarios that need a webhook delivery to observe their effect
(visibility/grants, `agent_scope`, pins, `allow_auto_install`, the decline gate
on an already-bound thread) live in `server_channels_policy_test.py` instead —
this file stays about the settings routes themselves.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.bundle import make_user_and_headers
from tests.utils.server_channel import create_server_channel, update_server_channel
from tests.utils.user_channel import (
    delete_my_channel,
    find_my_channel,
    get_my_channels,
    update_my_channel,
)

API = settings.API_V1_STR


def test_put_creates_the_settings_row_on_first_edit(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """First `PUT` materialises the row, and the stored choice is read back.

    Before the edit the user has no row: `has_settings` is False and
    `is_enabled` is True *by inheritance* from the channel default. After a
    single `PUT` switching the channel off for themselves, the same fields must
    report a user-owned False — and a fresh `GET` must agree, which is what
    separates "the row was written" from "the response was computed".

    This is also the regression guard for the creation path itself. It ran
    through `session.begin_nested()` until the insert became a native
    `INSERT ... ON CONFLICT DO UPDATE`; the savepoint's commit collided with
    the suite's `restart_savepoint` listener, so no test could reach this route
    at all and the only production creator of a `channel_user_setting` row was
    uncovered.
    """
    channel = create_server_channel(client, superuser_token_headers)
    _, user_headers = make_user_and_headers(client)

    before = client.get(f"{API}/users/me/channels", headers=user_headers)
    assert before.status_code == 200, before.text
    row = next(c for c in before.json() if c["id"] == channel["id"])
    assert row["has_settings"] is False, row
    assert row["is_enabled"] is True and row["is_enabled_inherited"] is True, row

    saved = client.put(
        f"{API}/users/me/channels/{channel['id']}",
        headers=user_headers,
        json={"is_enabled": False},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["has_settings"] is True, body
    assert body["is_enabled"] is False, body
    assert body["is_enabled_inherited"] is False, body

    after = client.get(f"{API}/users/me/channels", headers=user_headers)
    assert after.status_code == 200, after.text
    persisted = next(c for c in after.json() if c["id"] == channel["id"])
    assert persisted["has_settings"] is True, persisted
    assert persisted["is_enabled"] is False, persisted


def test_admin_default_flip_is_followed_by_the_inheriting_user_and_not_by_the_explicit_one(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`ChannelPolicyService.resolve`'s central rule, both directions at once.

    Two users on the same channel: A never touches their settings (inherits),
    B writes an explicit `is_enabled=True` — which reads identically to the
    channel default at that moment, so the only thing distinguishing them is
    provenance (`is_enabled_inherited`). The admin then flips the channel's
    `default_enabled_for_users` to False. A must follow the new default; B, who
    made a choice, must not move — that is the entire reason the column is
    nullable-meaning-inherit rather than `NOT NULL DEFAULT true` (see
    `ChannelUserSetting`'s module docstring).
    """
    channel = create_server_channel(client, superuser_token_headers)
    assert channel["default_enabled_for_users"] is True, channel

    user_a, headers_a = make_user_and_headers(client)
    user_b, headers_b = make_user_and_headers(client)

    # B makes an explicit choice that happens to match the current default —
    # the point is that it is now user-owned, not that it looks different yet.
    saved_b = update_my_channel(client, headers_b, channel["id"], is_enabled=True)
    assert saved_b["is_enabled"] is True and saved_b["is_enabled_inherited"] is False

    # A never touches theirs.
    before_a = find_my_channel(get_my_channels(client, headers_a), channel["id"])
    assert before_a["has_settings"] is False, before_a
    assert before_a["is_enabled"] is True and before_a["is_enabled_inherited"] is True

    update_server_channel(
        client, superuser_token_headers, channel["id"], default_enabled_for_users=False
    )

    # A followed the new default straight through — no row, no action needed.
    after_a = find_my_channel(get_my_channels(client, headers_a), channel["id"])
    assert after_a["has_settings"] is False, after_a
    assert after_a["is_enabled"] is False, after_a
    assert after_a["is_enabled_inherited"] is True, after_a
    assert after_a["is_available"] is False, after_a

    # B's explicit choice survived the admin's flip untouched.
    after_b = find_my_channel(get_my_channels(client, headers_b), channel["id"])
    assert after_b["has_settings"] is True, after_b
    assert after_b["is_enabled"] is True, after_b
    assert after_b["is_enabled_inherited"] is False, after_b
    assert after_b["is_available"] is True, after_b


def test_delete_reverts_to_inheritance_and_a_later_admin_default_change_is_followed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`DELETE` is the only way back to "follow the admin default".

    A user writes an explicit override, `DELETE`s it, and the row is gone —
    `has_settings` flips back to False and the resolved value reverts to
    whatever the channel default says *right now*. Proved by then moving the
    admin default a second time and watching the now-inheriting user follow it,
    which a stale cached "reverted value" could not do.
    """
    channel = create_server_channel(client, superuser_token_headers)

    user, headers = make_user_and_headers(client)
    saved = update_my_channel(client, headers, channel["id"], is_enabled=False)
    assert saved["has_settings"] is True
    assert saved["is_enabled"] is False and saved["is_enabled_inherited"] is False

    reverted = delete_my_channel(client, headers, channel["id"])
    assert reverted["has_settings"] is False, reverted
    assert reverted["is_enabled"] is True, reverted
    assert reverted["is_enabled_inherited"] is True, reverted

    # DELETE on a row that no longer exists is a no-op, not a 404 — already at
    # the end state a second call would try to reach.
    again = delete_my_channel(client, headers, channel["id"])
    assert again["has_settings"] is False, again

    # The user now genuinely inherits: a later admin flip is followed with no
    # further action on their side.
    update_server_channel(
        client, superuser_token_headers, channel["id"], default_enabled_for_users=False
    )
    followed = find_my_channel(get_my_channels(client, headers), channel["id"])
    assert followed["has_settings"] is False, followed
    assert followed["is_enabled"] is False, followed
    assert followed["is_enabled_inherited"] is True, followed


# ---------------------------------------------------------------------------
# Ownership isolation + the secret-adjacent-field defence
# ---------------------------------------------------------------------------

#: Every field `UserChannelPublic` declares (`channel_user_setting.py`). Used
#: as an allowlist rather than checking a hand-picked blocklist of secret
#: names: a field added to the model later that is NOT added here fails this
#: test immediately, forcing a human to decide whether it is safe to expose —
#: which is the "fails loudly instead of slipping through" the plan asks for.
#: A blocklist of just `webhook_token`/`config`/`email_whitelist`/
#: `has_outbound_credentials` would stay green forever against a new field
#: under a different name that carries the same class of secret.
_ALLOWED_USER_CHANNEL_FIELDS = {
    "id",
    "channel_type",
    "name",
    "is_available",
    "is_enabled",
    "is_enabled_inherited",
    "channel_default_enabled",
    "agent_scope",
    "agent_scope_inherited",
    "channel_default_agent_scope",
    "agent_ids",
    "pinned_agent_id",
    "allow_identity_routing",
    "has_settings",
}

#: The specific fields `ServerChannelPublic` (the ADMIN projection) carries
#: that must never reach a regular user — named explicitly, once, so a failure
#: here says exactly which secret leaked rather than just "extra field".
_SECRET_ADJACENT_FIELDS = {
    "webhook_token",
    "config",
    "email_whitelist",
    "has_outbound_credentials",
}

# Sanity on the allowlist itself: if a secret-adjacent name were ever added to
# it by mistake, the assertions below would pass over a real leak.
assert not (_SECRET_ADJACENT_FIELDS & _ALLOWED_USER_CHANNEL_FIELDS)


def test_user_channel_projection_carries_no_secret_adjacent_field(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`UserChannelPublic` is a separate model, not `ServerChannelPublic` minus
    some fields — checked against the actual wire response, both from `GET`
    (no row) and from `PUT` (row exists), since the two are built by different
    code paths (`UserChannelService._project` either way, but reached through
    `list_for_user`/`to_public` vs `upsert_settings`) and either could drift.
    """
    channel = create_server_channel(client, superuser_token_headers)
    user, headers = make_user_and_headers(client)

    from_list = find_my_channel(get_my_channels(client, headers), channel["id"])
    assert set(from_list.keys()) <= _ALLOWED_USER_CHANNEL_FIELDS, set(from_list.keys())
    assert not (set(from_list.keys()) & _SECRET_ADJACENT_FIELDS), from_list

    from_put = update_my_channel(client, headers, channel["id"], is_enabled=False)
    assert set(from_put.keys()) <= _ALLOWED_USER_CHANNEL_FIELDS, set(from_put.keys())
    assert not (set(from_put.keys()) & _SECRET_ADJACENT_FIELDS), from_put


def test_user_cannot_read_write_or_delete_another_users_channel_settings(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Every route here is scoped to `current_user` by construction — no route
    takes a user id — so there is no cross-user isolation *rule* to break, but
    there is a cross-user isolation *outcome* worth proving: user A's `PUT`
    cannot be read, overwritten or deleted through user B's session, because
    B's requests always resolve against B's own (non-existent) row.

    Structured as: A sets a distinctive value, B independently sets a
    different one, and neither `GET` ever shows the other's, `PUT`/`DELETE`
    issued by B provably only ever touch B's own row.
    """
    channel = create_server_channel(client, superuser_token_headers)
    user_a, headers_a = make_user_and_headers(client)
    user_b, headers_b = make_user_and_headers(client)

    update_my_channel(client, headers_a, channel["id"], is_enabled=False)

    # B has never touched their own settings — B's GET must show inheritance,
    # not A's explicit False, despite both requests naming the same channel.
    b_view = find_my_channel(get_my_channels(client, headers_b), channel["id"])
    assert b_view["has_settings"] is False, b_view
    assert b_view["is_enabled"] is True, b_view

    # B's own PUT only ever creates/edits B's row.
    b_saved = update_my_channel(client, headers_b, channel["id"], is_enabled=True)
    assert b_saved["is_enabled"] is True

    # A's row is untouched by anything B did.
    a_view = find_my_channel(get_my_channels(client, headers_a), channel["id"])
    assert a_view["has_settings"] is True, a_view
    assert a_view["is_enabled"] is False, a_view

    # B's DELETE reverts only B to inheritance; A's explicit False survives.
    delete_my_channel(client, headers_b, channel["id"])
    a_after = find_my_channel(get_my_channels(client, headers_a), channel["id"])
    assert a_after["has_settings"] is True, a_after
    assert a_after["is_enabled"] is False, a_after
    b_after = find_my_channel(get_my_channels(client, headers_b), channel["id"])
    assert b_after["has_settings"] is False, b_after
