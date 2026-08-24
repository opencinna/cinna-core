"""In-process capture of a single routing decision.

Channel / App MCP / identity routing all make an LLM-mediated choice per
message, and until now the only trace of it was a one-line ``no_match`` in the
channel debug buffer plus INFO log spam. When an admin asks "why didn't it find
my agent", the answer needs the things no log line carries: which agents were
even *candidates*, which were dropped and why, what prompt the classifier saw,
which providers were tried, and what the model actually said.

``RoutingTrace`` is that record. It is a **span object over a ``ContextVar``**:

- A consumer opts in with ``RoutingTrace.capture(...)``. Everything recorded
  inside the ``with`` block lands on that trace.
- Every instrumentation point calls the module-level ``record_*`` helpers, which
  read :func:`current` and **no-op when there is no active capture**. That is
  what keeps this feature out of the routing signatures: an un-instrumented
  caller pays one ``ContextVar`` read.
- The recorder is **mutable and lock-guarded**. Channel routing runs its LLM
  call in a worker thread (``anyio.to_thread.run_sync``), which propagates a
  *copy* of the caller's context; because the ContextVar holds a mutable object,
  appends made inside the thread are visible through the caller's reference.
  Captures are nevertheless opened *inside* the thread target so the span's
  lifetime is unambiguous, and the target hands the trace back as a return value
  (see ``ChannelRoutingService._route_installed_in_thread``).

**The single highest-value rule:** ``candidates`` must include *excluded*
candidates carrying a ``skip_reason``. A trace listing only the finalists cannot
diagnose the failure mode that actually bites — the expected agent was never a
candidate at all.

**A pass that ran always leaves a stage.** ``capture()`` materialises its named
stage on entry, so "ran and found nothing" is distinguishable from "never ran"
*by construction* rather than by the coincidence of which code path happened to
reach a stage-creating mutator. The whole terminal-verdict vocabulary
(``record_outcome``, ``record_error``, ``finish``, ``note_match_method``) leaves
``stages`` untouched, so without the eager creation a short-circuiting pass —
e.g. ``_route_catalog`` returning early on an empty ``ServerAutoInstallBundle``
table, which is the *default state of a fresh deployment* — persisted with
``stages == []`` and read as though it had never executed.

**What survives ``ROUTING_TRACE_STORE_MESSAGE_TEXT=False`` is an allowlist,
not a denylist.** ``SAFE_STAGE_FIELDS`` (below the dataclasses) names the stage
fields that may be stored and served while the sender's text is gated off;
everything else — including any field added after this was written — is withheld
by default. Read the comment above that constant before adding a field to
``StageTrace``, ``CandidateTrace`` or ``LLMAttempt``: three rounds of enumerating
the tainted fields each missed one, which is why the polarity is inverted.

**Recording must never break routing.** Every entry point swallows its own
errors, exactly like ``ChannelDebugBuffer.record``. Note the same trap
documented there: the guard protects the *recording*, not the caller's argument
expressions, which Python evaluates first. Keep call-site arguments to
attributes you are certain exist — an ``AttributeError`` raised while building
an argument lands in the caller's broad ``except``, not in ours. The helpers
below therefore take whole objects and do their attribute reads *inside* the
guard wherever they can.

**Nothing here is persisted, and nothing here may import ``app.*``.** Durable
storage, retention and the admin read API live in ``routing_trace_service``,
which this module must never import — ``app/agents/`` imports *this* file, and
``app/agents/`` sits below ``app/services/``. That inversion is harmless only
while the imported module pulls in nothing but the standard library; a model or
settings dependency here closes the cycle. An architecture test enforces it.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# Clamp for any free text held on a trace (message, prompt, raw response).
# Duplicates the default of ``ROUTING_TRACE_TEXT_MAX_CHARS`` (and of
# ``SERVER_CHANNEL_DEBUG_TEXT_MAX_CHARS``) rather than reading it: this module
# may not import settings — see the docstring. ``RoutingTraceService`` re-clamps
# against the live setting on the way to the database, so an operator lowering
# it takes effect on what is *stored* even though the in-memory trace used this
# constant.
TRACE_TEXT_MAX_CHARS = 2_000

# Upper bound on the one-line human summary handed to the debug buffer. That
# buffer clamps its own ``text`` field but not ``summary``.
SUMMARY_MAX_CHARS = 400


# --- Vocabularies -----------------------------------------------------------
# Plain strings, matching the feature's status-string convention. Consumers
# must tolerate unknown values.

ORIGIN_SERVER_CHANNEL = "server_channel"
#: RESERVED — Phase 5 vocabulary, **not reachable today**. Nothing opens a
#: capture with either of these, so no trace has ever carried one. They are
#: declared here so the origin vocabulary is written down in one place, not
#: because anything emits them. The setting that used to branch on
#: ``ORIGIN_APP_MCP`` (``ROUTING_TRACE_APP_MCP_MODE``) has been removed for
#: exactly that reason — an operator setting it to ``off`` would have believed
#: they disabled a capture that was never running. Reintroduce that setting in
#: the same change that starts emitting ``ORIGIN_APP_MCP``, never before
#: (plan §4).
ORIGIN_APP_MCP = "app_mcp"
ORIGIN_IDENTITY = "identity"
#: LIVE as of Phase 3, and the first origin other than ``server_channel`` that
#: anything actually writes. ``POST /admin/routing/simulate`` and
#: ``.../traces/{id}/replay`` open their captures with this, and those rows are
#: the only ones carrying ``actor_user_id`` (the admin who ran it) — the real
#: path leaves it NULL.
#:
#: **Readers must not assume a single origin.** The list route's ``origin``
#: filter is a free-form string and always was, ``_NameResolver`` handles the
#: NULL ``channel_id`` a simulate row carries, and the retention purge is
#: origin-blind — so a simulate row expires on the same window as a real one,
#: which is the intended behaviour and not an oversight. What a simulate row is
#: *not* is evidence that a message was ever sent: it is an admin's what-if.
#: Anything that counts traffic, or reads the table as a record of what senders
#: did, has to filter ``origin="server_channel"`` explicitly.
ORIGIN_SIMULATE = "simulate"

STAGE_PASS_1 = "pass_1"
STAGE_PASS_2 = "pass_2"
STAGE_IDENTITY_STAGE2 = "identity_stage2"

OUTCOME_ROUTED = "routed"
OUTCOME_NO_MATCH = "no_match"
OUTCOME_ERROR = "error"
OUTCOME_PARKED_INSTALL = "parked_install"

MATCH_PATTERN = "pattern"
MATCH_AI = "ai"
MATCH_ONLY_ONE = "only_one"

SKIP_ALREADY_INSTALLED = "already_installed"
SKIP_NOT_INSTALLABLE = "not_installable"
SKIP_NO_TRIGGER_PROMPT = "no_trigger_prompt"
SKIP_IDENTITY_ROUTE = "identity_route"
SKIP_FOREIGN_OWNER = "foreign_owner"
SKIP_ROUTE_INACTIVE = "route_inactive"
#: The bundle row is on the auto-install list but has no resolvable latest
#: revision — a data-integrity drop that was previously silent.
SKIP_NO_REVISION = "no_revision"
#: The route resolved to an agent id with no ``agent`` row behind it. A
#: dangling reference, not an inactive route — same distinction as
#: ``SKIP_NO_REVISION`` vs ``SKIP_NO_TRIGGER_PROMPT`` on the bundle side.
SKIP_AGENT_MISSING = "agent_missing"
#: The bundle side of ``SKIP_AGENT_MISSING``: the id that won Pass 2 has no
#: ``agent_bundle`` row behind it any more. Its own reason rather than
#: ``SKIP_NO_REVISION``, which means "published nothing" and would send the
#: reader off to publish a bundle that is simply gone.
SKIP_BUNDLE_MISSING = "bundle_missing"
#: Pass 2 held this bundle and could have offered it, but Pass 1 matched one of
#: the sender's own agents first, so the auto-install pass never classified.
#:
#: **Not a filter result** — it is the only ``skip_reason`` recorded for a
#: candidate that passed every gate. It exists because Pass 1's single-candidate
#: short-circuit scans the catalog to decide whether it may skip the classifier
#: (``ChannelRoutingService._catalog_ballot``), and that scan is written to the
#: trace so an admin can see what the choice space actually held. Recording
#: those rows as *eligible* would be the lie: they would join the
#: "N eligible candidates" the verdict counts and the near-miss ranking, and a
#: reachability diagnosis would tell somebody their bundle "was an eligible
#: candidate and the classifier did not pick it" about a classifier that was
#: never given it.
SKIP_PASS_1_MATCHED = "pass_1_matched"
#: Identity Stage 2 only: the binding is active and accessible, but has no
#: ``IdentityBindingAssignment`` for *this* caller, so every Stage-2 path aborts
#: on it after selecting it. Distinct from "not a candidate": it was on the
#: ballot and could even win, and the request would still end nowhere.
SKIP_NO_ASSIGNMENT = "no_assignment"
#: Stage 1 only: an identity **owner** who named this caller on at least one
#: binding, but none of those bindings is currently reachable — the binding is
#: switched off, the owner disabled it for this caller, or the caller has not
#: enabled the contact. The person is therefore not on the ballot.
#:
#: Deliberately **not** ``SKIP_IDENTITY_ROUTE``, which is an older reason with
#: an unrelated meaning: *a channel decision rejected an identity route it was
#: offered*. That one describes a candidate that existed and lost; this one
#: describes a person who never became a candidate. Reusing it would make the
#: two indistinguishable in exactly the diagnosis they are read for.
SKIP_IDENTITY_UNAVAILABLE = "identity_unavailable"

KIND_AGENT = "agent"
KIND_BUNDLE = "bundle"


# --- Trace records ----------------------------------------------------------


@dataclass
class CandidateTrace:
    """One considered candidate — **including rejected ones**.

    ``eligible=False`` plus a ``skip_reason`` is the whole point: it is how a
    trace explains an agent that never reached the classifier.
    """

    kind: str  # "agent" | "bundle"
    ref_id: str
    name: str
    owner_email: str | None = None
    source: str = ""  # "admin" | "user" | "identity" | "catalog"
    trigger_prompt: str = ""  # clamped
    prompt_examples: str | None = None
    eligible: bool = True
    skip_reason: str | None = None


@dataclass
class LLMAttempt:
    """One provider the cascade actually tried — success or failure."""

    provider: str
    model: str | None = None
    ok: bool = False
    error: str | None = None
    latency_ms: int = 0


@dataclass
class StageTrace:
    """One routing stage: ``pass_1`` | ``pass_2`` | ``identity_stage2``."""

    stage: str
    candidates: list[CandidateTrace] = field(default_factory=list)
    match_method: str | None = None
    matched_pattern: str | None = None
    prompt: str | None = None  # rendered classifier prompt (clamped)
    raw_response: str | None = None  # clamped
    llm_attempts: list[LLMAttempt] = field(default_factory=list)
    confidence: float | None = None
    reason: str | None = None
    runner_up_id: str | None = None


# --- The message-text allowlist ---------------------------------------------
#
# **This is an allowlist, and that is the whole point of it.** Read this before
# adding a field to any dataclass above.
#
# ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` exists to keep the *sender's* words out
# of what is stored and served. It was enforced three times by enumerating the
# fields that carry those words, and three times the enumeration turned out to
# be one field short:
#
#   1. ``message_text`` — gated first, and declared complete.
#   2. ``stages[].prompt`` / ``stages[].raw_response`` — found later, gated via a
#      per-field denylist, and declared complete again.
#   3. ``llm_attempts[].error`` — found after that. Provider SDK exceptions
#      routinely echo the request payload, which at the router's call site *is*
#      the rendered prompt containing the sender's message.
#
# Sender text is a taint that *propagates*: it reaches new fields by ordinary,
# reviewable-looking changes (a new diagnostic field, a wrapped exception, a
# prompt template edit). A denylist makes a newly added field default to
# **exposed** and relies on somebody noticing — which is structurally always one
# field behind. An allowlist makes a new field default to **hidden**, so the
# failure mode is a missing diagnostic (recoverable, visible, annoying) rather
# than a leak (not recoverable, and invisible until an audit finds it).
#
# So: **a field is served with the gate off only if it is named here.** Adding a
# field to ``StageTrace`` / ``CandidateTrace`` / ``LLMAttempt`` and wanting it
# visible while the gate is off is a deliberate act — name it here, and only
# after establishing that it cannot carry anything the sender wrote (not "does
# not today": ``stages[].prompt`` did not carry it either, purely because a
# markdown template happened to be longer than the clamp).
#
# Spec shape: ``field name -> None`` for a JSON scalar copied through, or a
# tuple of field names for a list of nested objects, each projected through that
# tuple. A container field declared as a scalar (``None``) is **dropped**, not
# passed through — see ``_project_safe_stages`` in ``routing_trace_service`` —
# so mis-declaring a new nested structure fails closed too.

#: Candidate identity, eligibility, and the *agent owner's own* routing
#: configuration.
#:
#: ``trigger_prompt`` and ``prompt_examples`` are admitted deliberately, and the
#: reasoning is the bar any future widening has to clear — not a precedent that
#: the list takes whatever is convenient:
#:
#:   - **They are not sender-derived.** The gate exists for *the sender's* words.
#:     These two are configuration the agent's owner wrote, and nothing the
#:     sender says can reach them.
#:   - **They are already visible to this audience.** The read API is
#:     superuser-only, and a superuser can see both through ordinary platform
#:     surfaces anyway, so admitting them changes no exposure class.
#:   - **Withholding them degraded a diagnosis unrelated to sender privacy.**
#:     The tuning card's near-miss verdict ("closest: Equation Assistant 0.31")
#:     is a Jaccard overlap computed against the trigger prompt; without it the
#:     card can say an agent lost but not how narrowly.
#:
#: A field earns a place here by being answerable on all three counts *when it
#: is added*, not by resembling something already on the list.
SAFE_CANDIDATE_FIELDS: tuple[str, ...] = (
    "kind",
    "ref_id",
    "name",
    "owner_email",
    "source",
    "trigger_prompt",
    "prompt_examples",
    "eligible",
    "skip_reason",
)

#: Which providers were reached and how they fared. ``error`` is **not** here:
#: it is de-tainted at the recording site (``ProviderManager._note_attempt``
#: passes an exception type, not ``str(exc)``), but the de-tainting fixes the
#: field we know about while this list covers the one we have not met yet. The
#: outage diagnosis an operator needs is unaffected either way — ``ok`` survives
#: the gate here, and the row-level ``error`` column feeding ``?outcome=error``
#: is not part of ``stages`` at all.
SAFE_LLM_ATTEMPT_FIELDS: tuple[str, ...] = (
    "provider",
    "model",
    "ok",
    "latency_ms",
)

#: The stage projection used on the write path *and* the read path (one
#: definition, so the two cannot drift into gating different fields).
#: ``prompt`` and ``raw_response`` are absent on purpose — they are the sender's
#: words — and so is anything nobody has declared safe yet.
#:
#: ``reason`` was here, and was **removed in the same change that started
#: populating it from the model.** It held our own parse literals ("classifier
#: reply was not JSON") and was safe on those terms alone. Phase 5's prompt
#: contract asks the classifier for a ``reason``, and a model explaining why it
#: chose an agent quotes the message back at us — so the field became a rewrite
#: of the sender's words, exactly like ``raw_response``, the moment that reply
#: started landing in it.
#:
#: This is the case the allowlist exists for: a field that is safe *now*, stops
#: being safe *later*, and where the default has to be that somebody thinks
#: about it rather than that nobody notices. Removing it costs a diagnostic —
#: with the gate off, "the classifier chose NONE" is no longer served — and that
#: cost is accepted, because the alternative is enumerating which reasons are
#: ours and which are the model's, which is the per-field inventory this list
#: replaced after it failed three times.
SAFE_STAGE_FIELDS: dict[str, tuple[str, ...] | None] = {
    "stage": None,
    "match_method": None,
    "matched_pattern": None,
    "confidence": None,
    "runner_up_id": None,
    "candidates": SAFE_CANDIDATE_FIELDS,
    "llm_attempts": SAFE_LLM_ATTEMPT_FIELDS,
}


# --- Helpers ----------------------------------------------------------------


def clamp(text: str | None, limit: int = TRACE_TEXT_MAX_CHARS) -> str | None:
    """Bound a free-text field. Returns ``None`` for empty or unreadable input.

    Total by design: several ``record_*`` helpers call this *before* entering
    their own guard, so a value whose ``__str__`` raises — which is exactly the
    kind of object the module docstring warns about — would otherwise escape
    into the caller's pipeline. Coercion failure is treated as "no text": a
    diagnostic losing a field is always preferable to a diagnostic losing the
    caller's message.

    **The guard covers the whole body, and that is the point.** It used not to:
    only the ``str()`` coercion sat inside a ``try``, while ``if not text``
    — the *first* statement — sat outside it, so a raising ``__bool__``
    (one of the five shapes §11a Rule 2 names) escaped a function whose
    docstring promised it could not. Every call site happened to be inside a
    ``try``, so nothing broke; the hazard was that the next instrumentation
    point would trust this paragraph. Found by firing a poison object at it,
    not by reading it, and pinned by
    ``tests/unit/test_routing_trace.py::TestHelperTotality`` so a future
    refactor cannot make the claim false again in silence. The same sweep
    caught ``len(text)`` on a ``str`` *subclass* with a poisoned ``__len__``,
    which is why the guard is placed around the body rather than around the
    two expressions we happened to think of.
    """
    try:
        if not text:
            return None
        if not isinstance(text, str):
            text = str(text)
            if not text:
                return None
        if len(text) <= limit:
            return text
        return f"{text[:limit]}… (truncated)"
    except Exception:  # noqa: BLE001 — see the docstring
        logger.debug("Routing trace clamp failed", exc_info=True)
        return None


#: Attributes an exception may carry an HTTP status on, in preference order.
#: Only ``int`` values are read: an integer cannot smuggle text, while an SDK's
#: string ``code``/``status`` is free-form and would reopen the hole this helper
#: closes.
_STATUS_ATTRS = ("status_code", "http_status", "status", "code")


def describe_exception(
    exc: BaseException | str | None, *, provider: str | None = None
) -> str | None:
    """A **de-tainted** one-line description of a failure.

    ``str(exc)`` is not safe to record here. Provider SDK exceptions routinely
    echo the request payload back in their message, and at the router's call
    site that payload is the rendered classifier prompt — which contains the
    sender's message. Recording it put the sender's words into
    ``llm_attempts[].error`` and into the trace's own ``error``, outside the
    ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` gate entirely.

    The fix is a field made *safe*, not a field made *invisible*: gating the
    error would hide genuine outage diagnostics behind a **text** flag and
    defeat ``?outcome=error``, which is the one filter an operator reaches for
    when the router stops working. So this keeps the parts that diagnose an
    outage and cannot carry a message — the exception type, the provider, and an
    integer HTTP status when one is available — and drops the message body.

    Total by design, like :func:`clamp`: callers pass it as a bare argument
    expression, so it must never raise into the routing pipeline. A ``str`` is
    returned as-is (clamped), because a caller passing a literal is describing
    the failure in its own words rather than handing over an exception's.
    """
    try:
        if exc is None:
            return None
        if isinstance(exc, str):
            return clamp(exc, 400)
        parts = [type(exc).__name__]
        if provider:
            parts.append(f"from {provider}")
        try:
            for attr in _STATUS_ATTRS:
                value = getattr(exc, attr, None)
                # ``bool`` is an ``int`` subclass and never a status code.
                if isinstance(value, int) and not isinstance(value, bool):
                    parts.append(f"(HTTP {value})")
                    break
        except Exception:  # noqa: BLE001 — an object whose attributes explode
            # still deserves to have its type reported.
            logger.debug("Routing trace status probe failed", exc_info=True)
        return " ".join(parts)
    except Exception:  # noqa: BLE001 — a diagnostic must never break routing
        logger.debug("Routing trace exception description failed", exc_info=True)
        return "unavailable"


def _sha256(text: str | None) -> str | None:
    """Digest of the sender's message. Total, for :func:`clamp`'s reasons.

    ``RoutingTrace.__init__`` calls this and ``clamp`` on the same value, one
    line apart, and its comment explains why coercing caller text must not be
    able to raise out of ``capture().__enter__``. That reasoning applied here
    too and the code did not: ``if not text`` and ``str(text)`` were both
    outside any guard, so ``__bool__`` and ``__str__`` escaped. The call site's
    ``try`` covered it — which is the same accident that made ``clamp``'s false
    claim harmless, and the same one that stops being an accident the moment
    someone adds a second call site.

    ``None`` on failure: a trace without a digest still carries its verdict.
    """
    try:
        if not text:
            return None
        return hashlib.sha256(str(text).encode("utf-8", errors="replace")).hexdigest()
    except Exception:  # noqa: BLE001 — a diagnostic must never break routing
        logger.debug("Routing trace digest failed", exc_info=True)
        return None


def _str_or_none(value: Any) -> str | None:
    """Coerce an id to ``str``. Total — and this one was **live**, not latent.

    ``clamp`` and ``_sha256`` were unsafe helpers with safe call sites. This
    one was an unsafe helper called from three *unguarded* assignments in
    ``RoutingTrace.__init__`` — ``user_id``, ``channel_id``, ``actor_user_id``
    — sitting directly above the guarded block whose comment explains why
    coercion there must not raise. So a value with a raising ``__str__``
    aborted ``capture().__enter__`` and took the whole routing pass with it:
    §11a Rule 2 instance 2, in the same constructor it was first found in, in
    the fields nobody re-checked. Confirmed by execution.

    ``None`` on failure rather than a placeholder: a trace whose ``user_id`` is
    absent is filterable as absent, while a made-up id is a false attribution
    on a superuser read surface.
    """
    if value is None:
        return None
    try:
        return str(value)
    except Exception:  # noqa: BLE001 — a diagnostic must never break routing
        logger.debug("Routing trace id coercion failed", exc_info=True)
        return None


# --- The recorder -----------------------------------------------------------


_CURRENT: ContextVar[RoutingTrace | None] = ContextVar(
    "routing_trace_current", default=None
)


class RoutingTrace:
    """Mutable recorder for one routing decision.

    Instances are created by :meth:`capture` and reached by instrumentation
    through :func:`current`. Every mutator is internally guarded and returns
    ``None``; none of them may raise into the routing pipeline.
    """

    def __init__(
        self,
        *,
        origin: str,
        user_id: uuid.UUID | str | None = None,
        channel_id: uuid.UUID | str | None = None,
        actor_user_id: uuid.UUID | str | None = None,
        thread_key: str | None = None,
        message: str | None = None,
        stage: str = STAGE_PASS_1,
    ) -> None:
        self.trace_id: str = str(uuid.uuid4())
        self.origin: str = origin
        # These three coerce caller-supplied ids with str(), exactly like the
        # message fields below, and for a while only the message fields were
        # guarded — so a raising __str__ on an id threw out of
        # capture().__enter__ and aborted the routing pass. The guard now lives
        # in _str_or_none itself rather than in a try wrapped around these
        # lines: the next field added here inherits it instead of depending on
        # whoever adds it noticing this comment.
        self.user_id: str | None = _str_or_none(user_id)
        self.channel_id: str | None = _str_or_none(channel_id)
        self.actor_user_id: str | None = _str_or_none(actor_user_id)
        self.thread_key: str | None = thread_key
        # Both derive from caller-supplied text and both call str() on it, so a
        # value with a raising __str__/__bool__ must not throw out of
        # capture().__enter__. A trace missing its message beats a dropped
        # inbound message. clamp() and _sha256() are each total on their own
        # now; this try stays as the belt to their braces, because what it
        # protects is a routing pass and the cost of keeping it is nothing.
        try:
            self.message_text: str | None = clamp(message)
            self.message_sha256: str | None = _sha256(message)
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace message capture failed", exc_info=True)
            self.message_text = None
            self.message_sha256 = None
        self.created_at: datetime = datetime.now(UTC)

        self.default_stage: str = stage
        self.stages: list[StageTrace] = []

        self.outcome: str | None = None
        self.match_method: str | None = None
        self.selected_agent_id: str | None = None
        self.selected_bundle_uuid: str | None = None
        self.confidence: float | None = None
        self.error: str | None = None
        self.latency_ms: int = 0

        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._current_stage: str = stage

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def current(cls) -> RoutingTrace | None:
        """The trace for the active capture, or ``None`` when uncaptured."""
        try:
            return _CURRENT.get()
        except Exception:  # noqa: BLE001 — a diagnostic must never break routing
            return None

    @classmethod
    @contextmanager
    def capture(
        cls,
        *,
        origin: str,
        user_id: uuid.UUID | str | None = None,
        channel_id: uuid.UUID | str | None = None,
        actor_user_id: uuid.UUID | str | None = None,
        thread_key: str | None = None,
        message: str | None = None,
        stage: str = STAGE_PASS_1,
    ) -> Iterator[RoutingTrace]:
        """Open a capture span. Everything recorded inside lands on the trace.

        Open this **inside** a worker-thread target rather than around the
        offload, and hand the trace back as a return value — see the module
        docstring. An exception escaping the block is recorded as
        ``outcome="error"`` and then re-raised unchanged.

        Entering the span materialises ``stage``, so a pass that runs but never
        reaches a stage-level mutator still persists a (possibly empty) marker
        for itself — see the module docstring.
        """
        trace = cls(
            origin=origin,
            user_id=user_id,
            channel_id=channel_id,
            actor_user_id=actor_user_id,
            thread_key=thread_key,
            message=message,
            stage=stage,
        )
        token = _CURRENT.set(trace)
        try:
            # Eager stage materialisation. Done here through ``begin_stage``
            # rather than by seeding ``self.stages`` in ``__init__`` for three
            # reasons: ``begin_stage`` already carries its own guard, so this
            # cannot raise out of ``__enter__`` and abort the routing pass (the
            # exact failure shape ``__init__``'s clamp/_sha256 guard exists to
            # prevent); it goes through the same get-or-create ``_stage_locked``
            # every mutator uses, so a later ``begin_stage``/``add_candidates``
            # for this name finds *this* stage instead of duplicating it; and it
            # keeps ``_current_stage`` and the materialised stage in sync by
            # construction rather than by two assignments that could drift.
            trace.begin_stage(stage)
            yield trace
        except BaseException as exc:  # noqa: BLE001 — record, then re-raise as-is
            trace.record_error(exc)
            raise
        finally:
            trace.finish()
            try:
                _CURRENT.reset(token)
            except Exception:  # noqa: BLE001 — reset across contexts is best-effort
                logger.debug("Routing trace context reset failed", exc_info=True)

    def finish(self) -> None:
        """Stamp elapsed time and settle a missing outcome. Never raises."""
        try:
            with self._lock:
                self.latency_ms = int((time.monotonic() - self._started) * 1000)
                if self.outcome is None:
                    self._settle_locked(OUTCOME_NO_MATCH)
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace finish failed", exc_info=True)

    # -- the single settler -------------------------------------------------

    def _settle_locked(self, outcome: str) -> None:
        """Write the terminal verdict. Caller must hold ``self._lock``.

        The **only** place ``outcome`` is assigned, so two invariants cannot be
        bypassed by a new writer.

        1. **A non-routed trace names no selection.** A stage may pick an agent
           that a later filter rejects; a ``no_match`` or ``error`` row still
           naming that agent is worse than no row at all — on the identity path
           the id in question is a placeholder resolving to nothing.

        2. **A trace carrying an ``error`` settles as ``error``.** Without this
           the settler happily overwrote a recorded failure with a softer
           verdict while leaving ``self.error`` populated, producing the one row
           shape the admin API cannot surface: ``outcome="no_match"`` with a
           non-NULL provider-outage ``error``, invisible to the ``?outcome=error``
           filter that exists to find exactly it. The live path is concrete:
           ``app_agent_router.route_to_agent`` catches an LLM-cascade failure,
           calls ``record_error`` and returns ``None``; the caller reads that
           ``None`` as "found nothing" and calls
           ``record_outcome(OUTCOME_NO_MATCH)``, which used to flip the verdict
           back. ``RoutingTraceService.persist`` performs the same promotion for
           an error carried in from an *earlier pass* — a separate trace object
           this settler never sees — so the rule is enforced at both ends and
           neither end depends on the other.

        The carve-out is deliberate and shared with ``persist``: a later stage
        that genuinely **routed** or **parked** overrides the error. A cascade
        that failed over to a working provider really did route, and the
        ``error`` field stays populated so the trace still shows what went wrong
        on the way.
        """
        if self.error and outcome not in (OUTCOME_ROUTED, OUTCOME_PARKED_INSTALL):
            outcome = OUTCOME_ERROR
        self.outcome = outcome
        if outcome not in (OUTCOME_ROUTED, OUTCOME_PARKED_INSTALL):
            self.selected_agent_id = None
            self.selected_bundle_uuid = None
            self.confidence = None

    # -- stage access -------------------------------------------------------

    def _stage_locked(self, stage: str | None) -> StageTrace:
        """Get-or-create a stage. Caller must hold ``self._lock``."""
        name = stage or self._current_stage or self.default_stage
        for existing in self.stages:
            if existing.stage == name:
                return existing
        created = StageTrace(stage=name)
        self.stages.append(created)
        return created

    def begin_stage(self, stage: str) -> None:
        """Make ``stage`` the target of subsequent un-addressed records.

        A **latch**, not a scope. Use :func:`stage_scope` for a nested stage
        that control returns from — see its docstring for what a bare
        ``begin_stage`` on a handoff costs.
        """
        try:
            with self._lock:
                self._current_stage = stage
                self._stage_locked(stage)
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace begin_stage failed", exc_info=True)

    def current_stage(self) -> str | None:
        """The stage un-addressed records currently land on. Never raises."""
        try:
            with self._lock:
                return self._current_stage
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace current_stage failed", exc_info=True)
            return None

    # -- mutators -----------------------------------------------------------

    def add_candidates(
        self, candidates: list[CandidateTrace], *, stage: str | None = None
    ) -> None:
        try:
            with self._lock:
                self._stage_locked(stage).candidates.extend(candidates)
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace add_candidates failed", exc_info=True)

    def add_llm_attempt(self, attempt: LLMAttempt, *, stage: str | None = None) -> None:
        try:
            with self._lock:
                self._stage_locked(stage).llm_attempts.append(attempt)
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace add_llm_attempt failed", exc_info=True)

    def update_stage(self, *, stage: str | None = None, **fields: Any) -> None:
        """Set non-``None`` ``StageTrace`` fields on the addressed stage."""
        try:
            with self._lock:
                target = self._stage_locked(stage)
                for key, value in fields.items():
                    if value is None or not hasattr(target, key):
                        continue
                    setattr(target, key, value)
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace update_stage failed", exc_info=True)

    def note_match_method(self, method: str) -> None:
        """Record *how* a stage matched, without settling the outcome.

        Read this as "how the last stage matched", not "how the decision was
        reached". It deliberately survives a later rejection: a trace reading
        ``outcome=no_match, match_method=ai, selected_agent_id=NULL`` says the
        classifier *did* pick something and a downstream filter threw it out,
        which is a different and more useful diagnosis than "nothing matched".
        """
        try:
            with self._lock:
                self.match_method = method
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace note_match_method failed", exc_info=True)

    def note_confidence(self, confidence: float | None) -> None:
        """Lift a stage's confidence to the decision, without settling it.

        Same shape and same reason as :meth:`note_match_method`: the classifier
        knows the score, but not whether the request finished — a later filter
        can still reject its pick. ``_settle_locked`` clears this on any
        non-positive outcome, exactly as it clears the selection, so a
        ``no_match`` row can never carry a confidence for an agent it does not
        name.
        """
        if confidence is None:
            return
        try:
            with self._lock:
                self.confidence = confidence
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace note_confidence failed", exc_info=True)

    def record_outcome(
        self,
        outcome: str,
        *,
        match_method: str | None = None,
        selected_agent_id: Any = None,
        selected_bundle_uuid: Any = None,
        confidence: float | None = None,
    ) -> None:
        """Settle the terminal verdict for the whole decision.

        Called by whoever knows the request finished. A non-routed outcome
        *clears* any selection: a stage may well have picked an agent that a
        later filter then rejected, and a ``no_match`` row still naming that
        agent is worse than no row at all — on the identity path the id in
        question is a placeholder that resolves to nothing.
        """
        try:
            with self._lock:
                if match_method is not None:
                    self.match_method = match_method
                if selected_agent_id is not None:
                    self.selected_agent_id = str(selected_agent_id)
                if selected_bundle_uuid is not None:
                    self.selected_bundle_uuid = str(selected_bundle_uuid)
                if confidence is not None:
                    self.confidence = confidence
                self._settle_locked(outcome)
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace record_outcome failed", exc_info=True)

    def mark_skipped(
        self, ref_id: str, reason: str, *, stage: str | None = None
    ) -> None:
        """Flip an already-recorded candidate to excluded."""
        try:
            with self._lock:
                for stage_trace in self.stages:
                    if stage is not None and stage_trace.stage != stage:
                        continue
                    for candidate in stage_trace.candidates:
                        if candidate.ref_id == ref_id:
                            candidate.eligible = False
                            candidate.skip_reason = reason
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace mark_skipped failed", exc_info=True)

    def record_error(self, exc: BaseException | str) -> None:
        """Settle the trace as failed. Goes through the same settler as the
        rest, so a selection recorded before the failure is cleared too.

        The description is **de-tainted** (:func:`describe_exception`): this used
        to store ``f"{type(exc).__name__}: {exc}"``, and the ``{exc}`` half is
        the sender's message whenever the exception is a provider error echoing
        the request payload — which is exactly the failure this field exists to
        record. Kept ungated on purpose: ``?outcome=error`` is the filter an
        operator uses to find an outage, and it must not stop working because
        someone turned off a *text* flag.
        """
        try:
            message = describe_exception(exc)
            with self._lock:
                self.error = message
                self._settle_locked(OUTCOME_ERROR)
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace record_error failed", exc_info=True)

    # -- projection ---------------------------------------------------------

    def stages_payload(self) -> list[dict[str, Any]]:
        """JSON-ready view of the stages. Never raises; ``[]`` on failure."""
        try:
            with self._lock:
                return [asdict(stage) for stage in self.stages]
        except Exception:  # noqa: BLE001
            logger.debug("Routing trace stages_payload failed", exc_info=True)
            return []


# --- Instrumentation API ----------------------------------------------------
#
# One guarded function per instrumentation point. Each reads the active capture
# and returns immediately when there is none, so an un-instrumented caller pays
# a ContextVar read. Attribute extraction happens *inside* the guard: the call
# site must never have to build an expression that could raise.


def current() -> RoutingTrace | None:
    """The active capture, or ``None``."""
    return RoutingTrace.current()


def begin_stage(stage: str) -> None:
    trace = RoutingTrace.current()
    if trace is None:
        return
    trace.begin_stage(stage)


@contextmanager
def stage_scope(stage: str) -> Iterator[None]:
    """Attribute records to ``stage`` **for the duration of a block only**.

    ``begin_stage`` latches: everything recorded afterwards with no explicit
    ``stage=`` targets the new name until something else latches over it. That
    is right for a linear pipeline advancing through passes, and wrong for a
    *handoff that returns* — which is what Stage 2 identity routing is.
    ``AppMCPRoutingService.route_message`` latched ``identity_stage2`` before
    calling into ``IdentityRoutingService`` and never restored, so every
    un-addressed record made after control came back — including
    ``ChannelRoutingService._route_installed``'s Pass-1
    ``SKIP_IDENTITY_ROUTE`` candidate, which is a *Pass 1* rejection — landed
    on ``identity_stage2``. Confirmed by execution, not by reading. Phase 4
    groups candidates by stage, so that row would have rendered under the wrong
    heading with nothing in the payload to reveal it.

    No-ops without an active capture, and never raises: the restore runs in a
    ``finally`` and ``begin_stage`` carries its own guard.
    """
    trace = RoutingTrace.current()
    if trace is None:
        yield
        return
    previous = trace.current_stage()
    trace.begin_stage(stage)
    try:
        yield
    finally:
        if previous:
            trace.begin_stage(previous)


def record_effective_routes(
    routes: Any, *, skip_reason: str | None = None, stage: str | None = None
) -> None:
    """Candidate set from ``AppAgentRouteService.get_effective_routes_for_user``.

    ``skip_reason`` records the batch as *excluded* instead — the shape a filter
    site needs when it drops routes for a reason (an inactive route) that would
    otherwise vanish silently. Attribute reads still happen inside this guard,
    so the caller never has to build an expression that could raise.
    """
    trace = RoutingTrace.current()
    if trace is None:
        return
    try:
        candidates = [
            CandidateTrace(
                kind=KIND_AGENT,
                ref_id=str(getattr(route, "agent_id", "") or ""),
                name=str(getattr(route, "agent_name", "") or ""),
                owner_email=getattr(route, "identity_owner_email", None),
                source=str(getattr(route, "source", "") or ""),
                trigger_prompt=clamp(getattr(route, "trigger_prompt", None)) or "",
                prompt_examples=clamp(getattr(route, "prompt_examples", None)),
                eligible=skip_reason is None,
                skip_reason=skip_reason,
            )
            for route in (routes or [])
        ]
    except Exception:  # noqa: BLE001
        logger.debug("Routing trace route capture failed", exc_info=True)
        return
    trace.add_candidates(candidates, stage=stage)


def record_candidate(
    *,
    kind: str,
    ref_id: Any,
    name: str,
    source: str = "",
    trigger_prompt: str | None = None,
    prompt_examples: str | None = None,
    owner_email: str | None = None,
    eligible: bool = True,
    skip_reason: str | None = None,
    stage: str | None = None,
) -> None:
    """Record one candidate — eligible, or excluded with a ``skip_reason``."""
    trace = RoutingTrace.current()
    if trace is None:
        return
    try:
        candidate = CandidateTrace(
            kind=kind,
            ref_id=str(ref_id) if ref_id is not None else "",
            name=name or "",
            owner_email=owner_email,
            source=source,
            trigger_prompt=clamp(trigger_prompt) or "",
            prompt_examples=clamp(prompt_examples),
            eligible=eligible,
            skip_reason=skip_reason,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Routing trace candidate capture failed", exc_info=True)
        return
    trace.add_candidates([candidate], stage=stage)


def record_skip(
    *,
    kind: str,
    ref_id: Any,
    name: str,
    reason: str,
    source: str = "",
    trigger_prompt: str | None = None,
    prompt_examples: str | None = None,
    owner_email: str | None = None,
    stage: str | None = None,
) -> None:
    """Shorthand for an *excluded* candidate. The diagnosis lives here.

    ``prompt_examples`` is accepted for the same reason ``trigger_prompt`` is:
    the near-miss ranking scores excluded candidates too, and it now scores on
    both fields (because the classifier now *sees* both). A skip recorded
    without them would rank below where it belongs and misreport how close the
    expected agent came.
    """
    record_candidate(
        kind=kind,
        ref_id=ref_id,
        name=name,
        source=source,
        trigger_prompt=trigger_prompt,
        prompt_examples=prompt_examples,
        owner_email=owner_email,
        eligible=False,
        skip_reason=reason,
        stage=stage,
    )


def mark_candidate_skipped(
    *, ref_id: Any, reason: str, stage: str | None = None
) -> None:
    """Flip an already-recorded candidate to excluded.

    Used where a candidate is captured up front (the effective-route set) and
    rejected further down the pipeline, so the trace shows one row per agent
    rather than a duplicate.
    """
    trace = RoutingTrace.current()
    if trace is None:
        return
    try:
        wanted = str(ref_id) if ref_id is not None else ""
    except Exception:  # noqa: BLE001
        logger.debug("Routing trace skip marking failed", exc_info=True)
        return
    trace.mark_skipped(wanted, reason, stage=stage)


def record_match(
    *,
    method: str,
    matched_pattern: str | None = None,
    stage: str | None = None,
) -> None:
    """How this stage matched — and, for a pattern hit, on which pattern.

    Deliberately **stage-level only**. A stage matching is not the request
    finishing: the classifier's pick still has to survive the identity handoff
    and the channel pipeline's ownership filter, either of which can reject it.
    Only the consumer that knows the request ended calls :func:`record_outcome`,
    so a trace can never claim ``routed`` for a call that returned nothing.
    """
    trace = RoutingTrace.current()
    if trace is None:
        return
    try:
        pattern = clamp(matched_pattern, 200)
    except Exception:  # noqa: BLE001
        logger.debug("Routing trace match capture failed", exc_info=True)
        return
    trace.update_stage(stage=stage, match_method=method, matched_pattern=pattern)
    trace.note_match_method(method)


def record_prompt(prompt: str | None, *, stage: str | None = None) -> None:
    """The rendered classifier prompt."""
    trace = RoutingTrace.current()
    if trace is None:
        return
    try:
        clamped = clamp(prompt)
    except Exception:  # noqa: BLE001
        logger.debug("Routing trace prompt capture failed", exc_info=True)
        return
    trace.update_stage(stage=stage, prompt=clamped)


def record_raw_response(raw: str | None, *, stage: str | None = None) -> None:
    """The classifier's raw reply, before parsing."""
    trace = RoutingTrace.current()
    if trace is None:
        return
    try:
        clamped = clamp(raw)
    except Exception:  # noqa: BLE001
        logger.debug("Routing trace raw response capture failed", exc_info=True)
        return
    trace.update_stage(stage=stage, raw_response=clamped)


def record_parse_outcome(
    *,
    reason: str | None = None,
    confidence: float | None = None,
    runner_up_id: str | None = None,
    stage: str | None = None,
) -> None:
    """What the parse made of the raw response."""
    trace = RoutingTrace.current()
    if trace is None:
        return
    try:
        clamped_reason = clamp(reason, 400)
    except Exception:  # noqa: BLE001
        logger.debug("Routing trace parse outcome capture failed", exc_info=True)
        return
    trace.update_stage(
        stage=stage,
        reason=clamped_reason,
        confidence=confidence,
        runner_up_id=runner_up_id,
    )


def record_confidence(confidence: float | None) -> None:
    """Lift the classifier's confidence to the decision level. Never settles."""
    trace = RoutingTrace.current()
    if trace is None:
        return
    trace.note_confidence(confidence)


def record_llm_attempt(
    *,
    provider: str,
    model: str | None,
    ok: bool,
    error: str | None = None,
    latency_ms: int = 0,
    stage: str | None = None,
) -> None:
    """One provider the cascade tried. Called for successes *and* failures."""
    trace = RoutingTrace.current()
    if trace is None:
        return
    try:
        attempt = LLMAttempt(
            provider=str(provider),
            model=_str_or_none(model),
            ok=bool(ok),
            error=clamp(error, 400),
            latency_ms=int(latency_ms),
        )
    except Exception:  # noqa: BLE001
        logger.debug("Routing trace attempt capture failed", exc_info=True)
        return
    trace.add_llm_attempt(attempt, stage=stage)


def record_outcome(
    outcome: str,
    *,
    match_method: str | None = None,
    selected_agent_id: Any = None,
    selected_bundle_uuid: Any = None,
    confidence: float | None = None,
) -> None:
    trace = RoutingTrace.current()
    if trace is None:
        return
    trace.record_outcome(
        outcome,
        match_method=match_method,
        selected_agent_id=selected_agent_id,
        selected_bundle_uuid=selected_bundle_uuid,
        confidence=confidence,
    )


def record_error(exc: BaseException | str) -> None:
    trace = RoutingTrace.current()
    if trace is None:
        return
    trace.record_error(exc)


# --- Consumer-facing projections -------------------------------------------


# NOTE — ``trace_ids(**traces)`` used to live here and has been **deleted**.
# It had zero callers, and its docstring claimed the job
# ``ChannelInboundService._decision_detail`` actually does. The two were not
# equivalent, which is why it was a hazard rather than merely dead:
# ``_decision_detail`` emits a ``trace_id`` only when ``persist`` actually
# wrote a row ("a dead link in a diagnostic panel is worse than no link"),
# while ``trace_ids`` emitted any in-memory trace's id unconditionally —
# advertising a link that ``GET /admin/routing/traces/{id}`` 404s on whenever
# tracing is off or a persist was swallowed. Exported, it was an invitation to
# reintroduce that bug. Build the detail dict with ``_decision_detail``.


def _summarize_one(trace: RoutingTrace) -> str:
    parts: list[str] = []
    for stage in trace.stages:
        segment = f"{stage.stage}: {len(stage.candidates)} candidate(s)"
        skipped = [c for c in stage.candidates if not c.eligible]
        if skipped:
            reasons = sorted({c.skip_reason or "skipped" for c in skipped})
            segment += f", {len(skipped)} skipped ({', '.join(reasons)})"
        if stage.match_method:
            segment += f", method={stage.match_method}"
        if stage.llm_attempts:
            attempts = ", ".join(
                f"{a.provider}/{a.model or '?'}" + ("" if a.ok else " failed")
                for a in stage.llm_attempts
            )
            segment += f", llm=[{attempts}]"
        if stage.reason:
            segment += f", {stage.reason}"
        parts.append(segment)
    if trace.error:
        parts.append(f"error: {trace.error}")
    return "; ".join(parts)


def summarize(*traces: RoutingTrace | None) -> str:
    """One-line diagnosis across traces, for the live debug feed.

    Never raises and never returns ``None`` — an empty string means "nothing
    worth adding", which callers append conditionally.
    """
    try:
        segments = [
            _summarize_one(trace) for trace in traces if trace is not None
        ]
        line = " | ".join(segment for segment in segments if segment)
        return clamp(line, SUMMARY_MAX_CHARS) or ""
    except Exception:  # noqa: BLE001
        logger.debug("Routing trace summary failed", exc_info=True)
        return ""


__all__ = [
    "CandidateTrace",
    "LLMAttempt",
    "RoutingTrace",
    "StageTrace",
    "SAFE_CANDIDATE_FIELDS",
    "SAFE_LLM_ATTEMPT_FIELDS",
    "SAFE_STAGE_FIELDS",
    "TRACE_TEXT_MAX_CHARS",
    "ORIGIN_APP_MCP",
    "ORIGIN_IDENTITY",
    "ORIGIN_SERVER_CHANNEL",
    "ORIGIN_SIMULATE",
    "STAGE_IDENTITY_STAGE2",
    "STAGE_PASS_1",
    "STAGE_PASS_2",
    "OUTCOME_ERROR",
    "OUTCOME_NO_MATCH",
    "OUTCOME_PARKED_INSTALL",
    "OUTCOME_ROUTED",
    "MATCH_AI",
    "MATCH_ONLY_ONE",
    "MATCH_PATTERN",
    "SKIP_AGENT_MISSING",
    "SKIP_ALREADY_INSTALLED",
    "SKIP_FOREIGN_OWNER",
    "SKIP_IDENTITY_ROUTE",
    "SKIP_NOT_INSTALLABLE",
    "SKIP_NO_REVISION",
    "SKIP_NO_TRIGGER_PROMPT",
    "SKIP_ROUTE_INACTIVE",
    "KIND_AGENT",
    "KIND_BUNDLE",
    "begin_stage",
    "clamp",
    "current",
    "describe_exception",
    "mark_candidate_skipped",
    "record_candidate",
    "record_effective_routes",
    "record_error",
    "record_llm_attempt",
    "record_match",
    "record_outcome",
    "record_parse_outcome",
    "record_prompt",
    "record_raw_response",
    "record_skip",
    "stage_scope",
    "summarize",
]
