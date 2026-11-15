"""Git pull provisions credential slots for specs new in the pulled revision.

``GitSourceService._apply_revision_to_install`` runs the same re-provisioning as
apply-update before touching the env: answered slots stay, a spec that is new in
the pulled revision gets its slot, nothing is unlinked. A git-shared bundle has no
publisher (``publisher_user_id`` is NULL), so a ``provided_by="publisher"`` spec
becomes a ``"<name> (placeholder)"`` and the pull reports ``last_update_status ==
"degraded"``; otherwise ``"synced"``.

Self-contained (no imports from sibling test files), same git-primitive patching
as ``agents_git_source_test.py``.
"""
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR

_CLONE_CTX = "app.services.bundles.git_source_service.clone_repository_context"
_GET_HASH = "app.services.bundles.git_source_service.get_current_commit_hash"
_LS_REMOTE = "app.services.bundles.git_source_service.ls_remote_head"

_SHA_V1 = "a" * 40
_SHA_V2 = "b" * 40
_SHA_V3 = "c" * 40


def _spec(name: str, provided_by: str = "user") -> dict:
    return {
        "name": name,
        "type": "email_imap",
        "allow_sharing": False,
        "allow_template_sharing": False,
        "description": None,
        "provided_by": provided_by,
        "publisher_credential_id": (
            str(uuid.uuid4()) if provided_by == "publisher" else None
        ),
        "service_uri": None,
    }


def _make_repo(base: Path, bundle_id: str, specs: list[dict]) -> Path:
    (base / "workspace" / "scripts").mkdir(parents=True, exist_ok=True)
    (base / "workspace" / "scripts" / "run.sh").write_text("#!/bin/bash\necho hi")
    manifest = {
        "schema_version": 2,
        "bundle_id": bundle_id,
        "revision_number": 1,
        "version": "1.0",
        "published_at": "2024-01-01T00:00:00+00:00",
        "prompts": {
            "workflow": "wf",
            "entrypoint": None,
            "refiner": None,
            "router_trigger": None,
        },
        "sdk": {
            "building": None,
            "conversation": None,
            "model_override_building": None,
            "model_override_conversation": None,
        },
        "required_credential_specs": specs,
        "schedules": [],
        "plugin_specs": [],
        "release_notes": "n",
    }
    (base / "cinna.agent.json").write_text(json.dumps(manifest))
    return base


def _fake_clone_ctx(repo_dir: Path, sha: str):
    @contextmanager
    def _ctx(*args, **kwargs):
        repo = MagicMock()
        repo.head.commit.hexsha = sha
        yield str(repo_dir), repo

    return _ctx


def _checkout(client: TestClient, headers: dict, repo: Path, sha: str) -> dict:
    with (
        patch(_CLONE_CTX, _fake_clone_ctx(repo, sha)),
        patch(_GET_HASH, return_value=sha),
        patch(_LS_REMOTE, return_value=sha),
    ):
        r = client.post(
            f"{API}/agents/checkout",
            headers=headers,
            json={
                "repo_url": "https://github.com/example/agent.git",
                "ref": "main",
                "sync_direction": "bidirectional",
            },
        )
    assert r.status_code == 200, r.text
    return r.json()


def _pull(client: TestClient, headers: dict, agent_id: str, repo: Path, sha: str):
    with (
        patch(_LS_REMOTE, return_value=sha),
        patch(_CLONE_CTX, _fake_clone_ctx(repo, sha)),
        patch(_GET_HASH, return_value=sha),
    ):
        return client.post(f"{API}/agents/{agent_id}/git/pull", headers=headers)


def _credential_names(client: TestClient, headers: dict, agent_id: str) -> list[str]:
    r = client.get(f"{API}/agents/{agent_id}/credentials", headers=headers)
    assert r.status_code == 200, r.text
    return sorted(c["name"] for c in r.json()["data"])


def test_git_pull_provisions_slot_for_new_credential_spec(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    1. Checkout a revision with one user spec → one placeholder slot
    2. Pull a revision adding a user spec → new slot appears, old one stays,
       status "synced", last_synced_commit advances
    3. Pull a revision adding a publisher-provided spec (git bundles have no
       publisher) → "<name> (placeholder)" slot, status "degraded"
    4. Pull a revision that drops specs → nothing is unlinked
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.pullcred.{random_lower_string()[:6]}"

    # ── Phase 1 ───────────────────────────────────────────────────────────
    v1 = _make_repo(tmp_path / "v1", bundle_id, [_spec("Mail A")])
    agent_id = _checkout(client, headers, v1, _SHA_V1)["agent"]["id"]
    assert _credential_names(client, headers, agent_id) == ["Mail A (placeholder)"]

    # ── Phase 2: new user spec ────────────────────────────────────────────
    v2 = _make_repo(tmp_path / "v2", bundle_id, [_spec("Mail A"), _spec("Mail B")])
    r = _pull(client, headers, agent_id, v2, _SHA_V2)
    assert r.status_code == 200, r.text
    assert r.json()["last_update_status"] == "synced"
    assert _credential_names(client, headers, agent_id) == [
        "Mail A (placeholder)",
        "Mail B (placeholder)",
    ]
    with patch(_LS_REMOTE, return_value=_SHA_V2):
        src = client.get(f"{API}/agents/{agent_id}/git", headers=headers).json()
    assert src["last_synced_commit"] == _SHA_V2
    assert src["status"] == "connected"

    # ── Phase 3: publisher spec on an ownerless bundle → degraded ─────────
    v3 = _make_repo(
        tmp_path / "v3",
        bundle_id,
        [_spec("Mail A"), _spec("Mail B"), _spec("Shared C", "publisher")],
    )
    r = _pull(client, headers, agent_id, v3, _SHA_V3)
    assert r.status_code == 200, r.text
    assert r.json()["last_update_status"] == "degraded"
    assert _credential_names(client, headers, agent_id) == [
        "Mail A (placeholder)",
        "Mail B (placeholder)",
        "Shared C (placeholder)",
    ]

    # ── Phase 4: dropping specs unlinks nothing ───────────────────────────
    v4 = _make_repo(tmp_path / "v4", bundle_id, [])
    sha4 = "d" * 40
    r = _pull(client, headers, agent_id, v4, sha4)
    assert r.status_code == 200, r.text
    assert len(_credential_names(client, headers, agent_id)) == 3
