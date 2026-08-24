"""Persistence and admin reads for routing traces.

``routing_trace.py`` captures a decision in process memory and knows nothing
about databases; this module is the seam where a *closed* trace becomes a
``routing_decision`` row, and where the admin API reads them back.

**Import direction matters here.** ``app/agents/`` imports ``routing_trace``
directly, and ``app/agents/`` sits below ``app/services/``. That inversion is
harmless only because ``routing_trace`` pulls in nothing but the standard
library. This module pulls in models, a session and settings — so it must never
be re-exported from ``app/services/routing/__init__.py``, and ``routing_trace``
must never import it. Import it by its full module path from the services and
routes that need it. The rule is written at length in that ``__init__``.

Two gates decide whether a trace is written and what it may carry. **Both gate
the read path as well as the write path** — the asymmetry §7 rejects for one of
them is wrong for the other for the same reason:

- ``ROUTING_TRACE_ENABLED`` — gates **persistence, not in-process capture**
  (the wording ``config.py`` uses; the recorder cannot read settings at all, and
  the live channel debug feed's no-match diagnosis is built from a capture, so
  this flag must not touch it). Off means no row is written *and* none is
  served: :meth:`list` returns an empty page and :meth:`get` returns ``None``,
  each accompanied by :data:`TRACING_DISABLED_NOTICE`. :meth:`clear` keeps
  working while it is off — precisely so the pile an operator just stopped
  serving can still be emptied.
- ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` — the policy flag from the plan's §7.
  With it off, ``message_text`` is withheld and each stage is projected through
  ``routing_trace.SAFE_STAGE_FIELDS``: an **allowlist**, so a stage field is
  stored and served only if somebody declared it free of the sender's words, and
  a field added later defaults to hidden. This replaced a per-field denylist,
  which had the defect its shape guarantees — three rounds of enumerating the
  tainted fields each shipped one field short (``message_text``, then
  ``stages[].prompt`` / ``raw_response``, then ``llm_attempts[].error``, whose
  provider exceptions echo the request payload). Applied on the write path as
  well as the read path, from that one definition, so the property is structural
  rather than an audit's high-water mark: ``stages[].prompt`` happened not to
  contain the message only because the router prompt template overruns
  ``TRACE_TEXT_MAX_CHARS`` before the user message is appended, and a privacy
  property must not rest on the byte length of a markdown file. With the gate
  off, ``message_sha256`` plus the candidate set and verdict still answer "which
  agents were even considered".

Neither gate erases. Both hide, and both say so, naming the two things that do
erase — retention expiry and the trace-clear endpoint.

Per-origin write policy (``app_mcp`` defaulting to metadata-only) is
**deliberately absent**: no ``origin="app_mcp"`` capture exists, so the setting
that expressed it was unreachable at every value and has been removed. Plan §4
records the condition for bringing it back — the same change that adds the
origin, never before.

Retention is the other half of that §7 argument — the case for storing message
text is that it bounds the *duration* of an exposure the debug buffer already
grants, so :meth:`purge` treats an unbounded window as something an operator has
to ask for by name (``ROUTING_TRACE_RETENTION_FOREVER``), never as the reading
of a vague value.

:meth:`persist` never raises. It is called from the routing pipeline, where a
failed diagnostic write must not cost the caller their message.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func
from sqlmodel import Session as DBSession, select

from app.core.config import ROUTING_TRACE_RETENTION_FOREVER, settings
from app.core.db import create_session
from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle
from app.models.routing.routing_decision import (
    MESSAGE_TEXT_HIDDEN_NOTICE,
    MAX_MATCH_METHOD_CHARS,
    MAX_MESSAGE_SHA256_CHARS,
    MAX_ORIGIN_CHARS,
    MAX_OUTCOME_CHARS,
    MAX_THREAD_KEY_CHARS,
    TRACING_DISABLED_NOTICE,
    RoutingDecision,
    RoutingDecisionPublic,
    RoutingDecisionSummary,
)
from app.models.server_channels.server_channel import ServerChannel
from app.models.users.user import User
from app.services.routing import routing_trace
from app.services.routing.routing_reachability_service import (
    RoutingReachabilityService,
)
from app.services.routing.routing_trace import SAFE_STAGE_FIELDS, RoutingTrace

logger = logging.getLogger(__name__)

# Backstop on a list page, so an admin cannot ask for the whole table.
MAX_PAGE_SIZE = 200


class RoutingTraceService:
    """Write, read and expire ``routing_decision`` rows."""

    # ── Write ────────────────────────────────────────────────────────

    @staticmethod
    def persist(
        trace: RoutingTrace | None,
        *,
        preceded_by: RoutingTrace | None = None,
    ) -> uuid.UUID | None:
        """Store a closed trace. Returns the row id, or ``None`` if not written.

        **Opens its own short-lived session and takes no ``db`` argument.** An
        earlier version wrote through the caller's session, which was wrong in
        both directions: the ``commit`` published whatever uncommitted work the
        caller happened to be holding, and — silently, which is worse — a failed
        diagnostic write rolled the caller's work away and returned
        ``None``, indistinguishable from "tracing is off". A diagnostic must not
        be able to move the caller's transaction at all. Owning the session also
        removes the expire-on-commit hazard at every call site at once: callers
        no longer have to hoist attributes before persisting.

        **Never raises.** Called from the routing pipeline, where the caller's
        broad ``except`` would otherwise turn a failed diagnostic write into a
        dropped inbound message. A failure logs and returns ``None``.

        Call this *after* the ``RoutingTrace.capture`` block has exited, so
        ``latency_ms`` and the settled ``outcome`` are on the trace.

        The row keeps the trace's own id, so the ``trace_id`` the live channel
        debug feed already shows is the id to fetch here.

        ``preceded_by`` folds an **earlier pass of the same decision** into this
        row. Channel routing runs Pass 1 and Pass 2 in two separate worker
        threads, and §5 requires each to own its own capture — but one inbound
        message is one decision, and the plan's schema says so: ``stages`` holds
        ``pass_1`` *and* ``pass_2``. Writing a row per pass would put a
        ``no_match`` row on the ``?outcome=no_match`` filter — the one view an
        admin uses to answer "why didn't it find my agent" — for every message
        Pass 2 went on to handle successfully. So the passes merge: stages
        concatenate in order, latency sums, ``created_at`` comes from the
        *earlier* pass (the decision began when Pass 1 did, so taking Pass 2's
        would put the row's start after work it accounts for), and the terminal
        pass supplies the outcome.

        **Any ``error`` on the row promotes the outcome.** ``_route_installed``
        catches a router outage, records it, and returns ``None`` rather than
        raising — "a router outage must not 500 the webhook". So no exception
        reaches the thread target, Pass 2 runs normally, and without this the
        whole provider outage would persist as ``outcome=no_match, error=NULL``:
        the exception text gone, and ``?outcome=error`` — the filter that exists
        to surface exactly this — empty. The earlier pass's error is prefixed
        with its stage, and whenever the merged row carries an error but no
        positive verdict, the outcome is promoted to ``error``.

        That promotion is deliberately **not** limited to the ``preceded_by``
        case, and it deliberately duplicates ``RoutingTrace._settle_locked``'s
        rule. The two enforce the same invariant — *a trace carrying an error
        settles as an error* — at ends that cannot see each other:
        ``_settle_locked`` fixes the in-memory trace (which the live debug feed
        also reads, via ``summarize``), while this fixes an error arriving from
        a **different trace object** the settler was never handed. Neither is
        redundant, and the shared carve-out is the same one: a later pass that
        genuinely ``routed`` or ``parked_install``ed keeps its verdict.

        ``match_method`` falls back to the earlier pass when the later one has
        none. That is the diagnostic residue the whole feature exists for: Pass 1
        classifying a match that the ownership filter then rejected is a
        completely different story from Pass 1 seeing nothing, and only
        ``match_method`` distinguishes them.
        """
        if trace is None:
            trace, preceded_by = preceded_by, None
        if trace is None:
            return None
        try:
            if not settings.ROUTING_TRACE_ENABLED:
                return None

            store_text = bool(settings.ROUTING_TRACE_STORE_MESSAGE_TEXT)

            # Stages are concatenated, not merged by name, and the premise that
            # made that safe has narrowed — read this before adding a caller.
            #
            # Pass 1 CAN now emit a ``pass_2`` stage: its single-candidate
            # short-circuit scans the catalog for availability and writes that
            # scan under ``pass_2``
            # (``ChannelRoutingService._record_catalog_ballot``). What still
            # prevents a collision is that the two writers are mutually
            # exclusive by construction — Pass 1 writes that stage only when it
            # ROUTED, and ``decide`` runs Pass 2 only when Pass 1 returned
            # nothing — so ``preceded_by`` never carries a second ``pass_2``.
            # That is now a fact about ``decide``'s branching rather than about
            # which names each pass can produce. If a change makes both write in
            # one decision, merge by name here; a UI keying on the stage name
            # will not expect two entries with the same value.
            stages: list = []
            prior_latency = 0
            prior_match_method: str | None = None
            prior_error: str | None = None
            started_at = trace.created_at
            if preceded_by is not None:
                stages.extend(preceded_by.stages_payload())
                prior_latency = preceded_by.latency_ms
                prior_match_method = preceded_by.match_method
                prior_error = preceded_by.error
                started_at = preceded_by.created_at
            stages.extend(trace.stages_payload())

            outcome = trace.outcome or routing_trace.OUTCOME_NO_MATCH
            error = trace.error
            if prior_error:
                # Both segments are keyed on the STAGE that raised. Keying the
                # second on ``outcome`` (as this once did) produced
                # "pass_1: ProviderError… | no_match: ProviderError…", which
                # reads as though an error belonged to a verdict rather than to
                # a pass — and was order-fragile besides, since ``outcome`` is
                # reassigned a few lines below.
                prior_label = preceded_by.default_stage or "earlier stage"
                later_label = trace.default_stage or "later stage"
                error = (
                    f"{prior_label}: {prior_error}"
                    if not error
                    else f"{prior_label}: {prior_error} | {later_label}: {error}"
                )
            # A pass that blew up followed by a pass that simply found nothing
            # is an outage, not a clean "no match" — say so. Applied to any
            # error on the merged row, not only a carried-forward one: see the
            # docstring on why this and ``_settle_locked`` both enforce it.
            positive = (
                routing_trace.OUTCOME_ROUTED,
                routing_trace.OUTCOME_PARKED_INSTALL,
            )
            if error and outcome not in positive:
                outcome = routing_trace.OUTCOME_ERROR
            # ``_settle_locked``'s OTHER invariant, mirrored for the same reason
            # the promotion above is: a non-positive outcome names no selection.
            # Unreachable today — every path into ``persist`` has already been
            # through ``record_outcome`` / ``record_error`` / ``finish``, all of
            # which settle and therefore clear first — but the docstring's whole
            # argument is that neither end depends on the other, and half-mirrored
            # enforcement makes that claim false. The promotion above can itself
            # produce a non-positive outcome from a positive one, so without this
            # the two rules would be applied in an order where the second never
            # sees the first's output.
            selected_agent_id = trace.selected_agent_id
            selected_bundle_uuid = trace.selected_bundle_uuid
            confidence = trace.confidence
            if outcome not in positive:
                selected_agent_id = None
                selected_bundle_uuid = None
                confidence = None

            row = RoutingDecision(
                # The row IS the trace: reuse the recorder's id rather than
                # minting a new one. ``ChannelDebugBuffer`` already publishes
                # ``detail.trace_id`` on the live feed, and that link is only
                # worth having if it is a key into this table.
                id=_as_uuid(trace.trace_id) or uuid.uuid4(),
                created_at=started_at,
                channel_id=_as_uuid(trace.channel_id),
                user_id=_as_uuid(trace.user_id),
                actor_user_id=_as_uuid(trace.actor_user_id),
                thread_key=_fit(trace.thread_key, MAX_THREAD_KEY_CHARS),
                message_text=(
                    RoutingTraceService._clamp_text(trace.message_text)
                    if store_text
                    else None
                ),
                # NOT ``_fit``: prefix-truncating a hash is the wrong failure
                # mode. A 65-char value would become a 64-char prefix that can
                # never match ``ix_routing_decision_message_sha256``, so
                # replay/dedupe would silently return nothing instead of
                # erroring. Dropping the field says "no usable hash" out loud.
                message_sha256=_fit_exact(
                    trace.message_sha256, MAX_MESSAGE_SHA256_CHARS
                ),
                # Clamped to the column widths even though every value written
                # today is a module constant. A vocabulary string that outgrew
                # its column would raise a DataError inside the never-raises
                # guard below, and the whole row would vanish silently rather
                # than one field truncating visibly.
                origin=_fit(trace.origin, MAX_ORIGIN_CHARS) or "",
                outcome=_fit(outcome, MAX_OUTCOME_CHARS)
                or routing_trace.OUTCOME_NO_MATCH,
                # Carried through verbatim, including on a ``no_match``. See
                # the model's column docs: this is how the last stage matched,
                # not how the decision was reached, and the residue after a
                # rejection is the diagnosis worth having.
                match_method=_fit(
                    trace.match_method or prior_match_method, MAX_MATCH_METHOD_CHARS
                ),
                selected_agent_id=_as_uuid(selected_agent_id),
                selected_bundle_uuid=_as_uuid(selected_bundle_uuid),
                confidence=confidence,
                latency_ms=trace.latency_ms + prior_latency,
                # WRITE gate, not only a read gate. Stage fields carry the
                # sender's text as surely as ``message_text`` does, so a
                # projection-only scrub would hide them from the API while
                # leaving them in the database for the full retention window —
                # breaking the promise §7 makes, the one ``config.py`` makes, and
                # the one ``MESSAGE_TEXT_HIDDEN_NOTICE`` makes to an operator's
                # face.
                stages=stages if store_text else _project_safe_stages(stages),
                error=RoutingTraceService._clamp_text(error),
            )
            row_id = row.id
            # Its own session, so nothing here can commit or discard work the
            # caller is holding. Closed before we return.
            with create_session() as db:
                db.add(row)
                db.commit()
            # No ``refresh``: the id was assigned client-side, so re-SELECTing
            # the row on the routing hot path would buy nothing.
            return row_id
        except Exception:  # noqa: BLE001 — a diagnostic must never break routing
            logger.warning("Failed to persist routing trace", exc_info=True)
            return None

    @staticmethod
    def _clamp_text(text: str | None) -> str | None:
        return routing_trace.clamp(text, settings.ROUTING_TRACE_TEXT_MAX_CHARS)

    # ── Read ─────────────────────────────────────────────────────────

    @staticmethod
    def disabled_notice() -> str | None:
        """Server-authored explanation when the read gate is closed, else ``None``.

        Phase 4 renders this rather than inventing copy that overstates what the
        flag did. Without it, ``ROUTING_TRACE_ENABLED=False`` would look exactly
        like "there have been no routing decisions" — §11a Rule 1: the dangerous
        state must not be able to look routine, and its corollary, a control must
        not appear to do *more* than it does.
        """
        return None if settings.ROUTING_TRACE_ENABLED else TRACING_DISABLED_NOTICE

    @staticmethod
    def list(
        db: DBSession,
        *,
        channel_id: uuid.UUID | None = None,
        origin: str | None = None,
        outcome: str | None = None,
        user_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[RoutingDecisionSummary], int]:
        """Decisions matching the filters, newest first, plus the total count.

        The count is the size of the filtered set, not of the page — the card's
        pager needs to know there is a page 2.

        ``ROUTING_TRACE_ENABLED`` is a READ gate here, not only the write gate it
        was. It used to be consulted in exactly one place — :meth:`persist` — so
        turning off what this module called the "master switch" left the API
        serving up to ``ROUTING_TRACE_RETENTION_DAYS`` of stored rows, message
        text included, with no notice. That is the same asymmetry §7 deliberately
        rejected for the text gate. Enforced in the service rather than in the
        route so a second caller cannot bypass it; the route pairs the empty page
        with :meth:`disabled_notice` so "off" never renders as "nothing here".
        """
        if not settings.ROUTING_TRACE_ENABLED:
            return [], 0

        conditions: list[Any] = []
        if channel_id is not None:
            conditions.append(RoutingDecision.channel_id == channel_id)
        if origin:
            conditions.append(RoutingDecision.origin == origin)
        if outcome:
            conditions.append(RoutingDecision.outcome == outcome)
        if user_id is not None:
            conditions.append(RoutingDecision.user_id == user_id)

        count = (
            db.exec(
                select(func.count()).select_from(RoutingDecision).where(*conditions)
            ).one()
            or 0
        )
        rows = db.exec(
            select(RoutingDecision)
            .where(*conditions)
            # ``id`` breaks ties: ``created_at`` is assigned in Python, so two
            # rows can share a timestamp and a paged read would then be free to
            # return one of them twice and the other never.
            .order_by(RoutingDecision.created_at.desc(), RoutingDecision.id.desc())
            .offset(max(skip, 0))
            .limit(min(max(limit, 1), MAX_PAGE_SIZE))
        ).all()

        names = _NameResolver(db, rows)
        return [
            RoutingTraceService._to_summary(row, names) for row in rows
        ], count

    @staticmethod
    def get(
        db: DBSession,
        decision_id: uuid.UUID,
        *,
        expected_agent_id: uuid.UUID | None = None,
    ) -> RoutingDecisionPublic | None:
        """One decision with its full stage trace, or ``None``.

        ``None`` also when ``ROUTING_TRACE_ENABLED`` is off — same read gate as
        :meth:`list`, and the route turns it into the same 404 a missing id gets,
        carrying :meth:`disabled_notice` as the detail so the reader is told
        which of the two it was.

        ``expected_agent_id`` narrows the attached ``diagnosis`` to one agent —
        "why was *this* one not a candidate", the question the tuning card is
        actually opened to answer. Optional, and the un-narrowed verdict is
        still a real answer ("N effective routes, none matched"), so the default
        stays free of it.

        **The diagnosis is attached here rather than in the route**, for the
        same reason ``RoutingTuningService.simulate`` returns this function's
        output rather than a matching projection: a simulate response has to
        expose exactly what a stored trace exposes, and the only way to say that
        so it cannot drift is for both to be the same call. Composing the
        diagnosis one layer up would have given the two surfaces different
        bodies the moment either grew a field.

        It is also computed from the **projected** model, not the row — see
        :meth:`RoutingReachabilityService.diagnose`. That is what keeps the
        near-miss ranking honest when the message-text gate is closed, without a
        second rule that has to remember the gate exists.
        """
        if not settings.ROUTING_TRACE_ENABLED:
            return None
        row = db.get(RoutingDecision, decision_id)
        if row is None:
            return None
        summary = RoutingTraceService._to_summary(row, _NameResolver(db, [row]))
        # READ gate through the same allowlist the write gate uses. Rows written
        # while the flag was ON keep their prompt and raw response at rest — §7
        # is explicit that the flag hides and does not erase — so the projection
        # has to happen here too, or flipping the flag would leave the detail
        # endpoint serving the sender's text while ``message_text_hidden=True``
        # claimed it was withheld. A response that actively asserts something
        # false is worse than one that merely omits.
        stages = list(row.stages or [])
        public = RoutingDecisionPublic(
            **summary.model_dump(),
            stages=(
                stages
                if settings.ROUTING_TRACE_STORE_MESSAGE_TEXT
                else _project_safe_stages(stages)
            ),
        )
        public.diagnosis = RoutingReachabilityService.diagnose(
            db, public, expected_agent_id=expected_agent_id
        )
        return public

    # ── Retention ────────────────────────────────────────────────────

    @staticmethod
    def purge(db: DBSession, retention_days: int | None = None) -> int:
        """Delete decisions older than the retention window. Returns the count.

        A bulk ``DELETE`` rather than a load-then-delete loop: nothing here has
        ORM cascades or events hanging off it, and the purge runs against a
        table sized by traffic.

        ``ROUTING_TRACE_RETENTION_FOREVER`` (``-1``) — and *only* that value —
        disables expiry. Everything else below ``1`` raises rather than
        quietly returning 0: this used to infer "keep forever" from ``<= 0``,
        which was a second, silent route to unbounded retention of external
        senders' message text, reachable without ever passing the settings
        validator. The sentinel is shared with ``config.py`` so the two ends
        cannot drift apart.

        Settings validation makes the bad value unreachable from configuration,
        so a raise here can only mean a caller passed one explicitly — a
        programming error, and one worth surfacing. The scheduler's job wrapper
        logs it rather than letting it kill the loop.
        """
        days = (
            retention_days
            if retention_days is not None
            else settings.ROUTING_TRACE_RETENTION_DAYS
        )
        if days == ROUTING_TRACE_RETENTION_FOREVER:
            return 0
        if days is None or days < 1:
            raise ValueError(
                f"retention_days must be at least 1, or exactly "
                f"{ROUTING_TRACE_RETENTION_FOREVER} to keep routing traces "
                f"forever (got {days})."
            )
        cutoff = datetime.now(UTC) - timedelta(days=days)
        result = db.execute(
            delete(RoutingDecision).where(RoutingDecision.created_at < cutoff)
        )
        db.commit()
        return int(result.rowcount or 0)

    @staticmethod
    def clear(
        db: DBSession, *, channel_id: uuid.UUID | None = None
    ) -> int:
        """Drop every decision (optionally only one channel's). Returns the count.

        Mirrors the debug-events clear: a diagnostic surface needs a way to
        start from a known-empty state before reproducing a problem.

        **A channel-scoped clear does not touch simulate rows.** A trace from
        ``POST /admin/routing/simulate`` typed in by hand has no channel, so it
        matches no ``channel_id`` filter and only the unscoped ``?all=true``
        form removes it. That is the correct behaviour for a scoped delete, but
        it matters here because this endpoint is one of the two erasure paths
        the hidden-state notices name: an operator emptying "this channel's"
        traces under privacy pressure has not emptied everything.

        **Deliberately NOT gated by ``ROUTING_TRACE_ENABLED``**, unlike
        :meth:`list` and :meth:`get`. This is the erasure path both hidden-state
        notices name to operators. Gating it would mean that turning tracing off
        — the obvious first move under privacy pressure — also removed the only
        way to delete the rows already written, leaving them for the full
        retention window with no way out. The gate stops serving; this empties.
        """
        statement = delete(RoutingDecision)
        if channel_id is not None:
            statement = statement.where(RoutingDecision.channel_id == channel_id)
        result = db.execute(statement)
        db.commit()
        return int(result.rowcount or 0)

    # ── Projection ───────────────────────────────────────────────────

    @staticmethod
    def _to_summary(
        row: RoutingDecision, names: _NameResolver
    ) -> RoutingDecisionSummary:
        candidates, skipped = _count_candidates(row.stages)
        provider, model = _winning_provider(row.stages)
        # ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` is a READ gate as well as a write
        # gate. Gating only the write would leave up to
        # ``ROUTING_TRACE_RETENTION_DAYS`` of already-captured external senders'
        # text still served from this API after an operator turned the flag off
        # — and someone turning it off under privacy pressure means "stop
        # showing me this text", not "stop adding to the pile".
        #
        # ``message_text`` is the only gated field a *summary* carries; the
        # stage payload is gated by the same flag and projected in ``get`` (see
        # ``_project_safe_stages``). Same flag, both directions — one rule to
        # reason about.
        #
        # It hides; it does not erase. Deliberately no purge on flag-flip: an
        # accidental toggle would irreversibly destroy diagnostic data, and a
        # privacy control whose misfire is unrecoverable is its own hazard.
        # Hide on flip, erase on explicit request — retention expiry or the
        # trace-clear endpoint, both named in the notice the UI renders.
        #
        # ``message_sha256`` is deliberately NOT swept up: it is what keeps a
        # gated trace replayable, and it disambiguates "text withheld" from
        # "there was no message".
        hidden = not settings.ROUTING_TRACE_STORE_MESSAGE_TEXT
        return RoutingDecisionSummary(
            id=row.id,
            created_at=row.created_at,
            origin=row.origin,
            channel_id=row.channel_id,
            channel_name=names.channel(row.channel_id),
            user_id=row.user_id,
            user_email=names.user_email(row.user_id),
            actor_user_id=row.actor_user_id,
            thread_key=row.thread_key,
            message_text=None if hidden else row.message_text,
            message_sha256=row.message_sha256,
            message_text_hidden=hidden,
            message_text_notice=MESSAGE_TEXT_HIDDEN_NOTICE if hidden else None,
            outcome=row.outcome,
            match_method=row.match_method,
            selected_agent_id=row.selected_agent_id,
            selected_agent_name=names.agent(row.selected_agent_id),
            selected_bundle_uuid=row.selected_bundle_uuid,
            selected_bundle_name=names.bundle(row.selected_bundle_uuid),
            confidence=row.confidence,
            latency_ms=row.latency_ms,
            error=row.error,
            candidate_count=candidates,
            skipped_count=skipped,
            provider=provider,
            model=model,
        )


class _NameResolver:
    """Batched id → display-name lookups for a page of decisions.

    One query per referenced table for the whole page, rather than four per
    row. A page of 50 no-match traces would otherwise issue 200 round trips to
    render a table that shows four names.
    """

    def __init__(self, db: DBSession, rows: list[RoutingDecision]) -> None:
        self._channels = _fetch_names(
            db, ServerChannel, ServerChannel.name, {r.channel_id for r in rows}
        )
        self._users = _fetch_names(
            db, User, User.email, {r.user_id for r in rows}
        )
        self._agents = _fetch_names(
            db, Agent, Agent.name, {r.selected_agent_id for r in rows}
        )
        self._bundles = _fetch_names(
            db,
            AgentBundle,
            AgentBundle.display_name,
            {r.selected_bundle_uuid for r in rows},
        )

    def channel(self, key: uuid.UUID | None) -> str | None:
        return self._channels.get(key) if key else None

    def user_email(self, key: uuid.UUID | None) -> str | None:
        return self._users.get(key) if key else None

    def agent(self, key: uuid.UUID | None) -> str | None:
        return self._agents.get(key) if key else None

    def bundle(self, key: uuid.UUID | None) -> str | None:
        return self._bundles.get(key) if key else None


def _fetch_names(
    db: DBSession, model: Any, name_column: Any, ids: set[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = db.exec(
        select(model.id, name_column).where(model.id.in_(wanted))
    ).all()
    return {row[0]: row[1] for row in rows}


def _count_candidates(stages: Any) -> tuple[int, int]:
    """``(total, skipped)`` across every stage. Tolerant of any stored shape.

    ``stages`` is JSONB written by a recorder whose dataclasses will change.
    A list projection must not 500 because an older row is shaped differently,
    so every access here is defensive and the fallback is zero.
    """
    total = 0
    skipped = 0
    try:
        for stage in stages or []:
            for candidate in (stage or {}).get("candidates") or []:
                total += 1
                if not (candidate or {}).get("eligible", True):
                    skipped += 1
    except Exception:  # noqa: BLE001
        logger.debug("Routing decision candidate count failed", exc_info=True)
    return total, skipped


def _winning_provider(stages: Any) -> tuple[str | None, str | None]:
    """The provider/model that actually answered.

    The last *successful* attempt across all stages — that is the one whose
    output produced the verdict. When every provider failed, falls back to the
    last attempt of any kind, so a cascade that died still names what was tried
    rather than showing blanks. Defensive throughout: ``stages`` is JSONB whose
    shape follows a recorder that will keep changing, and a list projection
    must not 500 on an older row.
    """
    winner: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    try:
        for stage in stages or []:
            for attempt in (stage or {}).get("llm_attempts") or []:
                attempt = attempt or {}
                last = attempt
                if attempt.get("ok"):
                    winner = attempt
    except Exception:  # noqa: BLE001
        logger.debug("Routing decision provider projection failed", exc_info=True)
    chosen = winner or last
    if not chosen:
        return None, None
    return chosen.get("provider"), chosen.get("model")


def _project_safe_stages(stages: Any) -> list:
    """``stages`` projected through the ``SAFE_STAGE_FIELDS`` allowlist.

    Used on **both** paths from one definition, so write-gating and read-gating
    cannot drift into covering different fields.

    **An allowlist, not a denylist, and that is the change worth understanding.**
    This was ``_scrub_stage_text``: it blanked a tuple of known-tainted field
    names. Three separate rounds of enumerating those names each shipped one
    field short — ``message_text``, then ``stages[].prompt`` /
    ``raw_response``, then ``llm_attempts[].error`` — because sender text is a
    taint that *propagates* into new fields by ordinary-looking changes, so a
    denylist is structurally always one field behind. Inverted, a field nobody
    has declared safe is simply not served, and the cost of forgetting is a
    missing diagnostic instead of a leak.

    **Nested-aware**, which the flat scrub was not: that is precisely why
    ``llm_attempts[].error`` sat outside a gate believed complete. A spec entry
    of ``None`` means "a JSON scalar, copied through"; a tuple means "a list of
    objects, each projected through these field names". A *container* declared
    as a scalar is dropped rather than passed through, so mis-declaring a newly
    added nested structure fails closed too.

    Copies rather than mutating: on the write path the input comes from
    ``stages_payload()`` (already a fresh projection), but on the read path it is
    the live ORM attribute of a ``RoutingDecision``, and mutating that in place
    would mark the row dirty and risk writing the projection back to the database
    — an erase, on a flag whose whole contract is that it does not erase.

    **Fails closed.** Anything unexpected in the stored shape drops the stage
    list entirely rather than passing it through ungated: losing a diagnostic is
    recoverable, leaking the text the operator just asked to hide is not.
    """
    try:
        projected: list = []
        for stage in stages or []:
            if not isinstance(stage, dict):
                # Unknown shape — cannot prove it holds no sender text.
                continue
            projected.append(_project_object(stage, SAFE_STAGE_FIELDS))
        return projected
    except Exception:  # noqa: BLE001 — fail closed, never raise
        logger.warning("Routing decision stage projection failed", exc_info=True)
        return []


def _project_object(
    source: dict, spec: dict[str, tuple[str, ...] | None] | tuple[str, ...]
) -> dict:
    """One object projected through one allowlist entry. Keys not named are gone.

    ``spec`` is either a mapping (nested-capable: ``None`` for a scalar, a tuple
    of names for a list of objects) or a plain tuple of scalar field names.
    """
    if isinstance(spec, tuple):
        spec = dict.fromkeys(spec)
    result: dict[str, Any] = {}
    for name, nested in spec.items():
        if name not in source:
            continue
        value = source[name]
        if nested is None:
            # Scalars only. A container reaching here means a new nested
            # structure was declared as a scalar; dropping it is the fail-closed
            # reading, since nothing has vouched for what is inside it.
            if isinstance(value, (str, int, float, bool)) or value is None:
                result[name] = value
            continue
        if not isinstance(value, list):
            continue
        result[name] = [
            _project_object(item, nested)
            for item in value
            if isinstance(item, dict)
        ]
    return result


def _fit_exact(value: Any, width: int) -> str | None:
    """Like :func:`_fit`, but **drops** an over-long value instead of truncating.

    For fixed-length, exact-match fields — ``message_sha256`` is the only one
    today. A hex SHA-256 is exactly 64 characters, so truncation cannot produce a
    "slightly wrong but still useful" value: it produces a prefix that matches
    nothing in ``ix_routing_decision_message_sha256``, and replay/dedupe then
    returns an empty result that looks like "no such message" rather than like a
    corrupted key. ``None`` is honest about the same failure.
    """
    text = _fit(value, width + 1)
    if text is None:
        return None
    return text if len(text) <= width else None


def _fit(value: Any, width: int) -> str | None:
    """Hard-truncate to a fixed-width column. ``None`` for empty input.

    Deliberately **not** ``routing_trace.clamp``: that appends a
    "… (truncated)" marker, which is right for a Text column an admin reads and
    exactly wrong for a ``VARCHAR(32)`` — the marker pushes the value back over
    the limit and the insert raises. That raise would land in ``persist``'s
    never-raises guard and take the whole row with it, so an over-long
    vocabulary value would silently produce no trace at all.
    """
    if value is None:
        return None
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:  # noqa: BLE001 — a diagnostic must never break routing
        logger.debug("Routing decision field coercion failed", exc_info=True)
        return None
    if not text:
        return None
    return text[:width]


def _as_uuid(value: Any) -> uuid.UUID | None:
    """Coerce a trace's stringified id back to a UUID, tolerating junk.

    The recorder stores ids as strings (it has no model imports), and a
    placeholder id from an identity route is not always a real UUID.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
