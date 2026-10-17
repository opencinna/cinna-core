"""
Platform-owned protocol files installed into every agent environment's core.

The Cinna Task Protocol and the artifacts tool are authored once, in
cinna-core's ``docs/local_agent_kit/`` (``protocols/tasks.schema.json`` with
the ``skills/cinna-tasks/`` skill, ``protocols/artifacts.schema.json`` with the
``skills/cinna-artifacts/`` skill). ``docs/`` is not in the backend image; the kit
reaches it as the synced snapshot under the platform-knowledge-env template
(``.cinna-core-kit/scripts/sync_platform_knowledge.py``). Whenever an
environment's ``app/core`` is laid down — at creation and at every rebuild —
these files are copied from that snapshot into it, so the environment carries
them with no second hand-edited copy in ``app_core_base``:

  /app/core/protocols/tasks.schema.json   — read by the `cinna` MCP server's tools
  /app/core/skills/cinna-tasks/           — the skill, for projection into the engine
  /app/core/protocols/artifacts.schema.json — read by the `cinna` server's artifacts_publish
  /app/core/skills/cinna-artifacts/       — its skill

Paths are relative to the environment's core directory and mirrored by
``core/server/tools/cinna_tasks_protocol.py`` (SCHEMA_RELATIVE_PATH /
SKILL_RELATIVE_PATH) and ``cinna_artifacts_protocol.py`` (the same names).
"""
import logging
import shutil
from pathlib import Path

from app.services.cli.platform_knowledge_assets import (
    KNOWLEDGE_TEMPLATE_NAME,
    LOCAL_KIT_SUBDIR,
)

logger = logging.getLogger(__name__)

# Kit-relative paths, installed at the same relative path under app/core.
PROTOCOL_FILES: tuple[str, ...] = (
    "protocols/tasks.schema.json",
    "skills/cinna-tasks",
    "protocols/artifacts.schema.json",
    "skills/cinna-artifacts",
)


SCHEMA_FILE = "protocols/tasks.schema.json"


def protocol_installed(instance_core_dir: Path) -> bool:
    """Whether the `cinna` tools' schema is present in an environment's core.

    The tools load it at runtime; without it the environment has no working
    task protocol, so its ``task_protocol_version`` must not be raised.
    """
    return (instance_core_dir / SCHEMA_FILE).is_file()


def kit_snapshot_dir(templates_dir: Path) -> Path:
    """The synced Local Agent Kit inside the env-templates tree."""
    return (
        templates_dir / KNOWLEDGE_TEMPLATE_NAME / "app" / "workspace" / "knowledge" / LOCAL_KIT_SUBDIR
    )


def install_platform_protocol(instance_core_dir: Path, templates_dir: Path) -> list[str]:
    """Copy the protocol files into an environment's core dir; returns what was installed.

    Replaces any earlier copy wholesale, so a file dropped from the kit does not
    linger in a rebuilt environment. A missing source is logged, not raised: an
    environment without the files still runs, its task tools just report that
    the schema is unavailable.
    """
    kit_dir = kit_snapshot_dir(templates_dir)
    installed: list[str] = []
    for relative in PROTOCOL_FILES:
        source = kit_dir / relative
        target = instance_core_dir / relative
        if not source.exists():
            logger.warning("Platform protocol file missing from the kit snapshot: %s", source)
            continue
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
        else:
            shutil.copy2(source, target)
        installed.append(relative)
    return installed
