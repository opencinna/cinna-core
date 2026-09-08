"""Unit tests: the one marshaller that turns a revision row into its response.

``_revision_to_public`` lists every field by hand, which is what makes it the
place a newly added column is silently forgotten — the field exists on the row,
the migration ran, the manifest round-trips, and the wire still says ``null``.

These tests pin the fields whose ``None`` carries meaning, so dropping a line
from the marshaller fails here instead of shipping.
"""

from __future__ import annotations

import types
import uuid
from datetime import UTC, datetime

from app.api.routes.bundles import _revision_to_public


def _revision(**overrides):
    base = dict(
        id=uuid.uuid4(),
        bundle_id=uuid.uuid4(),
        revision_number=3,
        version="1.2",
        manifest={"schema_version": 2},
        content_hash="a" * 64,
        workflow_prompt="WORKFLOW",
        entrypoint_prompt="ENTRY",
        refiner_prompt="REFINER",
        router_trigger_prompt="TRIGGER",
        agent_sdk_building="opencode/anthropic",
        agent_sdk_conversation="claude_code/anthropic",
        model_override_building=None,
        model_override_conversation=None,
        required_credential_specs=[],
        schedules=[],
        plugin_specs=[],
        skills_summary=[
            {"name": "pdf-report", "description": "Build a PDF.", "has_scripts": True}
        ],
        published_by_user_id=None,
        published_at=datetime.now(UTC),
        release_notes=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_skills_summary_reaches_the_wire() -> None:
    public = _revision_to_public(_revision(), install_count=0)

    assert public.skills_summary == [
        {"name": "pdf-report", "description": "Build a PDF.", "has_scripts": True}
    ]


def test_a_revision_that_predates_skills_stays_null() -> None:
    # NOT ``[]``: a consumer must be able to tell "ships no skills" from "we
    # have no idea, this revision is older than the feature".
    public = _revision_to_public(_revision(skills_summary=None), install_count=0)

    assert public.skills_summary is None


def test_a_revision_published_with_no_skills_is_an_empty_list() -> None:
    public = _revision_to_public(_revision(skills_summary=[]), install_count=0)

    assert public.skills_summary == []
