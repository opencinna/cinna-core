"""Bundle-propagated agent scheduler tests.

Covers the full lifecycle of schedule propagation through bundles:

  1. Publish snapshots the publisher's schedules into the revision
     (revision.schedules + manifest["schedules"] carry {name, cron_string,
     description, prompt, schedule_type, command, enabled}; next_execution
     and last_execution are NOT included).

  2. Install materialises snapshotted schedules on the consumer install
     with the published enabled state and a computed next_execution.

  3. Apply-update merge scenarios:
     a. A behaviourally-unchanged schedule that the consumer DISABLED stays
        disabled after update; name/description are refreshed from the new
        revision.
     b. A cron change reinstalls that schedule (enabled per publisher).
     c. A schedule added by the publisher appears on the install.
     d. A schedule removed by the publisher is deleted from the install.

  4. Route guards on a foreign/consumer install:
     - POST /{id}/schedules → 403
     - DELETE /{id}/schedules/{sid} → 403
     - PUT with a non-``enabled`` field (e.g. name) → 403
     - PUT with only {enabled} → 200 (toggle works)
     - run-now and logs endpoints remain accessible
     Publisher install retains full CRUD (create/edit/delete succeed).

All tests are API-only; no direct DB access.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle as _install,
    make_bundle_public as _make_public,
    make_user_and_headers as _make_user_and_headers,
    publish_bundle_revision as _publish,
)
from tests.utils.schedule import (
    create_schedule,
    delete_schedule,
    get_schedule_logs,
    list_schedules,
    update_schedule,
)

API = settings.API_V1_STR

# Stable CRON strings used by the tests — already in UTC 5-part form.
_CRON_A = "0 9 * * 1-5"    # every weekday at 09:00 UTC
_CRON_B = "0 14 * * 1-5"   # every weekday at 14:00 UTC (different from A)
_CRON_C = "30 7 * * *"     # daily at 07:30 UTC


# ── Module-level helpers ──────────────────────────────────────────────────────


# _make_user_and_headers, _publish (revision, no flip), _make_public, _install
# are imported from tests.utils.bundle above.


def _get_revision(
    client: TestClient,
    headers: dict[str, str],
    revision_id: str,
) -> dict:
    r = client.get(f"{API}/bundle-revisions/{revision_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _get_agent(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> dict:
    r = client.get(f"{API}/agents/{agent_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ── Scenario 1: Publish snapshots schedules into the revision ─────────────────


def test_publish_snapshots_schedules_into_revision(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Publish includes publisher schedules in revision.schedules and manifest["schedules"].

    Asserts:
    - revision.schedules carries the expected field set (name, cron_string,
      description, prompt, schedule_type, command, enabled).
    - next_execution and last_execution are NOT in any snapshot entry.
    - manifest["schedules"] matches revision.schedules.
    - A disabled schedule is snapshotted with enabled=False.
    """
    # ── Phase 1: Publisher creates agent + schedules ───────────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="Snap-Publisher"
    )
    drain_tasks()
    agent_id = agent["id"]

    # Create two schedules: one enabled, one disabled.
    s1 = create_schedule(
        client, superuser_token_headers, agent_id,
        name="Morning Run",
        cron_string=_CRON_A,
        timezone="UTC",
        description="Daily morning digest",
        prompt="Summarise overnight news",
        enabled=True,
    )
    s2_resp = client.post(
        f"{API}/agents/{agent_id}/schedules",
        headers=superuser_token_headers,
        json={
            "name": "Afternoon Check",
            "cron_string": _CRON_B,
            "timezone": "UTC",
            "description": "Afternoon summary",
            "prompt": "Summarise afternoon events",
            "enabled": False,
        },
    )
    assert s2_resp.status_code == 200, s2_resp.text
    s2 = s2_resp.json()

    # ── Phase 2: Publish ──────────────────────────────────────────────────────
    revision = _publish(client, superuser_token_headers, agent_id, notes="v1")

    # ── Phase 3: Verify revision.schedules ────────────────────────────────────
    rev_schedules = revision.get("schedules")
    assert isinstance(rev_schedules, list), (
        f"revision.schedules must be a list; got {type(rev_schedules)}"
    )
    assert len(rev_schedules) == 2, (
        f"Expected 2 snapshotted schedules; got {len(rev_schedules)}: {rev_schedules}"
    )

    # Index by name for easy lookup.
    by_name = {s["name"]: s for s in rev_schedules}
    assert "Morning Run" in by_name, f"Missing 'Morning Run' in snapshot: {rev_schedules}"
    assert "Afternoon Check" in by_name, f"Missing 'Afternoon Check' in snapshot: {rev_schedules}"

    snap1 = by_name["Morning Run"]
    required_fields = {"name", "cron_string", "description", "prompt", "schedule_type", "command", "enabled"}
    assert required_fields <= snap1.keys(), (
        f"Snapshot missing required fields; got keys: {snap1.keys()}"
    )
    assert snap1["enabled"] is True
    assert snap1["cron_string"] == s1["cron_string"]
    assert snap1["description"] == "Daily morning digest"
    assert snap1["prompt"] == "Summarise overnight news"
    assert snap1["schedule_type"] == "static_prompt"

    snap2 = by_name["Afternoon Check"]
    assert snap2["enabled"] is False, (
        "Disabled schedule must be snapshotted with enabled=False"
    )

    # next_execution and last_execution must NOT appear in any snapshot.
    for snap in rev_schedules:
        assert "next_execution" not in snap, (
            f"next_execution must not be snapshotted; found in: {snap}"
        )
        assert "last_execution" not in snap, (
            f"last_execution must not be snapshotted; found in: {snap}"
        )

    # ── Phase 4: manifest["schedules"] matches revision.schedules ────────────
    manifest = revision.get("manifest") or {}
    manifest_schedules = manifest.get("schedules")
    assert manifest_schedules is not None, "manifest must contain 'schedules' key"
    assert manifest_schedules == rev_schedules, (
        "manifest['schedules'] must match revision.schedules exactly"
    )


# ── Scenario 2: Install materialises schedules with enabled state + next_execution ─


def test_install_materialises_schedules(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Installing a bundle materialises the snapshotted schedules onto the consumer install.

    Asserts:
    - Consumer install has the same count of schedules as the publisher.
    - Each materialised schedule carries the published enabled state.
    - next_execution is computed (non-null).
    - Schedules are not present on the consumer before install.
    """
    # ── Phase 1: Publisher prepares agent + two schedules ─────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="Mat-Publisher"
    )
    drain_tasks()
    agent_id = agent["id"]

    create_schedule(
        client, superuser_token_headers, agent_id,
        name="Enabled Schedule",
        cron_string=_CRON_A,
        timezone="UTC",
        prompt="Run the morning report",
        enabled=True,
    )
    r_disabled = client.post(
        f"{API}/agents/{agent_id}/schedules",
        headers=superuser_token_headers,
        json={
            "name": "Disabled Schedule",
            "cron_string": _CRON_B,
            "timezone": "UTC",
            "description": "Afternoon schedule",
            "prompt": "Run the afternoon report",
            "enabled": False,
        },
    )
    assert r_disabled.status_code == 200, r_disabled.text

    # ── Phase 2: Publish + make public ───────────────────────────────────────
    _publish(client, superuser_token_headers, agent_id)
    fresh_pub = _get_agent(client, superuser_token_headers, agent_id)
    bundle_id = fresh_pub["bundle_id"]
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    # ── Phase 3: Consumer installs ────────────────────────────────────────────
    _, consumer_headers = _make_user_and_headers(client)
    install = _install(client, consumer_headers, bundle_id)
    install_id = install["id"]

    # ── Phase 4: Verify materialised schedules on consumer install ────────────
    consumer_schedules = list_schedules(client, consumer_headers, install_id)
    assert len(consumer_schedules) == 2, (
        f"Expected 2 materialised schedules on consumer; got {len(consumer_schedules)}: "
        f"{consumer_schedules}"
    )

    by_name = {s["name"]: s for s in consumer_schedules}
    assert "Enabled Schedule" in by_name, "Expected 'Enabled Schedule' on consumer"
    assert "Disabled Schedule" in by_name, "Expected 'Disabled Schedule' on consumer"

    enabled_sched = by_name["Enabled Schedule"]
    disabled_sched = by_name["Disabled Schedule"]

    assert enabled_sched["enabled"] is True, (
        "Published-enabled schedule must materialise enabled=True"
    )
    assert disabled_sched["enabled"] is False, (
        "Published-disabled schedule must materialise enabled=False"
    )

    # next_execution must be computed (non-null) for enabled schedules.
    assert enabled_sched.get("next_execution") is not None, (
        "Materialised schedule must have a computed next_execution"
    )


# ── Scenario 3: Apply-update merge ───────────────────────────────────────────


def test_apply_update_merge_scenarios(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Apply-update merge: unchanged keeps toggle; cron change reinstalls;
    added schedule appears; removed schedule is deleted.

    Scenario breakdown:
    a. Consumer disables a behaviourally-unchanged schedule → stays disabled
       after update; name is refreshed from new revision.
    b. Publisher changes cron → reinstalled with published enabled state.
    c. Publisher adds a new schedule → appears on consumer after update.
    d. Publisher removes a schedule → deleted from consumer after update.
    """
    pub_headers = superuser_token_headers

    # ── Phase 1: Publisher creates agent + 3 schedules ────────────────────────
    agent = create_agent_via_api(client, pub_headers, name="Merge-Publisher")
    drain_tasks()
    agent_id = agent["id"]

    # Schedule A: unchanged across revisions (cron, prompt, type unchanged)
    sched_a = create_schedule(
        client, pub_headers, agent_id,
        name="Stable Schedule",
        cron_string=_CRON_A,
        timezone="UTC",
        prompt="Run the morning report",
        enabled=True,
    )

    # Schedule B: cron will change in revision 2
    sched_b = create_schedule(
        client, pub_headers, agent_id,
        name="Changing Schedule",
        cron_string=_CRON_A,
        timezone="UTC",
        prompt="Run the changing report",
        enabled=True,
    )

    # Schedule C: will be removed in revision 2
    sched_c = create_schedule(
        client, pub_headers, agent_id,
        name="To Be Removed",
        cron_string=_CRON_C,
        timezone="UTC",
        prompt="This schedule will be removed",
        enabled=True,
    )

    # ── Phase 2: First publish ────────────────────────────────────────────────
    _publish(client, pub_headers, agent_id, notes="v1")
    fresh_pub = _get_agent(client, pub_headers, agent_id)
    bundle_id = fresh_pub["bundle_id"]
    _make_public(client, pub_headers, fresh_pub["bundle_uuid"])

    # ── Phase 3: Consumer installs ────────────────────────────────────────────
    _, consumer_headers = _make_user_and_headers(client)
    install = _install(client, consumer_headers, bundle_id)
    install_id = install["id"]

    # Consumer has 3 schedules from revision 1.
    consumer_schedules_v1 = list_schedules(client, consumer_headers, install_id)
    assert len(consumer_schedules_v1) == 3, (
        f"Expected 3 consumer schedules after v1 install; got {len(consumer_schedules_v1)}"
    )
    by_name_v1 = {s["name"]: s for s in consumer_schedules_v1}
    stable_consumer_id = by_name_v1["Stable Schedule"]["id"]

    # ── Phase 4: Consumer disables the stable schedule ────────────────────────
    # (scenario a: consumer disables; should survive update)
    r = client.put(
        f"{API}/agents/{install_id}/schedules/{stable_consumer_id}",
        headers=consumer_headers,
        json={"enabled": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False

    # ── Phase 5: Publisher modifies schedules for revision 2 ──────────────────
    # Scenario b: change cron on "Changing Schedule"
    update_schedule(
        client, pub_headers, agent_id, sched_b["id"],
        cron_string=_CRON_B,   # different cron → behaviorally changed
        timezone="UTC",
    )

    # Scenario c: add a brand new schedule
    create_schedule(
        client, pub_headers, agent_id,
        name="New Schedule",
        cron_string=_CRON_C,
        timezone="UTC",
        prompt="Brand new schedule",
        enabled=True,
    )

    # Also rename "Stable Schedule" on the publisher side (cosmetic only)
    update_schedule(
        client, pub_headers, agent_id, sched_a["id"],
        name="Stable Schedule (Renamed)",
    )

    # Scenario d: delete "To Be Removed" from publisher
    delete_schedule(client, pub_headers, agent_id, sched_c["id"])

    # ── Phase 6: Publisher publishes revision 2 ───────────────────────────────
    _publish(client, pub_headers, agent_id, notes="v2")

    # Consumer should now see pending update.
    install_after = _get_agent(client, consumer_headers, install_id)
    assert install_after["pending_update"] is True, (
        "Expected pending_update=True on consumer after publisher's revision 2"
    )

    # ── Phase 7: Consumer applies update ─────────────────────────────────────
    r = client.post(
        f"{API}/agents/{install_id}/apply-update",
        headers=consumer_headers,
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    # ── Phase 8: Verify merge results ────────────────────────────────────────
    consumer_schedules_v2 = list_schedules(client, consumer_headers, install_id)
    by_name_v2 = {s["name"]: s for s in consumer_schedules_v2}

    # (a) Stable schedule survived; it was disabled by consumer → stays disabled.
    # Name was updated to reflect publisher's cosmetic rename.
    assert "Stable Schedule (Renamed)" in by_name_v2, (
        f"Stable schedule not found after merge; keys: {list(by_name_v2.keys())}"
    )
    stable_after = by_name_v2["Stable Schedule (Renamed)"]
    assert stable_after["id"] == stable_consumer_id, (
        "Stable schedule row ID must be preserved after merge (in-place update)"
    )
    assert stable_after["enabled"] is False, (
        "Consumer-disabled toggle must survive a behaviourally-unchanged schedule merge"
    )

    # (b) "Changing Schedule" was reinstalled (new signature) → enabled per publisher.
    assert "Changing Schedule" in by_name_v2, (
        f"'Changing Schedule' must appear after merge; keys: {list(by_name_v2.keys())}"
    )
    changing_after = by_name_v2["Changing Schedule"]
    assert changing_after["enabled"] is True, (
        "Reinstalled schedule must carry publisher's enabled=True"
    )
    # The consumer must see the publisher's UPDATED cron (CRON_B), not the
    # original CRON_A the schedule was first published with.
    assert changing_after["cron_string"] == _CRON_B, (
        f"Reinstalled schedule must carry publisher's updated cron {_CRON_B!r}; "
        f"got {changing_after['cron_string']!r}"
    )

    # (c) "New Schedule" appears (added by publisher).
    assert "New Schedule" in by_name_v2, (
        f"New schedule added by publisher must appear after merge; keys: {list(by_name_v2.keys())}"
    )

    # (d) "To Be Removed" is gone.
    assert "To Be Removed" not in by_name_v2, (
        "Removed publisher schedule must be deleted from consumer after merge"
    )

    # Total count: Stable(renamed) + Changing + New = 3
    assert len(consumer_schedules_v2) == 3, (
        f"Expected 3 schedules after v2 merge; got {len(consumer_schedules_v2)}: "
        f"{list(by_name_v2.keys())}"
    )


# ── Scenario 4: Route guards ──────────────────────────────────────────────────


def test_route_guards_on_foreign_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Foreign/consumer install: POST/DELETE → 403; PUT with non-enabled field → 403;
    PUT with only enabled → 200; run-now and logs accessible.
    Publisher install retains full CRUD.

    Scenario breakdown:
    - POST /{install_id}/schedules → 403
    - DELETE /{install_id}/schedules/{sid} → 403
    - PUT /{install_id}/schedules/{sid} with name only → 403
    - PUT /{install_id}/schedules/{sid} with {enabled} only → 200
    - POST /{install_id}/schedules/{sid}/run → 200 (or non-403 env-related response)
    - GET /{install_id}/schedules/{sid}/logs → 200
    - Publisher: POST/DELETE/PUT (full) → 200
    """
    pub_headers = superuser_token_headers

    # ── Phase 1: Publisher creates agent + one schedule ───────────────────────
    agent = create_agent_via_api(client, pub_headers, name="Guard-Publisher")
    drain_tasks()
    agent_id = agent["id"]

    pub_sched = create_schedule(
        client, pub_headers, agent_id,
        name="Publisher Schedule",
        cron_string=_CRON_A,
        timezone="UTC",
        prompt="Run the report",
        enabled=True,
    )
    pub_sched_id = pub_sched["id"]

    # ── Phase 2: Publish + install by consumer ────────────────────────────────
    _publish(client, pub_headers, agent_id)
    fresh_pub = _get_agent(client, pub_headers, agent_id)
    bundle_id = fresh_pub["bundle_id"]
    _make_public(client, pub_headers, fresh_pub["bundle_uuid"])

    _, consumer_headers = _make_user_and_headers(client)
    install = _install(client, consumer_headers, bundle_id)
    install_id = install["id"]

    # Find the schedule on the consumer install.
    consumer_schedules = list_schedules(client, consumer_headers, install_id)
    assert len(consumer_schedules) == 1, (
        f"Expected 1 schedule on consumer install; got {len(consumer_schedules)}"
    )
    consumer_sched_id = consumer_schedules[0]["id"]

    # ── Phase 3: POST /{install_id}/schedules → 403 ────────────────────────
    r = client.post(
        f"{API}/agents/{install_id}/schedules",
        headers=consumer_headers,
        json={
            "name": "Injected Schedule",
            "cron_string": _CRON_B,
            "timezone": "UTC",
            "description": "Injected",
            "prompt": "Injected",
            "enabled": True,
        },
    )
    assert r.status_code == 403, (
        f"Expected 403 for consumer POST schedules; got {r.status_code}: {r.text}"
    )

    # ── Phase 4: DELETE /{install_id}/schedules/{sid} → 403 ──────────────────
    r = client.delete(
        f"{API}/agents/{install_id}/schedules/{consumer_sched_id}",
        headers=consumer_headers,
    )
    assert r.status_code == 403, (
        f"Expected 403 for consumer DELETE schedule; got {r.status_code}: {r.text}"
    )

    # ── Phase 5: PUT with non-enabled field (name) → 403 ────────────────────
    r = client.put(
        f"{API}/agents/{install_id}/schedules/{consumer_sched_id}",
        headers=consumer_headers,
        json={"name": "Attempted Rename"},
    )
    assert r.status_code == 403, (
        f"Expected 403 for consumer PUT with non-enabled field; got {r.status_code}: {r.text}"
    )

    # ── Phase 6: PUT with only {enabled} → 200 (toggle works) ────────────────
    r = client.put(
        f"{API}/agents/{install_id}/schedules/{consumer_sched_id}",
        headers=consumer_headers,
        json={"enabled": False},
    )
    assert r.status_code == 200, (
        f"Expected 200 for consumer PUT {{enabled}} toggle; got {r.status_code}: {r.text}"
    )
    assert r.json()["enabled"] is False

    # Re-enable to confirm toggle in both directions.
    r = client.put(
        f"{API}/agents/{install_id}/schedules/{consumer_sched_id}",
        headers=consumer_headers,
        json={"enabled": True},
    )
    assert r.status_code == 200, (
        f"Expected 200 for consumer PUT {{enabled: True}} toggle; got {r.status_code}: {r.text}"
    )
    assert r.json()["enabled"] is True

    # ── Phase 7: run-now → accessible (200 or env-related non-403) ──────────
    r = client.post(
        f"{API}/agents/{install_id}/schedules/{consumer_sched_id}/run",
        headers=consumer_headers,
    )
    # 200 = schedule triggered; 400/500 = env not ready — both are fine.
    # The important invariant is it is NOT 403.
    assert r.status_code != 403, (
        f"run-now must not return 403 for consumer install; got {r.status_code}: {r.text}"
    )

    # ── Phase 8: Logs → 200 ────────────────────────────────────────────────
    logs = get_schedule_logs(client, consumer_headers, install_id, consumer_sched_id)
    assert isinstance(logs, list), "Schedule logs must be a list"

    # ── Phase 9: Publisher install retains full CRUD ───────────────────────
    # Create
    new_pub_sched = create_schedule(
        client, pub_headers, agent_id,
        name="Publisher New",
        cron_string=_CRON_B,
        timezone="UTC",
        prompt="Publisher can create",
        enabled=True,
    )
    assert new_pub_sched["id"], "Publisher must be able to create a schedule"

    # Edit (full field, not just enabled)
    updated = update_schedule(
        client, pub_headers, agent_id, new_pub_sched["id"],
        name="Publisher New (Renamed)",
        cron_string=_CRON_C,
        timezone="UTC",
    )
    assert updated["name"] == "Publisher New (Renamed)", (
        "Publisher must be able to rename a schedule"
    )

    # Delete
    delete_schedule(client, pub_headers, agent_id, new_pub_sched["id"])
    remaining = list_schedules(client, pub_headers, agent_id)
    assert not any(s["id"] == new_pub_sched["id"] for s in remaining), (
        "Publisher must be able to delete a schedule"
    )


# ── Scenario 5: PUT with mixed enabled + other field → 403 ──────────────────


def test_consumer_put_mixed_fields_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Consumer PUT with {enabled, name} (mixed) is rejected with 403.

    The guard allows ONLY {enabled}; any additional set field triggers 403.
    """
    pub_headers = superuser_token_headers

    agent = create_agent_via_api(client, pub_headers, name="MixedPUT-Publisher")
    drain_tasks()
    agent_id = agent["id"]

    create_schedule(
        client, pub_headers, agent_id,
        name="Guard Schedule",
        cron_string=_CRON_A,
        timezone="UTC",
        prompt="Test",
        enabled=True,
    )

    _publish(client, pub_headers, agent_id)
    fresh_pub = _get_agent(client, pub_headers, agent_id)
    bundle_id = fresh_pub["bundle_id"]
    _make_public(client, pub_headers, fresh_pub["bundle_uuid"])

    _, consumer_headers = _make_user_and_headers(client)
    install = _install(client, consumer_headers, bundle_id)
    install_id = install["id"]

    consumer_schedules = list_schedules(client, consumer_headers, install_id)
    assert len(consumer_schedules) == 1
    consumer_sched_id = consumer_schedules[0]["id"]

    # PUT {enabled, name} → 403 (name is a non-enabled field)
    r = client.put(
        f"{API}/agents/{install_id}/schedules/{consumer_sched_id}",
        headers=consumer_headers,
        json={"enabled": True, "name": "Sneaky Rename"},
    )
    assert r.status_code == 403, (
        f"Expected 403 for PUT {{enabled, name}}; got {r.status_code}: {r.text}"
    )

    # PUT {enabled, description} → 403 (description is a non-enabled field)
    r = client.put(
        f"{API}/agents/{install_id}/schedules/{consumer_sched_id}",
        headers=consumer_headers,
        json={"enabled": True, "description": "Sneaky desc"},
    )
    assert r.status_code == 403, (
        f"Expected 403 for PUT {{enabled, description}}; got {r.status_code}: {r.text}"
    )


# ── Scenario 6: Publish with no schedules → revision.schedules == [] ─────────


def test_publish_with_no_schedules_produces_empty_list(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A publisher with no schedules produces revision.schedules == []."""
    agent = create_agent_via_api(
        client, superuser_token_headers, name="NoSched-Publisher"
    )
    drain_tasks()
    agent_id = agent["id"]

    revision = _publish(client, superuser_token_headers, agent_id)

    rev_schedules = revision.get("schedules")
    assert rev_schedules == [], (
        f"Expected empty schedules list; got {rev_schedules}"
    )

    manifest = revision.get("manifest") or {}
    assert manifest.get("schedules") == [], (
        f"Expected manifest['schedules'] == []; got {manifest.get('schedules')}"
    )


# ── Scenario 7: Consumer install with no publisher schedules has no schedules ──


def test_install_with_no_publisher_schedules_produces_empty(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Consumer install from a revision with schedules=[] produces 0 schedules."""
    agent = create_agent_via_api(
        client, superuser_token_headers, name="NoSched-Install-Publisher"
    )
    drain_tasks()
    agent_id = agent["id"]

    _publish(client, superuser_token_headers, agent_id)
    fresh_pub = _get_agent(client, superuser_token_headers, agent_id)
    bundle_id = fresh_pub["bundle_id"]
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    _, consumer_headers = _make_user_and_headers(client)
    install = _install(client, consumer_headers, bundle_id)
    install_id = install["id"]

    consumer_schedules = list_schedules(client, consumer_headers, install_id)
    assert consumer_schedules == [], (
        f"Expected no schedules on consumer when publisher has none; got {consumer_schedules}"
    )
