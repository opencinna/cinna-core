"""Bundle auto-update convergence + check-updates enrichment.

Covers the ``bundle_auto_update_and_install_ux`` plan (see
``docs/plans/bundle_auto_update_and_install_ux_plan.md``, sections 3 & 4):

  - ``InstallService.sweep_automatic_updates`` — the shared selection +
    application function that converges an automatic-mode install whose
    environment is not "live" (suspended / stopped / no environment at all),
    closing the gap where an install whose env was *already* suspended when a
    revision was published never converged.
  - ``PublishService.notify_installs``'s publish-time fast path, which fires
    the bundle-scoped sweep as a background task so an already-suspended
    install converges within the same publish's ``drain_tasks()`` instead of
    waiting for the (untestable-under-TESTING) periodic scheduler.
  - The enriched ``check_for_updates`` response (``latest_release_notes`` /
    ``latest_published_at``).

Triggering the sweep in tests: ``sweep_automatic_updates`` has exactly two
production entry points — the periodic ``bundle_auto_update_scheduler``
(registered only when ``not settings.TESTING``, per ``main.py``) and the
publish-time fast path inside ``PublishService.notify_installs``. The
periodic scheduler never runs under the test harness, and
``sweep_leader_session()`` itself short-circuits to ``create_session()``
under ``settings.TESTING`` (the real ``pg_try_advisory_lock`` path is not
exercised — there is no cross-process concurrency to guard against in a
single test transaction). So **every** scenario below is driven through an
actual ``POST /agents/{id}/publish`` call followed by ``drain_tasks()``,
never through a direct service-layer invocation of the sweep.

Two internal bookkeeping columns the sweep reads have no public API seam:
``Agent.installed_revision_id`` can only move forward via ``apply_update`` /
``PublishService.publish`` (the publisher's own install is kept in permanent
lockstep with the bundle's latest revision, so it can never itself be
"behind"), and ``Agent.last_update_attempt_at`` is deliberately not exposed
on ``AgentPublic``. ``force_install_revision`` / ``stamp_install_update_failure``
in ``tests/utils/bundle.py`` poke these two columns directly, mirroring the
documented DB-seam helpers already established in ``tests/utils/environment.py``
(``set_environment_status``) for state the API cannot otherwise produce.
"""
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    force_install_revision,
    install_bundle as _install,
    make_user_and_headers as _make_user_and_headers,
    publish_bundle_and_make_public as _publish_and_make_public,
    publish_bundle_revision as _publish,
    set_update_mode,
    stamp_install_update_failure,
)
from tests.utils.environment import (
    delete_environment,
    get_environment,
    set_environment_status,
)

API = settings.API_V1_STR


# ── Module-level helpers ───────────────────────────────────────────────────


def _get_agent(client: TestClient, headers: dict[str, str], agent_id: str) -> dict:
    r = client.get(f"{API}/agents/{agent_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _setup_consumer_install(
    client: TestClient,
    publisher_headers: dict[str, str],
    *,
    name: str = "Auto-Update Bundle",
) -> tuple[dict, dict, dict[str, str]]:
    """Publish revision 1 as public, install it as a fresh consumer.

    Returns ``(fresh_publisher_agent, install, consumer_headers)``. The
    install's ``update_mode`` is left at its default (``manual``) — callers
    that want to exercise the sweep flip it via ``set_update_mode``.
    """
    publisher_agent = create_agent_via_api(client, publisher_headers, name=name)
    drain_tasks()
    _publish_and_make_public(client, publisher_headers, publisher_agent["id"], notes="v1")
    fresh_publisher = _get_agent(client, publisher_headers, publisher_agent["id"])

    _, consumer_headers = _make_user_and_headers(client)
    install = _install(client, consumer_headers, fresh_publisher["bundle_id"])
    return fresh_publisher, install, consumer_headers


# ── Scenarios 1 & 9: suspended env converges via the publish fast path ────
#
# These two plan scenarios collapse into one test: there is no separate "run
# the sweep" call to make — the fast path IS the only way to converge an
# already-suspended install in this test harness, so proving the state
# transition (scenario 1) and proving the mechanism (scenario 9) are the same
# assertion.


def test_publish_fast_path_converges_suspended_automatic_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Suspended env + automatic + behind revision:
      1. Consumer installs (rev 1), flips to automatic, env is suspended.
      2. Publisher publishes rev 2 → publish-time fast path fires the
         bundle-scoped sweep as a background task, drained by drain_tasks()
         inside publish_bundle_revision.
      3. installed_revision_id advances to rev 2, pending_update clears,
         last_update_status='synced'.
      4. The environment stays suspended — apply_update never starts it
         because ``was_running`` was False going in.
    """
    publisher, install, consumer_headers = _setup_consumer_install(
        client, superuser_token_headers
    )
    install_id = install["id"]
    env_id = install["active_environment_id"]
    assert env_id is not None

    set_update_mode(client, consumer_headers, install_id, "automatic")
    set_environment_status(db, env_id, "suspended")

    revision2 = _publish(client, superuser_token_headers, publisher["id"], notes="v2")

    updated = _get_agent(client, consumer_headers, install_id)
    assert updated["installed_revision_id"] == revision2["id"]
    assert updated["installed_revision_number"] == 2
    assert updated["pending_update"] is False
    assert updated["last_update_status"] == "synced"

    env = get_environment(client, consumer_headers, env_id)
    assert env["status"] == "suspended"


# ── Scenario 2: no environment at all → DB-only apply ─────────────────────


def test_sweep_applies_db_only_when_install_has_no_environment(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Install with no environment + automatic + behind → applied (DB-only path):
      1. Consumer installs, flips to automatic, then deletes their environment
         (DELETE /environments/{id} nulls Agent.active_environment_id).
      2. Publisher publishes rev 2.
      3. installed_revision_id advances even though there is no environment to
         copy content into — apply_update's env=None branch is a DB-only
         update of the install row.
    """
    publisher, install, consumer_headers = _setup_consumer_install(
        client, superuser_token_headers
    )
    install_id = install["id"]
    env_id = install["active_environment_id"]
    assert env_id is not None

    set_update_mode(client, consumer_headers, install_id, "automatic")
    delete_environment(client, consumer_headers, env_id)

    detached = _get_agent(client, consumer_headers, install_id)
    assert detached["active_environment_id"] is None

    revision2 = _publish(client, superuser_token_headers, publisher["id"], notes="v2")

    updated = _get_agent(client, consumer_headers, install_id)
    assert updated["installed_revision_id"] == revision2["id"]
    assert updated["installed_revision_number"] == 2
    assert updated["pending_update"] is False
    assert updated["last_update_status"] == "synced"
    assert updated["active_environment_id"] is None


# ── Scenario 3: stopped env → applied ──────────────────────────────────────


def test_sweep_applies_when_env_is_stopped(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """``stopped`` is in AUTO_UPDATE_ALLOWED_ENV_STATUSES alongside ``suspended``."""
    publisher, install, consumer_headers = _setup_consumer_install(
        client, superuser_token_headers
    )
    install_id = install["id"]
    env_id = install["active_environment_id"]

    set_update_mode(client, consumer_headers, install_id, "automatic")
    set_environment_status(db, env_id, "stopped")

    revision2 = _publish(client, superuser_token_headers, publisher["id"], notes="v2")

    updated = _get_agent(client, consumer_headers, install_id)
    assert updated["installed_revision_id"] == revision2["id"]
    assert updated["pending_update"] is False
    assert updated["last_update_status"] == "synced"


# ── Scenario 4: running env → not touched ──────────────────────────────────


def test_sweep_skips_running_env(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A running env is never touched by the sweep — applying mid-stream would
    disrupt a live session. ``notify_installs`` still marks pending_update
    (that marking is unconditional; only the sweep's *application* is gated).
    """
    publisher, install, consumer_headers = _setup_consumer_install(
        client, superuser_token_headers
    )
    install_id = install["id"]

    set_update_mode(client, consumer_headers, install_id, "automatic")
    running_env = get_environment(client, consumer_headers, install["active_environment_id"])
    assert running_env["status"] == "running"

    revision2 = _publish(client, superuser_token_headers, publisher["id"], notes="v2")
    assert revision2["revision_number"] == 2

    updated = _get_agent(client, consumer_headers, install_id)
    assert updated["installed_revision_id"] == install["installed_revision_id"]
    assert updated["installed_revision_number"] == 1
    assert updated["pending_update"] is True

    env_after = get_environment(client, consumer_headers, install["active_environment_id"])
    assert env_after["status"] == "running"


# ── Scenario 5: transitional status → not touched ──────────────────────────


def test_sweep_skips_transitional_env_status(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Every transitional status (creating/building/initializing/starting/
    rebuilding/activating) is outside the allowlist — exercised here with
    ``starting`` and ``building``, matching the plan's explicit call-out.
    """
    for status in ("starting", "building"):
        publisher, install, consumer_headers = _setup_consumer_install(
            client, superuser_token_headers, name=f"Transitional {status}"
        )
        install_id = install["id"]
        env_id = install["active_environment_id"]

        set_update_mode(client, consumer_headers, install_id, "automatic")
        set_environment_status(db, env_id, status)

        revision2 = _publish(client, superuser_token_headers, publisher["id"], notes="v2")
        assert revision2["revision_number"] == 2

        updated = _get_agent(client, consumer_headers, install_id)
        assert updated["installed_revision_id"] == install["installed_revision_id"], (
            f"status={status} must not be applied"
        )
        assert updated["pending_update"] is True


# ── Scenario 6: manual mode → untouched ────────────────────────────────────


def test_sweep_skips_manual_mode_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    A behind install left at the default ``manual`` update_mode is never a
    sweep candidate, even when its environment is otherwise eligible
    (suspended).
    """
    publisher, install, consumer_headers = _setup_consumer_install(
        client, superuser_token_headers
    )
    install_id = install["id"]
    env_id = install["active_environment_id"]
    assert install["update_mode"] == "manual"

    set_environment_status(db, env_id, "suspended")

    revision2 = _publish(client, superuser_token_headers, publisher["id"], notes="v2")
    assert revision2["revision_number"] == 2

    updated = _get_agent(client, consumer_headers, install_id)
    assert updated["update_mode"] == "manual"
    assert updated["installed_revision_id"] == install["installed_revision_id"]
    assert updated["installed_revision_number"] == 1
    assert updated["pending_update"] is True

    # The install owner can still update manually — proves the install itself
    # was otherwise perfectly eligible, it's the mode that excluded it.
    r = client.post(f"{API}/agents/{install_id}/apply-update", headers=consumer_headers)
    assert r.status_code == 200, r.text
    drain_tasks()
    manually_applied = _get_agent(client, consumer_headers, install_id)
    assert manually_applied["installed_revision_id"] == revision2["id"]


# ── Scenario 7: publisher install → untouched ──────────────────────────────


def test_sweep_never_touches_publisher_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    The sweep's selection query filters ``Agent.is_publisher_install == False``
    — as does ``notify_installs``'s pending-update marking loop.

    ``PublishService.publish`` unconditionally resyncs the publishing
    install's own ``installed_revision_id`` to the revision it just created,
    *before* ``notify_installs`` runs — so under normal operation the
    publisher's own install can never actually be "behind" at the moment its
    own bundle's sweep fires. We additionally force it artificially behind via
    the DB seam (mirroring a hypothetical drift) to prove the exclusion holds
    even when the row would otherwise look exactly like a real candidate
    (automatic mode, suspended env, revision mismatch) — and use a sibling
    foreign install on the same bundle, converged in the very same
    publish/drain, to prove the sweep was genuinely active and had the
    opportunity to reach it.
    """
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Publisher Excluded"
    )
    drain_tasks()
    revision1 = _publish_and_make_public(
        client, superuser_token_headers, publisher_agent["id"], notes="v1"
    )
    fresh1 = _get_agent(client, superuser_token_headers, publisher_agent["id"])
    assert fresh1["is_publisher_install"] is True
    pub_env_id = fresh1["active_environment_id"]

    set_update_mode(client, superuser_token_headers, publisher_agent["id"], "automatic")
    set_environment_status(db, pub_env_id, "suspended")

    # A sibling foreign consumer on the same bundle — automatic + suspended —
    # gives the sweep real work in the same drain, proving it executed.
    _, consumer_headers = _make_user_and_headers(client)
    consumer_install = _install(client, consumer_headers, fresh1["bundle_id"])
    set_update_mode(client, consumer_headers, consumer_install["id"], "automatic")
    set_environment_status(db, consumer_install["active_environment_id"], "suspended")

    revision2 = _publish(client, superuser_token_headers, publisher_agent["id"], notes="v2")

    consumer_after_v2 = _get_agent(client, consumer_headers, consumer_install["id"])
    assert consumer_after_v2["installed_revision_id"] == revision2["id"], (
        "sibling consumer must converge — proves the sweep ran"
    )

    pub_after_v2 = _get_agent(client, superuser_token_headers, publisher_agent["id"])
    assert pub_after_v2["installed_revision_id"] == revision2["id"]
    assert pub_after_v2["pending_update"] is False

    # Force the publisher's own install artificially behind (needs a real FK
    # target — rev1 — since installed_revision_id has a DB foreign key).
    force_install_revision(db, publisher_agent["id"], revision1["id"])
    stale = _get_agent(client, superuser_token_headers, publisher_agent["id"])
    assert stale["installed_revision_id"] == revision1["id"]

    # Re-arm the sibling consumer so it's a real candidate again too.
    set_update_mode(client, consumer_headers, consumer_install["id"], "automatic")
    set_environment_status(db, consumer_install["active_environment_id"], "suspended")

    revision3 = _publish(client, superuser_token_headers, publisher_agent["id"], notes="v3")

    consumer_final = _get_agent(client, consumer_headers, consumer_install["id"])
    assert consumer_final["installed_revision_id"] == revision3["id"], (
        "sibling consumer converges again — the sweep executed during this "
        "publish too"
    )

    pub_final = _get_agent(client, superuser_token_headers, publisher_agent["id"])
    # The publisher's own install converges to rev3 via PublishService.publish's
    # own direct assignment (the same statement that ran for v1/v2), NOT via
    # the sweep — is_publisher_install excludes it from ever being a sweep
    # candidate for its own bundle.
    assert pub_final["installed_revision_id"] == revision3["id"]
    assert pub_final["pending_update"] is False


# ── Scenario 8: failure backoff defers a retry ─────────────────────────────


def test_sweep_defers_failed_install_within_backoff_window(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    ``last_update_status='failed'`` with a recent ``last_update_attempt_at``
    (inside ``BUNDLE_AUTO_UPDATE_RETRY_BACKOFF_HOURS``, default 6h) is
    deferred rather than retried on every sweep. Once the attempt is old
    enough to fall outside the backoff window, the same install converges on
    the next sweep.
    """
    publisher, install, consumer_headers = _setup_consumer_install(
        client, superuser_token_headers
    )
    install_id = install["id"]
    env_id = install["active_environment_id"]

    set_update_mode(client, consumer_headers, install_id, "automatic")
    set_environment_status(db, env_id, "suspended")
    stamp_install_update_failure(db, install_id, hours_ago=1)  # inside the 6h window

    revision2 = _publish(client, superuser_token_headers, publisher["id"], notes="v2")
    assert revision2["revision_number"] == 2

    deferred = _get_agent(client, consumer_headers, install_id)
    assert deferred["installed_revision_id"] == install["installed_revision_id"], (
        "deferred install must NOT be retried inside the backoff window"
    )
    assert deferred["installed_revision_number"] == 1
    assert deferred["pending_update"] is True
    assert deferred["last_update_status"] == "failed"

    # Backoff has now (simulated-)expired — the same install retries and converges.
    stamp_install_update_failure(db, install_id, hours_ago=7)  # outside the 6h window
    set_environment_status(db, env_id, "suspended")  # still eligible

    revision3 = _publish(client, superuser_token_headers, publisher["id"], notes="v3")

    retried = _get_agent(client, consumer_headers, install_id)
    assert retried["installed_revision_id"] == revision3["id"]
    assert retried["pending_update"] is False
    assert retried["last_update_status"] == "synced"


# ── Scenario 10: check_for_updates enrichment ──────────────────────────────


def test_check_updates_returns_release_notes_and_published_at(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    ``latest_release_notes`` / ``latest_published_at`` are read straight off
    the resolved latest revision row, so they're populated whenever the
    bundle has a latest revision — independent of whether the install is
    pending or already caught up.
    """
    publisher, install, consumer_headers = _setup_consumer_install(
        client, superuser_token_headers
    )
    install_id = install["id"]

    revision2 = _publish(
        client, superuser_token_headers, publisher["id"], notes="Fixed the frobnicator"
    )

    r = client.post(f"{API}/agents/{install_id}/check-updates", headers=consumer_headers)
    assert r.status_code == 200, r.text
    check = r.json()
    assert check["pending_update"] is True
    assert check["installed_revision_number"] == 1
    assert check["latest_revision_number"] == 2
    assert check["latest_release_notes"] == "Fixed the frobnicator"
    assert check["latest_published_at"] is not None
    assert check["latest_published_at"] == revision2["published_at"]

    # Catching up (manual apply-update) doesn't change the latest_* fields —
    # they still describe the (now installed) latest revision.
    r = client.post(f"{API}/agents/{install_id}/apply-update", headers=consumer_headers)
    assert r.status_code == 200, r.text
    drain_tasks()

    r = client.post(f"{API}/agents/{install_id}/check-updates", headers=consumer_headers)
    assert r.status_code == 200, r.text
    check_after = r.json()
    assert check_after["pending_update"] is False
    assert check_after["installed_revision_number"] == 2
    assert check_after["latest_release_notes"] == "Fixed the frobnicator"
    assert check_after["latest_published_at"] == check["latest_published_at"]
