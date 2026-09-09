"""A container built before the feature — the one failure a restart cannot fix.

``/app/core`` is copied out of the environment template when the environment is
*created*, so a container made before a route existed answers 404 to it forever.
Restarting re-runs the same image; only a rebuild replaces ``/app/core``. Until
the adapter distinguished that 404 from every other failure, all three surfaces
downstream had to guess which remedy to name — and told a pre-feature container
to restart, which can never work.

Scenarios:
  1. ``GET/POST /agents/{id}/skills`` — a container with no ``/config/skills``
     reports ``adapter_unsupported``, keeps its cached rows, and **still**
     reports ``adapter_unsupported`` once the environment is stopped. That
     precedence is the whole point: "wake it and refresh" is the one remedy
     that cannot break the loop for a pre-feature container. A container that
     merely fails to answer stays ``adapter_error``, and a rebuild that works
     clears the code.
  2. ``GET /agents/{id}/addons`` — the same code reaches the addons tab, and
     the plugin half of the list still comes back.
  3. Plugin install into such a container — ``unsupported_syncs`` is counted
     apart from ``failed_syncs``, the operation still reports ``success``
     (the link write genuinely happened), and the plugin is really installed.
     A transport failure on the same route is still a failure.

Test seam:
  ``EnvironmentTestAdapter.unsupported_endpoints`` — naming an env-core route
  there makes the stub raise ``EndpointUnsupportedError`` for it, which is what
  the Docker adapter raises on a 404 and only on a 404. The 404-versus-500
  split at the adapter itself is covered in
  ``tests/unit/test_adapter_endpoint_unsupported.py``; the reason-code
  classification in ``tests/unit/test_agent_skills_service.py``.
"""
import uuid
from typing import Any

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.addons import addons_by_name, get_agent_addons
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import stop_environment
from tests.utils.llm_plugin import (
    create_marketplace,
    install_agent_plugin,
    list_agent_plugins,
    seed_marketplace_plugin,
)

API = settings.API_V1_STR

SKILLS_ROUTE = "/config/skills"
PLUGINS_ROUTE = "/config/plugins"


# ── Helpers ────────────────────────────────────────────────────────────────


def _row(name: str, **overrides: Any) -> dict:
    """One entry in the shape env-core's ``GET /config/skills`` reports."""
    entry = {
        "name": name,
        "description": f"Does {name} things.",
        "source": "local",
        "plugin_ref": None,
        "path": f"skills/{name}",
        "has_scripts": False,
        "user_invocable": True,
        "model_invocable": True,
        "size_bytes": 512,
        "error": None,
        "warning": None,
        "secret_paths": [],
        "version": None,
    }
    entry.update(overrides)
    return entry


def _index(rows: list[dict], tree_hash: str = "hash-1") -> dict:
    return {"hash": tree_hash, "skills": rows, "errors": []}


def _install_adapter(
    lifecycle_manager, *, index: dict | None = None
) -> EnvironmentTestAdapter:
    """Route every ``get_adapter`` call at one adapter the test controls."""
    adapter = EnvironmentTestAdapter()
    adapter.skills_index = index or _index([])
    lifecycle_manager.get_adapter = lambda env: adapter
    return adapter


def _get_skills(client: TestClient, headers: dict[str, str], agent_id: str) -> dict:
    r = client.get(f"{API}/agents/{agent_id}/skills", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _refresh_skills(
    client: TestClient, headers: dict[str, str], agent_id: str
) -> dict:
    r = client.post(f"{API}/agents/{agent_id}/skills/refresh", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ── Scenario 1: the skills index ───────────────────────────────────────────


def test_a_pre_feature_container_asks_for_a_rebuild_even_while_asleep(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    ``adapter_unsupported`` is a third reason code, not a flavour of the other
    two, and it outranks both.

    Phases:
      1. An agent whose environment answers the index → one cached skill.
      2. ``/config/skills`` disappears (a container whose ``/app/core``
         predates the route) → 200, cached rows survive, and the reason is
         ``adapter_unsupported``, not ``adapter_error``.
      3. Stop the environment. A sleeping container normally reports
         ``env_not_running`` — but this one is ALSO too old, and a wake will
         not add the route, so the answer must stay ``adapter_unsupported``.
      4. A container that is merely unreachable is still ``adapter_error``:
         the split is about 404-versus-everything, not about being noisy.
      5. The rebuild lands (the route exists again) → the banner clears.
    """
    # ── Phase 1: a populated cache ───────────────────────────────────────
    adapter = _install_adapter(
        patch_environment_adapter, index=_index([_row("pdf-report")])
    )
    agent = create_agent_via_api(client, superuser_token_headers, name="Old-Core")
    agent_id = agent["id"]
    drain_tasks()

    assert [s["name"] for s in _get_skills(
        client, superuser_token_headers, agent_id
    )["skills"]] == ["pdf-report"]

    # ── Phase 2: the route is not there ──────────────────────────────────
    adapter.unsupported_endpoints = {SKILLS_ROUTE}

    payload = _refresh_skills(client, superuser_token_headers, agent_id)
    assert payload["error"] == "adapter_unsupported", (
        "a container that ANSWERED and has no such route is a rebuild, not a "
        "restart — sharing 'adapter_error' with an unreachable container is "
        "what made the card name the wrong remedy"
    )
    assert [s["name"] for s in payload["skills"]] == ["pdf-report"], (
        "cached rows must survive — blanking the card is a worse answer"
    )

    # The cached read serves the same reason without touching the container.
    assert _get_skills(client, superuser_token_headers, agent_id)[
        "error"
    ] == "adapter_unsupported"

    # ── Phase 3: asleep AND too old → still a rebuild ────────────────────
    env_id = get_agent(client, superuser_token_headers, agent_id)[
        "active_environment_id"
    ]
    stop_environment(client, superuser_token_headers, env_id)

    payload = _refresh_skills(client, superuser_token_headers, agent_id)
    assert payload["error"] == "adapter_unsupported", (
        "the unsupported branch is classified BEFORE the sleeping-status "
        "branch on purpose: 'refresh to wake it' sends the user around a loop "
        "the wake can never break"
    )

    # ── Phase 4: unreachable is a different story again ──────────────────
    adapter.unsupported_endpoints = set()

    async def _unreachable():
        raise RuntimeError("Failed to get skills index: connection refused")

    adapter.get_skills_index = _unreachable

    payload = _refresh_skills(client, superuser_token_headers, agent_id)
    assert payload["error"] == "env_not_running", (
        "the environment is stopped and the failure is not a 404 — the "
        "sleeping branch owns this one"
    )

    # ── Phase 5: the rebuild worked ──────────────────────────────────────
    async def _answers():
        return _index([_row("pdf-report"), _row("second")], tree_hash="hash-9")

    adapter.get_skills_index = _answers

    payload = _refresh_skills(client, superuser_token_headers, agent_id)
    assert payload["error"] is None, (
        "a sticky reason code would keep asking for the rebuild that already "
        "happened"
    )
    assert [s["name"] for s in payload["skills"]] == ["pdf-report", "second"]


# ── Scenario 2: the addons tab ─────────────────────────────────────────────


def test_the_addons_tab_names_the_same_remedy_and_keeps_its_plugin_rows(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    The addons projection reads the same cache, so the reason has to reach it
    verbatim — a second vocabulary here would be a second place to keep
    correct.

    Phases:
      1. An agent with a marketplace plugin on a pre-feature container.
      2. GET /addons → ``skills_error="adapter_unsupported"``; the plugin row
         is still returned, still ``ok``, still manageable.
    """
    # ── Phase 1: a plugin on a container with no skills route ────────────
    adapter = _install_adapter(patch_environment_adapter)
    agent = create_agent_via_api(client, superuser_token_headers, name="Old-Core-Addons")
    agent_id = agent["id"]
    drain_tasks()

    marketplace = create_marketplace(client, superuser_token_headers)
    plugin = seed_marketplace_plugin(
        client, superuser_token_headers, marketplace["id"], name="reporting"
    )
    install_agent_plugin(client, superuser_token_headers, agent_id, plugin["id"])

    adapter.unsupported_endpoints = {SKILLS_ROUTE}

    # ── Phase 2: the reason travels, the plugin half stands ──────────────
    r = client.post(
        f"{API}/agents/{agent_id}/addons/refresh", headers=superuser_token_headers
    )
    assert r.status_code == 200, r.text
    payload = r.json()

    assert payload["skills_error"] == "adapter_unsupported"
    assert payload["environment_id"] is not None
    rows = addons_by_name(payload)
    assert set(rows) == {"reporting"}
    assert rows["reporting"]["status"] == "ok", (
        "a plugin is not broken because the container is too old to list skills"
    )
    assert rows["reporting"]["can_manage"] is True

    # The cached read agrees with the refresh.
    assert get_agent_addons(client, superuser_token_headers, agent_id)[
        "skills_error"
    ] == "adapter_unsupported"


# ── Scenario 3: pushing a plugin manifest ──────────────────────────────────


def test_an_unsupported_environment_is_counted_apart_from_a_failed_one(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Installing into a container that cannot take a plugin manifest.

    Nothing about the link write went wrong — the install is real and shows up
    in the agent's plugin list. What did not happen is the push to a container
    whose core predates ``/config/plugins``, and no number of retries or
    restarts will change that. So it is counted in its own bucket, and the
    operation still reports success.

    Phases:
      1. Agent + marketplace plugin; the container has no ``/config/plugins``.
      2. Install → 200, ``unsupported_syncs=1``, ``failed_syncs=0``,
         ``success=True``, and the env entry says ``unsupported`` with a
         sentence explaining itself.
      3. The link write really did land: the plugin is in the agent's list.
      4. Contrast: a container that is merely unreachable is a *failure* —
         ``failed_syncs=1``, ``unsupported_syncs=0``, ``success=False``. If
         both landed in the same bucket the client could only ever guess.
    """
    # ── Phase 1: a container that cannot take a manifest ─────────────────
    adapter = _install_adapter(patch_environment_adapter)
    agent = create_agent_via_api(client, superuser_token_headers, name="No-Plugin-Route")
    agent_id = agent["id"]
    drain_tasks()

    marketplace = create_marketplace(client, superuser_token_headers)
    plugin = seed_marketplace_plugin(
        client, superuser_token_headers, marketplace["id"], name="reporting"
    )

    adapter.unsupported_endpoints = {PLUGINS_ROUTE}

    # ── Phase 2: unsupported is its own bucket ───────────────────────────
    resp = install_agent_plugin(
        client, superuser_token_headers, agent_id, plugin["id"]
    )

    assert resp["unsupported_syncs"] == 1, resp
    assert resp["failed_syncs"] == 0, (
        "a container that predates the endpoint has not failed — nothing here "
        "is retryable, and lumping it in with transport failures keeps "
        "offering 'restart and try again' for a state that outlives every "
        f"restart: {resp}"
    )
    assert resp["successful_syncs"] == 0, resp
    assert resp["success"] is True, (
        "the link write succeeded; only the push to one old container did not"
    )

    synced = resp["environments_synced"]
    assert len(synced) == 1, synced
    assert synced[0]["status"] == "unsupported", (
        "'unsupported' is the per-environment half of the same distinction — "
        f"a client that only sees 'error' here cannot name the remedy: {synced}"
    )
    # The copy itself is display text and is deliberately not asserted word for
    # word (it has already been reworded once); that it is *present* is the
    # contract — a bucket with no sentence in it tells the user nothing.
    assert (synced[0]["error_message"] or "").strip(), synced

    # ── Phase 3: the install is real ─────────────────────────────────────
    installed = list_agent_plugins(client, superuser_token_headers, agent_id)
    assert [p["plugin_name"] for p in installed] == ["reporting"], installed
    assert resp["plugin_link"] is not None
    assert resp["plugin_link"]["plugin_id"] == plugin["id"]

    # ── Phase 4: an unreachable container is still a failure ─────────────
    other_adapter = _install_adapter(patch_environment_adapter)
    other = create_agent_via_api(
        client, superuser_token_headers, name="Unreachable-Env"
    )
    other_id = other["id"]
    drain_tasks()

    async def _unreachable(_manifest):
        raise Exception("Failed to set plugins: connection refused")

    other_adapter.set_plugins = _unreachable

    resp = install_agent_plugin(
        client, superuser_token_headers, other_id, plugin["id"]
    )

    assert resp["failed_syncs"] == 1, resp
    assert resp["unsupported_syncs"] == 0, (
        "a transport failure must not borrow the rebuild copy"
    )
    assert resp["success"] is False, resp
    assert resp["environments_synced"][0]["status"] == "error", resp


# ── Guards ─────────────────────────────────────────────────────────────────


def test_the_reason_is_never_served_to_someone_elses_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """An unknown agent id is a 404 on every surface that carries the code."""
    ghost = str(uuid.uuid4())

    assert client.get(
        f"{API}/agents/{ghost}/skills", headers=superuser_token_headers
    ).status_code == 404
    assert client.post(
        f"{API}/agents/{ghost}/skills/refresh", headers=superuser_token_headers
    ).status_code == 404
    assert client.get(
        f"{API}/agents/{ghost}/addons", headers=superuser_token_headers
    ).status_code == 404
