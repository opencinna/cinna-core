"""Regression test for the bundle-install workspace-seed ordering fix.

Historical bug: ``InstallService._install_from_revision`` seeded the workspace
from the bundle revision snapshot **in the foreground**, immediately after
``EnvironmentService.create_environment`` returned. But ``create_environment``
only spawns a background task; the instance directory and its template
``app/workspace/`` are materialised asynchronously inside
``_create_environment_background`` → ``create_environment_instance``. The
foreground seed therefore raced the build and either:

  * found no instance dir yet (``seed_workspace_from_bundle_snapshot`` no-ops on
    a missing ``dst_root``), or
  * was clobbered by the template materialisation,

shipping installs with empty bundle-owned dirs (e.g. ``scripts/`` containing
only the template README, not the bundle's scripts).

The fix moves the seed INTO the background build: ``create_environment`` now
accepts ``bundle_snapshot_path`` and ``_create_environment_background`` seeds
**after** ``create_environment_instance`` and **before** ``start_environment``.

These MagicMock-driven tests assert that ordering invariant — the only place it
can be checked without a real Docker build (no clean API surface; see the unit
README's "MagicMock-driven defensive-branch tests" allowance).
"""
import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.environments.environment_service import EnvironmentService


def _run_background(*, bundle_snapshot_path, auto_start, call_log):
    """Drive ``_create_environment_background`` with everything mocked.

    ``call_log`` is appended to by the patched collaborators so the test can
    assert the relative order of: instance creation, snapshot seeding, and
    container start.
    """
    env = MagicMock()
    env.status = "stopped"  # so the auto_start branch fires start_environment
    agent = MagicMock()

    session = MagicMock()
    session.get.side_effect = lambda model, _id: env if "Environment" in model.__name__ else agent

    @contextmanager
    def _fake_create_session():
        yield session

    lifecycle = MagicMock()

    async def _create_instance(*args, **kwargs):
        call_log.append("create_instance")

    async def _start(*args, **kwargs):
        call_log.append("start")

    lifecycle.create_environment_instance = AsyncMock(side_effect=_create_instance)
    lifecycle.start_environment = AsyncMock(side_effect=_start)

    def _fake_seed(snapshot_path, env_id):
        call_log.append(("seed", str(snapshot_path)))

    with (
        patch(
            "app.services.environments.environment_service.create_session",
            _fake_create_session,
        ),
        patch.object(
            EnvironmentService, "get_lifecycle_manager", return_value=lifecycle
        ),
        patch(
            "app.services.environments.workspace_copy.seed_workspace_from_bundle_snapshot",
            _fake_seed,
        ),
    ):
        asyncio.run(
            EnvironmentService._create_environment_background(
                MagicMock(name="env_id"),
                MagicMock(name="agent_id"),
                {},  # credential bag
                auto_start,
                None,  # source_environment_id
                bundle_snapshot_path,
            )
        )


def test_seed_runs_after_instance_creation_and_before_start():
    """The snapshot seed lands between instance materialisation and container start."""
    call_log: list = []
    _run_background(
        bundle_snapshot_path="/app/data/bundles/io.test/2",
        auto_start=True,
        call_log=call_log,
    )

    # Reduce to the ordered list of phases (drop the seed payload tuple's path).
    phases = [c[0] if isinstance(c, tuple) else c for c in call_log]

    assert phases == ["create_instance", "seed", "start"], (
        "seed must run AFTER create_environment_instance and BEFORE "
        f"start_environment; got {phases}"
    )

    # The exact snapshot path is forwarded to the seed.
    seed_call = next(c for c in call_log if isinstance(c, tuple) and c[0] == "seed")
    assert seed_call[1] == "/app/data/bundles/io.test/2"


def test_no_seed_when_snapshot_path_absent():
    """Non-install env creation (no snapshot) never calls the seed."""
    call_log: list = []
    _run_background(bundle_snapshot_path=None, auto_start=True, call_log=call_log)

    phases = [c[0] if isinstance(c, tuple) else c for c in call_log]
    assert "seed" not in phases, f"seed must not run without a snapshot path; got {phases}"
    assert phases == ["create_instance", "start"]
