"""Skills catalog — the version and package id a publish derives for itself.

The publisher is not asked to invent either one. This file pins what they get
instead, end to end through the API:

Scenarios:
  1. A publish with no ``version`` in the body derives one, **writes it back
     into the workspace's ``SKILL.md``**, and the next publish continues the
     series from there. A version the author wrote by hand is honoured once and
     then continued.
  2. ``GET .../publish-preview`` answers with what the publish would do, and it
     matches what the publish then does — the guarantee the Share dialog's
     pre-filled fields rest on.
  3. The derived package id is ``<reversed host>.skill.<name>`` with no
     publisher slug, and a second publisher who has a skill of the same name
     gets a disambiguated id instead of a ``package_id_taken`` refusal.

Test seam:
  As ``agents_skills_catalog_test.py`` — the workspace is seeded on disk via
  ``write_skill`` and revision storage is redirected to a tmp tree.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.skill_catalog import (
    error_code,
    list_skill_catalog,
    make_agent_with_env,
    make_developer,
    patched_skill_storage,
    publish_skill,
    workspace_root,
    write_skill,
)

API = settings.API_V1_STR


@pytest.fixture(autouse=True)
def skill_storage(tmp_path: Path):
    with patched_skill_storage(tmp_path / "skill-storage") as root:
        yield root


def skill_md(env_id: str, name: str) -> str:
    return (workspace_root(env_id) / "skills" / name / "SKILL.md").read_text(
        encoding="utf-8"
    )


def publish_preview(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    name: str,
    *,
    expected_status: int = 200,
) -> dict:
    r = client.get(
        f"{API}/agents/{agent_id}/skills/{name}/publish-preview", headers=headers
    )
    assert r.status_code == expected_status, r.text
    return r.json()


# ── Scenario 1: the version series ─────────────────────────────────────────


def test_a_publish_derives_a_version_and_writes_it_into_the_skill(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
      1. A skill with no version publishes as 1.0.0, and SKILL.md now says so.
      2. The next publish continues from what it finds there → 1.0.1.
      3. An explicit version still wins, and is written back too.
      4. A hand-written version that has not been published is honoured.
    """
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, headers, "Version-Publisher")
    write_skill(env_id, "pdf-report", description="Turns a report into a PDF.")

    # ── Phase 1: nothing to go on → 1.0.0, and the header now carries it ──
    assert "version:" not in skill_md(env_id, "pdf-report")

    first = publish_skill(client, headers, agent_id, "pdf-report")
    assert first["version"] == "1.0.0"
    assert "version: 1.0.0" in skill_md(env_id, "pdf-report")
    # The stored frontmatter agrees with the snapshot it was taken from — one
    # answer to "what version is this", not two.
    assert first["frontmatter"]["version"] == "1.0.0"
    assert first["frontmatter"]["name"] == "pdf-report"

    # ── Phase 2: the series continues from the header ────────────────────
    second = publish_skill(client, headers, agent_id, "pdf-report")
    assert second["revision_number"] == 2
    assert second["version"] == "1.0.1"
    assert "version: 1.0.1" in skill_md(env_id, "pdf-report")

    # ── Phase 3: an explicit version wins and is written back ────────────
    third = publish_skill(client, headers, agent_id, "pdf-report", version="2.0.0")
    assert third["version"] == "2.0.0"
    assert "version: 2.0.0" in skill_md(env_id, "pdf-report")

    # ── Phase 4: and the series carries on from there ────────────────────
    fourth = publish_skill(client, headers, agent_id, "pdf-report")
    assert fourth["version"] == "2.0.1"

    entry = list_skill_catalog(client, headers)[0]
    assert entry["latest_version"] == "2.0.1"


def test_a_hand_written_version_is_published_as_written(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    # The author put 3.1.0 in the header; publishing it as 1.0.0 would
    # silently overrule them.
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, headers, "Header-Version")
    write_skill(
        env_id,
        "pdf-report",
        frontmatter="name: pdf-report\ndescription: Does things.\nversion: 3.1.0",
    )

    first = publish_skill(client, headers, agent_id, "pdf-report")
    assert first["version"] == "3.1.0"
    # It was already right, so nothing was rewritten.
    assert "version: 3.1.0" in skill_md(env_id, "pdf-report")

    # Published now, so the second publish continues rather than repeating.
    assert publish_skill(client, headers, agent_id, "pdf-report")["version"] == "3.1.1"


def test_the_write_back_leaves_the_rest_of_the_skill_alone(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    # The file belongs to the user: publishing edits one line of frontmatter
    # and touches nothing else.
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, headers, "Untouched")
    write_skill(
        env_id,
        "pdf-report",
        frontmatter=(
            "name: pdf-report\n"
            "description: Does things.\n"
            "allowed-tools: [Bash, Read]\n"
            "user-invocable: true"
        ),
        body="# Heading\n\nThe body the author wrote.",
    )

    publish_skill(client, headers, agent_id, "pdf-report")

    text = skill_md(env_id, "pdf-report")
    assert "allowed-tools: [Bash, Read]" in text
    assert "user-invocable: true" in text
    assert "The body the author wrote." in text
    assert "version: 1.0.0" in text


# ── Scenario 2: the preview matches the publish ────────────────────────────


def test_the_preview_answers_what_the_publish_then_does(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """The Share dialog pre-fills from this, so a mismatch is a lie on screen."""
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, headers, "Preview-Publisher")
    write_skill(env_id, "pdf-report")

    # ── Phase 1: before the first publish ────────────────────────────────
    before = publish_preview(client, headers, agent_id, "pdf-report")
    assert before["version"] == "1.0.0"
    assert before["header_version"] is None
    assert before["latest_published_version"] is None
    assert before["is_republish"] is False
    assert before["next_revision_number"] == 1
    assert before["package_id"].endswith(".skill.pdf-report")
    assert before["package_id_disambiguated"] is False

    revision = publish_skill(client, headers, agent_id, "pdf-report")
    assert revision["version"] == before["version"]
    entry = list_skill_catalog(client, headers)[0]
    assert entry["package_id"] == before["package_id"]

    # ── Phase 2: the preview now describes a re-publish ──────────────────
    after = publish_preview(client, headers, agent_id, "pdf-report")
    assert after["is_republish"] is True
    assert after["next_revision_number"] == 2
    assert after["version"] == "1.0.1"
    # The header the last publish wrote back, and the release it named.
    assert after["header_version"] == "1.0.0"
    assert after["latest_published_version"] == "1.0.0"
    # Immutable, so it is the id the package already has.
    assert after["package_id"] == before["package_id"]

    assert publish_skill(client, headers, agent_id, "pdf-report")["version"] == "1.0.1"


def test_the_preview_refuses_what_the_publish_would_refuse(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    # Same gate, same workspace lookup — so the dialog can show the reason
    # before the button is pressed rather than after.
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, _env_id = make_agent_with_env(client, headers, "Preview-Refusals")

    missing = publish_preview(
        client, headers, agent_id, "not-there", expected_status=404
    )
    assert error_code(missing) == "skill_not_found"


def test_the_preview_still_answers_for_a_skill_that_cannot_be_published(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    # A preview that refused over a content check would leave the dialog with
    # nothing to show for a skill whose row already carries the warning. The
    # refusal belongs to the publish.
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, headers, "Preview-Invalid")
    write_skill(
        env_id, "pdf-report", frontmatter="name: wrong-name\ndescription: d"
    )

    preview = publish_preview(client, headers, agent_id, "pdf-report")
    assert preview["version"] == "1.0.0"
    assert preview["package_id"].endswith(".skill.pdf-report")

    refused = publish_skill(
        client, headers, agent_id, "pdf-report", expected_status=422
    )
    assert error_code(refused) == "skill_invalid"


def test_a_stranger_cannot_preview_someone_elses_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    _user_a, headers_a = make_developer(client, superuser_token_headers)
    agent_a, env_a = make_agent_with_env(client, headers_a, "Preview-Owner")
    write_skill(env_a, "pdf-report")

    _user_b, headers_b = make_developer(client, superuser_token_headers)
    r = client.get(
        f"{API}/agents/{agent_a}/skills/pdf-report/publish-preview",
        headers=headers_b,
    )
    # Same answer as a nonexistent agent — `verify_agent_access` 404s a
    # non-owner router-wide rather than confirming the agent exists.
    assert r.status_code == 404, r.text


# ── Scenario 3: the derived package id and its collision walk ──────────────


def test_the_derived_id_has_no_publisher_slug_until_it_needs_one(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
      1. The first publisher of ``shared-name`` gets the plain id.
      2. A second publisher of the SAME skill name is not refused — the id is
         disambiguated with their own slug and the publish succeeds.
      3. Both packages exist, both keep the name, and the preview told the
         second publisher what was about to happen.
    """
    user_a, headers_a = make_developer(client, superuser_token_headers)
    agent_a, env_a = make_agent_with_env(client, headers_a, "Collide-A")
    write_skill(env_a, "shared-name")

    publish_skill(client, headers_a, agent_a, "shared-name")
    entry_a = list_skill_catalog(client, headers_a)[0]
    assert entry_a["package_id"].endswith(".skill.shared-name")
    assert user_a["id"][:8] not in entry_a["package_id"]

    # ── Phase 2: a second publisher of the same skill name ───────────────
    user_b, headers_b = make_developer(client, superuser_token_headers)
    agent_b, env_b = make_agent_with_env(client, headers_b, "Collide-B")
    write_skill(env_b, "shared-name")

    preview_b = publish_preview(client, headers_b, agent_b, "shared-name")
    assert preview_b["package_id_disambiguated"] is True
    assert preview_b["package_id"].endswith(
        f".skill.shared-name.{user_b['id'][:8]}"
    )

    publish_skill(client, headers_b, agent_b, "shared-name")
    entry_b = list_skill_catalog(client, headers_b)[0]

    # ── Phase 3: two packages, one name, two ids — and the preview was right
    assert entry_b["package_id"] == preview_b["package_id"]
    assert entry_b["package_id"] != entry_a["package_id"]
    assert entry_b["name"] == entry_a["name"] == "shared-name"


def test_an_explicit_package_id_still_overrides_the_derived_one(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    # The Advanced field is unchanged by the new derivation: what it sends is
    # used verbatim, and the derived id is only what happens when it is empty.
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, headers, "Explicit-Id")
    write_skill(env_id, "pdf-report")

    publish_skill(
        client,
        headers,
        agent_id,
        "pdf-report",
        package_id="io.example.team.pdf-report",
    )
    assert (
        list_skill_catalog(client, headers)[0]["package_id"]
        == "io.example.team.pdf-report"
    )
