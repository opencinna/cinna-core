"""SkillCatalogService — publish, browse and install server-catalog skills.

The catalog is the third and last way a skill can reach an agent, after the
agent's own ``skills/`` folder and a plugin's. It deliberately reuses the two
mechanisms that already exist rather than inventing a third:

* **Publish** is bundle publish in miniature — read the publisher's live
  workspace off disk, validate, snapshot atomically into immutable storage,
  append a revision row. Same source of truth, same ``.tmp`` → move discipline,
  same "derived, never authored" posture towards metadata.
* **Install** is a plugin install — an ``AgentPluginLink(source=catalog)``
  under the synthetic marketplace ``cinna-skills``. The container materialises
  it through the ordinary plugin manifest, so per-mode toggles, disable, prune
  on uninstall, the failure banner and the bundle-snapshot path all work with
  no new transport.

Two decisions worth knowing before reading the code:

**Publishing does not need a running container.** The publisher's workspace is
bind-mounted on the host at ``<ENV_INSTANCES_DIR>/<env id>/app/workspace``, so
a *suspended* environment publishes exactly like a running one — the same
reason ``PublishService`` never starts a container to publish a bundle. Waking
the environment would cost 30–120 s and change nothing about the bytes being
read. What is refused (409) is an agent with **no environment at all** or one
whose workspace has never been materialised on disk: there is nothing to read,
and the fix is "start the environment once", which is a sentence the dialog can
show.

**Install counts are computed, not stored.** See the note on
:class:`~app.models.skills.skill_package.SkillPackage`.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import logging
import shutil
import tarfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func
from sqlmodel import Session, select

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.plugins.llm_plugin import AgentPluginLink, PluginSource
from app.models.skills.schemas import (
    SkillPackageAccessGrantPublic,
    SkillPackageDetailPublic,
    SkillPackageEntry,
    SkillPackageRevisionPublic,
    SkillPackageUpdate,
)
from app.models.skills.skill_package import (
    CATALOG_MARKETPLACE_NAME,
    SkillPackage,
    SkillPackageVisibility,
)
from app.models.skills.skill_package_access_grant import (
    SkillPackageAccessGrant,
)
from app.models.skills.skill_package_revision import SkillPackageRevision
from app.models.users.user import User
from app.services.agents.agent_service import AgentService, CanBuildError
from app.services.agents.skill_manifest import (
    DEFAULT_MAX_TOTAL_BYTES,
    SKILL_NAME_RE,
    parse_skill_dir,
)
from app.services.bundles.bundle_id_service import BUNDLE_ID_REGEX, BundleIdService
from app.services.bundles.publish_service import PublishService
from app.services.environments.workspace_classification import (
    WORKSPACE_ROOT_REL,
    safe_copytree,
)
from app.services.skills.exceptions import SkillCatalogError

logger = logging.getLogger(__name__)

#: Hard cap on one published skill package (§4), measured over the
#: UNCOMPRESSED tree. Same number as the per-agent projection budget, read from
#: the parser so the two can never drift.
#:
#: The container's download cap
#: (``AgentEnvService._CATALOG_ARCHIVE_MAX_BYTES``) must stay strictly above
#: this: it measures the *compressed* archive, and a skill of already-compressed
#: assets barely shrinks, so equal limits would make a skill at the boundary
#: publishable but uninstallable.
MAX_PACKAGE_BYTES = DEFAULT_MAX_TOTAL_BYTES

#: Cap on a served ``SKILL.md`` preview. Matches ``AgentSkillsService``.
MAX_CONTENT_BYTES = 256 * 1024

#: Per-``(publisher, skill name)`` publish locks. A package is identified by
#: that pair before its row exists, so locking on the package uuid would leave
#: the create path — the one that races into a unique-constraint violation —
#: unguarded. Same shape as ``PublishService._lock_for``.
_publish_locks: dict[str, asyncio.Lock] = {}


@dataclass
class _EntryContext:
    """Grouped lookups shared by every row of one catalog projection.

    Exists so ``package_to_entry`` can be called once per package without each
    call reaching back into the database — see
    :meth:`SkillCatalogService._entry_context`.

    Bound to one viewer: ``installed_in`` answers "which of *your* agents", so
    reusing a context across users would report someone else's agents. The
    ``viewer_id`` field is what lets ``package_to_entry`` refuse to do that.
    """

    viewer_id: uuid.UUID
    revisions: dict[uuid.UUID, "SkillPackageRevision"] = field(default_factory=dict)
    publishers: dict[uuid.UUID, "User"] = field(default_factory=dict)
    install_counts: dict[uuid.UUID, int] = field(default_factory=dict)
    installed_in: dict[uuid.UUID, list[uuid.UUID]] = field(default_factory=dict)
    #: Packages of this listing the viewer holds an access grant on. Also
    #: viewer-bound, for the same reason ``installed_in`` is.
    granted_package_ids: set[uuid.UUID] = field(default_factory=set)


def _publish_lock_for(key: str) -> asyncio.Lock:
    lock = _publish_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _publish_locks[key] = lock
    return lock


class SkillCatalogService:
    """Publish / browse / install operations on catalog skill packages."""

    # ── Storage layout ─────────────────────────────────────────────────

    @staticmethod
    def package_dir(package_uuid: uuid.UUID) -> Path:
        return Path(settings.SKILL_STORAGE_DIR) / str(package_uuid)

    @staticmethod
    def revision_dir(package_uuid: uuid.UUID, revision_number: int) -> Path:
        return SkillCatalogService.package_dir(package_uuid) / str(revision_number)

    @staticmethod
    def archive_path(package_uuid: uuid.UUID, revision_number: int) -> Path:
        """Where the built tarball is cached, beside its snapshot."""
        return (
            SkillCatalogService.package_dir(package_uuid)
            / f"{revision_number}.tar.gz"
        )

    @staticmethod
    def skill_dir_of(revision: SkillPackageRevision) -> Path:
        """The ``skills/<name>/`` folder of a revision's snapshot.

        Prefers the stored ``snapshot_path`` — it is the recorded truth for
        revisions written before any future storage-root change — and falls
        back to recomputing it, so a relocated storage root degrades to "wrong
        path" rather than "no path at all".
        """
        stored = Path(revision.snapshot_path) if revision.snapshot_path else None
        if stored is not None and stored.is_dir():
            return stored
        name = str((revision.frontmatter or {}).get("name") or "")
        return (
            SkillCatalogService.revision_dir(
                revision.package_id, revision.revision_number
            )
            / "skills"
            / name
        )

    # ── Visibility / capability ────────────────────────────────────────

    @staticmethod
    def user_can_see(
        session: Session, package: SkillPackage, user: User
    ) -> bool:
        """The publisher; anyone once listed and public; a granted user; an
        admin once public.

        §4 says ``private`` means "the publisher only", and that is honoured
        literally: a private package is prompt text its author has not chosen
        to share, and an administrator has no product reason to read one.

        The one bypass is deliberately keyed on **visibility, not listing**: an
        admin may see a package the publisher made public even after it has
        been delisted. Without that, :meth:`delist` would be a trapdoor — its
        own output would 404 for the admin who produced it, a second delist
        would not be idempotent, and a publisher could re-list at will with the
        admin unable to look at what they were re-listing.

        A ``users`` package is visible to the people named on it, and — like a
        public one — only while it is listed: delisting is the administrator's
        one lever over a harmful package, and a grant must not be a way around
        it. The admin bypass is not extended here: ``users`` is an explicit
        allowlist, and an administrator is not on it.
        """
        if package.publisher_user_id and package.publisher_user_id == user.id:
            return True
        if package.visibility == SkillPackageVisibility.PUBLIC:
            return package.is_listed or user.is_superuser
        if package.visibility == SkillPackageVisibility.USERS:
            return package.is_listed and SkillCatalogService.user_has_grant(
                session, package, user.id
            )
        return False

    @staticmethod
    def user_can_manage(package: SkillPackage, user: User) -> bool:
        """Who may rename, re-describe or change the visibility of a package.

        The publisher, and only the publisher (§4). An administrator's power
        here is :meth:`delist` — a narrower verb on purpose, so "hide something
        harmful from the catalog" never widens into "edit or publish somebody
        else's unpublished skill".
        """
        return bool(
            package.publisher_user_id and package.publisher_user_id == user.id
        )

    # ── Publish ────────────────────────────────────────────────────────

    @staticmethod
    async def publish_from_agent(
        session: Session,
        *,
        agent: Agent,
        user: User,
        skill_name: str,
        version: str | None = None,
        release_notes: str | None = None,
        visibility: str | None = None,
        package_id: str | None = None,
        grant_emails: list[str] | None = None,
    ) -> SkillPackageRevision:
        """Publish ``skills/<skill_name>/`` from ``agent`` as a new revision.

        Order matters: every validator that can refuse runs **before** anything
        is written, because a revision is immutable and a half-published skill
        cannot be taken back.

        Raises:
            SkillCatalogError: with a code from
                :data:`~app.services.skills.exceptions.STATUS_BY_CODE`.
        """
        # 1. Authorization — developer role on an agent that is not a consumer
        #    install. The same gate bundle publish uses; a foreign install's
        #    workspace belongs to its publisher, not to the person holding it.
        try:
            AgentService.assert_can_build(session, user, agent)
        except CanBuildError as exc:
            raise SkillCatalogError(exc.reason, exc.message) from exc

        # 2. Locate the skill on disk. Deliberately the host-side workspace and
        #    not an adapter call — see the module docstring.
        skill_dir = SkillCatalogService._resolve_skill_dir(session, agent, skill_name)

        # 3. Validate. One parse, three refusals.
        entry = parse_skill_dir(skill_dir, rel_path=f"skills/{skill_name}")
        if entry.error is not None:
            raise SkillCatalogError(
                "skill_invalid",
                f"This skill cannot be published: {entry.error.message}",
            )
        if entry.secret_paths:
            raise SkillCatalogError(
                "skill_contains_secrets",
                "These files inside the skill look like credentials and must "
                "not be published: " + ", ".join(entry.secret_paths),
                paths=list(entry.secret_paths),
            )
        if entry.size_bytes > MAX_PACKAGE_BYTES:
            raise SkillCatalogError(
                "skill_too_large",
                f"This skill is {entry.size_bytes // (1024 * 1024)} MB; the "
                f"limit is {MAX_PACKAGE_BYTES // (1024 * 1024)} MB.",
            )

        visibility = SkillCatalogService._normalise_visibility(visibility)

        # 4. Resolve the people to share with BEFORE anything is written. An
        #    address that resolves to nobody is a typo, and a typo must not
        #    leave behind a published revision that cannot be un-published —
        #    the same reason every other validator above runs first.
        grant_targets = SkillCatalogService._resolve_grant_targets(
            session, grant_emails, publisher_user_id=user.id
        )

        lock = _publish_lock_for(f"{user.id}:{skill_name}")
        async with lock:
            # 5. Refuse addresses that a grant row could not act on. Grants are
            #    consulted by ``user_can_see`` and ``is_granted`` under
            #    ``visibility == "users"`` and nowhere else, so writing them under
            #    any other visibility does not share the skill — it leaves latent
            #    rows that a later :meth:`update_package` flipping the visibility to
            #    ``users`` turns into access nobody asked for. The one client that
            #    sends this field drops the emails itself; a second client (the CLI,
            #    a script) has to be told, and told before anything is written:
            #    ``_resolve_package`` below commits a package row.
            #
            #    Keyed on the EFFECTIVE visibility, never on the requested field
            #    alone. ``visibility=None`` is the documented "leave the package's
            #    current visibility alone" shape, so "re-publish an already-``users``
            #    package and add an address" has to keep working, while "add
            #    addresses to a ``public`` package without restating the visibility"
            #    has to be refused. Hence a read-only lookup of the package that
            #    already exists, and ``PRIVATE`` — the new-package default — when
            #    there is none.
            #
            #    Inside the publish lock, not in front of it: the lock is keyed on
            #    the same skill this reads the package for, so a second publish
            #    of it cannot commit a visibility change between this read and
            #    the grant write below. (A concurrent :meth:`update_package`
            #    still can — that path takes no lock — which is a narrower race
            #    than the one this closes, and inherent to a check-then-act.)
            #
            #    Keyed on the resolved targets rather than the raw list, because
            #    those are the rows this publish would actually write: a request
            #    naming only the publisher's own address writes nothing, and
            #    :meth:`_resolve_grant_targets` deliberately treats that as a no-op
            #    inside a publish rather than a refusal.
            if grant_targets:
                existing_package = SkillCatalogService._find_publisher_package(
                    session, publisher_user_id=user.id, entry_name=entry.name
                )
                effective_visibility = visibility or (
                    existing_package.visibility
                    if existing_package is not None
                    else SkillPackageVisibility.PRIVATE
                )
                if effective_visibility != SkillPackageVisibility.USERS:
                    raise SkillCatalogError(
                        "grants_require_users_visibility",
                        "Sharing a skill with named people only takes effect when "
                        "its visibility is 'users', and this publish would leave it "
                        f"'{effective_visibility}'. Set the visibility to 'users', "
                        "or publish without the email addresses.",
                    )

            package, created = SkillCatalogService._resolve_package(
                session,
                user=user,
                agent=agent,
                entry_name=entry.name,
                description=entry.description,
                requested_package_id=package_id,
            )

            next_number = (
                session.exec(
                    select(
                        func.coalesce(
                            func.max(SkillPackageRevision.revision_number), 0
                        )
                    ).where(SkillPackageRevision.package_id == package.id)
                ).one()
                or 0
            ) + 1

            revision_dir = SkillCatalogService.revision_dir(package.id, next_number)
            content_hash, size_bytes, archive_sha256 = await asyncio.to_thread(
                SkillCatalogService._write_snapshot_to_disk,
                package_uuid=package.id,
                skill_dir=skill_dir,
                skill_name=entry.name,
                revision_dir=revision_dir,
                revision_number=next_number,
            )

            revision = SkillPackageRevision(
                package_id=package.id,
                revision_number=next_number,
                version=version or None,
                frontmatter=dict(entry.frontmatter or {}),
                snapshot_path=str(revision_dir / "skills" / entry.name),
                content_hash=content_hash,
                archive_sha256=archive_sha256,
                size_bytes=size_bytes,
                release_notes=release_notes or None,
                published_by_user_id=user.id,
            )
            session.add(revision)
            session.commit()
            session.refresh(revision)

            SkillCatalogService._apply_publish_to_package(
                session,
                package=package,
                revision=revision,
                entry_description=entry.description,
                created=created,
                visibility=visibility,
                agent=agent,
                grant_targets=grant_targets,
                granted_by=user,
            )

        logger.info(
            "skill_published package_id=%s revision=%s agent_id=%s size=%s",
            package.package_id, revision.revision_number, agent.id, size_bytes,
        )
        return revision

    @staticmethod
    def _resolve_skill_dir(
        session: Session, agent: Agent, skill_name: str
    ) -> Path:
        """The publisher workspace's ``skills/<name>/`` folder, or a coded 409.

        Three distinguishable failures, because they have three different
        fixes: no environment (create one), a workspace that was never
        materialised (start the environment once), and no such skill (the
        card is stale — refresh it).
        """
        from app.models.environments.environment import AgentEnvironment

        if not SKILL_NAME_RE.match(skill_name or ""):
            # Guards the path join as much as it validates: the name becomes a
            # path segment two lines below.
            raise SkillCatalogError("skill_not_found", "Skill not found")

        env = (
            session.get(AgentEnvironment, agent.active_environment_id)
            if agent.active_environment_id
            else None
        )
        if env is None:
            raise SkillCatalogError(
                "no_environment",
                "This agent has no environment, so it has no skills to "
                "publish. Create one first.",
            )

        workspace_root = (
            Path(settings.ENV_INSTANCES_DIR) / str(env.id) / WORKSPACE_ROOT_REL
        )
        if not workspace_root.is_dir():
            raise SkillCatalogError(
                "workspace_unavailable",
                "This agent's workspace is not on disk yet. Start the "
                "environment once so its files are materialised, then publish.",
            )

        skill_dir = workspace_root / "skills" / skill_name
        if not skill_dir.is_dir() or skill_dir.is_symlink():
            raise SkillCatalogError(
                "skill_not_found",
                f"There is no skills/{skill_name}/ folder in this agent's "
                "workspace — refresh the skills list.",
            )
        return skill_dir

    @staticmethod
    def _normalise_visibility(visibility: str | None) -> str | None:
        if visibility is None:
            return None
        if visibility not in (
            SkillPackageVisibility.PRIVATE,
            SkillPackageVisibility.PUBLIC,
            SkillPackageVisibility.USERS,
        ):
            raise SkillCatalogError(
                "invalid_visibility",
                "Visibility must be 'private', 'users' or 'public'.",
            )
        return visibility

    @staticmethod
    def _resolve_grant_targets(
        session: Session,
        emails: list[str] | None,
        *,
        publisher_user_id: uuid.UUID,
    ) -> list[User]:
        """Resolve publish-time ``grant_emails`` to users, deduplicated.

        An address that resolves to nobody raises ``user_not_found`` on the
        first bad one, before the caller has written anything — a typo must not
        be able to leave an immutable revision behind.

        The publisher's own address is the one refusal that is **dropped** here
        rather than raised. On the dedicated grant endpoint it is a 409, where
        the whole request was "share with this person" and answering it is the
        point. Inside a publish it names a no-op, and failing an expensive,
        one-way operation over a redundant entry would be the wrong trade.

        Addresses are deduplicated **before** the lookups, not after. The
        resolved-user dedupe below still catches two spellings of one account,
        but only once each spelling has cost its own ``lower(email)`` scan —
        and that column has no functional index. Folding the list to a set of
        trimmed, lowercased addresses first makes one address repeated cost one
        query, which is what a client that submits its dialog twice sends.
        """
        addresses = dict.fromkeys(
            trimmed.lower()
            for trimmed in ((email or "").strip() for email in emails or [])
            if trimmed
        )

        targets: list[User] = []
        seen: set[uuid.UUID] = {publisher_user_id}
        for email in addresses:
            target = SkillCatalogService.resolve_grant_user(
                session, email, publisher_user_id=None
            )
            if target.id in seen:
                continue
            seen.add(target.id)
            targets.append(target)
        return targets

    @staticmethod
    def _find_publisher_package(
        session: Session,
        *,
        publisher_user_id: uuid.UUID,
        entry_name: str,
    ) -> SkillPackage | None:
        """This publisher's package for ``entry_name``, or ``None``. Read-only.

        Package identity is per publisher, so that pair is the whole key. Split
        out because the publish path needs the lookup twice with two different
        rights: the grant pre-flight has to read the package's *current*
        visibility while nothing may be written yet, and
        :meth:`_resolve_package` creates and commits a row when the lookup
        misses. One query, one predicate, no chance of the two drifting.
        """
        return session.exec(
            select(SkillPackage).where(
                SkillPackage.publisher_user_id == publisher_user_id,
                SkillPackage.name == entry_name,
            )
        ).first()

    @staticmethod
    def _resolve_package(
        session: Session,
        *,
        user: User,
        agent: Agent,
        entry_name: str,
        description: str,
        requested_package_id: str | None,
    ) -> tuple[SkillPackage, bool]:
        """Find this publisher's package for ``entry_name``, or create it.

        Returns ``(package, created)``. Identity is per publisher (§9): two
        people may both publish ``pdf-report``; ``package_id`` disambiguates.
        """
        existing = SkillCatalogService._find_publisher_package(
            session, publisher_user_id=user.id, entry_name=entry_name
        )

        if existing is not None:
            if (
                requested_package_id
                and requested_package_id.strip() != existing.package_id
            ):
                # Refuse rather than ignore: the caller asked for an id, and a
                # published package's id is immutable (installs and every
                # consumer's manifest reference it).
                raise SkillCatalogError(
                    "package_id_immutable",
                    f"This skill is already published as "
                    f"'{existing.package_id}'; a package id cannot be changed.",
                )
            return existing, False

        package_id = (requested_package_id or "").strip() or (
            SkillCatalogService.default_package_id(user.id, entry_name)
        )
        if not BUNDLE_ID_REGEX.match(package_id):
            raise SkillCatalogError(
                "package_id_invalid",
                "A package id must be a reverse-DNS name, e.g. "
                "io.example.team.pdf-report.",
            )
        taken = session.exec(
            select(SkillPackage).where(SkillPackage.package_id == package_id)
        ).first()
        if taken is not None:
            raise SkillCatalogError(
                "package_id_taken",
                f"The package id '{package_id}' is already in use on this "
                "instance. Choose another one.",
            )

        package = SkillPackage(
            package_id=package_id,
            name=entry_name,
            display_name=entry_name,
            description=description or None,
            publisher_user_id=user.id,
            source_agent_id=agent.id,
            visibility=SkillPackageVisibility.PRIVATE,
            is_listed=True,
        )
        session.add(package)
        session.commit()
        session.refresh(package)
        return package, True

    @staticmethod
    def default_package_id(publisher_user_id: uuid.UUID, skill_name: str) -> str:
        """``<reversed host>.<publisher slug>.<skill name>``.

        Mirrors :meth:`BundleIdService.generate_bundle_id` — same reversed host
        prefix, same 8-hex-char owner slug — so the two id families read as one
        namespace rather than two conventions.
        """
        prefix = BundleIdService.reversed_host_prefix()
        return f"{prefix}.{str(publisher_user_id)[:8]}.{skill_name}"

    @staticmethod
    def _write_snapshot_to_disk(
        *,
        package_uuid: uuid.UUID,
        skill_dir: Path,
        skill_name: str,
        revision_dir: Path,
        revision_number: int,
    ) -> tuple[str, int, str]:
        """Copy the skill tree into immutable storage. Runs OFF the event loop.

        ``<rev>.tmp/skills/<name>/`` is filled first and moved into place, so a
        reader can never observe a half-written revision. ``safe_copytree``
        refuses symlinks at every depth — the workspace is agent-controlled, and
        a symlink out of it would publish somebody's private file.

        The archive is built and cached here too, in the same thread, so its
        digest can be stored on the row: the plugin manifest carries that digest
        and is built on the event loop, where reading or gzipping a snapshot
        would stall every other request on the worker.

        Returns ``(content_hash, size_bytes, archive_sha256)``. ``content_hash``
        is ``PublishService.hash_workspace_tree`` over the snapshot root — path
        + bytes with no timestamps, so two identical publishes hash equal.

        NOTE on the ``rmtree`` of an existing ``revision_dir``: revision numbers
        are allocated under ``_publish_locks``, which is per process, and the
        backend runs a single worker (see ``docker-compose.yml``), so a live
        collision cannot happen. What the delete does handle is the leftover of
        an earlier publish that wrote files and then failed before its row
        committed — refusing there instead would wedge the package's publishing
        forever. Same trade-off, same code, as
        ``PublishService._write_snapshot_to_disk``.
        """
        tmp_dir = revision_dir.with_suffix(".tmp")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

        dest = tmp_dir / "skills" / skill_name
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        safe_copytree(skill_dir, dest)

        content_hash = PublishService.hash_workspace_tree(tmp_dir)
        size_bytes = sum(
            f.stat().st_size for f in tmp_dir.rglob("*") if f.is_file()
        )

        if revision_dir.exists():
            shutil.rmtree(revision_dir)
        revision_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        shutil.move(str(tmp_dir), str(revision_dir))

        data = SkillCatalogService._build_archive_bytes(
            revision_dir / "skills" / skill_name, skill_name
        )
        SkillCatalogService._cache_archive(package_uuid, revision_number, data)
        return content_hash, size_bytes, hashlib.sha256(data).hexdigest()

    @staticmethod
    def _apply_publish_to_package(
        session: Session,
        *,
        package: SkillPackage,
        revision: SkillPackageRevision,
        entry_description: str,
        created: bool,
        visibility: str | None,
        agent: Agent,
        grant_targets: list[User] | None = None,
        granted_by: User | None = None,
    ) -> None:
        """Point the package at the new revision and refresh its metadata.

        The description follows the skill's own frontmatter **until a publisher
        edits it**. §5.3 says publish "updates description"; taken literally
        that would silently overwrite the catalog blurb someone wrote in the
        edit dialog on their next publish. So it is carried over only while the
        stored value still equals what the previous revision declared.
        """
        previous_description: str | None = None
        if package.latest_revision_id:
            previous = session.get(SkillPackageRevision, package.latest_revision_id)
            if previous is not None:
                previous_description = (previous.frontmatter or {}).get(
                    "description"
                )

        if created or package.description in (None, "", previous_description):
            package.description = entry_description or None

        package.latest_revision_id = revision.id
        package.source_agent_id = agent.id
        if visibility is not None:
            package.visibility = visibility
        package.updated_at = datetime.now(UTC)
        session.add(package)

        # Grants ride the same commit as the package row — the FINAL commit of
        # the publish, not the one that wrote the revision. A publish that says
        # "share this with Ana" therefore cannot land as a published package
        # Ana cannot see, but the revision row is already committed by the time
        # we get here: if this commit fails, the grants roll back together with
        # ``latest_revision_id`` and the visibility change, leaving an orphan
        # revision behind. That window is pre-existing — the revision has always
        # been committed separately — and grants only add another passenger to
        # it, so it is documented here rather than papered over with a partial
        # restructure. What the resolution order *does* guarantee is that a bad
        # address never gets this far: ``_resolve_grant_targets`` runs before
        # the lock and before anything is written.
        #
        # Additive — a re-publish never revokes an address it omits, because
        # the dialog it came from may simply be out of date.
        if granted_by is not None:
            for target in grant_targets or []:
                SkillCatalogService.grant_to_user(
                    session, package, target, granted_by, commit=False
                )

        session.commit()
        session.refresh(package)

    # ── Read ───────────────────────────────────────────────────────────

    @staticmethod
    def list_catalog(session: Session, user: User) -> list[SkillPackageEntry]:
        """Every package this user may see, newest first.

        Filtering (all / public / mine / installed) is client-side over this
        list, exactly as the bundle catalog does: the result set is one row per
        published skill on the instance, and paging a list that small buys
        nothing but a worse filter experience.
        """
        visible_stmt = select(SkillPackage).where(
            SkillPackage.is_listed == True,  # noqa: E712
            SkillPackage.visibility == SkillPackageVisibility.PUBLIC,
        )
        packages: dict[uuid.UUID, SkillPackage] = {
            p.id: p for p in session.exec(visible_stmt).all()
        }
        # Packages shared with this user by name. Listed-only, exactly like
        # public ones: a grant is the publisher's lever, delisting is the
        # administrator's, and a grant must not be a way around it.
        granted_stmt = (
            select(SkillPackage)
            .join(
                SkillPackageAccessGrant,
                SkillPackageAccessGrant.package_id == SkillPackage.id,
            )
            .where(
                SkillPackage.is_listed == True,  # noqa: E712
                SkillPackage.visibility == SkillPackageVisibility.USERS,
                SkillPackageAccessGrant.user_id == user.id,
            )
        )
        for package in session.exec(granted_stmt).all():
            packages[package.id] = package

        own_stmt = select(SkillPackage).where(
            SkillPackage.publisher_user_id == user.id
        )
        for package in session.exec(own_stmt).all():
            packages[package.id] = package

        # A package whose publish failed between creating the row and committing
        # its first revision has nothing to show; it is private, it self-heals
        # on the publisher's next attempt, and listing it would put an empty
        # card in the catalog.
        rows = [p for p in packages.values() if p.latest_revision_id is not None]

        # One pass of grouped queries instead of four lookups per package: the
        # catalog is a grid, and 4N+2 queries is how a list endpoint becomes the
        # slowest page in the product. Same discipline as
        # ``AgentService.compute_capability_flags``.
        ctx = SkillCatalogService._entry_context(session, rows, user)
        entries = [
            SkillCatalogService.package_to_entry(session, package, user, ctx=ctx)
            for package in rows
        ]
        entries.sort(key=lambda e: e.updated_at, reverse=True)
        return entries

    @staticmethod
    def get_package(
        session: Session, package_uuid: uuid.UUID, user: User
    ) -> SkillPackage:
        """Load a package the caller may see, or raise ``package_not_found``.

        An invisible package answers 404, not 403: a private package's
        existence is not public information.
        """
        package = session.get(SkillPackage, package_uuid)
        if package is None or not SkillCatalogService.user_can_see(
            session, package, user
        ):
            raise SkillCatalogError("package_not_found", "Skill package not found")
        return package

    @staticmethod
    def get_revision(
        session: Session, package: SkillPackage, revision_number: int
    ) -> SkillPackageRevision:
        revision = session.exec(
            select(SkillPackageRevision).where(
                SkillPackageRevision.package_id == package.id,
                SkillPackageRevision.revision_number == revision_number,
            )
        ).first()
        if revision is None:
            raise SkillCatalogError("revision_not_found", "Revision not found")
        return revision

    @staticmethod
    def resolve_revision(
        session: Session,
        package: SkillPackage,
        revision_number: int | None = None,
    ) -> SkillPackageRevision:
        """The named revision, or the package's latest when none is named.

        The install route needs "whatever is current" and the upgrade path needs
        "revision N"; keeping the choice here means a route never has to reason
        about ``latest_revision_id`` being NULL, and both answers come back as
        one row from one query.
        """
        if revision_number is not None:
            return SkillCatalogService.get_revision(
                session, package, revision_number
            )
        revision = (
            session.get(SkillPackageRevision, package.latest_revision_id)
            if package.latest_revision_id
            else None
        )
        if revision is None:
            raise SkillCatalogError(
                "no_revision", "This package has no published revision yet."
            )
        return revision

    @staticmethod
    def list_revisions(
        session: Session, package: SkillPackage
    ) -> list[SkillPackageRevision]:
        return list(
            session.exec(
                select(SkillPackageRevision)
                .where(SkillPackageRevision.package_id == package.id)
                .order_by(SkillPackageRevision.revision_number.desc())
            ).all()
        )

    @staticmethod
    def read_revision_content(revision: SkillPackageRevision) -> tuple[str, bool]:
        """The revision's ``SKILL.md`` text, truncated at the preview cap."""
        skill_md = SkillCatalogService.skill_dir_of(revision) / "SKILL.md"
        try:
            raw = skill_md.read_bytes()
        except OSError as exc:
            # ``snapshot_missing`` (410), not ``archive_unavailable`` (503):
            # the snapshot is immutable, so a file that is not there will not
            # be there on a retry either, and telling a reader to try again
            # would be a lie.
            logger.error(
                "skill_snapshot_missing revision=%s package=%s number=%s path=%s",
                revision.id, revision.package_id, revision.revision_number,
                skill_md,
            )
            raise SkillCatalogError(
                "snapshot_missing",
                "The published files for this revision are no longer on disk.",
            ) from exc
        truncated = len(raw) > MAX_CONTENT_BYTES
        if truncated:
            raw = raw[:MAX_CONTENT_BYTES]
        return raw.decode("utf-8", errors="replace"), truncated

    # ── Projections ────────────────────────────────────────────────────

    @staticmethod
    def revision_to_public(
        revision: SkillPackageRevision,
    ) -> SkillPackageRevisionPublic:
        return SkillPackageRevisionPublic(
            id=revision.id,
            package_id=revision.package_id,
            revision_number=revision.revision_number,
            version=revision.version,
            frontmatter=revision.frontmatter or {},
            content_hash=revision.content_hash,
            size_bytes=revision.size_bytes,
            release_notes=revision.release_notes,
            published_by_user_id=revision.published_by_user_id,
            published_at=revision.published_at,
        )

    @staticmethod
    def _entry_context(
        session: Session, packages: list[SkillPackage], user: User
    ) -> "_EntryContext":
        """Resolve every per-package projection input in four grouped queries.

        Built once for a listing and passed into :meth:`package_to_entry`; the
        detail route builds one for its single package, so both paths run the
        same projection code with no special case.
        """
        package_ids = [p.id for p in packages]
        revision_ids = [
            p.latest_revision_id for p in packages if p.latest_revision_id
        ]
        publisher_ids = {
            p.publisher_user_id for p in packages if p.publisher_user_id
        }

        revisions: dict[uuid.UUID, SkillPackageRevision] = {}
        if revision_ids:
            revisions = {
                r.id: r
                for r in session.exec(
                    select(SkillPackageRevision).where(
                        SkillPackageRevision.id.in_(revision_ids)
                    )
                ).all()
            }

        publishers: dict[uuid.UUID, User] = {}
        if publisher_ids:
            publishers = {
                u.id: u
                for u in session.exec(
                    select(User).where(User.id.in_(publisher_ids))
                ).all()
            }

        install_counts: dict[uuid.UUID, int] = {}
        installed_in: dict[uuid.UUID, list[uuid.UUID]] = {}
        if package_ids:
            # Consumer installs per package. The publisher-exclusion is applied
            # in Python rather than in SQL because it is per package (each row
            # has its own publisher) and a correlated condition would defeat
            # the grouping this method exists for.
            rows = session.exec(
                select(
                    SkillPackageRevision.package_id,
                    AgentPluginLink.agent_id,
                    Agent.owner_id,
                )
                .select_from(AgentPluginLink)
                .join(
                    SkillPackageRevision,
                    SkillPackageRevision.id
                    == AgentPluginLink.skill_package_revision_id,
                )
                .join(Agent, Agent.id == AgentPluginLink.agent_id)
                .where(SkillPackageRevision.package_id.in_(package_ids))
            ).all()
            publisher_by_package = {
                p.id: p.publisher_user_id for p in packages
            }
            counted: dict[uuid.UUID, set[uuid.UUID]] = {}
            for pkg_id, agent_id, owner_id in rows:
                publisher_id = publisher_by_package.get(pkg_id)
                if publisher_id is None or owner_id != publisher_id:
                    counted.setdefault(pkg_id, set()).add(agent_id)
                if owner_id == user.id:
                    mine = installed_in.setdefault(pkg_id, [])
                    if agent_id not in mine:
                        mine.append(agent_id)
            install_counts = {k: len(v) for k, v in counted.items()}
            # Stable order: the rows come back in whatever order Postgres
            # chooses, and an identical refetch should not reshuffle a response
            # body the client may be diffing.
            for agent_ids in installed_in.values():
                agent_ids.sort(key=str)

        granted_package_ids: set[uuid.UUID] = set()
        if package_ids:
            granted_package_ids = set(
                session.exec(
                    select(SkillPackageAccessGrant.package_id).where(
                        SkillPackageAccessGrant.package_id.in_(package_ids),
                        SkillPackageAccessGrant.user_id == user.id,
                    )
                ).all()
            )

        return _EntryContext(
            viewer_id=user.id,
            revisions=revisions,
            publishers=publishers,
            install_counts=install_counts,
            installed_in=installed_in,
            granted_package_ids=granted_package_ids,
        )

    @staticmethod
    def package_to_entry(
        session: Session,
        package: SkillPackage,
        user: User,
        *,
        ctx: "_EntryContext | None" = None,
    ) -> SkillPackageEntry:
        """Project one package for one viewer.

        ``ctx`` carries the grouped lookups when this is one row of a listing;
        without it the method resolves its own, which is what the single-package
        routes want.
        """
        if ctx is None:
            ctx = SkillCatalogService._entry_context(session, [package], user)
        elif ctx.viewer_id != user.id:
            # ``installed_in`` is per viewer; a mismatched context would report
            # another person's agents on this user's card.
            raise ValueError(
                "Entry context was built for a different viewer"
            )

        latest = (
            ctx.revisions.get(package.latest_revision_id)
            if package.latest_revision_id
            else None
        )
        publisher = (
            ctx.publishers.get(package.publisher_user_id)
            if package.publisher_user_id
            else None
        )

        return SkillPackageEntry(
            id=package.id,
            package_id=package.package_id,
            name=package.name,
            display_name=package.display_name,
            description=package.description,
            publisher_user_id=package.publisher_user_id,
            publisher_name=(publisher.full_name or None) if publisher else None,
            publisher_email=(publisher.email or None) if publisher else None,
            publisher_email_confirmed=(
                bool(publisher.email_confirmed) if publisher else False
            ),
            source_agent_id=package.source_agent_id,
            latest_revision_id=package.latest_revision_id,
            latest_revision_number=latest.revision_number if latest else None,
            latest_version=latest.version if latest else None,
            visibility=package.visibility,
            is_listed=package.is_listed,
            created_at=package.created_at,
            updated_at=package.updated_at,
            latest_revision=(
                SkillCatalogService.revision_to_public(latest) if latest else None
            ),
            install_count=ctx.install_counts.get(package.id, 0),
            installed_in_agent_ids=ctx.installed_in.get(package.id, []),
            can_manage=SkillCatalogService.user_can_manage(package, user),
            is_granted=(
                package.visibility == SkillPackageVisibility.USERS
                and package.publisher_user_id != user.id
                and package.id in ctx.granted_package_ids
            ),
        )

    @staticmethod
    def package_to_detail(
        session: Session, package: SkillPackage, user: User
    ) -> SkillPackageDetailPublic:
        entry = SkillCatalogService.package_to_entry(session, package, user)
        return SkillPackageDetailPublic(
            **entry.model_dump(),
            revisions=[
                SkillCatalogService.revision_to_public(r)
                for r in SkillCatalogService.list_revisions(session, package)
            ],
        )

    # ── Manage ─────────────────────────────────────────────────────────

    @staticmethod
    def update_package(
        session: Session,
        package: SkillPackage,
        user: User,
        data: SkillPackageUpdate,
    ) -> SkillPackage:
        if not SkillCatalogService.user_can_manage(package, user):
            raise SkillCatalogError(
                "not_publisher", "Only the publisher can change this package."
            )
        if data.display_name is not None:
            # An all-whitespace name passes the schema's min_length but would
            # leave the package with no readable label.
            display_name = data.display_name.strip()[:255]
            if not display_name:
                raise SkillCatalogError(
                    "invalid_display_name", "A package needs a name."
                )
            package.display_name = display_name
        if data.description is not None:
            package.description = data.description or None
        if data.visibility is not None:
            package.visibility = SkillCatalogService._normalise_visibility(
                data.visibility
            )
        if data.is_listed is not None:
            package.is_listed = data.is_listed
        package.updated_at = datetime.now(UTC)
        session.add(package)
        session.commit()
        session.refresh(package)
        return package

    @staticmethod
    def delist(
        session: Session, package: SkillPackage, user: User
    ) -> SkillPackage:
        """Hide a package from the catalog. Superuser only — never a delete.

        Existing installs keep working: their link points at a revision, and
        delisting touches neither. That is the whole point of having a separate
        verb instead of offering an admin a delete button.
        """
        if not user.is_superuser:
            raise SkillCatalogError(
                "not_superuser", "Only an administrator can delist a package."
            )
        package.is_listed = False
        package.updated_at = datetime.now(UTC)
        session.add(package)
        session.commit()
        session.refresh(package)
        logger.info(
            "skill_package_delisted package_id=%s by=%s", package.package_id, user.id
        )
        return package

    # ── Access grants (``visibility='users'``) ──────────────────────────

    @staticmethod
    def user_has_grant(
        session: Session, package: SkillPackage, user_id: uuid.UUID
    ) -> bool:
        return (
            session.exec(
                select(SkillPackageAccessGrant).where(
                    SkillPackageAccessGrant.package_id == package.id,
                    SkillPackageAccessGrant.user_id == user_id,
                )
            ).first()
            is not None
        )

    @staticmethod
    def list_grants(
        session: Session, package: SkillPackage
    ) -> list[SkillPackageAccessGrant]:
        """Every grant on one package, newest first."""
        return list(
            session.exec(
                select(SkillPackageAccessGrant)
                .where(SkillPackageAccessGrant.package_id == package.id)
                .order_by(SkillPackageAccessGrant.created_at.desc())
            ).all()
        )

    @staticmethod
    def resolve_grant_user(
        session: Session, email: str, *, publisher_user_id: uuid.UUID | None
    ) -> User:
        """The user behind an email a publisher typed, or a coded refusal.

        The comparison lowercases **both sides**. Addresses are stored
        lowercased today, but they have not always been, and a publisher types
        an address the way their colleague writes it — a case-sensitive match
        would silently fail to share with a real account, which looks to the
        publisher like the person does not exist.
        """
        normalised = (email or "").strip()
        if not normalised:
            raise SkillCatalogError(
                "user_not_found", "No user with that email exists on this instance."
            )
        target = session.exec(
            select(User).where(func.lower(User.email) == func.lower(normalised))
        ).first()
        if target is None:
            raise SkillCatalogError(
                "user_not_found",
                f"No user with the email '{normalised}' exists on this instance.",
            )
        if publisher_user_id is not None and target.id == publisher_user_id:
            raise SkillCatalogError(
                "self_grant",
                "You publish this skill, so you can already see it — there is "
                "nothing to grant.",
            )
        return target

    @staticmethod
    def grant_access(
        session: Session,
        package: SkillPackage,
        email: str,
        granted_by: User,
    ) -> SkillPackageAccessGrant:
        """Grant catalog visibility to the user behind an email. Idempotent.

        Deliberately **permissive about visibility**, where publish is not: a
        publisher who grants three colleagues on a still-``private`` package and
        then flips it to ``users`` is preparing it, and that is the ordinary way
        the dialog is used. What publish refuses is the *side effect* — a
        request whose stated visibility contradicts the addresses it carries, so
        the rows would be written by a caller that never asked for them. Here
        the whole request is "let this person see it", stated per person, which
        is intent a route has no business second-guessing.
        """
        target = SkillCatalogService.resolve_grant_user(
            session, email, publisher_user_id=package.publisher_user_id
        )
        return SkillCatalogService.grant_to_user(
            session, package, target, granted_by
        )

    @staticmethod
    def grant_to_user(
        session: Session,
        package: SkillPackage,
        target: User,
        granted_by: User,
        *,
        commit: bool = True,
    ) -> SkillPackageAccessGrant:
        """Write one grant. The single place the idempotency rule lives.

        Re-granting somebody who already holds a grant returns the existing row
        rather than refusing: the publisher's intent ("this person can see it")
        is already true, and a 409 would make a retried request look like a
        failure.

        ``commit=False`` leaves the row pending in the caller's transaction —
        that is how publish makes the grants and the package land together or
        not at all.
        """
        existing = session.exec(
            select(SkillPackageAccessGrant).where(
                SkillPackageAccessGrant.package_id == package.id,
                SkillPackageAccessGrant.user_id == target.id,
            )
        ).first()
        if existing is not None:
            return existing

        grant = SkillPackageAccessGrant(
            package_id=package.id,
            user_id=target.id,
            granted_by_user_id=granted_by.id,
        )
        session.add(grant)
        if commit:
            session.commit()
            session.refresh(grant)
        return grant

    @staticmethod
    def revoke_grant(
        session: Session, package: SkillPackage, user_id: uuid.UUID
    ) -> None:
        """Remove one user's grant.

        Keyed on the *user*, not on the grant id: the publisher's mental model
        is "remove this person", and a card that has to hold a grant id to do
        that breaks the moment the list is refetched.

        This hides the package from that user's catalog. It does **not** touch
        an install they already made — the archive route authorises on the
        install, never on visibility, so a running agent keeps working.
        """
        grant = session.exec(
            select(SkillPackageAccessGrant).where(
                SkillPackageAccessGrant.package_id == package.id,
                SkillPackageAccessGrant.user_id == user_id,
            )
        ).first()
        if grant is None:
            raise SkillCatalogError(
                "grant_not_found", "That user has no access to this skill."
            )
        session.delete(grant)
        session.commit()

    @staticmethod
    def list_grants_public(
        session: Session, package: SkillPackage
    ) -> list[SkillPackageAccessGrantPublic]:
        """Every grant on one package, projected in two queries.

        The per-grant email lookup is resolved once for the whole list: a
        sharing card is a list, and one query per row is how a small card
        becomes the slowest thing on a page.
        """
        grants = SkillCatalogService.list_grants(session, package)
        if not grants:
            return []
        users = {
            u.id: u
            for u in session.exec(
                select(User).where(User.id.in_([g.user_id for g in grants]))
            ).all()
        }
        return [
            SkillCatalogService.grant_to_public(
                session, grant, users.get(grant.user_id)
            )
            for grant in grants
        ]

    @staticmethod
    def grant_to_public(
        session: Session,
        grant: SkillPackageAccessGrant,
        user: User | None = None,
    ) -> SkillPackageAccessGrantPublic:
        """Project a grant, resolving the granted user's current email."""
        if user is None:
            user = session.get(User, grant.user_id)
        return SkillPackageAccessGrantPublic(
            id=grant.id,
            package_id=grant.package_id,
            user_id=grant.user_id,
            user_email=user.email if user is not None else None,
            granted_by_user_id=grant.granted_by_user_id,
            created_at=grant.created_at,
        )

    # ── Install / upgrade / uninstall ───────────────────────────────────

    @staticmethod
    def install_into_agent(
        session: Session,
        *,
        agent: Agent,
        package: SkillPackage,
        revision: SkillPackageRevision,
        conversation_mode: bool = True,
        building_mode: bool = True,
    ) -> AgentPluginLink:
        """Create the ``source=catalog`` link. The caller syncs environments.

        The revision must belong to the package: a link pinned to another
        package's content would install the wrong files under the right name,
        and nothing downstream would notice.

        Dedupe is service-layer on
        ``(agent_id, "cinna-skills", package.name)`` — the same NULL-tolerant
        rule bundle links use, because the table's unique index only covers
        ``plugin_id`` and Postgres treats NULLs as distinct.
        """
        if revision.package_id != package.id:
            raise SkillCatalogError(
                "revision_not_found",
                "That revision does not belong to this package.",
            )

        existing = session.exec(
            select(AgentPluginLink).where(
                AgentPluginLink.agent_id == agent.id,
                AgentPluginLink.snapshot_marketplace_name
                == CATALOG_MARKETPLACE_NAME,
                AgentPluginLink.snapshot_plugin_name == package.name,
            )
        ).first()
        if existing is not None:
            # Skill names are unique per publisher, not globally (§9), but one
            # agent has a single ``plugins/cinna-skills/<name>/`` directory —
            # so two publishers' ``pdf-report`` packages genuinely cannot both
            # live in the same agent. Name the OTHER package when that is what
            # happened, rather than claiming this one is already installed:
            # the two situations need different words and different next steps.
            other = SkillCatalogService.package_of_link(session, existing)
            if other is not None and other.id != package.id:
                raise SkillCatalogError(
                    "name_conflict",
                    f"This agent already has a catalog skill named "
                    f"'{package.name}', from '{other.package_id}'. Two skills "
                    "cannot share a name in one agent — remove that one first.",
                )
            raise SkillCatalogError(
                "already_installed",
                f"'{package.display_name}' is already added to this agent.",
            )

        link = AgentPluginLink(
            agent_id=agent.id,
            plugin_id=None,
            source=PluginSource.catalog,
            snapshot_marketplace_name=CATALOG_MARKETPLACE_NAME,
            snapshot_plugin_name=package.name,
            snapshot_config={
                "name": package.name,
                "description": package.description,
                "version": revision.version,
            },
            skill_package_revision_id=revision.id,
            installed_version=revision.version,
            installed_commit_hash=None,
            conversation_mode=conversation_mode,
            building_mode=building_mode,
        )
        session.add(link)
        session.commit()
        session.refresh(link)
        logger.info(
            "skill_installed package_id=%s revision=%s agent_id=%s",
            package.package_id, revision.revision_number, agent.id,
        )
        return link

    @staticmethod
    def upgrade_link(
        session: Session, link: AgentPluginLink
    ) -> AgentPluginLink:
        """Re-pin a catalog link to its package's latest revision.

        Idempotent: a link already on the latest revision is returned
        unchanged, so the generic upgrade route can call this without first
        asking whether there is anything to do.
        """
        if link.source != PluginSource.catalog:
            raise SkillCatalogError(
                "not_a_catalog_link", "This plugin did not come from the catalog."
            )
        package = SkillCatalogService.package_of_link(session, link)
        if package is None or package.latest_revision_id is None:
            raise SkillCatalogError(
                "no_revision",
                "The catalog entry behind this skill is no longer available.",
            )
        latest = session.get(SkillPackageRevision, package.latest_revision_id)
        if latest is None:
            raise SkillCatalogError(
                "no_revision",
                "The catalog entry behind this skill is no longer available.",
            )
        if link.skill_package_revision_id != latest.id:
            link.skill_package_revision_id = latest.id
            link.installed_version = latest.version
            link.snapshot_config = {
                "name": package.name,
                "description": package.description,
                "version": latest.version,
            }
            link.updated_at = datetime.now(UTC)
            session.add(link)
            session.commit()
            session.refresh(link)
        return link

    # Uninstall is deliberately NOT a verb here. Removing a catalog skill is
    # `DELETE /llm-plugins/agents/{agent_id}/plugins/{link_id}` like every other
    # plugin: the row delete is source-agnostic and the container's prune step
    # removes the directory of anything missing from the manifest, so a second
    # entry point would only be a second thing to keep correct (plan §5.3,
    # "reuse LLMPluginService.uninstall_plugin_from_agent").

    @staticmethod
    def package_of_link(
        session: Session, link: AgentPluginLink
    ) -> SkillPackage | None:
        """The package behind a catalog link, or None when it is orphaned."""
        if link.skill_package_revision_id is None:
            return None
        revision = session.get(
            SkillPackageRevision, link.skill_package_revision_id
        )
        if revision is None:
            return None
        return session.get(SkillPackage, revision.package_id)

    # ── Archive ────────────────────────────────────────────────────────

    @staticmethod
    def build_archive(revision: SkillPackageRevision) -> tuple[bytes, str]:
        """Return ``(tar.gz bytes, sha256 hex)`` for one revision.

        The tarball holds the snapshot verbatim under ``skills/<name>/`` and
        nothing else. It deliberately does **not** carry the
        ``.claude-plugin/plugin.json`` §8 mentions: that file is package
        metadata (display name, description) which a publisher can edit without
        cutting a new revision, so baking it in would freeze stale text into an
        immutable artifact. The container synthesises it from the manifest
        entry instead, which is also what §5.3's ``_ensure_catalog_plugin``
        specifies.

        The archive is **deterministic** — sorted members, zeroed timestamps and
        ownership, gzip mtime 0 — so a cache rebuilt after an eviction still
        matches ``revision.archive_sha256``. Only the executable bit survives
        from the source mode (see :meth:`_build_archive_bytes`).

        Normally a no-op read: the archive was built and cached at publish.
        """
        cache = SkillCatalogService.archive_path(
            revision.package_id, revision.revision_number
        )
        if cache.is_file():
            try:
                data = cache.read_bytes()
                return data, hashlib.sha256(data).hexdigest()
            except OSError:
                logger.warning("skill_archive_cache_unreadable path=%s", cache)

        skill_dir = SkillCatalogService.skill_dir_of(revision)
        if not skill_dir.is_dir():
            raise SkillCatalogError(
                "snapshot_missing",
                "The published files for this revision are no longer on disk.",
            )

        data = SkillCatalogService._build_archive_bytes(skill_dir, skill_dir.name)
        SkillCatalogService._cache_archive(
            revision.package_id, revision.revision_number, data
        )
        return data, hashlib.sha256(data).hexdigest()

    @staticmethod
    def _cache_archive(
        package_uuid: uuid.UUID, revision_number: int, data: bytes
    ) -> None:
        """Write the built tarball beside its snapshot. Best effort.

        A cache we cannot write is a performance problem, not a failure: the
        archive is reproducible from the snapshot, so the next request simply
        rebuilds it.
        """
        cache = SkillCatalogService.archive_path(package_uuid, revision_number)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            tmp = cache.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(cache)
        except OSError as exc:
            logger.warning("skill_archive_cache_write_failed path=%s: %s", cache, exc)

    @staticmethod
    def _build_archive_bytes(skill_dir: Path, skill_name: str) -> bytes:
        raw = io.BytesIO()
        files = sorted(
            (
                p
                for p in skill_dir.rglob("*")
                if p.is_file() and not p.is_symlink()
            ),
            key=lambda p: p.relative_to(skill_dir).as_posix(),
        )
        with tarfile.open(fileobj=raw, mode="w") as tar:
            for path in files:
                rel = path.relative_to(skill_dir).as_posix()
                stat_result = path.stat()
                info = tarfile.TarInfo(name=f"skills/{skill_name}/{rel}")
                info.size = stat_result.st_size
                info.mtime = 0
                # Two modes only, chosen by whether the source file was
                # executable. Preserving the bit matters: a skill's
                # ``scripts/run.sh`` is meant to be run, and a catalog install
                # that silently drops the bit would behave differently from the
                # same skill sitting in a local ``skills/`` folder. Collapsing
                # to two values (rather than copying st_mode) is what keeps the
                # tarball deterministic and drops setuid/setgid/sticky, which is
                # the actual privilege concern.
                info.mode = 0o755 if stat_result.st_mode & 0o111 else 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.type = tarfile.REGTYPE
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
        compressed = io.BytesIO()
        with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as gz:
            gz.write(raw.getvalue())
        return compressed.getvalue()

    @staticmethod
    def archive_sha256(revision: SkillPackageRevision) -> str | None:
        """The stored digest of this revision's archive, or None.

        A pure column read — no filesystem, no gzip. That matters because the
        only caller is the plugin manifest builder, which runs on the asyncio
        event loop from environment activation and allowed-tools sync; doing
        I/O there would stall session streaming for every agent on the worker.

        None is unreachable on today's paths — every publish writes the digest
        or fails — so it is a guard, not a documented degradation: it exists so
        a future backfill, restore or hand-inserted row degrades to "the
        container reports ``catalog_revision_missing``" (§9) instead of raising
        somewhere in the middle of a manifest build. Reaching it means data is
        wrong, which is why it logs at ERROR.
        """
        if revision.archive_sha256:
            return revision.archive_sha256
        logger.error(
            "skill_archive_digest_missing revision=%s package=%s number=%s — "
            "the manifest will report this skill as unavailable to containers",
            revision.id, revision.package_id, revision.revision_number,
        )
        return None

    @staticmethod
    def env_may_download(
        session: Session, *, agent_id: uuid.UUID, revision: SkillPackageRevision
    ) -> bool:
        """True when ``agent_id`` holds a catalog link for this revision.

        The archive route's whole authorisation: an environment may fetch
        exactly the revisions its own agent was told to install, and nothing
        about the package's visibility enters into it — a package can be made
        private after an install without breaking the agents that already have
        it.
        """
        link = session.exec(
            select(AgentPluginLink).where(
                AgentPluginLink.agent_id == agent_id,
                AgentPluginLink.skill_package_revision_id == revision.id,
            )
        ).first()
        return link is not None
