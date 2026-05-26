"""
Discovery + app-build for the agent REST API.

Imports every ``*.py`` module under ``/app/workspace/agent_api/`` (importing a
module triggers its ``@api.*`` decorator registration onto the shared
``cinna_api.api`` router), then mounts that router onto a fresh ``FastAPI()``
instance whose ``servers`` entry points at the consumer-facing ``base_url`` (so
consumer-generated clients target the right URL).

The OpenAPI ``title`` / ``description`` / ``version`` are author-controlled: set
the module-level constants ``API_TITLE`` / ``API_DESCRIPTION`` / ``API_VERSION``
in any of your ``agent_api`` modules to label the spec. Absent that, generic
defaults are used. (We deliberately do NOT derive the title from the env name —
that is the env *image* name, not a meaningful API name.)

Escape hatch: if ``agent_api/app.py`` defines its own ``app = FastAPI()``, that
instance takes precedence (advanced authors get full control).

This module is import-safe: building the app imports arbitrary agent code, so a
bad import raises here and is captured by the supervisor as a boot error.
"""
import importlib.util
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Where the producer authors their API.
AGENT_API_DIR = Path(os.getenv("AGENT_API_DIR", "/app/workspace/agent_api"))
# Where the cinna_api SDK package lives (so `import cinna_api` resolves).
SDK_PARENT_DIR = os.getenv("CINNA_API_SDK_PARENT", "/app/core")

# Author-controlled OpenAPI metadata: define any of these as module-level
# constants in an agent_api module to label the spec. First non-empty value
# (in import order) wins per field; missing fields fall back to the defaults.
_META_CONSTANTS = {
    "title": "API_TITLE",
    "description": "API_DESCRIPTION",
    "version": "API_VERSION",
}
_META_DEFAULTS = {
    "title": "Agent API",
    "description": "REST API exposed by this agent.",
    "version": "1.0.0",
}


def _ensure_sdk_importable() -> None:
    """Put ``/app/core`` on sys.path so ``import cinna_api`` resolves."""
    if SDK_PARENT_DIR not in sys.path:
        sys.path.insert(0, SDK_PARENT_DIR)
    # Also make the workspace importable so agent modules can do intra-package
    # relative-ish imports of sibling files via the agent_api package name.
    workspace_parent = str(AGENT_API_DIR.parent)
    if workspace_parent not in sys.path:
        sys.path.insert(0, workspace_parent)


def _iter_module_files() -> list[Path]:
    """Return the importable ``*.py`` files under the agent_api dir, sorted.

    Excludes dunder files and ``app.py`` (handled separately as the explicit
    entrypoint).
    """
    if not AGENT_API_DIR.is_dir():
        return []
    files = []
    for f in sorted(AGENT_API_DIR.glob("*.py")):
        if f.name.startswith("__"):
            continue
        if f.name == "app.py":
            continue
        files.append(f)
    return files


def _import_module_from_path(path: Path):
    """Import a single module file by absolute path under a stable module name.

    Returns the imported module object.
    """
    module_name = f"agent_api_user.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _collect_api_metadata(modules: list) -> dict:
    """Read ``API_TITLE`` / ``API_DESCRIPTION`` / ``API_VERSION`` from the
    imported agent modules, falling back to generic defaults.

    First non-empty string value (in import order) wins per field.
    """
    meta = dict(_META_DEFAULTS)
    found: set[str] = set()
    for module in modules:
        for field, const_name in _META_CONSTANTS.items():
            if field in found:
                continue
            value = getattr(module, const_name, None)
            if isinstance(value, str) and value.strip():
                meta[field] = value.strip()
                found.add(field)
    return meta


def _load_explicit_app():
    """Load ``agent_api/app.py`` and return its ``app`` if it defines a FastAPI()."""
    app_path = AGENT_API_DIR / "app.py"
    if not app_path.is_file():
        return None
    _import_module_from_path(app_path)
    module = sys.modules.get("agent_api_user.app")
    candidate = getattr(module, "app", None) if module else None
    # Duck-typed FastAPI check — avoids a hard isinstance import dependency.
    if candidate is not None and hasattr(candidate, "openapi") and hasattr(candidate, "router"):
        return candidate
    return None


def _dedupe_routes(router) -> int:
    """Drop duplicate routes (same path + methods) from the shared router.

    A non-endpoint file left in ``agent_api/`` (a helper, a test, a Mutagen
    ``*.sync-conflict-*.py`` copy) is still discovered and imported. If it
    imports one of the endpoint modules — directly or transitively — that
    module's ``@api.*`` decorators run a *second* time against this shared
    singleton router, registering every route twice. Left in place the duplicates
    collide on operationId and break ``app.openapi()`` (the spec harvest then
    fails even though the endpoint module itself is correct). We keep the first
    occurrence of each ``(path, methods)`` and drop the rest, so a stray
    re-import degrades to a no-op instead of a boot error.
    """
    seen: set = set()
    deduped: list = []
    removed = 0
    for route in router.routes:
        key = (getattr(route, "path", None), frozenset(getattr(route, "methods", None) or []))
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        deduped.append(route)
    if removed:
        router.routes[:] = deduped
        logger.warning(
            "agent_api: dropped %d duplicate route(s) — a non-endpoint file in "
            "agent_api/ likely re-imported an endpoint module (keep helpers/tests "
            "out of agent_api/, or prefix them with '__')",
            removed,
        )
    return removed


def build_app(base_url: str | None = None):
    """
    Discover the agent's API modules and return a ready-to-serve FastAPI app.

    The OpenAPI title/description/version come from author-defined module
    constants (``API_TITLE`` / ``API_DESCRIPTION`` / ``API_VERSION``) with
    generic defaults; see ``_collect_api_metadata``.

    Args:
        base_url: Consumer-facing base URL written into the OpenAPI ``servers``
                  list so generated clients point at the proxy, not localhost.

    Returns:
        A FastAPI instance.

    Raises:
        ImportError / any exception from agent code — propagated so the
        supervisor can capture it as a boot error.
    """
    from fastapi import FastAPI

    _ensure_sdk_importable()

    # Explicit entrypoint wins — the author controls FastAPI() (title etc.) fully.
    explicit = _load_explicit_app()
    if explicit is not None:
        if base_url:
            explicit.servers = [{"url": base_url}]
        logger.info("agent_api: using explicit app.py entrypoint")
        return explicit

    # Reset the shared router so a second build in the same process (e.g. a
    # reload, or a harvest that re-imports) does not accumulate duplicate routes
    # — that would trip FastAPI's "Duplicate Operation ID" warnings and break
    # the spec. Importing the modules below re-registers them cleanly.
    import cinna_api

    cinna_api.api.routes.clear()

    # Discover decorated modules. Importing each triggers @api.* registration.
    module_files = _iter_module_files()
    modules = []
    for path in module_files:
        logger.info("agent_api: importing module %s", path.name)
        modules.append(_import_module_from_path(path))

    # A stray non-endpoint file may have re-registered an endpoint module's
    # routes; drop the duplicates so the spec harvest succeeds regardless.
    _dedupe_routes(cinna_api.api)

    meta = _collect_api_metadata(modules)

    app = FastAPI(
        title=meta["title"],
        description=meta["description"],
        version=meta["version"],
        servers=[{"url": base_url}] if base_url else None,
    )
    app.include_router(cinna_api.api)
    logger.info(
        "agent_api: built app '%s' with %d route(s) from %d module(s)",
        meta["title"], len(cinna_api.api.routes), len(module_files),
    )
    return app


def has_agent_api() -> bool:
    """True when the producer has authored at least one agent_api module."""
    if not AGENT_API_DIR.is_dir():
        return False
    if (AGENT_API_DIR / "app.py").is_file():
        return True
    return len(_iter_module_files()) > 0
