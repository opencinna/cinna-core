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
import os
import re
import shutil
import tarfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
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
    SkillRevisionFilePublic,
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
from app.services.agents.agent_skills_service import AgentSkillsService
from app.services.agents.skill_manifest import (
    DEFAULT_MAX_TOTAL_BYTES,
    SKILL_NAME_RE,
    coerce_version,
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

#: Cap on a served file listing. A skill is a folder somebody hand-writes, so
#: five hundred entries is far past anything real; the cap exists so a
#: pathological snapshot cannot turn one catalog page into a megabyte of JSON.
#: Over it, the response says ``truncated`` rather than lying about the count.
MAX_LISTED_FILES = 500

#: The fixed segment that marks a reverse-DNS id as a *skill package* rather
#: than a bundle. ``io.opencinna.cinna.skill.pdf-report``.
PACKAGE_ID_SKILL_SEGMENT = "skill"

#: The version a skill that has never been versioned starts at.
FIRST_VERSION = "1.0.0"

#: Splits a version into "everything up to the last run of digits" + that run,
#: which is what :func:`_bump_version` increments. Deliberately not a semver
#: parser: the frontmatter ``version`` is free text by the Agent Skills
#: standard, and ``1.0.0`` / ``1.2`` / ``v3`` / ``2026-09-09`` all have an
#: obvious successor under this rule while a strict parser would reject three
#: of the four and force the publisher to think of a number anyway.
_VERSION_TAIL_RE = re.compile(r"^(?P<head>.*?)(?P<num>\d+)$")

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

            next_number, published_versions, latest_version = (
                SkillCatalogService._version_history(session, package.id)
            )

            resolved_version = SkillCatalogService.resolve_version(
                requested=version,
                frontmatter_version=entry.version,
                published_versions=published_versions,
                latest_version=latest_version,
            )

            # Written back BEFORE the snapshot is taken, so the published
            # bytes carry the version in their own header — the snapshot is
            # immutable and there is no second chance to put it there. A
            # failure here is logged and not fatal: the revision row still
            # carries the version, and a workspace that cannot be written to
            # is not a reason to refuse a publish that is otherwise valid.
            version_written = await asyncio.to_thread(
                SkillCatalogService._write_version_to_skill_md,
                skill_dir / "SKILL.md",
                resolved_version,
            )
            if not version_written:
                logger.warning(
                    "skill_version_writeback_failed agent_id=%s skill=%s "
                    "version=%s — publishing anyway; the revision carries the "
                    "version even though its SKILL.md does not",
                    agent.id, entry.name, resolved_version,
                )

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
                version=resolved_version,
                # The frontmatter as the snapshot now carries it, not as it was
                # parsed a moment ago: the write-back above added or changed
                # the version key, and a revision whose stored frontmatter
                # disagreed with its own snapshot would be a second answer to
                # "what version is this".
                #
                # Which is exactly why the merge is conditional. On the
                # degraded path the branch above tolerates — the workspace
                # could not be written — the snapshot carries no ``version:``
                # at all, so merging one in here would manufacture the
                # disagreement this line exists to prevent. The revision's own
                # ``version`` column still records what it was published as.
                frontmatter=(
                    {**(entry.frontmatter or {}), "version": resolved_version}
                    if version_written
                    else dict(entry.frontmatter or {})
                ),
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

        # The write-back changed a file the index is a cache of, so the agent's
        # own row would keep showing the previous version — or none — until
        # something else happened to touch ``skills/``. Forced, because this is
        # direct evidence rather than a guess.
        #
        # **Only while the environment is actually running.** Publishing from a
        # SUSPENDED environment is a supported, pinned behaviour (the files are
        # read off the host), but ``refresh_after_action`` does not wake a
        # container — that is ``force_refresh`` — so on a sleeping env
        # ``fetch_index`` fails and ``_persist_error`` *commits*
        # ``skills_error="env_not_running"``. The exception is swallowed; the
        # stamped error is not. A successful publish would then paint "the
        # environment is asleep" over the Addons card until a manual Refresh.
        # Nothing is lost by skipping: the host-side backfill reads the very
        # header this publish just wrote, on the next read either way.
        from app.models.environments.environment import AgentEnvironment
        from app.services.agents.agent_skills_service import SLEEPING_STATUSES

        env = (
            session.get(AgentEnvironment, agent.active_environment_id)
            if agent.active_environment_id
            else None
        )
        if env is not None and env.status not in SLEEPING_STATUSES:
            await AgentSkillsService.refresh_after_action(
                env, db_session=session, force=True
            )

        return revision

    @staticmethod
    def _version_history(
        session: Session, package_uuid: uuid.UUID
    ) -> tuple[int, set[str], str | None]:
        """``(next revision number, every version used, the newest version)``.

        One read for all three, because deriving them separately is how a
        package could publish revision 4 as a version revision 3 already used.
        Shared by the publish path and the preview the dialog prefills from, so
        the number shown and the number written can never be two answers.
        """
        history = session.exec(
            select(
                SkillPackageRevision.revision_number,
                SkillPackageRevision.version,
            )
            .where(SkillPackageRevision.package_id == package_uuid)
            .order_by(SkillPackageRevision.revision_number.desc())
        ).all()
        next_number = (history[0][0] if history else 0) + 1
        return (
            next_number,
            {v for _, v in history if v},
            next((v for _, v in history if v), None),
        )

    @staticmethod
    def publish_preview(
        session: Session,
        *,
        agent: Agent,
        user: User,
        skill_name: str,
    ) -> "SkillPublishPreview":
        """What :meth:`publish_from_agent` would do if called with no body.

        Exists because both of the values the Share dialog needs to *show* can
        only be computed here. The version comes from the skill's own
        ``SKILL.md`` header, which lives on the publisher's workspace and is
        not on the wire anywhere; the package id has to be checked against
        every package on the instance, including the ones this viewer cannot
        see — a client-side uniqueness check over the catalog it is allowed to
        list would happily derive an id that is already taken by somebody
        else's private package.

        Deliberately does **not** run the three content refusals
        (``skill_invalid`` / ``skill_contains_secrets`` / ``skill_too_large``):
        a preview that refused would leave the dialog with nothing to show for
        a skill whose row already carries that warning, and the publish itself
        is where a refusal belongs. What it does share with publish is the
        authorization gate and the workspace lookup, so a dialog that opens is
        a dialog whose Share button can work.
        """
        from app.models.skills.schemas import SkillPublishPreview

        try:
            AgentService.assert_can_build(session, user, agent)
        except CanBuildError as exc:
            raise SkillCatalogError(exc.reason, exc.message) from exc

        skill_dir = SkillCatalogService._resolve_skill_dir(session, agent, skill_name)
        entry = parse_skill_dir(skill_dir, rel_path=f"skills/{skill_name}")
        # The directory name, not ``entry.name``: an invalid skill has no
        # trustworthy frontmatter name, and the folder is what publish keys on.
        existing = SkillCatalogService._find_publisher_package(
            session, publisher_user_id=user.id, entry_name=skill_name
        )

        if existing is not None:
            package_id = existing.package_id
            disambiguated = False
            next_number, published_versions, latest_version = (
                SkillCatalogService._version_history(session, existing.id)
            )
        else:
            package_id, disambiguated = SkillCatalogService.derive_package_id(
                session, publisher_user_id=user.id, skill_name=skill_name
            )
            next_number, published_versions, latest_version = 1, set(), None

        return SkillPublishPreview(
            version=SkillCatalogService.resolve_version(
                requested=None,
                frontmatter_version=entry.version,
                published_versions=published_versions,
                latest_version=latest_version,
            ),
            header_version=entry.version,
            latest_published_version=latest_version,
            package_id=package_id,
            package_id_disambiguated=disambiguated,
            is_republish=existing is not None,
            next_revision_number=next_number,
        )

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
            SkillCatalogService.derive_package_id(
                session, publisher_user_id=user.id, skill_name=entry_name
            )[0]
        )
        if not BUNDLE_ID_REGEX.match(package_id):
            raise SkillCatalogError(
                "package_id_invalid",
                "A package id must be a reverse-DNS name, e.g. "
                "io.example.team.pdf-report.",
            )
        if SkillCatalogService._package_id_taken(session, package_id):
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
        try:
            session.commit()
        except IntegrityError as exc:
            # The check above is a read, and ``derive_package_id`` is
            # read-then-write: two publishers of a same-named skill can both
            # find the base id free and both insert. ``uq_skill_package_package_id``
            # is what actually decides, and without this the loser got a raw
            # 500 on an aborted session — while the tech doc promised them a
            # coded ``package_id_taken``. The doc was right about the intent;
            # only the handler was missing.
            #
            # The rollback is not optional: the session is aborted, and the
            # publish path goes on to write a revision row.
            session.rollback()
            raise SkillCatalogError(
                "package_id_taken",
                f"The package id '{package_id}' was claimed by another "
                "publish a moment ago. Try again — a fresh id will be "
                "derived.",
            ) from exc
        session.refresh(package)
        return package, True

    @staticmethod
    def base_package_id(skill_name: str) -> str:
        """``<reversed host>.skill.<skill name>``.

        Mirrors :meth:`BundleIdService.generate_bundle_id` — same reversed host
        prefix — so bundle ids and package ids read as one namespace rather
        than two conventions, with the fixed ``skill`` segment as the thing
        that says which family an id belongs to.

        Deliberately carries **no publisher slug**: the id a publisher pastes
        into a README should be readable, and the common case is the only
        person on the instance with a skill by that name. The slug is what
        :meth:`derive_package_id` falls back to when that turns out not to
        hold.
        """
        prefix = BundleIdService.reversed_host_prefix()
        return f"{prefix}.{PACKAGE_ID_SKILL_SEGMENT}.{skill_name}"

    @classmethod
    def derive_package_id(
        cls,
        session: Session,
        *,
        publisher_user_id: uuid.UUID,
        skill_name: str,
    ) -> tuple[str, bool]:
        """The free package id this skill would get, and whether it collided.

        ``package_id`` is unique **instance-wide** while a package's identity
        is per-publisher, so two people who each keep a ``pdf-report`` skill
        derive the same base id and the second one's publish would die on
        ``package_id_taken`` — a refusal whose only fix is "open Advanced and
        invent a reverse-DNS name", asked of someone who did nothing wrong.

        So the collision is resolved rather than reported: the publisher's own
        8-hex slug (the one :meth:`BundleIdService.generate_bundle_id` uses) is
        appended, then a counter if even that is taken. The second element of
        the return is what the Share dialog needs to *show* the disambiguated
        id rather than spring it on the publisher after the fact.

        Read-then-write, so two simultaneous first publishes of the same skill
        name can still both pick the same free id; the unique constraint is
        what actually decides, and the loser sees ``package_id_taken``. The
        publish lock is keyed per ``(publisher, skill)`` and cannot close a
        race between two *different* publishers.
        """
        base = cls.base_package_id(skill_name)
        if not cls._package_id_taken(session, base):
            return base, False

        owned = f"{base}.{publisher_user_id.hex[:8]}"
        if not cls._package_id_taken(session, owned):
            return owned, True

        for suffix in range(2, 100):
            candidate = f"{owned}-{suffix}"
            if not cls._package_id_taken(session, candidate):
                return candidate, True

        # 98 packages by one publisher under one skill name is not a state
        # anybody reaches; a random tail beats raising at this point.
        return f"{owned}-{uuid.uuid4().hex[:8]}", True

    @staticmethod
    def _package_id_taken(session: Session, package_id: str) -> bool:
        return (
            session.exec(
                select(SkillPackage).where(SkillPackage.package_id == package_id)
            ).first()
            is not None
        )

    # ── Versioning ─────────────────────────────────────────────────────

    @staticmethod
    def _bump_version(version: str) -> str | None:
        """The obvious successor of ``version``, or ``None`` if it has none.

        Increments the **last run of digits** in the string, so ``1.0.0`` →
        ``1.0.1``, ``1.2`` → ``1.3``, ``v3`` → ``v4`` and ``2026-09-09`` →
        ``2026-09-10``. A version with no digits at all (``alpha``) has no
        successor and the caller starts a fresh series instead.
        """
        match = _VERSION_TAIL_RE.match(version.strip())
        if match is None:
            return None
        return f"{match['head']}{int(match['num']) + 1}"

    @classmethod
    def resolve_version(
        cls,
        *,
        requested: str | None,
        frontmatter_version: str | None,
        published_versions: set[str],
        latest_version: str | None,
    ) -> str:
        """What this publish should be called.

        The rule is one sentence: **the header's version, unless it has already
        been published — then the next one after the newest release.**

        That is what makes "press Share and get a sensible number" and "hand-edit
        ``SKILL.md`` to 2.0.0 and have it honoured" the same rule rather than two
        competing ones. An explicitly requested version always wins and is never
        de-duplicated: the publisher typed it, and two revisions may legitimately
        carry one version (a re-publish of the same release).

        Args:
            requested: What the caller asked for, if anything.
            frontmatter_version: ``version:`` as it stands in ``SKILL.md`` now.
            published_versions: Every version this package has already used.
            latest_version: The newest revision's version, if it has one.
        """
        # Through the same normaliser the header goes through, never raw: this
        # value is written into a frontmatter block, and ``coerce_version`` is
        # where the one-line rule that makes that safe lives. A request whose
        # version is nothing but control characters falls through to the
        # derivation rather than publishing a blank.
        explicit = coerce_version(requested)
        if explicit:
            return explicit

        header = (frontmatter_version or "").strip()
        if header and header not in published_versions:
            return header

        # The series to continue: the newest release, else the header (a
        # package whose only releases were unversioned), else nothing.
        seed = (latest_version or "").strip() or header
        if seed:
            # ``.1`` rather than ``FIRST_VERSION`` when a release exists but has
            # no successor: a package whose latest revision is ``2.0.0-beta``
            # would otherwise publish its *next* revision as ``1.0.0`` — lower
            # than its predecessor — and stamp that into the author's header.
            # Only a package with no releases at all starts a fresh series.
            candidate = cls._bump_version(seed) or f"{seed}.1"
        else:
            candidate = FIRST_VERSION

        # A bump can still land on a taken version — versions are not unique,
        # so a publisher who hand-typed 1.0.5 before 1.0.4 was released leaves
        # a hole the series walks into.
        seen = 0
        while candidate in published_versions and seen < 100:
            candidate = cls._bump_version(candidate) or f"{candidate}.1"
            seen += 1
        return candidate

    @staticmethod
    def _write_version_to_skill_md(skill_md: Path, version: str) -> bool:
        """Set ``version:`` in the frontmatter of ``skill_md``. Runs OFF the loop.

        The version a skill was published as belongs **in the skill**, not only
        in a database row: a reader of the folder, the snapshot inside the
        published revision and the agent's own addon row should all say the
        same number, and the next publish reads this line back to work out the
        one after it.

        A **surgical line edit**, never a re-serialisation. ``parse_frontmatter``
        is a restricted-subset reader with no writer behind it, so rebuilding
        the block from the parsed mapping would quietly rewrite the author's
        quoting, drop their comments and flatten any shape the reader kept "as
        text". One line is replaced or inserted and every other byte — the BOM,
        the line endings, the body — survives.

        Returns True when the file carries ``version`` afterwards — including
        when it already did. False means the write genuinely did not happen
        (an unreadable file, a missing fence, a read-only workspace), which is
        the only case a caller should say anything about.
        """
        try:
            raw = skill_md.read_bytes()
        except OSError:
            return False
        had_bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return False

        if not text.startswith("---"):
            return False
        newline = "\r\n" if "\r\n" in text else "\n"
        trailing = newline if text.endswith(("\n", "\r")) else ""
        lines = text.splitlines()

        end_index: int | None = None
        for index in range(1, len(lines)):
            if lines[index].strip() in ("---", "..."):
                end_index = index
                break
        if end_index is None:
            return False

        line = f"version: {version}"
        # Only top-level keys: an indented ``version:`` belongs to a nested
        # mapping the author wrote and is none of our business.
        at: int | None = None
        after_name: int | None = None
        for index in range(1, end_index):
            candidate = lines[index]
            if candidate[:1].isspace():
                continue
            key = candidate.split(":", 1)[0].strip()
            if key == "version":
                at = index
                break
            if key == "name":
                after_name = index

        if at is not None:
            if lines[at] == line:
                return True
            lines[at] = line
        else:
            # Beside ``name``, which is the key it qualifies. Falling back to
            # the top of the block rather than the bottom keeps it above a
            # multi-line ``description``, where a reader looks for it.
            lines.insert((after_name + 1) if after_name is not None else 1, line)

        updated = newline.join(lines) + trailing
        payload = (b"\xef\xbb\xbf" if had_bom else b"") + updated.encode("utf-8")

        # Atomic: a half-written SKILL.md is an unparseable skill, and this
        # file belongs to the user, not to us. The temp name is dot-prefixed so
        # a concurrent scan of the folder ignores it.
        tmp = skill_md.with_name(f".{skill_md.name}.tmp")
        try:
            tmp.write_bytes(payload)
            os.replace(tmp, skill_md)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass  # the temp file is already the failure being handled
            return False
        return True

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

    @staticmethod
    def list_revision_files(
        revision: SkillPackageRevision,
    ) -> tuple[list[SkillRevisionFilePublic], int, int]:
        """What one revision ships: ``(files, total count, total bytes)``.

        ``files`` is capped at :data:`MAX_LISTED_FILES`; the count and the byte
        total always describe the whole snapshot, so a client can say "the list
        below is the first 500 of 900" instead of printing a wrong number.

        The catalog preview renders one file — ``SKILL.md`` — but a skill is a
        folder, and a reader deciding whether to install one cannot see the
        ``scripts/`` and ``references/`` that come with it. This is that list:
        paths and sizes, never contents, so it stays a cheap directory walk of
        an immutable tree and never a general file reader over the snapshot.

        Read off disk rather than stored on the row: the snapshot is immutable,
        so the walk cannot go stale, and a column would answer nothing for
        every revision published before it existed.

        ``total`` is the size of the whole snapshot, which is also
        ``revision.size_bytes`` — computed here rather than read from the row
        so a listing and its total describe the same walk.
        """
        skill_dir = SkillCatalogService.skill_dir_of(revision)
        if not skill_dir.is_dir():
            # Same code and same reasoning as ``read_revision_content``: an
            # immutable snapshot that is not on disk will not be there on a
            # retry either.
            logger.error(
                "skill_snapshot_missing revision=%s package=%s number=%s path=%s",
                revision.id, revision.package_id, revision.revision_number,
                skill_dir,
            )
            raise SkillCatalogError(
                "snapshot_missing",
                "The published files for this revision are no longer on disk.",
            )

        total_bytes = 0
        total_count = 0
        files: list[SkillRevisionFilePublic] = []
        for path in SkillCatalogService.snapshot_files(skill_dir):
            try:
                stat_result = path.stat()
            except OSError:
                # A file that vanished between the walk and the stat is not a
                # reason to lose the listing; it is simply not in it.
                continue
            total_bytes += stat_result.st_size
            total_count += 1
            if len(files) >= MAX_LISTED_FILES:
                # Past the cap the file still counts towards the totals — the
                # header describes the snapshot, not the page of it the client
                # received.
                continue
            files.append(
                SkillRevisionFilePublic(
                    path=path.relative_to(skill_dir).as_posix(),
                    size_bytes=stat_result.st_size,
                    is_executable=bool(stat_result.st_mode & 0o111),
                )
            )
        return files, total_count, total_bytes

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
            # Consumer installs per package, counted **one per person**. The
            # publisher-exclusion is applied in Python rather than in SQL
            # because it is per package (each row has its own publisher) and a
            # correlated condition would defeat the grouping this method exists
            # for.
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
            # Owners, not agents: the catalog number answers "how many other
            # people use this", and somebody who puts one skill into six of
            # their own agents is one adopter, not six. Counting agents let a
            # single enthusiastic consumer outvote a dozen real ones.
            counted: dict[uuid.UUID, set[uuid.UUID]] = {}
            for pkg_id, agent_id, owner_id in rows:
                publisher_id = publisher_by_package.get(pkg_id)
                if publisher_id is None or owner_id != publisher_id:
                    counted.setdefault(pkg_id, set()).add(owner_id)
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
    def snapshot_files(skill_dir: Path) -> list[Path]:
        """Every regular file of a snapshot, in archive order.

        The one definition of "what this revision ships", shared by the
        listing route and :meth:`_build_archive_bytes`. Two walks with the same
        predicate written twice is how a card ends up saying "4 files" over a
        tarball holding five — the symlink rule in particular is a security
        decision (a snapshot must never hand out a link pointing off the tree),
        and a second copy of it is a second place to get it wrong.
        """
        return sorted(
            (
                path
                for path in skill_dir.rglob("*")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: path.relative_to(skill_dir).as_posix(),
        )

    @staticmethod
    def _build_archive_bytes(skill_dir: Path, skill_name: str) -> bytes:
        raw = io.BytesIO()
        files = SkillCatalogService.snapshot_files(skill_dir)
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
