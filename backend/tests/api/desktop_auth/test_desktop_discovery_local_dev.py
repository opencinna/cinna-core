"""Backend tests for the ``local_dev`` block on /.well-known/cinna-desktop.

Covers commit 1d8f3c6d ("desktop discovery: advertise the local-dev bootstrap
block"):

  1. Both gates on  → ``local_dev`` present, shaped, and derived from the
     BACKEND origin (not FRONTEND_HOST) — including a split-host phase that
     would fail if the endpoint were built from the SPA origin.
  2. ``DESKTOP_LOCAL_DEV_ENABLED=False`` → the key is absent entirely.
  3. ``DESKTOP_AUTH_ENABLED=False``      → the key is absent entirely.
  4. In every case the six pre-existing keys are present and unchanged — the
     backward-compatibility guarantee the whole "a desktop that does not know
     the block ignores it" argument rests on.
  5. No drift: ``local_dev.cinna_cli_version`` equals the ``cinna_cli_version``
     that GET /cli/agents/{id}/sync-runtime serves, because both read the same
     ``settings.CINNA_CLI_VERSION``.

Notes:
  - The route reads ``settings`` at call time, so the two "absent" tests are
    themselves the proof that ``monkeypatch.setattr(settings, ...)`` takes
    effect: the field defaults to ``True``, so a no-op patch would leave the
    block present and both tests would fail.
  - Versions are asserted against the ``settings`` object rather than against a
    literal, so bumping the pin does not fail the suite.
"""
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.cli import (
    cli_auth_headers,
    create_setup_token,
    exchange_setup_token,
)
from tests.utils.fixtures import (
    BACKGROUND_TASK_TARGETS_FULL,
    CREATE_SESSION_TARGETS_AGENT,
    create_default_ai_credential,
    patched_background_tasks,
    patched_create_sessions,
    patched_external_services,
    setup_environment_adapter,
    teardown_environment_adapter,
)

_DISCOVERY = "/.well-known/cinna-desktop"

# The six keys that existed before the local_dev block was added. Adding an
# optional block must not disturb any of them.
_LEGACY_KEYS = (
    "instance_name",
    "authorization_endpoint",
    "token_endpoint",
    "userinfo_endpoint",
    "version",
    "desktop_auth_enabled",
)


def _assert_legacy_payload_intact(data: dict, *, desktop_auth_enabled: bool) -> None:
    """The pre-existing discovery contract, asserted the same way every time."""
    for key in _LEGACY_KEYS:
        assert key in data, f"Pre-existing discovery key {key!r} is missing from {data}"

    assert data["instance_name"] == settings.PROJECT_NAME
    assert data["version"] == "1.0"
    assert data["desktop_auth_enabled"] is desktop_auth_enabled

    base = f"{settings.backend_base_url}{settings.API_V1_STR}/desktop-auth"
    assert data["authorization_endpoint"] == f"{base}/authorize"
    assert data["token_endpoint"] == f"{base}/token"
    assert data["userinfo_endpoint"] == f"{base}/userinfo"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


# ── Scenario 1: both gates on → the block is present and correctly derived ───


def test_discovery_advertises_local_dev_when_both_gates_are_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    local_dev block, default settings:
      1. The six pre-existing keys are untouched
      2. local_dev is present with exactly its three documented keys
      3. Versions come from settings (a pin bump must not fail this test)
      4. setup_token_endpoint is the BACKEND origin + API_V1_STR + the existing
         mint route — asserted as a shared prefix with authorization_endpoint
         rather than as a literal, so it is coupled to the split-host property
      5. Split-host: with BACKEND_BASE_URL pointed somewhere other than
         FRONTEND_HOST, the endpoint follows the backend origin
    """
    # ── Phase 1: Pre-existing keys are untouched ──────────────────────────
    r = client.get(_DISCOVERY)
    assert r.status_code == 200, r.text
    data = r.json()
    _assert_legacy_payload_intact(data, desktop_auth_enabled=True)

    # ── Phase 2: local_dev present, exact shape ───────────────────────────
    assert "local_dev" in data, f"Expected a local_dev block in {data}"
    local_dev = data["local_dev"]
    assert set(local_dev) == {
        "setup_token_endpoint",
        "cinna_cli_version",
        "mutagen_version",
    }, f"Unexpected local_dev shape: {local_dev}"

    # ── Phase 3: Versions are the settings pins, not literals ─────────────
    assert local_dev["cinna_cli_version"] == settings.CINNA_CLI_VERSION
    assert local_dev["mutagen_version"] == settings.MUTAGEN_VERSION
    # A pin, not "latest" — an empty or placeholder value would be a silent
    # regression the equality above cannot see.
    assert local_dev["cinna_cli_version"], "cinna_cli_version must not be empty"
    assert local_dev["mutagen_version"], "mutagen_version must not be empty"

    # ── Phase 4: Derived from the backend origin, not FRONTEND_HOST ───────
    authorize = data["authorization_endpoint"]
    api_root, sep, _ = authorize.partition(f"{settings.API_V1_STR}/desktop-auth")
    assert sep, f"authorization_endpoint has an unexpected shape: {authorize!r}"
    api_root += settings.API_V1_STR
    assert local_dev["setup_token_endpoint"] == f"{api_root}/cli/account/setup-tokens", (
        "setup_token_endpoint must share the backend origin + API_V1_STR prefix "
        f"with authorization_endpoint ({authorize!r})"
    )

    # ── Phase 5: Split host — the backend origin is the one that wins ─────
    monkeypatch.setattr(settings, "BACKEND_BASE_URL", "https://api.split-host.test")
    monkeypatch.setattr(settings, "FRONTEND_HOST", "https://app.split-host.test/")

    r = client.get(_DISCOVERY)
    assert r.status_code == 200, r.text
    split = r.json()
    setup_endpoint = split["local_dev"]["setup_token_endpoint"]
    assert setup_endpoint == (
        f"https://api.split-host.test{settings.API_V1_STR}/cli/account/setup-tokens"
    ), f"setup_token_endpoint must follow the backend origin, got {setup_endpoint!r}"
    assert _origin(setup_endpoint) == _origin(split["authorization_endpoint"])
    assert "app.split-host.test" not in setup_endpoint, (
        "setup_token_endpoint must not be built from FRONTEND_HOST — the SPA "
        "origin has no /api/v1 on a split-host deployment"
    )


# ── Scenario 2: DESKTOP_LOCAL_DEV_ENABLED off → the block is absent ──────────


def test_discovery_omits_local_dev_when_local_dev_is_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    DESKTOP_LOCAL_DEV_ENABLED=False:
      1. ``local_dev`` is absent from the payload (not present-and-empty)
      2. Desktop auth itself is unaffected — the six legacy keys stand

    This test doubles as the proof that patching ``settings`` reaches the route:
    the field defaults to True, so a no-op patch leaves the block present and
    Phase 1 fails.
    """
    monkeypatch.setattr(settings, "DESKTOP_LOCAL_DEV_ENABLED", False)

    r = client.get(_DISCOVERY)
    assert r.status_code == 200, r.text
    data = r.json()

    # ── Phase 1: The key is gone, not emptied ─────────────────────────────
    assert "local_dev" not in data, (
        f"local_dev must be absent when DESKTOP_LOCAL_DEV_ENABLED is off, got {data}"
    )

    # ── Phase 2: Desktop auth discovery is unchanged ──────────────────────
    _assert_legacy_payload_intact(data, desktop_auth_enabled=True)


# ── Scenario 3: DESKTOP_AUTH_ENABLED off → the block is absent ───────────────


def test_discovery_omits_local_dev_when_desktop_auth_is_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    DESKTOP_AUTH_ENABLED=False:
      1. ``local_dev`` is absent even though DESKTOP_LOCAL_DEV_ENABLED is on —
         the local-dev gate is applied *in addition to* the auth gate, never
         instead of it
      2. The six legacy keys stand, with desktop_auth_enabled now False
    """
    monkeypatch.setattr(settings, "DESKTOP_AUTH_ENABLED", False)
    # Explicitly on, so the assertion below is about the AND, not about both
    # gates happening to be off.
    monkeypatch.setattr(settings, "DESKTOP_LOCAL_DEV_ENABLED", True)

    r = client.get(_DISCOVERY)
    assert r.status_code == 200, r.text
    data = r.json()

    # ── Phase 1: Gated on both flags ──────────────────────────────────────
    assert "local_dev" not in data, (
        f"local_dev must be absent when DESKTOP_AUTH_ENABLED is off, got {data}"
    )

    # ── Phase 2: Legacy payload intact, auth flag reflects the setting ────
    _assert_legacy_payload_intact(data, desktop_auth_enabled=False)


# ── Scenario 4: no drift between discovery and sync-runtime ─────────────────


def test_discovery_cli_version_matches_sync_runtime(
    client: TestClient,
    db,
    tmp_path_factory,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Single-source guarantee for the cinna-cli pin:
      1. Discovery advertises local_dev.cinna_cli_version
      2. A CLI token for an agent reads GET /cli/agents/{id}/sync-runtime
      3. Both serve the same value, from the same setting — they cannot drift
      4. mutagen_version is single-sourced the same way

    The environment/background stubs are entered as explicit context managers
    rather than as a conftest, so the rest of ``tests/api/desktop_auth/``
    (which needs none of them) is unaffected.
    """
    # ── Phase 1: Discovery side ───────────────────────────────────────────
    r = client.get(_DISCOVERY)
    assert r.status_code == 200, r.text
    local_dev = r.json()["local_dev"]

    # ── Phase 2: sync-runtime side (agent-scoped CLI token) ───────────────
    with (
        patched_create_sessions(db, CREATE_SESSION_TARGETS_AGENT),
        patched_background_tasks(BACKGROUND_TASK_TARGETS_FULL),
        patched_external_services(mock_ai_functions=True, mock_a2a_skills=True),
    ):
        setup_environment_adapter(tmp_path_factory)
        try:
            # Agent creation validates that the owner has a default AI
            # credential for the conversation SDK.
            create_default_ai_credential(client, superuser_token_headers)
            agent = create_agent_via_api(client, superuser_token_headers)
            setup_token = create_setup_token(
                client, superuser_token_headers, agent["id"]
            )
            cli_jwt = exchange_setup_token(client, setup_token["token"])["cli_token"]

            r = client.get(
                f"{settings.API_V1_STR}/cli/agents/{agent['id']}/sync-runtime",
                headers=cli_auth_headers(cli_jwt),
            )
        finally:
            teardown_environment_adapter()

    assert r.status_code == 200, r.text
    runtime = r.json()

    # ── Phase 3: The pins agree ───────────────────────────────────────────
    assert "cinna_cli_version" in runtime, (
        f"sync-runtime must serve cinna_cli_version so `cinna doctor` can "
        f"compare against the discovery pin, got {runtime}"
    )
    assert runtime["cinna_cli_version"] == local_dev["cinna_cli_version"], (
        "discovery and sync-runtime have drifted apart on the cinna-cli pin: "
        f"{local_dev['cinna_cli_version']!r} vs {runtime['cinna_cli_version']!r}"
    )
    assert runtime["cinna_cli_version"] == settings.CINNA_CLI_VERSION

    # ── Phase 4: Same guarantee for the Mutagen pin ───────────────────────
    assert runtime["mutagen_version"] == local_dev["mutagen_version"]
    assert runtime["mutagen_version"] == settings.MUTAGEN_VERSION
