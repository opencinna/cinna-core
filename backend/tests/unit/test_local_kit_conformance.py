"""Local Agent Kit contract — `kit.py` against the conformance set.

`docs/local_agent_kit/conformance/manifests/*.json` pairs a manifest with the
field paths a validator must report for it (contract 1.5.0). Cinna Desktop's
validator runs the same set, which is what keeps the two hosts from disagreeing
about a manifest silently. The file format and the matching rule are stated in
`conformance/README.md`; this module applies that rule to `kit.py`'s
manifest-level validation (`validate_manifest`), the same entry point
`kit.py validate` uses before it looks at the folder.

A finding's paths are the backticked tokens in its message that are shaped like
a field path, with array indices dropped — `handovers[0].target_kind` reports
`handovers.target_kind`.

Kit location follows `test_local_kit_tool.py`: `$LOCAL_AGENT_KIT_DIR`, then the
repo checkout, then the synced snapshot. Name which copy a run exercised.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest


def _find_kit_dir() -> Path | None:
    override = os.environ.get("LOCAL_AGENT_KIT_DIR")
    if override:
        candidate = Path(override)
        return candidate if (candidate / "kit.json").is_file() else None
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "docs" / "local_agent_kit"
        if (candidate / "kit.json").is_file():
            return candidate
    snapshot = (
        here.parents[2]
        / "app"
        / "env-templates"
        / "platform-knowledge-env"
        / "app"
        / "workspace"
        / "knowledge"
        / "local-kit"
    )
    return snapshot if (snapshot / "kit.json").is_file() else None


KIT_DIR = _find_kit_dir()
if KIT_DIR is None:
    pytest.skip("Local Agent Kit source not found.", allow_module_level=True)

CASES_DIR = KIT_DIR / "conformance" / "manifests"
if not CASES_DIR.is_dir():
    # The synced snapshot may predate the conformance set; a repo checkout never does.
    pytest.skip(f"no conformance set in the kit at {KIT_DIR}", allow_module_level=True)

CASE_FILES = sorted(CASES_DIR.glob("*.json"))

BACKTICKED_RE = re.compile(r"`([^`]+)`")
FIELD_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[\d+\])?(\.[A-Za-z_][A-Za-z0-9_]*(\[\d+\])?)*$")
INDEX_RE = re.compile(r"\[\d+\]")


def reported_paths(messages: list[str]) -> set[str]:
    """The matching rule's paths: backticked field-path tokens, indices dropped."""
    paths: set[str] = set()
    for message in messages:
        for token in BACKTICKED_RE.findall(message):
            if FIELD_PATH_RE.match(token):
                paths.add(INDEX_RE.sub("", token))
    return paths


@pytest.fixture(scope="module")
def kit_module():
    spec = importlib.util.spec_from_file_location("cinna_local_kit_conformance", KIT_DIR / "tools" / "kit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_conformance_set_is_not_empty() -> None:
    # A glob that matched nothing would pass every parametrised case vacuously.
    assert len(CASE_FILES) >= 10, [p.name for p in CASE_FILES]


@pytest.mark.parametrize("case_file", CASE_FILES, ids=[p.stem for p in CASE_FILES])
def test_kit_py_satisfies_the_conformance_case(kit_module, case_file: Path) -> None:
    case = json.loads(case_file.read_text(encoding="utf-8"))
    assert set(case) <= {"description", "manifest", "expect"}, case_file.name
    assert isinstance(case.get("description"), str) and case["description"], case_file.name
    expect = case["expect"]
    assert set(expect) <= {"errors", "warnings", "no_warnings"}, case_file.name

    report = kit_module.Report()
    kit_module.validate_manifest(case["manifest"], report)

    # Every finding must name its field, or the matching rule cannot see it.
    for message in report.errors + report.warnings:
        assert reported_paths([message]), f"finding names no field path: {message}"

    errors = reported_paths(report.errors)
    warnings = reported_paths(report.warnings)
    detail = f"errors={report.errors}\nwarnings={report.warnings}"

    if "errors" in expect:
        assert errors == set(expect["errors"]), detail
    for path in expect.get("warnings", []):
        assert path in warnings, f"missing warning for {path}\n{detail}"
    for path in expect.get("no_warnings", []):
        assert path not in warnings, f"unexpected warning for {path}\n{detail}"


def test_the_matching_rule_drops_indices_and_ignores_non_paths() -> None:
    assert reported_paths(["x: `handovers[0].target_kind` must be a string."]) == {"handovers.target_kind"}
    assert reported_paths(["`runtime.engine` 'x' is unknown; see `kit.py validate`."]) == {"runtime.engine"}
