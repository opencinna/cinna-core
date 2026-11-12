"""ImprovementRequestService — the one cross-user data path, kept narrow.

Consent **is** the write: a request row can only be created by an authenticated
user acting on their own session. There is no admin, publisher, or automated
path that creates one. Everything after the write is read-only projection of
frozen data — see ``docs/plans/agent_improvement_requests_plan.md`` §4.

Responsibilities:

* :meth:`resolve_target` — who receives the request (publisher install, or self)
* :meth:`create_from_session` — the eligibility gate, capture, scrub, write, emit
* :meth:`build_context_preview` — the consent modal's pre-flight, running the
  *same* gate and the *same* resolution so the copy can never disagree with what
  submitting will do
* :meth:`create_from_agent_api` — a capability request filed by an identified
  caller of an agent's REST API (``kind = agent_api``, no snapshot)
* list / get / update / delete surfaces, with 404-not-403 semantics for
  inaccessible ids
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

from sqlalchemy import case, func
from sqlmodel import Session as DBSession, select

from app.models.agent_api.agent_api_token import AgentApiToken
from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle
from app.models.events.event import EventType
from app.models.improvement.agent_improvement_request import (
    AgentApiCapabilityRequestCreate,
    AgentApiCapabilityRequestPublic,
    AgentApiRequestToolDetail,
    AgentApiRequestToolRow,
    AgentImprovementRequest,
    FALLBACK_PUBLISHER_UNAVAILABLE,
    IMPROVEMENT_KIND_AGENT_API,
    IMPROVEMENT_KIND_SESSION,
    IMPROVEMENT_SOURCE_AGENT_API,
    IMPROVEMENT_SOURCE_COMMAND,
    IMPROVEMENT_SOURCE_WEB_UI,
    IMPROVEMENT_STATUSES,
    IMPROVEMENT_STATUS_NEW,
    ImprovementContextPublic,
    ImprovementRequestDetailPublic,
    ImprovementRequestPublic,
    MAX_COMMENT_CHARS,
    MAX_RESOLUTION_NOTE_CHARS,
)
from app.models.sessions.session import Session as ChatSession, SessionMessage
from app.models.users.user import User
from app.services.credentials.credentials_service import CredentialsService
from app.services.improvement import secret_scrubber
from app.services.improvement.improvement_archive_service import (
    ImprovementArchiveService,
    archive_filename,
)
from app.services.improvement.session_snapshot_service import SessionSnapshotService

logger = logging.getLogger(__name__)

# ── Rate limits (plan §4.4 rule 5) ───────────────────────────────────
MAX_REQUESTS_PER_SESSION = 5
MAX_REQUESTS_PER_USER_PER_DAY = 20
RATE_LIMIT_WINDOW = timedelta(hours=24)

# The surfaces a *session* request may come from.
SESSION_SOURCES: frozenset[str] = frozenset(
    {IMPROVEMENT_SOURCE_WEB_UI, IMPROVEMENT_SOURCE_COMMAND}
)

# Agent-API capability requests (plan D9): DB-counted over the same rolling
# window, so the limit holds across workers and is deterministic in tests.
MAX_AGENT_API_REQUESTS_PER_PRODUCER = 10
MAX_AGENT_API_REQUESTS_PER_CALLER = 30
# The consumer's "my requests on this API" list is a short status board.
MAX_AGENT_API_REQUESTER_LIST = 50

# Denial reasons — stable machine-readable codes the modal and the command
# handler both key off.
REASON_NOT_OWNER = "not_owner"
REASON_NOT_ELIGIBLE = "not_eligible"
REASON_EMPTY_SESSION = "empty_session"
REASON_AGENT_MISSING = "agent_missing"
REASON_RATE_LIMITED = "rate_limited"
REASON_NOT_FOUND = "not_found"
REASON_INVALID_STATUS = "invalid_status"

ROLE_OWNER = "owner"
ROLE_REQUESTER = "requester"


class ImprovementRequestDenied(Exception):
    """A typed refusal both transports can map.

    The REST layer turns it into ``HTTPException(status_code, detail=message)``;
    the ``/session-improve`` command handler turns it into
    ``CommandResult(content=message, is_error=True)``.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        status_code: int = 400,
        retry_after: int | None = None,
    ) -> None:
        self.reason = reason
        self.message = message
        self.status_code = status_code
        # Seconds until a rate-limited caller may retry (``Retry-After``).
        self.retry_after = retry_after
        super().__init__(message)


@dataclass
class TargetResolution:
    """Where an improvement request lands, and why."""

    target_agent: Agent
    owner_user_id: uuid.UUID
    bundle: AgentBundle | None = None
    fallback_reason: str | None = None


def display_name_for_user(user: User | None) -> str | None:
    """A human label for a user, preferring the least-identifying form."""
    if user is None:
        return None
    return user.full_name or user.username or (user.email or "").split("@")[0] or None


class ImprovementRequestService:
    """Create, authorize, project, and transition improvement requests."""

    # ── Target resolution ────────────────────────────────────────────

    @staticmethod
    def resolve_target(db: DBSession, source_agent: Agent) -> TargetResolution:
        """Decide who receives a request raised against ``source_agent``.

        A consumer install of a published bundle routes to the **publisher
        install** so the person who can actually fix the agent sees it.
        Everything else (standalone agent, or the publisher's own working
        install) targets the agent itself.

        Falling back to self when no publisher install is reachable is safe by
        construction: it can only ever *narrow* who can read the data.
        """
        if source_agent.bundle_uuid is not None and not source_agent.is_publisher_install:
            bundle = db.get(AgentBundle, source_agent.bundle_uuid)
            if bundle is not None and bundle.publisher_user_id is not None:
                publisher_install = db.exec(
                    select(Agent).where(
                        Agent.bundle_uuid == bundle.id,
                        Agent.is_publisher_install == True,  # noqa: E712
                    )
                ).first()
                if publisher_install is not None:
                    return TargetResolution(
                        target_agent=publisher_install,
                        # Taken from the install, not `bundle.publisher_user_id`:
                        # `list_for_agent` authorizes the card query on
                        # `agent.owner_id`, so sourcing both from the same column
                        # makes the two surfaces unable to disagree.
                        owner_user_id=publisher_install.owner_id,
                        bundle=bundle,
                    )
            # Publisher install deleted, or an ownerless git-imported bundle.
            return TargetResolution(
                target_agent=source_agent,
                owner_user_id=source_agent.owner_id,
                bundle=bundle,
                fallback_reason=FALLBACK_PUBLISHER_UNAVAILABLE,
            )

        bundle = (
            db.get(AgentBundle, source_agent.bundle_uuid)
            if source_agent.bundle_uuid
            else None
        )
        return TargetResolution(
            target_agent=source_agent,
            owner_user_id=source_agent.owner_id,
            bundle=bundle,
        )

    # ── Eligibility (plan §4.4) ──────────────────────────────────────

    @staticmethod
    def _evaluate_eligibility(
        db: DBSession, session: ChatSession, requester: User
    ) -> tuple[Agent | None, int, ImprovementRequestDenied | None]:
        """Run the submission gate without raising.

        Returns ``(source_agent, message_count, denial)``. ``create_from_session``
        raises the denial; ``build_context_preview`` reports it. Sharing one
        implementation is what keeps the modal's copy honest.
        """
        if session.user_id != requester.id:
            return None, 0, ImprovementRequestDenied(
                REASON_NOT_OWNER,
                "Only the owner of this session can share it.",
                status_code=403,
            )

        if session.guest_share_id is not None or session.webapp_share_id is not None:
            return None, 0, ImprovementRequestDenied(
                REASON_NOT_ELIGIBLE,
                "Sessions started from a guest or webapp share cannot be shared "
                "— there is no identifiable consenting account behind them.",
            )

        message_count = (
            db.exec(
                select(func.count())
                .select_from(SessionMessage)
                .where(SessionMessage.session_id == session.id)
            ).one()
            or 0
        )
        if message_count < 1:
            return None, 0, ImprovementRequestDenied(
                REASON_EMPTY_SESSION,
                "This session has no messages yet, so there is nothing to share.",
            )

        source_agent = (
            db.get(Agent, session.agent_id) if session.agent_id else None
        )
        if source_agent is None:
            return None, message_count, ImprovementRequestDenied(
                REASON_AGENT_MISSING,
                "The agent this session ran on no longer exists.",
            )

        denial = ImprovementRequestService._check_rate_limits(db, session, requester)
        return source_agent, message_count, denial

    @staticmethod
    def _check_rate_limits(
        db: DBSession, session: ChatSession, requester: User
    ) -> ImprovementRequestDenied | None:
        per_session = (
            db.exec(
                select(func.count())
                .select_from(AgentImprovementRequest)
                .where(AgentImprovementRequest.session_id == session.id)
            ).one()
            or 0
        )
        if per_session >= MAX_REQUESTS_PER_SESSION:
            return ImprovementRequestDenied(
                REASON_RATE_LIMITED,
                f"You have already submitted {MAX_REQUESTS_PER_SESSION} improvement "
                "requests for this session, which is the limit.",
                status_code=429,
            )

        since = datetime.now(UTC) - RATE_LIMIT_WINDOW
        per_user = (
            db.exec(
                select(func.count())
                .select_from(AgentImprovementRequest)
                .where(
                    AgentImprovementRequest.requester_user_id == requester.id,
                    # Capability requests have their own quota (D9).
                    AgentImprovementRequest.kind == IMPROVEMENT_KIND_SESSION,
                    AgentImprovementRequest.created_at >= since,
                )
            ).one()
            or 0
        )
        if per_user >= MAX_REQUESTS_PER_USER_PER_DAY:
            return ImprovementRequestDenied(
                REASON_RATE_LIMITED,
                f"You have submitted {MAX_REQUESTS_PER_USER_PER_DAY} improvement "
                "requests in the last 24 hours, which is the limit. Try again later.",
                status_code=429,
            )
        return None

    # ── Creation ─────────────────────────────────────────────────────

    @staticmethod
    async def create_from_session(
        db: DBSession,
        session: ChatSession,
        requester: User,
        comment: str | None = None,
        source: str = IMPROVEMENT_SOURCE_WEB_UI,
        include_memory: bool = True,
    ) -> AgentImprovementRequest:
        """Capture, scrub, and persist the request; notify the recipient.

        Args:
            include_memory: whether to capture the install's personal memory
                area. Prompts are always captured — they are agent
                configuration, and for a bundle install the baseline text is
                the publisher's own. ``app-data/memory`` is not: it holds the
                requester's personal notes, so it is opt-**out** rather than
                unconditional, and declining reads nothing at all.

        Raises:
            ImprovementRequestDenied: when the §4.4 gate fails.
        """
        source_agent, _, denial = ImprovementRequestService._evaluate_eligibility(
            db, session, requester
        )
        if denial is not None:
            raise denial
        assert source_agent is not None  # guaranteed by the gate

        # Only the session surfaces may file a session request; ``agent_api``
        # belongs to ``create_from_agent_api``.
        if source not in SESSION_SOURCES:
            raise ImprovementRequestDenied(
                REASON_NOT_ELIGIBLE,
                f"Unsupported submission source: {source}.",
            )

        resolution = ImprovementRequestService.resolve_target(db, source_agent)

        snapshot, truncated, message_count = SessionSnapshotService.capture(db, session)
        context = SessionSnapshotService.capture_context(
            db, session, source_agent, resolution
        )
        # The one live container read in this feature, and it happens *before*
        # the row is written — after that the no-live-read-through invariant
        # holds as absolutely as it did before.
        context["memory"] = await SessionSnapshotService.capture_memory(
            db, session, source_agent, include=include_memory
        )

        # Both blocks are scrubbed. The transcript is free text by definition;
        # the context block is mostly ids, names and settings — which the
        # scrubber leaves alone because they are not under a
        # ``SCRUBBED_KEYS`` key — but its ``prompts`` and ``memory`` entries
        # carry captured documents, and those are exactly the kind of free text
        # a pasted token hides in.
        secrets = ImprovementRequestService._collect_secrets(db, source_agent)
        snapshot, snapshot_hits = secret_scrubber.scrub(snapshot, secrets)
        context, context_hits = secret_scrubber.scrub(context, secrets)
        # Observability only — the values themselves are never logged.
        context.setdefault("platform", {})["scrubbed_hits"] = (
            snapshot_hits + context_hits
        )

        request = AgentImprovementRequest(
            session_id=session.id,
            source_agent_id=source_agent.id,
            target_agent_id=resolution.target_agent.id,
            bundle_uuid=resolution.bundle.id if resolution.bundle else None,
            requester_user_id=requester.id,
            owner_user_id=resolution.owner_user_id,
            comment=_clean_text(comment, MAX_COMMENT_CHARS),
            status=IMPROVEMENT_STATUS_NEW,
            kind=IMPROVEMENT_KIND_SESSION,
            source=source,
            snapshot=snapshot,
            context=context,
            snapshot_message_count=message_count,
            snapshot_truncated=truncated,
        )
        db.add(request)
        db.commit()
        db.refresh(request)

        await ImprovementRequestService._emit(
            EventType.IMPROVEMENT_REQUEST_CREATED, request
        )
        return request

    # ── Agent-API capability requests (plan §4.3) ─────────────────────

    @staticmethod
    def _check_agent_api_rate_limits(
        db: DBSession, requester_id: uuid.UUID, target_id: uuid.UUID
    ) -> None:
        """Enforce D9: per caller per producer, and per caller overall.

        A soft limit by design: count-then-insert is not atomic, so concurrent
        requests from one caller can overshoot by the number in flight. That
        is accepted — the limit bounds inbox noise, it is not a security
        boundary — and avoids a lock on the hot path.

        Raises:
            ImprovementRequestDenied: 429 with ``retry_after`` = seconds until
                the oldest request in the window ages out.
        """
        since = datetime.now(UTC) - RATE_LIMIT_WINDOW
        base = [
            AgentImprovementRequest.requester_user_id == requester_id,
            AgentImprovementRequest.kind == IMPROVEMENT_KIND_AGENT_API,
            AgentImprovementRequest.created_at >= since,
        ]
        checks = (
            (
                [*base, AgentImprovementRequest.target_agent_id == target_id],
                MAX_AGENT_API_REQUESTS_PER_PRODUCER,
                f"You have filed {MAX_AGENT_API_REQUESTS_PER_PRODUCER} capability "
                "requests to this API in the last 24 hours, which is the limit.",
            ),
            (
                base,
                MAX_AGENT_API_REQUESTS_PER_CALLER,
                f"You have filed {MAX_AGENT_API_REQUESTS_PER_CALLER} capability "
                "requests in the last 24 hours, which is the limit.",
            ),
        )
        for conditions, limit, message in checks:
            count, oldest = db.exec(
                select(
                    func.count(), func.min(AgentImprovementRequest.created_at)
                ).where(*conditions)
            ).one()
            if (count or 0) < limit:
                continue
            retry_after = 1
            if oldest is not None:
                if oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=UTC)
                remaining = oldest + RATE_LIMIT_WINDOW - datetime.now(UTC)
                retry_after = max(1, int(remaining.total_seconds()) + 1)
            raise ImprovementRequestDenied(
                REASON_RATE_LIMITED,
                f"{message} Try again later.",
                status_code=429,
                retry_after=retry_after,
            )

    @staticmethod
    async def create_from_agent_api(
        db: DBSession,
        *,
        producer: Agent,
        token: AgentApiToken,
        caller: User,
        consumer_agent_id: uuid.UUID | None,
        body: AgentApiCapabilityRequestCreate,
        raw_secrets: set[str],
    ) -> AgentImprovementRequest:
        """File a capability request against ``producer`` on behalf of ``caller``.

        The caller is already authenticated (valid token for ``producer``) and
        identified by the route. The consumer agent is attribution only (D12):
        it is recorded only when it exists and ``caller`` owns it.

        Never touches the producer's environment.

        Raises:
            ImprovementRequestDenied: 429 when a D9 rate limit is hit.
        """
        ImprovementRequestService._check_agent_api_rate_limits(
            db, caller.id, producer.id
        )

        consumer_agent: Agent | None = None
        if consumer_agent_id is not None:
            candidate = db.get(Agent, consumer_agent_id)
            if candidate is not None and candidate.owner_id == caller.id:
                consumer_agent = candidate

        context: dict = {
            "api_request": body.model_dump(mode="json", by_alias=True),
            "consumer": {
                "agent_id": str(consumer_agent.id) if consumer_agent else None,
                "agent_name": consumer_agent.name if consumer_agent else None,
                "token_kind": token.kind,
                "token_label": token.label,
            },
            "producer": {
                "agent_id": str(producer.id),
                "agent_name": producer.name,
                "api_version_seen": body.spec_version_seen,
            },
        }

        # The whole payload is caller-authored, so every string is scrubbed —
        # not just the transcript keys a session snapshot uses.
        secrets = {s for s in raw_secrets if s}
        if consumer_agent is not None:
            secrets |= ImprovementRequestService._collect_secrets(db, consumer_agent)
        context, hits = secret_scrubber.scrub(context, secrets, all_strings=True)
        context["platform"] = {"scrubbed_hits": hits}

        api_request = context["api_request"]
        request = AgentImprovementRequest(
            session_id=None,
            source_agent_id=consumer_agent.id if consumer_agent else None,
            target_agent_id=producer.id,
            bundle_uuid=None,
            requester_user_id=caller.id,
            owner_user_id=producer.owner_id,
            # Mirrors ``need`` so list/CLI consumers that read ``comment`` work.
            comment=_clean_text(api_request.get("need"), MAX_COMMENT_CHARS),
            status=IMPROVEMENT_STATUS_NEW,
            kind=IMPROVEMENT_KIND_AGENT_API,
            source=IMPROVEMENT_SOURCE_AGENT_API,
            snapshot={},
            context=context,
            snapshot_message_count=0,
            snapshot_truncated=False,
        )
        db.add(request)
        db.commit()
        db.refresh(request)

        await ImprovementRequestService._emit(
            EventType.IMPROVEMENT_REQUEST_CREATED, request
        )
        return request

    @staticmethod
    def list_for_requester_on_target(
        db: DBSession,
        requester_id: uuid.UUID,
        target_id: uuid.UUID,
        kind: str = IMPROVEMENT_KIND_AGENT_API,
    ) -> tuple[list[AgentImprovementRequest], int]:
        """The caller's own requests on one producer, newest first (≤ 50)."""
        conditions = [
            AgentImprovementRequest.requester_user_id == requester_id,
            AgentImprovementRequest.target_agent_id == target_id,
            AgentImprovementRequest.kind == kind,
        ]
        count = (
            db.exec(
                select(func.count())
                .select_from(AgentImprovementRequest)
                .where(*conditions)
            ).one()
            or 0
        )
        rows = list(
            db.exec(
                select(AgentImprovementRequest)
                .where(*conditions)
                .order_by(AgentImprovementRequest.created_at.desc())  # type: ignore[attr-defined]
                .limit(MAX_AGENT_API_REQUESTER_LIST)
            ).all()
        )
        return rows, count

    @staticmethod
    def to_capability_public(
        row: AgentImprovementRequest,
    ) -> AgentApiCapabilityRequestPublic:
        """The consumer's projection: status and the owner's note, nothing else."""
        api_request = _api_request(row)
        return AgentApiCapabilityRequestPublic(
            id=row.id,
            title=api_request.get("title"),
            status=row.status,
            resolution_note=row.resolution_note,
            created_at=row.created_at,
            status_changed_at=row.status_changed_at,
            spec_version_seen=api_request.get("spec_version_seen"),
        )

    # ── Producer env tools (plan §4.5) ───────────────────────────────

    @staticmethod
    def list_agent_api_for_target(
        db: DBSession,
        target_id: uuid.UUID,
        status: str | None = None,
        limit: int = 20,
    ) -> tuple[list[AgentApiRequestToolRow], int]:
        """Capability requests received on one producer, for its building agent."""
        conditions = [
            AgentImprovementRequest.target_agent_id == target_id,
            AgentImprovementRequest.kind == IMPROVEMENT_KIND_AGENT_API,
        ]
        if status:
            conditions.append(AgentImprovementRequest.status == status)
        count = (
            db.exec(
                select(func.count())
                .select_from(AgentImprovementRequest)
                .where(*conditions)
            ).one()
            or 0
        )
        rows = list(
            db.exec(
                select(AgentImprovementRequest)
                .where(*conditions)
                .order_by(
                    case(
                        (AgentImprovementRequest.status == IMPROVEMENT_STATUS_NEW, 0),
                        else_=1,
                    ),
                    AgentImprovementRequest.created_at.desc(),  # type: ignore[attr-defined]
                )
                .limit(limit)
            ).all()
        )
        users_by_id = {
            u.id: u
            for u in db.exec(
                select(User).where(
                    User.id.in_({r.requester_user_id for r in rows})  # type: ignore[attr-defined]
                )
            ).all()
        } if rows else {}
        return [
            ImprovementRequestService._to_tool_row(
                row, users_by_id.get(row.requester_user_id)
            )
            for row in rows
        ], count

    @staticmethod
    def get_agent_api_for_target(
        db: DBSession, request_id: uuid.UUID, target_id: uuid.UUID
    ) -> AgentImprovementRequest | None:
        """A capability request on this producer, or None (never another's)."""
        row = db.get(AgentImprovementRequest, request_id)
        if (
            row is None
            or row.target_agent_id != target_id
            or row.kind != IMPROVEMENT_KIND_AGENT_API
        ):
            return None
        return row

    @staticmethod
    def to_tool_detail(
        db: DBSession, row: AgentImprovementRequest
    ) -> AgentApiRequestToolDetail:
        api_request = _api_request(row)
        base = ImprovementRequestService._to_tool_row(
            row, db.get(User, row.requester_user_id)
        )
        return AgentApiRequestToolDetail(
            **base.model_dump(),
            need=api_request.get("need"),
            proposed_endpoint=api_request.get("proposed_endpoint"),
            example_request=api_request.get("example_request"),
            example_response=api_request.get("example_response"),
            resolution_note=row.resolution_note,
            status_changed_at=row.status_changed_at,
        )

    @staticmethod
    def _to_tool_row(
        row: AgentImprovementRequest, requester: User | None
    ) -> AgentApiRequestToolRow:
        api_request = _api_request(row)
        consumer = (row.context or {}).get("consumer") or {}
        return AgentApiRequestToolRow(
            id=row.id,
            title=api_request.get("title"),
            status=row.status,
            requester_display=display_name_for_user(requester),
            consumer_agent_name=consumer.get("agent_name"),
            created_at=row.created_at,
            spec_version_seen=api_request.get("spec_version_seen"),
        )

    @staticmethod
    def _collect_secrets(db: DBSession, source_agent: Agent) -> set[str]:
        """Secret values that must never appear in the shared transcript.

        The source install's linked credentials (filtered through the shared
        ``CredentialsService.SENSITIVE_FIELDS`` map) plus the AI credentials its
        environment is wired to. Best-effort: an unreadable credential narrows
        the scrub but must not block the request.
        """
        secrets: set[str] = set()
        try:
            credentials = CredentialsService.get_agent_credentials_with_data(
                session=db, agent_id=source_agent.id
            )
            secrets |= secret_scrubber.collect_credential_secrets(credentials)
        except Exception as e:  # noqa: BLE001
            # Only the exception *type* is logged, never the exception itself:
            # a pydantic ``ValidationError`` renders the offending ``input_value``
            # inline, which for a malformed credential row is the secret (§4.2).
            logger.warning(
                "Improvement scrub: credential collection failed: %s",
                type(e).__name__,
            )

        try:
            from app.models.credentials.ai_credential import AICredential
            from app.models.environments.environment import AgentEnvironment
            from app.services.credentials.ai_credentials_service import (
                ai_credentials_service,
            )

            environments = db.exec(
                select(AgentEnvironment).where(
                    AgentEnvironment.agent_id == source_agent.id
                )
            ).all()
            ai_credential_ids = {
                cred_id
                for env in environments
                for cred_id in (
                    env.conversation_ai_credential_id,
                    env.building_ai_credential_id,
                )
                if cred_id is not None
            }
            for cred_id in ai_credential_ids:
                ai_credential = db.get(AICredential, cred_id)
                if ai_credential is None:
                    continue
                data = ai_credentials_service.decrypt_credential(ai_credential)
                if data.api_key:
                    secrets.add(data.api_key)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Improvement scrub: AI credential collection failed: %s",
                type(e).__name__,
            )

        return secrets

    # ── Preview ──────────────────────────────────────────────────────

    @staticmethod
    def build_context_preview(
        db: DBSession, session: ChatSession, user: User
    ) -> ImprovementContextPublic:
        """The consent modal's pre-flight payload. Writes nothing."""
        source_agent, message_count, denial = (
            ImprovementRequestService._evaluate_eligibility(db, session, user)
        )
        existing_request_count = (
            db.exec(
                select(func.count())
                .select_from(AgentImprovementRequest)
                .where(AgentImprovementRequest.session_id == session.id)
            ).one()
            or 0
        )
        if denial is not None or source_agent is None:
            return ImprovementContextPublic(
                eligible=False,
                reason=denial.reason if denial else REASON_NOT_ELIGIBLE,
                message_count=message_count,
                existing_request_count=existing_request_count,
            )

        resolution = ImprovementRequestService.resolve_target(db, source_agent)
        owner = db.get(User, resolution.owner_user_id)
        installed_version = ImprovementRequestService._installed_version(
            db, source_agent
        )
        return ImprovementContextPublic(
            eligible=True,
            reason=None,
            is_shared_externally=resolution.owner_user_id != user.id,
            recipient_display=display_name_for_user(owner),
            target_agent_name=resolution.target_agent.name,
            bundle_id=(
                source_agent.bundle_id if source_agent.bundle_uuid else None
            ),
            installed_version=installed_version,
            message_count=message_count,
            existing_request_count=existing_request_count,
        )

    @staticmethod
    def _installed_version(db: DBSession, agent: Agent) -> str | None:
        """The install's bundle version label, best-effort."""
        if not agent.installed_revision_id:
            return None
        try:
            from app.models.bundles.agent_bundle_revision import AgentBundleRevision

            revision = db.get(AgentBundleRevision, agent.installed_revision_id)
            return revision.version if revision else None
        except Exception as e:  # noqa: BLE001
            logger.warning("Improvement preview: revision lookup failed: %s", e)
            return None

    # ── Listing ──────────────────────────────────────────────────────

    @staticmethod
    def list_for_agent(
        db: DBSession,
        agent_id: uuid.UUID,
        owner: User,
        status: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[ImprovementRequestPublic], int]:
        """Requests received on one agent the caller owns.

        An agent the caller does not own is indistinguishable from one that does
        not exist — 404, not 403 (plan §4.3).
        """
        agent = db.get(Agent, agent_id)
        if agent is None or agent.owner_id != owner.id:
            raise ImprovementRequestDenied(
                REASON_NOT_FOUND, "Agent not found.", status_code=404
            )
        return ImprovementRequestService._list(
            db,
            [AgentImprovementRequest.target_agent_id == agent_id],
            status=status,
            skip=skip,
            limit=limit,
        )

    @staticmethod
    def list_for_owner(
        db: DBSession,
        owner: User,
        status: str | None = None,
        agent_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[ImprovementRequestPublic], int]:
        """Cross-agent view of everything the account user receives (CLI)."""
        conditions = [AgentImprovementRequest.owner_user_id == owner.id]
        if agent_id is not None:
            conditions.append(AgentImprovementRequest.target_agent_id == agent_id)
        return ImprovementRequestService._list(
            db, conditions, status=status, skip=skip, limit=limit
        )

    @staticmethod
    def list_for_requester(
        db: DBSession,
        user: User,
        status: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[ImprovementRequestPublic], int]:
        """The caller's own submitted requests."""
        return ImprovementRequestService._list(
            db,
            [AgentImprovementRequest.requester_user_id == user.id],
            status=status,
            skip=skip,
            limit=limit,
        )

    @staticmethod
    def _list(
        db: DBSession,
        conditions: list,
        status: str | None,
        skip: int,
        limit: int,
    ) -> tuple[list[ImprovementRequestPublic], int]:
        if status:
            conditions = [*conditions, AgentImprovementRequest.status == status]

        count = (
            db.exec(
                select(func.count())
                .select_from(AgentImprovementRequest)
                .where(*conditions)
            ).one()
            or 0
        )
        # Unhandled work first, then newest-first within each group. The sort
        # has to live here rather than in a client: every one of these lists is
        # paginated server-side, so reordering a page in the UI would produce a
        # wrong global order the moment there is more than one page. Keeping it
        # in the single shared ``_list`` is also what stops the web card and
        # ``cinna improve list`` from disagreeing.
        #
        # When the caller filters on a status the CASE is constant and this
        # collapses to plain ``created_at DESC`` — no cost for the card's
        # default New view.
        rows = list(
            db.exec(
                select(AgentImprovementRequest)
                .where(*conditions)
                .order_by(
                    case(
                        (AgentImprovementRequest.status == IMPROVEMENT_STATUS_NEW, 0),
                        else_=1,
                    ),
                    AgentImprovementRequest.created_at.desc(),  # type: ignore[attr-defined]
                )
                .offset(skip)
                .limit(limit)
            ).all()
        )
        return ImprovementRequestService.project_many(db, rows), count

    @staticmethod
    def project_many(
        db: DBSession, rows: list[AgentImprovementRequest]
    ) -> list[ImprovementRequestPublic]:
        """Project rows for a list surface with **batched** lookups.

        Agent names and requester identities are resolved with two ``IN``
        queries and indexed by id — never a per-row ``db.get``, which is the N+1
        that already bit the user-search assignment projection. Bundle id and
        installed version come from the row's own frozen ``context``, so they
        cost nothing and stay true to what was captured.
        """
        if not rows:
            return []

        agent_ids = {r.target_agent_id for r in rows} | {
            r.source_agent_id for r in rows if r.source_agent_id
        }
        agents_by_id: dict[uuid.UUID, Agent] = {}
        if agent_ids:
            agents_by_id = {
                a.id: a
                for a in db.exec(
                    select(Agent).where(Agent.id.in_(agent_ids))  # type: ignore[attr-defined]
                ).all()
            }

        user_ids = {r.requester_user_id for r in rows}
        users_by_id = {
            u.id: u
            for u in db.exec(
                select(User).where(User.id.in_(user_ids))  # type: ignore[attr-defined]
            ).all()
        }

        return [
            ImprovementRequestService._to_public(row, agents_by_id, users_by_id)
            for row in rows
        ]

    @staticmethod
    def _to_public(
        row: AgentImprovementRequest,
        agents_by_id: dict[uuid.UUID, Agent],
        users_by_id: dict[uuid.UUID, User],
    ) -> ImprovementRequestPublic:
        agent_context = (row.context or {}).get("agent") or {}
        target = agents_by_id.get(row.target_agent_id)
        source = (
            agents_by_id.get(row.source_agent_id) if row.source_agent_id else None
        )
        requester = users_by_id.get(row.requester_user_id)
        is_agent_api = row.kind == IMPROVEMENT_KIND_AGENT_API
        return ImprovementRequestPublic(
            id=row.id,
            kind=row.kind,
            title=_api_request(row).get("title") if is_agent_api else None,
            session_id=row.session_id,
            target_agent_id=row.target_agent_id,
            target_agent_name=target.name if target else None,
            source_agent_id=row.source_agent_id,
            source_agent_name=source.name if source else None,
            bundle_id=agent_context.get("bundle_id"),
            # Prefer what was captured; a request written before the context
            # carried the flag still has ``bundle_uuid`` on the row itself.
            is_bundle_install=bool(
                agent_context.get("is_bundle_install")
                if agent_context.get("is_bundle_install") is not None
                else row.bundle_uuid
            ),
            installed_version=agent_context.get("installed_version"),
            installed_revision_number=agent_context.get("installed_revision_number"),
            requester_display=display_name_for_user(requester),
            requester_email=requester.email if requester else None,
            comment=row.comment,
            status=row.status,
            resolution_note=row.resolution_note,
            source=row.source,
            snapshot_message_count=row.snapshot_message_count,
            snapshot_truncated=row.snapshot_truncated,
            created_at=row.created_at,
            status_changed_at=row.status_changed_at,
        )

    @staticmethod
    def to_detail_public(
        db: DBSession, row: AgentImprovementRequest
    ) -> ImprovementRequestDetailPublic:
        """Detail projection — the list fields plus the frozen context block."""
        base = ImprovementRequestService.project_many(db, [row])[0]
        snapshot_session = (row.snapshot or {}).get("session") or {}
        return ImprovementRequestDetailPublic(
            **base.model_dump(),
            context=row.context or {},
            session_title=(
                None
                if row.kind == IMPROVEMENT_KIND_AGENT_API
                else snapshot_session.get("title")
            ),
        )

    # ── Single-row access ────────────────────────────────────────────

    @staticmethod
    def get_authorized(
        db: DBSession, request_id: uuid.UUID, user: User
    ) -> tuple[AgentImprovementRequest, str]:
        """Fetch a request the caller may see, with their role on it.

        Anything the caller is not party to raises 404 — matching
        ``assert_can_build``'s existence-leak-safe convention. A 403 here would
        confirm that a given request id exists.
        """
        row = db.get(AgentImprovementRequest, request_id)
        if row is None:
            raise ImprovementRequestService._not_found()
        if row.owner_user_id == user.id:
            return row, ROLE_OWNER
        if row.requester_user_id == user.id:
            return row, ROLE_REQUESTER
        raise ImprovementRequestService._not_found()

    @staticmethod
    def _not_found() -> ImprovementRequestDenied:
        return ImprovementRequestDenied(
            REASON_NOT_FOUND, "Improvement request not found.", status_code=404
        )

    # ── Mutations (recipient only) ───────────────────────────────────

    @staticmethod
    async def update_status(
        db: DBSession,
        request: AgentImprovementRequest,
        owner: User,
        status: str | None = None,
        note: str | None = None,
    ) -> AgentImprovementRequest:
        """Set the status and/or resolution note. Recipient only.

        Last write wins — a single-owner surface does not need optimistic
        locking; ``status_changed_at`` simply reflects the last transition.
        """
        ImprovementRequestService._assert_owner(request, owner)

        if status is not None:
            if status not in IMPROVEMENT_STATUSES:
                raise ImprovementRequestDenied(
                    REASON_INVALID_STATUS,
                    f"Status must be one of: {', '.join(IMPROVEMENT_STATUSES)}.",
                )
            if status != request.status:
                request.status_changed_at = datetime.now(UTC)
            request.status = status

        if note is not None:
            request.resolution_note = _clean_text(note, MAX_RESOLUTION_NOTE_CHARS)

        request.updated_at = datetime.now(UTC)
        db.add(request)
        db.commit()
        db.refresh(request)

        await ImprovementRequestService._emit(
            EventType.IMPROVEMENT_REQUEST_UPDATED, request
        )
        return request

    @staticmethod
    def delete(
        db: DBSession, request: AgentImprovementRequest, owner: User
    ) -> None:
        """Delete a request. Recipient only — the requester cannot withdraw."""
        ImprovementRequestService._assert_owner(request, owner)
        db.delete(request)
        db.commit()

    @staticmethod
    def _assert_owner(request: AgentImprovementRequest, user: User) -> None:
        """Only the recipient may mutate. Requesters get 403, strangers 404.

        The distinction is deliberate: ``get_authorized`` has already confirmed
        the requester is party to this row, so refusing them with 403 leaks
        nothing they did not already know.
        """
        if request.owner_user_id == user.id:
            return
        if request.requester_user_id == user.id:
            raise ImprovementRequestDenied(
                REASON_NOT_OWNER,
                "Only the agent's owner can change an improvement request.",
                status_code=403,
            )
        raise ImprovementRequestService._not_found()

    # ── Archive ──────────────────────────────────────────────────────

    @staticmethod
    def build_archive(
        db: DBSession, request: AgentImprovementRequest
    ) -> tuple[bytes, str]:
        """Assemble the projections and build the ZIP in memory.

        Returns ``(zip_bytes, filename)``. Shared by the web and CLI archive
        routes so the two cannot drift.
        """
        requester = db.get(User, request.requester_user_id)
        target = db.get(Agent, request.target_agent_id)
        source = (
            db.get(Agent, request.source_agent_id) if request.source_agent_id else None
        )
        snapshot_session = (request.snapshot or {}).get("session") or {}
        payload = ImprovementArchiveService.build(
            request,
            {
                "display": display_name_for_user(requester),
                "email": requester.email if requester else None,
            },
            {
                "agent_name": target.name if target else None,
                "owner_display": display_name_for_user(
                    db.get(User, request.owner_user_id)
                ),
                "session_title": snapshot_session.get("title"),
                "source_agent_name": source.name if source else None,
            },
        )
        return payload, archive_filename(request)

    # ── Events ───────────────────────────────────────────────────────

    @staticmethod
    async def _emit(event_type: str, request: AgentImprovementRequest) -> None:
        """Notify the RECIPIENT's user room. Never fails the caller."""
        try:
            from app.services.events.event_service import event_service

            await event_service.emit_event(
                event_type=event_type,
                model_id=request.id,
                user_id=request.owner_user_id,
                meta={
                    "request_id": str(request.id),
                    "target_agent_id": str(request.target_agent_id),
                    "source_agent_id": (
                        str(request.source_agent_id)
                        if request.source_agent_id
                        else None
                    ),
                    "bundle_uuid": (
                        str(request.bundle_uuid) if request.bundle_uuid else None
                    ),
                    "status": request.status,
                    "kind": request.kind,
                },
            )
        except Exception as e:  # noqa: BLE001 — the row is already committed
            logger.warning("Failed to emit %s for request %s: %s", event_type, request.id, e)


def _api_request(row: AgentImprovementRequest) -> dict:
    """The capability-request payload of an ``agent_api`` row ({} otherwise)."""
    payload = (row.context or {}).get("api_request")
    return payload if isinstance(payload, dict) else {}


def _clean_text(value: str | None, limit: int) -> str | None:
    """Strip and cap free text; empty becomes NULL."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:limit]
