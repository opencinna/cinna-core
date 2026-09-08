"""Unit tests: what a bundle does to a consumer's ``skills/`` folder.

``skills/`` is an ordinary bundle-owned top-level directory, so the seed and
apply-update passes in ``workspace_copy`` carry it with no special case. That is
exactly why it is worth pinning: the D4 rule (the whole folder is replaced and
pruned, unlike ``plugins/``, which merges) is a consequence of that classification
rather than of any code that mentions skills, and a future "let's merge skills
too" change would break it silently.

The API-observable half — publish capture and the derived ``skills_summary`` —
is covered in ``tests/api/agents/bundles/agents_bundles_skills_test.py``; the
publish gate itself in ``tests/unit/test_publish_skills_gate.py``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch

from app.core.config import settings
from app.services.bundles.revision_format import RevisionFormat
from app.services.environments.workspace_classification import WORKSPACE_ROOT_REL


# ── helpers ────────────────────────────────────────────────────────────────


def _write_tree(root: Path, tree: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, value in tree.items():
        child = root / name
        if isinstance(value, dict):
            _write_tree(child, value)
        elif value is None:
            child.mkdir(parents=True, exist_ok=True)
        else:
            child.parent.mkdir(parents=True, exist_ok=True)
            child.write_text(str(value))


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: Does {name} things.\n---\n\nBody.\n"


def _v2_snapshot(root: Path, workspace_tree: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write_tree(root / "workspace", workspace_tree)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "bundle_id": "io.test.skills",
                "revision_number": 1,
                "prompts": {},
            }
        )
    )
    return root


def _v1_snapshot(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write_tree(root / "scripts", {"run.sh": "#!/bin/bash\nv1"})
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundle_id": "io.test.skills.v1",
                "revision_number": 1,
                "prompts": {},
            }
        )
    )
    return root


def _install_workspace(env_dir: Path, tree: dict) -> Path:
    ws = env_dir / WORKSPACE_ROOT_REL
    _write_tree(ws, tree)
    return ws


# ── Install: the seed brings skills/ across ────────────────────────────────


def test_install_seeds_the_skills_folder(tmp_path: Path) -> None:
    """A consumer install starts with the publisher's skills, scripts and all."""
    from app.services.environments.workspace_copy import (
        seed_workspace_from_bundle_snapshot as _seed,
    )

    snapshot = _v2_snapshot(
        tmp_path / "snap",
        {
            "skills": {
                "pdf-report": {
                    "SKILL.md": _skill_md("pdf-report"),
                    "scripts": {"run.py": "print(1)\n"},
                },
                "README.md": "How skills work\n",
            }
        },
    )
    env_id = uuid.UUID("aaaaaaaa-1111-2222-3333-444444444444")
    _install_workspace(tmp_path / str(env_id), {})

    with patch.object(settings, "ENV_INSTANCES_DIR", str(tmp_path)):
        _seed(snapshot, env_id)

    skills = tmp_path / str(env_id) / WORKSPACE_ROOT_REL / "skills"
    assert (skills / "pdf-report" / "SKILL.md").read_text() == _skill_md("pdf-report")
    assert (skills / "pdf-report" / "scripts" / "run.py").exists()
    assert (skills / "README.md").exists()


# ── Apply-update: replace and prune the WHOLE folder (D4) ──────────────────


def test_apply_update_replaces_and_prunes_the_whole_skills_folder(
    tmp_path: Path,
) -> None:
    """D4: ``skills/`` is bundle-owned, so an update replaces it outright.

    Three things happen in one pass, and all three are the rule:
      * a skill the new revision dropped is gone;
      * a skill the consumer added themselves is gone too — unlike
        ``plugins/``, this folder has no merge branch, which is what makes
        "the publisher's skills" a truthful statement about an install;
      * a skill the new revision changed is updated in place.

    ``plugins/cinna-skills/`` is asserted alongside as the contrast: skills that
    arrived as plugins are merged and survive.
    """
    from app.services.environments.workspace_copy import (
        replace_bundle_content as _replace,
    )

    snapshot = _v2_snapshot(
        tmp_path / "snap2",
        {
            "skills": {
                "pdf-report": {"SKILL.md": _skill_md("pdf-report") + "v2\n"},
                "brand-new": {"SKILL.md": _skill_md("brand-new")},
            },
            "plugins": {
                "bundle-mkt": {"p": {"plugin.json": '{"name":"p"}'}},
            },
        },
    )
    env_id = uuid.UUID("bbbbbbbb-1111-2222-3333-444444444444")
    ws = _install_workspace(
        tmp_path / str(env_id),
        {
            "skills": {
                "pdf-report": {"SKILL.md": _skill_md("pdf-report")},
                "dropped-by-publisher": {"SKILL.md": _skill_md("dropped-by-publisher")},
                "consumer-wrote-this": {"SKILL.md": _skill_md("consumer-wrote-this")},
            },
            "plugins": {
                "cinna-skills": {
                    "installed-skill": {"plugin.json": '{"name":"installed-skill"}'}
                },
            },
            "credentials": {"secret.json": "{}"},
        },
    )

    with patch.object(settings, "ENV_INSTANCES_DIR", str(tmp_path)):
        _replace(snapshot, env_id)

    skills = ws / "skills"
    assert sorted(p.name for p in skills.iterdir()) == ["brand-new", "pdf-report"]
    assert (skills / "pdf-report" / "SKILL.md").read_text().endswith("v2\n")
    assert not (skills / "dropped-by-publisher").exists()
    assert not (skills / "consumer-wrote-this").exists(), (
        "skills/ has no merge branch — a consumer edit here is not preserved"
    )

    # Contrast: a catalog skill installed as a plugin lives under plugins/ and
    # rides the plugins merge rule instead.
    assert (
        ws / "plugins" / "cinna-skills" / "installed-skill" / "plugin.json"
    ).exists()
    assert (ws / "credentials" / "secret.json").exists()


def test_a_pre_feature_revision_leaves_existing_skills_alone(
    tmp_path: Path,
) -> None:
    """Updating from a snapshot that predates skills must not delete them.

    A v1-flat snapshot has no ``workspace/`` tree at all, and its manifest has
    no ``skills_summary`` key — "we have no idea what this ships", not "it ships
    nothing". The no-delete behaviour of the v1 path is what turns that into
    the safe outcome: whatever skills the install has stay put.
    """
    from app.services.bundles.revision_format import RevisionFormat as _RF
    from app.services.environments.workspace_copy import (
        replace_bundle_content as _replace,
    )

    snapshot = _v1_snapshot(tmp_path / "snap_v1")
    env_id = uuid.UUID("cccccccc-1111-2222-3333-444444444444")
    ws = _install_workspace(
        tmp_path / str(env_id),
        {
            "skills": {"pdf-report": {"SKILL.md": _skill_md("pdf-report")}},
            "scripts": {"run.sh": "#!/bin/bash\nold"},
        },
    )

    with patch.object(settings, "ENV_INSTANCES_DIR", str(tmp_path)):
        _replace(snapshot, env_id)

    assert (ws / "skills" / "pdf-report" / "SKILL.md").exists(), (
        "a pre-feature revision must not prune a folder it never knew about"
    )
    assert (ws / "scripts" / "run.sh").read_text() == "#!/bin/bash\nv1"

    # The missing-key half of the same story, on the restore side.
    manifest = json.loads((snapshot / "manifest.json").read_text())
    assert "skills_summary" not in manifest
    assert _RF.manifest_to_revision_fields(manifest)["skills_summary"] is None


# ── Git trees: skills are committed, engine state is not ───────────────────


def test_the_generated_gitignore_hides_engine_state_but_not_skills() -> None:
    """``.claude/`` is per-env engine state; ``skills/`` is publisher content.

    The same denylist drives the snapshot copy and the generated ``.gitignore``,
    so getting this pair backwards would either commit the agent's engine state
    or drop its skills from every git-backed revision.
    """
    body = RevisionFormat.generate_gitignore()
    lines = set(body.splitlines())

    assert "workspace/.claude" in lines
    assert "workspace/skills" not in lines
    assert not any(
        line.startswith("workspace/skills") for line in lines
    ), f"skills/ must never be ignored: {sorted(lines)}"
