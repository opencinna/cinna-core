"""
Improvement-request archive rendering — the parts a recipient reads first.

Pure renderers over a frozen ``context`` dict: no database, no HTTP. They are
unit-tested because the states that matter are hard to reach through the API
(a git-origin revision, an agent whose SDK config was never written) yet each
one produces a sentence a publisher acts on.

Paired with tests/api/agents/agents_improvement_requests_signals_test.py, which
covers the API-observable half of the same behaviour.
"""
import uuid
from datetime import UTC, datetime

from app.models.improvement.agent_improvement_request import (
    AgentImprovementRequest,
)
from app.services.improvement.improvement_archive_service import (
    ImprovementArchiveService,
)


def _request(context: dict, comment: str | None = "It looped.") -> AgentImprovementRequest:
    return AgentImprovementRequest(
        id=uuid.uuid4(),
        target_agent_id=uuid.uuid4(),
        requester_user_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        comment=comment,
        context=context,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _readme(agent_block: dict) -> str:
    return ImprovementArchiveService.render_readme(
        _request({"agent": agent_block}),
        {"display": "Dana", "email": "dana@example.com"},
        {"agent_name": "Rates Agent"},
    )


def test_install_above_latest_published_reads_as_a_track_difference() -> None:
    """
    Revision numbers are shared between published and git-origin revisions, and
    only a publish moves the bundle's pointer. "Installed 9 / latest published
    7" with nothing pending must therefore explain itself — unexplained, it
    reads as a regression, or as `update_pending: false` contradicting itself.
    """
    readme = _readme(
        {
            "is_bundle_install": True,
            "bundle_id": "localhost.test-agent",
            "installed_version": None,
            "installed_revision_number": 9,
            "installed_revision_origin": "git",
            "latest_published_version": "1.2",
            "latest_published_revision_number": 7,
            "head_revision_number": 9,
            "update_pending": False,
        }
    )
    # No bare em-dash standing in for a version that was never assigned.
    assert "revision 9 (unversioned) · from git" in readme
    assert "1.2 (revision 7)" in readme
    assert "above** the latest published revision (7)" in readme


def test_published_install_says_nothing_extra() -> None:
    """The note is for the confusing case only — it must not editorialise a
    plain, up-to-date install."""
    readme = _readme(
        {
            "is_bundle_install": True,
            "bundle_id": "localhost.test-agent",
            "installed_version": "1.2",
            "installed_revision_number": 7,
            "installed_revision_origin": "publish",
            "latest_published_version": "1.2",
            "latest_published_revision_number": 7,
            "head_revision_number": 7,
            "update_pending": False,
        }
    )
    assert "1.2 (revision 7)" in readme
    assert "above** the latest published revision" not in readme
    assert "from publish" not in readme


def test_older_capture_still_renders_its_revision_numbers() -> None:
    """A request captured before the rename carries ``latest_*``. Rendering it
    as two em-dashes would lose facts that were captured correctly."""
    readme = _readme(
        {
            "is_bundle_install": True,
            "bundle_id": "localhost.test-agent",
            "installed_version": "1.0",
            "installed_revision_number": 3,
            "latest_version": "1.1",
            "latest_revision_number": 4,
            "update_pending": True,
        }
    )
    assert "1.0 (revision 3)" in readme
    assert "1.1 (revision 4)" in readme


def test_tool_configuration_states_which_empty_it_is() -> None:
    """``null`` (no auto-approval list at all) and ``[]`` (one exists, empty)
    are the same empty list to a reader and different answers to "why was the
    tool not used"."""
    unset = ImprovementArchiveService.render_prompts_readme(
        {"baseline": "none", "workflow": {"text": "x", "chars": 1}, "sdk_tools": ["Read"]}
    )
    assert "no auto-approval list configured" in unset

    empty = ImprovementArchiveService.render_prompts_readme(
        {
            "baseline": "none",
            "workflow": {"text": "x", "chars": 1},
            "sdk_tools": ["Read"],
            "allowed_tools": [],
        }
    )
    assert "none — every tool use prompted the user" in empty

    configured = ImprovementArchiveService.render_prompts_readme(
        {
            "baseline": "none",
            "workflow": {"text": "x", "chars": 1},
            "sdk_tools": ["Read", "Write"],
            "allowed_tools": ["Read"],
        }
    )
    assert "`Read`" in configured


def test_routing_metadata_is_not_reported_as_an_unknown_comparison() -> None:
    """"unknown" (no baseline existed) and "not compared" (this field is not
    part of the published prompt surface) lead a publisher to different next
    steps."""
    readme = ImprovementArchiveService.render_prompts_readme(
        {
            "baseline": "installed_revision",
            "baseline_version": "1.0",
            "diverged": False,
            "diverged_fields": [],
            "workflow": {
                "text": "x",
                "chars": 1,
                "role": "published_prompt",
                "diverged_from_installed_revision": False,
            },
            "router_trigger": {
                "text": "Use for rate lookups.",
                "chars": 21,
                "role": "routing_metadata",
                "diverged_from_installed_revision": None,
                "divergence_reason": "platform_managed_no_baseline",
            },
            "sdk_tools": [],
            "allowed_tools": [],
        }
    )
    assert "not compared (routing metadata)" in readme
    assert "says *when to route to* this agent" in readme
