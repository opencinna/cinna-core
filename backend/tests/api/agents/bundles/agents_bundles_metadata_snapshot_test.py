"""Tests for agent metadata snapshot completeness in bundle/git round-trips.

Covers:
  1. Bundle publish → install round-trip: all 8 metadata fields land on the
     consumer install (``description``, ``example_prompts``,
     ``status_refresh_command``, ``agent_api_enabled``,
     ``agent_api_identity_enabled``, ``a2a_config``, ``agent_sdk_config``,
     ``webapp_enabled``).
  2. Bundle apply-update: publisher edits all 8 fields and republishes;
     consumer applies update → consumer values overwritten with publisher's.
  3. Git checkout: a v2 manifest with a ``metadata`` block → installed agent
     has those field values, incl. ``a2a_config.skills`` deep structure.
  4. Back-compat / missing-key tolerance:
       (A) Git checkout from a manifest WITHOUT a ``metadata`` block (simulates
           a pre-feature git repo, i.e. NULL columns in old revision rows) →
           checkout succeeds and the agent has proper Agent-model defaults, not
           null garbage.
       (B) Bundle apply-update where ``revision.description is None`` (publisher
           never set description) does NOT null-out the consumer's existing
           custom description — the is-not-None guard is the regression target.
  5. Exclusion guard: per-install secrets (``agent_api_token``,
     ``agent_api_access_grant``) and UI prefs (``ui_color_preset``,
     ``conversation_mode_ui``, ``show_on_dashboard``) are absent from the
     serialized manifest ``metadata`` block.

Unit tests for ``RevisionFormat.build_manifest`` / ``manifest_to_revision_fields``
live in ``tests/unit/test_revision_format.py``.

Git network isolation strategy (for tests 3 and 4A): all git primitives are
patched at their import sites inside ``app.services.bundles.git_source_service``
— the same technique used by ``agents_git_source_test.py``.
"""
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle,
    make_bundle_public,
    make_user_and_headers as _make_user_and_headers,
)
from tests.utils.user import (
    create_random_user,
    promote_to_developer,
    user_authentication_headers,
)
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR

# ── Git patch targets (module-local import sites in git_source_service.py) ────
# Naming matches the convention established in agents_git_source_test.py.
_CLONE_CTX = "app.services.bundles.git_source_service.clone_repository_context"
_GET_HASH = "app.services.bundles.git_source_service.get_current_commit_hash"
_LS_REMOTE = "app.services.bundles.git_source_service.ls_remote_head"

_SHA_V1 = "a" * 40


# ── Helpers ───────────────────────────────────────────────────────────────────


def _publish_agent(
    client: TestClient,
    headers: dict,
    agent_id: str,
    *,
    notes: str | None = None,
) -> dict:
    """Publish an agent and return the ``AgentBundleRevisionPublic`` JSON.

    Drains background tasks so downstream state (pending_update flags, env
    provisioning) settles before the caller continues.
    """
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={"release_notes": notes} if notes else {},
    )
    assert r.status_code == 200, f"Publish failed: {r.text}"
    revision = r.json()
    drain_tasks()
    return revision


def _get_agent(client: TestClient, headers: dict, agent_id: str) -> dict:
    r = client.get(f"{API}/agents/{agent_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _set_agent_fields(
    client: TestClient,
    headers: dict,
    agent_id: str,
    fields: dict,
) -> dict:
    """Update agent fields via PUT /agents/{id}. Returns refreshed agent JSON.

    Requires the caller to be a developer or superuser.
    """
    r = client.put(f"{API}/agents/{agent_id}", headers=headers, json=fields)
    assert r.status_code == 200, f"Agent PUT failed: {r.text}"
    return r.json()


def _add_allowed_tools(
    client: TestClient,
    headers: dict,
    agent_id: str,
    tools: list[str],
) -> dict:
    """Merge ``tools`` into ``agent_sdk_config.allowed_tools``.

    Uses PATCH /agents/{id}/allowed-tools which adds (not replaces). Returns
    the updated ``AgentSdkConfig`` JSON (sdk_tools + allowed_tools lists).
    """
    r = client.patch(
        f"{API}/agents/{agent_id}/allowed-tools",
        headers=headers,
        json={"tools": tools},
    )
    assert r.status_code == 200, f"add_allowed_tools failed: {r.text}"
    return r.json()


def _fake_clone_ctx(repo_dir: Path, sha: str = _SHA_V1):
    """Return a context-manager factory that replaces ``clone_repository_context``.

    The returned callable accepts any arguments (url, branch, ssh_key_path,
    depth) and yields ``(str(repo_dir), mock_repo)``, so the service sees a
    real directory tree but never touches the network.  Matches the pattern
    used in ``agents_git_source_test.py``.
    """
    @contextmanager
    def _ctx(*args, **kwargs):
        mock_repo = MagicMock()
        mock_repo.head.commit.hexsha = sha
        yield str(repo_dir), mock_repo

    return _ctx


def _write_git_checkout_dir(
    base_dir: Path,
    bundle_id: str,
    *,
    with_metadata: bool = True,
    metadata_override: dict | None = None,
) -> Path:
    """Write a minimal v2 manifest + workspace tree to ``base_dir``.

    When ``with_metadata=True`` (default), the manifest includes a
    ``metadata`` block with all 8 new fields.  When ``False``, the
    ``metadata`` key is absent entirely — simulating a pre-feature git repo
    whose manifest predates the agent-metadata snapshot plan.

    ``metadata_override`` replaces the default metadata dict when provided.

    Returns ``base_dir`` (the clone root / effective source dir).
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "workspace" / "scripts").mkdir(parents=True, exist_ok=True)
    (base_dir / "workspace" / "scripts" / "run.sh").write_text("#!/bin/bash\necho hello")

    manifest: dict = {
        "schema_version": 2,
        "bundle_id": bundle_id,
        "revision_number": 1,
        "version": "1.0",
        "published_at": "2024-01-01T00:00:00+00:00",
        "prompts": {
            "workflow": "Test workflow prompt",
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
        "required_credential_specs": [],
        "schedules": [],
        "plugin_specs": [],
        "release_notes": "initial",
    }
    if with_metadata:
        manifest["metadata"] = metadata_override or {
            "description": "Git-backed agent description",
            "example_prompts": ["Help me with code", "Write a test"],
            "status_refresh_command": "/run:status-check",
            "agent_api_enabled": True,
            "agent_api_identity_enabled": False,
            "a2a_config": {
                "enabled": True,
                "skills": [{"name": "write-tests", "description": "Writes unit tests"}],
                "version": "2.0",
                "generated_at": "2024-06-01T00:00:00",
            },
            "agent_sdk_config": {"sdk_tools": [], "allowed_tools": ["mcp__git__clone"]},
            "webapp_enabled": False,
        }
    (base_dir / "cinna.agent.json").write_text(json.dumps(manifest))
    return base_dir


def _do_git_checkout(
    client: TestClient,
    headers: dict,
    *,
    clone_dir: Path,
    sha: str = _SHA_V1,
    repo_url: str = "https://github.com/example/agent.git",
) -> dict:
    """Execute a git checkout with all git primitives mocked out.

    Returns the ``{agent, git_source}`` response JSON (asserts 200).
    """
    with (
        patch(_CLONE_CTX, _fake_clone_ctx(clone_dir, sha)),
        patch(_GET_HASH, return_value=sha),
        patch(_LS_REMOTE, return_value=sha),
    ):
        r = client.post(
            f"{API}/agents/checkout",
            headers=headers,
            json={"repo_url": repo_url, "ref": "main"},
        )
    assert r.status_code == 200, f"Git checkout failed: {r.text}"
    return r.json()


def _make_developer_consumer(
    client: TestClient,
    superuser_headers: dict,
) -> tuple[dict, dict]:
    """Create a random user, promote them to developer, add a default AI credential.

    Returns ``(user_data, auth_headers)``.  The developer role is required by
    ``PUT /agents/{id}`` so the consumer can update their own installed agent
    (needed by the back-compat apply-update test).
    """
    consumer = create_random_user(client)
    consumer_headers = user_authentication_headers(
        client=client, email=consumer["email"], password=consumer["_password"]
    )
    create_random_ai_credential(client, consumer_headers, set_default=True)
    promote_to_developer(client, superuser_headers, consumer["id"])
    return consumer, consumer_headers


# ── Test 1: Bundle publish → install round-trip ───────────────────────────────


def test_bundle_metadata_publish_install_roundtrip(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """All 8 metadata fields survive a bundle publish → install round-trip.

    1. Publisher sets all 8 metadata fields on their agent (7 via PUT /agents/{id},
       ``agent_sdk_config.allowed_tools`` via PATCH /agents/{id}/allowed-tools).
    2. Publisher publishes; the returned revision's ``manifest.metadata`` block
       carries all 8 fields.
    3. Consumer installs the bundle from the catalog.
    4. All 8 fields appear on the consumer's installed agent.
    5. ``a2a_config.skills`` list survives identically (deep structure preserved).
    """
    # ── Phase 1: Publisher sets all 8 metadata fields ─────────────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="MetaRoundtrip-Publisher"
    )
    drain_tasks()
    pub_id = publisher_agent["id"]

    description = "Publisher's canonical agent description — v1"
    example_prompts = ["Fix a bug in my code", "Explain this concept", "Write unit tests"]
    status_refresh_command = "/run:healthcheck"
    a2a_config = {
        "enabled": True,
        "skills": [
            {"name": "code-review", "description": "Reviews code quality"},
            {"name": "testing", "description": "Writes automated tests"},
        ],
        "version": "1.0",
        "generated_at": "2024-01-01T00:00:00",
    }
    allowed_tools = ["mcp__platform__create_subtask", "mcp__platform__add_comment"]

    # 7 fields accessible via AgentUpdate (PUT).
    _set_agent_fields(
        client, superuser_token_headers, pub_id,
        {
            "description": description,
            "example_prompts": example_prompts,
            "status_refresh_command": status_refresh_command,
            "agent_api_enabled": True,
            "agent_api_identity_enabled": True,
            "webapp_enabled": True,
            "a2a_config": a2a_config,
        },
    )
    # 8th field: agent_sdk_config is not in AgentUpdate; set via allowed-tools.
    _add_allowed_tools(client, superuser_token_headers, pub_id, allowed_tools)

    # Confirm the fields are persisted on the publisher agent before publishing.
    pub_before = _get_agent(client, superuser_token_headers, pub_id)
    assert pub_before["description"] == description
    assert pub_before["example_prompts"] == example_prompts
    assert pub_before["a2a_config"]["skills"] == a2a_config["skills"]
    sdk = pub_before.get("agent_sdk_config") or {}
    assert set(sdk.get("allowed_tools", [])) == set(allowed_tools)

    # ── Phase 2: Publish + verify manifest metadata block ─────────────────────
    revision = _publish_agent(
        client, superuser_token_headers, pub_id, notes="metadata-roundtrip-v1"
    )

    manifest = revision.get("manifest", {})
    assert manifest.get("schema_version") == 2, "Revision must be schema_version 2"
    assert "metadata" in manifest, (
        f"Revision manifest must have a 'metadata' block; got keys: {list(manifest.keys())}"
    )
    meta = manifest["metadata"]
    assert meta["description"] == description
    assert meta["example_prompts"] == example_prompts
    assert meta["status_refresh_command"] == status_refresh_command
    assert meta["agent_api_enabled"] is True
    assert meta["agent_api_identity_enabled"] is True
    assert meta["webapp_enabled"] is True
    assert meta["a2a_config"]["enabled"] is True
    assert meta["a2a_config"]["skills"] == a2a_config["skills"]
    assert set(meta["agent_sdk_config"]["allowed_tools"]) == set(allowed_tools)

    # ── Phase 3: Make bundle public and consumer installs ─────────────────────
    pub_fresh = _get_agent(client, superuser_token_headers, pub_id)
    bundle_id_str = pub_fresh["bundle_id"]
    bundle_uuid = pub_fresh["bundle_uuid"]
    assert bundle_uuid, "bundle_uuid must be set after publish"
    make_bundle_public(client, superuser_token_headers, bundle_uuid)

    _, consumer_headers = _make_user_and_headers(client)
    consumer_install = install_bundle(client, consumer_headers, bundle_id_str)
    consumer_id = consumer_install["id"]

    # ── Phase 4: All 8 fields on the consumer's installed agent ───────────────
    consumer = _get_agent(client, consumer_headers, consumer_id)

    assert consumer["description"] == description, (
        f"Consumer description mismatch: {consumer['description']!r}"
    )
    assert consumer["example_prompts"] == example_prompts, (
        f"Consumer example_prompts mismatch: {consumer['example_prompts']!r}"
    )
    assert consumer["status_refresh_command"] == status_refresh_command, (
        f"Consumer status_refresh_command mismatch: {consumer['status_refresh_command']!r}"
    )
    assert consumer["agent_api_enabled"] is True, (
        "Consumer agent_api_enabled should be True after install"
    )
    assert consumer["agent_api_identity_enabled"] is True, (
        "Consumer agent_api_identity_enabled should be True after install"
    )
    assert consumer["webapp_enabled"] is True, (
        "Consumer webapp_enabled should be True after install"
    )

    # ── Phase 5: a2a_config.skills deep structure preserved ───────────────────
    consumer_a2a = consumer.get("a2a_config") or {}
    assert consumer_a2a.get("enabled") is True, "Consumer a2a_config.enabled should be True"
    assert consumer_a2a.get("skills") == a2a_config["skills"], (
        f"Consumer a2a_config.skills mismatch: {consumer_a2a.get('skills')!r}"
    )
    assert consumer_a2a.get("version") == "1.0"

    # agent_sdk_config.allowed_tools preserved (order-independent)
    consumer_sdk = consumer.get("agent_sdk_config") or {}
    assert set(consumer_sdk.get("allowed_tools", [])) == set(allowed_tools), (
        f"Consumer agent_sdk_config.allowed_tools mismatch: {consumer_sdk!r}"
    )


# ── Test 2: Bundle apply-update overwrites metadata fields ────────────────────


def test_bundle_metadata_apply_update_overwrites(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Publisher edits metadata fields + republishes; consumer apply-update overwrites.

    1. Publisher publishes revision 1 with initial metadata values.
    2. Consumer installs, verifying initial values are present.
    3. Publisher edits all 8 fields and publishes revision 2.
    4. Consumer applies update → every field is overwritten with the
       publisher's new values (publisher-authoritative rule).
    5. ``pending_update`` cleared and ``last_update_status = "synced"``.
    """
    # ── Phase 1: Publisher publishes revision 1 ────────────────────────────────
    pub_agent = create_agent_via_api(
        client, superuser_token_headers, name="MetaApplyUpdate-Publisher"
    )
    drain_tasks()
    pub_id = pub_agent["id"]

    v1_description = "Version 1 — initial description"
    v1_example_prompts = ["Prompt A", "Prompt B"]
    v1_a2a_config = {
        "enabled": False,
        "skills": [{"name": "skill-v1", "description": "V1 skill"}],
        "version": "1.0",
        "generated_at": "2024-01-01T00:00:00",
    }

    _set_agent_fields(
        client, superuser_token_headers, pub_id,
        {
            "description": v1_description,
            "example_prompts": v1_example_prompts,
            "status_refresh_command": "/run:v1",
            "agent_api_enabled": False,
            "agent_api_identity_enabled": False,
            "webapp_enabled": False,
            "a2a_config": v1_a2a_config,
        },
    )
    _add_allowed_tools(client, superuser_token_headers, pub_id, ["mcp__tool__v1"])

    _publish_agent(client, superuser_token_headers, pub_id, notes="v1")

    pub_fresh = _get_agent(client, superuser_token_headers, pub_id)
    bundle_id_str = pub_fresh["bundle_id"]
    bundle_uuid = pub_fresh["bundle_uuid"]
    make_bundle_public(client, superuser_token_headers, bundle_uuid)

    # ── Phase 2: Consumer installs; verifies v1 values ────────────────────────
    _, consumer_headers = _make_user_and_headers(client)
    consumer_install = install_bundle(client, consumer_headers, bundle_id_str)
    consumer_id = consumer_install["id"]

    consumer_v1 = _get_agent(client, consumer_headers, consumer_id)
    assert consumer_v1["description"] == v1_description
    assert consumer_v1["example_prompts"] == v1_example_prompts
    assert consumer_v1["agent_api_enabled"] is False
    assert consumer_v1["webapp_enabled"] is False
    c_sdk_v1 = consumer_v1.get("agent_sdk_config") or {}
    assert "mcp__tool__v1" in c_sdk_v1.get("allowed_tools", [])

    # ── Phase 3: Publisher edits all 8 fields + publishes revision 2 ──────────
    v2_description = "Version 2 — substantially updated"
    v2_example_prompts = ["New prompt X", "New prompt Y", "New prompt Z"]
    v2_a2a_config = {
        "enabled": True,
        "skills": [
            {"name": "analysis-v2", "description": "Analysis skill"},
            {"name": "reporting-v2", "description": "Reporting skill"},
        ],
        "version": "2.0",
        "generated_at": "2024-06-01T00:00:00",
    }

    _set_agent_fields(
        client, superuser_token_headers, pub_id,
        {
            "description": v2_description,
            "example_prompts": v2_example_prompts,
            "status_refresh_command": "/run:v2",
            "agent_api_enabled": True,
            "agent_api_identity_enabled": True,
            "webapp_enabled": True,
            "a2a_config": v2_a2a_config,
        },
    )
    # add_allowed_tools merges; after this the publisher has both v1 and v2 tools.
    _add_allowed_tools(client, superuser_token_headers, pub_id, ["mcp__tool__v2"])

    revision_2 = _publish_agent(client, superuser_token_headers, pub_id, notes="v2")
    assert revision_2["revision_number"] == 2

    # Consumer now has a pending update.
    consumer_check = _get_agent(client, consumer_headers, consumer_id)
    assert consumer_check["pending_update"] is True

    # ── Phase 4: Consumer applies update → v2 values overwrite v1 values ──────
    r = client.post(
        f"{API}/agents/{consumer_id}/apply-update", headers=consumer_headers
    )
    assert r.status_code == 200, f"apply-update failed: {r.text}"
    drain_tasks()

    consumer_v2 = _get_agent(client, consumer_headers, consumer_id)

    assert consumer_v2["description"] == v2_description, (
        f"description not overwritten by apply-update: {consumer_v2['description']!r}"
    )
    assert consumer_v2["example_prompts"] == v2_example_prompts, (
        f"example_prompts not overwritten: {consumer_v2['example_prompts']!r}"
    )
    assert consumer_v2["status_refresh_command"] == "/run:v2", (
        f"status_refresh_command not overwritten: {consumer_v2['status_refresh_command']!r}"
    )
    assert consumer_v2["agent_api_enabled"] is True, "agent_api_enabled not overwritten"
    assert consumer_v2["agent_api_identity_enabled"] is True, (
        "agent_api_identity_enabled not overwritten"
    )
    assert consumer_v2["webapp_enabled"] is True, "webapp_enabled not overwritten"

    consumer_a2a_v2 = consumer_v2.get("a2a_config") or {}
    assert consumer_a2a_v2.get("enabled") is True, "a2a_config.enabled not overwritten"
    assert consumer_a2a_v2.get("skills") == v2_a2a_config["skills"], (
        f"a2a_config.skills not overwritten: {consumer_a2a_v2.get('skills')!r}"
    )
    assert consumer_a2a_v2.get("version") == "2.0"

    # agent_sdk_config: consumer gets publisher's merged list (v1+v2 tools).
    consumer_sdk_v2 = consumer_v2.get("agent_sdk_config") or {}
    assert "mcp__tool__v2" in consumer_sdk_v2.get("allowed_tools", []), (
        f"agent_sdk_config.allowed_tools not updated: {consumer_sdk_v2!r}"
    )

    # ── Phase 5: Housekeeping fields cleared correctly ─────────────────────────
    assert consumer_v2["pending_update"] is False, "pending_update must be cleared"
    assert consumer_v2["last_update_status"] == "synced"


# ── Test 3: Git checkout metadata round-trip ──────────────────────────────────


def test_git_metadata_checkout_roundtrip(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """Git checkout with ``metadata`` block lands all 8 fields on the install.

    1. Build a v2 agent manifest containing the ``metadata`` block with all
       8 new fields set to non-default values.
    2. Run POST /agents/checkout (git network mocked via patch).
    3. The resulting agent has all 8 field values from the manifest.
    4. ``a2a_config.skills`` list (2 entries) survives identically.
    5. ``agent_sdk_config.allowed_tools`` list survives.
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.metadata.git.{random_lower_string()[:8]}"

    expected_a2a_config = {
        "enabled": True,
        "skills": [
            {"name": "write-tests", "description": "Writes unit tests"},
            {"name": "code-review", "description": "Reviews code quality"},
        ],
        "version": "2.0",
        "generated_at": "2024-06-01T00:00:00",
    }
    expected_sdk_config = {
        "sdk_tools": [],
        "allowed_tools": ["mcp__git__clone", "mcp__platform__file_read"],
    }

    # ── Phase 1: Write git repo tree with full metadata block ─────────────────
    clone_dir = tmp_path / "meta_git_repo"
    _write_git_checkout_dir(
        clone_dir,
        bundle_id,
        with_metadata=True,
        metadata_override={
            "description": "Git-checkout agent description",
            "example_prompts": ["Show me the code", "Explain the architecture"],
            "status_refresh_command": "/run:git-status",
            "agent_api_enabled": True,
            "agent_api_identity_enabled": True,
            "a2a_config": expected_a2a_config,
            "agent_sdk_config": expected_sdk_config,
            "webapp_enabled": True,
        },
    )

    # ── Phase 2: Git checkout (git network mocked) ────────────────────────────
    result = _do_git_checkout(client, headers, clone_dir=clone_dir)

    agent_id = result["agent"]["id"]
    assert agent_id, "Checkout must produce an agent"
    assert result["git_source"]["status"] == "connected"

    # ── Phase 3: All 8 metadata fields present on the installed agent ─────────
    agent = _get_agent(client, headers, agent_id)

    assert agent["description"] == "Git-checkout agent description", (
        f"description mismatch after git checkout: {agent['description']!r}"
    )
    assert agent["example_prompts"] == ["Show me the code", "Explain the architecture"], (
        f"example_prompts mismatch after git checkout: {agent['example_prompts']!r}"
    )
    assert agent["status_refresh_command"] == "/run:git-status", (
        f"status_refresh_command mismatch: {agent['status_refresh_command']!r}"
    )
    assert agent["agent_api_enabled"] is True, "agent_api_enabled should be True"
    assert agent["agent_api_identity_enabled"] is True, "agent_api_identity_enabled should be True"
    assert agent["webapp_enabled"] is True, "webapp_enabled should be True"

    # ── Phase 4: a2a_config.skills deep structure preserved ───────────────────
    a2a = agent.get("a2a_config") or {}
    assert a2a.get("enabled") is True, "a2a_config.enabled should be True"
    assert a2a.get("skills") == expected_a2a_config["skills"], (
        f"a2a_config.skills mismatch: {a2a.get('skills')!r}"
    )
    assert a2a.get("version") == "2.0"

    # ── Phase 5: agent_sdk_config.allowed_tools preserved (order-independent) ──
    sdk = agent.get("agent_sdk_config") or {}
    assert set(sdk.get("allowed_tools", [])) == set(expected_sdk_config["allowed_tools"]), (
        f"agent_sdk_config.allowed_tools mismatch: {sdk!r}"
    )


# ── Test 4: Back-compat / missing-key tolerance ──────────────────────────────


def test_bundle_metadata_back_compat_missing_keys(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """Revisions lacking new metadata do not crash and preserve existing values.

    Two sub-scenarios (A and B) cover the same underlying mechanism from
    different angles:

    A. **Git checkout — no metadata block (NULL columns)**:  A v2 manifest
       without a ``metadata`` key (simulating a pre-feature git repo) → checkout
       succeeds AND the installed agent has proper Agent-model defaults (False /
       [] / {}) rather than null garbage.  This is the main regression target
       for the ``is not None`` guards in ``_apply_revision_metadata``.

    B. **Bundle apply-update — NULL description**: The publisher never sets
       ``description`` (stays None).  After the consumer sets their own custom
       description and the publisher republishes (revision still has
       ``description=None``), apply-update must NOT null-out the consumer's
       description — the ``if revision.description is not None`` guard is the
       invariant under test.
    """
    # ── Part A: Git checkout with no ``metadata`` block ───────────────────────
    # The manifest produced by git repos predating this feature has no
    # ``metadata`` key at all.  ``manifest_to_revision_fields`` maps missing
    # keys to None via ``.get()``, so the revision row has NULL for every new
    # column.  ``_apply_revision_metadata``'s ``is not None`` guards must then
    # leave the Agent-model field defaults intact.
    git_bundle_id = f"io.test.backcompat.{random_lower_string()[:8]}"
    clone_dir = tmp_path / "no_metadata_repo"
    _write_git_checkout_dir(clone_dir, git_bundle_id, with_metadata=False)

    result = _do_git_checkout(
        client,
        superuser_token_headers,
        clone_dir=clone_dir,
        repo_url="https://github.com/example/old-agent.git",
    )
    assert result["agent"]["id"], (
        "Git checkout from a manifest without 'metadata' block must succeed (no crash)"
    )
    git_agent_id = result["agent"]["id"]
    git_agent = _get_agent(client, superuser_token_headers, git_agent_id)

    # The ``is not None`` guards must leave Agent-model defaults intact.
    # revision.agent_api_enabled == None → skip → stays at model default False.
    assert git_agent["agent_api_enabled"] is False, (
        "agent_api_enabled must default to False (not None) when absent from manifest"
    )
    # revision.webapp_enabled == None → skip → stays at model default False.
    assert git_agent["webapp_enabled"] is False, (
        "webapp_enabled must default to False (not None) when absent from manifest"
    )
    # revision.example_prompts == None → skip → stays at model default [].
    assert isinstance(git_agent["example_prompts"], list), (
        "example_prompts must be a list (Agent model default []) when absent from manifest"
    )
    # No crash means the checkout + env provisioning completed cleanly.
    assert result["git_source"]["status"] == "connected"

    # ── Part B: Bundle apply-update with NULL description ─────────────────────
    # When the publisher's agent.description is None (never set), the manifest
    # emits "description": null, revision.description == None.  After the
    # consumer sets their own description and the publisher republishes,
    # apply-update must not null-out the consumer's value.
    pub_agent = create_agent_via_api(
        client, superuser_token_headers, name="BackCompat-Publisher"
    )
    drain_tasks()
    pub_id = pub_agent["id"]

    # Publisher does NOT set description — it stays None (Agent model default).
    _publish_agent(client, superuser_token_headers, pub_id, notes="rev1-no-description")

    pub_fresh = _get_agent(client, superuser_token_headers, pub_id)
    bundle_id_str = pub_fresh["bundle_id"]
    bundle_uuid = pub_fresh["bundle_uuid"]
    make_bundle_public(client, superuser_token_headers, bundle_uuid)

    # Consumer is a developer so they can PUT their own installed agent.
    _, consumer_headers = _make_developer_consumer(client, superuser_token_headers)
    consumer_install = install_bundle(client, consumer_headers, bundle_id_str)
    consumer_id = consumer_install["id"]

    # Consumer sets their own custom description after install.
    custom_description = "My own custom description — must survive apply-update"
    _set_agent_fields(
        client, consumer_headers, consumer_id,
        {"description": custom_description},
    )

    consumer_before = _get_agent(client, consumer_headers, consumer_id)
    assert consumer_before["description"] == custom_description, (
        "Pre-condition: consumer must have their custom description before apply-update"
    )

    # Publisher republishes — description still None.
    revision_2 = _publish_agent(
        client, superuser_token_headers, pub_id, notes="rev2-still-no-description"
    )
    assert revision_2["revision_number"] == 2

    # Consumer applies update.  revision.description = None → ``is not None``
    # guard fires → skip → consumer's custom description preserved.
    r = client.post(
        f"{API}/agents/{consumer_id}/apply-update", headers=consumer_headers
    )
    assert r.status_code == 200, f"apply-update failed: {r.text}"
    drain_tasks()

    consumer_after = _get_agent(client, consumer_headers, consumer_id)
    assert consumer_after["description"] == custom_description, (
        "apply-update must NOT null-out the consumer's description when "
        "revision.description is None (pre-existing revisions without the "
        "description column must not clobber consumer-local values)"
    )


# ── Test 5: Exclusion guard ───────────────────────────────────────────────────


def test_bundle_metadata_exclusion_guard(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Per-install secrets and UI prefs are absent from the serialized manifest.

    The plan explicitly excludes from snapshot:
    - ``ui_color_preset``, ``conversation_mode_ui``, ``show_on_dashboard``
      (UI prefs that belong to the installing user, not the published definition)
    - ``agent_api_token``, ``agent_api_access_grant``, ``AgentAccessToken``
      (per-install secrets / access grants that must never travel)

    This test:
    1. Sets both definitional fields (which SHOULD appear) and excluded UI prefs
       on the publisher agent.
    2. Publishes.
    3. Verifies the revision manifest ``metadata`` block contains only the 8
       definitional fields and none of the excluded fields.
    4. Belt-and-suspenders: searches the full serialized manifest string for any
       occurrence of excluded secret key names.
    """
    # ── Phase 1: Set definitional fields AND excluded UI prefs ────────────────
    pub_agent = create_agent_via_api(
        client, superuser_token_headers, name="ExclusionGuard-Publisher"
    )
    drain_tasks()
    pub_id = pub_agent["id"]

    _set_agent_fields(
        client, superuser_token_headers, pub_id,
        {
            # These 4 fields SHOULD appear in the manifest metadata.
            "description": "Definitional description that should travel",
            "example_prompts": ["Legitimate prompt"],
            "webapp_enabled": True,
            "a2a_config": {
                "enabled": True,
                "skills": [{"name": "test-skill", "description": "A skill"}],
            },
            # These 3 UI prefs must NOT appear in the manifest.
            "ui_color_preset": "purple",
            "conversation_mode_ui": "compact",
            "show_on_dashboard": False,
        },
    )

    # ── Phase 2: Publish ───────────────────────────────────────────────────────
    revision = _publish_agent(
        client, superuser_token_headers, pub_id, notes="exclusion-guard-test"
    )

    # ── Phase 3: Inspect the manifest metadata block ──────────────────────────
    manifest = revision.get("manifest", {})
    assert manifest.get("schema_version") == 2, "Must be a schema_version=2 manifest"

    # The ``metadata`` block must be present (definitional fields do travel).
    meta = manifest.get("metadata")
    assert meta, "manifest must have a non-empty 'metadata' block after publish"
    assert meta.get("description") == "Definitional description that should travel", (
        "description (definitional) must appear in manifest metadata"
    )
    assert meta.get("example_prompts") == ["Legitimate prompt"], (
        "example_prompts (definitional) must appear in manifest metadata"
    )
    assert meta.get("webapp_enabled") is True, (
        "webapp_enabled (definitional) must appear in manifest metadata"
    )

    # ── Phase 4: UI prefs must NOT appear in metadata ─────────────────────────
    excluded_ui_prefs = ("ui_color_preset", "conversation_mode_ui", "show_on_dashboard")
    for key in excluded_ui_prefs:
        assert key not in meta, (
            f"'{key}' must NOT be in manifest['metadata'] — it is a per-install "
            f"UI pref that belongs to the installer, not the published definition"
        )

    # ── Phase 5: UI prefs must not appear anywhere in the manifest (top-level) ─
    for key in excluded_ui_prefs:
        assert key not in manifest, (
            f"'{key}' must NOT appear at the top level of the manifest either"
        )

    # ── Phase 6: Secret fields must not appear anywhere in the manifest ────────
    # Scan the full JSON string so a deeply-nested occurrence is also caught.
    manifest_str = json.dumps(manifest)
    secret_keys = ("agent_api_token", "agent_api_access_grant", "AgentAccessToken")
    for key in secret_keys:
        assert key not in manifest_str, (
            f"'{key}' must NOT appear anywhere in the manifest — it is a "
            f"per-install secret / access grant that must never be snapshotted"
        )
