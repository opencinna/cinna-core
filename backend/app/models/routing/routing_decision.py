"""RoutingDecision — the durable record of one routing decision.

``RoutingTrace`` (``app/services/routing/routing_trace.py``) captures a decision
in process memory; this table is where a closed trace lands. The in-memory
channel debug buffer answers "what just happened", for minutes, in one worker.
Tuning needs the other question — "why did it pick that, last Tuesday, on the
worker that has since restarted" — and only a row can answer it.

Two shape decisions worth knowing before editing this file:

- **``stages`` is JSONB, not child tables.** It is read whole, never queried by
  an inner field, and its shape follows the router's — which will keep moving
  (the two-pass channel structure is not the final one). Child tables would
  fossilise today's layout. Same call as ``InputTask.refinement_history`` and
  ``AgentEnvironment.config``.
- **``message_text`` is gated and ``stages`` is projected; ``message_sha256`` is
  neither.** Server Channels otherwise keeps inbound text out of the database;
  storing it here is a deliberate, documented exception (see the field comment
  and the plan's §7). With ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` off,
  ``message_text`` is withheld and every stage is projected through
  ``routing_trace.SAFE_STAGE_FIELDS`` — an **allowlist**: a stage field is stored
  and served only if it has been declared free of the sender's words, so a field
  added later defaults to hidden rather than to exposed. Write path and read path
  both, from that one definition. With the gate off the hash still supports
  replay and dedupe, and the candidate set plus the verdict still answer the
  question that matters most — which agents were even considered.

Rows are disposable. ``routing_trace_scheduler`` purges past
``ROUTING_TRACE_RETENTION_DAYS``; nothing references a decision row, and losing
one loses a diagnostic, never state.

See ``docs/plans/auto_routing_tuning_plan.md`` §4.
"""
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

# Free-text columns are clamped by the recorder before they get here; these are
# the *storage* bounds, sized so a clamp change cannot start truncating at the
# database layer instead of visibly at the recorder.
MAX_THREAD_KEY_CHARS = 512
MAX_ORIGIN_CHARS = 32
MAX_OUTCOME_CHARS = 32
MAX_MATCH_METHOD_CHARS = 32
#: A hex SHA-256 is exactly 64 characters, so this column has zero headroom.
#: Bounded on the way in rather than assigned verbatim — the value is
#: recorder-computed today, but replay/dedupe may accept a caller-supplied hash.
#: Via ``_fit_exact``, NOT ``_fit``: prefix-truncating a hash yields a value that
#: matches nothing in ``ix_routing_decision_message_sha256``, so a lookup would
#: silently return "no such message" instead of failing. Over-long input is
#: dropped instead.
MAX_MESSAGE_SHA256_CHARS = 64

# NOTE — ``GATED_STAGE_TEXT_FIELDS`` used to live here: a denylist of the
# ``stages[]`` fields ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` blanked. It has been
# **inverted** into ``routing_trace.SAFE_STAGE_FIELDS``, an allowlist, and moved
# next to the dataclasses it projects so that adding a field puts the allowlist
# in the diff's eyeline. A denylist made a newly added field default to
# *exposed*; three separate rounds of enumerating the tainted fields each shipped
# one field short. See the comment above ``SAFE_STAGE_FIELDS``.

#: Rendered by the admin UI wherever gated text would have gone while
#: ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` is off. Server-authored so Phase 4 shows
#: this rather than inventing copy that overstates what the flag did — the whole
#: point is that it **hides and does not erase**, and the reader has to be told
#: the two things that actually remove the text.
MESSAGE_TEXT_HIDDEN_NOTICE = (
    "Message text is hidden: ROUTING_TRACE_STORE_MESSAGE_TEXT is off. While it "
    "is off, message_text is withheld and each stage is projected through an "
    "allowlist — only fields explicitly declared free of the sender's words are "
    "served, and anything else is withheld by default, including fields added "
    "to the trace after this gate was written. This hides, it does not erase: "
    "anything captured before the flag was turned off is still stored, and "
    "stays stored until its retention window (ROUTING_TRACE_RETENTION_DAYS) "
    "expires it, or until an admin clears the traces "
    "(DELETE /api/v1/admin/routing/traces). The message hash, the candidate "
    "list and the verdict are unaffected."
)

#: Rendered wherever traces would have been listed or shown while
#: ``ROUTING_TRACE_ENABLED`` is off. Without it, an operator who turned that flag
#: off would see an empty table indistinguishable from "this server has made no
#: routing decisions" — §11a Rule 1. It is the same hides-does-not-erase shape as
#: the message-text notice and names the same two erasure paths, plus the one
#: thing that differs: clearing keeps working while this gate is closed, on
#: purpose, so the pile can still be emptied.
TRACING_DISABLED_NOTICE = (
    "Routing traces are not being served: ROUTING_TRACE_ENABLED is off. That "
    "flag gates reads as well as writes, so no stored decision is returned while "
    "it is off, whatever is still in the table. It hides, it does not erase: "
    "rows written before it was turned off stay stored until their retention "
    "window (ROUTING_TRACE_RETENTION_DAYS) expires them, or until an admin "
    "clears them (DELETE /api/v1/admin/routing/traces) — which keeps working "
    "while this gate is closed, precisely so the stored rows can still be "
    "removed."
)


class RoutingDecision(SQLModel, table=True):
    """One persisted routing decision, with its full stage trace."""

    __tablename__ = "routing_decision"
    __table_args__ = (
        # The unfiltered admin list ("recent decisions, newest first").
        Index("ix_routing_decision_created", text("created_at DESC")),
        # Filtered by channel — the common case on the tuning card.
        Index("ix_routing_decision_channel_created", "channel_id", text("created_at DESC")),
        # "Everything we routed for this sender."
        Index("ix_routing_decision_user_created", "user_id", text("created_at DESC")),
        # Replay/dedupe lookup by message identity, text gate or not.
        Index("ix_routing_decision_message_sha256", "message_sha256"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # TIMESTAMPTZ, not the naive TIMESTAMP some older tables use: the retention
    # purge compares this against a tz-aware ``datetime.now(UTC)`` and must not
    # depend on the database session's timezone setting.
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    # ``server_channel`` | ``app_mcp`` | ``identity`` | ``simulate``. Plain
    # VARCHAR, matching the feature's status-string convention — a new origin
    # then needs no migration, and readers must tolerate unknown values.
    origin: str = Field(sa_column=Column(String(MAX_ORIGIN_CHARS), nullable=False))

    # A deleted channel takes its traces with it: they are channel diagnostics
    # and mean nothing without it.
    channel_id: uuid.UUID | None = Field(
        default=None, foreign_key="server_channel.id", ondelete="CASCADE"
    )
    # The *sender* being routed for. SET NULL rather than CASCADE: the trace
    # still explains a routing rule after the account is gone, and the row
    # holds no content beyond what the gate already allows.
    user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    # The admin who ran a simulate/replay. NULL on the real path.
    actor_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )

    thread_key: str | None = Field(
        default=None, sa_column=Column(String(MAX_THREAD_KEY_CHARS), nullable=True)
    )

    # The inbound message, clamped to ``ROUTING_TRACE_TEXT_MAX_CHARS`` and
    # written only when ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` is on. Superuser
    # read only, and purged with the row. Not the only thing that flag governs:
    # with it off, ``stages`` is projected through the
    # ``routing_trace.SAFE_STAGE_FIELDS`` allowlist as well, since stage fields
    # carry the sender's words too.
    message_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    # Written whatever the text gate says — it is what makes a trace replayable
    # and dedupable with the text withheld. Nullable only because a trace can
    # legitimately have no message at all (an empty simulate); in practice
    # every real decision carries one.
    message_sha256: str | None = Field(
        default=None,
        sa_column=Column(String(MAX_MESSAGE_SHA256_CHARS), nullable=True),
    )

    # ``routed`` | ``no_match`` | ``error`` | ``parked_install``.
    outcome: str = Field(sa_column=Column(String(MAX_OUTCOME_CHARS), nullable=False))

    # ``pattern`` | ``ai`` | ``only_one``.
    #
    # **"How the last stage matched", not "how the decision was reached."** It
    # deliberately survives a ``no_match``: a row reading
    # ``outcome=no_match, match_method=ai, selected_agent_id=NULL`` says the
    # classifier *did* pick something and a downstream filter (the ownership
    # check, the identity handoff) threw it out. That is a different and far
    # more useful diagnosis than "nothing matched", and it is the signal the
    # motivating bug is read from. Do not "fix" it by clearing it on a
    # non-routed outcome — the residue is the point.
    match_method: str | None = Field(
        default=None, sa_column=Column(String(MAX_MATCH_METHOD_CHARS), nullable=True)
    )

    # What the decision landed on, when it landed on anything. Both SET NULL:
    # deleting the agent or bundle must not delete the evidence about it.
    selected_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL"
    )
    selected_bundle_uuid: uuid.UUID | None = Field(
        default=None, foreign_key="agent_bundle.id", ondelete="SET NULL"
    )

    # Recorded, never acted on: gating routing on confidence is a later,
    # data-backed decision (plan §8).
    confidence: float | None = Field(default=None, sa_column=Column(Float, nullable=True))

    latency_ms: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )

    # ``[StageTrace]`` as produced by ``RoutingTrace.stages_payload()``,
    # projected through ``routing_trace.SAFE_STAGE_FIELDS`` when
    # ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` is off:
    # candidates (including rejected ones with a ``skip_reason``), the rendered
    # classifier prompt, the raw LLM response, and one entry per provider tried.
    #
    # Plain JSONB assignment only. ``row.stages.append(...)`` is NOT
    # dirty-tracked and the commit silently drops it — assign a new list, the
    # convention used across this codebase.
    stages: list = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )

    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


# ── API schemas (Pydantic, no table=True) ────────────────────────────


class RoutingDecisionSummary(SQLModel):
    """List-row projection — everything the table needs, without ``stages``.

    ``stages`` is the large field and the only one a list view never reads;
    keeping it out means a page of 50 rows does not haul 50 rendered prompts
    and raw LLM responses across the wire.
    """

    id: uuid.UUID
    created_at: datetime
    origin: str
    channel_id: uuid.UUID | None = None
    channel_name: str | None = None
    user_id: uuid.UUID | None = None
    user_email: str | None = None
    actor_user_id: uuid.UUID | None = None
    thread_key: str | None = None
    # Withheld — NOT merely absent — while ``ROUTING_TRACE_STORE_MESSAGE_TEXT``
    # is off; see ``message_text_hidden`` below.
    message_text: str | None = None
    # Returned whatever the text gate says. It is what keeps a gated trace
    # replayable and dedupable, and it is how a reader tells "text withheld"
    # (hash present, text NULL) from "there was no message" (both NULL).
    message_sha256: str | None = None
    # True while the read gate is closed, for every row alike — it describes the
    # server's current setting, not this row's contents. Paired with
    # ``message_text_notice`` so the UI renders the server's wording.
    message_text_hidden: bool = False
    message_text_notice: str | None = None
    outcome: str
    # See the column docs: this is how the last stage matched, and it survives
    # a ``no_match`` on purpose.
    match_method: str | None = None
    selected_agent_id: uuid.UUID | None = None
    selected_agent_name: str | None = None
    selected_bundle_uuid: uuid.UUID | None = None
    selected_bundle_name: str | None = None
    confidence: float | None = None
    latency_ms: int = 0
    error: str | None = None
    # Cheap list-projection counters, so the table can show "4 candidates, 3
    # skipped" without the client deserializing ``stages``.
    candidate_count: int = 0
    skipped_count: int = 0
    # Whichever provider/model actually answered, for the "is my local LLM
    # broken" glance. NULL when no provider was reached.
    provider: str | None = None
    model: str | None = None


class RoutingNearMiss(SQLModel):
    """One candidate ranked by token overlap against the routed message.

    A *hint*, explicitly not a rule: the classifier is an LLM and there is no
    similarity cut-off anywhere in routing. This is the same Jaccard overlap
    ``app/services/routing/text_similarity.py`` already uses for install-time
    route-conflict detection, borrowed to answer the question an admin
    actually asks about a ``no_match`` — "how close did it come?". Saying
    "0.31, below the threshold" would be a claim the router does not
    implement; the wording says "closest", not "just missed".
    """

    ref_id: str
    kind: str
    name: str
    #: Jaccard overlap of the message's tokens with the candidate's
    #: ``trigger_prompt``. Rounded to two places — this is read by eye, and
    #: three more digits of a heuristic imply a precision it does not have.
    similarity: float
    #: Carried so a ranking can be read against the candidate table: the top
    #: near-miss is a very different finding when it was excluded before the
    #: classifier ever saw it.
    eligible: bool = True
    skip_reason: str | None = None


class RoutingDiagnosisPublic(SQLModel):
    """Why this decision went the way it did, in a sentence, plus near-misses.

    **Computed on the backend on purpose** (plan §10, Phase 4): the wording is
    the feature for the motivating case, so it has to be testable and it has to
    live next to the rules it describes. In a component it could be neither —
    nothing would fail when the rule it paraphrases changed.

    ``verdict`` is the sentence; ``code`` is the branch that produced it, so a
    client can style or group without parsing prose and a test can pin both
    independently. Every branch names a remedy: a diagnosis that says only what
    is wrong leaves the reader exactly where they started.

    **What this exposes about the expected agent is an allowlist of two fields**
    — ``expected_agent_name`` and ``expected_agent_owner_email`` — chosen by the
    same standard §7 applies to ``candidates[].trigger_prompt``: they are the
    agent owner's own configuration, not sender-derived, and already visible to
    a superuser. Nothing else about the agent is read into the response, and a
    field wanted here later has to clear that bar when it is added.
    """

    #: Machine-readable branch id. See ``routing_reachability_service`` for the
    #: full vocabulary; readers must tolerate an unknown value, like every other
    #: string vocabulary in this feature.
    code: str
    #: The plain-language sentence, remedy included. Server-authored.
    verdict: str
    #: The remedy clause on its own, for a client that wants to render it as an
    #: action. ``verdict`` is *composed from* this, so the two cannot drift.
    action: str
    #: Eligible candidates the classifier could actually have chosen — "this
    #: user has N effective routes".
    eligible_candidate_count: int = 0
    #: Candidates that were considered and dropped, by ``skip_reason``. The
    #: single highest-value field on the whole trace (plan §4) rolled up.
    skipped_by_reason: dict[str, int] = Field(default_factory=dict)
    #: Echoed back so a client can tell a general verdict from one about a
    #: specific agent without tracking its own request.
    expected_agent_id: uuid.UUID | None = None
    expected_agent_name: str | None = None
    expected_agent_owner_email: str | None = None
    #: Ranked best-first. Empty when there is nothing to rank *or* when the
    #: message text is not available — ``near_miss_notice`` says which.
    near_misses: list[RoutingNearMiss] = Field(default_factory=list)
    near_miss_notice: str | None = None


class RoutingDecisionPublic(RoutingDecisionSummary):
    """Full detail — the summary plus the stage trace."""

    stages: list[Any] = Field(default_factory=list)
    #: Attached by ``RoutingTraceService.get`` rather than by the route, so a
    #: simulate response carries it for the same reason it carries everything
    #: else: it is the *same function*, not a matching projection. ``None`` only
    #: when the diagnosis itself failed — a broken diagnostic must not take the
    #: trace down with it.
    diagnosis: RoutingDiagnosisPublic | None = None


class RoutingDecisionsPublic(SQLModel):
    """Paginated list envelope."""

    data: list[RoutingDecisionSummary]
    count: int
    # Set to ``TRACING_DISABLED_NOTICE`` while ``ROUTING_TRACE_ENABLED`` is off,
    # in which case ``data`` is empty and ``count`` is 0 whatever the table
    # holds. Without it the UI would render the gate's effect as "no routing
    # decisions have been made" — the empty state lying about the server's
    # configuration rather than reporting it.
    notice: str | None = None


# ── Simulate / replay / recommendation (plan §6) ─────────────────────
#
# The **response** side deliberately reuses ``RoutingDecisionPublic`` rather
# than defining a simulate-shaped twin. A simulate run must expose exactly what
# a stored trace exposes and nothing more, and the only way to say that in a way
# that cannot drift is for both to be the same type built by the same function
# (``RoutingTraceService.get`` — see ``RoutingTuningService.simulate``). A
# parallel model would start identical, diverge on the next field somebody adds
# to one of them, and the divergence would be silent.


#: Ceiling on a hand-typed simulate message. Comfortably above any real chat
#: message and above ``ROUTING_TRACE_TEXT_MAX_CHARS`` (2000 by default), so the
#: cap never truncates something the recorder would have stored in full — it
#: only stops an admin pasting a novel into a provider request.
MAX_SIMULATE_MESSAGE_CHARS = 8_000


class RoutingSimulateRequest(SQLModel):
    """Run one message through routing for another user, with no effects."""

    #: The message to route. Capped here, not only clamped by the recorder:
    #: the clamp bounds what gets *stored*, while the whole body is what goes
    #: to the provider. Superuser-only and rate-limited, so an uncapped field
    #: was a cost-and-latency hole rather than a vector — but the ceiling is
    #: free and the recorder's clamp discards anything past it anyway.
    #:
    #: An empty message is rejected in the route, because "no candidates matched
    #: an empty string" is not a diagnosis anybody asked for and it still costs
    #: an LLM call.
    message: str = Field(max_length=MAX_SIMULATE_MESSAGE_CHARS)
    #: Whose routing state to decide against — the sender being diagnosed, not
    #: the admin. This is the parameter that makes the route sensitive.
    as_user_id: uuid.UUID
    #: Include the Pass-2 auto-install catalog. Off answers the narrower
    #: question "would this have matched something they already have?", which
    #: is usually the one being asked when an install went somewhere odd.
    include_catalog: bool = True


class RoutingReplayRequest(SQLModel):
    """Re-run a stored trace's message against current state."""

    include_catalog: bool = True


class RoutingRecommendationRequest(SQLModel):
    """Ask for a drafted trigger prompt for one candidate from a trace."""

    #: Which candidate to draft for, by the ``ref_id`` it carries in the
    #: trace's ``stages[].candidates[]``. Optional: with one obvious subject
    #: (the trace selected something, or considered exactly one candidate) the
    #: server picks it. Restricted to candidates *of this trace* on purpose —
    #: otherwise the route would be a general "describe any agent" oracle
    #: hanging off a diagnostics endpoint.
    ref_id: str | None = None


class RoutingReplayDiff(SQLModel):
    """What changed between a stored decision and its re-run.

    Field-by-field rather than a text blob: the card renders the changed rows,
    and a test can assert on one property without parsing prose. ``summary`` is
    server-authored so the wording lives with the rules it describes (same call
    as the reachability verdict in plan §9).
    """

    changed: bool = False
    outcome_changed: bool = False
    original_outcome: str | None = None
    replay_outcome: str | None = None
    selection_changed: bool = False
    #: ``"agent:<name>"`` / ``"bundle:<name>"``, or ``None`` for no selection.
    original_selection: str | None = None
    replay_selection: str | None = None
    match_method_changed: bool = False
    original_match_method: str | None = None
    replay_match_method: str | None = None
    original_confidence: float | None = None
    replay_confidence: float | None = None
    original_candidate_count: int = 0
    replay_candidate_count: int = 0
    #: Candidate display names present on one side only. The interesting half of
    #: a replay after a route change: an agent that was never a candidate before
    #: and now is (or the reverse) explains the verdict change on its own.
    candidates_added: list[str] = Field(default_factory=list)
    candidates_removed: list[str] = Field(default_factory=list)
    summary: str = ""


class RoutingReplayResult(SQLModel):
    """The original decision, the re-run, and the diff between them."""

    original: RoutingDecisionPublic
    replay: RoutingDecisionPublic
    diff: RoutingReplayDiff


#: Rendered above every drafted recommendation. The admin surface is read-only
#: with respect to agents — it never edits another user's agent, trigger prompt
#: or bundle — and the draft is the one output that could be mistaken for a
#: change having been made. Server-authored so the boundary is stated by the
#: thing that enforces it rather than by UI copy that can drift away from it.
RECOMMENDATION_ADVISORY_NOTICE = (
    "Draft only — nothing has been changed. This endpoint never edits an "
    "agent, a trigger prompt or a bundle, including ones you own. Send this "
    "wording to the agent's owner, who can apply it themselves; for a bundle, "
    "the owner applies it and republishes."
)


class RoutingRecommendationPublic(SQLModel):
    """A copyable trigger-prompt draft for one candidate. Writes nothing."""

    trace_id: uuid.UUID
    ref_id: str
    kind: str
    name: str
    owner_email: str | None = None
    current_trigger_prompt: str | None = None
    suggested_trigger_prompt: str | None = None
    success: bool = False
    #: Populated when the generator failed. Surfaced rather than raised: a
    #: provider outage on an advisory draft is a diagnosis in its own right on a
    #: card whose other job is answering "is my local LLM broken".
    error: str | None = None
    notice: str = RECOMMENDATION_ADVISORY_NOTICE
