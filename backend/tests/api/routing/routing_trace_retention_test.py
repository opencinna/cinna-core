"""The `ROUTING_TRACE_RETENTION_DAYS` matrix (plan §4, §11a Rule 1).

Two surfaces enforce the same contract and both are covered here:

  A. Settings validation (`app.core.config.Settings._validate_routing_trace_retention`)
     — rejects the bad value *before* the app can even start.
  B. `RoutingTraceService.purge()` — the runtime enforcement, reachable via
     `tests/utils/routing.py::purge_routing_traces` (a documented Rule-1
     exemption: the purge scheduler is `TESTING`-gated like every other
     scheduler in this project, so there is no HTTP surface that triggers a
     purge on demand).

**The trap this file exists to not fall into** (see the plan's §11a Rule 1 and
`RoutingTraceService.purge`'s own docstring): an earlier version of `purge()`
read `retention_days <= 0` as "keep forever" and returned `0`. That is now a
bug shape, not a behavior — `0` is rejected at settings validation, and
`purge(db, retention_days=0)` **raises** `ValueError` rather than returning
`0`. A test asserting `purge(db, 0) == 0` would pass against that bug. Every
test below for `0` (and other negatives) asserts the raise, never the return
value, and is named to say so.

`-1` — `ROUTING_TRACE_RETENTION_FOREVER` — is the *only* spelling of "keep
forever", both at settings-validation time and at `purge()` call time.

Seeding uses `tests/utils/routing.py::seed_routing_trace`, the other
documented Rule-1 exemption in this domain: `RoutingDecision.created_at` is
server-assigned and there is no route that lets a caller backdate a decision,
which is exactly the axis `purge`'s `created_at < cutoff` boundary needs to
be tested against.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session

from app.core.config import ROUTING_TRACE_RETENTION_FOREVER
from tests.utils.routing import get_routing_trace, purge_routing_traces, seed_routing_trace

# `app.core.config` is the one `app.*` module the test-suite rules already
# carve out (tests/README.md Rule 1: "app.core.config.settings ... for config
# values"). `Settings` itself (the class, used below to construct a fresh,
# unvalidated-until-construction instance) is imported lazily inside each
# settings test, matching the existing precedent in
# tests/unit/test_default_user_role_service.py — constructing `Settings()` is
# the only way to exercise a `model_validator` that runs once, at startup;
# there is no HTTP route that re-validates configuration.


def _minimal_settings_kwargs() -> dict:
    """Enough required fields to construct a fresh `Settings()` without
    tripping unrelated validators (e.g. the `SECRET_KEY != "changethis"`
    check). Templated off the live, already-validated settings singleton
    (`app.core.config.settings`) — the same technique
    `tests/unit/test_default_user_role_service.py` uses — so this doesn't
    have to enumerate every required field by hand and doesn't break when a
    new required field is added elsewhere.

    Constructing a fresh `Settings` instance is the only way to exercise a
    `model_validator` that runs once, at startup — there is no HTTP route
    that re-validates configuration. `app.core.config` is the one `app.*`
    module the test-suite rules already carve out (see `tests/README.md`
    Rule 1: "app.core.config.settings ... for config values").
    """
    from app.core.config import settings as live_settings

    return {
        "PROJECT_NAME": live_settings.PROJECT_NAME,
        "POSTGRES_SERVER": live_settings.POSTGRES_SERVER,
        "POSTGRES_PORT": live_settings.POSTGRES_PORT,
        "POSTGRES_USER": live_settings.POSTGRES_USER,
        "POSTGRES_PASSWORD": live_settings.POSTGRES_PASSWORD,
        "POSTGRES_DB": live_settings.POSTGRES_DB,
        "FIRST_SUPERUSER": live_settings.FIRST_SUPERUSER,
        "FIRST_SUPERUSER_PASSWORD": live_settings.FIRST_SUPERUSER_PASSWORD,
        "SECRET_KEY": "test-secret-key-that-is-long-enough-for-testing",
        "ENCRYPTION_KEY": "test-encryption-key-that-is-long-enough-ok",
    }


# ── A. Settings validation ──────────────────────────────────────────────


@pytest.mark.parametrize("days", [1, 14, 100])
def test_settings_accepts_any_retention_of_at_least_one_day(days: int) -> None:
    from app.core.config import Settings

    s = Settings(**_minimal_settings_kwargs(), ROUTING_TRACE_RETENTION_DAYS=days)
    assert s.ROUTING_TRACE_RETENTION_DAYS == days


def test_settings_accepts_exactly_minus_one_as_the_keep_forever_sentinel() -> None:
    from app.core.config import Settings

    assert ROUTING_TRACE_RETENTION_FOREVER == -1
    s = Settings(
        **_minimal_settings_kwargs(),
        ROUTING_TRACE_RETENTION_DAYS=ROUTING_TRACE_RETENTION_FOREVER,
    )
    assert s.ROUTING_TRACE_RETENTION_DAYS == ROUTING_TRACE_RETENTION_FOREVER


def test_settings_rejects_zero_rather_than_reading_it_as_keep_forever() -> None:
    """The Rule 1 trap, at the settings layer: `0` used to mean "keep
    forever". It must now be rejected outright — an operator who typed `0`
    meaning "don't retain this" must be told they were about to get the
    opposite, not have their input silently reinterpreted."""
    from app.core.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(**_minimal_settings_kwargs(), ROUTING_TRACE_RETENTION_DAYS=0)

    message = str(exc_info.value)
    # The error must name -1 (the only correct spelling of "keep forever")...
    assert "-1" in message
    # ...and the two settings that actually express "store nothing", so an
    # operator who meant that is told how to ask for it correctly.
    assert "ROUTING_TRACE_STORE_MESSAGE_TEXT" in message
    assert "ROUTING_TRACE_ENABLED" in message


@pytest.mark.parametrize("days", [-2, -5, -100])
def test_settings_rejects_negative_values_other_than_the_sentinel(days: int) -> None:
    from app.core.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(**_minimal_settings_kwargs(), ROUTING_TRACE_RETENTION_DAYS=days)
    assert "-1" in str(exc_info.value)


# ── B. RoutingTraceService.purge() ──────────────────────────────────────


def test_purge_with_forever_sentinel_spares_every_row_and_returns_zero(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """`-1` must not be reachable by computing a cutoff at all — it is a
    short-circuit, not "a cutoff so old nothing matches". Proven
    behaviorally: an old-enough-to-normally-purge row and a fresh row both
    survive, and the reported delete count is 0."""
    now = datetime.now(UTC)
    old_id = seed_routing_trace(created_at=now - timedelta(days=400))
    new_id = seed_routing_trace(created_at=now - timedelta(hours=1))
    assert old_id is not None and new_id is not None

    deleted = purge_routing_traces(db, retention_days=ROUTING_TRACE_RETENTION_FOREVER)
    assert deleted == 0

    get_routing_trace(client, superuser_token_headers, str(old_id))
    get_routing_trace(client, superuser_token_headers, str(new_id))


def test_purge_with_positive_retention_deletes_only_rows_older_than_the_cutoff(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """A 7-day window deletes a 10-day-old row and spares a 1-hour-old one,
    and the returned count matches exactly what was deleted."""
    now = datetime.now(UTC)
    old_id = seed_routing_trace(created_at=now - timedelta(days=10))
    new_id = seed_routing_trace(created_at=now - timedelta(hours=1))
    assert old_id is not None and new_id is not None

    deleted = purge_routing_traces(db, retention_days=7)
    assert deleted == 1

    get_routing_trace(client, superuser_token_headers, str(old_id), expected_status=404)
    get_routing_trace(client, superuser_token_headers, str(new_id), expected_status=200)


def test_purge_with_retention_days_zero_raises_rather_than_meaning_keep_everything(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """THE TRAP: `purge(db, retention_days=0)` must RAISE `ValueError`, not
    return `0`. An earlier version of `purge()` treated `retention_days <= 0`
    as "keep forever" and returned `0` — indistinguishable, from the return
    value alone, from "nothing was old enough to delete". Asserting
    `purge(db, 0) == 0` would pass against that exact bug, so this test
    asserts the raise, and — to prove nothing was silently deleted on the way
    to raising — that a row old enough to have been purged under any positive
    window is still there afterwards."""
    now = datetime.now(UTC)
    old_id = seed_routing_trace(created_at=now - timedelta(days=30))
    assert old_id is not None

    with pytest.raises(ValueError, match="-1"):
        purge_routing_traces(db, retention_days=0)

    get_routing_trace(client, superuser_token_headers, str(old_id), expected_status=200)


@pytest.mark.parametrize("days", [-2, -5, -100])
def test_purge_with_negative_retention_other_than_the_sentinel_raises(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    days: int,
) -> None:
    """Same trap as `0`, for every other negative value: only `-1` means
    keep-forever. Anything else negative is a programming error the settings
    validator should have already made unreachable from configuration, so
    `purge` raises rather than inventing a reading for it."""
    now = datetime.now(UTC)
    old_id = seed_routing_trace(created_at=now - timedelta(days=30))
    assert old_id is not None

    with pytest.raises(ValueError, match="-1"):
        purge_routing_traces(db, retention_days=days)

    get_routing_trace(client, superuser_token_headers, str(old_id), expected_status=200)
