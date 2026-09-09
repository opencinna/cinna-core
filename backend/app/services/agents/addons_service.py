"""AddonsService — one deduplicated list of everything an agent carries.

The agent page used to answer "what can this agent do?" twice: the Plugins tab
listed ``AgentPluginLink`` rows, the Skills card listed the environment's skill
index, and a catalog install appeared in both. They are not two answers to two
questions — they are two halves of one, and the seam between them was visible
to the user.

This service joins the halves **on the server**, so the tab, the CLI and any
later consumer fold them the same way (§2 of the plan):

1. every plugin link is one row — ``kind="skill"`` for a catalog link (it wraps
   exactly one skill) and for a ``skills``-format marketplace entry (the same
   thing by another road), ``kind="plugin"`` for everything else;
2. every ``source="local"`` index entry is one row;
3. index entries that belong to a plugin are **not** rows: they hang off their
   plugin's row as ``skills[]``, which is what removes the double listing;
4. an index entry whose plugin has no link — a plugin uninstalled but not yet
   pruned from the workspace — hangs off a synthetic *orphan* row, because
   something the engine can still load must not be invisible here.

Two properties this service is built around:

**It never writes and never calls a container.** The skill half comes from the
cached index on the environment row (the same read path
``GET /agents/{id}/skills`` uses). Every mutation stays on the existing plugin
and skills routes, so there is exactly one place that installs a plugin.

**The skill half failing must not take the plugin half with it.** A sleeping
environment, an ``adapter_error`` on a pre-feature container: the projection
still returns every link row and reports the reason in ``skills_error``.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlmodel import Session, select

from app.models.agents.addons import (
    AddonCounts,
    AddonPublic,
    AgentAddonsPublic,
)
from app.models.agents.agent import Agent
from app.models.environments.environment import AgentEnvironment
from app.models.plugins.llm_plugin import (
    AgentPluginLinkWithUpdateInfo,
    LLMPluginMarketplace,
    LLMPluginMarketplacePlugin,
    PluginSource,
)
from app.models.skills.skill_package import (
    CATALOG_MARKETPLACE_NAME,
    SkillPackage,
)
from app.models.users.user import User
from app.services.agents.agent_service import AgentService
from app.services.agents.agent_skills_service import AgentSkillsService
from app.services.agents.agent_status_service import AgentStatusService
from app.services.agents.skill_manifest import SkillEntry
from app.services.plugins.llm_plugin_service import LLMPluginService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PluginFacts:
    """What a marketplace link's row reads off its live plugin + marketplace.

    One record per plugin id, from one joined query — a list endpoint must not
    pay N queries for a badge, a link and a status.
    """

    plugin_type: str | None
    author: str | None
    repository_url: str | None

#: The marketplace format whose entries *are* skills rather than plugins. A
#: ``skills``-format repository ships one ``SKILL.md`` folder per entry, so an
#: install from it is a skill by every account the user can check — its format
#: fact says "Skill", its directory holds a skill, and the same entry in the Add
#: addon dialog is offered as one. Only the word differed by transport, and §10
#: of the plan exists to stop that: one entry, one vocabulary.
SKILLS_FORMAT = "skills"

#: Row status vocabulary. The same three states the skills card already uses,
#: because a user who has learned the dot on one surface must not have to learn
#: a second meaning on another.
STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"

#: Attention first, then alphabetical — mirrors ``sortSkills`` in
#: ``frontend/src/utils/skills.ts``. Kind is a filter, never a sort key: a
#: broken plugin and a broken skill are equally urgent, and grouping by kind
#: would push one of them off the preview.
_STATUS_RANK = {STATUS_ERROR: 0, STATUS_WARNING: 1, STATUS_OK: 2}


class AddonsService:
    """Compose the addons projection for one agent."""

    @staticmethod
    async def refresh(
        session: Session, agent: Agent, user: User
    ) -> AgentAddonsPublic:
        """Re-read the index from the environment, then project.

        The tab refreshes both halves with one call, so the refresh posture
        (wake a suspended environment, ignore the rate limit, never raise on an
        unreachable one) lives here rather than being restated by every
        controller that wants a current list.
        """
        environment = AddonsService._environment(session, agent)
        if environment is not None:
            await AgentSkillsService.force_refresh(
                environment, agent=agent, db_session=session
            )
            session.refresh(environment)
        return AddonsService.build(session, agent, user, environment=environment)

    @staticmethod
    def build(
        session: Session,
        agent: Agent,
        user: User,
        *,
        environment: AgentEnvironment | None = None,
    ) -> AgentAddonsPublic:
        """Everything ``agent`` carries beyond its prompt, for ``user``.

        Pass ``environment`` when the caller already holds it (the refresh path
        does) so it is not resolved twice.
        """
        if environment is None:
            environment = AddonsService._environment(session, agent)
        links = LLMPluginService.get_agent_plugins(session, agent.id)
        entries = (
            AgentSkillsService.get_cached_entries(environment)
            if environment is not None
            else []
        )

        # One capability answer for the whole response: the developer role on
        # an agent that is not a consumer install. The client holds only half
        # of that (the role), which is exactly why it is replied rather than
        # inferred.
        can_build = AgentService.can_build(session, user, agent)

        plugin_facts, unfetchable = AddonsService._plugin_facts(session, links)
        package_publishers = AddonsService._package_publishers(session, links)

        rows: list[AddonPublic] = []
        #: plugin_ref → the row its skills belong to.
        by_ref: dict[str, AddonPublic] = {}

        for link in links:
            facts = plugin_facts.get(link.plugin_id) if link.plugin_id else None
            row = AddonsService._link_row(
                link,
                plugin_type=facts.plugin_type if facts else None,
                author=(
                    package_publishers.get(link.skill_package_id)
                    if link.source == PluginSource.catalog
                    else (facts.author if facts else None)
                ),
                repository_url=(
                    facts.repository_url
                    if facts
                    else AddonsService._snapshot_repository_url(link)
                ),
            )
            rows.append(row)
            ref = AddonsService._link_ref(link)
            if ref:
                # Two links can claim one directory: a dead install (its
                # marketplace entry gone, ``plugin_id`` NULL) and the live one
                # the owner made afterwards. The select is unordered, so "first
                # wins" would hand the directory's skills to whichever row the
                # database happened to return — the working install could
                # render empty while the error row above it listed its skills.
                # A row whose source can still deliver files always outranks
                # one that cannot; between two rows of the same kind the first
                # still wins, exactly as the container's own install would.
                incumbent = by_ref.get(ref)
                if incumbent is None or (
                    AddonsService._source_unavailable(incumbent, unfetchable)
                    and not AddonsService._source_unavailable(row, unfetchable)
                ):
                    by_ref[ref] = row

        local_entries = [e for e in entries if e.source == "local"]
        published = AddonsService._published_package_ids(
            session,
            agent,
            [e.name for e in local_entries],
            can_build=can_build,
        )
        for entry in local_entries:
            rows.append(
                AddonsService._local_row(
                    entry, can_build=can_build, published=published
                )
            )

        orphans: dict[str, AddonPublic] = {}
        for entry in entries:
            if entry.source == "local":
                continue
            skill = AgentSkillsService.entry_to_public(
                entry, can_publish=can_build
            )
            ref = entry.plugin_ref or ""
            owner = by_ref.get(ref) if ref else None
            if owner is None:
                # An entry with no ``plugin_ref`` cannot be attributed to a
                # plugin at all, so it gets a row of its own keyed by its skill
                # name. Grouping those under one empty key would hide every one
                # of them but the first inside a row named after that first —
                # the exact opposite of what the orphan row is for.
                orphan_key = ref or f":{entry.name}"
                owner = orphans.get(orphan_key)
                if owner is None:
                    owner = AddonsService._orphan_row(ref, entry)
                    orphans[orphan_key] = owner
                    rows.append(owner)
            owner.skills.append(skill)

        for row in rows:
            AddonsService._settle_status(
                row, can_build=can_build, unfetchable=unfetchable
            )

        rows.sort(key=lambda r: (_STATUS_RANK.get(r.status, 2), r.display_name.lower()))

        return AgentAddonsPublic(
            agent_id=agent.id,
            environment_id=environment.id if environment else None,
            addons=rows,
            counts=AddonCounts(
                plugins=sum(1 for r in rows if r.kind == "plugin"),
                skills=sum(1 for r in rows if r.kind == "skill"),
                local_skills=sum(
                    1 for r in rows if r.kind == "skill" and r.source == "local"
                ),
            ),
            # With no environment there is no index and no failure to report:
            # a brand-new agent has nothing to say here, and ``environment_id``
            # is already null, which is how a client tells "never had one" from
            # "had one, could not read it".
            skills_error=environment.skills_error if environment else None,
            fetched_at=environment.skills_fetched_at if environment else None,
            can_add=can_build,
        )

    @staticmethod
    def _environment(
        session: Session, agent: Agent
    ) -> AgentEnvironment | None:
        """The environment whose cached index this agent's skills come from."""
        return AgentStatusService.get_primary_environment(
            session, agent.id, agent.active_environment_id
        )

    # ── Rows ───────────────────────────────────────────────────────────

    @staticmethod
    def _link_ref(link: AgentPluginLinkWithUpdateInfo) -> str:
        """The ``<marketplace>/<plugin>`` the environment's index reports.

        This is a **directory** identity, not a display one. Marketplace links
        resolve it from their live rows; every other source carries it in its
        snapshot fields — and a catalog link's display name is the *package's*
        (which a publisher may edit at any time), so matching on that would
        silently stop folding the row's own skill into it.

        A marketplace link whose plugin row was deleted (``plugin_id`` is
        ``SET NULL``) has no live identity left and falls back to the snapshot
        fields, which the marketplace install path writes for exactly this
        case (existing rows were backfilled). So it still folds its skills into
        the one row the user can act on, rather than leaving a nameless install
        beside a synthetic orphan — one directory, two rows. That row is
        reported as ``error`` / ``source_unavailable`` by
        :meth:`_source_unavailable`: its files are about to be pruned and the
        only remaining verb is uninstall.

        A catalog link falls back to the synthetic ``cinna-skills`` marketplace
        when its snapshot names none, because that is exactly what the manifest
        builder does (``LLMPluginService._build_catalog_entry``) — so that is
        the directory the container reports back. Reading the snapshot alone
        here would ref to ``""`` while the index says
        ``cinna-skills/<name>``, and one directory would render as two rows: a
        real one with no skills, plus an orphan holding them.

        The one shape with no directory identity left is a marketplace or
        bundle link written before that snapshot existed and orphaned before the
        backfill ran — it refs to ``""`` and its skills land on an orphan row.
        """
        if link.source == PluginSource.marketplace:
            marketplace = link.marketplace_name or link.snapshot_marketplace_name
            plugin = link.plugin_name or link.snapshot_plugin_name
        elif link.source == PluginSource.catalog:
            marketplace = link.snapshot_marketplace_name or CATALOG_MARKETPLACE_NAME
            plugin = link.snapshot_plugin_name
        else:
            marketplace = link.snapshot_marketplace_name
            plugin = link.snapshot_plugin_name
        if not marketplace or not plugin:
            return ""
        return f"{marketplace}/{plugin}"

    @staticmethod
    def _link_row(
        link: AgentPluginLinkWithUpdateInfo,
        *,
        plugin_type: str | None,
        author: str | None = None,
        repository_url: str | None = None,
    ) -> AddonPublic:
        """One installed plugin link as an addon row."""
        is_catalog = link.source == PluginSource.catalog
        # The word follows the *format*, the routes follow the ``source``. A
        # ``skills``-format marketplace entry is a skill: it is what the format
        # badge on this very row says, what the Add addon dialog called it
        # before it was installed, and what its directory holds. Deriving the
        # noun from the transport instead gave one entry two vocabularies —
        # "plugin" in the aria-label, the actions menu and the uninstall
        # sentence, "Skill" in the fact right beside them. Nothing routes on
        # ``kind``: install, toggle, upgrade and uninstall all key on
        # ``source`` and on the link, so this moves a noun and a count, never
        # a verb.
        is_skill_format = plugin_type == SKILLS_FORMAT
        if link.source == PluginSource.marketplace:
            name = link.plugin_name or ""
        else:
            name = link.snapshot_plugin_name or link.plugin_name or ""

        return AddonPublic(
            key=f"plugin:{link.id}",
            # A catalog link *is* a skill to the user: one row, one SKILL.md,
            # a catalog page behind it. Calling it a plugin would expose the
            # transport rather than the thing. A ``skills``-format marketplace
            # entry is the same thing arriving by a different road.
            kind="skill" if is_catalog or is_skill_format else "plugin",
            source=link.source.value,
            name=name,
            display_name=link.plugin_name or name or "Unknown plugin",
            description=link.plugin_description or "",
            version=link.installed_version,
            marketplace_name=link.marketplace_name,
            plugin_type=plugin_type,
            author=author,
            repository_url=repository_url,
            link=link,
        )

    @staticmethod
    def _local_row(
        entry: SkillEntry,
        *,
        can_build: bool,
        published: dict[str, uuid.UUID],
    ) -> AddonPublic:
        """One of the agent's own ``skills/<name>/`` folders as an addon row."""
        skill = AgentSkillsService.entry_to_public(entry, can_publish=can_build)
        return AddonPublic(
            key=f"skill:local:{entry.name}",
            kind="skill",
            source="local",
            name=entry.name,
            display_name=entry.name,
            description=entry.description or "",
            skills=[skill],
            # Both halves are server-side: the capability, and whether this
            # particular skill is clean enough to publish.
            can_share=skill.can_publish,
            published_package_id=published.get(entry.name),
        )

    @staticmethod
    def _orphan_row(ref: str, entry: SkillEntry) -> AddonPublic:
        """A plugin directory the engine still loads but no link owns.

        Read-only and always flagged: the next environment sync prunes it, and
        until then the user deserves to see that something is loaded which the
        install list does not admit to.
        """
        marketplace, _, plugin = ref.partition("/")
        return AddonPublic(
            key=f"plugin:orphan:{ref or entry.name}",
            kind="plugin",
            source="marketplace",
            name=plugin or entry.name,
            display_name=plugin or entry.name,
            marketplace_name=marketplace or None,
            orphan=True,
        )

    # ── Derived fields ─────────────────────────────────────────────────

    @staticmethod
    def _settle_status(
        row: AddonPublic,
        *,
        can_build: bool,
        unfetchable: set[uuid.UUID] | None = None,
    ) -> None:
        """Derive the row's status and its management capability.

        Errors are resolved before warnings, and a link-level failure counts as
        an error of its own: a row whose upstream is gone — a catalog package
        or revision, a marketplace plugin — often ships no skills at all, so
        waiting for a skill to complain would leave the most broken row in the
        list looking healthy.
        """
        # A local skill has no link and therefore no toggles, no upgrade and
        # no uninstall — it is a folder in the workspace. Its verb is `Share`,
        # which is `can_share`. Saying `can_manage` here would offer a client
        # actions that have nothing to act on.
        row.can_manage = can_build and not row.orphan and row.link is not None

        if row.orphan:
            row.status = STATUS_ERROR
            row.status_code = "orphan"
            return

        if AddonsService._source_unavailable(row, unfetchable):
            row.status = STATUS_ERROR
            row.status_code = "source_unavailable"
            return

        for skill in row.skills:
            if skill.error is not None:
                row.status = STATUS_ERROR
                row.status_code = skill.error.code
                return

        for skill in row.skills:
            if skill.warning is not None:
                row.status = STATUS_WARNING
                row.status_code = skill.warning.code
                return

        row.status = STATUS_OK
        row.status_code = None

    @staticmethod
    def _source_unavailable(
        row: AddonPublic, unfetchable: set[uuid.UUID] | None = None
    ) -> bool:
        """A row whose upstream — package, revision or plugin — cannot deliver.

        Every one of those FKs is ``ON DELETE SET NULL`` precisely so this
        stays visible as a broken install instead of silently uninstalling
        someone's skill:

        * ``catalog`` — the package or the pinned revision was deleted;
        * ``marketplace`` — the marketplace was deleted, which cascades its
          plugin rows away and leaves the link with ``plugin_id IS NULL``. The
          row then has no version, no update signal and nothing to re-sync
          from, so reporting it as ``ok`` would leave the one row the user can
          act on looking like the healthy one.

        The plugin row surviving is not enough: a sync can re-derive
        ``supported=false`` onto an entry an agent already has (upstream moved
        it to npm, dropped its SKILL.md, wrote a path that escapes the repo).
        The manifest then stops shipping it and the environment prunes the
        directory, so the row is in exactly the state above — the same code
        rather than a new one, because it is the same fact to the user: this
        install's source cannot give it files any more, and uninstall is the
        only verb left.

        ``bundle`` links are snapshot-identified and have no upstream row to
        lose.
        """
        link = row.link
        if link is None:
            return False
        if link.source == PluginSource.catalog:
            return (
                link.skill_package_revision_id is None
                or link.skill_package_id is None
            )
        if link.source == PluginSource.marketplace:
            if link.plugin_id is None:
                return True
            return bool(unfetchable and link.plugin_id in unfetchable)
        return False

    # ── Grouped lookups ────────────────────────────────────────────────

    @staticmethod
    def _snapshot_repository_url(link: AgentPluginLinkWithUpdateInfo) -> str | None:
        """The marketplace repository the install was made from, browser-openable.

        The fallback for a marketplace link whose plugin row is gone: the link
        froze the repository at install time, and that is still where the
        source lived. Same ``http(s)``-only rule as ``plugin_repository_url``.
        """
        if link.source != PluginSource.marketplace:
            return None
        return LLMPluginService.plugin_repository_url(
            None, getattr(link, "snapshot_repository_url", None)
        )

    @staticmethod
    def _plugin_facts(
        session: Session, links: list[AgentPluginLinkWithUpdateInfo]
    ) -> tuple[dict[uuid.UUID, "_PluginFacts"], set[uuid.UUID]]:
        """``(plugin_id → the row's marketplace-derived facts, ids we can no longer fetch)``.

        Only marketplace links resolve a live plugin row; every other source
        has no format of its own, and a per-row lookup would make a list
        endpoint pay N queries for one badge and one status.

        The second half is the sync-time verdict read back: an entry marked
        ``supported=false`` is skipped by the plugin manifest, so its files go
        away on the next environment sync. That has to reach the row, or the
        install the user is about to lose reads ``ok``.

        The format is clamped through the same helper the manifest uses
        (``LLMPluginService._manifest_plugin_type``): ``plugin_type`` is a plain
        string copied from ``marketplace.type``, so a legacy row can hold a word
        from before this vocabulary existed. Reading it raw here would badge the
        row with a format the container does not have a branch for and silently
        treats as ``claude`` — two surfaces disagreeing about one install.
        """
        plugin_ids = [link.plugin_id for link in links if link.plugin_id]
        if not plugin_ids:
            return {}, set()
        rows = session.exec(
            select(LLMPluginMarketplacePlugin, LLMPluginMarketplace)
            .join(
                LLMPluginMarketplace,
                LLMPluginMarketplace.id == LLMPluginMarketplacePlugin.marketplace_id,
            )
            .where(LLMPluginMarketplacePlugin.id.in_(plugin_ids))
        ).all()
        facts: dict[uuid.UUID, _PluginFacts] = {}
        unfetchable: set[uuid.UUID] = set()
        for plugin, marketplace in rows:
            facts[plugin.id] = _PluginFacts(
                plugin_type=LLMPluginService._manifest_plugin_type(
                    plugin.plugin_type
                ),
                # Manifest author first (name, then email), the marketplace's
                # owner as the fallback — the same precedence the discover
                # payload gives the Add addon dialog, so the badge on an entry
                # does not change the moment it is installed.
                author=(
                    plugin.author_name
                    or plugin.author_email
                    or LLMPluginService.marketplace_owner_label(marketplace)
                ),
                repository_url=LLMPluginService.plugin_repository_url(
                    plugin, marketplace.url
                ),
            )
            if not plugin.supported:
                unfetchable.add(plugin.id)
        return facts, unfetchable

    @staticmethod
    def _package_publishers(
        session: Session, links: list[AgentPluginLinkWithUpdateInfo]
    ) -> dict[uuid.UUID, str]:
        """``package id → publisher label`` for the catalog links, one query.

        The label is the publisher's full name, else their email — the same
        precedence the catalog's own entries use (``skillPublisherLabel`` on
        the client), so the badge on the installed row matches the one on the
        catalog card. A package whose publisher account is gone yields no
        entry, and the row's ``author`` stays ``None``.
        """
        package_ids = [
            link.skill_package_id
            for link in links
            if link.source == PluginSource.catalog and link.skill_package_id
        ]
        if not package_ids:
            return {}
        rows = session.exec(
            select(SkillPackage.id, User.full_name, User.email)
            .join(User, User.id == SkillPackage.publisher_user_id)
            .where(SkillPackage.id.in_(package_ids))
        ).all()
        return {
            row[0]: (row[1] or row[2]) for row in rows if (row[1] or row[2])
        }

    @staticmethod
    def _published_package_ids(
        session: Session,
        agent: Agent,
        names: list[str],
        *,
        can_build: bool,
    ) -> dict[str, uuid.UUID]:
        """Local skill name → the catalog package this agent published it as.

        Drives "Update published skill" versus "Share". Matched on
        ``source_agent_id`` plus the skill name: a package remembers the agent
        it came from, and a re-publish of the same folder must append a
        revision to that package rather than offer to create a second one.

        Gated on ``can_build``, which is the same capability ``can_share``
        rides on: the match is keyed on the *agent*, not on the viewer, so a
        superuser reading somebody else's agent would otherwise be handed the
        id of the owner's package — one they cannot act on (``can_share`` is
        false) and would 404 on if they followed it, because a ``private``
        package has no admin bypass.
        """
        if not names or not can_build:
            return {}
        packages = session.exec(
            select(SkillPackage).where(
                SkillPackage.source_agent_id == agent.id,
                SkillPackage.name.in_(names),
            )
        ).all()
        return {package.name: package.id for package in packages}
