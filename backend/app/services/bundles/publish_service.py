"""PublishService — snapshot a publisher install into an immutable bundle revision.

Each publish:

1. Validates the install is the publisher install (or promotes the agent into
   a publisher install on first publish, creating the ``AgentBundle`` row).
2. Captures the **entire** ``app/workspace/`` tree from the install env
   workspace (everything except the per-user / runtime / secret denylist —
   ``app-data/``, ``credentials/``, ``logs/``, ``databases/``, ``uploads/``,
   the template ``__init__.py`` marker, and the plugins-root derived files).
   Classification lives in :mod:`app.services.environments.workspace_classification`.
3. Writes a SHA-256-hashed snapshot tree to
   ``<BUNDLE_STORAGE_DIR>/<bundle_id>/<revision>/`` plus a ``manifest.json``.
   The captured workspace lives under a ``workspace/`` subtree
   (``schema_version: 2``); legacy ``schema_version: 1`` revisions used a flat
   allowlist layout at the snapshot root and remain readable by the consumer.
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
from typing import TYPE_CHECKING

from sqlmodel import Session, select
from sqlalchemy import func

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle, BundleInstallMode
from app.models.bundles.agent_bundle_revision import (
    AgentBundleRevision,
    REVISION_ORIGIN_PUBLISH,
)
from app.models.environments.environment import AgentEnvironment
from app.models.events.event import EventType
from app.models.credentials.credential import Credential
from app.models.credentials.link_models import AgentCredentialLink
from app.services.bundles.bundle_service import BundleService
from app.services.bundles.revision_format import RevisionFormat
from app.services.credentials.credentials_service import CredentialsService
from app.services.environments.workspace_classification import (
    PLUGIN_DERIVED_FILES,
    PLUGINS_DIRNAME,
    WORKSPACE_ROOT_REL,
    is_nested_excluded,
    iter_bundle_toplevel,
    safe_copytree,
)

if TYPE_CHECKING:
    from app.models.bundles.catalog import BundleCredentialDrift

logger = logging.getLogger(__name__)


# Env-relative path to the publisher plugins tree (used by the plugin-files
# pre-flight check, which reads the live env workspace — not the snapshot).
_BUNDLE_PLUGINS_ENV_REL = f"{WORKSPACE_ROOT_REL}/{PLUGINS_DIRNAME}"


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
            # Transfer the pre-publish AI credential draft (set via
            # PATCH /agents/{id}/publish-settings while the bundle row
            # didn't yet exist) onto the new bundle row. After this point
            # the bundle FK columns are the source of truth and the
            # frontend writes to them via PATCH /bundles/{uuid}.
            PublishService._apply_pre_publish_ai_drafts(session, install, bundle)
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
        # Tmp dir for atomic-ish move on success. Created lazily AFTER the
        # pre-flight validators below so a failed pre-flight never leaves an
        # orphan empty ``<rev>.tmp`` dir behind. (Set by ``_create_tmp_dir``.)
        tmp_dir = snapshot_dir.with_suffix(".tmp")

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

            # Pre-flight: when an env IS active its workspace must be present
            # and readable on disk — otherwise we'd silently ship an empty
            # revision (the historical "bundle without scripts/" bug). Same
            # failure class as ``_ensure_publisher_plugin_files``. No-env
            # publishing (prompts-only) is still allowed.
            PublishService._assert_workspace_readable(env, env_workspace_root)

            # Pre-flight: publisher-provided AI credentials must match the
            # env's per-mode SDK provider. The bundle PATCH endpoint already
            # validates this, but the publisher can change either side
            # (env SDK or AI credential) between writes — this is the last
            # line of defence before we ship an unusable revision.
            PublishService._validate_publisher_ai_credentials_sdk(
                session, install, bundle, env
            )

            # Pre-flight: every declared plugin's files must be present in the
            # publisher env workspace before we snapshot — a bundle is immutable,
            # so a missing plugin tree is a hard block (names the plugin).
            PublishService._ensure_publisher_plugin_files(
                session, install, env_workspace_root
            )

            # All pre-flight validators passed. The DB-bound collectors below
            # build the manifest body; they MUST stay on the event loop (the
            # sync ``Session`` is not safe to touch from a worker thread). The
            # heavy filesystem half — workspace copy + per-file hash + atomic
            # move — is offloaded afterwards (see below), so the tmp dir is now
            # created inside that worker thread rather than here.

            # Required credential specs from the install's linked credentials.
            # Validate first so a misconfigured publisher-provided credential
            # surfaces a clean error before we touch the snapshot tree.
            PublishService._validate_publisher_provides(session, install)
            cred_specs = PublishService._collect_credential_specs(session, install)

            # Snapshot the publisher install's schedules. Feeds both the
            # manifest (so a schedule-only change yields a new content_hash
            # → installs see a pending update) and the revision row.
            schedule_specs = PublishService._collect_schedule_specs(session, install)

            # Snapshot the publisher install's plugin links. Feeds the manifest
            # (a plugin-only change → new content_hash → pending update) and the
            # revision row. The plugin files land in tmp_dir/workspace later,
            # when the snapshot tree is written to disk inside
            # ``_write_snapshot_to_disk`` (run via ``asyncio.to_thread``).
            plugin_specs = PublishService._collect_plugin_specs(session, install)

            # Build the manifest + serialize the revision tree through the
            # single canonical (de)serializer. ``write_tree`` performs the
            # full-tree capture into ``tmp_dir/workspace/`` (schema_version 2 —
            # empty when there's no active env), computes the ``content_hash``,
            # stamps it into ``manifest``, and writes ``manifest.json``.
            manifest = RevisionFormat.build_manifest(
                install=install,
                env=env,
                cred_specs=cred_specs,
                schedule_specs=schedule_specs,
                plugin_specs=plugin_specs,
                revision_number=revision_number,
                version=version,
                release_notes=release_notes,
            )

            # The snapshot copy + per-file hash + atomic move is heavy, fully
            # synchronous filesystem I/O over a potentially large workspace
            # tree. Running it inline on the asyncio event loop blocks EVERY
            # other request on this worker until it finishes (chat streaming,
            # API calls, websockets all stall). Offload it to a worker thread —
            # the same treatment the env-lifecycle copy paths already use. It
            # touches no DB session, so it is safe to run off-loop. ``manifest``
            # is mutated in place (``content_hash`` stamped), visible here after
            # the thread joins.
            content_hash = await asyncio.to_thread(
                PublishService._write_snapshot_to_disk,
                env_workspace_root=env_workspace_root,
                tmp_dir=tmp_dir,
                snapshot_dir=snapshot_dir,
                manifest=manifest,
            )
        except Exception:
            # If the failure was in pre-flight (before the tmp dir was created)
            # there is nothing to leave behind. Once the snapshot tree exists we
            # leave the partial ``.tmp`` in place for debugging per the plan's
            # "rollback to .tmp" guidance. Either way, surface the error.
            if tmp_dir.exists():
                logger.exception(
                    "Publish failed for bundle %s rev %s — snapshot tree left at %s",
                    install.bundle_id,
                    revision_number,
                    tmp_dir,
                )
            else:
                logger.exception(
                    "Publish failed for bundle %s rev %s during pre-flight "
                    "(no snapshot tree written)",
                    install.bundle_id,
                    revision_number,
                )
            raise

        # 4. Insert revision row. The prompt / SDK / model-override / spec
        # fields are deserialized from the manifest through the same canonical
        # mapping git checkout (Phase 3) consumes, so the row and the manifest
        # can never disagree. The remaining columns (FK uuid, revision number,
        # snapshot path, content hash, author, raw manifest) are not derivable
        # from the manifest body and are supplied here.
        revision = AgentBundleRevision(
            bundle_id=bundle.id,
            revision_number=revision_number,
            origin=REVISION_ORIGIN_PUBLISH,
            manifest=manifest,
            snapshot_path=str(snapshot_dir),
            content_hash=content_hash,
            published_by_user_id=publisher_user_id,
            **RevisionFormat.manifest_to_revision_fields(manifest),
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
    def _write_snapshot_to_disk(
        *,
        env_workspace_root: Path | None,
        tmp_dir: Path,
        snapshot_dir: Path,
        manifest: dict,
    ) -> str:
        """Filesystem half of a publish — runs OFF the event loop.

        Pure, blocking filesystem work over a potentially large workspace
        tree (no DB session is touched), so it is dispatched via
        ``asyncio.to_thread`` from :meth:`_publish_locked` to avoid stalling
        the asyncio event loop while the bundle is copied + hashed:

        1. (re)create the ``<rev>.tmp`` staging dir;
        2. ``write_tree`` captures the workspace, hashes it, and stamps the
           ``content_hash`` into ``manifest`` (mutated in place — the caller
           sees it after the thread joins);
        3. atomically swap the staging dir into the final ``<rev>`` path.

        ``shutil.move`` is atomic within a single filesystem root; bundle
        storage is a single configured root so we accept that. A failure after
        the tmp dir exists deliberately leaves the partial tree in place for
        debugging (the caller's ``except`` logs its location). Returns the bare
        hex ``content_hash``.
        """
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

        content_hash = RevisionFormat.write_tree(
            env_workspace_root=env_workspace_root,
            dest=tmp_dir,
            manifest=manifest,
        )

        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        shutil.move(str(tmp_dir), str(snapshot_dir))
        return content_hash

    @staticmethod
    def _assert_workspace_readable(
        env: AgentEnvironment | None, env_workspace_root: Path | None
    ) -> None:
        """Fail loudly when an active env's workspace is absent/unreadable.

        The historical silent-data-loss bug: when the publisher env's
        ``app/workspace/`` dir was missing or unreadable, the old copy walk
        created empty folders and publish "succeeded" — shipping a bundle with
        no content. New rule (mirrors ``_ensure_publisher_plugin_files``):

        * No active env (``env is None``) → allowed (prompts-only revision).
        * Active env but its ``app/workspace/`` is missing or not a readable
          directory → raise ``ValueError`` (mapped to 400 at the route) naming
          the env and path.

        An *empty* workspace dir is NOT an error here — empty-workspace publish
        is allowed by business rule; the loud failure is specifically the dir
        being absent/unreadable when an env is supposed to have one.
        """
        if env is None or env_workspace_root is None:
            return
        workspace_dir = env_workspace_root / WORKSPACE_ROOT_REL
        if not workspace_dir.exists() or not workspace_dir.is_dir():
            raise ValueError(
                "Cannot publish: the publisher environment's workspace is not "
                f"available on disk ({workspace_dir}). Start the environment "
                f"(id={env.id}) so its workspace is materialised, then publish "
                "again."
            )
        try:
            # Probe readability — a permission error here would otherwise
            # surface as a half-empty snapshot.
            next(iter(workspace_dir.iterdir()), None)
        except OSError as exc:
            raise ValueError(
                "Cannot publish: the publisher environment's workspace is not "
                f"readable on disk ({workspace_dir}: {exc}). Check the "
                f"environment (id={env.id}) and try again."
            ) from exc

    @staticmethod
    def _snapshot_workspace_tree(
        env_workspace_root: Path | None, dest: Path
    ) -> None:
        """Capture the full ``app/workspace/`` tree into ``dest/workspace/``.

        Full-tree, denylist-driven capture (schema_version 2). Everything under
        the env's ``app/workspace/`` is copied verbatim into the snapshot's
        ``workspace/`` subtree **except**:

        * the per-user / runtime / secret top-level denylist
          (``app-data/``, ``credentials/``, ``logs/``, ``databases/``,
          ``uploads/``, the ``__init__.py`` marker) and runtime dotfiles —
          enforced by :func:`iter_bundle_toplevel`;
        * the plugins-root derived files (``settings.json`` / ``manifest.json``)
          — they are regenerated per consumer.

        When ``env_workspace_root is None`` (no active env) the ``workspace/``
        subtree is created empty (prompts-only revision) — we do NOT synthesize
        per-folder empty dirs.
        """
        dest_workspace_dir = dest / "workspace"
        dest_workspace_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

        if env_workspace_root is None:
            return

        # ``iter_bundle_toplevel`` already skips top-level symlinks (denylist
        # bypass / host-file exfiltration guard); ``safe_copytree`` guards
        # nested symlinks inside the captured trees.
        workspace_root = env_workspace_root / WORKSPACE_ROOT_REL
        for src in iter_bundle_toplevel(workspace_root):
            target = dest_workspace_dir / src.name
            if src.name == PLUGINS_DIRNAME and src.is_dir():
                # Plugins keep their special derived-file exclusion.
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
                PublishService._copy_plugins_tree(src, target)
            elif src.is_dir():
                safe_copytree(src, target)
            else:
                shutil.copy2(src, target)

    @staticmethod
    def _copy_plugins_tree(src: Path, dest: Path) -> None:
        """Copy a plugins/ tree, skipping top-level derived files.

        Only ``settings.json`` / ``manifest.json`` at the plugins root are
        excluded (they are regenerated per consumer); plugin subdirectories
        and any per-plugin ``.cinna_plugin_ref`` markers are copied verbatim so
        the immutable snapshot is a faithful copy of the publisher's files.

        Symlinks are skipped at every depth (top-level here, nested via
        ``safe_copytree``) — the workspace is agent-controlled.
        """
        for child in src.iterdir():
            if child.is_symlink():
                logger.warning(
                    "Skipping symlink in plugins tree (never followed): %s", child
                )
                continue
            # Regenerated cache junk (``__pycache__/``, ``*.pyc``/``*.pyo``)
            # sitting directly at the plugins root — parity with the
            # workspace-root path (is_bundle_owned_toplevel), which excludes
            # these so they are never recreated as empty dirs in the snapshot.
            if is_nested_excluded(child.name):
                continue
            if child.is_file() and child.name in PLUGIN_DERIVED_FILES:
                continue
            target = dest / child.name
            if child.is_dir():
                safe_copytree(child, target)
            else:
                shutil.copy2(child, target)

    @staticmethod
    def _hash_tree_with_manifest(root: Path, manifest_without_hash: dict) -> str:
        """SHA-256 over the canonical snapshot tree + manifest body.

        ``rglob`` walks the whole snapshot root, so the captured ``workspace/``
        subtree (schema_version 2) is included automatically — relative paths
        simply become ``workspace/...``. No layout-specific logic is needed.

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
    def hash_workspace_tree(workspace_root: Path) -> str:
        """SHA-256 over the files under a ``workspace/`` subtree only (NO manifest).

        The workspace-only sibling of :meth:`_hash_tree_with_manifest`: it omits
        the manifest body, so the digest is **stable across rebuilds** (it does
        not move with ``revision_number`` / ``published_at`` / ``version``). Two
        captures of byte-identical workspace files therefore hash equal — the
        primitive the git dirty-check uses to compare the live env workspace
        against the last synced revision snapshot.

        ``workspace_root`` must point at the ``workspace/`` directory itself on
        both sides so relative paths line up. A missing root hashes to the empty
        digest (no files).
        """
        h = hashlib.sha256()
        if not workspace_root.exists():
            return h.hexdigest()
        files: list[tuple[str, Path]] = []
        for f in workspace_root.rglob("*"):
            if f.is_symlink() or not f.is_file():
                continue
            rel = f.relative_to(workspace_root).as_posix()
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
        return h.hexdigest()

    @staticmethod
    def resolve_provided_by(
        credential: Credential, publisher_install: Agent
    ) -> str:
        """Single source of truth for ``provided_by`` resolution.

        Resolution order:

        1. ``publisher_install.publish_settings["credential_overrides"]
           [credential.name]["provided_by"]`` — explicit publisher choice
           from the bundle tab UI. Wins if present and equals ``"user"``,
           ``"publisher"``, or ``"template"``.
        2. Fallback inference:
           - ``allow_sharing=True`` → ``"publisher"``
           - else if ``allow_template_sharing=True`` → ``"template"``
           - else → ``"user"``

        Returns the resolved string. Used by both the publish-time spec
        collector and the credential-bundle-usages projection so the two
        paths can never disagree.
        """
        publish_settings = (
            getattr(publisher_install, "publish_settings", None) or {}
        )
        overrides_raw = publish_settings.get("credential_overrides") or {}
        if isinstance(overrides_raw, dict):
            entry = overrides_raw.get(credential.name)
            if isinstance(entry, dict):
                raw = entry.get("provided_by")
                if raw in ("user", "publisher", "template"):
                    return raw

        if bool(credential.allow_sharing):
            return "publisher"
        if bool(getattr(credential, "allow_template_sharing", False)):
            return "template"
        return "user"

    @staticmethod
    def _collect_credential_specs(session: Session, install: Agent) -> list[dict]:
        """Snapshot the install's credential set as ``required_credential_specs``.

        Emits the evolved per-spec shape with ``provided_by``,
        ``publisher_credential_id`` and (for templates) ``template_data``
        + ``template_private_fields``. ``provided_by`` is resolved by
        :meth:`resolve_provided_by`.

        ``_validate_publisher_provides`` (run before this) guarantees that
        every ``provided_by="publisher"`` spec references a credential
        with ``allow_sharing=True`` and every ``provided_by="template"``
        spec references a credential with ``allow_template_sharing=True``.
        """
        stmt = select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install.id
        )
        links = list(session.exec(stmt).all())
        specs: list[dict] = []
        for link in links:
            cred = session.get(Credential, link.credential_id)
            if not cred:
                continue
            allow_sharing = bool(cred.allow_sharing)
            allow_template_sharing = bool(getattr(cred, "allow_template_sharing", False))
            provided_by = PublishService.resolve_provided_by(cred, install)

            publisher_credential_id = cred.id if provided_by == "publisher" else None
            spec: dict = {
                "name": cred.name,
                "type": cred.type.value if hasattr(cred.type, "value") else str(cred.type),
                "allow_sharing": allow_sharing,
                "allow_template_sharing": allow_template_sharing,
                "description": cred.notes or None,
                "provided_by": provided_by,
                "publisher_credential_id": (
                    str(publisher_credential_id)
                    if publisher_credential_id is not None
                    else None
                ),
                "service_uri": cred.service_uri,
            }
            if provided_by == "template":
                template_data, template_private_fields = (
                    PublishService._template_payload_for(session, cred)
                )
                spec["template_data"] = template_data
                spec["template_private_fields"] = template_private_fields
            specs.append(spec)
        return specs

    @staticmethod
    def compute_credential_spec_drift(
        session: Session, install: Agent
    ) -> "BundleCredentialDrift":
        """Diff each linked credential's live vs published ``provided_by``.

        The bundle tab recomputes ``provided_by`` live from the credential's
        current ``allow_sharing`` / override, but installers receive the value
        frozen into the latest published revision's
        ``required_credential_specs``. The two diverge whenever the publisher
        changes a credential's sharing mode after the last publish. This
        surfaces that gap so the UI can prompt a republish.

        Live resolution reuses :meth:`resolve_provided_by`; the snapshot side
        reuses :func:`parse_credential_spec`, so the comparison can never
        disagree with what publish actually writes / what install actually
        reads. A credential added or removed relative to the snapshot is
        reported as drifted.

        Returns an empty, non-stale result when the install is not a publisher
        install or has never published a revision (nothing to be stale
        against).
        """
        from app.models.bundles.catalog import (
            BundleCredentialDrift,
            CredentialSpecDrift,
        )
        from app.services.bundles.credential_spec import parse_credential_spec

        if not install.is_publisher_install or install.bundle_uuid is None:
            return BundleCredentialDrift(stale=False, drift=[])

        bundle = session.get(AgentBundle, install.bundle_uuid)
        revision = (
            BundleService.latest_revision(session, bundle) if bundle else None
        )
        if revision is None:
            return BundleCredentialDrift(stale=False, drift=[])

        # Snapshot side: index the latest revision's specs by credential name.
        snapshot_by_name: dict[str, str] = {}
        for raw_spec in revision.required_credential_specs or []:
            parsed = parse_credential_spec(raw_spec)
            if parsed is not None:
                snapshot_by_name[parsed.name] = parsed.provided_by

        # Live side: recompute provided_by for every currently linked cred.
        # ``name`` is the spec key (it keys both the snapshot index and
        # ``resolve_provided_by``'s override lookup), so dedupe by name — a
        # duplicate link or two same-named credentials must not yield two rows.
        stmt = select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install.id
        )
        links = list(session.exec(stmt).all())

        drift: list[CredentialSpecDrift] = []
        live_names: set[str] = set()
        for link in links:
            cred = session.get(Credential, link.credential_id)
            if cred is None:
                continue
            if cred.name in live_names:
                continue
            live_names.add(cred.name)
            live = PublishService.resolve_provided_by(cred, install)
            # A credential newly linked since the last publish has no snapshot
            # entry — installers don't receive it at all, so treat it as
            # "user" (the install-side default) and flag the drift.
            snapshot = snapshot_by_name.get(cred.name, "user")
            drift.append(
                CredentialSpecDrift(
                    name=cred.name,
                    type=(
                        cred.type.value
                        if hasattr(cred.type, "value")
                        else str(cred.type)
                    ),
                    live_provided_by=live,
                    snapshot_provided_by=snapshot,
                    drifted=(live != snapshot)
                    or cred.name not in snapshot_by_name,
                )
            )

        # Credentials present in the snapshot but no longer linked are also
        # drift — installers still receive a spec the publisher has removed.
        # We do NOT emit a per-row entry for them (there is no live credential
        # to render, so a row would carry an empty ``type`` the UI can't show);
        # instead they still flip ``stale`` so the publisher is nudged to
        # republish.
        removed_in_snapshot = bool(set(snapshot_by_name) - live_names)

        return BundleCredentialDrift(
            stale=removed_in_snapshot or any(d.drifted for d in drift),
            drift=drift,
        )

    @staticmethod
    def _collect_schedule_specs(session: Session, install: Agent) -> list[dict]:
        """Snapshot the publisher install's ``AgentSchedule`` rows.

        Emits ``{name, cron_string, description, prompt, schedule_type,
        command, enabled}`` per schedule. ``next_execution`` /
        ``last_execution`` are deliberately excluded — they are per-install
        runtime state recomputed when the consumer materialises the
        schedule. ``cron_string`` is already UTC on the row.

        Because the snapshot is included in the manifest body that feeds
        ``content_hash``, a schedule-only change yields a new hash so
        installs see a pending update.
        """
        from app.services.agents.agent_scheduler_service import (
            AgentSchedulerService,
        )
        from app.services.bundles.schedule_sync import snapshot_schedules

        schedules = AgentSchedulerService.get_agent_schedules(session, install.id)
        return snapshot_schedules(schedules)

    @staticmethod
    def _collect_plugin_specs(session: Session, install: Agent) -> list[dict]:
        """Snapshot the publisher install's plugin links as ``plugin_specs``.

        Emits one spec per ``AgentPluginLink`` (both marketplace- and
        bundle-sourced publisher links): identity, per-mode + disabled flags,
        version/commit, frozen ``plugin.json`` config, and the ``snapshot_subdir``
        logical coordinate (``plugins/<mkt>/<plugin>``). The plugin *files* are
        captured by ``_snapshot_workspace_tree`` (under ``workspace/plugins/``);
        this captures only coordinates/flags.

        Included in the manifest body that feeds ``content_hash`` so a
        plugin-only change yields a new hash → installs see a pending update.
        """
        from app.services.bundles.plugin_sync import snapshot_plugin_specs
        from app.models.plugins.llm_plugin import AgentPluginLink

        links = list(
            session.exec(
                select(AgentPluginLink).where(
                    AgentPluginLink.agent_id == install.id
                )
            ).all()
        )
        return snapshot_plugin_specs(links)

    @staticmethod
    def _ensure_publisher_plugin_files(
        session: Session,
        install: Agent,
        env_workspace_root: Path | None,
    ) -> None:
        """Hard-block publish if any publisher plugin's files are not on disk.

        Decided default (§14.3): ensure-then-copy the publisher's live workspace
        ``plugins/`` tree. A bundle is an immutable artifact, so every declared
        plugin's files MUST be present before we snapshot. If the publisher has
        no env (no workspace), publishing plugin *files* is impossible — block if
        any plugin links exist.

        We do NOT attempt a backend-side git fetch here: the files are produced
        by the container install routine (the publisher env materialised them on
        its last start/sync). A missing tree means the publisher's env never
        successfully installed that plugin — shipping it would create a broken
        immutable revision, so we fail loudly naming the plugin.
        """
        from app.models.plugins.llm_plugin import AgentPluginLink

        links = list(
            session.exec(
                select(AgentPluginLink).where(
                    AgentPluginLink.agent_id == install.id
                )
            ).all()
        )
        if not links:
            return

        from app.services.bundles.plugin_sync import _resolve_link_identity

        missing: list[str] = []
        for link in links:
            marketplace_name, plugin_name, _config = _resolve_link_identity(link)
            label = f"{marketplace_name or '?'}/{plugin_name or '?'}"
            if env_workspace_root is None:
                missing.append(label)
                continue
            if not (marketplace_name and plugin_name):
                missing.append(label)
                continue
            plugin_dir = (
                env_workspace_root
                / _BUNDLE_PLUGINS_ENV_REL
                / marketplace_name
                / plugin_name
            )
            if not (plugin_dir.exists() and plugin_dir.is_dir() and any(plugin_dir.iterdir())):
                missing.append(label)

        if missing:
            names = ", ".join(sorted(set(missing)))
            raise ValueError(
                "Cannot publish: plugin files are missing from the publisher "
                f"environment for: {names}. Start the environment so the plugins "
                "install, then publish again."
            )

    # Credential types whose ``credential_data`` is intrinsically per-user
    # (OAuth tokens, SA JSON). Template sharing is still allowed on these
    # types so the publisher can ship the credential's ``notes`` as setup
    # instructions, but no field of ``credential_data`` is ever included
    # in the template payload — the backend strips the dict regardless of
    # UI state. Defence in depth.
    _TEMPLATE_FORCE_PRIVATE_TYPES = frozenset({
        "gmail_oauth",
        "gmail_oauth_readonly",
        "gdrive_oauth",
        "gdrive_oauth_readonly",
        "gcalendar_oauth",
        "gcalendar_oauth_readonly",
        "google_service_account",
    })

    # Per-type templatable allowlist. When a type has an entry, ONLY those
    # fields are eligible for ``template_data`` even if the publisher
    # forgot to mark the rest as private. Types not listed allow every
    # field through (modulo the publisher's private-field selection).
    #
    # ssh_key: ``host_aliases`` is configuration the publisher can share
    # as a default; ``public_key`` / ``fingerprint`` / ``key_type`` are
    # generated per-key (the installer generates their own); the secret
    # ``private_key`` / ``passphrase`` are stripped here AND by the
    # whitelist on the agent-env sync path.
    _TEMPLATE_TEMPLATABLE_FIELDS_BY_TYPE: dict[str, frozenset[str]] = {
        "ssh_key": frozenset({"host_aliases"}),
    }

    @staticmethod
    def _apply_pre_publish_ai_drafts(
        session: Session, install: Agent, bundle: AgentBundle
    ) -> None:
        """Transfer the pre-publish AI credential draft onto a new bundle.

        Reads ``install.publish_settings["ai_credentials"]`` (set via
        ``PATCH /agents/{id}/publish-settings`` while the bundle didn't
        yet exist) and writes the resolved UUIDs to
        ``bundle.publisher_ai_credential_*_id``. The route already
        validated ownership; we re-check here as defence in depth and
        skip on any mismatch.
        """
        from app.models.credentials.ai_credential import AICredential

        publish_settings = getattr(install, "publish_settings", None) or {}
        draft = publish_settings.get("ai_credentials")
        if not isinstance(draft, dict):
            return

        changed = False
        for column, key in (
            ("publisher_ai_credential_conversation_id", "conversation_credential_id"),
            ("publisher_ai_credential_building_id", "building_credential_id"),
        ):
            raw = draft.get(key)
            if raw is None:
                continue
            try:
                cred_id = uuid.UUID(str(raw))
            except (ValueError, TypeError):
                continue
            ai_cred = session.get(AICredential, cred_id)
            if ai_cred is None or ai_cred.owner_id != bundle.publisher_user_id:
                logger.warning(
                    "Pre-publish AI credential draft %s for bundle %s "
                    "is not owned by the publisher; skipping transfer",
                    cred_id, bundle.bundle_id,
                )
                continue
            setattr(bundle, column, cred_id)
            changed = True

        if changed:
            session.add(bundle)
            session.commit()
            session.refresh(bundle)

    @staticmethod
    def _template_payload_for(
        session: Session, cred: Credential
    ) -> tuple[dict, list[str]]:
        """Return ``(template_data, template_private_fields)`` for ``cred``.

        ``template_data`` carries only the non-private credential_data
        fields — values the publisher consents to ship inside the bundle
        revision JSON. The private fields list tells the install screen
        and the runtime gate which fields the installer must supply.

        For credential types in ``_TEMPLATE_FORCE_PRIVATE_TYPES`` the
        whole credential_data dict is dropped regardless of UI state — the
        installer authenticates themselves (OAuth, SSH key generation,
        service-account JSON upload). The publisher's ``notes`` still
        carry through via the spec's ``description`` field.

        Decryption failures are surfaced as a publish-time ``ValueError``
        rather than silently shipping an empty template — a corrupt
        publisher credential would otherwise produce a bundle with zero
        defaults that no installer could complete usefully.
        """
        cred_type_value = (
            cred.type.value if hasattr(cred.type, "value") else str(cred.type)
        )
        if cred_type_value in PublishService._TEMPLATE_FORCE_PRIVATE_TYPES:
            return {}, []

        try:
            full = CredentialsService.decrypt_credential_data(
                session=session, credential=cred
            )
        except Exception as exc:
            logger.exception(
                "Failed to decrypt credential %s for template publishing",
                cred.id,
            )
            raise ValueError(
                f"Credential '{cred.name}' is marked template-provided "
                "but its stored data could not be decrypted. Re-save the "
                "credential and try again."
            ) from exc
        private_fields = [
            f for f in (cred.template_private_fields or []) if isinstance(f, str)
        ]
        private_set = set(private_fields)
        template_data = {
            k: v for k, v in full.items() if k not in private_set
        }

        # Apply the per-type templatable allowlist (defence in depth) — fields
        # not in the allowlist are stripped even if the publisher forgot to
        # mark them private.
        allowlist = PublishService._TEMPLATE_TEMPLATABLE_FIELDS_BY_TYPE.get(
            cred_type_value
        )
        if allowlist is not None:
            template_data = {
                k: v for k, v in template_data.items() if k in allowlist
            }
        return template_data, private_fields

    @staticmethod
    def _validate_publisher_ai_credentials_sdk(
        session: Session,
        install: Agent,
        bundle: AgentBundle,
        env: AgentEnvironment | None,
    ) -> None:
        """Reject publish when a publisher AI credential doesn't match the env SDK.

        Mirrors :meth:`BundleService.update_bundle` and
        :meth:`InstallService._validate_ai_credentials_draft`. Catches the
        case where the publisher set ``publisher_ai_credential_conversation_id``
        to an OpenAI credential, then changed the env's conversation SDK to
        ``opencode/anthropic`` (or vice versa) before publishing — at runtime
        the env would write the wrong key into the wrong provider slot and
        the LLM call would fail with 401.

        When ``env`` is ``None`` (publishing without an active environment),
        the check is skipped — the install will be unable to run anyway and
        the publisher will see the missing-env warning instead.
        """
        from app.models.credentials.ai_credential import AICredential
        from app.services.environments.sdk_constants import (
            sdk_expected_credential_type,
        )

        if env is None:
            return

        for ai_field, sdk_attr, mode_label in (
            (
                "publisher_ai_credential_conversation_id",
                "agent_sdk_conversation",
                "conversation",
            ),
            (
                "publisher_ai_credential_building_id",
                "agent_sdk_building",
                "building",
            ),
        ):
            cred_id = getattr(bundle, ai_field, None)
            if cred_id is None:
                continue
            ai_cred = session.get(AICredential, cred_id)
            if ai_cred is None:
                continue
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
                    f"Publisher-provided AI credential for {mode_label} mode "
                    f"is of type '{cred_type_value}', but the env's "
                    f"{mode_label} SDK '{sdk_id}' requires a "
                    f"'{expected_value}' credential. Update the env's SDK "
                    "provider or clear the publisher AI credential before "
                    "publishing."
                )

    @staticmethod
    def _validate_publisher_provides(
        session: Session, install: Agent
    ) -> None:
        """Assert publisher-provided / template spec credentials are valid.

        Walks the install's linked credentials, resolves each one's
        ``provided_by`` via :meth:`resolve_provided_by`, and verifies the
        underlying ``Credential`` row carries the matching consent flag
        (``allow_sharing`` or ``allow_template_sharing``). Raises a
        publish-time error otherwise — a publisher who flips a
        credential's ``provided_by`` override but leaves the consent flag
        off would publish a bundle that no foreign install could resolve.
        """
        stmt = select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install.id
        )
        links = list(session.exec(stmt).all())
        for link in links:
            cred = session.get(Credential, link.credential_id)
            if cred is None:
                continue
            provided_by = PublishService.resolve_provided_by(cred, install)
            if provided_by == "publisher" and not bool(cred.allow_sharing):
                raise ValueError(
                    f"Credential '{cred.name}' is marked publisher-provided "
                    "but is not shareable (allow_sharing=False)"
                )
            if provided_by == "template" and not bool(
                getattr(cred, "allow_template_sharing", False)
            ):
                raise ValueError(
                    f"Credential '{cred.name}' is marked template-provided "
                    "but template sharing is not enabled (allow_template_sharing=False)"
                )
