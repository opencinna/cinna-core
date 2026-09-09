"""Unit tests: the addons row status decision table (``_settle_status``).

The rule under test is the newest column of that table: a ``kind="skill"`` row
that contributes **no skill** did not land. A catalog skill row wraps exactly
one ``SKILL.md`` by definition, so an empty ``skills`` list is a contradiction
— the link was written correctly, the files never reached the container, and
before this rule the row read ``ok`` while the model could not load the skill.

What makes it more than an `if` is the tri-state ``index_readable``. An absence
is only evidence when somebody looked:

    True   the index was read → the skill really is not there → ``error`` /
           ``not_materialized``
    False  the read failed → we know the link and nothing about the files →
           ``warning`` / ``unverified``
    None   no environment, or never read → say nothing at all → ``ok``

And the boundary that keeps the rule honest: it applies to ``kind="skill"``
only. A **plugin** legitimately ships commands or agents and no skills at all,
in perfect health — so a plugin row with an empty list under a readable index
must stay ``ok``. Lose that and the change turns every healthy plugin in the
list red, which is a louder bug than the one it fixes.

Pure logic: ``_settle_status`` reads the row it is handed and writes two
fields on it. No DB, no environment, no session.

Deliberately NOT covered — a known gap, not an oversight. The rule carries two
further conditions, both of which say "this absence means nothing", and both of
which belong to a separate in-flight fix rather than to this file:

* a **disabled** link contributes no skills by design (env-core builds the
  index from active entries only), so a switched-off skill must not be read as
  a missing one. Every link built here is enabled;
* an **index older than the link** has not looked yet
  (``_index_saw_link``), so a fresh install is not judged against a cache
  filled before it existed. Every assertion here that expects
  ``not_materialized`` passes ``AFTER_THE_LINK`` to satisfy it.

Neither condition is asserted here — that coverage is owed by whoever owns the
fix, and duplicating it would put two files in charge of one decision.

Notes:
  The API-observable end of this decision — a catalog install whose files never
  materialised, and the same row while the index cannot be read — is covered by
  ``test_a_skill_row_that_ships_no_skill_is_not_healthy`` in
  ``tests/api/agents/core/agents_addons_projection_test.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.agents.addons import AddonPublic
from app.models.agents.agent_skills import SkillEntryPublic, SkillIssuePublic
from app.models.plugins.llm_plugin import (
    AgentPluginLinkWithUpdateInfo,
    PluginSource,
)
from app.services.agents.addons_service import AddonsService

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"


# ── Builders ───────────────────────────────────────────────────────────────


def _link(source: PluginSource = PluginSource.catalog, **overrides):
    now = datetime.now(UTC)
    fields = dict(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        plugin_id=None,
        source=source,
        installed_version="1.0",
        installed_commit_hash=None,
        conversation_mode=True,
        building_mode=True,
        disabled=False,
        created_at=now,
        updated_at=now,
    )
    if source == PluginSource.catalog:
        fields["skill_package_revision_id"] = uuid.uuid4()
        fields["skill_package_id"] = uuid.uuid4()
    if source == PluginSource.marketplace:
        fields["plugin_id"] = uuid.uuid4()
    fields.update(overrides)
    return AgentPluginLinkWithUpdateInfo(**fields)


def _skill(name: str = "pdf-report", **overrides) -> SkillEntryPublic:
    return SkillEntryPublic(name=name, **overrides)


def _row(
    *,
    kind: str = "skill",
    source: str = "catalog",
    name: str = "pdf-report",
    skills: list[SkillEntryPublic] | None = None,
    link=None,
    orphan: bool = False,
) -> AddonPublic:
    return AddonPublic(
        key=f"{kind}:{source}:{name}",
        kind=kind,
        source=source,
        name=name,
        display_name=name,
        skills=list(skills or []),
        link=link,
        orphan=orphan,
    )


#: An index read AFTER any link this file builds. The rule needs the ordering:
#: the addons projection serves the CACHED index, so "no skills on this row" is
#: only evidence of absence when the cache was filled after the link last
#: changed. A read that predates the install has not looked yet.
AFTER_THE_LINK = datetime.now(UTC) + timedelta(minutes=5)


def _settle(
    row: AddonPublic,
    *,
    index_readable=None,
    index_fetched_at: datetime | None = AFTER_THE_LINK,
    can_build: bool = True,
):
    AddonsService._settle_status(
        row,
        can_build=can_build,
        index_readable=index_readable,
        index_fetched_at=index_fetched_at,
    )
    return row


# ── The tri-state on a skill row that ships nothing ────────────────────────


class TestASkillRowWithNoSkills:

    def test_a_readable_index_makes_the_absence_an_error(self):
        """The index was read, after the install, and the skill is not in it.

        ``index_fetched_at`` is load-bearing, not decoration: the projection
        serves the cached index, so a read that predates the link proves
        nothing (see ``AFTER_THE_LINK``).
        """
        row = _settle(_row(link=_link()), index_readable=True)

        assert row.status == STATUS_ERROR
        assert row.status_code == "not_materialized"

    def test_an_unreadable_index_makes_the_absence_a_warning(self):
        """We know the link and nothing about the files: neither claim is safe."""
        row = _settle(_row(link=_link()), index_readable=False)

        assert row.status == STATUS_WARNING
        assert row.status_code == "unverified"

    def test_no_index_at_all_says_nothing(self):
        """``None`` is "nobody looked" — an agent with no environment, or one
        whose index has never been read. Accusing an install here would flag
        every fresh install before its first index read."""
        row = _settle(_row(link=_link()), index_readable=None)

        assert row.status == STATUS_OK
        assert row.status_code is None

    def test_the_default_is_silence(self):
        """Callers that pass no index (``_settle_status`` has other callers)
        must not start flagging rows."""
        row = _row(link=_link())
        AddonsService._settle_status(row, can_build=True)

        assert row.status == STATUS_OK
        assert row.status_code is None


# ── The boundary: plugins legitimately ship no skills ──────────────────────


class TestAPluginRowWithNoSkills:
    """The regression guard that matters most.

    Most installed plugins contribute nothing to the skill index — they ship
    commands, agents or MCP servers. If the emptiness rule leaked past
    ``kind="skill"``, a readable index would turn every one of those rows red
    and the tab would report a fleet-wide outage that is not happening.
    """

    @pytest.mark.parametrize("index_readable", [True, False, None])
    def test_it_stays_ok_whatever_the_index_says(self, index_readable):
        # Every other condition of the emptiness rule is deliberately SATISFIED
        # here — enabled link, index read after it — so ``kind`` is the only
        # thing keeping this row green.
        row = _settle(
            _row(kind="plugin", source="marketplace", name="reporting",
                 link=_link(PluginSource.marketplace)),
            index_readable=index_readable,
        )

        assert row.status == STATUS_OK, (
            "a plugin that ships no skills is healthy — commands and agents "
            "are not visible in the skill index"
        )
        assert row.status_code is None

    @pytest.mark.parametrize("index_readable", [True, False, None])
    def test_a_bundle_delivered_plugin_stays_ok_too(self, index_readable):
        row = _settle(
            _row(kind="plugin", source="bundle", name="ops-pack",
                 link=_link(PluginSource.bundle)),
            index_readable=index_readable,
        )

        assert row.status == STATUS_OK
        assert row.status_code is None


# ── A skill row that DID land is untouched by the rule ─────────────────────


class TestASkillRowThatShipsItsSkill:

    @pytest.mark.parametrize("index_readable", [True, False, None])
    def test_a_healthy_skill_is_ok(self, index_readable):
        row = _settle(
            _row(link=_link(), skills=[_skill()]), index_readable=index_readable
        )

        assert row.status == STATUS_OK
        assert row.status_code is None

    def test_a_skills_own_error_still_wins_and_keeps_its_code(self):
        """The emptiness rule sits BELOW the per-skill error scan, so a skill
        that landed broken reports what is wrong with it, not that it is
        missing."""
        row = _settle(
            _row(
                link=_link(),
                skills=[
                    _skill(error=SkillIssuePublic(code="name_mismatch",
                                                  message="Nope."))
                ],
            ),
            index_readable=True,
        )

        assert row.status == STATUS_ERROR
        assert row.status_code == "name_mismatch"

    def test_a_skills_warning_is_reported_as_a_warning(self):
        row = _settle(
            _row(
                link=_link(),
                skills=[
                    _skill(warning=SkillIssuePublic(code="shadowed",
                                                    message="Shadowed."))
                ],
            ),
            index_readable=True,
        )

        assert row.status == STATUS_WARNING
        assert row.status_code == "shadowed"

    def test_a_local_skill_row_is_never_reached_by_the_rule(self):
        """A ``source="local"`` row is built FROM an index entry, so it always
        carries one — but it also has no link, and asserting the healthy case
        keeps the rule from being widened to rows it cannot reason about."""
        row = _settle(
            _row(source="local", link=None, skills=[_skill("zeta-local")]),
            index_readable=True,
        )

        assert row.status == STATUS_OK
        assert row.can_manage is False, "a workspace folder has nothing to manage"


# ── Precedence above the new rule ──────────────────────────────────────────


class TestTheRulesAboveIt:
    """An empty skill list is the *weakest* evidence in the table.

    Both checks above it describe a row whose upstream is gone — a fact about
    the install itself — and each names a different remedy. If the emptiness
    rule ran first, a row with a deleted package would report
    ``not_materialized`` ("the files did not arrive") when the truth is that
    there is nothing left to arrive from.
    """

    def test_an_orphan_row_reports_orphan(self):
        row = _settle(
            _row(kind="plugin", source="marketplace", orphan=True),
            index_readable=True,
        )

        assert row.status == STATUS_ERROR
        assert row.status_code == "orphan"

    def test_a_catalog_row_whose_package_is_gone_reports_source_unavailable(self):
        row = _settle(
            _row(link=_link(skill_package_revision_id=None)), index_readable=True
        )

        assert row.status == STATUS_ERROR
        assert row.status_code == "source_unavailable", (
            "a deleted package is not an install that failed to materialise — "
            "the remedy is uninstall, not a re-sync"
        )

    def test_a_marketplace_row_whose_plugin_is_gone_reports_source_unavailable(self):
        row = _settle(
            _row(kind="plugin", source="marketplace",
                 link=_link(PluginSource.marketplace, plugin_id=None)),
            index_readable=True,
        )

        assert row.status == STATUS_ERROR
        assert row.status_code == "source_unavailable"


# ── The two conditions that make an absence mean nothing ───────────────────
#
# Both were missing from the first cut of the rule, and both turned a supported
# state into a permanent or transient false red. They live here, beside the
# rule they qualify, rather than in the fix's own file: the decision table has
# one home, and a reader asking "when does an empty skill row go red" must not
# have to find two.


class TestADisabledLinkIsNotAMissingOne:
    """A switched-off skill contributes nothing to the index BY DESIGN.

    env-core builds its index from ``active_plugins``, which skips every entry
    flagged ``disabled`` — so the row genuinely has no skills, and the absence
    carries no information at all. Without this the rule told anyone who used
    the disable toggle, permanently, that their files never arrived; and the
    client's own "off" tone was unreachable, because it is evaluated after the
    error branch and so never got the chance to answer.
    """

    def test_a_disabled_skill_row_is_not_reported_as_missing(self):
        row = _settle(_row(link=_link(disabled=True)), index_readable=True)

        assert row.status == STATUS_OK, (
            "disabling a skill is a supported verb, not a broken install"
        )
        assert row.status_code is None

    def test_a_disabled_row_is_not_downgraded_to_unverified_either(self):
        """An unreadable index changes nothing about a row we already excuse."""
        row = _settle(_row(link=_link(disabled=True)), index_readable=False)

        assert row.status == STATUS_OK
        assert row.status_code is None

    def test_an_enabled_row_is_still_reported(self):
        """The guard must key on ``disabled``, not on merely having a link."""
        row = _settle(_row(link=_link(disabled=False)), index_readable=True)

        assert row.status == STATUS_ERROR
        assert row.status_code == "not_materialized"

    def test_a_disabled_link_that_does_ship_a_skill_is_still_ok(self):
        row = _settle(
            _row(link=_link(disabled=True), skills=[_skill()]), index_readable=True
        )

        assert row.status == STATUS_OK


class TestAnIndexOlderThanTheLinkHasNotLookedYet:
    """The addons projection serves the CACHED index.

    Nothing on the install path refills that cache, and the client refetches
    immediately after a write — so a fresh install is otherwise judged against
    a read taken before it existed, and every successful install renders as a
    failed one until someone presses Refresh.

    Silence is deliberate here rather than ``unverified``: amber on every
    install would teach people that amber means nothing, which is the same
    defect as ``ok`` meaning nothing, one colour along.
    """

    def test_an_index_read_before_the_install_says_nothing(self):
        just_installed = _link(updated_at=AFTER_THE_LINK + timedelta(minutes=1))

        row = _settle(
            _row(link=just_installed),
            index_readable=True,
            index_fetched_at=AFTER_THE_LINK,
        )

        assert row.status == STATUS_OK, (
            "the cache was filled before this link existed, so its silence "
            "about the link is not evidence of anything"
        )
        assert row.status_code is None

    def test_an_index_read_after_the_install_is_evidence(self):
        row = _settle(
            _row(link=_link()),
            index_readable=True,
            index_fetched_at=AFTER_THE_LINK,
        )

        assert row.status == STATUS_ERROR
        assert row.status_code == "not_materialized"

    def test_an_index_with_no_timestamp_says_nothing(self):
        row = _settle(
            _row(link=_link()), index_readable=True, index_fetched_at=None
        )

        assert row.status == STATUS_OK

    def test_the_ordering_survives_a_naive_timestamp(self):
        """Postgres can hand back naive datetimes; comparing them would raise.

        Not a hypothetical: the column type decides this, so the rule has to
        survive either shape rather than depend on which one it is handed.
        """
        naive_link = _link(
            updated_at=(AFTER_THE_LINK - timedelta(minutes=10)).replace(tzinfo=None)
        )

        row = _settle(
            _row(link=naive_link),
            index_readable=True,
            index_fetched_at=AFTER_THE_LINK,
        )

        assert row.status == STATUS_ERROR
        assert row.status_code == "not_materialized"
