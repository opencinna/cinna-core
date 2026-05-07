"""PublishService — snapshot a publisher install into an immutable bundle revision.

Each publish:

1. Validates the install is the publisher install (or promotes the agent into
   a publisher install on first publish, creating the ``AgentBundle`` row).
2. Reads bundle folders from the install env workspace
   (``scripts/``, ``docs/``, ``knowledge/``, ``files/``,
   ``workspace_requirements.txt``, ``workspace_system_packages.txt``).
3. Writes a SHA-256-hashed snapshot tree to
   ``<BUNDLE_STORAGE_DIR>/<bundle_id>/<revision>/`` plus a ``manifest.json``.
4. Inserts an ``AgentBundleRevision`` row with the manifest + prompt copies.
5. Sets ``bundle.latest_revision_id`` and emits ``BUNDLE_PUBLISHED``.
6. Notifies dependent installs — manual installs flip ``pending_update``
   and emit ``INSTALL_UPDATE_AVAILABLE``; automatic installs are picked up
   by the suspension scheduler on next idle cycle.

Concurrency: per-bundle in-memory ``asyncio.Lock`` serialises publishes for
the same bundle on a single backend process. Cross-process the DB-level
unique ``(bundle_id, revision_number)`` constraint catches the race —
the second publish retries with the next number.
"""
import asyncio
import hashlib
import json
import logging
import shutil
import uuid
from datetime import datetime, UTC
from pathlib import Path

from sqlmodel import Session, select
from sqlalchemy import func

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle, BundleInstallMode
from app.models.bundles.agent_bundle_revision import AgentBundleRevision
from app.models.environments.environment import AgentEnvironment
from app.models.events.event import EventType
from app.models.credentials.credential import Credential
from app.models.credentials.link_models import AgentCredentialLink
from app.services.bundles.bundle_service import BundleService

logger = logging.getLogger(__name__)


# Bundle folders snapshotted by publish. Keys are "name on disk inside the
# revision tree". Values are the relative path inside the env's instance
# directory (under ``ENV_INSTANCES_DIR/<env_id>/``). Both forms exist
# because the snapshot tree is flat-ish; the env workspace is nested under
# ``app/workspace/``.
_BUNDLE_FOLDERS: tuple[tuple[str, str], ...] = (
    ("scripts", "app/workspace/scripts"),
    ("docs", "app/workspace/docs"),
    ("knowledge", "app/workspace/knowledge"),
    ("files", "app/workspace/files"),
)
_BUNDLE_FILES: tuple[tuple[str, str], ...] = (
    ("workspace_requirements.txt", "app/workspace/workspace_requirements.txt"),
    ("workspace_system_packages.txt", "app/workspace/workspace_system_packages.txt"),
)


# Per-bundle in-memory locks. Cleared on bundle deletion is best-effort.
_publish_locks: dict[str, asyncio.Lock] = {}


def _lock_for(bundle_id: str) -> asyncio.Lock:
    lock = _publish_locks.get(bundle_id)
    if lock is None:
        lock = asyncio.Lock()
        _publish_locks[bundle_id] = lock
    return lock


class PublishService:
    """Publish snapshots from publisher installs into bundle revisions."""

    @staticmethod
    async def publish(
        session: Session,
        install: Agent,
        publisher_user_id: uuid.UUID,
        release_notes: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        bundle_id_override: str | None = None,
        version: str | None = None,
    ) -> AgentBundleRevision:
        """Snapshot the publisher install workspace + create a new revision.

        On the first publish the ``AgentBundle`` row is created, the agent is
        marked ``is_publisher_install=True``, and ``bundle_uuid`` is linked.
        Subsequent publishes only require the install row to be the
        publisher install of an existing bundle.

        ``bundle_id_override`` is honoured only on the first publish — it
        lets the publisher pick the final bundle ID inside the publish
        form instead of a separate edit modal. After publish the bundle
        ID is locked.

        ``version`` is the user-entered human-friendly version label
        stored on the revision (independent from ``revision_number``).
        """
        if install.owner_id != publisher_user_id:
            raise ValueError("Only the install owner may publish")
        if install.bundle_id is None:
            raise ValueError("Install has no bundle_id (data integrity error)")
        if install.is_general_assistant:
            raise ValueError("The General Assistant cannot be published as a bundle")

        # Apply the bundle_id override on first publish.
        if bundle_id_override is not None:
            from app.services.bundles.install_service import InstallService

            override = bundle_id_override.strip()
            if override and override != install.bundle_id:
                # ``edit_bundle_id`` rejects with 409 when the agent is
                # already published, validates format/reserved prefixes,
                # and enforces uniqueness.
                install = InstallService.edit_bundle_id(
                    session=session,
                    install=install,
                    new_bundle_id=override,
                )

        async with _lock_for(install.bundle_id):
            return await PublishService._publish_locked(
                session,
                install,
                publisher_user_id,
                release_notes=release_notes,
                display_name=display_name,
                description=description,
                version=version,
            )

    @staticmethod
    async def _publish_locked(
        session: Session,
        install: Agent,
        publisher_user_id: uuid.UUID,
        *,
        release_notes: str | None,
        display_name: str | None,
        description: str | None,
        version: str | None,
    ) -> AgentBundleRevision:
        # 1. Resolve / create bundle row.
        bundle = BundleService.get_bundle_by_id(session, install.bundle_id)
        if bundle is None:
            # First publish — promote the install into a publisher install.
            bundle = BundleService.create_bundle(
                session=session,
                bundle_id=install.bundle_id,
                publisher_user_id=publisher_user_id,
                display_name=display_name or install.name,
                description=description if description is not None else install.description,
            )
            install.bundle_uuid = bundle.id
            install.is_publisher_install = True
            session.add(install)
            session.commit()
            session.refresh(install)
        else:
            if bundle.publisher_user_id != publisher_user_id:
                raise ValueError("This bundle belongs to a different publisher")
            if not install.is_publisher_install:
                raise ValueError(
                    "This install is not the publisher install for the bundle"
                )

        # 2. Compute next revision number.
        next_number_stmt = (
            select(func.coalesce(func.max(AgentBundleRevision.revision_number), 0))
            .where(AgentBundleRevision.bundle_id == bundle.id)
        )
        last_number = session.exec(next_number_stmt).one() or 0
        revision_number = last_number + 1

        # 3. Snapshot env workspace into <BUNDLE_STORAGE_DIR>/<bundle_id>/<rev>/
        snapshot_dir = (
            Path(settings.BUNDLE_STORAGE_DIR)
            / install.bundle_id
            / str(revision_number)
        )
        # Tmp dir for atomic-ish move on success.
        tmp_dir = snapshot_dir.with_suffix(".tmp")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

        try:
            env = (
                session.get(AgentEnvironment, install.active_environment_id)
                if install.active_environment_id
                else None
            )
            if env is None:
                logger.info(
                    "Publishing bundle %s rev %s with no active env — "
                    "snapshot will only contain prompts (no workspace files)",
                    install.bundle_id,
                    revision_number,
                )
                env_workspace_root: Path | None = None
            else:
                env_workspace_root = Path(settings.ENV_INSTANCES_DIR) / str(env.id)

            PublishService._copy_bundle_tree(env_workspace_root, tmp_dir)

            # Required credential specs from the install's linked credentials.
            cred_specs = PublishService._collect_credential_specs(session, install)

            manifest = {
                "schema_version": 1,
                "bundle_id": install.bundle_id,
                "revision_number": revision_number,
                "version": version,
                "published_at": datetime.now(UTC).isoformat(),
                "prompts": {
                    "workflow": install.workflow_prompt,
                    "entrypoint": install.entrypoint_prompt,
                    "refiner": install.refiner_prompt,
                },
                "sdk": {
                    "building": env.agent_sdk_building if env else None,
                    "conversation": env.agent_sdk_conversation if env else None,
                    "model_override_building": getattr(
                        env, "model_override_building", None
                    ) if env else None,
                    "model_override_conversation": getattr(
                        env, "model_override_conversation", None
                    ) if env else None,
                },
                "required_credential_specs": cred_specs,
                "release_notes": release_notes,
            }
            content_hash = PublishService._hash_tree_with_manifest(tmp_dir, manifest)
            manifest["content_hash"] = f"sha256:{content_hash}"

            # Write manifest.json into the snapshot.
            (tmp_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

            # Atomic-ish move. shutil.move is not atomic across filesystems;
            # within the same FS root it is. Bundle storage is a single
            # configured root so we accept this.
            if snapshot_dir.exists():
                shutil.rmtree(snapshot_dir)
            shutil.move(str(tmp_dir), str(snapshot_dir))
        except Exception:
            # Leave the .tmp tree in place for debugging per the plan's
            # "rollback to .tmp" guidance; surface the error to the caller.
            logger.exception(
                "Publish failed for bundle %s rev %s — snapshot tree left at %s",
                install.bundle_id,
                revision_number,
                tmp_dir,
            )
            raise

        # 4. Insert revision row.
        revision = AgentBundleRevision(
            bundle_id=bundle.id,
            revision_number=revision_number,
            version=version,
            manifest=manifest,
            workflow_prompt=install.workflow_prompt,
            entrypoint_prompt=install.entrypoint_prompt,
            refiner_prompt=install.refiner_prompt,
            agent_sdk_building=env.agent_sdk_building if env else None,
            agent_sdk_conversation=env.agent_sdk_conversation if env else None,
            model_override_building=getattr(env, "model_override_building", None) if env else None,
            model_override_conversation=getattr(env, "model_override_conversation", None) if env else None,
            required_credential_specs=cred_specs,
            snapshot_path=str(snapshot_dir),
            content_hash=content_hash,
            published_by_user_id=publisher_user_id,
            release_notes=release_notes,
        )
        session.add(revision)
        session.commit()
        session.refresh(revision)

        # 5. Update bundle metadata + record installed revision on publisher install.
        bundle.latest_revision_id = revision.id
        bundle.updated_at = datetime.now(UTC)
        if display_name:
            bundle.display_name = display_name
        if description is not None:
            bundle.description = description
        session.add(bundle)

        install.installed_revision_id = revision.id
        install.last_sync_at = datetime.now(UTC)
        install.last_update_status = "synced"
        install.pending_update = False
        install.pending_update_at = None
        session.add(install)
        session.commit()

        # 6. Fire event + notify foreign installs.
        try:
            from app.services.events.event_service import event_service

            await event_service.emit_event(
                event_type=EventType.BUNDLE_PUBLISHED,
                model_id=bundle.id,
                user_id=publisher_user_id,
                meta={
                    "bundle_id": install.bundle_id,
                    "bundle_uuid": str(bundle.id),
                    "revision_number": revision.revision_number,
                    "revision_id": str(revision.id),
                },
            )
        except Exception as e:
            logger.warning("Failed to emit BUNDLE_PUBLISHED event: %s", e)

        await PublishService.notify_installs(session, bundle, revision)

        return revision

    # ── Notify ─────────────────────────────────────────────────────

    @staticmethod
    async def notify_installs(
        session: Session, bundle: AgentBundle, revision: AgentBundleRevision
    ) -> None:
        """Mark foreign installs pending-update and emit per-user events.

        Automatic installs stay pending here too — the suspension scheduler
        applies updates while the env is idle to avoid mid-stream disruption.
        """
        from app.services.events.event_service import event_service

        stmt = select(Agent).where(
            Agent.bundle_uuid == bundle.id,
            Agent.is_publisher_install == False,  # noqa: E712
        )
        installs = list(session.exec(stmt).all())

        for install in installs:
            if install.installed_revision_id == revision.id:
                # Already on this revision; skip.
                continue
            install.pending_update = True
            install.pending_update_at = datetime.now(UTC)
            session.add(install)
        session.commit()

        for install in installs:
            try:
                await event_service.emit_event(
                    event_type=EventType.INSTALL_UPDATE_AVAILABLE,
                    model_id=install.id,
                    user_id=install.owner_id,
                    meta={
                        "agent_id": str(install.id),
                        "bundle_id": bundle.bundle_id,
                        "revision_number": revision.revision_number,
                        "release_notes": revision.release_notes,
                        "update_mode": install.update_mode,
                    },
                )
            except Exception as e:
                logger.warning(
                    "Failed to emit INSTALL_UPDATE_AVAILABLE for install %s: %s",
                    install.id, e,
                )

    # ── Snapshot helpers ───────────────────────────────────────────

    @staticmethod
    def _copy_bundle_tree(
        env_workspace_root: Path | None, dest: Path
    ) -> None:
        """Copy bundle folders + files from env workspace into ``dest``."""
        for snap_name, env_rel in _BUNDLE_FOLDERS:
            target = dest / snap_name
            if env_workspace_root is None:
                # No env yet — leave an empty folder so the manifest is
                # consistent and ``replace_bundle_content`` still works.
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
                continue
            src = env_workspace_root / env_rel
            if src.exists():
                shutil.copytree(src, target)
            else:
                target.mkdir(parents=True, exist_ok=True, mode=0o755)

        for snap_name, env_rel in _BUNDLE_FILES:
            target = dest / snap_name
            if env_workspace_root is None:
                continue
            src = env_workspace_root / env_rel
            if src.exists():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                shutil.copy2(src, target)

    @staticmethod
    def _hash_tree_with_manifest(root: Path, manifest_without_hash: dict) -> str:
        """SHA-256 over the canonical snapshot tree + manifest body.

        Hash inputs (sorted by path):
          - relative path strings
          - file bytes
          - serialized manifest body (excluding ``content_hash`` itself)
        """
        h = hashlib.sha256()
        files: list[tuple[str, Path]] = []
        for f in root.rglob("*"):
            if f.is_file():
                rel = f.relative_to(root).as_posix()
                if rel == "manifest.json":
                    continue
                files.append((rel, f))
        files.sort(key=lambda x: x[0])
        for rel, p in files:
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            try:
                h.update(p.read_bytes())
            except OSError:
                continue
            h.update(b"\0")
        manifest_body = json.dumps(
            {k: v for k, v in manifest_without_hash.items() if k != "content_hash"},
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        h.update(manifest_body)
        return h.hexdigest()

    @staticmethod
    def _collect_credential_specs(session: Session, install: Agent) -> list[dict]:
        """Snapshot the install's credential set as ``required_credential_specs``."""
        stmt = select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install.id
        )
        links = list(session.exec(stmt).all())
        specs: list[dict] = []
        for link in links:
            cred = session.get(Credential, link.credential_id)
            if not cred:
                continue
            specs.append({
                "name": cred.name,
                "type": cred.type.value if hasattr(cred.type, "value") else str(cred.type),
                "allow_sharing": bool(cred.allow_sharing),
                "description": cred.notes or None,
            })
        return specs
