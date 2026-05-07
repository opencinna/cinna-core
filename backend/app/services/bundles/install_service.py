"""InstallService — install / uninstall / apply-update for ``Agent`` rows.

The ``Agent`` table is the "Install" table in the new model. Every Install
row has a ``bundle_id`` (reverse-DNS string); foreign installs additionally
link a ``bundle_uuid`` and ``installed_revision_id`` so we know what's
running where.

Key flows:

- ``install_bundle``: idempotent (one install per ``(user, bundle_uuid)``).
  Creates a fresh Agent row, an environment seeded from the bundle's
  latest revision, ensures the per-user app-data volume, and starts the
  env. Credentials are bound from user selections + auto-share rules
  inherited from the legacy clone path.

- ``apply_update``: stops the env, calls
  ``EnvironmentLifecycleManager.replace_bundle_content()`` to swap the
  bundle folders (preserving ``app-data/`` + ``credentials/``), restarts.

- ``uninstall``: marks the user's app-data volume orphaned BEFORE the
  Agent row is deleted (``AppDataService.wipe_volume`` requires
  ``is_orphaned=true``); deletes the env (DOWN -v on the env workspace
  volume); deletes the Agent row.

- ``check_for_updates`` / ``set_update_mode``: small read/write helpers.

- ``install_bundle_for_email``: thin wrapper used by the email routing
  service. Auto-promotes the publisher install into a bundle on first
  email-driven install (mirrors today's ``create_auto_share`` semantics).
"""
import logging
import uuid
from datetime import datetime, UTC
from pathlib import Path

from sqlmodel import Session, select
from fastapi import HTTPException

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle, BundleInstallMode
from app.models.bundles.agent_bundle_revision import AgentBundleRevision
from app.models.bundles.app_data_volume import AppDataVolume
from app.models.bundles.catalog import (
    AICredentialSelections,
    InstallRequest,
)
from app.models.environments.environment import (
    AgentEnvironment,
    AgentEnvironmentCreate,
)
from app.models.events.event import EventType
from app.models.users.user import User

logger = logging.getLogger(__name__)


class InstallError(Exception):
    """Generic install/apply-update error."""


class InstallService:
    """High-level operations on installs (Agent rows)."""

    # ── Install ────────────────────────────────────────────────────

    @staticmethod
    async def install_bundle(
        session: Session,
        user: User,
        bundle: AgentBundle,
        request: InstallRequest | None = None,
    ) -> Agent:
        """Install ``bundle`` for ``user``.

        Returns the existing install if the user already has one (idempotent).
        Raises ``InstallError`` if the bundle has no revisions yet.
        """
        if bundle.latest_revision_id is None:
            raise InstallError("Bundle has no published revisions")
        revision = session.get(AgentBundleRevision, bundle.latest_revision_id)
        if revision is None:
            raise InstallError("Bundle latest revision is missing")

        # Idempotent: re-use the user's existing install if any.
        existing_stmt = select(Agent).where(
            Agent.bundle_uuid == bundle.id,
            Agent.owner_id == user.id,
        )
        existing = session.exec(existing_stmt).first()
        if existing:
            return existing

        return await InstallService._install_from_revision(
            session=session,
            user=user,
            bundle=bundle,
            revision=revision,
            request=request,
            is_publisher_install=False,
        )

    @staticmethod
    async def install_bundle_for_email(
        session: Session,
        publisher_agent_id: uuid.UUID,
        recipient_user_id: uuid.UUID,
    ) -> Agent:
        """Auto-install for an email sender.

        If the publisher hasn't published yet, lazily create an empty bundle
        + first revision so installs can attach. This preserves today's
        email-integration behaviour where the first sender effectively
        forks a clone of the unpublished agent.
        """
        from app.services.bundles.bundle_service import BundleService
        from app.services.bundles.publish_service import PublishService

        publisher = session.get(Agent, publisher_agent_id)
        if not publisher:
            raise InstallError("Publisher agent not found")
        recipient = session.get(User, recipient_user_id)
        if not recipient:
            raise InstallError("Recipient user not found")

        bundle = BundleService.get_bundle_by_id(session, publisher.bundle_id)
        if bundle is None or bundle.latest_revision_id is None:
            # Promote the publisher install into a bundle by publishing
            # an initial revision. This is the email-driven equivalent of
            # the publish-from-UI flow.
            await PublishService.publish(
                session=session,
                install=publisher,
                publisher_user_id=publisher.owner_id,
                release_notes="Initial revision (auto-published via email integration)",
            )
            session.refresh(publisher)
            bundle = BundleService.get_bundle_by_id(session, publisher.bundle_id)
            if bundle is None or bundle.latest_revision_id is None:
                raise InstallError("Auto-publish for email integration failed")

        return await InstallService.install_bundle(
            session=session,
            user=recipient,
            bundle=bundle,
        )

    @staticmethod
    async def admin_install(
        session: Session,
        target_user: User,
        bundle: AgentBundle,
        request: InstallRequest | None = None,
    ) -> Agent:
        """Install on behalf of another user (admin-only path)."""
        return await InstallService.install_bundle(
            session=session,
            user=target_user,
            bundle=bundle,
            request=request,
        )

    @staticmethod
    async def _install_from_revision(
        *,
        session: Session,
        user: User,
        bundle: AgentBundle,
        revision: AgentBundleRevision,
        request: InstallRequest | None,
        is_publisher_install: bool,
    ) -> Agent:
        from app.services.bundles.app_data_service import AppDataService
        from app.services.bundles.bundle_id_service import BundleIdService
        from app.services.environments.environment_service import EnvironmentService
        from app.services.environments.workspace_copy import (
            seed_workspace_from_bundle_snapshot,
        )

        # 1. Allocate Install row up-front so the bundle_id can be derived.
        install_id = uuid.uuid4()
        # Foreign installs share the publisher's reverse-DNS bundle_id —
        # this is what app-data is keyed on, so reinstalls reattach.
        install = Agent(
            id=install_id,
            owner_id=user.id,
            user_workspace_id=None,
            name=bundle.display_name,
            description=bundle.description,
            workflow_prompt=revision.workflow_prompt,
            entrypoint_prompt=revision.entrypoint_prompt,
            refiner_prompt=revision.refiner_prompt,
            bundle_id=bundle.bundle_id,
            bundle_uuid=bundle.id,
            installed_revision_id=revision.id,
            is_publisher_install=is_publisher_install,
            update_mode=bundle.default_install_mode or BundleInstallMode.MANUAL,
            last_sync_at=datetime.now(UTC),
            last_update_status="synced",
        )
        # Ensure name is unique per owner — append "(2)" etc.
        install.name = await InstallService._ensure_unique_name(
            session, user.id, install.name
        )
        session.add(install)
        session.commit()
        session.refresh(install)

        # 2. Build environment (uses revision SDK selection).
        env_data = AgentEnvironmentCreate(
            env_name=settings.DEFAULT_AGENT_ENV_NAME,
            env_version=settings.DEFAULT_AGENT_ENV_VERSION,
            instance_name="Default",
            type="docker",
            config={},
            agent_sdk_conversation=revision.agent_sdk_conversation,
            agent_sdk_building=revision.agent_sdk_building,
            use_default_ai_credentials=False,
            conversation_ai_credential_id=(
                request.ai_credential_selections.conversation_credential_id
                if request and request.ai_credential_selections
                else None
            ),
            building_ai_credential_id=(
                request.ai_credential_selections.building_credential_id
                if request and request.ai_credential_selections
                else None
            ),
        )
        try:
            env = await EnvironmentService.create_environment(
                session=session,
                agent_id=install.id,
                data=env_data,
                user=user,
                auto_start=True,
            )
            install.active_environment_id = env.id
            session.add(install)
            session.commit()
            session.refresh(install)
        except Exception as e:
            logger.error(
                "Failed to create env for install %s of bundle %s: %s",
                install.id, bundle.bundle_id, e,
            )
            # Best-effort rollback of the Install row on env-create failure;
            # the per-user app-data volume is preserved.
            try:
                session.delete(install)
                session.commit()
            except Exception:
                session.rollback()
            raise InstallError(f"Failed to provision environment: {e}") from e

        # 3. Seed workspace from snapshot. The env's workspace dir is
        # created during ``create_environment_instance``; we drop the
        # bundle folders into it before the container starts. This races
        # with the background create task in theory; in practice the
        # ``create_environment`` API kicks off the build asynchronously
        # and the workspace files are read at container boot, so seeding
        # here is fine for the common case. For Docker-in-Docker setups,
        # see the env-service for the bind path indirection.
        try:
            seed_workspace_from_bundle_snapshot(
                Path(revision.snapshot_path), env.id
            )
        except Exception as e:
            logger.warning(
                "Failed to seed workspace from snapshot for install %s: %s",
                install.id, e,
            )

        # 4. Ensure / reattach app-data volume.
        try:
            AppDataService.get_or_create_volume(
                session,
                user_id=user.id,
                bundle_id=install.bundle_id,
                current_install_id=install.id,
            )
        except Exception as e:
            logger.warning(
                "Failed to attach app-data volume for install %s: %s",
                install.id, e,
            )

        # 5. Setup credentials (placeholders for required specs).
        try:
            await InstallService._setup_install_credentials(
                session=session,
                install=install,
                revision=revision,
                user_provided_data=(request.credentials if request else None) or {},
            )
        except Exception as e:
            logger.warning(
                "Failed to setup credentials for install %s: %s",
                install.id, e,
            )

        return install

    @staticmethod
    async def _ensure_unique_name(
        session: Session, owner_id: uuid.UUID, name: str
    ) -> str:
        original_name = name
        counter = 1
        while True:
            stmt = select(Agent).where(
                Agent.owner_id == owner_id,
                Agent.name == name,
            )
            existing = session.exec(stmt).first()
            if not existing:
                return name
            counter += 1
            name = f"{original_name} ({counter})"

    @staticmethod
    async def _setup_install_credentials(
        *,
        session: Session,
        install: Agent,
        revision: AgentBundleRevision,
        user_provided_data: dict,
    ) -> None:
        """Create placeholders / link selections for the install's credentials.

        ``required_credential_specs`` lives on the revision; for each spec
        we create a placeholder credential owned by the install owner and
        link it via ``AgentCredentialLink``. Selection data follows the
        legacy accept-share shape — a credential ID string links an
        existing user credential, while a dict places legacy values into
        the placeholder.
        """
        from app.services.credentials.credentials_service import CredentialsService
        from app.models.credentials.credential import Credential, CredentialType
        from app.models.credentials.link_models import AgentCredentialLink

        for spec in revision.required_credential_specs or []:
            name = spec.get("name")
            cred_type_str = spec.get("type")
            if not name or not cred_type_str:
                continue

            user_selection = user_provided_data.get(name)
            selected_credential_id = None
            legacy_data = None
            if isinstance(user_selection, str):
                try:
                    selected_credential_id = uuid.UUID(user_selection)
                except (ValueError, TypeError):
                    pass
            elif isinstance(user_selection, dict):
                legacy_data = user_selection

            if selected_credential_id:
                selected = session.get(Credential, selected_credential_id)
                if selected and selected.owner_id == install.owner_id:
                    session.add(AgentCredentialLink(
                        agent_id=install.id,
                        credential_id=selected.id,
                    ))
                    continue
                logger.warning(
                    "Credential %s not owned by install owner %s — falling back to placeholder",
                    selected_credential_id, install.owner_id,
                )

            try:
                cred_type = CredentialType(cred_type_str)
            except ValueError:
                logger.warning(
                    "Unknown credential type '%s' for spec '%s' — skipping",
                    cred_type_str, name,
                )
                continue

            placeholder = Credential(
                owner_id=install.owner_id,
                name=f"{name} (placeholder)",
                type=cred_type,
                notes="Placeholder for required bundle credential.",
                encrypted_data=CredentialsService._encrypt_data(legacy_data or {}),
                is_placeholder=legacy_data is None,
                allow_sharing=False,
            )
            if legacy_data:
                placeholder.name = name
            session.add(placeholder)
            session.flush()
            session.add(AgentCredentialLink(
                agent_id=install.id,
                credential_id=placeholder.id,
            ))
        session.commit()

    # ── Apply update ───────────────────────────────────────────────

    @staticmethod
    async def apply_update(
        session: Session, install: Agent
    ) -> Agent:
        """Apply the bundle's latest revision to ``install``.

        Steps:
          1. Resolve bundle + latest revision; bail if already up-to-date.
          2. Stop env (if running) — preserves a foreground stream from
             being cut mid-flight; callers that hit this while streaming
             should defer to the suspension scheduler.
          3. Replace bundle folders from the snapshot (preserves
             ``app-data/`` and ``credentials/``).
          4. Sync prompts onto the install row + push them into env docs.
          5. Update install bookkeeping fields and emit
             ``INSTALL_UPDATE_APPLIED`` / ``INSTALL_UPDATE_FAILED``.
        """
        from app.services.bundles.bundle_service import BundleService
        from app.services.environments.environment_lifecycle import (
            EnvironmentLifecycleManager,
        )
        from app.services.environments.workspace_copy import replace_bundle_content
        from app.services.events.event_service import event_service

        if not install.bundle_uuid:
            raise InstallError("Install is not linked to a published bundle")

        bundle = BundleService.get_bundle_by_uuid(session, install.bundle_uuid)
        if bundle is None or bundle.latest_revision_id is None:
            raise InstallError("Bundle has no published revisions")
        revision = session.get(AgentBundleRevision, bundle.latest_revision_id)
        if revision is None:
            raise InstallError("Latest revision row missing")

        if install.installed_revision_id == revision.id:
            install.pending_update = False
            install.pending_update_at = None
            session.add(install)
            session.commit()
            return install

        env = (
            session.get(AgentEnvironment, install.active_environment_id)
            if install.active_environment_id else None
        )
        lifecycle = EnvironmentLifecycleManager()

        was_running = env is not None and env.status == "running"
        try:
            if was_running:
                try:
                    await lifecycle.stop_environment(session, env)
                except Exception as e:
                    logger.warning(
                        "Failed to stop env %s before update: %s — continuing",
                        env.id, e,
                    )

            if env is not None:
                replace_bundle_content(Path(revision.snapshot_path), env.id)

            install.workflow_prompt = revision.workflow_prompt
            install.entrypoint_prompt = revision.entrypoint_prompt
            install.refiner_prompt = revision.refiner_prompt
            install.installed_revision_id = revision.id
            install.last_sync_at = datetime.now(UTC)
            install.last_update_status = "synced"
            install.pending_update = False
            install.pending_update_at = None
            session.add(install)
            session.commit()
            session.refresh(install)

            # Best-effort restart — failures here surface via env status.
            if env is not None and was_running:
                try:
                    await lifecycle.start_environment(session, env, install)
                except Exception as e:
                    logger.warning(
                        "Failed to restart env %s after update: %s",
                        env.id, e,
                    )

            try:
                await event_service.emit_event(
                    event_type=EventType.INSTALL_UPDATE_APPLIED,
                    model_id=install.id,
                    user_id=install.owner_id,
                    meta={
                        "agent_id": str(install.id),
                        "bundle_id": install.bundle_id,
                        "revision_number": revision.revision_number,
                    },
                )
            except Exception:
                pass

            return install
        except Exception as e:
            install.last_update_status = "failed"
            session.add(install)
            session.commit()
            try:
                await event_service.emit_event(
                    event_type=EventType.INSTALL_UPDATE_FAILED,
                    model_id=install.id,
                    user_id=install.owner_id,
                    meta={
                        "agent_id": str(install.id),
                        "bundle_id": install.bundle_id,
                        "error": str(e),
                    },
                )
            except Exception:
                pass
            raise

    # ── Uninstall ─────────────────────────────────────────────────

    @staticmethod
    async def uninstall(session: Session, install: Agent) -> None:
        """Delete the install + env, mark per-user app-data orphaned.

        Calling order matters: ``AppDataService.wipe_volume`` requires
        ``is_orphaned=true``, and that flag is set HERE — *before* the
        Agent row is deleted. ``AgentService.delete_agent`` handles env
        teardown and the row delete.
        """
        from app.services.agents.agent_service import AgentService

        # ``delete_agent`` in agent_service already calls
        # ``AppDataService.mark_orphaned`` before the row delete, so we just
        # delegate. Centralising the deletion path keeps env-cleanup, session
        # cleanup, and app-data orphaning consistent across uninstall +
        # admin delete.
        ok = await AgentService.delete_agent(session, install.id)
        if not ok:
            raise InstallError("Install not found")

    # ── Update mode + check ──────────────────────────────────────

    @staticmethod
    def set_update_mode(
        session: Session, install: Agent, mode: str
    ) -> Agent:
        if mode not in (BundleInstallMode.AUTOMATIC, BundleInstallMode.MANUAL):
            raise ValueError(f"Invalid update_mode: {mode}")
        install.update_mode = mode
        session.add(install)
        session.commit()
        session.refresh(install)
        return install

    @staticmethod
    def check_for_updates(
        session: Session, install: Agent
    ) -> dict:
        from app.services.bundles.bundle_service import BundleService

        installed_number: int | None = None
        latest_number: int | None = None
        if install.installed_revision_id:
            rev = session.get(AgentBundleRevision, install.installed_revision_id)
            if rev:
                installed_number = rev.revision_number
        if install.bundle_uuid:
            bundle = BundleService.get_bundle_by_uuid(session, install.bundle_uuid)
            if bundle:
                latest_rev = BundleService.latest_revision(session, bundle)
                if latest_rev:
                    latest_number = latest_rev.revision_number

        if (
            latest_number is not None
            and installed_number is not None
            and latest_number > installed_number
        ):
            install.pending_update = True
            if install.pending_update_at is None:
                install.pending_update_at = datetime.now(UTC)
        else:
            if install.pending_update:
                install.pending_update = False
                install.pending_update_at = None
        session.add(install)
        session.commit()
        session.refresh(install)

        return {
            "pending_update": install.pending_update,
            "installed_revision_number": installed_number,
            "latest_revision_number": latest_number,
            "last_update_status": install.last_update_status,
            "last_sync_at": install.last_sync_at,
            "update_mode": install.update_mode,
        }

    # ── Bundle id editing (publisher install pre-publish) ───────

    @staticmethod
    def edit_bundle_id(
        session: Session, install: Agent, new_bundle_id: str
    ) -> Agent:
        """Edit ``Agent.bundle_id`` on a not-yet-published install.

        Rules:
          - The agent must not be published yet (no ``bundle_uuid`` set).
          - ``new_bundle_id`` must match the DNS-like regex.
          - Reserved prefixes are rejected.
          - Per-instance uniqueness is enforced at the DB layer; this
            method also pre-checks for nicer error messages.
        """
        from app.services.bundles.bundle_id_service import BundleIdService

        if install.bundle_uuid is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot change bundle_id after publish — would silently "
                    "orphan installed app-data on dependent installs."
                ),
            )
        if not BundleIdService.is_valid_format(new_bundle_id):
            raise HTTPException(
                status_code=400,
                detail="bundle_id format invalid (must be DNS-like, 1-254 chars)",
            )
        if BundleIdService.is_reserved(new_bundle_id):
            raise HTTPException(
                status_code=400,
                detail="bundle_id uses a reserved prefix",
            )

        # Pre-check uniqueness — clearer than the generic IntegrityError.
        if new_bundle_id != install.bundle_id:
            collision_stmt = select(Agent).where(Agent.bundle_id == new_bundle_id)
            if session.exec(collision_stmt).first():
                raise HTTPException(
                    status_code=409,
                    detail="bundle_id is already in use on this instance",
                )
            from app.models.bundles.agent_bundle import AgentBundle

            bundle_collision_stmt = select(AgentBundle).where(
                AgentBundle.bundle_id == new_bundle_id
            )
            if session.exec(bundle_collision_stmt).first():
                raise HTTPException(
                    status_code=409,
                    detail="bundle_id is already in use by a published bundle",
                )

        install.bundle_id = new_bundle_id
        session.add(install)
        session.commit()
        session.refresh(install)
        return install
