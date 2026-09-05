"""Status-repair sweep — Pass D (channel turn deliveries left as drafts).

See ``docs/plans/system_status_repair_plan.md`` and
``app/services/system/status_repair_channels.py``. Pass D reaps
``ChannelTurnDelivery`` rows a dead process left at ``role="draft"`` — a
message standing in an external thread that the streaming relay was still
rewriting when it died — by sealing them (``role="sealed"``,
``status="diverged"``), which takes the row out of the running for the next
completion's mis-attribution (see that module's docstring for exactly what
goes wrong if nothing reaps it). Like the other three passes, it never ticks
under pytest (``settings.TESTING`` gate) and its pass function IS the test
surface: see ``tests/utils/status_repair.py::RepairTick``.

**Why this table needs a construction seam, not just an aged one.**
``ChannelTurnDelivery``/``ChannelThreadBinding`` have NO API surface at all —
``tests/utils/server_channel.py`` already carries read-only Rule-1 exemptions
for both (``list_turn_deliveries``, ``get_binding_status_message_id``),
documented there as "internal bookkeeping ... invisible from the thread by
construction". The row Pass D repairs compounds that: a draft only survives
to the next tick when the process dies before the seal/final write that would
otherwise close it out in the SAME turn, and that close-out runs
synchronously in this harness (nothing here kills the process mid-stream), so
there is no sequence of API calls that leaves the gap open. This file
therefore uses ``seed_stale_draft_delivery`` (added alongside the existing
read-only exemptions, same file, same posture) to construct the row directly,
the same way ``force_session_interaction_claim``'s "running" branch and
``force_task_stranded_in_progress`` construct their own unreachable
preconditions in ``tests/api/agents/sessions/agents_status_repair_test.py``.

**Scope note.** The notice-settlement half of Pass D
(``_settle_stale_notice`` rewriting the thread's "working…" message) is not
covered here: exercising it needs a real or stubbed channel-adapter network
call, which is disproportionate setup for this pass given the seal/diverged
write it happens strictly after already fully captures the repair's
database-observable effect. The tests below therefore never set a
``status_message_id`` on the seeded binding, which keeps
``_settle_stale_notice`` a no-op (``if plan.status_message_id is None:
return``) without masking anything the repair itself does.
"""
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.models import CHANNEL_DELIVERY_DIVERGED, CHANNEL_DELIVERY_SEALED
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.server_channel import create_server_channel, list_turn_deliveries, seed_stale_draft_delivery
from tests.utils.session import create_session_via_api, force_session_interaction_claim, get_session
from tests.utils.status_repair import RepairTick


def test_stale_draft_delivery_with_no_live_session_is_sealed_as_diverged(
    client: TestClient, superuser_token_headers: dict, db
) -> None:
    """
    A draft delivery whose binding has no session at all (a channel turn that
    never got as far as opening one, or whose session was later detached) is
    the simplest case Pass D's evidence-gathering has to clear: no session to
    probe, no live relay in this process (``session_id=None`` short-circuits
    the veto), and the row is old enough to look at. It is sealed as
    diverged.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    channel = create_server_channel(client, superuser_token_headers)
    thread_key = "status-repair-thread-no-session"

    binding_id, delivery_id = seed_stale_draft_delivery(
        db,
        channel["id"],
        user_id=agent["owner_id"],
        agent_id=agent["id"],
        thread_key=thread_key,
        session_id=None,
        updated_at=datetime.now(UTC) - timedelta(minutes=70),
    )

    tick = RepairTick(db)
    assert tick.run_channel_deliveries() == 1

    rows = list_turn_deliveries(db, channel["id"], thread_key)
    assert len(rows) == 1
    assert rows[0].id == delivery_id
    assert rows[0].role == CHANNEL_DELIVERY_SEALED
    assert rows[0].status == CHANNEL_DELIVERY_DIVERGED

    # Idempotent: the row is no longer a "draft" candidate.
    assert RepairTick(db).run_channel_deliveries() == 0


def test_pass_d_skips_a_delivery_whose_session_pass_b_touched_this_tick(
    client: TestClient, superuser_token_headers: dict, db
) -> None:
    """
    The third cross-pass regression the plan calls out: Pass B clears a
    long-running session's ``interaction_status`` WITHOUT cancelling the
    stream behind it. Pass D's evidence that a channel turn is over is that
    same column — so, absent the guard, a turn that legitimately exceeds the
    stream threshold would be cleared by B and then sealed by D in the SAME
    tick, rewriting a message the (still-live) relay is still patching. The
    fix is ``RepairContext.sessions_in_motion``: Pass D must not read
    ``interaction_status`` as evidence for any session Pass B touched this
    tick, and skips its delivery rows outright.

    The session's "running" claim is constructed via the documented seam (see
    ``agents_status_repair_test.py``'s module docstring for why): the
    harness's own defensive cleanup prevents an in-process test from leaving
    one abandoned for real.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()  # default environment running + active — required to create a session
    session = create_session_via_api(client, superuser_token_headers, agent["id"])

    force_session_interaction_claim(
        db,
        session["id"],
        interaction_status="running",
        streaming_started_at=datetime.now(UTC) - timedelta(minutes=125),
    )

    channel = create_server_channel(client, superuser_token_headers)
    thread_key = "status-repair-thread-live-session"
    binding_id, delivery_id = seed_stale_draft_delivery(
        db,
        channel["id"],
        user_id=agent["owner_id"],
        agent_id=agent["id"],
        thread_key=thread_key,
        session_id=session["id"],
        updated_at=datetime.now(UTC) - timedelta(minutes=70),
    )

    tick = RepairTick(db)
    assert tick.run_sessions() == 1  # Pass B clears the stale "running" claim
    cleared = get_session(client, superuser_token_headers, session["id"])
    assert cleared["interaction_status"] == ""
    assert tick.is_session_in_motion(session["id"])

    # Same tick, same context: Pass D must leave the delivery row alone even
    # though the session it points at now reads "not running".
    assert tick.run_channel_deliveries() == 0

    rows = list_turn_deliveries(db, channel["id"], thread_key)
    assert len(rows) == 1
    assert rows[0].id == delivery_id
    assert rows[0].role == "draft"  # untouched
