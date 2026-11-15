"""Only ``EnvironmentService`` may construct ``EnvironmentLifecycleManager``.

Every other path goes through ``EnvironmentService.get_lifecycle_manager()`` so
they share one manager (one ``_allocated_ports`` set) and tests can swap the
adapter in one place. A direct construction elsewhere is invisible to the
``patch_environment_adapter`` fixture.
"""
from __future__ import annotations

import ast
import pathlib

from tests.architecture._source_index import parse_source, python_files

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
ALLOWED = {pathlib.Path("services/environments/environment_service.py")}


def test_environment_lifecycle_manager_is_constructed_only_by_environment_service() -> None:
    offenders = []
    for path in python_files(APP, exclude=("alembic", "env-templates")):
        if path.relative_to(APP) in ALLOWED:
            continue
        for node in ast.walk(parse_source(path)):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if name == "EnvironmentLifecycleManager":
                    offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert not offenders, f"construct via get_lifecycle_manager(): {offenders}"
