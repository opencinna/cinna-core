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
import asyncio
import copy
import json
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, select
from fastapi import HTTPException

from app.core.config import settings
from app.core.db import create_session, engine
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
from app.models.credentials.ai_credential import AICredential, AICredentialType
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
from app.services.environments.sdk_constants import DEFAULT_SDK

logger = logging.getLogger(__name__)


# Environment statuses on which the automatic-update sweep is allowed to act.
# Deliberately an allowlist: everything not listed here — "running", "error",
# and every transitional status ("creating", "building", "initializing",
# "starting", "rebuilding", "activating") — is skipped. Applying to a running
# env would disrupt a live stream; applying to a transitional one would race the
# lifecycle operation that currently owns the workspace directory.
AUTO_UPDATE_ALLOWED_ENV_STATUSES = frozenset({"suspended", "stopped"})


def _as_utc(value: datetime) -> datetime:
    """Interpret a possibly-naive DB timestamp as UTC.

    The install bookkeeping columns (``last_update_attempt_at`` and friends)
    are ``TIMESTAMP WITHOUT TIME ZONE`` but always written from
    ``datetime.now(UTC)``, so a naive value read back is UTC wall-clock.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# Stable, arbitrary 64-bit key for the Postgres advisory lock that makes the
# auto-update sweep single-leader. Shared by BOTH entry points (the periodic
# scheduler and the publish-time fast path) so they can never apply the same
# install concurrently — ``apply_update`` copies a bundle snapshot into the
# environment's instance directory, and two of those racing on one directory
# is not something the row lock below can prevent (it is released by the
# attempt-stamp commit that must happen before the apply).
BUNDLE_AUTO_UPDATE_LOCK_KEY = 0x42554E444C4155  # "BUNDLAU"


@contextmanager
def sweep_leader_session():
    """Yield a session that holds the sweep's leader lock, or ``None``.

    ``None`` means another process already holds the lock and this run should
    skip.

    The lock has to live on the *same physical connection* for its whole life:
    ``pg_try_advisory_lock`` is connection-scoped, while a ``Session`` bound to
    an **engine** hands its connection back to the pool at every ``commit()``.
    Since the sweep commits once per install, an engine-bound session would
    strand the lock on a pooled connection — the ``pg_advisory_unlock`` then
    returns false, the lock is never released, and every subsequent run is
    locked out forever (i.e. the feature would silently disable itself after
    its first productive run). Binding the ``Session`` to an explicit
    ``engine.connect()`` pins it.
    """
    if settings.TESTING:
        # Under test there is no cross-process concurrency to guard against,
        # and the harness patches ``create_session`` to hand back the
        # rolled-back test transaction. Checking out a real pooled connection
        # here would escape that isolation and write to the live database.
        with create_session() as session:
            yield session
        return

    with engine.connect() as connection:
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": BUNDLE_AUTO_UPDATE_LOCK_KEY},
        ).scalar_one()
        connection.commit()
        if not acquired:
            yield None
            return
        try:
            with Session(bind=connection) as session:
                yield session
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:k)"),
                {"k": BUNDLE_AUTO_UPDATE_LOCK_KEY},
            )
            connection.commit()


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

        # Idempotent: re-use the user's existing **consumer** install if
        # any. We deliberately exclude the publisher install — installing
        # one's own bundle as a consumer is a legitimate dogfood path
        # that materialises a separate consumer-slot install. Without
        # this filter the publisher would be short-circuited back into
        # their dev / source copy.
        existing_stmt = select(Agent).where(
            Agent.bundle_uuid == bundle.id,
            Agent.owner_id == user.id,
            Agent.is_publisher_install == False,  # noqa: E712
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
    def _apply_revision_metadata(
        install: Agent,
        revision: AgentBundleRevision,
        *,
        skip_fields: set[str] | None = None,
    ) -> None:
        """Copy agent-row definitional metadata from a revision onto an install.

        Publisher-authoritative + missing-key-tolerant (agent-metadata-snapshot
        plan, locked decisions 1 & 2): each field is overwritten ONLY when the
        revision actually carries it (``is not None``). A field absent from an
        older snapshot (NULL column / pre-existing revision) leaves the install's
        current value untouched — it is never clobbered to null. Shared by fresh
        install / checkout, apply-update, and git pull so all three restore paths
        stay identical. Tokens / grants / per-install UI prefs are deliberately
        NOT part of this set (see plan exclusions).

        ``skip_fields`` names raw attributes to leave alone regardless of what
        the revision carries — the git pull ``keep_local`` resolution, where the
        user chose to keep their drifted values. It is symmetric with (and
        applied on top of) the per-field ``is not None`` guard; ``None``
        (the default) preserves the behavior every other caller relies on.
        """
        skip = skip_fields or frozenset()

        def _restore(field: str, *, deep: bool = False) -> None:
            # Reads through ``getattr`` on the REVISION so a misspelled name
            # fails loud here (AttributeError) instead of silently no-op'ing —
            # ``setattr`` on a SQLModel instance would happily create a junk
            # attribute. Every name below must also appear in
            # ``git_source_service._METADATA_FIELDS``, or git pull's
            # ``keep_local`` would preserve a field this restores (or vice versa).
            value = getattr(revision, field)
            if value is None or field in skip:
                return
            # Deep-copy the mutable JSON payloads so the install never aliases the
            # (immutable) revision row's object — a later in-place edit of the
            # install's config must not reach back into the snapshot it came from.
            setattr(install, field, copy.deepcopy(value) if deep else value)

        _restore("description")
        _restore("example_prompts", deep=True)
        _restore("status_refresh_command")
        _restore("agent_api_enabled")
        _restore("agent_api_identity_enabled")
        _restore("a2a_config", deep=True)
        _restore("agent_sdk_config", deep=True)
        _restore("webapp_enabled")

    @staticmethod
    def _importable_model_override(
        override: str | None,
        effective_sdk: str | None,
        context: str = "",
    ) -> str | None:
        """Filter a publisher-authored per-mode model override before import.

        An ``openai_compatible`` model id names a model inside the *endpoint
        owner's* namespace — it is only meaningful against the ``base_url`` of
        the credential serving that mode. The consumer runs against their OWN
        ``openai_compatible`` credential: a different endpoint, a different
        model catalogue. So a model id the publisher pinned is very likely not
        served there, and it would win anyway — ``resolve_model`` honours any
        truthy override verbatim and returns BEFORE its ``openai_compatible``
        branch, so an imported id outranks the consumer's own
        ``openai_compatible_model``. The result is a hard provider error on the
        first message, behind a green badge (``model_health_service`` reports
        ``openai_compatible`` as always OK, on the assumption that whoever set
        the model also owns the endpoint — exactly what importing breaks).

        Therefore: for a mode that resolves to ``openai_compatible`` we drop the
        imported override and let the installer's own fallback chain decide (see
        below). Every other provider names models in a shared, portable
        namespace, so the publisher's pin is kept.

        This is deliberately scoped to IMPORTED (publisher-authored) overrides
        and MUST NOT be folded into ``resolve_model``: that code path cannot
        tell who authored an override, so the same rule there would also break
        the legitimate case of a user deliberately pinning a model on their own
        ``openai_compatible`` environment.

        What takes the pin's place is ``EnvironmentService.create_environment``'s
        pre-existing chain, which passing ``None`` here hands control to:
        the installer's own ``User.default_model_override_*`` when they have one
        set (the new-environment form pre-fills from it), and only when that is
        NULL does the serving credential's configured ``model`` stand. So even
        when the bundle ships the publisher's own AI credential
        (publisher-provided credentials), the outcome is the publisher's default
        on that endpoint only for an installer with no personal global default —
        otherwise it is that personal default. Either way it is a model the
        installer's own account chose, which is the point; the per-user
        substitution itself is how every environment this user creates already
        behaves and is not something this filter introduces.

        Note this also fires on self-checkout: ``checkout`` of a Git repo you
        authored yourself installs through the same path, so an
        ``openai_compatible`` override you set is dropped even though publisher
        and consumer are the same person on the same endpoint. That is accepted
        — the override lands in a durable environment row while the credential
        binding behind it stays mutable, so a later swap to a different
        ``openai_compatible`` credential would carry the pin onto a foreign
        endpoint. Keeping the suppression unconditional avoids that.
        """
        if not override:
            return None
        # Same split resolve_model / the lifecycle use: a missing SDK or a bare
        # engine means the default ``anthropic`` provider.
        _engine, _, provider = (effective_sdk or "").partition("/")
        if (provider or "anthropic") != AICredentialType.OPENAI_COMPATIBLE.value:
            return override
        logger.info(
            "Dropping imported model override '%s' — mode resolves to SDK '%s' "
            "(openai_compatible model ids are endpoint-local and not portable "
            "from publisher to consumer) [%s]",
            override,
            effective_sdk,
            context,
        )
        return None

    @staticmethod
    async def _install_from_revision(
        *,
        session: Session,
        user: User,
        bundle: AgentBundle,
        revision: AgentBundleRevision,
        request: InstallRequest | None,
    ) -> Agent:
        # Always produces a consumer install (``is_publisher_install=False``).
        # Publisher install rows are created by the publish flow's in-place
        # promotion path (``PublishService.publish`` flips the flag on an
        # existing Agent row), not here.
        from app.services.bundles.app_data_service import AppDataService
        from app.services.bundles.bundle_id_service import BundleIdService
        from app.services.environments.environment_service import EnvironmentService

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
            is_publisher_install=False,
            update_mode=bundle.default_install_mode or BundleInstallMode.MANUAL,
            last_sync_at=datetime.now(UTC),
            last_update_status="synced",
        )
        # Restore agent-row definitional metadata captured in the revision.
        # ``description`` was seeded from ``bundle.description`` above as a
        # display fallback; this overwrites it with the revision's value when
        # the snapshot carries one (and likewise fills the other definitional
        # columns, leaving the Agent defaults in place for any field an older
        # snapshot omits).
        InstallService._apply_revision_metadata(install, revision)
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

        # 2b. Decide which of the revision's per-mode model overrides may be
        # imported. A mode's provider comes from its SDK id ("engine/provider"),
        # so resolve the EFFECTIVE per-mode SDK first, mirroring
        # ``EnvironmentService.create_environment``'s normalisation: a NULL
        # conversation SDK falls back to the installer's own default, while a
        # NULL building SDK means "building mode not needed" and stays NULL (the
        # lifecycle then reads it as claude-code/anthropic). Each mode is judged
        # independently — building and conversation can resolve to different
        # providers, so one may be suppressed while the other is kept.
        effective_sdk_conversation = (
            revision.agent_sdk_conversation
            or user.default_sdk_conversation
            or DEFAULT_SDK
        )
        effective_sdk_building = (
            None
            if revision.agent_sdk_building is None
            else (
                revision.agent_sdk_building
                or user.default_sdk_building
                or DEFAULT_SDK
            )
        )
        override_log_context = (
            f"install={install.id} bundle={bundle.bundle_id} "
            f"revision={revision.id} user={user.id}"
        )
        model_override_conversation = InstallService._importable_model_override(
            revision.model_override_conversation,
            effective_sdk_conversation,
            context=f"mode=conversation {override_log_context}",
        )
        model_override_building = InstallService._importable_model_override(
            revision.model_override_building,
            effective_sdk_building,
            context=f"mode=building {override_log_context}",
        )

        # 2c. Build environment (uses the revision's full SDK block).
        # The per-mode model overrides travel with the engine selection: they are
        # written into the manifest's ``sdk`` block by
        # ``RevisionFormat.build_manifest`` and stored on the revision, so
        # dropping them wholesale here would silently discard half of what the
        # publisher pinned (only the non-portable ``openai_compatible`` case is
        # filtered above). ``create_environment`` still falls back to the
        # installer's own ``User.default_model_override_*`` when the revision
        # leaves them NULL — or when the filter suppressed them.
        env_data = AgentEnvironmentCreate(
            env_name=settings.DEFAULT_AGENT_ENV_NAME,
            env_version=settings.DEFAULT_AGENT_ENV_VERSION,
            instance_name="Default",
            type="docker",
            config={},
            agent_sdk_conversation=revision.agent_sdk_conversation,
            agent_sdk_building=revision.agent_sdk_building,
            model_override_conversation=model_override_conversation,
            model_override_building=model_override_building,
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
                bundle_snapshot_path=revision.snapshot_path,
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

        # 3. Workspace seeding from the revision snapshot is performed INSIDE
        # the background env build (``EnvironmentService._create_environment_background``
        # seeds after ``create_environment_instance`` materialises the instance
        # dir and before the container starts) — passed via
        # ``bundle_snapshot_path`` above. Seeding there closes the historical
        # race where a foreground seed here either found no instance dir yet
        # (no-op) or was clobbered by the async template materialisation,
        # shipping installs with empty bundle-owned dirs (e.g. ``scripts/``).

        # 4. Ensure / reattach app-data volume. Slot by source so the
        # publisher's dev / source copy (NULL slot) and a consumer install
        # ("server" slot) of the same bundle stay separate on disk.
        # ``EnvironmentLifecycleManager`` also lazily creates / reattaches
        # the volume during compose generation and uses the same slot
        # policy, so this call is mostly belt-and-braces: it guarantees the
        # row exists even if the env never reaches compose-generation
        # (e.g. env creation succeeded but the background build is still
        # pending when the installer reads the install).
        try:
            AppDataService.get_or_create_volume(
                session,
                user_id=user.id,
                bundle_id=install.bundle_id,
                current_install_id=install.id,
                catalog_type=install.app_data_catalog_type,
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

        # 6. Materialise the publisher's schedules onto the install.
        # Best-effort: a failure here logs a warning and marks the install
        # degraded but does NOT abort the install. The created rows are
        # ordinary ``AgentSchedule`` rows and are polled / executed by the
        # background scheduler unchanged.
        try:
            InstallService._materialise_schedules(
                session=session,
                install=install,
                revision=revision,
            )
        except Exception as e:
            logger.warning(
                "Failed to materialise schedules for install %s: %s",
                install.id, e,
            )
            try:
                install.last_update_status = "degraded"
                session.add(install)
                session.commit()
                session.refresh(install)
            except Exception:
                session.rollback()

        # 7. Materialise the publisher's plugins as source=bundle links. The
        # plugin files were already seeded into the env workspace in step 3;
        # these links drive the manifest (git=null) so the container install
        # routine marks them installed without a marketplace fetch. Best-effort:
        # a failure logs a warning and marks the install degraded.
        try:
            InstallService._materialise_plugin_links(
                session=session,
                install=install,
                revision=revision,
            )
        except Exception as e:
            logger.warning(
                "Failed to materialise plugin links for install %s: %s",
                install.id, e,
            )
            try:
                install.last_update_status = "degraded"
                session.add(install)
                session.commit()
                session.refresh(install)
            except Exception:
                session.rollback()

        return install

    @staticmethod
    def _materialise_schedules(
        *,
        session: Session,
        install: Agent,
        revision: AgentBundleRevision,
    ) -> None:
        """Create the install's schedules from the revision snapshot.

        Thin wrapper over ``schedule_sync.materialise`` that owns the commit
        — the helper only stages rows so the install flow controls the
        transaction boundary. Exceptions propagate to the best-effort call
        site, which marks the install degraded.
        """
        from app.services.bundles import schedule_sync

        created = schedule_sync.materialise(session, install, revision)
        if created:
            session.commit()

    @staticmethod
    def _materialise_plugin_links(
        *,
        session: Session,
        install: Agent,
        revision: AgentBundleRevision,
    ) -> None:
        """Create the install's ``source=bundle`` plugin links from the revision.

        Thin wrapper over ``plugin_sync.materialise`` that owns the commit — the
        helper only stages rows so the install flow controls the transaction
        boundary. The plugin *files* are already seeded into the env workspace by
        ``seed_workspace_from_bundle_snapshot``; the first container setup builds
        the manifest from these links (source=bundle, git=null) and the install
        routine marks them installed without any fetch. Exceptions propagate to
        the best-effort call site, which marks the install degraded.
        """
        from app.services.bundles import plugin_sync

        created = plugin_sync.materialise(session, install, revision)
        if created:
            session.commit()

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
                # Accept the credential when the installer owns it OR when it has
                # been explicitly shared with them (CredentialShare row exists).
                # The latter covers the per-user-scoped second-token flow where
                # the publisher pre-shares a slot-tagged credential before install.
                if selected:
                    installer_owns = selected.owner_id == install.owner_id
                    installer_has_share = False
                    if not installer_owns:
                        from app.models.credentials.credential_share import CredentialShare
                        installer_has_share = session.exec(
                            select(CredentialShare).where(
                                CredentialShare.credential_id == selected.id,
                                CredentialShare.shared_with_user_id == install.owner_id,
                            )
                        ).first() is not None
                    if installer_owns or installer_has_share:
                        session.add(AgentCredentialLink(
                            agent_id=install.id,
                            credential_id=selected.id,
                        ))
                        continue
                logger.warning(
                    "Credential %s not owned by or shared with install owner %s "
                    "— falling back to placeholder",
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
          - ``service_uri`` (a non-secret slot id) copied from the spec like
            a non-private template field, unless the publisher marked it
            private via ``template_private_fields``

        Returns ``True`` on success; ``False`` if the spec lacks a usable
        type so the caller falls back to the regular placeholder path.
        """
        from app.models.credentials.credential import CredentialType

        name = parsed.name or "template credential"
        try:
            cred_type = CredentialType(parsed.type)
        except ValueError:
            return False

        # service_uri is a non-secret slot id, not a credential_data field.
        # Copy it as a shared template default unless the publisher listed
        # "service_uri" in template_private_fields (installer provides),
        # mirroring how non_private_template_data strips private fields.
        service_uri = (
            None
            if "service_uri" in parsed.template_private_fields
            else parsed.service_uri
        )

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
            service_uri=service_uri,
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
                # First-writer-wins: stamp provenance only on insert. A
                # pre-existing direct share (source="direct") is left untouched,
                # so the credential stays under "My Credentials" rather than
                # being flipped to "bundle_install".
                session.add(CredentialShare(
                    credential_id=publisher_credential_id,
                    shared_with_user_id=install.owner_id,
                    shared_by_user_id=publisher_cred.owner_id,
                    access_level="read",
                    source="bundle_install",
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
                # Full workspace tree copy — off the event loop. The publish
                # fast path runs this sweep as a background task on the request
                # loop, up to BUNDLE_AUTO_UPDATE_BATCH_LIMIT times in a row,
                # and the owner-triggered POST /apply-update route runs it
                # inline; either way an inline copy blocks every other request
                # on the worker. Mirrors the ``asyncio.to_thread`` treatment
                # ``PublishService._write_snapshot_to_disk`` already gets.
                await asyncio.to_thread(
                    replace_bundle_content, Path(revision.snapshot_path), env.id
                )
                # The snapshot just overwrote the env prompt files with the new
                # revision content. Reset the prompt-sync baselines to None so
                # the reconcile that runs on the next env start treats the DB as
                # authoritative (SEED_PUSH) and does NOT pull the freshly-applied
                # env files back as a stale env-side change, reverting the update.
                env.workflow_prompt_synced_hash = None
                env.entrypoint_prompt_synced_hash = None
                env.refiner_prompt_synced_hash = None
                session.add(env)

            install.workflow_prompt = revision.workflow_prompt
            install.entrypoint_prompt = revision.entrypoint_prompt
            install.refiner_prompt = revision.refiner_prompt
            install.router_trigger_prompt = revision.router_trigger_prompt
            # Overwrite the agent-row definitional metadata from the new revision
            # (publisher-authoritative), but only for the fields the revision
            # actually carries — an older snapshot that omits a field leaves the
            # consumer's current value untouched.
            InstallService._apply_revision_metadata(install, revision)
            # The DB just authoritatively changed — bump the per-prompt logical
            # clocks so the prompt-sync reconcile treats the revision content as
            # the newest DB-side edit (out-ranking any stale env mtime in LWW).
            prompt_now = datetime.now(UTC)
            install.workflow_prompt_updated_at = prompt_now
            install.entrypoint_prompt_updated_at = prompt_now
            install.refiner_prompt_updated_at = prompt_now
            install.installed_revision_id = revision.id
            install.last_sync_at = datetime.now(UTC)
            install.last_update_status = "synced"
            install.pending_update = False
            install.pending_update_at = None
            session.add(install)
            session.commit()
            session.refresh(install)

            # Merge the install's schedules against the new revision.
            # Behaviorally-unchanged schedules keep the user's enable/disable
            # toggle (and execution history); changed / removed schedules are
            # reinstalled / deleted. Best-effort: a failure here logs a
            # warning but does not fail the update.
            try:
                from app.services.bundles import schedule_sync

                schedule_sync.merge(session, install, revision)
            except Exception as e:
                logger.warning(
                    "Failed to merge schedules for install %s on "
                    "apply-update: %s",
                    install.id, e,
                )

            # Merge the install's bundle plugin links against the new revision.
            # Behaviorally-unchanged plugins keep the user's enable/disable +
            # per-mode toggles; changed / removed plugins are reinstalled /
            # deleted. ONLY source=bundle links are touched — the consumer's own
            # source=marketplace plugins are left alone. The plugin files were
            # already re-seeded by replace_bundle_content above. Best-effort.
            try:
                from app.services.bundles import plugin_sync

                plugin_sync.merge(session, install, revision)
            except Exception as e:
                logger.warning(
                    "Failed to merge plugins for install %s on "
                    "apply-update: %s",
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

    # ── Automatic update convergence ──────────────────────────────

    @staticmethod
    async def sweep_automatic_updates(
        session: Session,
        *,
        bundle: AgentBundle | None = None,
        limit: int = 50,
    ) -> dict:
        """Apply pending revisions to automatic-mode installs whose env is not live.

        The suspension-time hook in ``environment_suspension_scheduler`` only
        fires on the running → suspended transition, so an install whose
        environment was *already* suspended when a revision was published never
        converges. This sweep closes that gap and is the single implementation
        shared by the periodic scheduler and the publish-time fast path.

        Selection is on **revision mismatch**, not ``pending_update`` — the
        sweep is self-healing if the publish-time notification was lost.

        Args:
            session: Database session (owned by the caller; the sweep commits).
            bundle: Restrict the sweep to a single bundle (publish fast path).
                ``None`` sweeps the whole fleet.
            limit: Maximum number of installs to *attempt* in this batch. Rows
                beyond the limit are logged and picked up by the next run.

        Returns:
            ``{"applied": int, "skipped": int, "failed": int, "deferred": int}``
        """
        backoff_cutoff = datetime.now(UTC) - timedelta(
            hours=settings.BUNDLE_AUTO_UPDATE_RETRY_BACKOFF_HOURS
        )

        # Single joined query — no per-row bundle/env lookup. The LEFT JOIN on
        # ``active_environment_id`` gives us the gating status inline; installs
        # with no environment at all come back with ``env_id is None``.
        #
        # Scalar columns, not ORM entities, on purpose: the loop below commits
        # per install, and ``expire_on_commit`` would expire every still-unvisited
        # entity, turning the batch back into the per-row SELECT this query
        # exists to avoid (and risking ObjectDeletedError if an install is
        # uninstalled mid-sweep). Plain tuples are immune; the ``Agent`` row is
        # loaded once, only for installs we are actually about to mutate.
        query = (
            select(
                Agent.id,  # type: ignore[arg-type]
                Agent.last_update_status,  # type: ignore[arg-type]
                Agent.last_update_attempt_at,  # type: ignore[arg-type]
                Agent.active_environment_id,  # type: ignore[arg-type]
                AgentEnvironment.id,  # type: ignore[arg-type]
                AgentEnvironment.status,  # type: ignore[arg-type]
            )
            .join(AgentBundle, AgentBundle.id == Agent.bundle_uuid)
            .outerjoin(
                AgentEnvironment,
                AgentEnvironment.id == Agent.active_environment_id,
            )
            .where(
                Agent.is_publisher_install == False,  # noqa: E712
                Agent.update_mode == BundleInstallMode.AUTOMATIC,
                AgentBundle.latest_revision_id.is_not(None),  # type: ignore[union-attr]
                Agent.installed_revision_id.is_distinct_from(  # type: ignore[union-attr]
                    AgentBundle.latest_revision_id
                ),
            )
            .order_by(Agent.created_at)  # type: ignore[arg-type]
        )
        if bundle is not None:
            query = query.where(AgentBundle.id == bundle.id)

        candidates = list(session.exec(query).all())
        if not candidates:
            return {"applied": 0, "skipped": 0, "failed": 0, "deferred": 0}

        applied = 0
        skipped = 0
        failed = 0
        deferred = 0

        for index, row in enumerate(candidates):
            (
                install_id,
                last_update_status,
                last_update_attempt_at,
                active_environment_id,
                env_id,
                env_status,
            ) = row

            if applied + failed >= limit:
                logger.info(
                    "Bundle auto-update sweep hit the batch limit of %s; "
                    "%s matching install(s) were not examined and are left "
                    "for the next run",
                    limit,
                    len(candidates) - index,
                )
                break

            try:
                # Failure backoff — don't retry a persistently failing install
                # on every sweep.
                if (
                    last_update_status == "failed"
                    and last_update_attempt_at is not None
                    and _as_utc(last_update_attempt_at) > backoff_cutoff
                ):
                    deferred += 1
                    logger.debug(
                        "Deferring automatic update for install %s: last "
                        "attempt at %s failed and is inside the %sh backoff",
                        install_id,
                        last_update_attempt_at,
                        settings.BUNDLE_AUTO_UPDATE_RETRY_BACKOFF_HOURS,
                    )
                    continue

                if env_id is None:
                    if active_environment_id is not None:
                        # Dangling reference — apply_update degrades to the
                        # DB-only path, which is exactly what we want here.
                        logger.warning(
                            "Install %s references missing environment %s; "
                            "applying update on the DB-only path",
                            install_id,
                            active_environment_id,
                        )
                elif env_status not in AUTO_UPDATE_ALLOWED_ENV_STATUSES:
                    skipped += 1
                    logger.debug(
                        "Skipping automatic update for install %s: env %s "
                        "status '%s' is not in the allowlist %s",
                        install_id,
                        env_id,
                        env_status,
                        sorted(AUTO_UPDATE_ALLOWED_ENV_STATUSES),
                    )
                    continue

                if env_id is not None:
                    # Re-read the env status under a row lock. The batch query
                    # and this apply are seconds apart and the env may have been
                    # activated in between; this shrinks the activation race to
                    # the workspace copy itself without introducing a new env
                    # status value. ``skip_locked`` keeps a concurrent lifecycle
                    # transaction from blocking the whole batch (and, in the
                    # scheduler, from blocking it while holding the leader lock)
                    # — a row someone else has locked is one we should not touch
                    # anyway.
                    locked_status = session.exec(
                        select(AgentEnvironment.status)  # type: ignore[arg-type]
                        .where(AgentEnvironment.id == env_id)
                        .with_for_update(skip_locked=True)
                    ).first()
                    if locked_status not in AUTO_UPDATE_ALLOWED_ENV_STATUSES:
                        session.rollback()  # release the row lock
                        skipped += 1
                        logger.debug(
                            "Skipping automatic update for install %s: env %s "
                            "is %s",
                            install_id,
                            env_id,
                            (
                                f"now in status '{locked_status}'"
                                if locked_status is not None
                                else "locked by another transaction or deleted"
                            ),
                        )
                        continue

                install = session.get(Agent, install_id)
                if install is None:
                    # Uninstalled between the batch query and now.
                    session.rollback()
                    skipped += 1
                    logger.debug(
                        "Skipping automatic update for install %s: row is gone",
                        install_id,
                    )
                    continue

                # Record the attempt BEFORE applying, so a crash mid-apply still
                # lands in the backoff window. This commit also releases the
                # row lock taken above.
                install.last_update_attempt_at = datetime.now(UTC)
                session.add(install)
                session.commit()

                await InstallService.apply_update(session, install)
                applied += 1
                logger.info(
                    "Bundle auto-update applied for install %s (bundle %s)",
                    install.id,
                    install.bundle_id,
                )
            except Exception as e:  # noqa: BLE001 — one failure must not abort
                failed += 1
                session.rollback()
                # apply_update stamps last_update_status="failed" itself, but
                # only for errors raised inside its own try block — its early
                # guard clauses (e.g. a dangling latest_revision_id) raise
                # before that. Without a "failed" marker the backoff never
                # engages and the install is retried on every sweep forever, so
                # make the sweep authoritative about its own bookkeeping.
                InstallService._mark_update_failed(session, install_id)
                logger.error(
                    "Bundle auto-update failed for install %s: %s",
                    install_id,
                    e,
                    exc_info=True,
                )

        result = {
            "applied": applied,
            "skipped": skipped,
            "failed": failed,
            "deferred": deferred,
        }
        if applied or failed:
            logger.info("Bundle auto-update sweep complete: %s", result)
        else:
            logger.debug("Bundle auto-update sweep complete: %s", result)
        return result

    @staticmethod
    def _mark_update_failed(session: Session, install_id: uuid.UUID) -> None:
        """Best-effort failure bookkeeping after a sweep error.

        Stamps **both** halves the backoff gate reads — ``last_update_status``
        and ``last_update_attempt_at`` — unconditionally, so that *every* failed
        attempt restarts the backoff window no matter which stage it failed at.

        Both writes are needed, and both must be unconditional:

        - A failure raised *before* the attempt-stamp commit (from the row-lock
          re-read, or ``session.get``) never stamped ``last_update_attempt_at``
          at all, so without this the gate — which requires both halves — never
          engages and the install is retried on every sweep forever.
        - Refreshing an already-``failed`` row matters just as much. If the
          timestamp were left at the value written by the *first* failure, the
          row would be deferred for one backoff window and then, once that
          window expired, be retried every sweep forever — the same hot loop,
          merely postponed.

        The redundant write on the normal path (where the sweep already stamped
        the attempt seconds earlier) costs one UPDATE on a row that is failing
        anyway; correctness of the gate is worth more than avoiding it.

        Swallows its own errors: this runs on the failure path and must never
        mask the original exception or abort the batch.
        """
        try:
            install = session.get(Agent, install_id)
            if install is None:
                return
            install.last_update_status = "failed"
            install.last_update_attempt_at = datetime.now(UTC)
            session.add(install)
            session.commit()
        except Exception as e:  # noqa: BLE001 — failure path, stay quiet
            logger.warning(
                "Could not mark install %s as failed after a sweep error: %s",
                install_id, e,
            )
            session.rollback()

    @staticmethod
    async def sweep_bundle_updates_background(bundle_uuid: uuid.UUID) -> None:
        """Bundle-scoped sweep for detached background use (publish fast path).

        Used by ``PublishService.notify_installs`` so a publish returns
        immediately and never fails because of a sweep. Must not reuse the
        request session — the task outlives the request — so it takes the same
        leader-locked session the periodic scheduler uses, which also keeps the
        two entry points from applying the same install concurrently. If the
        periodic sweep happens to be running, this one skips and the install
        converges on the next scheduled run instead.
        """
        if not settings.BUNDLE_AUTO_UPDATE_ENABLED:
            return

        with sweep_leader_session() as session:
            if session is None:
                logger.info(
                    "Publish-time auto-update sweep for bundle %s skipped: a "
                    "sweep is already running; the periodic run will converge it",
                    bundle_uuid,
                )
                return
            bundle = session.get(AgentBundle, bundle_uuid)
            if bundle is None:
                logger.warning(
                    "Publish-time auto-update sweep skipped: bundle %s not found",
                    bundle_uuid,
                )
                return
            await InstallService.sweep_automatic_updates(
                session,
                bundle=bundle,
                limit=settings.BUNDLE_AUTO_UPDATE_BATCH_LIMIT,
            )

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
        installed_version: str | None = None
        latest_version: str | None = None
        latest_release_notes: str | None = None
        latest_published_at: datetime | None = None
        if install.installed_revision_id:
            rev = session.get(AgentBundleRevision, install.installed_revision_id)
            if rev:
                installed_number = rev.revision_number
                installed_version = rev.version
        if install.bundle_uuid:
            bundle = BundleService.get_bundle_by_uuid(session, install.bundle_uuid)
            if bundle:
                latest_rev = BundleService.latest_revision(session, bundle)
                if latest_rev:
                    latest_number = latest_rev.revision_number
                    latest_version = latest_rev.version
                    latest_release_notes = latest_rev.release_notes
                    latest_published_at = latest_rev.published_at

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
            "installed_version": installed_version,
            "latest_version": latest_version,
            "latest_release_notes": latest_release_notes,
            "latest_published_at": latest_published_at,
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
