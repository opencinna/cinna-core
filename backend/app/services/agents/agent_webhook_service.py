"""
Agent Webhook Service — CRUD, token validation, and webhook execution.

Mirrors the static-method style used by ``AgentSchedulerService`` and
``TaskTriggerService``. Token encryption / one-time reveal / timing-safe
compare follow the Task Triggers conventions exactly.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime

from sqlmodel import Session as DBSession, desc, select

from app.core.config import settings
from app.core.db import engine
from app.core.security import decrypt_field, encrypt_field
from app.models import (
    Agent,
    AgentWebhook,
    AgentWebhookCreateScript,
    AgentWebhookCreateSession,
    AgentWebhookLog,
    AgentWebhookType,
    AgentWebhookUpdate,
    SessionCreate,
)
from app.services.agents.agent_webhook_errors import (
    WebhookNotFoundError,
    WebhookPermissionError,
    WebhookTokenInvalidError,
    WebhookValidationError,
)
from app.services.agents.environment_resolver import (
    ensure_environment_running,
    get_active_environment,
)

logger = logging.getLogger(__name__)


# Maximum assembled prompt size for session-type webhooks
_MAX_PROMPT_CHARS = 20_000
# Truncation marker appended when the assembled prompt exceeds the cap
_TRUNCATED_MARKER = "\n[truncated]"
# stdout / stderr truncation on log
_MAX_OUTPUT_CHARS = 10_000
# payload truncation on log
_MAX_PAYLOAD_LOG_CHARS = 10_000


class AgentWebhookService:
    """Service for managing agent webhooks."""

    # Allowlisted request headers forwarded to the agent (session prompt or
    # script env) and logged. Anything outside this set is dropped — in
    # particular ``authorization`` and ``cookie``, which would otherwise leak
    # the bearer token into prompts and logs.
    FORWARDED_HEADERS: tuple[str, ...] = (
        "user-agent",
        "x-forwarded-for",
        "x-real-ip",
        "x-github-event",
        "x-gitlab-event",
        "x-hub-signature-256",
        "x-event-key",
    )

    # ==================== Access Control Helpers ====================

    @staticmethod
    def verify_agent_access(
        db_session: DBSession,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Agent:
        """
        Verify agent exists and the caller owns it.

        Raises:
            WebhookNotFoundError: If agent does not exist.
            WebhookPermissionError: If agent is not owned by ``user_id``.
        """
        agent = db_session.get(Agent, agent_id)
        if not agent:
            raise WebhookNotFoundError("Agent not found")
        if agent.owner_id != user_id:
            raise WebhookPermissionError()
        return agent

    @staticmethod
    def get_webhook_for_agent(
        db_session: DBSession,
        webhook_pk: uuid.UUID,
        agent_id: uuid.UUID,
    ) -> AgentWebhook:
        """
        Fetch a webhook row and verify it belongs to ``agent_id``. Guards
        against cross-agent ID reuse.
        """
        webhook = db_session.get(AgentWebhook, webhook_pk)
        if not webhook or webhook.agent_id != agent_id:
            raise WebhookNotFoundError()
        return webhook

    # ==================== Token helpers ====================

    @staticmethod
    def generate_webhook_credentials() -> tuple[str, str, str, str]:
        """
        Generate webhook credentials.

        Returns:
            (webhook_id, plaintext_token, encrypted_token, token_prefix)

        ``webhook_id`` uniqueness is enforced at the DB layer via a UNIQUE
        index; callers should handle the (extremely rare) collision by
        retrying. See ``_generate_unique_webhook_id`` for the retrying
        variant used during create.
        """
        webhook_id = secrets.token_urlsafe(8)
        token = secrets.token_urlsafe(32)
        encrypted_token = encrypt_field(token)
        token_prefix = token[:8]
        return webhook_id, token, encrypted_token, token_prefix

    @staticmethod
    def _generate_unique_webhook_id(db_session: DBSession, max_attempts: int = 5) -> str:
        """
        Generate a ``webhook_id`` slug that isn't already taken.

        ``secrets.token_urlsafe(8)`` is ~11 chars — the collision space is
        huge, but guarding is cheap and avoids a confusing IntegrityError.
        """
        for _ in range(max_attempts):
            candidate = secrets.token_urlsafe(8)
            existing = db_session.exec(
                select(AgentWebhook).where(AgentWebhook.webhook_id == candidate)
            ).first()
            if not existing:
                return candidate
        raise RuntimeError(
            "Failed to generate a unique webhook_id after several attempts"
        )

    @staticmethod
    def build_webhook_url(webhook_id: str) -> str:
        """
        Build the full public webhook URL.

        The ``/agent-hooks/`` path is mounted at the root of the backend app
        (not under ``/api/v1``), matching the task-trigger ``/hooks/`` pattern.
        """
        base = (settings.FRONTEND_HOST or "https://localhost").rstrip("/")
        return f"{base}/agent-hooks/{webhook_id}"

    # ==================== Header / payload helpers ====================

    @classmethod
    def filter_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        """
        Keep only allowlisted headers. Case-insensitive lookup, canonical
        lowercase keys in the result.
        """
        lowered = {k.lower(): v for k, v in headers.items()}
        return {h: lowered[h] for h in cls.FORWARDED_HEADERS if h in lowered}

    # ==================== CRUD methods ====================

    @staticmethod
    def create_session_webhook(
        db_session: DBSession,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        data: AgentWebhookCreateSession,
    ) -> tuple[AgentWebhook, str]:
        """
        Create a session-type webhook.

        Returns:
            (AgentWebhook, plaintext_token) — token shown to the user once.
        """
        AgentWebhookService.verify_agent_access(db_session, agent_id, user_id)

        webhook_id = AgentWebhookService._generate_unique_webhook_id(db_session)
        _, token, encrypted_token, token_prefix = (
            AgentWebhookService.generate_webhook_credentials()
        )

        webhook = AgentWebhook(
            agent_id=agent_id,
            owner_id=user_id,
            type=AgentWebhookType.SESSION,
            name=data.name,
            payload_template=data.payload_template,
            prompt=data.prompt,
            session_mode=data.session_mode,
            command=None,
            command_timeout_seconds=None,
            webhook_id=webhook_id,
            webhook_token_encrypted=encrypted_token,
            webhook_token_prefix=token_prefix,
        )
        db_session.add(webhook)
        db_session.commit()
        db_session.refresh(webhook)
        logger.info(
            f"Created session webhook {webhook.id} for agent {agent_id}"
        )
        return webhook, token

    @staticmethod
    def create_script_webhook(
        db_session: DBSession,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        data: AgentWebhookCreateScript,
    ) -> tuple[AgentWebhook, str]:
        """
        Create a script-type webhook.

        Returns:
            (AgentWebhook, plaintext_token) — token shown to the user once.
        """
        AgentWebhookService.verify_agent_access(db_session, agent_id, user_id)

        webhook_id = AgentWebhookService._generate_unique_webhook_id(db_session)
        _, token, encrypted_token, token_prefix = (
            AgentWebhookService.generate_webhook_credentials()
        )

        webhook = AgentWebhook(
            agent_id=agent_id,
            owner_id=user_id,
            type=AgentWebhookType.SCRIPT,
            name=data.name,
            payload_template=data.payload_template,
            prompt=None,
            session_mode=None,
            command=data.command,
            command_timeout_seconds=data.command_timeout_seconds,
            webhook_id=webhook_id,
            webhook_token_encrypted=encrypted_token,
            webhook_token_prefix=token_prefix,
        )
        db_session.add(webhook)
        db_session.commit()
        db_session.refresh(webhook)
        logger.info(
            f"Created script webhook {webhook.id} for agent {agent_id}"
        )
        return webhook, token

    @staticmethod
    def list_webhooks(
        db_session: DBSession,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[AgentWebhook]:
        """List all webhooks for an agent, ordered by created_at DESC."""
        AgentWebhookService.verify_agent_access(db_session, agent_id, user_id)
        statement = (
            select(AgentWebhook)
            .where(AgentWebhook.agent_id == agent_id)
            .order_by(desc(AgentWebhook.created_at))
        )
        return list(db_session.exec(statement).all())

    @staticmethod
    def get_webhook(
        db_session: DBSession,
        agent_id: uuid.UUID,
        webhook_pk: uuid.UUID,
        user_id: uuid.UUID,
    ) -> AgentWebhook:
        """Fetch a single webhook after verifying ownership."""
        AgentWebhookService.verify_agent_access(db_session, agent_id, user_id)
        return AgentWebhookService.get_webhook_for_agent(
            db_session, webhook_pk, agent_id
        )

    @staticmethod
    def update_webhook(
        db_session: DBSession,
        agent_id: uuid.UUID,
        webhook_pk: uuid.UUID,
        user_id: uuid.UUID,
        data: AgentWebhookUpdate,
    ) -> AgentWebhook:
        """
        Partial update.

        Rejects fields that don't belong to the webhook's type (e.g. setting
        ``command`` on a session webhook).
        """
        AgentWebhookService.verify_agent_access(db_session, agent_id, user_id)
        webhook = AgentWebhookService.get_webhook_for_agent(
            db_session, webhook_pk, agent_id
        )

        update_fields = data.model_dump(exclude_unset=True)

        # Type-field mismatch check
        session_only = {"prompt", "session_mode"}
        script_only = {"command", "command_timeout_seconds"}

        if webhook.type == AgentWebhookType.SESSION:
            invalid = script_only & update_fields.keys()
            if invalid:
                raise WebhookValidationError(
                    f"Fields {sorted(invalid)} are not valid for a session-type webhook"
                )
        elif webhook.type == AgentWebhookType.SCRIPT:
            invalid = session_only & update_fields.keys()
            if invalid:
                raise WebhookValidationError(
                    f"Fields {sorted(invalid)} are not valid for a script-type webhook"
                )

        for field, value in update_fields.items():
            if hasattr(webhook, field):
                setattr(webhook, field, value)

        webhook.updated_at = datetime.now(UTC)
        db_session.add(webhook)
        db_session.commit()
        db_session.refresh(webhook)
        logger.info(f"Updated webhook {webhook.id}")
        return webhook

    @staticmethod
    def delete_webhook(
        db_session: DBSession,
        agent_id: uuid.UUID,
        webhook_pk: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Delete webhook. Cascades to logs via FK."""
        AgentWebhookService.verify_agent_access(db_session, agent_id, user_id)
        webhook = AgentWebhookService.get_webhook_for_agent(
            db_session, webhook_pk, agent_id
        )
        db_session.delete(webhook)
        db_session.commit()
        logger.info(f"Deleted webhook {webhook_pk}")

    @staticmethod
    def regenerate_token(
        db_session: DBSession,
        agent_id: uuid.UUID,
        webhook_pk: uuid.UUID,
        user_id: uuid.UUID,
    ) -> tuple[AgentWebhook, str]:
        """
        Rotate the bearer token. Keeps the same ``webhook_id`` slug so
        existing URLs remain valid.
        """
        AgentWebhookService.verify_agent_access(db_session, agent_id, user_id)
        webhook = AgentWebhookService.get_webhook_for_agent(
            db_session, webhook_pk, agent_id
        )

        token = secrets.token_urlsafe(32)
        webhook.webhook_token_encrypted = encrypt_field(token)
        webhook.webhook_token_prefix = token[:8]
        webhook.updated_at = datetime.now(UTC)
        db_session.add(webhook)
        db_session.commit()
        db_session.refresh(webhook)
        logger.info(f"Regenerated token for webhook {webhook_pk}")
        return webhook, token

    # ==================== Webhook execution ====================

    @staticmethod
    def validate_webhook_token(
        db_session: DBSession,
        webhook_id: str,
        provided_token: str,
    ) -> AgentWebhook:
        """
        Look up a webhook by its public slug and verify the bearer token.

        Disabled webhooks return ``WebhookNotFoundError`` (not a 401) to avoid
        leaking existence.
        """
        statement = select(AgentWebhook).where(
            AgentWebhook.webhook_id == webhook_id
        )
        webhook = db_session.exec(statement).first()
        if not webhook or not webhook.enabled:
            raise WebhookNotFoundError("Webhook not found")

        try:
            stored_token = decrypt_field(webhook.webhook_token_encrypted)
        except Exception as exc:
            logger.error(
                f"Failed to decrypt webhook token for {webhook_id}: {exc}"
            )
            raise WebhookTokenInvalidError()

        if not hmac.compare_digest(stored_token, provided_token):
            raise WebhookTokenInvalidError()

        return webhook

    @classmethod
    async def fire_webhook(
        cls,
        db_session: DBSession,
        webhook: AgentWebhook,
        payload_text: str | None,
        payload_content_type: str | None,
        headers: dict[str, str],
        remote_ip: str | None,
    ) -> AgentWebhookLog:
        """
        Dispatch a validated webhook.

        Orchestrator contract:
        - Always creates exactly one ``AgentWebhookLog`` (even on infra errors).
        - Session-type success → ``status="session_started"``, ``session_id`` set.
        - Script-type exit 0 → ``status="success"``.
        - Script-type non-zero exit → ``status="script_error"`` (logged output preserved).
        - Any internal exception → ``status="error"``, ``error_message=str(e)``.
        - Updates ``webhook.last_execution`` on every fire.
        """
        start = time.perf_counter()
        headers_subset = cls.filter_headers(headers)

        agent = db_session.get(Agent, webhook.agent_id)
        if not agent:
            log = cls._create_log(
                db_session,
                webhook=webhook,
                status="error",
                remote_ip=remote_ip,
                headers_subset=headers_subset,
                payload_received=payload_text,
                payload_content_type=payload_content_type,
                error_message="Agent not found",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
            cls._touch_last_execution(db_session, webhook)
            return log

        try:
            if webhook.type == AgentWebhookType.SESSION:
                log = await cls._fire_session(
                    db_session,
                    webhook=webhook,
                    agent=agent,
                    payload_text=payload_text,
                    payload_content_type=payload_content_type,
                    headers_subset=headers_subset,
                    remote_ip=remote_ip,
                    start=start,
                )
            elif webhook.type == AgentWebhookType.SCRIPT:
                log = await cls._fire_script(
                    db_session,
                    webhook=webhook,
                    agent=agent,
                    payload_text=payload_text,
                    payload_content_type=payload_content_type,
                    headers_subset=headers_subset,
                    remote_ip=remote_ip,
                    start=start,
                )
            else:
                log = cls._create_log(
                    db_session,
                    webhook=webhook,
                    status="error",
                    remote_ip=remote_ip,
                    headers_subset=headers_subset,
                    payload_received=payload_text,
                    payload_content_type=payload_content_type,
                    error_message=f"Unknown webhook type: {webhook.type}",
                    duration_ms=int((time.perf_counter() - start) * 1000),
                )
        except Exception as exc:
            logger.error(
                f"fire_webhook: unexpected failure for webhook {webhook.id}: {exc}",
                exc_info=True,
            )
            log = cls._create_log(
                db_session,
                webhook=webhook,
                status="error",
                remote_ip=remote_ip,
                headers_subset=headers_subset,
                payload_received=payload_text,
                payload_content_type=payload_content_type,
                error_message=str(exc),
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        cls._touch_last_execution(db_session, webhook)
        return log

    # ==================== Internal dispatchers ====================

    @classmethod
    async def _fire_session(
        cls,
        db_session: DBSession,
        *,
        webhook: AgentWebhook,
        agent: Agent,
        payload_text: str | None,
        payload_content_type: str | None,
        headers_subset: dict[str, str],
        remote_ip: str | None,
        start: float,
    ) -> AgentWebhookLog:
        """Assemble prompt, create session, enqueue user message, log."""
        from app.services.bundles.install_gate_dispatcher import InstallGateDispatcher
        from app.services.sessions.message_service import MessageService
        from app.services.sessions.session_service import SessionService

        # Install readiness gate (plan §6.2 — webhook rendering). When the
        # install isn't ready we skip session creation entirely (no Session
        # anchor exists yet). The webhook log row records
        # ``status="setup_required"`` so the publisher can see why the agent
        # declined to act.
        gate_result = InstallGateDispatcher.check(db_session, agent)
        if gate_result is not None:
            logger.info(
                "webhook %s: install readiness gate blocking agent %s (status=%s, %d missing)",
                webhook.id, agent.id, gate_result.status, len(gate_result.missing),
            )
            await InstallGateDispatcher.emit_events(
                agent=agent,
                gate_result=gate_result,
                channel="webhook",
                webhook_id=webhook.id,
            )
            return cls._create_log(
                db_session,
                webhook=webhook,
                status="setup_required",
                remote_ip=remote_ip,
                headers_subset=headers_subset,
                payload_received=payload_text,
                payload_content_type=payload_content_type,
                error_message=json.dumps(
                    InstallGateDispatcher.build_webhook_log_summary(gate_result)
                ),
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        prompt = cls._assemble_session_prompt(
            webhook=webhook,
            agent=agent,
            payload_text=payload_text,
            payload_content_type=payload_content_type,
            headers_subset=headers_subset,
        )

        mode = webhook.session_mode or "conversation"
        session = SessionService.create_session(
            db_session=db_session,
            user_id=webhook.owner_id,
            data=SessionCreate(
                agent_id=agent.id,
                mode=mode,
                title=f"Webhook: {webhook.name}",
            ),
            integration_type="webhook",
        )
        if not session:
            return cls._create_log(
                db_session,
                webhook=webhook,
                status="error",
                remote_ip=remote_ip,
                headers_subset=headers_subset,
                payload_received=payload_text,
                payload_content_type=payload_content_type,
                prompt_used=prompt,
                error_message="Could not create session — no active environment",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        try:
            await MessageService.create_user_message_and_emit_event(
                db_session=db_session,
                session_id=session.id,
                message_content=prompt,
                answers_to_message_id=None,
            )
        except Exception as exc:
            logger.error(
                f"webhook {webhook.id}: failed to enqueue user message: {exc}",
                exc_info=True,
            )
            return cls._create_log(
                db_session,
                webhook=webhook,
                status="error",
                remote_ip=remote_ip,
                headers_subset=headers_subset,
                payload_received=payload_text,
                payload_content_type=payload_content_type,
                prompt_used=prompt,
                session_id=session.id,
                error_message=f"Failed to enqueue user message: {exc}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        return cls._create_log(
            db_session,
            webhook=webhook,
            status="session_started",
            remote_ip=remote_ip,
            headers_subset=headers_subset,
            payload_received=payload_text,
            payload_content_type=payload_content_type,
            prompt_used=prompt,
            session_id=session.id,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    @classmethod
    async def _fire_script(
        cls,
        db_session: DBSession,
        *,
        webhook: AgentWebhook,
        agent: Agent,
        payload_text: str | None,
        payload_content_type: str | None,
        headers_subset: dict[str, str],
        remote_ip: str | None,
        start: float,
    ) -> AgentWebhookLog:
        """Resolve env, inject WEBHOOK_* env vars, exec command, log."""
        from app.services.environments.agent_env_connector import (
            agent_env_connector,
        )
        from app.services.sessions.message_service import MessageService

        environment = get_active_environment(db_session, agent.id)
        if not environment:
            return cls._create_log(
                db_session,
                webhook=webhook,
                status="error",
                remote_ip=remote_ip,
                headers_subset=headers_subset,
                payload_received=payload_text,
                payload_content_type=payload_content_type,
                command_executed=webhook.command,
                error_message="No active environment found for agent",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        try:
            environment = await ensure_environment_running(
                environment,
                get_fresh_db_session=lambda: DBSession(engine),
            )
        except RuntimeError as exc:
            return cls._create_log(
                db_session,
                webhook=webhook,
                status="error",
                remote_ip=remote_ip,
                headers_subset=headers_subset,
                payload_received=payload_text,
                payload_content_type=payload_content_type,
                command_executed=webhook.command,
                error_message=str(exc),
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        base_url = MessageService.get_environment_url(environment)
        auth_token = (environment.config or {}).get("auth_token", "")

        exec_env = {
            "WEBHOOK_PAYLOAD": payload_text or "",
            "WEBHOOK_NAME": webhook.name,
            "WEBHOOK_ID": webhook.webhook_id,
            "WEBHOOK_HEADERS_JSON": json.dumps(headers_subset),
            "WEBHOOK_CONTENT_TYPE": payload_content_type or "",
        }

        timeout_seconds = webhook.command_timeout_seconds or 120

        try:
            exec_result = await agent_env_connector.exec_command(
                base_url=base_url,
                auth_token=auth_token,
                command=webhook.command or "",
                timeout=timeout_seconds,
                env=exec_env,
                stdin=payload_text,
            )
        except RuntimeError as exc:
            return cls._create_log(
                db_session,
                webhook=webhook,
                status="error",
                remote_ip=remote_ip,
                headers_subset=headers_subset,
                payload_received=payload_text,
                payload_content_type=payload_content_type,
                command_executed=webhook.command,
                error_message=str(exc),
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        exit_code = exec_result.get("exit_code", -1)
        stdout = cls._truncate(exec_result.get("stdout", ""), _MAX_OUTPUT_CHARS)
        stderr = cls._truncate(exec_result.get("stderr", ""), _MAX_OUTPUT_CHARS)
        status = "success" if exit_code == 0 else "script_error"

        return cls._create_log(
            db_session,
            webhook=webhook,
            status=status,
            remote_ip=remote_ip,
            headers_subset=headers_subset,
            payload_received=payload_text,
            payload_content_type=payload_content_type,
            command_executed=webhook.command,
            command_output=stdout,
            command_stderr=stderr,
            command_exit_code=exit_code,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    # ==================== Prompt / output helpers ====================

    @staticmethod
    def _assemble_session_prompt(
        *,
        webhook: AgentWebhook,
        agent: Agent,
        payload_text: str | None,
        payload_content_type: str | None,
        headers_subset: dict[str, str],
    ) -> str:
        """
        Build the prompt for a session-type webhook.

        Cap the assembled prompt at ``_MAX_PROMPT_CHARS``. When truncated, the
        payload slice is trimmed and a ``[truncated]`` marker is appended —
        caller-supplied content is the only variable-size input.
        """
        base_prompt = (
            webhook.prompt
            or agent.entrypoint_prompt
            or "Start webhook-triggered execution."
        )

        parts: list[str] = [base_prompt]

        has_context = bool(
            webhook.payload_template or payload_text or headers_subset
        )
        if has_context:
            parts.append("---")
            parts.append(f"Webhook: {webhook.name}")
            if webhook.payload_template:
                parts.append(webhook.payload_template)
            if payload_text:
                content_type = payload_content_type or "unknown"
                parts.append(
                    f"Payload (Content-Type: {content_type}):\n{payload_text}"
                )
            if headers_subset:
                parts.append(
                    "Headers:\n" + json.dumps(headers_subset, indent=2)
                )

        combined = "\n\n".join(parts)
        if len(combined) > _MAX_PROMPT_CHARS:
            combined = combined[: _MAX_PROMPT_CHARS - len(_TRUNCATED_MARKER)] + _TRUNCATED_MARKER
        return combined

    @staticmethod
    def _truncate(value: str | None, limit: int) -> str | None:
        """
        Truncate ``value`` to ``limit`` chars with a ``[truncated]`` marker.

        ``None`` passes through unchanged so callers can use this as a simple
        normalizer without having to guard every call site.
        """
        if value is None:
            return None
        if len(value) <= limit:
            return value
        return value[: limit - len(_TRUNCATED_MARKER)] + _TRUNCATED_MARKER

    # ==================== Log helpers ====================

    @staticmethod
    def _create_log(
        db_session: DBSession,
        *,
        webhook: AgentWebhook,
        status: str,
        remote_ip: str | None = None,
        headers_subset: dict[str, str] | None = None,
        payload_received: str | None = None,
        payload_content_type: str | None = None,
        prompt_used: str | None = None,
        command_executed: str | None = None,
        command_output: str | None = None,
        command_stderr: str | None = None,
        command_exit_code: int | None = None,
        session_id: uuid.UUID | None = None,
        error_message: str | None = None,
        duration_ms: int | None = None,
    ) -> AgentWebhookLog:
        """Insert an immutable AgentWebhookLog row and return it."""
        log = AgentWebhookLog(
            webhook_id_fk=webhook.id,
            agent_id=webhook.agent_id,
            webhook_type=webhook.type,
            status=status,
            remote_ip=remote_ip,
            headers_subset=headers_subset,
            payload_received=AgentWebhookService._truncate(
                payload_received, _MAX_PAYLOAD_LOG_CHARS
            ),
            payload_content_type=payload_content_type,
            prompt_used=prompt_used,
            command_executed=command_executed,
            command_output=command_output,
            command_stderr=command_stderr,
            command_exit_code=command_exit_code,
            session_id=session_id,
            error_message=error_message,
            duration_ms=duration_ms,
            executed_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()
        db_session.refresh(log)
        logger.debug(
            f"Created webhook log for {webhook.id}: type={webhook.type}, status={status}"
        )
        return log

    @staticmethod
    def _touch_last_execution(db_session: DBSession, webhook: AgentWebhook) -> None:
        """Update ``webhook.last_execution`` to now. Best-effort."""
        try:
            webhook.last_execution = datetime.now(UTC)
            webhook.updated_at = datetime.now(UTC)
            db_session.add(webhook)
            db_session.commit()
        except Exception as exc:
            logger.warning(
                f"Failed to update last_execution for webhook {webhook.id}: {exc}"
            )

    @staticmethod
    def get_webhook_logs(
        db_session: DBSession,
        agent_id: uuid.UUID,
        webhook_pk: uuid.UUID,
        user_id: uuid.UUID,
        limit: int = 50,
    ) -> list[AgentWebhookLog]:
        """
        Fetch recent logs for a webhook, newest first.

        ``limit`` is clamped to the route-level max; service enforces a
        hard floor of 1.
        """
        AgentWebhookService.verify_agent_access(db_session, agent_id, user_id)
        AgentWebhookService.get_webhook_for_agent(db_session, webhook_pk, agent_id)

        limit = max(1, limit)
        statement = (
            select(AgentWebhookLog)
            .where(AgentWebhookLog.webhook_id_fk == webhook_pk)
            .order_by(desc(AgentWebhookLog.executed_at))
            .limit(limit)
        )
        return list(db_session.exec(statement).all())
