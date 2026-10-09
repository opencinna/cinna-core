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


# ── Contract 1.8.0: shared agents ──────────────────────────────────────────────


def _load(rel: str):
    return json.loads((KIT_DIR / rel).read_text(encoding="utf-8"))


def test_contract_version_1_8_0_is_coherent_across_the_four_carriers() -> None:
    # LocalAgentKitService 503s the contract when these disagree.
    file_version = (KIT_DIR / "CONTRACT_VERSION").read_text(encoding="utf-8").strip()
    assert file_version == "1.8.0"
    assert _load("kit.json")["contract_version"] == file_version
    assert _load("layout.json")["contract_version"] == file_version
    changelog = (KIT_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {file_version} — shared agents" in changelog
    # newest entry first: no higher-numbered heading precedes it
    first_heading = re.search(r"^## (\d+\.\d+\.\d+)", changelog, re.MULTILINE)
    assert first_heading is not None and first_heading.group(1) == file_version


def test_layout_declares_the_shared_remote_section() -> None:
    layout = _load("layout.json")
    remote = layout["shared_remote"]
    assert remote["tree"] == "agent_folder"
    assert remote["default_branch"] == "main"
    assert remote["one_branch"] is True
    never = remote["never_travels"]
    assert never["secret_rule"] == "secret_files"
    assert set(never["roles"]) == {"credentials", "runtime_data"}
    # the paths are derived from those two roles, not free text
    roles = {r["role"]: r["path"] for r in layout["agent"]["roles"]}
    assert set(never["paths"]) == {roles["credentials"] + "/", roles["runtime_data"] + "/"}
    local = remote["local_state"]
    assert local["file"] == "app-data/desktop.json"
    assert local["key"] == "shared_remote"
    assert local["fields"] == ["kind", "repo_id", "clone_url"]
    assert local["kind_values"] == ["cinna", "external"]
    # local state lives under a never-travelling path, so it can't leak into the tree
    assert any(local["file"].startswith(p) for p in never["paths"])
    # and the cloud import excludes already drop both never-travelling paths
    excludes = layout["cloud_import_excludes"]
    assert "app-data/" in excludes and "credentials/" in excludes


def test_workshop_has_a_marketplaces_role_that_survives_update_and_is_ignored() -> None:
    roles = _load("layout.json")["workshop"]["roles"]
    entry = next((r for r in roles if r["path"] == "Marketplaces"), None)
    assert entry is not None
    assert entry["kind"] == "directory"
    assert entry["role"] == "marketplaces"
    assert entry["survives_update"] is True
    ignore = (KIT_DIR / "templates" / "root" / "gitignore").read_text(encoding="utf-8")
    assert "Marketplaces/" in ignore.splitlines()


def test_kit_json_lists_the_marketplace_schema_and_it_exists() -> None:
    kit = _load("kit.json")
    assert kit["marketplace_schema"] == "schema/cinna-marketplace.schema.json"
    assert (KIT_DIR / kit["marketplace_schema"]).is_file()


def test_agent_schema_registers_the_core_only_bundle_block() -> None:
    bundle = _load("schema/cinna-agent.schema.json")["properties"]["bundle"]
    assert bundle["type"] == "object"
    assert bundle["x-scope"] == "host:cinna-core"
    assert bundle["x-import"] == "ignored"


def _marketplace_validator():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _load("schema/cinna-marketplace.schema.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


_CLAUDE_SHAPED_MARKETPLACE = {
    "name": "acme-tools",
    "owner": {"name": "Acme", "email": "tools@acme.example"},
    "description": "Acme's plugins",
    "version": "1.0.0",
    "metadata": {"pluginRoot": "./plugins"},
    "renames": {"old-name": "new-name", "removed": None},
    "plugins": [
        {"name": "local", "source": "./plugins/local", "description": "d", "version": "1.2.3"},
        {"name": "gh", "source": {"source": "github", "repo": "acme/gh-plugin", "ref": "v1"}},
        {"name": "u", "source": {"source": "url", "url": "https://git.example/x.git"}},
        {
            "name": "sub",
            "source": {"source": "git-subdir", "url": "https://git.example/m.git", "path": "a/b"},
            "dependencies": ["gh", {"name": "u"}],
            "metadata": {"cinna": {"kind": "agent", "minContract": "1.8.0"}},
        },
    ],
}


def test_marketplace_schema_accepts_a_claude_shaped_sample() -> None:
    validator = _marketplace_validator()
    errors = [e.message for e in validator.iter_errors(_CLAUDE_SHAPED_MARKETPLACE)]
    assert errors == []


def test_marketplace_schema_accepts_unknown_keys_at_every_level() -> None:
    import copy

    validator = _marketplace_validator()
    doc = copy.deepcopy(_CLAUDE_SHAPED_MARKETPLACE)
    doc["x-top"] = 1
    doc["owner"]["x-owner"] = 1
    doc["metadata"]["x-meta"] = 1
    doc["plugins"][0]["x-entry"] = {"anything": True}
    doc["plugins"][1]["source"]["x-src"] = 1
    doc["plugins"][3]["metadata"]["cinna"]["x-cinna"] = 1
    assert list(validator.iter_errors(doc)) == []


@pytest.mark.parametrize(
    "mutate, why",
    [
        (lambda d: d.pop("name"), "name required"),
        (lambda d: d.pop("owner"), "owner required"),
        (lambda d: d.pop("plugins"), "plugins required"),
        (lambda d: d.update(name="-bad"), "name pattern"),
        (lambda d: d.update(name="has space"), "name pattern"),
        (lambda d: d["owner"].pop("name"), "owner.name required"),
        (lambda d: d["plugins"][0].pop("source"), "entry source required"),
        (lambda d: d["plugins"][0].update(source="plugins/no-dot-slash"), "relative needs ./"),
        (lambda d: d["plugins"][0].update(source="./a/../../etc"), "no .. segments"),
        (lambda d: d["plugins"][1]["source"].update(repo="not-a-repo-slug"), "github repo shape"),
        (lambda d: d["plugins"][3]["metadata"]["cinna"].update(kind="widget"), "kind enum"),
        (lambda d: d["plugins"][3]["metadata"]["cinna"].update(minContract="one"), "minContract semver"),
    ],
)
def test_marketplace_schema_rejects_structural_violations(mutate, why) -> None:
    import copy

    validator = _marketplace_validator()
    doc = copy.deepcopy(_CLAUDE_SHAPED_MARKETPLACE)
    mutate(doc)
    assert list(validator.iter_errors(doc)), why
