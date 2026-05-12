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
import json
import logging
import uuid
from datetime import datetime, UTC
from pathlib import Path

from sqlmodel import Session, select
from fastapi import HTTPException

from app.core.config import settings
from app.core.security import encrypt_field
from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle, BundleInstallMode
from app.models.bundles.agent_bundle_revision import AgentBundleRevision
from app.models.bundles.app_data_volume import AppDataVolume
from app.models.bundles.catalog import (
    AICredentialSelections,
    InstallRequest,
)
from app.models.bundles.catalog import SetupCredentialSummary
from app.models.credentials.ai_credential import AICredential
from app.models.credentials.credential import Credential
from app.models.credentials.link_models import AgentCredentialLink
from app.models.environments.environment import (
    AgentEnvironment,
    AgentEnvironmentCreate,
)
from app.models.events.event import EventType
from app.models.users.user import User
from app.services.bundles.credential_spec import (
    ParsedCredentialSpec,
    parse_credential_spec,
)

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
            # Self-heal: if the publisher's AICredentialShare row was
            # deleted (manually, or by a future housekeeping job) we
            # recreate it on every idempotent re-install. The helper is
            # itself idempotent, so this is safe to call repeatedly.
            await InstallService._link_publisher_ai_credential(
                session=session,
                user=user,
                bundle=bundle,
            )
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
            router_trigger_prompt=revision.router_trigger_prompt,
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

        # 2. Resolve AI credential ids before env creation. Resolution order
        # for each mode (conversation / building) is:
        #   1. publisher AI credential FK on the bundle (Phase 2 PBP);
        #   2. installer's request selection (existing path);
        #   3. None — env-side resolver falls back to the installer's defaults.
        # When the bundle provides an AI credential, the request selection
        # for THAT mode is ignored — we ensure the AICredentialShare exists
        # so the env-side resolver can decrypt the publisher's row, then
        # link the env to the publisher's credential id.
        request_conv_id = (
            request.ai_credential_selections.conversation_credential_id
            if request and request.ai_credential_selections
            else None
        )
        request_build_id = (
            request.ai_credential_selections.building_credential_id
            if request and request.ai_credential_selections
            else None
        )
        await InstallService._link_publisher_ai_credential(
            session=session,
            user=user,
            bundle=bundle,
        )
        conversation_ai_credential_id = (
            bundle.publisher_ai_credential_conversation_id
            if bundle.publisher_ai_credential_conversation_id is not None
            else request_conv_id
        )
        building_ai_credential_id = (
            bundle.publisher_ai_credential_building_id
            if bundle.publisher_ai_credential_building_id is not None
            else request_build_id
        )

        # 2b. Build environment (uses revision SDK selection).
        env_data = AgentEnvironmentCreate(
            env_name=settings.DEFAULT_AGENT_ENV_NAME,
            env_version=settings.DEFAULT_AGENT_ENV_VERSION,
            instance_name="Default",
            type="docker",
            config={},
            agent_sdk_conversation=revision.agent_sdk_conversation,
            agent_sdk_building=revision.agent_sdk_building,
            use_default_ai_credentials=False,
            conversation_ai_credential_id=conversation_ai_credential_id,
            building_ai_credential_id=building_ai_credential_id,
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
        # Normalise the payload up-front so the new
        # InstallCredentialSelection shape and the legacy
        # ``{name: uuid_string | dict}`` shape both reach the writer in a
        # single, well-typed dict. The shim is sunset in Phase 5; see
        # ``_normalise_credentials_payload``.
        normalised_credentials = InstallService._normalise_credentials_payload(
            (request.credentials if request else None),
            revision,
        )
        try:
            await InstallService._setup_install_credentials(
                session=session,
                install=install,
                revision=revision,
                user_provided_data=normalised_credentials,
            )
        except HTTPException:
            # Validation errors (e.g. user-override on a publisher spec)
            # MUST propagate so the route returns 422. Rollback the
            # half-built install before bubbling up.
            try:
                session.rollback()
                session.delete(install)
                session.commit()
            except Exception:
                session.rollback()
            raise
        except Exception as e:
            logger.warning(
                "Failed to setup credentials for install %s: %s",
                install.id, e,
            )

        # 6. Auto-create the App MCP route so the installer can reach the
        # agent through the App MCP Server immediately. Best-effort: a
        # failure here logs a warning and marks the install degraded but
        # does NOT abort the install.
        try:
            InstallService._auto_create_app_mcp_route(
                session=session,
                install=install,
                revision=revision,
                user=user,
            )
        except Exception as e:
            logger.warning(
                "Failed to auto-create App MCP route for install %s: %s",
                install.id, e,
            )

        return install

    @staticmethod
    def _auto_create_app_mcp_route(
        *,
        session: Session,
        install: Agent,
        revision: AgentBundleRevision,
        user: User,
    ) -> None:
        """Create the installer's auto-managed ``AppAgentRoute`` for this install.

        Idempotent: if a route already exists for this agent (e.g. the
        admin install path re-running), this is a no-op. When the
        revision has no ``router_trigger_prompt``, the route is skipped
        and the install is marked degraded so the UI can prompt the
        owner to set one before the agent is reachable via App MCP.

        Exceptions are caught at the call site so install never aborts on
        a router failure.
        """
        from app.models.app_mcp.app_agent_route import (
            AppAgentRoute,
            AppAgentRouteCreate,
        )
        from app.services.app_mcp.app_agent_route_service import (
            AppAgentRouteService,
        )

        trigger_prompt = (revision.router_trigger_prompt or "").strip()
        if not trigger_prompt:
            logger.info(
                "Install %s of bundle %s has no router_trigger_prompt — "
                "skipping auto-route creation. Owner can set one via the "
                "Prompts tab and the route will be created on next "
                "apply-update.",
                install.id, install.bundle_id,
            )
            # Mark degraded so the UI can surface a hint. Preserve any
            # existing "degraded" state set by credentials setup.
            if install.last_update_status not in ("degraded",):
                install.last_update_status = "degraded"
                session.add(install)
                session.commit()
                session.refresh(install)
            return

        # Idempotency: skip when an auto-managed route already exists for
        # this agent owned by the installer. Reinstall after uninstall
        # always creates a new Agent row anyway, so this branch only
        # triggers on internal retries. We intentionally scope the
        # filter to ``is_auto_managed=True`` so a user-created route on
        # the same agent (e.g. a developer who manually added an extra
        # route in the UI) doesn't suppress the auto-route creation.
        existing_stmt = select(AppAgentRoute).where(
            AppAgentRoute.agent_id == install.id,
            AppAgentRoute.created_by == user.id,
            AppAgentRoute.is_auto_managed == True,  # noqa: E712
        )
        existing = session.exec(existing_stmt).first()
        if existing is not None:
            logger.debug(
                "Auto-route skipped — install %s already has route %s",
                install.id, existing.id,
            )
            return

        payload = AppAgentRouteCreate(
            name=install.name,
            agent_id=install.id,
            session_mode="conversation",
            trigger_prompt=trigger_prompt,
            channel_app_mcp=True,
            is_active=True,
            auto_enable_for_users=False,
            activate_for_myself=True,
        )
        AppAgentRouteService.create_route(
            db_session=session,
            data=payload,
            current_user=user,
            auto_managed=True,
        )

    @staticmethod
    def _refresh_or_create_auto_route_on_update(
        *,
        session: Session,
        install: Agent,
        revision: AgentBundleRevision,
    ) -> None:
        """Apply-update: refresh the auto-managed route off the new revision.

        - If an auto-managed route already exists, refresh its
          ``trigger_prompt`` and ``name`` from the new revision (but only
          if it's still flagged auto-managed; manual edits flip that to
          ``False`` and we leave them alone).
        - If no route exists yet AND the revision has a trigger prompt,
          create one — covers installs from before this feature shipped
          (Phase 8 backfill handles existing installs more thoroughly,
          but apply-update is a natural per-install retry point).
        - If the user already edited the route (``is_auto_managed=False``),
          do nothing — their override wins.
        """
        from app.models.app_mcp.app_agent_route import AppAgentRoute

        new_prompt = (revision.router_trigger_prompt or "").strip()

        # Look up the install's auto-managed route directly. Filtering
        # on ``is_auto_managed=True`` keeps a user-created sibling route
        # on the same agent (a developer who manually added an extra
        # route in the UI) from shadowing the lookup.
        existing_stmt = select(AppAgentRoute).where(
            AppAgentRoute.agent_id == install.id,
            AppAgentRoute.created_by == install.owner_id,
            AppAgentRoute.is_auto_managed == True,  # noqa: E712
        )
        existing = session.exec(existing_stmt).first()

        if existing is not None:
            if not new_prompt:
                # The revision dropped its trigger prompt; keep the old
                # route value rather than blanking a NOT NULL column.
                return
            changed = False
            if existing.trigger_prompt != new_prompt:
                existing.trigger_prompt = new_prompt
                changed = True
            if existing.name != install.name:
                existing.name = install.name
                changed = True
            if changed:
                existing.updated_at = datetime.now(UTC)
                session.add(existing)
                session.commit()
                session.refresh(existing)
            return

        # No auto-managed route yet. Before creating one, check whether
        # the installer already has a manual (``is_auto_managed=False``)
        # route on this agent — that's the "user flipped auto-managed
        # off via PUT" path, and the plan says their override wins.
        # Without this check we'd silently mint a second route alongside
        # the user's customised one on every apply-update.
        manual_stmt = select(AppAgentRoute).where(
            AppAgentRoute.agent_id == install.id,
            AppAgentRoute.created_by == install.owner_id,
            AppAgentRoute.is_auto_managed == False,  # noqa: E712
        )
        if session.exec(manual_stmt).first() is not None:
            return

        if not new_prompt:
            return
        InstallService._auto_create_app_mcp_route(
            session=session,
            install=install,
            revision=revision,
            user=session.get(User, install.owner_id),
        )

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
    def _normalise_credentials_payload(
        raw: dict | None,
        revision: AgentBundleRevision,
    ) -> dict[str, dict]:
        """Validate and pass through the install request's ``credentials`` body.

        Phase 5: only the typed :class:`InstallCredentialSelection` shape
        is accepted — the legacy ``{spec_name: uuid_string | dict}`` shim
        was sunset here. Each value must be a dict with a ``mode`` key in
        ``{"use_existing", "placeholder", "publisher_provides", "skip"}``;
        unknown modes are coerced to ``"placeholder"`` so a misconfigured
        client doesn't abort the install.
        """
        if not raw:
            return {}

        normalised: dict[str, dict] = {}
        for spec_name, value in raw.items():
            # Accept either an :class:`InstallCredentialSelection` instance
            # (when the route validated the body) or a plain dict (in-tree
            # callers that bypass FastAPI validation, e.g. service tests).
            if hasattr(value, "model_dump"):
                value_dict = value.model_dump()
            elif isinstance(value, dict):
                value_dict = value
            else:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Invalid credentials payload for spec '{spec_name}': "
                        "expected an object with a 'mode' field "
                        "({'use_existing','placeholder','publisher_provides','skip'})."
                    ),
                )

            if "mode" not in value_dict:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Invalid credentials payload for spec '{spec_name}': "
                        "missing 'mode' field."
                    ),
                )

            mode = value_dict.get("mode")
            if mode not in (
                "use_existing",
                "placeholder",
                "publisher_provides",
                "skip",
            ):
                # Unknown mode — coerce to placeholder so we don't reject
                # the install on a typo.
                normalised[spec_name] = {"mode": "placeholder"}
                continue
            normalised[spec_name] = {
                "mode": mode,
                "credential_id": value_dict.get("credential_id"),
            }
        return normalised

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
        we either link a foreign publisher-shared credential (when the
        spec is ``provided_by="publisher"``) or create a placeholder /
        link the installer's selection (``provided_by="user"``).

        Backward-compat reader: spec dicts authored before Phase 1 of the
        install-experience-redesign plan have no ``provided_by`` /
        ``publisher_credential_id`` fields. We default missing
        ``provided_by`` to ``"user"`` and missing ``publisher_credential_id``
        to ``None``, which keeps install-time behaviour unchanged for
        every pre-Phase-1 revision.

        Phase 3: ``user_provided_data`` arrives normalised via
        :meth:`_normalise_credentials_payload` — values are dicts with
        ``mode`` and optional ``credential_id``. Legacy single-string and
        raw-dict values from older clients are accepted there.

        Validation (plan §5):
          - ``mode="use_existing"`` for a publisher-provided spec is
            rejected with 422 (publisher specs are not user-overridable).
          - ``mode="use_existing"`` with a credential_id that doesn't
            belong to the installer falls back to placeholder (existing
            soft behaviour; surfacing a hard error here would break old
            tests sending best-effort uuids).

        Degradation, not failure: if the publisher branch fails (missing
        row, ``allow_sharing=false``, share creation hiccup) we log a
        warning, mark the install ``last_update_status="degraded"``, and
        fall through to the placeholder path. The runtime gate (Phase 4)
        will surface this to the user; Phase 2+ just keeps the install
        from aborting.
        """
        from app.models.credentials.credential import Credential, CredentialType
        from app.models.credentials.link_models import AgentCredentialLink

        degraded = False
        for raw_spec in revision.required_credential_specs or []:
            parsed = parse_credential_spec(raw_spec)
            if parsed is None:
                continue

            user_selection = (
                user_provided_data.get(parsed.name) if user_provided_data else None
            )

            # ── Template-provided branch ─────────────────────────────────
            # The publisher chose to ship non-private fields as template
            # defaults; the installer only needs to fill in the private
            # ones. We materialise a fresh Credential row owned by the
            # installer with the template_data pre-filled and
            # is_placeholder=True so the runtime gate keeps the install
            # in needs_setup until the private fields are supplied.
            #
            # If the installer explicitly opted to link an existing
            # credential of theirs (``mode="use_existing"``) we honour
            # that and skip the template materialisation — a fully-set-up
            # credential they already own beats a half-filled template.
            if parsed.provided_by == "template":
                wants_existing = (
                    isinstance(user_selection, dict)
                    and user_selection.get("mode") == "use_existing"
                    and user_selection.get("credential_id")
                )
                if not wants_existing:
                    if InstallService._materialise_template_credential(
                        session=session,
                        install=install,
                        parsed=parsed,
                    ):
                        continue
                    # Bad spec data falls through to a regular placeholder so
                    # the install still completes and the runtime gate guides
                    # the installer. Mark the install as degraded so the
                    # publisher can see template materialisation didn't take.
                    degraded = True
                    logger.warning(
                        "Failed to materialise template credential for spec '%s' "
                        "on install %s — falling back to placeholder",
                        parsed.name, install.id,
                    )

            # ── Publisher-provided branch ─────────────────────────────────
            if (
                parsed.provided_by == "publisher"
                and parsed.publisher_credential_id is not None
            ):
                # Validate: explicit user_existing override on a publisher
                # spec is not permitted.
                if (
                    isinstance(user_selection, dict)
                    and user_selection.get("mode") == "use_existing"
                ):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Spec '{parsed.name}' is provided by the publisher and "
                            "cannot be overridden with a personal credential. "
                            "Re-submit with mode='publisher_provides' or omit "
                            "the entry."
                        ),
                    )
                linked = InstallService._try_link_publisher_credential(
                    session=session,
                    install=install,
                    publisher_credential_id_raw=str(parsed.publisher_credential_id),
                    spec_name=parsed.name,
                )
                if linked:
                    continue
                # Fall through to placeholder; record degradation.
                degraded = True
                logger.warning(
                    "Falling back to placeholder for spec '%s' on install %s "
                    "(publisher credential %s unusable)",
                    parsed.name, install.id, parsed.publisher_credential_id,
                )

            # ── User-provided branch (default) ────────────────────────────
            mode: str = "placeholder"
            selected_credential_id: uuid.UUID | None = None
            if isinstance(user_selection, dict):
                mode = user_selection.get("mode") or "placeholder"
                cred_id_raw = user_selection.get("credential_id")
                if cred_id_raw:
                    try:
                        selected_credential_id = uuid.UUID(str(cred_id_raw))
                    except (ValueError, TypeError):
                        selected_credential_id = None

            # Treat publisher_provides on a user-spec as a no-op echo —
            # the user-branch fall-through creates the placeholder.
            if mode == "use_existing" and selected_credential_id:
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
                cred_type = CredentialType(parsed.type)
            except ValueError:
                logger.warning(
                    "Unknown credential type '%s' for spec '%s' — skipping",
                    parsed.type, parsed.name,
                )
                continue

            placeholder = Credential(
                owner_id=install.owner_id,
                name=f"{parsed.name} (placeholder)",
                type=cred_type,
                notes="Placeholder for required bundle credential.",
                encrypted_data=encrypt_field(json.dumps({})),
                is_placeholder=True,
                allow_sharing=False,
            )
            session.add(placeholder)
            session.flush()
            session.add(AgentCredentialLink(
                agent_id=install.id,
                credential_id=placeholder.id,
            ))
        session.commit()

        if degraded:
            install.last_update_status = "degraded"
            session.add(install)
            session.commit()
            session.refresh(install)

    @staticmethod
    def _materialise_template_credential(
        *,
        session: Session,
        install: Agent,
        parsed: ParsedCredentialSpec,
    ) -> bool:
        """Create a placeholder Credential seeded from a template spec.

        Reads ``template_data`` (publisher's non-private values) and
        ``template_private_fields`` (the field names the installer must
        supply). Persists a Credential row owned by the installer with:

          - ``encrypted_data`` initialised from ``template_data``
          - ``is_placeholder=True`` so the runtime gate keeps the install
            in needs_setup until the private fields are filled in
          - ``allow_sharing=False`` and ``allow_template_sharing=False``
            (the installer's row is private to them; downstream re-sharing
            requires an explicit toggle)
          - ``template_private_fields`` mirrored onto the installer's row
            so the setup page can highlight which fields are still empty

        Returns ``True`` on success; ``False`` if the spec lacks a usable
        type so the caller falls back to the regular placeholder path.
        """
        from app.models.credentials.credential import CredentialType

        name = parsed.name or "template credential"
        try:
            cred_type = CredentialType(parsed.type)
        except ValueError:
            return False

        cred = Credential(
            owner_id=install.owner_id,
            name=name,
            type=cred_type,
            notes=parsed.description or "Created from bundle template.",
            encrypted_data=encrypt_field(json.dumps(parsed.non_private_template_data)),
            is_placeholder=True,
            allow_sharing=False,
            allow_template_sharing=False,
            template_private_fields=parsed.template_private_fields,
        )
        session.add(cred)
        session.flush()
        session.add(AgentCredentialLink(
            agent_id=install.id,
            credential_id=cred.id,
        ))
        return True

    @staticmethod
    def _try_link_publisher_credential(
        *,
        session: Session,
        install: Agent,
        publisher_credential_id_raw: str,
        spec_name: str,
    ) -> bool:
        """Best-effort link of a publisher-shared service credential.

        Returns True on success, False on any validation failure (caller
        falls through to the placeholder path).

        Steps:
          1. Resolve the publisher's ``Credential`` row by id.
          2. Verify ownership matches the bundle publisher and the row
             still has ``allow_sharing=True`` (a publisher-revoked share
             is the most common breakage at this point).
          3. Ensure a ``CredentialShare`` exists from publisher to
             installer (idempotent — keys on credential_id +
             shared_with_user_id).
          4. Insert the ``AgentCredentialLink`` for the install.
        """
        from app.models.credentials.credential import Credential
        from app.models.credentials.credential_share import CredentialShare
        from app.models.credentials.link_models import AgentCredentialLink

        try:
            publisher_credential_id = uuid.UUID(str(publisher_credential_id_raw))
        except (ValueError, TypeError):
            logger.warning(
                "Invalid publisher_credential_id %r on spec '%s'",
                publisher_credential_id_raw, spec_name,
            )
            return False

        publisher_cred = session.get(Credential, publisher_credential_id)
        if publisher_cred is None:
            logger.warning(
                "Publisher credential %s missing for spec '%s' on install %s",
                publisher_credential_id, spec_name, install.id,
            )
            return False
        if not publisher_cred.allow_sharing:
            logger.warning(
                "Publisher credential %s no longer allows sharing for spec '%s' on install %s",
                publisher_credential_id, spec_name, install.id,
            )
            return False

        # Bundle publisher == credential owner is the trust boundary.
        bundle = session.get(AgentBundle, install.bundle_uuid) if install.bundle_uuid else None
        if bundle is not None and publisher_cred.owner_id != bundle.publisher_user_id:
            logger.warning(
                "Publisher credential %s not owned by bundle publisher %s "
                "(actual owner %s) — falling through for spec '%s'",
                publisher_credential_id, bundle.publisher_user_id,
                publisher_cred.owner_id, spec_name,
            )
            return False

        # Installer-as-publisher (publisher install) doesn't need a share.
        if publisher_cred.owner_id != install.owner_id:
            existing_share = session.exec(
                select(CredentialShare).where(
                    CredentialShare.credential_id == publisher_credential_id,
                    CredentialShare.shared_with_user_id == install.owner_id,
                )
            ).first()
            if existing_share is None:
                session.add(CredentialShare(
                    credential_id=publisher_credential_id,
                    shared_with_user_id=install.owner_id,
                    shared_by_user_id=publisher_cred.owner_id,
                    access_level="read",
                ))
                session.flush()

        # Idempotent link insertion (re-install hits this path again).
        existing_link = session.exec(
            select(AgentCredentialLink).where(
                AgentCredentialLink.agent_id == install.id,
                AgentCredentialLink.credential_id == publisher_credential_id,
            )
        ).first()
        if existing_link is None:
            session.add(AgentCredentialLink(
                agent_id=install.id,
                credential_id=publisher_credential_id,
            ))
        return True

    @staticmethod
    async def _link_publisher_ai_credential(
        *,
        session: Session,
        user: User,
        bundle: AgentBundle,
    ) -> None:
        """Materialise ``AICredentialShare`` rows for the bundle's PBP AI credentials.

        Idempotent: if the share already exists (or the user is the
        publisher) the call is a no-op. Called both at install time and
        at re-install time so a publisher who flips ``publisher_ai_credential_*_id``
        post-publish gets shares created on the next install / reinstall.
        """
        from app.services.credentials.ai_credentials_service import (
            ai_credentials_service,
        )

        for credential_id in (
            bundle.publisher_ai_credential_conversation_id,
            bundle.publisher_ai_credential_building_id,
        ):
            if credential_id is None:
                continue
            if bundle.publisher_user_id == user.id:
                # The publisher install never needs a share-with-self row.
                continue
            try:
                ai_credentials_service.share_credential(
                    session=session,
                    credential_id=credential_id,
                    owner_id=bundle.publisher_user_id,
                    recipient_id=user.id,
                )
            except HTTPException as exc:
                # Most likely: credential row vanished (FK is SET NULL on
                # the bundle but a small race remains) or some other
                # transient validation hiccup. Don't abort the install —
                # the env-side resolver will surface "no credential" via
                # the existing path and the runtime gate will catch it.
                logger.warning(
                    "Failed to ensure AI credential share for bundle %s "
                    "credential %s -> user %s: %s",
                    bundle.id, credential_id, user.id, exc.detail,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "Unexpected error sharing AI credential %s for bundle %s: %s",
                    credential_id, bundle.id, exc,
                )

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
            install.router_trigger_prompt = revision.router_trigger_prompt
            install.installed_revision_id = revision.id
            install.last_sync_at = datetime.now(UTC)
            install.last_update_status = "synced"
            install.pending_update = False
            install.pending_update_at = None
            session.add(install)
            session.commit()
            session.refresh(install)

            # Refresh the auto-managed App MCP route off the new revision.
            # User-edited routes (is_auto_managed=False) are left alone.
            # If no route exists yet (install pre-dates auto-routing, or
            # the previous revision shipped without a trigger prompt), we
            # create one now.
            try:
                InstallService._refresh_or_create_auto_route_on_update(
                    session=session,
                    install=install,
                    revision=revision,
                )
            except Exception as e:
                logger.warning(
                    "Failed to refresh auto-managed App MCP route for "
                    "install %s on apply-update: %s",
                    install.id, e,
                )

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

    # ── Publish settings ───────────────────────────────────────────

    @staticmethod
    def update_publish_settings(
        session: Session,
        install: Agent,
        *,
        credential_overrides: dict[str, dict] | None,
        ai_credentials: dict | None,
    ) -> Agent:
        """Validate and persist a partial update to ``install.publish_settings``.

        Both arguments are partial: ``None`` leaves that section
        untouched, an empty/populated value replaces it.

        - ``credential_overrides``: ``{spec_name: {"provided_by": ...}}``.
          Each key must match the name of a credential currently linked
          to the install; each ``provided_by`` must be ``"user"``,
          ``"publisher"``, or ``"template"``.
        - ``ai_credentials``: ``{"conversation_credential_id": uuid|None,
          "building_credential_id": uuid|None}``. Each non-null id must
          reference an ``AICredential`` owned by the install owner.

        Raises ``ValueError`` for any validation failure (route maps to
        HTTP 400). Caller must have already verified the install is the
        publisher install — this method asserts and raises ``ValueError``
        defensively if not.
        """
        if not install.is_publisher_install:
            raise ValueError(
                "Publish settings can only be edited on the publisher install."
            )

        current = dict(install.publish_settings or {})

        if credential_overrides is not None:
            current["credential_overrides"] = (
                InstallService._validate_credential_overrides(
                    session=session, install=install, overrides=credential_overrides
                )
            )

        if ai_credentials is not None:
            current["ai_credentials"] = (
                InstallService._validate_ai_credentials_draft(
                    session=session, install=install, draft=ai_credentials
                )
            )

        install.publish_settings = current
        session.add(install)
        session.commit()
        session.refresh(install)
        return install

    @staticmethod
    def _validate_credential_overrides(
        *,
        session: Session,
        install: Agent,
        overrides: dict[str, dict],
    ) -> dict[str, dict]:
        """Validate the ``credential_overrides`` map and return its cleaned form."""
        linked_names: set[str] = set()
        rows = session.exec(
            select(Credential)
            .join(
                AgentCredentialLink,
                AgentCredentialLink.credential_id == Credential.id,
            )
            .where(AgentCredentialLink.agent_id == install.id)
        ).all()
        for cred in rows:
            if cred.name:
                linked_names.add(cred.name)

        cleaned: dict[str, dict] = {}
        for spec_name, override in overrides.items():
            if spec_name not in linked_names:
                raise ValueError(
                    f"Override targets unknown spec '{spec_name}' — only "
                    "credentials currently linked to this install can be "
                    "overridden."
                )
            provided_by = override.get("provided_by") if isinstance(override, dict) else None
            if provided_by not in ("user", "publisher", "template"):
                raise ValueError(
                    f"Invalid provided_by '{provided_by}' for spec "
                    f"'{spec_name}': must be 'user', 'publisher', or 'template'."
                )
            cleaned[spec_name] = {"provided_by": provided_by}
        return cleaned

    @staticmethod
    def _validate_ai_credentials_draft(
        *,
        session: Session,
        install: Agent,
        draft: dict,
    ) -> dict:
        """Validate the AI-credentials draft and return its serialised form.

        Also enforces SDK ↔ credential provider match against the publisher
        install's active env, mirroring the post-publish check in
        :meth:`BundleService.update_bundle`. The draft is what gets copied
        onto the bundle row at first publish, so catching the mismatch here
        prevents shipping a broken bundle.
        """
        from app.services.environments.sdk_constants import (
            sdk_expected_credential_type,
        )

        env: AgentEnvironment | None = None
        if install.active_environment_id is not None:
            env = session.get(AgentEnvironment, install.active_environment_id)

        conversation_id = draft.get("conversation_credential_id")
        building_id = draft.get("building_credential_id")
        for slot_label, ai_cred_id, sdk_attr in (
            ("conversation", conversation_id, "agent_sdk_conversation"),
            ("building", building_id, "agent_sdk_building"),
        ):
            if ai_cred_id is None:
                continue
            ai_cred = session.get(AICredential, ai_cred_id)
            if ai_cred is None or ai_cred.owner_id != install.owner_id:
                raise ValueError(
                    f"AI credential for {slot_label} mode must reference "
                    "an AI credential you own."
                )
            if env is not None:
                sdk_id = getattr(env, sdk_attr, None)
                expected = sdk_expected_credential_type(sdk_id)
                if expected is None:
                    continue
                cred_type_value = (
                    ai_cred.type.value if hasattr(ai_cred.type, "value")
                    else str(ai_cred.type)
                )
                expected_value = (
                    expected.value if hasattr(expected, "value") else str(expected)
                )
                if cred_type_value != expected_value:
                    raise ValueError(
                        f"AI credential for {slot_label} mode is of type "
                        f"'{cred_type_value}', but the env's {slot_label} "
                        f"SDK '{sdk_id}' requires a '{expected_value}' "
                        "credential. Switch the env's SDK provider or pick a "
                        "matching credential."
                    )
        return {
            "conversation_credential_id": (
                str(conversation_id) if conversation_id is not None else None
            ),
            "building_credential_id": (
                str(building_id) if building_id is not None else None
            ),
        }

    # ── Setup credentials ──────────────────────────────────────────

    @staticmethod
    def list_setup_credentials(
        session: Session, install: Agent
    ) -> list[SetupCredentialSummary]:
        """List the install's user-fillable placeholder credentials.

        Returns rows owned by the install owner AND linked to the install
        AND ``is_placeholder=True``. For credentials materialised from a
        bundle template, ``template_private_fields`` lists the field names
        the installer is expected to fill in and ``template_prefilled_data``
        carries the publisher's non-private values so the setup page can
        render them as read-only context.

        Decryption failures fall back to an empty prefilled dict — a
        corrupted credential should still surface in the setup list so
        the installer can re-fill it from scratch rather than disappear.
        """
        from app.services.credentials.credentials_service import CredentialsService

        rows = session.exec(
            select(Credential)
            .join(
                AgentCredentialLink,
                AgentCredentialLink.credential_id == Credential.id,
            )
            .where(
                AgentCredentialLink.agent_id == install.id,
                Credential.owner_id == install.owner_id,
                Credential.is_placeholder == True,  # noqa: E712
            )
        ).all()

        summaries: list[SetupCredentialSummary] = []
        for cred in rows:
            private_fields = list(cred.template_private_fields or [])
            prefilled: dict = {}
            if private_fields:
                try:
                    full = CredentialsService.decrypt_credential_data(
                        session=session, credential=cred
                    )
                except Exception:
                    full = {}
                private_set = set(private_fields)
                prefilled = {k: v for k, v in full.items() if k not in private_set}
            summaries.append(
                SetupCredentialSummary(
                    id=cred.id,
                    name=cred.name,
                    type=cred.type.value if hasattr(cred.type, "value") else str(cred.type),
                    description=cred.notes,
                    template_private_fields=private_fields,
                    template_prefilled_data=prefilled,
                )
            )
        return summaries
