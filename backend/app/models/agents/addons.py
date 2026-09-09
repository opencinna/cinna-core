"""Wire shapes for the agent addons projection (no tables).

"Addon" is a presentation-layer umbrella over the two things an agent can carry
beyond its prompt: **plugins** (``AgentPluginLink`` rows — marketplace, bundle
or catalog) and **skills** (``skills/<name>/`` folders the engine loads). Both
already exist; what did not exist is one list that agrees about them.

The two sources overlap. A catalog install is *both* a plugin link and a skill
in the environment's index, and before this projection the agent page showed it
twice — once on the Plugins tab, once on the Skills card. The dedupe rule lives
here, on the server, so the tab, the CLI and any later consumer see the same
list rather than each folding the two halves their own way:

1. every plugin link is one row;
2. every ``source="local"`` index entry is one row;
3. index entries belonging to a plugin are **not** rows — they are attached to
   their plugin's row as ``skills[]``;
4. an index entry whose plugin has no link is attached to a synthetic *orphan*
   row, so nothing the engine can load is invisible here.

Nothing in this module is a table: the projection is derived on every read from
the plugin links plus the environment's cached skill index.
"""
import uuid
from datetime import datetime

from sqlmodel import SQLModel

from app.models.agents.agent_skills import SkillEntryPublic
from app.models.plugins.llm_plugin import AgentPluginLinkWithUpdateInfo

#: One skill inside an addon row. Deliberately the *same* shape the skills
#: index serves — a skill does not become a different thing because it is
#: rendered inside a plugin, and a second shape would be a second place to keep
#: the status vocabulary correct.
AddonSkillPublic = SkillEntryPublic


class AddonPublic(SQLModel):
    """One row of the addons list — a plugin, or a skill, never both."""

    #: Stable identity for a client list key. Rows come from three different
    #: origins and none of their natural ids are unique across all three:
    #: ``plugin:<link id>`` | ``skill:local:<name>`` |
    #: ``plugin:orphan:<marketplace>/<plugin>``.
    key: str
    #: What the row *is* to the user, derived from **what it holds** and never
    #: from how it was delivered. A catalog link is a ``skill`` (it wraps exactly
    #: one) and so is a ``skills``-format marketplace entry (same thing, other
    #: road); everything else is a ``plugin``. ``source`` is the delivery route
    #: and stays the field every install / toggle / upgrade / uninstall decision
    #: keys on — the two split the moment a marketplace could ship skills, and
    #: keeping them one field is what made a row say "Skill" in its format fact
    #: and "plugin" in the sentence beside it.
    kind: str  # "plugin" | "skill"
    #: Where it came from: ``marketplace`` | ``bundle`` | ``catalog`` | ``local``.
    source: str
    #: The engine-facing identity — the plugin's directory name, or the skill
    #: folder name. This is what ``plugin_ref`` and the on-disk layout use, so
    #: it is what a client must match on; ``display_name`` is for humans.
    name: str
    display_name: str
    description: str = ""
    version: str | None = None
    #: Marketplace name for marketplace rows, the bundle's snapshot
    #: marketplace for bundle rows, ``cinna-skills`` for catalog rows, ``None``
    #: for local skills.
    marketplace_name: str | None = None
    #: ``claude`` | ``codex`` | ``skills`` — the marketplace format this plugin
    #: was parsed as. Marketplace rows only; every other source has no format
    #: of its own.
    plugin_type: str | None = None
    #: Who made it, as one label: the marketplace manifest's author (name,
    #: else email), else the marketplace's owner (name, else email), for a
    #: marketplace row — the official marketplace leaves ``author`` blank on
    #: most entries and names itself once at the top; the package's publisher
    #: (name, else email) for a catalog row. ``None`` for a bundle row (the
    #: bundle's publisher *delivers* it, which is a different fact and already
    #: the source flag), for a local skill and for an orphan. The same word the
    #: Add addon dialog badges a result with, so an entry reads the same before
    #: and after it is installed.
    author: str | None = None
    #: Where the source lives, browser-openable — marketplace rows only: the
    #: manifest's ``homepage``, else a ``url``-sourced entry's repository,
    #: else the marketplace repository (from the link's frozen
    #: ``snapshot_repository_url`` when the marketplace row is gone). ``None``
    #: when nothing on record is an ``http(s)`` URL.
    repository_url: str | None = None
    #: The underlying link for every non-local row: per-mode toggles, update
    #: availability and ``skill_package_id`` all ride on it, so the row's
    #: mutations stay on the existing plugin routes rather than growing addon
    #: verbs that would have to be kept in sync.
    link: AgentPluginLinkWithUpdateInfo | None = None
    #: A local row carries itself; a catalog row its single skill; a plugin row
    #: every skill it ships. Empty when the index could not be read — see
    #: ``AgentAddonsPublic.skills_error``.
    skills: list[AddonSkillPublic] = []
    #: ``ok`` | ``warning`` | ``error``. The worst of the row's skills, or a
    #: link-level failure. ``has_update`` is deliberately not a status: it is a
    #: flag, and colouring it would make routine maintenance look like a fault.
    status: str = "ok"
    #: Why the status is not ``ok``: the offending skill's issue code, or
    #: ``source_unavailable`` (the catalog package or revision is gone) or
    #: ``orphan``.
    status_code: str | None = None
    #: A plugin directory the engine can still load whose link has been
    #: deleted. Read-only — the next environment sync prunes it.
    orphan: bool = False
    #: Local skills only: may the caller publish this one to the catalog right
    #: now. Both halves are server-side (the capability *and* the skill being
    #: clean), so a client never re-derives it from a role.
    can_share: bool = False
    #: May the caller change this row at all (toggles, upgrade, uninstall).
    #: False on a foreign install, for a non-developer, on orphan rows, and on
    #: local skills — a workspace folder has no link to toggle, and its verb is
    #: ``can_share``. Which of those verbs a given source offers is a separate
    #: question the client already answers from ``source``: a bundle row is
    #: manageable but has no uninstall, because its publisher owns what it
    #: delivers.
    can_manage: bool = False
    #: Local skills only: the catalog package this agent already published this
    #: skill as. Drives "Update published skill" versus "Share" — a distinction
    #: with business meaning, since a re-publish appends a revision to an
    #: immutable package.
    published_package_id: uuid.UUID | None = None


class AddonCounts(SQLModel):
    """Totals for the tab's "Show all (N)" affordances.

    Counted over **rows**, not over installs, and the two are deliberately not
    the same number:

    * ``plugins`` — every ``kind="plugin"`` row, *including orphans*. A
      ``skills``-format marketplace install is not one of them: it counts under
      ``skills``, with the noun its row prints. An orphan
      is a directory the engine still loads, so a count that omitted it would
      promise a shorter list than the one the user is about to open. It follows
      that ``plugins`` can exceed the number of installed plugin links, which
      is the point: the surplus is what needs attention.
    * ``skills`` — every ``kind="skill"`` row: catalog installs,
      ``skills``-format marketplace installs, *and* the agent's own
      ``skills/<name>/`` folders, because to the user they are one kind of thing
      with three origins.
    * ``local_skills`` — a **subset** of ``skills``, not a third bucket beside
      it: the folders the agent owns and can publish. ``skills`` minus
      ``local_skills`` is the catalog half.

    Skills that hang off a plugin row are counted nowhere: they are not rows,
    which is exactly the double listing this projection exists to remove.
    """

    plugins: int = 0
    skills: int = 0
    local_skills: int = 0


class AgentAddonsPublic(SQLModel):
    """Everything one agent can do beyond its prompt, in one list."""

    agent_id: uuid.UUID
    environment_id: uuid.UUID | None = None
    addons: list[AddonPublic] = []
    counts: AddonCounts = AddonCounts()
    #: Why the skill half is missing or stale (``env_not_running`` /
    #: ``adapter_error`` / ``adapter_unsupported`` / ``parse_error``). The
    #: plugin half is returned
    #: regardless: a tab that blanks because the environment is asleep is a
    #: worse answer than a tab that says so.
    skills_error: str | None = None
    fetched_at: datetime | None = None
    #: May the caller add an addon to this agent — the developer role on an
    #: agent that is not a consumer install. A capability reply: the client
    #: holds only half of that answer.
    can_add: bool = False
