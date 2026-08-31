"""Helpers for Auto Routing Tuning API tests (`tests/api/routing/`).

Most of this domain is plain HTTP wrappers around
``GET/DELETE /api/v1/admin/routing/traces``. Two functions below are
documented Rule-1 exemptions, mirroring the shape already established in
``tests/utils/server_channel.py`` (``flush_pending_bindings`` /
``route_installed``) — each covers functionality that genuinely has no HTTP
surface, by design:

1. ``purge_routing_traces`` — ``RoutingTraceService.purge`` is invoked by
   ``routing_trace_scheduler``, which (like every other scheduler in this
   project) is gated behind ``settings.TESTING`` and never runs in tests.
   There is no admin route that triggers a purge on demand; tests call the
   service method directly instead of racing a background thread.

2. ``seed_routing_trace`` — ``RoutingDecision.created_at`` is server-assigned
   at persist time and there is no route that lets a caller backdate a
   decision. That is deliberate (forging a trace's timestamp has no
   legitimate use), which also means it is the *only* way to test
   ``purge``'s ``created_at < cutoff`` boundary. It is also still the only
   way to produce an ``identity`` row — nothing opens a capture with that
   origin, and by ruling nothing will: identity is a *stage* inside another
   surface's decision. Neither ``simulate`` nor ``app_mcp`` is in that list
   any more: ``POST /admin/routing/simulate`` and ``.../replay`` emit the
   first for real, and ``AppMCPRoutingService.route_message`` emits the
   second (phase 6 of ``docs/plans/channels_identity_unification/``), so a
   test wanting a row on either should drive the surface rather than seed
   one — ``tests/api/app_mcp/app_mcp_routing_trace_test.py`` does. Seeding an
   ``app_mcp`` row is still legitimate where the *shape* of the row is the
   fixture rather than the thing under test, but note it now passes through
   ``ROUTING_TRACE_APP_MCP_MODE``: at the default ``metadata`` the seeded row
   comes back without its ``message_text``, and at ``off`` it is not written
   at all. Pin the mode around the seed if the test cares.
   Builds a real ``RoutingTrace``
   through the recorder (so ``message_sha256`` etc. are computed the same
   way production computes them), then backdates ``created_at`` before
   handing it to ``RoutingTraceService.persist``.

   Takes no ``db`` parameter: ``persist`` opens its own short-lived session
   (see its docstring — a diagnostic write must never borrow the caller's
   transaction), and that session is already routed onto the test
   transaction by the domain conftest's ``patch_create_session`` fixture
   (``CREATE_SESSION_TARGETS_AGENT`` includes
   ``routing_trace_service.create_session``). An earlier draft of this
   helper accepted ``db`` and never used it — a signature that lied about
   what makes the write land in the test transaction.
"""
from __future__ import annotations

import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.fixtures import UnstubbedLLMProvider
from tests.utils.server_channel import post_webhook

API = settings.API_V1_STR
_BASE = f"{API}/admin/routing/traces"

_SEND_TARGET = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.send_message"
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"
_CLASSIFY_TARGET = "app.services.routing.agent_classifier.AgentClassifier.classify"
_ROUTE_INSTALLED_TARGET = (
    "app.services.server_channels.channel_routing_service."
    "ChannelRoutingService._route_installed"
)


#: What an unstubbed classifier does inside these helpers.
#:
#: Both helpers used to leave ``AgentClassifier.classify`` unpatched when the
#: caller named no result — so the *default* was "call a real model", and
#: sixteen call sites took it. The default is now the loud case: a test only
#: gets a classifier if it says what that classifier should answer.
#:
#: Patched in at the ``classify`` boundary rather than left to the global
#: provider guard in ``tests/conftest.py`` because this message can name the
#: two keyword arguments that fix it, which the seam-level guard cannot.
#:
#: ``UnstubbedLLMProvider`` is a ``BaseException`` — see its docstring. A plain
#: ``Exception`` raised here would be swallowed by
#: ``ChannelRoutingService._route_installed``'s deliberate catch-all and
#: reported to the test as an ordinary no-match.
_UNSTUBBED_CLASSIFIER_MESSAGE = (
    "AgentClassifier.classify was reached with no answer named, which would "
    "call a real LLM provider. Either this scenario was not supposed to "
    "classify at all (App MCP Stage 1 takes the `only_one` short-circuit on a "
    "single effective route, and an empty candidate list short-circuits "
    "everywhere) and the setup has drifted — or it does classify and has to "
    "say what the answer is: "
    "`classify_result=<ClassificationResult>` to route, `classify_no_match="
    "True` for a classifier that runs and finds nothing "
    "(`post_channel_message` also takes `classify_side_effect=`). A test that "
    "needs the real render/parse path patches "
    "`app.services.routing.agent_classifier.get_provider_manager` itself and "
    "passes `classify_via_provider=True` — see tests/api/routing/README.md. "
    "NOTE channel Pass 1's `only_one` short-circuit is CONDITIONAL: exactly "
    "one eligible owned agent AND nothing the auto-install list could offer "
    "this sender routes without a model. So a sender who owns two or more "
    "eligible agents always classifies, and one who owns exactly one "
    "classifies only when a catalog bundle is still available to them."
)


def refuse_to_classify(*args, **kwargs):
    raise UnstubbedLLMProvider(_UNSTUBBED_CLASSIFIER_MESSAGE)


#: Public because a test that builds its own ``patch()`` chain rather than going
#: through ``enter_classifier_patch`` still needs the loud stub — see
#: ``server_channels_pending_outbound_test.py``. Leaving it private meant a
#: second module importing an underscore name.


def classification(ref_id: Any, **fields: Any):
    """A ``ClassificationResult`` naming ``ref_id`` — what ``classify`` answers.

    ``ref_id`` is an agent id for Pass 1 and a bundle id for Pass 2, exactly as
    ``Candidate.ref_id`` is.

    **When a channel Pass-1 test needs one.** Pass 1 classifies over the agents
    the sender owns and short-circuits on a single eligible candidate only when
    Pass 2 has nothing to offer them — so a test needs an answer here when the
    sender owns two or more eligible agents, or owns one *and* the auto-install
    list still holds a bundle they could be given. A single-agent sender on a
    server with an empty auto-install list — the most common setup in this
    suite — routes with ``match_method="only_one"`` and needs no answer at all;
    naming none is the stronger form, because the refusal stub then fails the
    test if the classifier is reached after all.

    Note the ``ref_id`` a channel test passes is the **agent's** id, not a
    route's. Pass 1 reads no ``AppAgentRoute``.
    """
    from app.services.routing.agent_classifier import ClassificationResult

    return ClassificationResult(agent_id=str(ref_id), **fields)


def enter_classifier_patch(
    stack: ExitStack,
    *,
    classify_result: Any = None,
    classify_no_match: bool = False,
    classify_side_effect: Any = None,
    classify_via_provider: bool = False,
) -> None:
    """Install the classifier stub both helpers below use. One decision, one place.

    This existed as three copies until the defect it now prevents was found —
    the two helpers below and ``server_channels_routing_test.py``'s ``_post`` —
    and the defect was in every one of them, which is the argument for the
    function. Each copy independently ended with "and if the caller named
    nothing, leave ``AgentClassifier.classify`` alone", i.e. calling a live
    model; fixing one would have left the others, and the third was found by a
    reviewer rather than by the first two fixes. All three now call this.

    Precedence is narrow-to-broad: an explicit side effect, then an explicit
    result, then no-match, then the caller's own provider stub, and only then
    the refusal.

    One thing this does NOT preserve, worth knowing before you rely on it: the
    refusal is an inner patch, so it overrides an *outer*
    ``patch(AgentClassifier.classify, ...)`` the caller entered around the
    helper. That pattern used to exist in ``server_channels_routing_test.py``,
    where three tests wrapped ``_post`` in their own classify mock to assert it
    was never called. They now say the same thing by naming no answer — which
    is the stronger form, since it fails at the call instead of afterwards.
    Passing the answer through the helper is the supported way; an outer patch
    will be shadowed, loudly rather than silently.
    """
    if classify_side_effect is not None:
        stack.enter_context(patch(_CLASSIFY_TARGET, side_effect=classify_side_effect))
    elif classify_result is not None:
        stack.enter_context(patch(_CLASSIFY_TARGET, return_value=classify_result))
    elif classify_no_match:
        stack.enter_context(patch(_CLASSIFY_TARGET, return_value=None))
    elif not classify_via_provider:
        stack.enter_context(patch(_CLASSIFY_TARGET, refuse_to_classify))


# ---------------------------------------------------------------------------
# Admin read/clear API — plain HTTP wrappers
# ---------------------------------------------------------------------------


def list_routing_traces(
    client: TestClient,
    token_headers: dict[str, str],
    *,
    channel_id: str | None = None,
    origin: str | None = None,
    outcome: str | None = None,
    user_id: str | None = None,
    skip: int | None = None,
    limit: int | None = None,
    expected_status: int = 200,
) -> Any:
    """GET /admin/routing/traces with optional filters."""
    params: dict[str, Any] = {}
    if channel_id is not None:
        params["channel_id"] = channel_id
    if origin is not None:
        params["origin"] = origin
    if outcome is not None:
        params["outcome"] = outcome
    if user_id is not None:
        params["user_id"] = user_id
    if skip is not None:
        params["skip"] = skip
    if limit is not None:
        params["limit"] = limit
    r = client.get(_BASE, headers=token_headers, params=params)
    assert r.status_code == expected_status, (
        f"List routing traces failed: {r.status_code} {r.text}"
    )
    return r.json() if r.status_code == expected_status else None


def get_routing_trace(
    client: TestClient,
    token_headers: dict[str, str],
    trace_id: str,
    *,
    expected_agent_id: str | None = None,
    expected_status: int = 200,
) -> Any:
    """GET /admin/routing/traces/{trace_id}.

    ``expected_agent_id`` narrows the response's ``diagnosis`` to one agent —
    "why was THIS one not a candidate". Omitted, the verdict describes the
    decision as a whole; both forms are real answers, which is why the
    parameter is optional here as it is on the route.
    """
    params = (
        {"expected_agent_id": expected_agent_id}
        if expected_agent_id is not None
        else None
    )
    r = client.get(f"{_BASE}/{trace_id}", headers=token_headers, params=params)
    assert r.status_code == expected_status, (
        f"Get routing trace failed: {r.status_code} {r.text}"
    )
    return r.json() if r.status_code == expected_status else None


def clear_routing_traces(
    client: TestClient,
    token_headers: dict[str, str],
    *,
    channel_id: str | None = None,
    all_channels: bool | None = None,
    expected_status: int = 200,
) -> Any:
    """DELETE /admin/routing/traces, optionally scoped to one channel.

    ``all_channels`` maps to the route's ``?all=true`` query param — required
    to clear every channel's traces in one call. Neither ``channel_id`` nor
    ``all_channels`` gets a 400 (the route refuses to run unscoped by
    omission); passing neither here reproduces that "forgot the parameter"
    shape on purpose, for tests pinning the 400.
    """
    params: dict[str, Any] = {}
    if channel_id is not None:
        params["channel_id"] = channel_id
    if all_channels is not None:
        params["all"] = all_channels
    r = client.delete(_BASE, headers=token_headers, params=params)
    assert r.status_code == expected_status, (
        f"Clear routing traces failed: {r.status_code} {r.text}"
    )
    return r.json() if r.status_code == expected_status else None


# ---------------------------------------------------------------------------
# Webhook delivery — shared with tests/api/server_channels/server_channels_routing_test.py
# ---------------------------------------------------------------------------


def post_channel_message(
    client: TestClient,
    channel: dict,
    signer,
    event: dict,
    *,
    stream_stub: Any = None,
    classify_result: Any = None,
    classify_no_match: bool = False,
    classify_side_effect: Any = None,
    classify_via_provider: bool = False,
    route_installed_side_effect: Any = None,
) -> tuple[Any, Any]:
    """Deliver one verified webhook event and drain the resulting background work.

    Passing no classifier argument patches ``classify`` to raise rather than
    leaving it live — see ``patched_routing_externals`` for why. Two shapes may
    legitimately name no answer, and in both the stub is installed and never
    invoked: a sender who owns nothing eligible (an empty ballot short-circuits
    everywhere), and a sender who owns exactly one eligible agent on a server
    whose auto-install list has nothing for them (Pass 1's conditional
    ``only_one``). Anything else has to say what the answer is
    (``classification(agent_id)``).

    ``classify_via_provider=True`` is the one exception, and it is an
    *explicit* one rather than a hole: the message-text-gating tests patch
    ``agent_classifier.get_provider_manager`` themselves precisely so the real
    render/parse path runs and the real ``record_prompt`` /
    ``record_raw_response`` instrumentation fires. They have stubbed the model
    — one layer deeper than this helper does — so refusing at ``classify``
    would pre-empt the very code they exist to exercise. Note what the flag
    does *not* do: it removes this helper's stub, not the global
    ``block_llm_provider`` guard underneath, so a caller who passes it and then
    forgets its own provider patch still fails loudly instead of dialling out.

    Shares its classifier stub with ``server_channels_routing_test.py``'s
    ``_post`` (both call ``enter_classifier_patch``) and patches the same
    stream/send targets, plus ``route_installed_side_effect`` — a hook onto
    ``ChannelRoutingService._route_installed`` itself (not the candidate build
    or the classify call it wraps), used only to reach the thread-target-level
    exception path: an exception from either of those is already caught and
    swallowed *inside* ``_route_installed`` (see
    ``server_channels_routing_test.py::test_pass1_swallows_a_classifier_exception``),
    so it never reaches ``_route_installed_in_thread``'s own
    ``except: persist(...); raise``. Forcing ``_route_installed``
    itself to raise is the only way to exercise that outer boundary.
    """
    token = signer.token(audience=channel["config"]["project_number"])
    stub = stream_stub or StubAgentEnvConnector(response_text="ok")
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stub))
        send_mock = stack.enter_context(
            patch(_SEND_TARGET, AsyncMock(return_value="fake-ext-id"))
        )
        if route_installed_side_effect is not None:
            stack.enter_context(
                patch(_ROUTE_INSTALLED_TARGET, side_effect=route_installed_side_effect)
            )
        enter_classifier_patch(
            stack,
            classify_result=classify_result,
            classify_no_match=classify_no_match,
            classify_side_effect=classify_side_effect,
            classify_via_provider=classify_via_provider,
        )
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        from tests.utils.background_tasks import drain_tasks

        drain_tasks()
    return resp, send_mock


# ---------------------------------------------------------------------------
# Simulate / replay / recommendation — plain HTTP wrappers
# ---------------------------------------------------------------------------


def simulate_routing(
    client: TestClient,
    token_headers: dict[str, str],
    *,
    message: str,
    as_user_id: str,
    channel_id: str | None = None,
    include_catalog: bool = True,
    expected_status: int = 200,
) -> Any:
    """POST /admin/routing/simulate. Returns the trace (a RoutingDecisionPublic).

    The response is the *same shape* ``get_routing_trace`` returns, because the
    route returns ``RoutingTraceService.get``'s output rather than projecting
    anything of its own. A test comparing the two is comparing one function's
    output with itself, which is the point.

    ``channel_id`` names the channel to decide **under** (phase 6). Omitted, the
    request carries no channel at all and the run resolves
    ``ResolvedChannelPolicy.for_no_channel()`` — whose ``allow_identity_routing``
    is ``False`` by design, so an identity candidate can never reach that
    ballot. The key is left out of the body entirely rather than sent as
    ``null``, so the "no channel named" case exercises the shape a client
    actually sends.
    """
    body: dict[str, Any] = {
        "message": message,
        "as_user_id": as_user_id,
        "include_catalog": include_catalog,
    }
    if channel_id is not None:
        body["channel_id"] = channel_id
    r = client.post(
        f"{API}/admin/routing/simulate",
        headers=token_headers,
        json=body,
    )
    assert r.status_code == expected_status, (
        f"Simulate routing failed: {r.status_code} {r.text}"
    )
    return r.json() if r.status_code == expected_status else None


def replay_routing_trace(
    client: TestClient,
    token_headers: dict[str, str],
    trace_id: str,
    *,
    include_catalog: bool = True,
    expected_status: int = 200,
) -> Any:
    """POST /admin/routing/traces/{id}/replay. Returns {original, replay, diff}."""
    r = client.post(
        f"{_BASE}/{trace_id}/replay",
        headers=token_headers,
        json={"include_catalog": include_catalog},
    )
    assert r.status_code == expected_status, (
        f"Replay routing trace failed: {r.status_code} {r.text}"
    )
    return r.json() if r.status_code == expected_status else None


def draft_routing_recommendation(
    client: TestClient,
    token_headers: dict[str, str],
    trace_id: str,
    *,
    ref_id: str | None = None,
    expected_status: int = 200,
) -> Any:
    """POST /admin/routing/traces/{id}/recommendation. Returns the draft."""
    r = client.post(
        f"{_BASE}/{trace_id}/recommendation",
        headers=token_headers,
        json={"ref_id": ref_id},
    )
    assert r.status_code == expected_status, (
        f"Draft routing recommendation failed: {r.status_code} {r.text}"
    )
    return r.json() if r.status_code == expected_status else None


@contextmanager
def patched_routing_externals(
    *,
    classify_result: Any = None,
    classify_no_match: bool = False,
    classify_side_effect: Any = None,
    classify_via_provider: bool = False,
):
    """Patch the outbound send + the classifier around a direct API call.

    ``post_channel_message`` does this for a webhook delivery; simulate and
    replay are called straight from the admin API, so a test driving those
    patches the same two boundaries itself.

    ``AgentClassifier.classify`` is the classifier for **both** passes (Pass 1
    reaches it through ``AppMCPRoutingService._ai_classify``), so
    ``classify_result`` and ``classify_no_match`` govern whichever pass runs.
    Phase 5 collapsed three hand-built candidate lists into it; before that the
    single boundary was ``AIFunctionsService.route_to_agent``, which is now a
    ``list[dict]`` adapter over the same code and is no longer on the routing
    path.

    **Naming an answer is mandatory.** Passing neither argument patches the
    classifier to raise (:func:`refuse_to_classify`): it used to leave it
    unpatched, which meant the default behaviour of this helper was to call a
    live model. Sixteen call sites took that default.

    What the guard's first run measured is worth writing down, because it is
    not what "sixteen unstubbed call sites" sounds like: none of the sixteen
    actually invokes the classifier today — every one of those scenarios
    short-circuits before it (a single effective route, or an empty candidate
    list). They were not calling a model; they were one setup change away from
    it, silently. The live calls this domain has actually made came from
    elsewhere: ``generate_router_trigger_prompt`` (see
    ``patched_trigger_prompt_draft``) and a Phase-5 measurement run that
    exhausted a provider quota.

    ``classify_no_match=True`` is therefore a separate flag rather than
    ``classify_result=None``: ``None`` is the "you have not said" case, and a
    scenario that needs the classifier to *run and find nothing* — the whole
    no-match family — has to be able to say so. Naming it also makes the intent
    readable at the call site, which "return_value=None" is not.

    Yields the ``send_message`` mock. Asserting it was never called is one half
    of "simulate sent no reply" — the durable half is that no binding and no
    debug-feed outbound event exist either, which is what the no-side-effects
    test actually leans on.
    """
    with ExitStack() as stack:
        send_mock = stack.enter_context(
            patch(_SEND_TARGET, AsyncMock(return_value="fake-ext-id"))
        )
        enter_classifier_patch(
            stack,
            classify_result=classify_result,
            classify_no_match=classify_no_match,
            classify_side_effect=classify_side_effect,
            classify_via_provider=classify_via_provider,
        )
        yield send_mock


_TRIGGER_DRAFT_TARGET = (
    "app.services.ai_functions.ai_functions_service."
    "AIFunctionsService.generate_router_trigger_prompt"
)

#: What the stubbed trigger-prompt generator returns. A fixed string, so a test
#: can assert the value reached the response without asserting on model wording.
STUB_TRIGGER_DRAFT = "Handle eigenvalue and matrix questions."


@contextmanager
def patched_trigger_prompt_draft(*, result: Any = None):
    """Stub ``generate_router_trigger_prompt`` for recommendation tests.

    Unlike the two routing classifiers, this one is **not** covered by the
    domain's ``patched_external_services`` stack — that mocks
    ``AIFunctionsService.is_available``, which this function never consults. So
    an unstubbed recommendation test reaches a real provider: it was found
    returning genuine model prose ("Handles calculating and analyzing
    eigenvalues...") in the suite, which makes the test cost money, need
    network, and vary run to run. Stub it.
    """
    with patch(
        _TRIGGER_DRAFT_TARGET,
        return_value=result
        or {"success": True, "trigger_prompt": STUB_TRIGGER_DRAFT},
    ) as mock:
        yield mock


# ---------------------------------------------------------------------------
# Scheduler-only purge — Rule-1 exemption
# ---------------------------------------------------------------------------


def purge_routing_traces(db: Session, *, retention_days: int | None = None) -> int:
    """Directly invoke ``RoutingTraceService.purge``. See module docstring."""
    from app.services.routing.routing_trace_service import RoutingTraceService

    return RoutingTraceService.purge(db, retention_days=retention_days)


# ---------------------------------------------------------------------------
# Backdated / off-channel-path trace seeding — Rule-1 exemption
# ---------------------------------------------------------------------------


def seed_routing_trace(
    *,
    created_at: datetime,
    origin: str = "server_channel",
    channel_id: uuid.UUID | str | None = None,
    user_id: uuid.UUID | str | None = None,
    outcome: str = "no_match",
    message: str = "seeded message",
) -> uuid.UUID | None:
    """Persist a routing decision through the real recorder, then backdate it.

    See module docstring for why this is a documented exemption rather than
    an HTTP call. Returns the row's id (``None`` only if persistence itself
    was disabled/swallowed, which should not happen with default settings).
    """
    from app.services.routing.routing_trace import RoutingTrace
    from app.services.routing.routing_trace_service import RoutingTraceService

    with RoutingTrace.capture(
        origin=origin,
        user_id=user_id,
        channel_id=channel_id,
        message=message,
    ) as trace:
        trace.record_outcome(outcome)
    # Backdate AFTER the capture closes — `finish()` (run by `capture`'s
    # `__exit__`) stamps `created_at`-independent latency, not the timestamp
    # itself, so this is safe to override afterwards.
    trace.created_at = created_at
    return RoutingTraceService.persist(trace)
