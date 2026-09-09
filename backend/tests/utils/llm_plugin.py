"""Helpers for marketplace plugins in API tests.

A marketplace plugin row is only ever created by a *sync* — there is no "add
plugin" endpoint — so seeding one means driving the real sync route with the
git clone stubbed out. That is the same seam
``tests/api/agents/core/agents_resilient_plugins_test.py`` uses; it lives here
so a second test file does not need a second copy of the ninety lines.

Two seams, deliberately different:

* ``seed_marketplace_plugin`` stubs the **parser** too and hands it a row —
  the fastest way to get one plugin a test only needs as a fixture.
* ``sync_marketplace(populate=...)`` stubs only the clone and writes a real
  fixture repository into it, so the parser under test, ``_upsert_plugins``
  and discover are all shipping code. Use this one whenever what the parser
  produces is part of the assertion.
"""
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings

API = settings.API_V1_STR
PLUGINS_BASE = f"{API}/llm-plugins"


def create_marketplace(
    client: TestClient,
    superuser_headers: dict[str, str],
    *,
    url: str = "https://example.com/plugins.git",
    public_discovery: bool = True,
    marketplace_type: str | None = None,
    expected_status: int = 200,
) -> dict:
    """Register a plugin marketplace. Superuser-only route.

    ``marketplace_type`` is the repository format (``claude`` | ``codex`` |
    ``skills``). Omitted, the route's own default (``claude``) applies, so an
    unknown value can be passed deliberately to assert the 422.
    """
    payload: dict = {
        "url": url,
        "git_branch": "main",
        "public_discovery": public_discovery,
    }
    if marketplace_type is not None:
        payload["type"] = marketplace_type
    r = client.post(
        f"{PLUGINS_BASE}/marketplaces", headers=superuser_headers, json=payload
    )
    assert r.status_code == expected_status, (
        f"create marketplace: expected {expected_status}, got "
        f"{r.status_code}: {r.text}"
    )
    return r.json()


def update_marketplace(
    client: TestClient,
    superuser_headers: dict[str, str],
    marketplace_id: str,
    payload: dict,
    *,
    expected_status: int = 200,
) -> dict:
    """PUT ``/llm-plugins/marketplaces/{id}``."""
    r = client.put(
        f"{PLUGINS_BASE}/marketplaces/{marketplace_id}",
        headers=superuser_headers,
        json=payload,
    )
    assert r.status_code == expected_status, (
        f"update marketplace: expected {expected_status}, got "
        f"{r.status_code}: {r.text}"
    )
    return r.json()


def get_marketplace(
    client: TestClient,
    superuser_headers: dict[str, str],
    marketplace_id: str,
) -> dict:
    """GET ``/llm-plugins/marketplaces/{id}`` — status, message, metadata."""
    r = client.get(
        f"{PLUGINS_BASE}/marketplaces/{marketplace_id}", headers=superuser_headers
    )
    assert r.status_code == 200, r.text
    return r.json()


@contextmanager
def stubbed_clone(
    populate: Callable[[Path], None] | None = None,
    *,
    commit_hash: str = "abc123def456",
) -> Iterator[None]:
    """Replace the sync's git clone with a fixture repository on disk.

    ``populate`` receives the throwaway clone directory the real
    ``sync_marketplace`` created and writes the repository's files into it —
    a ``.agents/plugins/marketplace.json``, a tree of ``SKILL.md`` folders,
    whatever the format under test reads. **The parser itself is not stubbed**:
    everything from the catalog file down through ``_upsert_plugins`` to
    discover is the shipping code path.
    """

    def _fake_clone(url, target_dir, branch, ssh_key=None):
        if populate is not None:
            populate(Path(target_dir))

        class _FakeRepo:
            pass

        return _FakeRepo()

    with (
        patch(
            "app.services.plugins.llm_plugin_service.clone_repository",
            side_effect=_fake_clone,
        ),
        patch(
            "app.services.plugins.llm_plugin_service.get_current_commit_hash",
            return_value=commit_hash,
        ),
    ):
        yield


def sync_marketplace(
    client: TestClient,
    superuser_headers: dict[str, str],
    marketplace_id: str,
    *,
    populate: Callable[[Path], None] | None = None,
    commit_hash: str = "abc123def456",
    expected_status: int = 200,
) -> dict:
    """Sync ``marketplace_id`` against the fixture repo ``populate`` writes."""
    with stubbed_clone(populate, commit_hash=commit_hash):
        r = client.post(
            f"{PLUGINS_BASE}/marketplaces/{marketplace_id}/sync",
            headers=superuser_headers,
        )
    assert r.status_code == expected_status, (
        f"sync marketplace: expected {expected_status}, got "
        f"{r.status_code}: {r.text}"
    )
    return r.json()


def discover_plugins(
    client: TestClient,
    headers: dict[str, str],
    *,
    search: str | None = None,
    plugin_type: str | None = None,
    category: str | None = None,
    marketplace_id: str | None = None,
    limit: int | None = None,
) -> dict:
    """GET ``/llm-plugins/discover`` — the full ``{data, count}`` payload."""
    params: dict = {}
    if search is not None:
        params["search"] = search
    if plugin_type is not None:
        params["plugin_type"] = plugin_type
    if category is not None:
        params["category"] = category
    if marketplace_id is not None:
        params["marketplace_id"] = marketplace_id
    if limit is not None:
        params["limit"] = limit
    r = client.get(f"{PLUGINS_BASE}/discover", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def plugins_by_name(payload: dict) -> dict[str, dict]:
    """Discover rows keyed by plugin name."""
    rows = {row["name"]: row for row in payload["data"]}
    assert len(rows) == len(payload["data"]), (
        f"two rows share a name: {[r['name'] for r in payload['data']]}"
    )
    return rows


def list_agent_plugins(
    client: TestClient, headers: dict[str, str], agent_id: str
) -> list[dict]:
    """GET ``/llm-plugins/agents/{id}/plugins`` — the link rows."""
    r = client.get(f"{PLUGINS_BASE}/agents/{agent_id}/plugins", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def upgrade_agent_plugin(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    link_id: str,
    *,
    expected_status: int = 200,
) -> dict:
    """POST ``/llm-plugins/agents/{id}/plugins/{link_id}/upgrade``."""
    r = client.post(
        f"{PLUGINS_BASE}/agents/{agent_id}/plugins/{link_id}/upgrade",
        headers=headers,
    )
    assert r.status_code == expected_status, (
        f"upgrade plugin: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def force_marketplace_type(db: Session, marketplace_id: str, raw_type: str) -> None:
    """Write ``type`` straight onto the row, bypassing the API's validation.

    A documented DB seam, and a narrow one: the create/update schemas validate
    ``type`` against ``MarketplaceType``, which is *the behaviour under test* —
    so the "legacy row written before that validation existed" this exercises
    cannot be produced through any endpoint. Only the model is imported (never
    a service or CRUD function), and only this one column is written.
    """
    from app.models.plugins.llm_plugin import LLMPluginMarketplace

    marketplace = db.get(LLMPluginMarketplace, uuid.UUID(str(marketplace_id)))
    assert marketplace is not None, f"marketplace {marketplace_id} not found"
    marketplace.type = raw_type
    db.add(marketplace)
    db.commit()


def orphan_plugin_link(db: Session, link_id: str) -> None:
    """Null one install's ``plugin_id``, keeping its snapshot names.

    A documented DB seam, and a narrow one: this is the state a link is left in
    by an *older* build — orphaned while its marketplace entry was gone, then
    left behind when the entry came back. The sync now re-attaches such a link
    the moment the entry reappears (``_reattach_orphaned_links``), which is
    exactly the behaviour under test, so the state cannot be produced through
    any endpoint any more. Only the model is imported, and only this one column
    is written.
    """
    from app.models.plugins.llm_plugin import AgentPluginLink

    link = db.get(AgentPluginLink, uuid.UUID(str(link_id)))
    assert link is not None, f"plugin link {link_id} not found"
    assert link.snapshot_plugin_name, (
        "the snapshot names are the identity this seam depends on"
    )
    link.plugin_id = None
    db.add(link)
    db.commit()


def clear_link_repository_url(db: Session, link_id: str) -> None:
    """Null one install's ``snapshot_repository_url``, keeping everything else.

    A documented DB seam, and a narrow one: this is the state of a marketplace
    install made *before* the column existed and already orphaned *before*
    migration ``d7b41e0c9a35`` ran — the migration backfills from the live
    marketplace row only where ``plugin_id`` survived, so an already-orphaned
    link had nothing to backfill from and keeps a NULL. Every install written
    since records the URL (``llm_plugin_service.py:1716``), so this state is
    unreachable through any endpoint, and it is exactly the fleet the
    fail-closed rule in ``_reattach_orphaned_links`` is about. Only the model is
    imported, and only this one column is written.
    """
    from app.models.plugins.llm_plugin import AgentPluginLink

    link = db.get(AgentPluginLink, uuid.UUID(str(link_id)))
    assert link is not None, f"plugin link {link_id} not found"
    link.snapshot_repository_url = None
    db.add(link)
    db.commit()


def force_plugin_type(db: Session, plugin_id: str, raw_type: str) -> None:
    """Write ``plugin_type`` straight onto a marketplace plugin row.

    A documented DB seam. ``plugin_type`` is copied from ``marketplace.type``
    on every sync, and that column is now a closed ``Literal`` — so a row
    holding a word from before that vocabulary existed (the documented values
    once included ``custom``) is unreachable through the API. That legacy row
    is what the manifest builder's allowlist exists for. Only the model is
    imported, and only this one column is written.
    """
    from app.models.plugins.llm_plugin import LLMPluginMarketplacePlugin

    plugin = db.get(LLMPluginMarketplacePlugin, uuid.UUID(str(plugin_id)))
    assert plugin is not None, f"plugin {plugin_id} not found"
    plugin.plugin_type = raw_type
    db.add(plugin)
    db.commit()


@contextmanager
def capturing_plugin_manifest(lifecycle_manager) -> Iterator[list[dict]]:
    """Route every environment adapter at one stub and collect its manifests.

    ``build_plugin_manifest`` has no endpoint of its own — the manifest is only
    ever observable as the argument the environment adapter is handed during an
    install/sync. Yields the list the adapter appends to, newest last.
    """
    from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter

    captured: list[dict] = []

    class _Capture(EnvironmentTestAdapter):
        async def set_plugins(self, manifest: dict) -> list[dict]:
            captured.append(manifest)
            return await super().set_plugins(manifest)

    original = lifecycle_manager.get_adapter
    lifecycle_manager.get_adapter = lambda env: _Capture()
    try:
        yield captured
    finally:
        lifecycle_manager.get_adapter = original


def seed_marketplace_plugin(
    client: TestClient,
    superuser_headers: dict[str, str],
    marketplace_id: str,
    *,
    name: str = "test-plugin",
    marketplace_name: str = "test-marketplace",
    description: str = "Test plugin",
    source_path: str | None = None,
    commit_hash: str = "abc123def456",
    version: str = "1.0",
) -> dict:
    """Sync one plugin row into ``marketplace_id`` and return its discover row.

    The clone and the parser are stubbed; everything downstream of them — the
    upsert, the marketplace metadata, discover — is the real code path.
    """
    from app.models.plugins.llm_plugin import PluginSourceType

    source_path = source_path or f"plugins/{name}"
    parsed = {
        "metadata": {"name": marketplace_name},
        "plugins": [
            {
                "name": name,
                "description": description,
                "version": version,
                "author_name": "tester",
                "author_email": "tester@example.com",
                "category": "tools",
                "homepage": "",
                "source_path": source_path,
                "source_type": PluginSourceType.local,
                "source_url": None,
                "source_branch": "main",
                "config": {"name": name},
            }
        ],
    }

    def _fake_clone(url, target_dir, branch, ssh_key=None):
        class _FakeRepo:
            pass

        return _FakeRepo()

    with (
        patch(
            "app.services.plugins.llm_plugin_service.clone_repository",
            side_effect=_fake_clone,
        ),
        patch(
            "app.services.plugins.llm_plugin_service.get_current_commit_hash",
            return_value=commit_hash,
        ),
        patch(
            "app.services.plugins.llm_plugin_service."
            "LLMPluginService._parse_claude_marketplace",
            return_value=parsed,
        ),
    ):
        r = client.post(
            f"{PLUGINS_BASE}/marketplaces/{marketplace_id}/sync",
            headers=superuser_headers,
        )
        assert r.status_code == 200, r.text

    r = client.get(
        f"{PLUGINS_BASE}/discover", headers=superuser_headers, params={"search": name}
    )
    assert r.status_code == 200, r.text
    matched = [p for p in r.json()["data"] if p["name"] == name]
    assert matched, f"plugin {name!r} not found after sync"
    return matched[0]


def install_agent_plugin(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    plugin_id: str,
    *,
    conversation_mode: bool = True,
    building_mode: bool = True,
    expected_status: int = 200,
) -> dict:
    """POST ``/llm-plugins/agents/{agent_id}/plugins``."""
    r = client.post(
        f"{PLUGINS_BASE}/agents/{agent_id}/plugins",
        headers=headers,
        json={
            "plugin_id": plugin_id,
            "conversation_mode": conversation_mode,
            "building_mode": building_mode,
        },
    )
    assert r.status_code == expected_status, (
        f"install plugin: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def delete_marketplace(
    client: TestClient,
    superuser_headers: dict[str, str],
    marketplace_id: str,
    *,
    expected_status: int = 200,
) -> dict:
    """DELETE ``/llm-plugins/marketplaces/{id}`` — takes its plugin rows with it."""
    r = client.delete(
        f"{PLUGINS_BASE}/marketplaces/{marketplace_id}", headers=superuser_headers
    )
    assert r.status_code == expected_status, (
        f"delete marketplace: expected {expected_status}, got "
        f"{r.status_code}: {r.text}"
    )
    return r.json()
