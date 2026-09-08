"""Response models for the agent skills endpoints.

The wire shape of an agent's skill index. It mirrors
``app.services.agents.skill_manifest.SkillEntry`` — the parser is the authority
on what a skill is — plus the two things only the server can answer: whether
*this* user may publish *this* skill, and the state of the environment the
index was read from.
"""
import uuid
from datetime import datetime

from sqlmodel import SQLModel


class SkillIssuePublic(SQLModel):
    """A flagged condition on a skill: a stable code plus a human sentence.

    Clients pick their status tone from ``code`` and never match on ``message``
    — the codes are the contract, the sentence is the copy. ``paths`` is
    populated only for ``code="secrets"`` and lists the offending files
    relative to the skill folder, so the card can name them before a publish is
    attempted rather than after it is refused.

    Error codes: ``not_a_directory``, ``missing_skill_md``, ``unreadable``,
    ``invalid_frontmatter``, ``missing_name``, ``invalid_name``,
    ``name_mismatch``, ``reserved_name``, ``missing_description``,
    ``description_too_long``, ``budget``, ``projection_error``.
    Warning codes: ``secrets``, ``shadowed``, ``oversized``.
    """

    code: str
    message: str = ""
    paths: list[str] = []


class SkillEntryPublic(SQLModel):
    """One skill in the agent's index."""

    name: str
    description: str = ""
    #: ``local`` (the agent's own ``skills/``), ``plugin`` (came with an
    #: installed plugin) or ``catalog`` (Phase 3).
    source: str = "local"
    plugin_ref: str | None = None  # "<marketplace>/<plugin>" for plugin skills
    path: str = ""                 # workspace-relative folder, e.g. "skills/pdf-report"
    has_scripts: bool = False
    user_invocable: bool = True    # may a person invoke it with /<name>
    model_invocable: bool = True   # may the model reach for it unprompted
    size_bytes: int = 0            # whole folder, not just SKILL.md
    error: SkillIssuePublic | None = None
    warning: SkillIssuePublic | None = None
    #: Structured secret-scan result: paths inside the skill that look like key
    #: material. Empty means the scan ran and found nothing.
    secret_paths: list[str] = []
    #: Whether **this** entry could be published to the skills catalog by the
    #: caller right now: the caller holds the capability (see
    #: ``AgentSkillsPublic.can_publish``) **and** the skill is clean — no
    #: ``error``, no ``secret_paths``, and locally owned. A client must branch
    #: on this rather than on the viewer's role: the role is only half the
    #: answer, and the server owns both halves.
    can_publish: bool = False


class AgentSkillsPublic(SQLModel):
    """The agent's cached skill index, as read from its environment."""

    agent_id: uuid.UUID
    environment_id: uuid.UUID | None = None
    skills: list[SkillEntryPublic] = []
    #: Workspace ``skills/`` tree hash the cache was built from. Empty when the
    #: index has never been read.
    hash: str | None = None
    fetched_at: datetime | None = None
    #: Why the last read failed, if it did: ``env_not_running`` (asleep — a
    #: refresh wakes it), ``adapter_error`` (unreachable, or a container built
    #: before agent skills existed — needs a rebuild), ``parse_error``.
    #: Cached rows are still returned alongside it.
    error: str | None = None
    #: Whether the caller may publish skills from this agent at all — the
    #: agent-developer role on an agent that is not a foreign install. The
    #: capability reply that replaces a role check in the client: a client that
    #: asked ``useRole()`` instead would show the verb on a consumer install,
    #: where every role is use-only.
    can_publish: bool = False


class SkillContentPublic(SQLModel):
    """The text of one skill's ``SKILL.md``, as the model sees it."""

    agent_id: uuid.UUID
    name: str
    path: str            # workspace-relative path to the SKILL.md itself
    content: str
    truncated: bool = False  # True when the body was cut at the size cap
