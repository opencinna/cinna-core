"""Publishing an agent that ships ``skills/``.

Scenarios:
  1. Publish captures the ``skills/`` tree into the snapshot and derives
     ``skills_summary`` from the publisher's LIVE workspace — onto the revision,
     into the manifest, and out to the catalog card.
  2. Publish is hard-blocked by an invalid skill and by key material inside
     ``skills/``, both before anything is written, and both name what to fix.

Test seam:
  Publisher workspace files are seeded on disk under
  ``ENV_INSTANCES_DIR/<env_id>/app/workspace/`` — the same seam
  ``agents_bundles_workspace_snapshot_test.py`` uses. The snapshot root is
  derived from the storage layout (``BUNDLE_STORAGE_DIR/<bundle_id>/<n>``);
  ``patch_storage_dirs`` in the domain conftest points it at a tmp tree.

  The two publish-gate helpers are unit-tested directly against a tmp tree in
  ``tests/unit/test_publish_skills_gate.py``; this file covers what the API
  does with their outcome.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    make_bundle_public,
    publish_bundle_revision,
)
from tests.utils.environment import list_environments

API = settings.API_V1_STR


# ── FS helpers ─────────────────────────────────────────────────────────────


def _workspace_root(env_id: str) -> Path:
    return Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"


def _write_skill(
    env_id: str,
    name: str,
    *,
    frontmatter: str | None = None,
    extra: dict[str, str] | None = None,
) -> Path:
    """Write ``skills/<name>/SKILL.md`` (plus optional extra files) on disk."""
    skill_dir = _workspace_root(env_id) / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    front = frontmatter or f"name: {name}\ndescription: Does {name} things."
    (skill_dir / "SKILL.md").write_text(
        f"---\n{front}\n---\n\n# {name}\n\nStep one.\n", encoding="utf-8"
    )
    for rel, content in (extra or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill_dir


def _publisher_agent(
    client: TestClient, headers: dict[str, str], name: str
) -> tuple[str, str]:
    """Create an agent and return ``(agent_id, env_id)``."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    envs = list_environments(client, headers, agent["id"])
    assert envs["data"], "publisher must have an environment"
    return agent["id"], envs["data"][0]["id"]


def _snapshot_root(client, headers, agent_id: str, revision_number: int) -> Path:
    fresh = get_agent(client, headers, agent_id)
    return (
        Path(settings.BUNDLE_STORAGE_DIR)
        / fresh["bundle_id"]
        / str(revision_number)
    )


# ── Scenario 1: capture + derive ───────────────────────────────────────────


def test_publish_captures_skills_and_derives_the_summary(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Publishing an agent with skills:
      1. Seed two skills (one with ``scripts/``) plus the kit's
         ``skills/README.md``.
      2. Publish → the revision carries ``skills_summary``: name, description,
         has_scripts — and nothing else.
      3. The manifest carries the same derived key.
      4. The snapshot on disk carries the actual ``skills/`` tree.
      5. The catalog card reads the summary off the latest revision.
      6. A second publish with no skills at all reports ``[]`` — "ships no
         skills", which is not the same as the ``None`` an older revision has.
    """
    # ── Phase 1: seed the publisher workspace ────────────────────────────
    agent_id, env_id = _publisher_agent(
        client, superuser_token_headers, "Skills-Publisher"
    )
    _write_skill(env_id, "pdf-report", extra={"scripts/run.py": "print(1)\n"})
    _write_skill(env_id, "plain")
    # The Local Agent Kit ships a README at the skills root — not a skill.
    (_workspace_root(env_id) / "skills" / "README.md").write_text(
        "How skills work\n", encoding="utf-8"
    )

    # ── Phase 2: publish → derived summary on the revision ───────────────
    revision = publish_bundle_revision(
        client, superuser_token_headers, agent_id, notes="v1 with skills"
    )
    assert revision["skills_summary"] == [
        {
            "name": "pdf-report",
            "description": "Does pdf-report things.",
            "has_scripts": True,
        },
        {"name": "plain", "description": "Does plain things.", "has_scripts": False},
    ]

    # ── Phase 3: the manifest carries it too ─────────────────────────────
    assert revision["manifest"]["skills_summary"] == revision["skills_summary"]

    # ── Phase 4: the tree itself travelled ───────────────────────────────
    snapshot = _snapshot_root(
        client, superuser_token_headers, agent_id, revision["revision_number"]
    )
    skills_dir = snapshot / "workspace" / "skills"
    assert (skills_dir / "pdf-report" / "SKILL.md").exists()
    assert (skills_dir / "pdf-report" / "scripts" / "run.py").exists()
    assert (skills_dir / "plain" / "SKILL.md").exists()
    assert (skills_dir / "README.md").exists(), (
        "the whole folder travels — only the summary is filtered"
    )

    # ── Phase 5: the catalog card ────────────────────────────────────────
    fresh = get_agent(client, superuser_token_headers, agent_id)
    make_bundle_public(client, superuser_token_headers, fresh["bundle_uuid"])
    r = client.get(
        f"{API}/catalog/{fresh['bundle_id']}", headers=superuser_token_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["skills"] == revision["skills_summary"]

    # ── Phase 6: "no skills" is [] and says so ───────────────────────────
    empty_agent_id, empty_env_id = _publisher_agent(
        client, superuser_token_headers, "No-Skills-Publisher"
    )
    assert not (_workspace_root(empty_env_id) / "skills").exists()
    empty_revision = publish_bundle_revision(
        client, superuser_token_headers, empty_agent_id, notes="nothing to see"
    )
    assert empty_revision["skills_summary"] == [], (
        "an empty list is a claim; None would mean 'we have no idea'"
    )


# ── Scenario 2: the publish hard blocks ────────────────────────────────────


def test_publish_is_blocked_by_an_invalid_skill_and_by_secrets(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A revision is immutable, so both checks run before anything is written:
      1. A skill whose frontmatter name does not match its folder → 400 naming
         the folder, and no revision.
      2. Fixed — but the skill now carries a ``scripts/.env`` → 400 naming the
         file path.
      3. A ``.env.example`` beside it is documentation, not key material, and
         does not block.
      4. With the ``.env`` gone the publish succeeds, and the summary describes
         exactly the skills that got through.
    """
    agent_id, env_id = _publisher_agent(
        client, superuser_token_headers, "Blocked-Publisher"
    )

    # ── Phase 1: the name does not match the folder ──────────────────────
    _write_skill(
        env_id, "on-disk", frontmatter="name: in-frontmatter\ndescription: x"
    )
    body = publish_bundle_revision(
        client, superuser_token_headers, agent_id, expected_status=400
    )
    detail = body["detail"]
    assert "on-disk" in detail, f"the publisher must be told which skill: {detail}"
    assert "folder name" in detail
    assert get_agent(client, superuser_token_headers, agent_id)[
        "installed_revision_id"
    ] is None, "a blocked publish must not leave a revision behind"

    # ── Phase 2: valid now, but it carries a .env ────────────────────────
    _write_skill(
        env_id,
        "on-disk",
        extra={"scripts/.env": "TOKEN=abc\n", ".env.example": "TOKEN=\n"},
    )
    body = publish_bundle_revision(
        client, superuser_token_headers, agent_id, expected_status=400
    )
    detail = body["detail"]
    assert "skills/on-disk/scripts/.env" in detail, (
        f"the refusal must name the file to delete: {detail}"
    )

    # ── Phase 3+4: remove the secret → publish goes through ──────────────
    (_workspace_root(env_id) / "skills" / "on-disk" / "scripts" / ".env").unlink()
    assert (
        _workspace_root(env_id) / "skills" / "on-disk" / ".env.example"
    ).exists(), "the example file stays — it must not have been the blocker"

    revision = publish_bundle_revision(
        client, superuser_token_headers, agent_id, notes="cleaned up"
    )
    assert [entry["name"] for entry in revision["skills_summary"]] == ["on-disk"]
    assert revision["revision_number"] == 1, (
        "the two refusals consumed no revision numbers"
    )
