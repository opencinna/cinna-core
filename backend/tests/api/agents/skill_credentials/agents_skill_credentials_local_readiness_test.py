"""Credential readiness for skills with no pinned revision (local, plugin).

A catalog install is judged against its revision's frozen specs. A local
skill, or a marketplace plugin's skill, declares its slots only in its own
``SKILL.md``, which reaches the backend through the environment's skill index.
The Addons row judges those declarations with the same reasons — amber, never
an error, never a block — and the agent's credential listing carries each
credential's slot, so a client reading it can tell which slot is filled.

Scenarios:
  1. A local skill declaring a slot nothing carries reads warning /
     ``credential_missing`` / ``not_linked``. A credential of another type on
     that slot changes nothing; one of the declared type clears the issue on
     the next cached read, with no refresh. The agent credential listing
     reports the same ``service_uri`` as the credential itself.
  2. A credential another user shares with the agent owner fills a local
     skill's slot, and the agent listing shows that shared credential's slot.
  3. A marketplace plugin whose two skills declare the same slot: one issue on
     the plugin row, not two.
  4. A container that reports entries without ``credentials`` (pre-feature):
     no issues, the row stays ``ok``.

Notes:
  ``not_configured`` and ``access_revoked`` for declarations are unit-tested in
  ``tests/unit/test_skill_slot_index_declarations.py``: a placeholder can only
  be created by an install. Catalog rows are covered in
  ``agents_skill_credentials_install_test.py``.
"""
from typing import Any

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.addons import addons_by_name, get_agent_addons, refresh_agent_addons
from tests.utils.credential import (
    create_random_credential,
    get_agent_credentials,
    get_credential,
    link_credential_to_agent,
    share_credential_via_api,
    update_credential,
)
from tests.utils.llm_plugin import (
    create_marketplace,
    install_agent_plugin,
    seed_marketplace_plugin,
)
from tests.utils.skill_catalog import make_agent_with_env, make_developer

API = settings.API_V1_STR
SLOT = "some-token.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(name: str, **overrides: Any) -> dict:
    """One env-core ``GET /config/skills`` entry, a local skill by default."""
    entry = {
        "name": name,
        "description": f"Does {name} things.",
        "source": "local",
        "plugin_ref": None,
        "path": f"skills/{name}",
        "has_scripts": True,
        "user_invocable": True,
        "model_invocable": True,
        "size_bytes": 512,
        "error": None,
        "warning": None,
        "secret_paths": [],
    }
    entry.update(overrides)
    return entry


def _declares(slot: str = SLOT, credential_type: str = "api_token") -> list[dict]:
    return [{"slot": slot, "type": credential_type, "description": None}]


def _install_adapter(lifecycle_manager, entries: list[dict]) -> EnvironmentTestAdapter:
    """Route every ``get_adapter`` call at one adapter reporting ``entries``."""
    adapter = EnvironmentTestAdapter()
    adapter.skills_index = {"hash": "hash-1", "skills": entries, "errors": []}
    adapter.workspace_files = {}
    lifecycle_manager.get_adapter = lambda env: adapter
    return adapter


def _slotted_credential(
    client: TestClient,
    headers: dict[str, str],
    credential_type: str,
    **fields: Any,
) -> dict:
    credential = create_random_credential(client, headers, credential_type=credential_type)
    update_credential(client, headers, credential["id"], service_uri=SLOT, **fields)
    return credential


# ---------------------------------------------------------------------------
# Scenario 1: a local skill's slot, from missing to filled
# ---------------------------------------------------------------------------


def test_local_skill_slot_warns_until_a_credential_of_its_type_is_linked(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. The index reports a local skill declaring an ``api_token`` slot.
    2. Nothing carries the slot → warning / ``credential_missing`` /
       ``not_linked``.
    3. An ``odoo`` credential on the same slot → still ``not_linked``.
    4. An ``api_token`` credential on the slot → ``ok`` on a cached read.
    5. The agent credential listing reports each linked credential's slot,
       equal to the credential's own ``service_uri``.
    """
    _, headers = make_developer(client, superuser_token_headers)
    agent_id, _ = make_agent_with_env(client, headers, "LocalSlot-Agent")
    _install_adapter(
        patch_environment_adapter, [_entry("dad-jokes", credentials=_declares())]
    )

    row = addons_by_name(refresh_agent_addons(client, headers, agent_id))["dad-jokes"]
    assert row["source"] == "local"
    assert row["status"] == "warning"
    assert row["status_code"] == "credential_missing"
    assert row["credential_issues"] == [
        {"slot": SLOT, "type": "api_token", "reason": "not_linked"}
    ]

    other_type = _slotted_credential(client, headers, "odoo")
    link_credential_to_agent(client, headers, agent_id, other_type["id"])
    row = addons_by_name(get_agent_addons(client, headers, agent_id))["dad-jokes"]
    assert row["credential_issues"] == [
        {"slot": SLOT, "type": "api_token", "reason": "not_linked"}
    ]

    token = _slotted_credential(client, headers, "api_token")
    link_credential_to_agent(client, headers, agent_id, token["id"])
    row = addons_by_name(get_agent_addons(client, headers, agent_id))["dad-jokes"]
    assert row["status"] == "ok"
    assert row["status_code"] is None
    assert row["credential_issues"] == []

    listing = {c["id"]: c for c in get_agent_credentials(client, headers, agent_id)["data"]}
    for credential_id in (token["id"], other_type["id"]):
        assert listing[credential_id]["service_uri"] == SLOT
        assert (
            listing[credential_id]["service_uri"]
            == get_credential(client, headers, credential_id)["service_uri"]
        )


# ---------------------------------------------------------------------------
# Scenario 2: a shared credential fills a local slot
# ---------------------------------------------------------------------------


def test_shared_credential_fills_a_local_slot_and_shows_its_slot(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. User B owns an ``api_token`` credential on the slot and shares it with A.
    2. A links it to an agent whose local skill declares the slot.
    3. The row is ``ok``, and A's agent listing shows the credential's slot.
    """
    _, owner_headers = make_developer(client, superuser_token_headers)
    user_a, a_headers = make_developer(client, superuser_token_headers)
    shared = _slotted_credential(client, owner_headers, "api_token", allow_sharing=True)
    share_credential_via_api(client, owner_headers, shared["id"], user_a["email"])

    agent_id, _ = make_agent_with_env(client, a_headers, "SharedSlot-Agent")
    _install_adapter(
        patch_environment_adapter, [_entry("dad-jokes", credentials=_declares())]
    )
    link_credential_to_agent(client, a_headers, agent_id, shared["id"])

    row = addons_by_name(refresh_agent_addons(client, a_headers, agent_id))["dad-jokes"]
    assert row["status"] == "ok"
    assert row["credential_issues"] == []

    listing = {c["id"]: c for c in get_agent_credentials(client, a_headers, agent_id)["data"]}
    assert listing[shared["id"]]["service_uri"] == SLOT


# ---------------------------------------------------------------------------
# Scenario 3: a plugin's skills share one slot
# ---------------------------------------------------------------------------


def test_plugin_row_reports_a_slot_its_skills_share_once(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. A marketplace plugin is installed; the index folds two of its skills
       into its row, both declaring the same slot.
    2. The plugin row reads warning / ``credential_missing`` with ONE issue.
    """
    _, headers = make_developer(client, superuser_token_headers)
    agent_id, _ = make_agent_with_env(client, headers, "PluginSlot-Agent")
    adapter = _install_adapter(patch_environment_adapter, [])

    marketplace = create_marketplace(client, superuser_token_headers)
    plugin = seed_marketplace_plugin(
        client, superuser_token_headers, marketplace["id"], name="joke-tools"
    )
    install_agent_plugin(client, headers, agent_id, plugin["id"])
    plugin_row = addons_by_name(get_agent_addons(client, headers, agent_id))["joke-tools"]
    ref = f"{plugin_row['marketplace_name']}/{plugin_row['name']}"

    adapter.skills_index = {
        "hash": "hash-2",
        "skills": [
            _entry(
                name,
                source="plugin",
                plugin_ref=ref,
                path=f"plugins/{ref}/skills/{name}",
                credentials=_declares(),
            )
            for name in ("puns", "one-liners")
        ],
        "errors": [],
    }
    row = addons_by_name(refresh_agent_addons(client, headers, agent_id))["joke-tools"]
    assert row["kind"] == "plugin"
    assert sorted(skill["name"] for skill in row["skills"]) == ["one-liners", "puns"]
    assert row["status"] == "warning"
    assert row["status_code"] == "credential_missing"
    assert row["credential_issues"] == [
        {"slot": SLOT, "type": "api_token", "reason": "not_linked"}
    ]


# ---------------------------------------------------------------------------
# Scenario 4: a pre-feature container reports no declarations
# ---------------------------------------------------------------------------


def test_entries_without_declarations_raise_no_credential_issues(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """An index entry with no ``credentials`` key leaves the row ``ok``."""
    _, headers = make_developer(client, superuser_token_headers)
    agent_id, _ = make_agent_with_env(client, headers, "NoSlot-Agent")
    _install_adapter(patch_environment_adapter, [_entry("dad-jokes")])

    row = addons_by_name(refresh_agent_addons(client, headers, agent_id))["dad-jokes"]
    assert row["skills"][0]["credentials"] == []
    assert row["status"] == "ok"
    assert row["credential_issues"] == []


# ---------------------------------------------------------------------------
# Scenario 5 (C14): an issued agent_api key is ready only while it is active
# ---------------------------------------------------------------------------


def test_issued_agent_api_key_slot_is_not_ready_once_revoked(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. A local skill declares an ``agent_api`` slot; an issued key's credential
       (slot = the producer's default ``<slug>-api``) is linked → ``ok``.
    2. The issuer revokes the key → the slot reads ``access_revoked``.
       (An expired key is not reachable through the API: ``expires_in_days >= 1``.)
    """
    from tests.utils.agent_api_keys import setup_producer

    _, headers = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, headers, name="Ready Producer")
    agent_id, _ = make_agent_with_env(client, headers, "KeySlot-Consumer")

    r = client.post(
        f"{API}/agents/{producer['id']}/agent-api/keys",
        headers=headers,
        json={"read_only_override": False},
    )
    assert r.status_code == 200, r.text
    key = r.json()
    slot = get_credential(client, headers, key["credential_id"])["service_uri"]
    assert slot == "ready-producer-api"

    _install_adapter(
        patch_environment_adapter,
        [_entry("keyed", credentials=_declares(slot, "agent_api"))],
    )
    link_credential_to_agent(client, headers, agent_id, key["credential_id"])

    row = addons_by_name(refresh_agent_addons(client, headers, agent_id))["keyed"]
    assert row["status"] == "ok"
    assert row["credential_issues"] == []

    r = client.post(
        f"{API}/agents/{producer['id']}/agent-api/keys/{key['id']}/revoke", headers=headers
    )
    assert r.status_code == 200, r.text

    row = addons_by_name(refresh_agent_addons(client, headers, agent_id))["keyed"]
    assert row["status"] == "warning"
    assert row["credential_issues"] == [
        {"slot": slot, "type": "agent_api", "reason": "access_revoked"}
    ]
