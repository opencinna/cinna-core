"""SessionSnapshotService — freeze a session and its runtime context.

Two independent captures, both taken once at consent time and never refreshed:

* :meth:`capture` — the transcript (plan §3.2). Messages, a compact per-message
  tool digest derived from ``streaming_events``, and attachment *descriptors*.
  File bytes are never copied: a publisher does not get the consumer's uploads.
* :meth:`capture_context` — the tuning-relevant runtime context (plan §3.3):
  agent / bundle revision, environment, SDK + effective model, plugins,
  **prompts**, recipient, platform.
* :meth:`capture_memory` — the per-install personal memory area (plan §3.3).
  Async and separate from :meth:`capture_context` because it is the one block
  that does not live in the database: ``app-data/memory/*.md`` exists only
  inside the container, so it has to be read over the env HTTP channel.

Every optional lookup in :meth:`capture_context` is best-effort. A deleted
environment, an unreadable plugin manifest, or a missing bundle revision yields
``null`` / ``[]`` for that block and never aborts the request (plan §9). The
same holds for :meth:`capture_memory`: a stopped container records
``available: false`` with a reason, never an error.
"""
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, UTC
from typing import Any

from sqlalchemy import func
from sqlmodel import Session as DBSession, select

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle_revision import AgentBundleRevision
from app.models.environments.environment import AgentEnvironment
from app.models.files.file_upload import FileUpload, MessageFile
from app.models.sessions.session import Session as ChatSession, SessionMessage

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA_VERSION = 1
CONTEXT_SCHEMA_VERSION = 2

# ── Prompts + memory caps (plan §3.3) ────────────────────────────────
# Per-prompt-field text cap. Generous: the prompt IS the artefact under review,
# so truncating it defeats the point — this only guards against a pathological
# document.
MAX_PROMPT_TEXT_CHARS = 40_000
# Personal-memory caps. The total mirrors ``PERSONAL_MEMORY_MAX_CHARS`` in
# ``app_core_base/core/server/prompt_generator.py``: capturing more than the
# runtime can inject would show the recipient text that never reached the
# system prompt.
MAX_MEMORY_TOTAL_CHARS = 20_000
MAX_MEMORY_FILES = 20
# Container path of the per-install personal memory area. Matches
# ``prompt_generator._load_personal_memory`` (``workspace_dir/app-data/memory``)
# with the container's fixed workspace root.
MEMORY_DIR = "/app/workspace/app-data/memory"
# Seconds allowed for the one exec that reads the memory area. Short on
# purpose: this runs inline on the submit request, and a slow container must
# degrade to "unavailable", never stall the consent action.
MEMORY_READ_TIMEOUT = 15

# ``context.memory.unavailable_reason`` vocabulary. Stable codes — the archive
# README and the detail modal both render copy off them.
MEMORY_REASON_DECLINED = "declined_by_requester"
MEMORY_REASON_NO_ENVIRONMENT = "no_environment"
MEMORY_REASON_ENV_NOT_RUNNING = "env_not_running"
MEMORY_REASON_READ_FAILED = "read_failed"
MEMORY_REASON_EMPTY = "empty"

# The four prompt documents that shape a run, mapped to their ``Agent`` /
# ``AgentBundleRevision`` column names. The same attribute name exists on both
# models, which is what makes the divergence comparison a one-liner.
_PROMPT_FIELDS: tuple[tuple[str, str], ...] = (
    ("workflow", "workflow_prompt"),
    ("entrypoint", "entrypoint_prompt"),
    ("refiner", "refiner_prompt"),
    ("router_trigger", "router_trigger_prompt"),
)

# ── Capture caps (plan §3.2) ─────────────────────────────────────────
# Per-message content cap; the tail is dropped with an explicit marker.
MAX_MESSAGE_CONTENT_CHARS = 50_000
TRUNCATION_MARKER = "\n\n…[truncated]"
# Tool digest: entries per message (the NEWEST are kept), and the length of
# each `brief`.
MAX_TOOL_DIGEST_ENTRIES = 200
MAX_TOOL_BRIEF_CHARS = 500
# Total serialized snapshot cap. When exceeded, the OLDEST messages are dropped
# first — defects cluster at the end of a conversation.
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
# Rows fetched per page while walking the transcript newest-first. Bounds peak
# memory to the surviving budget plus one page instead of the whole session.
MESSAGE_PAGE_SIZE = 100

# ``message_metadata.streaming_events`` is never copied verbatim: it is the
# largest field in the DB and mostly redundant with `content`. Instead the
# events that actually explain "what went wrong" are distilled into a digest.
# Left: the event `type` the runtime emits. Right: the stable digest type in
# the frozen snapshot schema (kept decoupled so a runtime rename does not
# silently change the archive's contract).
_DIGEST_EVENT_TYPES: dict[str, str] = {
    "tool": "tool_use",
    "tool_result_delta": "tool_result",
    "thinking": "thinking",
    "error": "error",
}

# Synthetic digest type for the marker entry that stands in for the older
# events ``MAX_TOOL_DIGEST_ENTRIES`` dropped. Never emitted by the runtime.
DIGEST_OMISSION_TYPE = "omitted"


def _truncate(text: str | None, limit: int, marker: str = "") -> str:
    """Tail-truncate ``text`` to ``limit`` chars, appending ``marker``."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + marker


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class SessionSnapshotService:
    """Builds the two immutable JSON blocks stored on an improvement request."""

    # ── Transcript ───────────────────────────────────────────────────

    @staticmethod
    def capture(db: DBSession, session: ChatSession) -> tuple[dict, bool, int]:
        """Freeze the session transcript.

        Returns:
            ``(snapshot, truncated, message_count)`` where ``message_count`` is
            the number of messages that survived the caps (the list-projection
            field), and ``truncated`` is True when the total cap dropped any.
        """
        total_message_count = db.exec(
            select(func.count())
            .select_from(SessionMessage)
            .where(SessionMessage.session_id == session.id)
        ).one()

        envelope = SessionSnapshotService._envelope(session, total_message_count)
        kept = SessionSnapshotService._collect_capped_entries(db, session, envelope)
        # Clamped: a message appended between the count and the walk would make
        # the difference negative, and "-1 omitted" is worse than "0".
        omitted = max(total_message_count - len(kept), 0)

        snapshot = {
            **envelope,
            "messages": kept,
            "truncated": omitted > 0,
            "omitted_message_count": omitted,
        }
        return snapshot, omitted > 0, len(kept)

    @staticmethod
    def _envelope(session: ChatSession, total_message_count: int) -> dict:
        """The snapshot fields that surround the message list."""
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "captured_at": datetime.now(UTC).isoformat(),
            "session": {
                "id": str(session.id),
                "title": session.title,
                "mode": session.mode,
                "status": session.status,
                "result_state": session.result_state,
                "result_summary": session.result_summary,
                "integration_type": session.integration_type,
                "created_at": _iso(session.created_at),
                "last_message_at": _iso(session.last_message_at),
                "total_message_count": total_message_count,
            },
        }

    @staticmethod
    def _load_attachments(
        db: DBSession, message_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[dict]]:
        """Batch-load attachment **descriptors** for a set of messages.

        Metadata columns only — filename, mime type, size. File bytes never
        enter the snapshot or the archive.
        """
        if not message_ids:
            return {}
        by_message: dict[uuid.UUID, list[dict]] = {}
        try:
            rows = db.exec(
                select(MessageFile.message_id, FileUpload)
                .join(FileUpload, MessageFile.file_id == FileUpload.id)
                .where(MessageFile.message_id.in_(message_ids))  # type: ignore[attr-defined]
            ).all()
            for message_id, upload in rows:
                by_message.setdefault(message_id, []).append(
                    {
                        "filename": upload.filename,
                        "mime_type": upload.mime_type,
                        "size": upload.file_size,
                    }
                )
        except Exception as e:  # noqa: BLE001 — descriptors are best-effort
            logger.warning("Improvement snapshot: attachment load failed: %s", e)
        return by_message

    @staticmethod
    def _message_entry(message: SessionMessage, attachments: list[dict]) -> dict:
        """Project one message into its snapshot entry."""
        metadata = message.message_metadata or {}
        entry: dict[str, Any] = {
            "sequence_number": message.sequence_number,
            "role": message.role,
            "content": _truncate(
                message.content, MAX_MESSAGE_CONTENT_CHARS, TRUNCATION_MARKER
            ),
            "timestamp": _iso(message.timestamp),
            "status": message.status or "completed",
            "is_command": bool(metadata.get("command")),
            "command_name": metadata.get("command_name"),
        }
        if attachments:
            entry["attachments"] = attachments
        if message.role != "user":
            digest = SessionSnapshotService._tool_digest(
                metadata.get("streaming_events") or []
            )
            if digest:
                entry["tool_digest"] = digest
        return entry

    @staticmethod
    def _tool_digest(streaming_events: list) -> list[dict]:
        """Distil ``streaming_events`` into a compact, capped digest.

        Scans **newest-first** so the cap keeps the *last*
        ``MAX_TOOL_DIGEST_ENTRIES`` events — a turn that fails after hundreds of
        tool calls must ship the calls around the failure, not the setup work
        that preceded it. Same reasoning as :meth:`_collect_capped_entries`.
        Dropped older events are recorded by a leading marker entry.
        """
        digest: list[dict] = []
        omitted = 0
        for event in reversed(streaming_events):
            if not isinstance(event, dict):
                continue
            digest_type = _DIGEST_EVENT_TYPES.get(event.get("type") or "")
            if digest_type is None:
                continue
            if len(digest) >= MAX_TOOL_DIGEST_ENTRIES:
                omitted += 1
                continue
            event_metadata = event.get("metadata") or {}
            tool_name = event.get("tool_name") or event_metadata.get("tool_name")
            brief = event.get("content") or ""
            if not brief and digest_type == "tool_use":
                # A tool call with no textual content — summarise its input so
                # the digest still says what the agent tried to do.
                tool_input = event_metadata.get("tool_input")
                if tool_input is not None:
                    try:
                        brief = json.dumps(tool_input, default=str)
                    except (TypeError, ValueError):
                        brief = str(tool_input)
            digest.append(
                {
                    "seq": event.get("event_seq"),
                    "type": digest_type,
                    "tool_name": tool_name,
                    "brief": _truncate(
                        str(brief), MAX_TOOL_BRIEF_CHARS, "…(truncated)"
                    ),
                }
            )
        digest.reverse()
        if omitted:
            digest.insert(
                0,
                {
                    "seq": None,
                    "type": DIGEST_OMISSION_TYPE,
                    "tool_name": None,
                    "brief": f"…{omitted} earlier tool event(s) omitted",
                },
            )
        return digest

    @staticmethod
    def _collect_capped_entries(
        db: DBSession, session: ChatSession, envelope: dict
    ) -> list[dict]:
        """Build the message entries that fit the total cap, newest-first.

        Messages are paged **newest-first** and the walk stops the moment the
        byte budget is spent, so the oldest messages are never loaded at all.
        This matters because ``message_metadata.streaming_events`` is the
        largest field in the DB and the cap discards most of it: materialising
        the whole session first would make peak memory the session size rather
        than the 2 MB that survives — on a synchronous handler holding a pooled
        connection, at up to 5 captures per session.

        Paging is keyset (``sequence_number <`` the last row seen), not
        ``OFFSET``, so a message appended while the capture runs cannot shift
        the window and duplicate or skip a row. Returned in ascending
        ``sequence_number`` order.
        """
        # Reserve room for the envelope plus the `messages` / `truncated` /
        # `omitted_message_count` scaffolding.
        overhead = len(json.dumps(envelope, default=str).encode()) + 128
        budget = MAX_SNAPSHOT_BYTES - overhead
        if budget <= 0:
            return []

        kept_reversed: list[dict] = []
        used = 0
        cursor: int | None = None
        while True:
            query = select(SessionMessage).where(
                SessionMessage.session_id == session.id
            )
            if cursor is not None:
                query = query.where(SessionMessage.sequence_number < cursor)
            messages = list(
                db.exec(
                    query.order_by(SessionMessage.sequence_number.desc())  # type: ignore[attr-defined]
                    .limit(MESSAGE_PAGE_SIZE)
                ).all()
            )
            if not messages:
                return list(reversed(kept_reversed))
            cursor = messages[-1].sequence_number

            attachments_by_message = SessionSnapshotService._load_attachments(
                db, [m.id for m in messages]
            )
            for message in messages:
                entry = SessionSnapshotService._message_entry(
                    message, attachments_by_message.get(message.id, [])
                )
                # +1 for the separating comma in the serialized array.
                size = len(json.dumps(entry, default=str).encode()) + 1
                if used + size > budget:
                    return list(reversed(kept_reversed))
                used += size
                kept_reversed.append(entry)

            if len(messages) < MESSAGE_PAGE_SIZE:
                return list(reversed(kept_reversed))

    # ── Runtime context ──────────────────────────────────────────────

    @staticmethod
    def capture_context(
        db: DBSession,
        session: ChatSession,
        source_agent: Agent,
        resolution: Any,
    ) -> dict:
        """Freeze the runtime context (plan §3.3).

        Args:
            resolution: the ``TargetResolution`` from
                ``ImprovementRequestService.resolve_target`` — carries the
                target agent, recipient user id, bundle, and fallback reason.

        Every block is independently guarded: a failure records ``null`` / ``[]``
        for that block and the request still goes through.

        The ``memory`` block is **not** produced here — it is the one part of
        the run context that never reaches the database, so it needs an async
        container read. Callers add it via :meth:`capture_memory`.
        """
        environment = SessionSnapshotService._resolve_environment(
            db, session, source_agent
        )
        return {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "agent": SessionSnapshotService._agent_block(db, source_agent, resolution),
            "environment": SessionSnapshotService._environment_block(environment),
            "sdk": SessionSnapshotService._sdk_block(
                db, session, source_agent, environment
            ),
            "plugins": SessionSnapshotService._plugins_block(db, source_agent),
            "prompts": SessionSnapshotService._prompts_block(db, source_agent),
            "recipient": SessionSnapshotService._recipient_block(db, session, resolution),
            "platform": {
                "captured_at": datetime.now(UTC).isoformat(),
                "frontend_host": settings.FRONTEND_HOST,
            },
        }

    @staticmethod
    def _resolve_environment(
        db: DBSession, session: ChatSession, source_agent: Agent
    ) -> AgentEnvironment | None:
        """The environment the session ran on, falling back to the active one."""
        try:
            env_id = session.environment_id or source_agent.active_environment_id
            return db.get(AgentEnvironment, env_id) if env_id else None
        except Exception as e:  # noqa: BLE001 — context is best-effort
            logger.warning("Improvement context: environment lookup failed: %s", e)
            return None

    @staticmethod
    def _agent_block(db: DBSession, source_agent: Agent, resolution: Any) -> dict:
        block: dict[str, Any] = {
            "source_agent_id": str(source_agent.id),
            "name": source_agent.name,
            "is_bundle_install": source_agent.bundle_uuid is not None,
            "is_publisher_install": bool(source_agent.is_publisher_install),
            # Every Agent row carries a reverse-DNS ``bundle_id`` from creation,
            # published or not. Only report it once the agent is actually linked
            # to a bundle, so this matches ``build_context_preview`` and the card
            # never shows a bundle identity for a standalone agent.
            "bundle_id": (
                source_agent.bundle_id if source_agent.bundle_uuid else None
            ),
            "installed_revision_number": None,
            "installed_version": None,
            "latest_revision_number": None,
            "latest_version": None,
            "update_pending": bool(source_agent.pending_update),
        }
        try:
            if source_agent.installed_revision_id:
                installed = db.get(
                    AgentBundleRevision, source_agent.installed_revision_id
                )
                if installed:
                    block["installed_revision_number"] = installed.revision_number
                    block["installed_version"] = installed.version
            bundle = getattr(resolution, "bundle", None)
            if bundle is not None and bundle.latest_revision_id:
                latest = db.get(AgentBundleRevision, bundle.latest_revision_id)
                if latest:
                    block["latest_revision_number"] = latest.revision_number
                    block["latest_version"] = latest.version
        except Exception as e:  # noqa: BLE001
            logger.warning("Improvement context: bundle revision lookup failed: %s", e)
        return block

    @staticmethod
    def _environment_block(environment: AgentEnvironment | None) -> dict:
        if environment is None:
            return {
                "env_name": None,
                "env_version": None,
                "instance_name": None,
                "current_image_tag": None,
                "expected_image_tag": None,
                "image_stale": None,
                "status_at_capture": "absent",
                "critical_state": False,
                "critical_cause": None,
            }

        expected_tag: str | None = None
        try:
            from app.services.environments.template_image_service import (
                template_image_service,
            )

            expected_tag = template_image_service.get_image_tag(environment.env_name)
        except Exception as e:  # noqa: BLE001 — a missing template is not fatal
            logger.warning("Improvement context: expected image tag failed: %s", e)

        current_tag = environment.current_image_tag
        image_stale = (
            (current_tag != expected_tag)
            if (current_tag and expected_tag)
            else None
        )
        return {
            "env_name": environment.env_name,
            "env_version": environment.env_version,
            "instance_name": environment.instance_name,
            "current_image_tag": current_tag,
            "expected_image_tag": expected_tag,
            "image_stale": image_stale,
            "status_at_capture": environment.status,
            "critical_state": bool(environment.critical_state),
            "critical_cause": environment.critical_cause,
        }

    @staticmethod
    def _sdk_block(
        db: DBSession,
        session: ChatSession,
        source_agent: Agent,
        environment: AgentEnvironment | None,
    ) -> dict:
        mode = session.mode or "conversation"
        block: dict[str, Any] = {
            "session_mode": mode,
            "agent_sdk_conversation": None,
            "agent_sdk_building": None,
            "model_override_conversation": None,
            "model_override_building": None,
            "effective_engine": None,
            "effective_model": None,
        }
        if environment is None:
            return block

        block["agent_sdk_conversation"] = environment.agent_sdk_conversation
        block["agent_sdk_building"] = environment.agent_sdk_building
        block["model_override_conversation"] = environment.model_override_conversation
        block["model_override_building"] = environment.model_override_building
        block["effective_engine"] = (
            environment.agent_sdk_building
            if mode == "building"
            else environment.agent_sdk_conversation
        )

        # Reuse the EXISTING resolver rather than re-implementing the
        # override → credential default_model → catalog tier precedence.
        # ``evaluate_environment`` runs that same chain per mode and never
        # raises (it degrades to an empty roll-up).
        try:
            from app.services.environments.model_health_service import (
                evaluate_environment,
            )

            health = evaluate_environment(db, environment, agent=source_agent)
            for mode_health in health.modes:
                if mode_health.mode == mode:
                    block["effective_model"] = mode_health.model
                    break
        except Exception as e:  # noqa: BLE001
            logger.warning("Improvement context: effective model resolution failed: %s", e)
        return block

    @staticmethod
    def _plugins_block(db: DBSession, source_agent: Agent) -> list[dict]:
        """Plugin identities from the persisted manifest. Never blocks."""
        try:
            from app.services.plugins.llm_plugin_service import LLMPluginService

            manifest = LLMPluginService.build_plugin_manifest(db, source_agent.id)
            return [
                {
                    "name": entry.get("plugin_name"),
                    "source": entry.get("source"),
                    "commit": entry.get("commit_hash"),
                }
                for entry in (manifest.get("plugins") or [])
            ]
        except Exception as e:  # noqa: BLE001 — plan §9: plugins: [], never abort
            logger.warning("Improvement context: plugin manifest unreadable: %s", e)
            return []

    @staticmethod
    def _recipient_block(
        db: DBSession, session: ChatSession, resolution: Any
    ) -> dict:
        from app.services.improvement.improvement_request_service import (
            display_name_for_user,
        )
        from app.models.users.user import User

        owner_user_id = getattr(resolution, "owner_user_id", None)
        owner_display: str | None = None
        try:
            owner = db.get(User, owner_user_id) if owner_user_id else None
            owner_display = display_name_for_user(owner)
        except Exception as e:  # noqa: BLE001
            logger.warning("Improvement context: recipient lookup failed: %s", e)
        return {
            "target_agent_id": str(resolution.target_agent.id),
            "owner_display": owner_display,
            "is_shared_externally": owner_user_id != session.user_id,
            "fallback_reason": getattr(resolution, "fallback_reason", None),
        }

    # ── Prompts ──────────────────────────────────────────────────────

    @staticmethod
    def _prompts_block(db: DBSession, source_agent: Agent) -> dict:
        """Freeze the agent's prompt documents and tool configuration.

        This is the half of "what the system prompt looked like" that lives in
        the database. It matters most for a **bundle install**: the consumer
        may have edited ``WORKFLOW_PROMPT.md`` in their own container, the edit
        flowed back into their ``Agent`` row through the prompt reconcile, and
        the publisher has no way to see it — they only ever see the text they
        themselves published. Without this block a publisher debugs a prompt
        that was not the one running.

        Divergence is **computed, not guessed**: each field's live text is
        hashed and compared against the same field on the agent's installed
        ``AgentBundleRevision``. With no installed revision (a standalone
        agent, or a bundle row whose revision was deleted) there is no baseline
        and every ``diverged_from_installed_revision`` is ``null`` — never
        ``false``, which would assert a match that was never checked.

        Hashes are taken **before** the secret scrub runs over the context, so
        they identify the text as the agent actually ran it. A field whose
        ``text`` contains ``***REDACTED***`` therefore will not re-hash to its
        recorded ``sha256``; that is deliberate and documented in the archive.
        """
        try:
            return SessionSnapshotService._build_prompts_block(db, source_agent)
        except Exception as e:  # noqa: BLE001 — plan §9: degrade, never abort
            logger.warning("Improvement context: prompt capture failed: %s", e)
            return {"schema_version": CONTEXT_SCHEMA_VERSION, "baseline": "none"}

    @staticmethod
    def _build_prompts_block(db: DBSession, source_agent: Agent) -> dict:
        baseline = SessionSnapshotService._installed_revision(db, source_agent)
        fields: dict[str, Any] = {}
        any_diverged = False

        for key, column in _PROMPT_FIELDS:
            text = getattr(source_agent, column, None)
            baseline_text = getattr(baseline, column, None) if baseline else None
            diverged: bool | None = None
            if baseline is not None:
                diverged = _normalise(text) != _normalise(baseline_text)
                any_diverged = any_diverged or diverged
            fields[key] = {
                "chars": len(text or ""),
                "sha256": _sha256(text),
                "updated_at": _iso(
                    getattr(source_agent, f"{column}_updated_at", None)
                ),
                "diverged_from_installed_revision": diverged,
                "truncated": len(text or "") > MAX_PROMPT_TEXT_CHARS,
                "text": _truncate(text, MAX_PROMPT_TEXT_CHARS, TRUNCATION_MARKER)
                or None,
            }

        sdk_config = source_agent.agent_sdk_config or {}
        return {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            # ``baseline`` names what the divergence flags were measured
            # against, so a reader never has to infer it from a null.
            "baseline": "installed_revision" if baseline is not None else "none",
            "baseline_version": baseline.version if baseline is not None else None,
            "diverged": any_diverged if baseline is not None else None,
            **fields,
            "sdk_tools": list(sdk_config.get("sdk_tools") or []),
            "allowed_tools": list(sdk_config.get("allowed_tools") or []),
            "example_prompts": list(source_agent.example_prompts or []),
        }

    @staticmethod
    def _installed_revision(
        db: DBSession, source_agent: Agent
    ) -> AgentBundleRevision | None:
        """The revision this install was materialised from, best-effort."""
        if not source_agent.installed_revision_id:
            return None
        try:
            return db.get(AgentBundleRevision, source_agent.installed_revision_id)
        except Exception as e:  # noqa: BLE001 — no baseline is not fatal
            logger.warning("Improvement context: prompt baseline lookup failed: %s", e)
            return None

    # ── Personal memory ──────────────────────────────────────────────

    @staticmethod
    async def capture_memory(
        db: DBSession,
        session: ChatSession,
        source_agent: Agent,
        include: bool = True,
    ) -> dict:
        """Freeze the per-install personal memory area (``app-data/memory``).

        The other half of "what the system prompt looked like". Unlike the
        prompt documents this content is **never** synced to the backend and is
        excluded from bundle snapshots and git, so the container is the only
        place it exists — hence the one live read, taken at consent time and
        frozen like everything else. Nothing re-reads it afterwards.

        Args:
            include: the requester's choice. ``False`` records
                ``declined_by_requester`` and reads nothing — the opt-out must
                not leave a trace of the content it declined to share.

        Returns:
            The ``context.memory`` block. Never raises: an absent, stopped, or
            unreachable environment records ``available: false`` with a reason.
        """
        if not include:
            return _memory_unavailable(MEMORY_REASON_DECLINED)

        environment = SessionSnapshotService._resolve_environment(
            db, session, source_agent
        )
        if environment is None:
            return _memory_unavailable(MEMORY_REASON_NO_ENVIRONMENT)
        if environment.status != "running":
            # Deliberately does NOT wake the container. Submitting an
            # improvement request must never start billable compute on the
            # requester's behalf.
            return _memory_unavailable(MEMORY_REASON_ENV_NOT_RUNNING)

        try:
            from app.services.environments.agent_env_connector import (
                AgentEnvConnector,
            )
            from app.services.sessions.message_service import MessageService

            result = await AgentEnvConnector().exec_command(
                base_url=MessageService.get_environment_url(environment),
                auth_token=(environment.config or {}).get("auth_token", ""),
                # The reader is piped in over stdin rather than embedded in the
                # command string: no quoting, and nothing user-controlled ever
                # reaches the shell.
                command="python3 -",
                stdin=_MEMORY_READER_SCRIPT,
                timeout=MEMORY_READ_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001 — plan §9: degrade, never abort
            logger.warning(
                "Improvement context: memory read failed: %s", type(e).__name__
            )
            return _memory_unavailable(MEMORY_REASON_READ_FAILED)

        if result.get("exit_code") != 0:
            return _memory_unavailable(MEMORY_REASON_READ_FAILED)
        try:
            raw_files = json.loads(result.get("stdout") or "[]")
        except (TypeError, ValueError):
            return _memory_unavailable(MEMORY_REASON_READ_FAILED)
        if not isinstance(raw_files, list):
            return _memory_unavailable(MEMORY_REASON_READ_FAILED)

        return SessionSnapshotService._memory_block(raw_files)

    @staticmethod
    def _memory_block(raw_files: list) -> dict:
        """Shape, sanitise, and cap the reader's output.

        Filenames come from inside the requester's container, so they are
        treated as untrusted: :func:`_safe_member_name` strips them to a bare
        basename before they can become a path in the downloaded ZIP.
        """
        files: list[dict] = []
        used = 0
        truncated = False
        for index, entry in enumerate(raw_files[:MAX_MEMORY_FILES]):
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            if not isinstance(text, str):
                continue
            remaining = MAX_MEMORY_TOTAL_CHARS - used
            if remaining <= 0:
                truncated = True
                break
            clipped = text[:remaining]
            file_truncated = len(clipped) < len(text) or bool(entry.get("truncated"))
            truncated = truncated or file_truncated
            used += len(clipped)
            files.append(
                {
                    "filename": _safe_member_name(entry.get("filename"), index),
                    "chars": len(text),
                    "sha256": _sha256(text),
                    "truncated": file_truncated,
                    "text": clipped + (TRUNCATION_MARKER if file_truncated else ""),
                }
            )
        truncated = truncated or len(raw_files) > MAX_MEMORY_FILES

        if not files:
            return _memory_unavailable(MEMORY_REASON_EMPTY)
        return {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "available": True,
            "unavailable_reason": None,
            "captured_at": datetime.now(UTC).isoformat(),
            "file_count": len(files),
            "total_chars": used,
            "truncated": truncated,
            "files": files,
        }


# ── Memory helpers ───────────────────────────────────────────────────

# Runs inside the agent container. Emits the memory area as JSON on stdout so
# the backend never has to parse a delimiter out of arbitrary file content.
# Mirrors ``prompt_generator._load_personal_memory``: ``*.md`` only, sorted
# case-insensitively by filename — the same order the runtime injects them in.
_MEMORY_READER_SCRIPT = f"""
import json, os, sys

directory = {MEMORY_DIR!r}
per_file_cap = {MAX_MEMORY_TOTAL_CHARS}
out = []
try:
    names = sorted(
        (n for n in os.listdir(directory) if n.endswith(".md")),
        key=lambda n: n.lower(),
    )
except OSError:
    names = []
for name in names[:{MAX_MEMORY_FILES}]:
    path = os.path.join(directory, name)
    if not os.path.isfile(path):
        continue
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(per_file_cap + 1)
    except OSError:
        continue
    out.append({{
        "filename": name,
        "text": text[:per_file_cap],
        "truncated": len(text) > per_file_cap,
    }})
json.dump(out, sys.stdout)
"""


def _memory_unavailable(reason: str) -> dict:
    """The empty memory block, carrying why nothing was captured."""
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "available": False,
        "unavailable_reason": reason,
        "captured_at": datetime.now(UTC).isoformat(),
        "file_count": 0,
        "total_chars": 0,
        "truncated": False,
        "files": [],
    }


# Anything outside this set is replaced. Keeps a container-supplied filename
# from becoming a path (``../``), a hidden file, or a Windows-hostile name once
# the recipient extracts the archive.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _safe_member_name(filename: Any, index: int) -> str:
    """Reduce an untrusted filename to a bare, extractable basename."""
    candidate = str(filename or "").strip().replace("\\", "/").split("/")[-1]
    candidate = _UNSAFE_NAME_CHARS.sub("_", candidate).lstrip(".")[:80]
    return candidate or f"memory-{index + 1}.md"


def _sha256(text: str | None) -> str | None:
    """Hex digest of ``text``, or ``None`` when there is no text at all."""
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalise(text: str | None) -> str:
    """Comparison form for divergence: trailing whitespace is not a change."""
    return (text or "").strip()
