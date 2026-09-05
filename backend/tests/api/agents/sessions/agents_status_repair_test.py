"""Status-repair sweep — Pass A (environments), Pass B (sessions), Pass C (tasks).

See ``docs/plans/system_status_repair_plan.md`` and the module docstrings under
``app/services/system/status_repair_*.py``. The sweep reconciles rows a
background task abandoned in a transitional status when the process died
before its ``except Exception`` fallback could run (a killed process cancels
the task with ``asyncio.CancelledError``, a ``BaseException``, which every
``except Exception`` in the lifecycle skips). It never ticks under pytest —
gated behind ``settings.TESTING`` like the other 17 schedulers — so, per each
pass module's own docstring, the pass functions themselves ARE the test
surface: a test builds a ``RepairContext`` and awaits one directly.
``tests/utils/status_repair.py::RepairTick`` does that, documented there as a
structural exemption from the "no ``app.services`` imports in ``tests/api/``"
rule, because there is no HTTP surface for a sweep tick to call instead.

**Why this file, in this group.** ``tests/api/agents/`` is the one domain
whose ``conftest.py`` patches BOTH ``CREATE_SESSION_TARGETS_AGENT`` and
``BACKGROUND_TASK_TARGETS_FULL`` (needed for Pass B's
``SessionService.clear_interaction_status`` / re-entered drain and Pass C's
``update_task_status``) AND stubs the environment adapter (needed for Pass
A's container probe). See the "Fixture-target scope" note in
``tests/utils/fixtures.py`` for the domain that looks plausible but is NOT
enough (``tests/api/agent_environments/`` patches BASE only — a Pass B/C test
written there would write through to the real database while every assertion
reads the untouched test transaction).

**Every agent already has an environment.** ``AgentService.create_agent``
creates a default ``AgentEnvironment`` with ``auto_start=True`` as part of
agent creation (``app/services/agents/agent_service.py``). One ``drain_tasks()``
after ``create_agent_via_api`` runs that build AND its auto-start to
completion, landing on a genuinely ``"running"``, active environment with a
real allocated port — which is exactly the starting point most of these tests
want. ``Session.create`` (``session_service.py``) refuses to create a session
at all until ``agent.active_environment_id`` is set, which only happens once
that background task has run — so every test that creates a session drains
at least once first.

**Which preconditions are real, and which need a seam.** Each transitional
state this sweep repairs is normally cleared by the very background task
whose death is what leaves it stuck — and in an in-process test, nothing
kills the process. Most of the states here ARE reachable by simply not
draining a background task the API legitimately queued (a stuck
``AgentEnvironment.status="creating"``/``"starting"``, a stuck
``Session.interaction_status="pending_stream"``) — those are driven through
the API and then only AGED via a documented seam
(``age_environment_status_changed_at`` / ``force_session_interaction_claim``),
because the sweep's thresholds are 10-120 minutes and no test may block that
long. Two combinations are not reachable at all in-process, and are
constructed directly via the same documented seams (which say so at the call
site): ``interaction_status="running"`` stuck forever — the harness's own
defensive cleanup (a bare ``finally``, not ``except Exception``, in
``MessageService.process_pending_messages``) clears it for anything this
process can do to a stream — and a task stranded ``in_progress`` after its
session already resolved, which requires landing between two handlers that
run back-to-back in the very same drain.
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import (
    activate_environment,
    age_environment_status_changed_at,
    get_environment,
    list_environments,
    stop_environment,
)
from tests.utils.input_task import (
    create_task,
    execute_task,
    force_task_stranded_in_progress,
    get_task,
    get_task_detail,
)
from tests.utils.message import get_messages_by_role, send_message
from tests.utils.session import (
    create_session_via_api,
    force_session_interaction_claim,
    get_session,
)
from tests.utils.status_repair import RepairTick, set_environment_probe


# ── Pass A: environments abandoned mid-lifecycle ────────────────────────────

def test_stuck_activating_environment_with_healthy_container_repairs_to_running(
    client: TestClient, superuser_token_headers: dict, db
) -> None:
    """
    A crashed activation, where the container actually came up fine:
      1. Create an agent and drain its default environment to a real
         "running" state.
      2. Activate it again for real (sets status="starting" synchronously)
         but never drain the background task that would finish the job — the
         exact gap a killed process leaves.
      3. Age the claim past the 10-minute activation threshold (the one thing
         no API call can do) and point this environment's adapter probe at
         "running" (the crash's other half: the container is fine).
      4. Pass A repairs it to "running" and the repair is idempotent.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()  # builds AND auto-starts the default environment
    agent = get_agent(client, superuser_token_headers, agent["id"])
    env_id = agent["active_environment_id"]
    assert env_id is not None

    running = get_environment(client, superuser_token_headers, env_id)
    assert running["status"] == "running"

    activate_environment(client, superuser_token_headers, agent["id"], env_id)
    stuck = get_environment(client, superuser_token_headers, env_id)
    assert stuck["status"] == "starting"

    age_environment_status_changed_at(db, env_id, minutes_ago=15)
    set_environment_probe(env_id, "running")

    tick = RepairTick(db)
    assert tick.run_environments() == 1

    healed = get_environment(client, superuser_token_headers, env_id)
    assert healed["status"] == "running"
    assert "reconciled by status repair" in (healed["status_message"] or "")

    # Idempotent: nothing left to repair on a second tick.
    assert RepairTick(db).run_environments() == 0


def test_stuck_creating_environment_with_dead_container_repairs_to_error(
    client: TestClient, superuser_token_headers: dict, db
) -> None:
    """
    A crashed creation, where the build never got far enough to have a
    container at all: create an agent and never drain its default
    environment's build task (status stays "creating", no port ever
    allocated). Aged past the 60-minute build threshold, Pass A's probe treats
    "never provisioned" as decisive evidence of "gone" and repairs straight to
    "error" without even reaching the adapter — a container that never
    existed needs no liveness check.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    envs = list_environments(client, superuser_token_headers, agent["id"])
    env_id = envs["data"][0]["id"]

    stuck = get_environment(client, superuser_token_headers, env_id)
    assert stuck["status"] == "creating"

    age_environment_status_changed_at(db, env_id, minutes_ago=65)

    tick = RepairTick(db)
    assert tick.run_environments() == 1

    healed = get_environment(client, superuser_token_headers, env_id)
    assert healed["status"] == "error"
    assert "interrupted (reconciled by status repair)" in (healed["status_message"] or "")

    assert RepairTick(db).run_environments() == 0


def test_stuck_environment_with_inconclusive_probe_repairs_nothing(
    client: TestClient, superuser_token_headers: dict, db
) -> None:
    """
    Golden rule made concrete: a probe that cannot answer must not license a
    repair, even once the row is old enough to look at. An environment stuck
    at "starting" whose container reports neither "running" nor "stopped" (a
    wedged-but-up container, or an unreachable daemon) is left exactly as it
    was — well short of the 6x escalation window that would otherwise treat
    the silence itself as evidence.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    env_id = agent["active_environment_id"]

    activate_environment(client, superuser_token_headers, agent["id"], env_id)
    age_environment_status_changed_at(db, env_id, minutes_ago=15)  # > 10 min, < 6x60
    set_environment_probe(env_id, "unhealthy-but-up")

    tick = RepairTick(db)
    assert tick.run_environments() == 0

    untouched = get_environment(client, superuser_token_headers, env_id)
    assert untouched["status"] == "starting"


# ── Pass B: sessions wedged mid-interaction ─────────────────────────────────

def test_stale_running_claim_is_cleared_and_pending_count_recomputed(
    client: TestClient, superuser_token_headers: dict, db
) -> None:
    """
    A session claiming ``interaction_status="running"`` for two hours with no
    terminal event is cleared to idle on age alone (no environment probe is
    relevant to a stream this old — see the module docstring). In the same
    write, Pass B also re-derives ``pending_messages_count`` from the actual
    ``message`` rows, correcting a counter the same crash left disagreeing
    with reality.

    The "running" claim itself is constructed via the documented seam (see
    this file's module docstring): the harness's own defensive ``finally`` in
    ``process_pending_messages`` clears it for anything an in-process test can
    do to a stream, so a genuinely abandoned "running" claim cannot be
    produced by any live path here.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()  # default environment running + active
    session = create_session_via_api(client, superuser_token_headers, agent["id"])

    # A real pending user message, so there is an honest "actual" count for
    # the recompute to find. The queued process_pending_messages is
    # deliberately never drained.
    send_message(client, superuser_token_headers, session["id"], "are you there?")

    force_session_interaction_claim(
        db,
        session["id"],
        interaction_status="running",
        streaming_started_at=datetime.now(UTC) - timedelta(minutes=125),
        pending_messages_count=0,  # deliberately wrong, to prove the recompute
    )
    stuck = get_session(client, superuser_token_headers, session["id"])
    assert stuck["interaction_status"] == "running"
    assert stuck["pending_messages_count"] == 0

    tick = RepairTick(db)
    assert tick.run_sessions() == 1

    healed = get_session(client, superuser_token_headers, session["id"])
    assert healed["interaction_status"] == ""
    assert healed["pending_messages_count"] == 1

    assert RepairTick(db).run_sessions() == 0


def test_pending_stream_with_environment_not_running_clears_to_idle(
    client: TestClient, superuser_token_headers: dict, db
) -> None:
    """
    The fully organic half of Pass B.2: a message sent while the environment
    is genuinely not running parks the session at ``pending_stream`` for
    real (``SessionService.initiate_stream``'s non-running branch). With the
    environment still not running by the time Pass B looks (no evidence that
    delivering now would even reach anywhere), the row is cleared to idle and
    the message is left ``pending`` — visible and recoverable via
    ``/session-recover`` — rather than resent. Only the claim's age
    (``updated_at``) is a seam; everything else here is real.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()  # default environment running + active
    agent = get_agent(client, superuser_token_headers, agent["id"])
    env_id = agent["active_environment_id"]
    stop_environment(client, superuser_token_headers, env_id)  # real, synchronous

    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    send_message(client, superuser_token_headers, session["id"], "are you there?")
    parked = get_session(client, superuser_token_headers, session["id"])
    assert parked["interaction_status"] == "pending_stream"

    force_session_interaction_claim(
        db,
        session["id"],
        interaction_status="pending_stream",
        updated_at=datetime.now(UTC) - timedelta(minutes=20),
    )

    tick = RepairTick(db)
    assert tick.run_sessions() == 1

    idle = get_session(client, superuser_token_headers, session["id"])
    assert idle["interaction_status"] == ""
    msgs = get_messages_by_role(client, superuser_token_headers, session["id"], "user")
    assert msgs[-1]["sent_to_agent_status"] == "pending"  # recoverable, not lost

    assert RepairTick(db).run_sessions() == 0


def test_pending_stream_resend_fires_once_per_episode_then_converges_to_idle(
    client: TestClient, superuser_token_headers: dict, db
) -> None:
    """
    The bug this guards against: before the fix, a re-entered drain restamped
    the very clock the resend window was measured against, so the same stuck
    message triggered a fresh drain attempt every tick, forever. This proves
    both halves of the fix in one scenario:

      1. A session parked at ``pending_stream`` whose environment is (really)
         running and whose oldest pending message is (really) fresh — Pass B
         re-enters the drain exactly once, and the resend is spawned as a
         background task rather than delivered inline (see
         ``BACKGROUND_TASK_TARGETS_BASE`` — deliberately collected, not run,
         so this test can inspect the "spawned but not delivered" moment).
      2. A second tick, minutes later, with the SAME oldest pending message
         (the spawned drain was never actually run, so nothing changed
         underneath it) must NOT spawn a second drain — the episode marker
         caps it at one. Instead the row converges: cleared to idle, message
         left ``pending`` and recoverable. (The claim is re-aged before this
         second tick: the first tick's own resend bumps ``updated_at`` to buy
         itself one threshold's worth of re-entry suppression — that is not
         the episode cap, and without re-aging the row would not even be a
         candidate again yet, which would make the second tick's ``0`` a
         trivial pass rather than a proof the cap fired.)

    ``interaction_status="pending_stream"`` itself is the one field forced
    here (see this file's module docstring): with the environment already
    running, sending a message takes ``initiate_stream``'s "running" branch,
    which queues ``process_pending_messages`` without ever touching
    ``interaction_status`` — the column stays at its resting ``""`` until that
    queued task actually runs, which this test deliberately never drains (real
    delivery would resolve the very state Pass B needs to see). The pending
    *message* underneath the claim — its row, its ``sent_to_agent_status``, its
    ``timestamp`` — is entirely real, sent through the API against a genuinely
    running environment; only the claim label is forced, standing in for the
    real-world race the sweep guards against (this particular send's own
    ``initiate_stream`` call losing the race against a same-agent activation
    check moments earlier).
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()  # default environment running + active
    agent = get_agent(client, superuser_token_headers, agent["id"])
    running_env = get_environment(client, superuser_token_headers, agent["active_environment_id"])
    assert running_env["status"] == "running"

    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    send_message(client, superuser_token_headers, session["id"], "please handle this")

    force_session_interaction_claim(
        db,
        session["id"],
        interaction_status="pending_stream",
        updated_at=datetime.now(UTC) - timedelta(minutes=20),
    )

    tick = RepairTick(db)
    with patch(
        "app.services.sessions.message_service.agent_env_connector",
        StubAgentEnvConnector(response_text="on it"),
    ):
        assert tick.run_sessions() == 1

    # The resend was spawned (fire-and-forget) but deliberately not drained —
    # the message is still pending and the row is still parked, which is
    # exactly the moment a second tick minutes later would observe.
    still_pending = get_session(client, superuser_token_headers, session["id"])
    assert still_pending["interaction_status"] == "pending_stream"
    msgs = get_messages_by_role(client, superuser_token_headers, session["id"], "user")
    assert msgs[-1]["sent_to_agent_status"] == "pending"

    # The first tick's own resend deliberately bought the row one more
    # threshold's worth of quiet (``_stamp_resend_claim`` bumps ``updated_at``
    # precisely so the SAME tick's candidate query does not re-select it a
    # moment later) — that is re-entry suppression, not the episode cap, and
    # it means the row is not even a candidate again yet. Age the claim once
    # more, simulating the next tick 15+ minutes on, with the SAME oldest
    # pending message (nothing was drained) — this is what actually exercises
    # the hard cap: the episode marker, not the age, is what must stop a
    # second resend here.
    force_session_interaction_claim(
        db,
        session["id"],
        interaction_status="pending_stream",
        updated_at=datetime.now(UTC) - timedelta(minutes=20),
    )

    # Second tick, same oldest pending message: must NOT re-fire the drain.
    # It converges instead — cleared to idle, message left pending/recoverable.
    second_tick = RepairTick(db)
    assert second_tick.run_sessions() == 1

    converged = get_session(client, superuser_token_headers, session["id"])
    assert converged["interaction_status"] == ""
    msgs_after = get_messages_by_role(client, superuser_token_headers, session["id"], "user")
    assert msgs_after[-1]["sent_to_agent_status"] == "pending"


# ── Cross-pass: A's drain must not be re-acted-on by B ──────────────────────

def test_pass_b_skips_a_session_whose_environment_pass_a_just_drained(
    client: TestClient, superuser_token_headers: dict, db
) -> None:
    """
    The first of the three cross-pass regressions the plan calls out: Pass A
    repairing an environment to "running" emits ``ENVIRONMENT_ACTIVATED``,
    which drains that environment's ``pending_stream`` sessions — but
    asynchronously, several task hops away, so the session ROW has not moved
    yet. Before the fix, Pass B (running immediately after, in the same tick)
    would see the still-parked claim and act on it a second time — spawning a
    duplicate drain, or clearing a session mid-drain. The fix is
    ``RepairContext.drained_environment_ids``: Pass A records what it just
    started, and Pass B skips those sessions outright.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()  # default environment running + active
    agent = get_agent(client, superuser_token_headers, agent["id"])
    env_id = agent["active_environment_id"]
    stop_environment(client, superuser_token_headers, env_id)

    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    send_message(client, superuser_token_headers, session["id"], "waiting on the environment")
    parked = get_session(client, superuser_token_headers, session["id"])
    assert parked["interaction_status"] == "pending_stream"

    # The environment's own crash: activation started, never finished.
    activate_environment(client, superuser_token_headers, agent["id"], env_id)
    age_environment_status_changed_at(db, env_id, minutes_ago=15)
    set_environment_probe(env_id, "running")

    # The session's claim is ALSO aged past its own threshold, so — absent the
    # cross-pass guard — Pass B would consider it abandoned on its own merits.
    force_session_interaction_claim(
        db,
        session["id"],
        interaction_status="pending_stream",
        updated_at=datetime.now(UTC) - timedelta(minutes=20),
    )

    tick = RepairTick(db)
    assert tick.run_environments() == 1
    assert tick.is_environment_draining(env_id)

    assert tick.run_sessions() == 0  # skipped, not resent and not cleared
    assert tick.is_session_in_motion(session["id"])  # noted for Pass D's benefit

    untouched = get_session(client, superuser_token_headers, session["id"])
    assert untouched["interaction_status"] == "pending_stream"


# ── Pass C: input tasks re-derived from repaired sessions ───────────────────

def test_pass_c_rederives_task_status_after_pass_b_clears_its_session(
    client: TestClient, superuser_token_headers: dict, db
) -> None:
    """
    The real B → C hand-off (the one ordering dependency in the sweep that
    consumes a committed result rather than context state): a task stranded
    at ``in_progress`` whose only session Pass B has just cleared to idle
    re-derives, through the platform's own ``compute_status_from_sessions``,
    to ``completed`` — written via ``update_task_status`` so a
    ``TaskStatusHistory`` row records the transition with the repairer's
    reason, not a raw status flip.

    The task's ``in_progress`` + aged ``executed_at`` combination is
    constructed via the documented seam (see this file's module docstring):
    ``in_progress`` is normally set by the ``STREAM_STARTED`` handler and
    resynced away by the completion handler in the very same drain, so
    landing between the two — with the session already resolved — is a
    process-death race this harness cannot hold open.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()  # default environment running + active
    agent = get_agent(client, superuser_token_headers, agent["id"])
    env_id = agent["active_environment_id"]
    stop_environment(client, superuser_token_headers, env_id)

    task = create_task(
        client, superuser_token_headers,
        original_message="Investigate the outage",
        selected_agent_id=agent["id"],
    )
    exec_result = execute_task(client, superuser_token_headers, task["id"])
    assert exec_result["success"] is True
    session_id = str(exec_result["session_id"])

    parked = get_session(client, superuser_token_headers, session_id)
    assert parked["interaction_status"] == "pending_stream"  # env not running

    force_task_stranded_in_progress(
        db, task["id"], executed_at=datetime.now(UTC) - timedelta(minutes=35)
    )
    stranded = get_task(client, superuser_token_headers, task["id"])
    assert stranded["status"] == "in_progress"

    force_session_interaction_claim(
        db, session_id, interaction_status="pending_stream",
        updated_at=datetime.now(UTC) - timedelta(minutes=20),
    )

    tick = RepairTick(db)
    assert tick.run_sessions() == 1
    idle = get_session(client, superuser_token_headers, session_id)
    assert idle["interaction_status"] == ""

    assert tick.run_input_tasks() == 1
    healed_task = get_task(client, superuser_token_headers, task["id"])
    assert healed_task["status"] == "completed"

    detail = get_task_detail(client, superuser_token_headers, task["id"])
    history = detail["status_history"]
    assert history, "expected at least one TaskStatusHistory row"
    last = history[-1]
    assert last["from_status"] == "in_progress"
    assert last["to_status"] == "completed"
    assert last["reason"] == "Re-derived from session state by status repair"

    assert RepairTick(db).run_input_tasks() == 0
