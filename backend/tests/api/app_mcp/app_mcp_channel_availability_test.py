"""App MCP availability is enforced at token verification, not at token issue.

App MCP is a `ServerChannel` (`channel_type="app_mcp"`), so the admin kill
switch, the channel's `visibility` + grant allowlist, and the user's own
per-channel toggle all apply to it. `AppMCPTokenVerifier` reads that policy on
every verified token, which is what makes the switch a switch: a token minted
while the channel was open stops working the moment it closes.

`app_mcp_token` rows are never touched by any of this. Nothing is revoked; the
same, still-valid token is simply refused — and refused with the same `None`
an expired or forged one gets, because a distinguishable "your token is fine
but you are not allowed" would be an oracle for a server's channel
configuration.

WHY THE TTL IS SET FROM `settings` RATHER THAN SLEPT THROUGH
------------------------------------------------------------
Availability is cached per user id for
`settings.APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS`, because verification runs
once per HTTP request to `/mcp/app/mcp`. Patching that setting to `0` bypasses
the cache entirely, so a revocation is observable in the same test without a
`sleep`. The cache's *own* behaviour is proved separately, in three scenarios
that each pin a different property: that it really caches (long TTL, explicit
reset), that the TTL is a deadline the cache honours rather than a number it
stores (long TTL, patched clock), and that a lookup which *fails* is refused
without being remembered as an answer.

`verify_app_mcp_token` is a documented Rule-1 exemption; see
`tests/utils/app_mcp.py` for why the verifier has no clean HTTP surface. Every
*input* to the decision here is produced through real routes: the token comes
from the full OAuth dance, the channel is toggled through the admin API, the
grant through the grant API, and the user's own switch through
`PUT /users/me/channels/{id}`.

Notes:
  - The channel row itself, its singleton-ness and both listing surfaces are
    covered in `tests/api/server_channels/server_channels_app_mcp_test.py`.
"""
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.utils.app_mcp import (
    obtain_app_mcp_access_token,
    reset_app_mcp_availability_cache,
    verify_app_mcp_token,
)
from tests.utils.mcp import MCP_BASE_URL
from tests.utils.server_channel import (
    find_server_channel_by_type,
    replace_channel_grants,
    update_server_channel,
)
from tests.utils.user import create_random_user_with_headers
from tests.utils.user_channel import update_my_channel

_APP_MCP = "app_mcp"

#: Patch targets, named because three of the scenarios below need them and the
#: literals do not fit on one line inside a nested ``with``.
_TTL_SETTING = "app.core.config.settings.APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS"
#: The verifier's clock. Patched, not slept through — the TTL is an ``int``.
_MONOTONIC = "app.mcp.app_token_verifier.time.monotonic"
#: The availability lookup's final step, and the one made to fail on demand.
_POLICY_RESOLVE = "app.mcp.app_token_verifier.ChannelPolicyService.resolve"


# ── Module fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def patch_mcp_server_base_url():
    """`is_app_mcp_resource()` compares against this at runtime.

    Without the patch it compares against whatever tunnel URL `.env` carries,
    and the authorize step routes to per-connector consent instead of App MCP.
    """
    with patch("app.core.config.settings.MCP_SERVER_BASE_URL", MCP_BASE_URL):
        yield


@pytest.fixture(autouse=True)
def patch_oauth_create_session(db):
    """Keep the OAuth routes' own sessions on the test transaction."""
    from tests.utils.db_proxy import NonClosingSessionProxy

    with patch(
        "app.mcp.oauth_routes.create_session",
        lambda: NonClosingSessionProxy(db),
    ):
        yield


@pytest.fixture(autouse=True)
def clean_availability_cache():
    """The verifier's cache is process-global, like the channel debug buffer.

    Reset on both sides so a decision cached by one test can never answer
    another test's question.
    """
    reset_app_mcp_availability_cache()
    yield
    reset_app_mcp_availability_cache()


@pytest.fixture
def no_availability_cache():
    """TTL 0 — every verification resolves against the database.

    The deterministic way to observe a revocation. A `<= 0` TTL is a real,
    supported setting (see `settings.APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS`),
    not a test-only branch.
    """
    with patch(_TTL_SETTING, 0):
        yield


# ── Scenarios ────────────────────────────────────────────────────────────────


def test_app_mcp_token_follows_the_channel_switches(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    no_availability_cache,
) -> None:
    """One token, unchanged throughout, refused and restored by each switch:

    1. A real OAuth flow mints an access token for a normal user
    2. The verifier accepts it — the channel is enabled and public
    3. Admin disables the channel → the same token is refused
    4. Admin re-enables it → accepted again
    5. `visibility="restricted"` with no grant → refused
    6. A grant for this user → accepted
    7. The user switches the channel off for themselves → refused
    8. The user switches it back on → accepted
    """
    user, headers = create_random_user_with_headers(client)

    # ── Phase 1: mint a genuine token through the OAuth dance ────────────
    token = obtain_app_mcp_access_token(client, headers)

    # ── Phase 2: baseline — enabled + public means usable ────────────────
    channel = find_server_channel_by_type(client, superuser_token_headers, _APP_MCP)
    channel_id = channel["id"]
    assert channel["enabled"] is True
    assert verify_app_mcp_token(token) is True

    # ── Phase 3: the admin kill switch ───────────────────────────────────
    update_server_channel(client, superuser_token_headers, channel_id, enabled=False)
    assert verify_app_mcp_token(token) is False, (
        "a disabled App MCP channel must refuse an otherwise-valid token"
    )

    # ── Phase 4: reversible — nothing about the token was revoked ────────
    update_server_channel(client, superuser_token_headers, channel_id, enabled=True)
    assert verify_app_mcp_token(token) is True

    # ── Phase 5: restricted visibility with no grant ─────────────────────
    update_server_channel(
        client, superuser_token_headers, channel_id, visibility="restricted"
    )
    assert verify_app_mcp_token(token) is False

    # ── Phase 6: a grant re-opens it for this user only ──────────────────
    replace_channel_grants(client, superuser_token_headers, channel_id, [user["id"]])
    assert verify_app_mcp_token(token) is True

    # ── Phase 7: the user's own toggle is the third term ─────────────────
    # Their own choice, not the admin's — and it is enough on its own.
    update_my_channel(client, headers, channel_id, is_enabled=False)
    assert verify_app_mcp_token(token) is False

    # ── Phase 8: and back ────────────────────────────────────────────────
    update_my_channel(client, headers, channel_id, is_enabled=True)
    assert verify_app_mcp_token(token) is True


def test_availability_is_cached_but_not_permanently(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """The cache is real, and it is the only thing standing between a
    revocation and its effect:

    1. With a long TTL, a first verification resolves and caches "available"
    2. The admin disables the channel — the cached answer still stands, which
       is exactly the documented revocation delay
    3. Expiring the cache (what the TTL does in production) refuses the same
       token, proving the stale answer was a cache entry and not a missing
       check

    Written with an explicit reset rather than a `sleep(TTL)`: the property
    under test is "the cached answer is re-derived once it stops being fresh",
    and sleeping through a real TTL would make this the slowest test in the
    suite to assert exactly the same thing.
    """
    _user, headers = create_random_user_with_headers(client)
    token = obtain_app_mcp_access_token(client, headers)
    channel_id = find_server_channel_by_type(
        client, superuser_token_headers, _APP_MCP
    )["id"]

    with patch(_TTL_SETTING, 300):
        # ── Phase 1: populate the cache ──────────────────────────────────
        assert verify_app_mcp_token(token) is True

        # ── Phase 2: the switch is thrown, the cached answer stands ──────
        update_server_channel(
            client, superuser_token_headers, channel_id, enabled=False
        )
        assert verify_app_mcp_token(token) is True, (
            "expected the cached answer to survive within the TTL — if this "
            "fails, the cache is not being consulted at all"
        )

        # ── Phase 3: once it is no longer fresh, the switch bites ────────
        reset_app_mcp_availability_cache()
        assert verify_app_mcp_token(token) is False


def test_a_forged_token_is_refused_the_same_way(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    no_availability_cache,
) -> None:
    """An unknown token and a disallowed one are indistinguishable.

    1. A string that was never issued is refused
    2. A real token whose owner is not granted a restricted channel is refused
    3. Both are the same `None` — the caller learns nothing about which

    The negative half is what keeps the availability check from becoming a
    probe: if a refusal told the caller "your token is valid, the channel is
    off", the App MCP endpoint would answer questions about a server's
    configuration to anyone holding any token.
    """
    user, headers = create_random_user_with_headers(client)
    token = obtain_app_mcp_access_token(client, headers)
    channel_id = find_server_channel_by_type(
        client, superuser_token_headers, _APP_MCP
    )["id"]

    # ── Phase 1: never-issued token ──────────────────────────────────────
    assert verify_app_mcp_token("not-a-token-this-server-ever-minted") is False

    # ── Phase 2: real token, no access ───────────────────────────────────
    update_server_channel(
        client, superuser_token_headers, channel_id, visibility="restricted"
    )
    assert verify_app_mcp_token(token) is False

    # ── Phase 3: the grant is what separates them, and it works ──────────
    replace_channel_grants(client, superuser_token_headers, channel_id, [user["id"]])
    assert verify_app_mcp_token(token) is True
    assert verify_app_mcp_token("not-a-token-this-server-ever-minted") is False


def test_the_ttl_is_a_real_deadline_and_not_only_a_reset(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """The revocation delay is the TTL, so the TTL has to actually expire.

    The scenario above proves the cache is consulted and that an emptied cache
    re-derives — but `reset_availability_cache()` is documented as *not* being
    the revocation mechanism, so neither of those exercises
    `entry.expires_at <= now`. Break that comparison (write entries a billion
    seconds into the future, a plausible unit slip) and both still pass, while
    the admin kill switch quietly becomes advisory until the process restarts.

    So this one moves the clock instead of the cache:

    1. A verification at a pinned instant caches "available"
    2. The admin disables the channel — nothing is cleared or reset
    3. One second before the entry expires, the token still works
    4. At the expiry instant it is refused

    The clock is patched rather than slept through because the TTL is an
    `int`: the shortest window a sleeping version could observe is a second,
    and it would pay that second on every run of the suite.
    """
    ttl = 60
    start = 1_000.0
    clock = {"now": start}

    _user, headers = create_random_user_with_headers(client)
    token = obtain_app_mcp_access_token(client, headers)
    channel_id = find_server_channel_by_type(
        client, superuser_token_headers, _APP_MCP
    )["id"]

    # ── Phase 1: cache "available" at a known instant ────────────────────
    with patch(_TTL_SETTING, ttl):
        with patch(_MONOTONIC, lambda: clock["now"]):
            assert verify_app_mcp_token(token) is True

    # ── Phase 2: the switch is thrown, on the real clock ─────────────────
    # Outside the patch on purpose: this is ordinary HTTP, and freezing the
    # clock underneath a request is not part of what is being tested.
    update_server_channel(client, superuser_token_headers, channel_id, enabled=False)

    with patch(_TTL_SETTING, ttl):
        with patch(_MONOTONIC, lambda: clock["now"]):
            # ── Phase 3: one second inside the window ────────────────────
            clock["now"] = start + ttl - 1
            assert verify_app_mcp_token(token) is True, (
                "the cached answer expired early — the revocation delay is "
                "supposed to be exactly the TTL"
            )

            # ── Phase 4: the deadline itself ─────────────────────────────
            clock["now"] = start + ttl
            assert verify_app_mcp_token(token) is False, (
                "the cached answer outlived its TTL: nothing but a process "
                "restart would ever apply the admin's kill switch"
            )


def test_a_failed_availability_lookup_denies_and_is_not_cached(
    client: TestClient,
) -> None:
    """An answer that could not be computed denies, and is not an answer.

    1. Availability resolution is made to raise — the token is refused
    2. The fault clears, and the very next verification is accepted

    Phase 1 is the property the whole design rests on: a lookup error that
    returned "available" would make the kill switch advisory whenever the
    database is unhappy, which is when an operator is most likely to be
    reaching for it.

    Phase 2 is the other half of the same rule, and the one that is easy to
    lose. A failure stored as a denial keeps refusing this user for the rest
    of the TTL — every retry answered from memory, never touching the database
    that recovered milliseconds later. The TTL is set long here so a
    regression cannot hide behind a short window.
    """
    _user, headers = create_random_user_with_headers(client)
    token = obtain_app_mcp_access_token(client, headers)

    with patch(_TTL_SETTING, 300):
        # ── Phase 1: the lookup faults, and the answer is "no" ───────────
        with patch(
            _POLICY_RESOLVE, side_effect=RuntimeError("channel policy unavailable")
        ):
            assert verify_app_mcp_token(token) is False, (
                "a lookup that raises must deny — a kill switch that stops "
                "working when the database does is not a kill switch"
            )

        # ── Phase 2: the fault clears, and so does the refusal ───────────
        assert verify_app_mcp_token(token) is True, (
            "the failure was cached as a denial: this user stays refused for "
            "a full TTL after the lookup recovered, without the database "
            "being asked again"
        )


def test_the_verifier_materializes_the_channel_row_itself(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    no_availability_cache,
) -> None:
    """A deployment where nobody ever opened the Channels tab still works.

    Every other scenario here reaches the channel through a listing endpoint
    first, and the listing materializes the row — so none of them would notice
    if the verifier had come to depend on that having happened. The whole
    argument for materializing lazily rather than seeding at boot is that the
    verifier does not depend on anything else having run.

    1. A token is minted without any channel surface being read
    2. The verifier accepts it, which it can only do by materializing the row
    3. The admin list then returns exactly one App MCP channel, created before
       that list was ever called
    """
    _user, headers = create_random_user_with_headers(client)
    token = obtain_app_mcp_access_token(client, headers)

    # ── Phase 1+2: nothing above has listed channels ─────────────────────
    assert verify_app_mcp_token(token) is True

    # ── Phase 3: the row predates the first listing ──────────────────────
    # `find_server_channel_by_type` also asserts there is exactly one, so a
    # verifier that materialized a *second* row would fail here too.
    before_first_listing = datetime.now(UTC).replace(tzinfo=None)
    channel = find_server_channel_by_type(client, superuser_token_headers, _APP_MCP)
    created_at = datetime.fromisoformat(channel["created_at"])
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(UTC).replace(tzinfo=None)
    assert created_at <= before_first_listing, (
        "the App MCP row was created by the admin listing rather than by the "
        "verification above — the verifier is relying on some other surface "
        "having run first"
    )
