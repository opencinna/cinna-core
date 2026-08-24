"""``RoutingTrace`` recorder semantics.

Pure in-process logic with no I/O — no database, no settings, no HTTP — so it
is unit-tested rather than driven through the API (see the module docstring
on ``app.services.routing.routing_trace`` for why that isolation matters: a
later phase adds a DB-backed sibling module, and this one must stay clean).

This file pins the three properties §5 of the Auto Routing Tuning plan
(``docs/plans/auto_routing_tuning_plan.md``) demands of the recorder:

  1. Every ``record_*`` entry point no-ops when there is no active capture —
     an un-instrumented caller pays one ``ContextVar`` read and nothing else.
  2. Capture survives ``anyio.to_thread.run_sync``. Channel routing runs its
     LLM call in a worker thread; anyio propagates a *copy* of the calling
     context into that thread, and because the ``ContextVar`` holds a
     **mutable** ``RoutingTrace``, mutations made inside the worker must be
     visible through the caller's own reference once the awaited call
     returns. This is the single highest-value test in this file: per §12,
     silent loss of capture across the thread hop is the worst failure mode
     for a debugging aid, and ``tests/api/server_channels/conftest.py``'s
     ``patch_anyio_to_thread`` fixture runs that same offload **inline** for
     every API-level test — so nothing under ``tests/api/`` ever exercises
     the real cross-thread path. This unit test is the only place it is
     exercised, and it deliberately does NOT import or rely on that fixture.
  3. The recorder never raises into its caller, even fed garbage. §5 draws a
     sharp line here: the guard protects the *recording*, not the caller's
     own argument-expression evaluation (an ``AttributeError`` raised while
     *building* an argument happens before the call and is not this module's
     problem). What every guarded entry point must do is swallow an error
     raised while *it itself* reads attributes off (or stringifies) the
     value it was already handed.

A genuine gap in property 3 turned up while writing this file — the three
``record_prompt`` / ``record_raw_response`` / ``record_parse_outcome``
helpers called ``clamp()`` unguarded. It has since been fixed; see
``test_record_prompt_raw_response_and_parse_outcome_guard_bad_str`` below,
which now pins the guard.
"""
from __future__ import annotations

import dataclasses
import threading
import uuid
from typing import Any

import anyio
import pytest

from app.services.routing import routing_trace as rt


# --- garbage fixtures ---------------------------------------------------


class _Poison:
    """Looks like a normal value until the recorder tries to read it.

    ``str()`` and any attribute access both explode — the exact shape of
    input §5 warns about (a route/candidate-like object whose attributes
    can't be trusted)."""

    def __str__(self) -> str:  # noqa: D105 — deliberately explosive
        raise RuntimeError("boom: __str__")

    def __getattr__(self, name: str) -> Any:  # noqa: D105
        raise RuntimeError(f"boom: attribute {name!r}")


class _MissingAttrs:
    """A stand-in for a route/candidate object with none of the attributes
    the recorder normally reads via ``getattr(obj, name, default)``."""


# --- 1. No-op without an active capture ---------------------------------


def test_record_functions_no_op_without_active_capture() -> None:
    """Every mutator / record_* entry point is silently inert with no
    ``RoutingTrace.capture()`` span open — no exception, no state created."""
    assert rt.current() is None

    rt.begin_stage(rt.STAGE_PASS_1)
    rt.record_effective_routes([_MissingAttrs()])
    rt.record_candidate(kind=rt.KIND_AGENT, ref_id=uuid.uuid4(), name="Agent")
    rt.record_skip(
        kind=rt.KIND_AGENT,
        ref_id=uuid.uuid4(),
        name="Agent",
        reason=rt.SKIP_FOREIGN_OWNER,
    )
    rt.mark_candidate_skipped(ref_id=uuid.uuid4(), reason=rt.SKIP_ROUTE_INACTIVE)
    rt.record_match(method=rt.MATCH_AI, matched_pattern="foo")
    rt.record_prompt("rendered prompt")
    rt.record_raw_response("raw response")
    rt.record_parse_outcome(reason="ok", confidence=0.9)
    rt.record_llm_attempt(provider="openai", model="gpt-4", ok=True)
    rt.record_outcome(rt.OUTCOME_ROUTED, selected_agent_id=uuid.uuid4())
    rt.record_error(RuntimeError("boom"))

    # No capture was ever opened, so none of the above could have created or
    # mutated anything reachable — `current()` is still None afterwards.
    # (`trace_ids(**traces)` used to be asserted here too; it was deleted as
    # dead code with a misleading docstring — see the NOTE in
    # `routing_trace.py` right after the "Consumer-facing projections"
    # header — so there is nothing left of it to exercise.)
    assert rt.current() is None
    assert rt.summarize(None) == ""


# --- 2. Capture survives anyio.to_thread.run_sync -----------------------


def test_capture_survives_anyio_to_thread_run_sync() -> None:
    """The critical §5/§12 case: a real ``anyio.to_thread.run_sync`` offload
    (NOT patched to run inline) must still let a worker-thread mutation land
    on the caller's ``RoutingTrace`` reference.

    Uses ``anyio.run`` directly rather than any pytest-async plugin, so this
    test has zero dependency on how the API test suite's event loop is set
    up — and, crucially, does not go anywhere near
    ``tests/api/server_channels/conftest.py``'s ``patch_anyio_to_thread``,
    which exists precisely to make the offload synchronous for API tests and
    would defeat the point of this test if reused here.
    """
    worker_thread_idents: list[int] = []
    seen_trace_identities: list[int] = []

    async def _run() -> rt.RoutingTrace:
        with rt.RoutingTrace.capture(origin=rt.ORIGIN_SERVER_CHANNEL) as trace:

            def _worker() -> None:
                # Executes on a real OS worker thread spawned by anyio.
                worker_thread_idents.append(threading.get_ident())
                inner = rt.RoutingTrace.current()
                seen_trace_identities.append(id(inner))
                inner.add_candidates(
                    [
                        rt.CandidateTrace(
                            kind=rt.KIND_AGENT, ref_id="a1", name="Agent One"
                        )
                    ]
                )
                inner.record_outcome(rt.OUTCOME_ROUTED, selected_agent_id="a1")

            await anyio.to_thread.run_sync(_worker)
            return trace

    trace = anyio.run(_run)

    # Mutations made inside the worker thread are visible on the caller's
    # own `trace` reference — this is the whole point of the mutable
    # ContextVar design.
    assert len(trace.stages) == 1
    assert [c.ref_id for c in trace.stages[0].candidates] == ["a1"]
    assert trace.outcome == rt.OUTCOME_ROUTED
    assert trace.selected_agent_id == "a1"

    # Sanity checks on *how* that happened: it really ran on a different OS
    # thread, and `RoutingTrace.current()` inside that thread really
    # resolved to the SAME instance the caller holds — not a stale copy that
    # happens to produce the same values.
    assert worker_thread_idents, "worker never ran"
    assert worker_thread_idents[0] != threading.get_ident()
    assert seen_trace_identities == [id(trace)]


# --- 3. The recorder never raises into its caller ------------------------


def test_record_functions_never_raise_with_malformed_arguments() -> None:
    """Feed every guarded entry point garbage — missing attributes, objects
    that explode on ``str()``/attribute access, and outright wrong types —
    with an active capture, and confirm none of it escapes to the caller.

    This is the module docstring's "every entry point swallows its own
    errors" claim, exercised directly: each function below wraps its own
    attribute reads in a guard, so a poisoned *value already in hand* (as
    opposed to a poisoned call-site *expression* — out of scope, see the
    module docstring) must never propagate.
    """
    poison = _Poison()
    missing = _MissingAttrs()

    with rt.RoutingTrace.capture(origin=rt.ORIGIN_SERVER_CHANNEL) as trace:
        # record_effective_routes: a mix of routes missing every attribute
        # it reads, a route that explodes on attribute access, and outright
        # wrong types (None / str / int) in the same batch.
        rt.record_effective_routes([missing, poison, None, "not-a-route", 42])

        # record_candidate / record_skip: every string-ish field poisoned.
        rt.record_candidate(
            kind=poison,
            ref_id=poison,
            name=poison,
            trigger_prompt=poison,
            prompt_examples=poison,
            owner_email=poison,
        )
        rt.record_skip(kind=poison, ref_id=poison, name=poison, reason=poison)

        # mark_candidate_skipped: ref_id explodes on str().
        rt.mark_candidate_skipped(ref_id=poison, reason=poison)

        # record_match: matched_pattern explodes on str()/len().
        rt.record_match(method=poison, matched_pattern=poison)

        # record_llm_attempt: provider/model/error poisoned, ok and
        # latency_ms the wrong type entirely.
        rt.record_llm_attempt(
            provider=poison,
            model=poison,
            ok="not-a-bool",
            error=poison,
            latency_ms=poison,
        )

        # record_outcome: selected ids and confidence all wrong/poisoned.
        rt.record_outcome(
            rt.OUTCOME_ROUTED,
            match_method=poison,
            selected_agent_id=poison,
            selected_bundle_uuid=poison,
            confidence="not-a-float",
        )

        # record_error: an exception-shaped object that explodes on str().
        rt.record_error(poison)

        # begin_stage with a poisoned stage name.
        rt.begin_stage(poison)

    # The capture span itself completed cleanly (its own __exit__ never
    # raises), and projections over whatever partially landed above must
    # also never raise.
    assert rt.current() is None
    assert isinstance(trace.stages_payload(), list)
    assert isinstance(rt.summarize(trace), str)


# NOTE — real defect found while writing this file, NOT worked around here.
#
# `record_prompt`, `record_raw_response`, and `record_parse_outcome` call
# `clamp()` directly at module scope with no surrounding try/except, unlike
# every other entry point above (and unlike `record_match`, which wraps the
# structurally identical `clamp(matched_pattern, ...)` call). `clamp()`
# itself calls `str(text)` unguarded. So a prompt / raw-response / reason
# argument whose `__str__` raises propagates straight out of the recorder
# into the routing pipeline — the exact failure the module docstring rules
# out ("every entry point swallows its own errors") and the exact failure
# §5 calls "never break the pipeline".
#
# This was a real defect when this file was written: the three functions
# called `clamp()` as a bare argument expression, so a value whose `__str__`
# raised propagated out of the recorder into the caller. They now wrap it the
# way `record_match` already did, and this test is an ordinary regression
# guard rather than an xfail.
def test_record_prompt_raw_response_and_parse_outcome_guard_bad_str() -> None:
    poison = _Poison()
    with rt.RoutingTrace.capture(origin=rt.ORIGIN_SERVER_CHANNEL):
        rt.record_prompt(poison)
        rt.record_raw_response(poison)
        rt.record_parse_outcome(reason=poison)


def test_exception_escaping_the_capture_block_is_recorded_then_reraised() -> None:
    """``capture()``'s docstring: an exception escaping the ``with`` block is
    recorded as ``outcome="error"`` and then re-raised unchanged — the
    recorder observes a real pipeline failure, it does not swallow it."""

    class _Boom(RuntimeError):
        pass

    holder: list[rt.RoutingTrace] = []
    with pytest.raises(_Boom, match="pipeline exploded"):
        with rt.RoutingTrace.capture(origin=rt.ORIGIN_SERVER_CHANNEL) as trace:
            holder.append(trace)
            raise _Boom("pipeline exploded")

    trace = holder[0]
    assert trace.outcome == rt.OUTCOME_ERROR
    assert trace.error is not None
    # The exception TYPE is recorded; its message is deliberately NOT. A
    # provider SDK exception's message routinely echoes the request payload
    # back, and at the router's call site that payload is the rendered
    # classifier prompt containing the sender's words — so `record_error` now
    # goes through `describe_exception`, which keeps what diagnoses an outage
    # (type, provider, integer HTTP status) and drops the message body.
    assert "_Boom" in trace.error
    assert "pipeline exploded" not in trace.error


# --- record_error / record_outcome settle through _settle_locked ---------


def test_record_outcome_clears_stale_selection_on_non_routed_outcomes() -> None:
    """``record_outcome`` and ``record_error`` both funnel through the single
    ``_settle_locked`` writer. A stage may have picked an agent that a later
    filter then rejects — the terminal trace must not keep naming that agent
    once the outcome settles as ``no_match``."""
    with rt.RoutingTrace.capture(origin=rt.ORIGIN_SERVER_CHANNEL) as trace:
        rt.record_outcome(
            rt.OUTCOME_NO_MATCH,
            selected_agent_id=uuid.uuid4(),
            selected_bundle_uuid=uuid.uuid4(),
            confidence=0.87,
        )

    assert trace.outcome == rt.OUTCOME_NO_MATCH
    assert trace.selected_agent_id is None
    assert trace.selected_bundle_uuid is None
    assert trace.confidence is None


def test_record_error_also_clears_a_prior_selection() -> None:
    """A trace that had already recorded a routed selection, then fails
    later (e.g. delivery to the agent errors), must not keep claiming the
    earlier selection once it settles as `error` — same settler, same rule."""
    with rt.RoutingTrace.capture(origin=rt.ORIGIN_SERVER_CHANNEL) as trace:
        rt.record_outcome(rt.OUTCOME_ROUTED, selected_agent_id=uuid.uuid4(), confidence=0.5)
        assert trace.outcome == rt.OUTCOME_ROUTED
        assert trace.selected_agent_id is not None

        rt.record_error(RuntimeError("classifier blew up"))

    assert trace.outcome == rt.OUTCOME_ERROR
    assert trace.selected_agent_id is None
    assert trace.confidence is None
    assert trace.error is not None
    # Type only — see the de-tainting note above.
    assert "RuntimeError" in trace.error
    assert "classifier blew up" not in trace.error


def test_finish_settles_unresolved_trace_to_no_match_without_stale_selection() -> None:
    """If nothing ever calls ``record_outcome``, ``capture()``'s finally
    block settles the trace to ``no_match`` on exit. A stage that merely
    noted *how* it matched (``note_match_method``) without a downstream
    consumer confirming the route must not leave a selection behind — but
    ``match_method`` itself is allowed to survive, per
    ``note_match_method``'s docstring ("how the last stage matched", not
    "how the decision was reached")."""
    with rt.RoutingTrace.capture(origin=rt.ORIGIN_SERVER_CHANNEL) as trace:
        rt.record_match(method=rt.MATCH_AI, matched_pattern=None)
        # Deliberately no record_outcome call.

    assert trace.outcome == rt.OUTCOME_NO_MATCH
    assert trace.selected_agent_id is None
    assert trace.selected_bundle_uuid is None
    assert trace.confidence is None
    assert trace.match_method == rt.MATCH_AI


# --- 4. A pass that ran always leaves a stage ---------------------------
#
# §11a Rule 1 ("the dangerous state must not be able to look routine"), third
# instance and the first found in the *read* path. `capture()` used to
# materialise a stage only as a side effect of a stage-level mutator
# (`begin_stage` / `add_candidates` / `add_llm_attempt` / `update_stage` are
# the only callers of `_stage_locked`), while the entire terminal-verdict
# vocabulary — `record_outcome`, `record_error`, `finish`, `note_match_method`
# — leaves `stages` untouched. So a pass that ran and found nothing persisted
# with `stages == []`, indistinguishable from a pass that never ran at all.
#
# The production case is not a test artifact: `_route_catalog` in
# `channel_inbound_service` short-circuits with a bare
# `record_outcome(OUTCOME_NO_MATCH)` when the `ServerAutoInstallBundle` table
# is empty — the default state of every fresh deployment — so on any server
# whose admin has not populated the auto-install list, every no-match trace
# omitted Pass 2 entirely.


def test_capture_materialises_its_stage_even_when_nothing_is_recorded() -> None:
    """A capture block that records absolutely nothing still yields exactly
    one stage, named after the stage the span was opened for."""
    with rt.RoutingTrace.capture(
        origin=rt.ORIGIN_SERVER_CHANNEL, stage=rt.STAGE_PASS_2
    ) as trace:
        pass

    payload = trace.stages_payload()
    assert [stage["stage"] for stage in payload] == [rt.STAGE_PASS_2]
    assert payload[0]["candidates"] == []


def test_ran_and_found_nothing_is_distinguishable_from_never_ran() -> None:
    """The defect this pins, in the exact shape production hits it.

    ``_route_catalog``'s empty-table short-circuit records a terminal outcome
    and nothing else. That trace must still say "Pass 2 ran, with zero
    candidates" — while a decision that genuinely never opened a Pass 2 span
    says so by *not* carrying a ``pass_2`` stage at all. Both rows are
    ``no_match``; the stage list is the only thing that separates them.
    """
    # Pass 2 ran and found nothing — the fresh-deployment case.
    with rt.RoutingTrace.capture(
        origin=rt.ORIGIN_SERVER_CHANNEL, stage=rt.STAGE_PASS_2
    ) as ran:
        rt.record_outcome(rt.OUTCOME_NO_MATCH)

    # Pass 2 never ran: the decision stopped after Pass 1.
    with rt.RoutingTrace.capture(
        origin=rt.ORIGIN_SERVER_CHANNEL, stage=rt.STAGE_PASS_1
    ) as never_ran:
        rt.record_outcome(rt.OUTCOME_NO_MATCH)

    assert ran.outcome == never_ran.outcome == rt.OUTCOME_NO_MATCH

    ran_stages = {stage["stage"]: stage for stage in ran.stages_payload()}
    never_ran_stages = {
        stage["stage"]: stage for stage in never_ran.stages_payload()
    }

    # "Ran and found nothing": the stage is present, and empty.
    assert rt.STAGE_PASS_2 in ran_stages
    assert ran_stages[rt.STAGE_PASS_2]["candidates"] == []
    assert ran_stages[rt.STAGE_PASS_2]["match_method"] is None

    # "Never ran": no pass_2 entry exists to be misread as one.
    assert rt.STAGE_PASS_2 not in never_ran_stages


def test_eager_stage_materialisation_is_idempotent() -> None:
    """Capture entry plus later stage-level records for the same name must
    converge on ONE stage. ``_stage_locked`` is get-or-create, and the eager
    call must not turn every subsequent addressed record into a duplicate."""
    with rt.RoutingTrace.capture(
        origin=rt.ORIGIN_SERVER_CHANNEL, stage=rt.STAGE_PASS_2
    ) as trace:
        # Explicitly re-addressed...
        rt.begin_stage(rt.STAGE_PASS_2)
        rt.record_candidate(
            kind=rt.KIND_BUNDLE,
            ref_id=uuid.uuid4(),
            name="Bundle One",
            stage=rt.STAGE_PASS_2,
        )
        # ...and un-addressed, which resolves to the same current stage.
        rt.record_skip(
            kind=rt.KIND_BUNDLE,
            ref_id=uuid.uuid4(),
            name="Bundle Two",
            reason=rt.SKIP_NO_REVISION,
        )
        rt.record_llm_attempt(provider="openai", model="gpt-4", ok=True)

    payload = trace.stages_payload()
    assert [stage["stage"] for stage in payload] == [rt.STAGE_PASS_2]
    assert [c["name"] for c in payload[0]["candidates"]] == [
        "Bundle One",
        "Bundle Two",
    ]
    assert len(payload[0]["llm_attempts"]) == 1

    # A *different* stage still gets its own entry — idempotence is per name,
    # not "only ever one stage".
    with rt.RoutingTrace.capture(
        origin=rt.ORIGIN_SERVER_CHANNEL, stage=rt.STAGE_PASS_1
    ) as two_stage:
        rt.begin_stage(rt.STAGE_PASS_2)

    assert [stage["stage"] for stage in two_stage.stages_payload()] == [
        rt.STAGE_PASS_1,
        rt.STAGE_PASS_2,
    ]


def test_capture_entry_never_raises_when_stage_materialisation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§11a Rule 2: the eager call must not reintroduce instance 2's shape.

    ``clamp()``/``_sha256()`` were once unguarded in ``RoutingTrace.__init__``
    — worse than the ``record_*`` variants precisely because that code runs
    inside ``capture().__enter__`` and aborts the whole routing pass rather
    than dropping a field. Anything added to the entry path inherits that
    stake, so it is proved here by firing, not by reading the code:

    (a) a poison ``stage`` through the public signature, and
    (b) a hard failure inside ``_stage_locked`` itself.

    In both cases entering the span must succeed, the block must run, and
    ``finish()`` must still settle the trace.
    """
    # (a) The signature permits a poison stage name, so fire one.
    with rt.RoutingTrace.capture(
        origin=rt.ORIGIN_SERVER_CHANNEL, stage=_Poison()
    ) as poisoned:
        pass

    assert poisoned.outcome == rt.OUTCOME_NO_MATCH
    # Projection over a poisoned stage name is guarded too (``asdict`` deep-
    # copies it) — it degrades to ``[]`` rather than escaping.
    assert isinstance(poisoned.stages_payload(), list)

    # (b) Materialisation failing outright, not just on a hostile argument.
    def _boom(self: rt.RoutingTrace, stage: str | None) -> rt.StageTrace:
        raise RuntimeError("boom: stage materialisation")

    monkeypatch.setattr(rt.RoutingTrace, "_stage_locked", _boom)

    reached_body = False
    with rt.RoutingTrace.capture(
        origin=rt.ORIGIN_SERVER_CHANNEL, stage=rt.STAGE_PASS_2
    ) as broken:
        reached_body = True

    assert reached_body, "capture().__enter__ did not reach the block body"
    assert broken.stages == []
    assert broken.outcome == rt.OUTCOME_NO_MATCH


# --- 5. The message-text allowlist --------------------------------------
#
# `SAFE_STAGE_FIELDS` is a projection spec written as string field names, so a
# typo or a renamed dataclass field would silently hide a field that was meant
# to survive the gate — the allowlist's one failure mode, and invisible to
# reading. Checked here against `dataclasses.fields`, by execution.


def test_safe_stage_fields_name_only_real_dataclass_fields() -> None:
    stage_fields = {f.name for f in dataclasses.fields(rt.StageTrace)}
    candidate_fields = {f.name for f in dataclasses.fields(rt.CandidateTrace)}
    attempt_fields = {f.name for f in dataclasses.fields(rt.LLMAttempt)}

    assert set(rt.SAFE_STAGE_FIELDS) <= stage_fields
    assert set(rt.SAFE_CANDIDATE_FIELDS) <= candidate_fields
    assert set(rt.SAFE_LLM_ATTEMPT_FIELDS) <= attempt_fields

    # The fields the gate exists for are absent — by omission, which is the
    # whole point of an allowlist: nothing has to remember to name them.
    assert "prompt" not in rt.SAFE_STAGE_FIELDS
    assert "raw_response" not in rt.SAFE_STAGE_FIELDS
    assert "error" not in rt.SAFE_LLM_ATTEMPT_FIELDS

    # Every list-valued stage field carries its own nested spec. Declared as a
    # scalar it would be dropped by the projection (fail-closed), so this pins
    # the intent rather than the accident.
    nested = {
        f.name
        for f in dataclasses.fields(rt.StageTrace)
        if f.name in rt.SAFE_STAGE_FIELDS and f.default_factory is list  # type: ignore[misc]
    }
    for name in nested:
        assert isinstance(rt.SAFE_STAGE_FIELDS[name], tuple), name


def test_describe_exception_keeps_the_diagnosis_and_drops_the_message() -> None:
    """The de-tainting item 2 asks for, at the level it happens: an exception's
    TYPE, provider and integer HTTP status survive; its message — which for a
    provider SDK error routinely echoes the request payload, i.e. the rendered
    router prompt containing the sender's words — does not."""

    class _Rate(RuntimeError):
        status_code = 429

    described = rt.describe_exception(_Rate("please help me with SECRET-TEXT"), provider="openai")
    assert described is not None
    assert "_Rate" in described
    assert "openai" in described
    assert "429" in described
    assert "SECRET-TEXT" not in described

    # No provider, no status: still the type, still no message.
    plain = rt.describe_exception(ValueError("SECRET-TEXT"))
    assert plain == "ValueError"

    # A caller-supplied literal is the caller's own words, not an exception's.
    assert rt.describe_exception("cascade unavailable") == "cascade unavailable"
    assert rt.describe_exception(None) is None
    # Total, like clamp(): callers pass it as a bare argument expression.
    assert isinstance(rt.describe_exception(_Poison()), str)


# --- Totality contracts -------------------------------------------------
#
# `clamp()` documented itself as "Total by design" while `if not text:` — its
# first statement — sat OUTSIDE its own `try`, so a raising `__bool__` escaped
# it. Nothing broke, because every call site happened to be inside a `try`;
# the hazard was that the next instrumentation point would trust the
# docstring. §11a Rule 2's proof standard applies to contracts as much as to
# call sites: fire the poison object, do not read the code. These tests are
# the mechanism that stops the claim silently becoming false again — a
# refactor that moves a guard back inside a branch fails here rather than
# waiting to be rediscovered by whoever trusts the paragraph next.
#
# The five shapes are the ones §11a Rule 2 names. `__len__` and `__format__`
# are added because a `str` SUBCLASS reaches `len(text)` and the truncation
# f-string without passing through any coercion, and the first pass of this
# audit had guarded only the coercion.


class _ShapedPoison:
    """Raises from exactly one dunder, so a failure names its own shape."""

    def __init__(self, shape: str) -> None:
        self._shape = shape

    def _maybe(self, shape: str) -> None:
        if self._shape == shape:
            raise RuntimeError(f"boom: {shape}")

    def __repr__(self) -> str:
        return f"<_ShapedPoison {self._shape}>"

    def __str__(self) -> str:
        self._maybe("__str__")
        return "readable"

    def __bool__(self) -> bool:
        self._maybe("__bool__")
        return True

    def __eq__(self, other: Any) -> bool:
        self._maybe("__eq__")
        return NotImplemented

    def __hash__(self) -> int:
        self._maybe("__hash__")
        return 1

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if self._shape == "__getattr__":
            raise RuntimeError(f"boom: attribute {name!r}")
        raise AttributeError(name)


class _PoisonLen(str):
    """A ``str`` subclass whose ``len()`` explodes — skips every coercion."""

    def __len__(self) -> int:
        raise RuntimeError("boom: __len__")


class _PoisonFormat(str):
    """A ``str`` subclass that cannot be interpolated into an f-string."""

    def __format__(self, spec: str) -> str:
        raise RuntimeError("boom: __format__")


_RULE_2_SHAPES = ("__str__", "__bool__", "__eq__", "__hash__", "__getattr__")


def _every_poison() -> list[Any]:
    """One object per shape, including the two ``str``-subclass shapes."""
    values: list[Any] = [_ShapedPoison(shape) for shape in _RULE_2_SHAPES]
    values.append(_PoisonLen("x" * (rt.TRACE_TEXT_MAX_CHARS + 50)))
    values.append(_PoisonFormat("x" * (rt.TRACE_TEXT_MAX_CHARS + 50)))
    return values


def test_clamp_is_total_against_every_poison_shape() -> None:
    """The contract the docstring claims, pinned.

    ``__bool__`` is the shape that was actually broken: `if not text:` ran
    before the guard. ``__len__`` is the one the first fix attempt would still
    have missed, which is why the guard wraps the body rather than the two
    expressions anyone thought of.
    """
    for poison in _every_poison():
        assert rt.clamp(poison) is None or isinstance(rt.clamp(poison), str)
        # A tiny limit forces the truncation branch — the f-string and the
        # slice — for the shapes that survive the earlier statements.
        assert rt.clamp(poison, 3) is None or isinstance(rt.clamp(poison, 3), str)

    # Totality must not have cost the ordinary behaviour.
    assert rt.clamp(None) is None
    assert rt.clamp("") is None
    assert rt.clamp("short") == "short"
    long = rt.clamp("y" * 50, 10)
    assert long is not None and long.startswith("y" * 10) and "truncated" in long


def test_sha256_is_total_against_every_poison_shape() -> None:
    """``_sha256`` is ``clamp``'s neighbour in ``__init__`` and had the same
    two unguarded statements. A trace without a digest still carries a
    verdict; a routing pass aborted inside ``capture().__enter__`` does not."""
    for poison in _every_poison():
        assert rt._sha256(poison) is None or isinstance(rt._sha256(poison), str)

    assert rt._sha256(None) is None
    assert rt._sha256("") is None
    digest = rt._sha256("hello")
    assert isinstance(digest, str) and len(digest) == 64


def test_str_or_none_is_total_against_every_poison_shape() -> None:
    """The one that was **live** rather than latent.

    ``_str_or_none`` is called from three unguarded assignments in
    ``RoutingTrace.__init__``, so a raising ``__str__`` on an id aborted the
    whole routing pass — Rule 2 instance 2 again, in the same constructor, in
    the fields nobody re-checked. See the capture test below for the
    end-to-end proof; this pins the helper itself.
    """
    for poison in _every_poison():
        assert rt._str_or_none(poison) is None or isinstance(
            rt._str_or_none(poison), str
        )

    assert rt._str_or_none(None) is None
    ident = uuid.uuid4()
    assert rt._str_or_none(ident) == str(ident)


def test_describe_exception_is_total_against_every_poison_shape() -> None:
    """Audited alongside the others; it already held, and now says so in a
    test rather than only in its docstring."""

    class _PoisonExc(Exception):
        def __init__(self, shape: str) -> None:
            self._shape = shape

        def __str__(self) -> str:
            if self._shape == "__str__":
                raise RuntimeError("boom")
            return "readable"

        def __bool__(self) -> bool:
            if self._shape == "__bool__":
                raise RuntimeError("boom")
            return True

        def __eq__(self, other: Any) -> bool:
            if self._shape == "__eq__":
                raise RuntimeError("boom")
            return NotImplemented

        def __hash__(self) -> int:
            if self._shape == "__hash__":
                raise RuntimeError("boom")
            return 1

        def __getattr__(self, name: str) -> Any:
            if name.startswith("_"):
                raise AttributeError(name)
            if self._shape == "__getattr__":
                raise RuntimeError("boom")
            raise AttributeError(name)

    for shape in _RULE_2_SHAPES:
        described = rt.describe_exception(_PoisonExc(shape))
        assert isinstance(described, str) and described
        assert isinstance(
            rt.describe_exception(_PoisonExc(shape), provider="openai"), str
        )
    # And a poisoned *provider* label, which is also a bare argument.
    for poison in _every_poison():
        assert isinstance(rt.describe_exception(RuntimeError("x"), provider=poison), str)


def test_capture_survives_a_poisoned_id_rather_than_aborting_the_pass() -> None:
    """The end-to-end shape of the live defect.

    ``capture()`` runs ``__init__``, and ``__init__`` coerced ``user_id`` /
    ``channel_id`` / ``actor_user_id`` with a bare ``str()``. A value whose
    ``__str__`` raised therefore threw out of ``capture().__enter__`` — not
    dropping a field, but aborting the routing pass the trace was only
    supposed to be watching. This is Rule 2 instance 2's exact failure mode,
    which is why it gets its own end-to-end test and not only a helper test.
    """
    for field in ("user_id", "channel_id", "actor_user_id", "thread_key", "message"):
        for poison in _every_poison():
            with rt.RoutingTrace.capture(
                origin=rt.ORIGIN_SERVER_CHANNEL, **{field: poison}
            ) as trace:
                rt.record_outcome(rt.OUTCOME_NO_MATCH)
            # The pass completed and the trace still carries its verdict.
            assert trace.outcome == rt.OUTCOME_NO_MATCH


def test_summarize_and_stages_payload_stay_total_over_a_poisoned_trace() -> None:
    """Both claim "never raises" in their docstrings. Both are reached with a
    trace whose recorded fields were built from poison, which is the only way
    a poisoned value gets *into* them."""
    for poison in _every_poison():
        with rt.RoutingTrace.capture(origin=rt.ORIGIN_SERVER_CHANNEL) as trace:
            rt.record_prompt(poison)
            rt.record_candidate(kind=rt.KIND_AGENT, ref_id=poison, name="n")
            rt.record_parse_outcome(reason=poison)
            rt.record_llm_attempt(provider="p", model=poison, ok=False, error=poison)
            payload = trace.stages_payload()
            assert isinstance(payload, list)
            assert isinstance(rt.summarize(trace), str)
