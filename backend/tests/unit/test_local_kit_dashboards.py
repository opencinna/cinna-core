"""Local Agent Kit dashboard components (contract 1.19.0) - pure unit tests.

Runs the kit's `skills/cinna-dashboards/scripts/check_component.py` over every
`conformance/dashboards/<case>/` and compares the findings as
(path, code, field) triples, as conformance/README.md specifies; checks the shipped
templates pass `--run`; and checks `kit.py dashboards check` / `kit.py validate`.

`docs/` is not mounted in the backend container: these tests skip there unless
`LOCAL_AGENT_KIT_DIR` points at a kit with the dashboards checker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _find_kit_dir() -> Path | None:
    override = os.environ.get("LOCAL_AGENT_KIT_DIR")
    if override:
        candidate = Path(override)
        return candidate if (candidate / "kit.json").is_file() else None
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "local_agent_kit"
        if (candidate / "kit.json").is_file():
            return candidate
    return None


KIT_DIR = _find_kit_dir()
if KIT_DIR is None:
    pytest.skip("local agent kit not available", allow_module_level=True)

KIT_PY = KIT_DIR / "tools" / "kit.py"
CHECK_PY = KIT_DIR / "skills" / "cinna-dashboards" / "scripts" / "check_component.py"
CASES_DIR = KIT_DIR / "conformance" / "dashboards"
if not CHECK_PY.is_file() or not CASES_DIR.is_dir():
    pytest.skip("kit has no dashboards checker (pre-1.19.0)", allow_module_level=True)

CASES = sorted(p.name for p in CASES_DIR.iterdir() if (p / "case.json").is_file())
HAVE_NODE = shutil.which("node") is not None


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *cmd], capture_output=True, text=True, timeout=120,
        check=False, cwd=cwd,
    )


def _check(component: Path, agent: Path, *, run: bool = False) -> tuple[int, list[dict]]:
    args = [str(CHECK_PY), str(component), "--agent", str(agent), "--json"]
    if run:
        args.append("--run")
    result = _run(args)
    assert result.stdout.strip(), result.stderr
    return result.returncode, json.loads(result.stdout)["problems"]


def _triple(problem: dict) -> tuple:
    return (problem["path"], problem["code"], problem.get("field"))


def _matches(problem: dict, wanted: dict) -> bool:
    return all(problem.get(key) == value for key, value in wanted.items())


def test_the_conformance_set_is_not_empty() -> None:
    assert len(CASES) >= 28


@pytest.mark.parametrize("case", CASES)
def test_conformance_case(case: str) -> None:
    case_dir = CASES_DIR / case
    spec = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    run = bool(spec["run"])
    if run and not HAVE_NODE and case == "valid-html":
        pytest.skip("node is not installed (the html case has a Node endpoint)")
    _, problems = _check(case_dir / spec["component"], case_dir, run=run)
    expect = spec["expect"]

    if "errors" in expect:
        got = {_triple(p) for p in problems if p["severity"] == "error"}
        want = {(e["path"], e["code"], e.get("field")) for e in expect["errors"]}
        assert got == want
    warnings = [p for p in problems if p["severity"] == "warning"]
    for wanted in expect.get("warnings", []):
        assert any(_matches(w, wanted) for w in warnings), (wanted, warnings)
    for banned in expect.get("no_warnings", []):
        assert not any(_matches(w, banned) for w in warnings), (banned, warnings)


# ── shipped templates ─────────────────────────────────────────────────────


def _scaffold(tmp_path: Path, slug: str = "dash-agent") -> Path:
    result = _run([str(KIT_PY), "new", slug, "--root", str(tmp_path)])
    assert result.returncode == 0, result.stdout + result.stderr
    created = [d for d in tmp_path.rglob(slug) if (d / "cinna-agent.json").is_file()]
    assert len(created) == 1, created
    return created[0]


@pytest.mark.parametrize("template", ["dashboard-list", "dashboard-html"])
def test_shipped_template_passes_the_check(tmp_path: Path, template: str) -> None:
    agent = _scaffold(tmp_path)
    component = agent / "dashboards" / "sample"
    shutil.copytree(KIT_DIR / "templates" / template, component)
    run = template != "dashboard-html" or HAVE_NODE
    code, problems = _check(component, agent, run=run)
    assert [p for p in problems if p["severity"] == "error"] == []
    assert code == 0


# ── kit.py dashboards check / validate ────────────────────────────────────


def _break_component(component: Path) -> None:
    manifest = json.loads((component / "component.json").read_text(encoding="utf-8"))
    for endpoint in manifest.get("endpoints", {}).values():
        endpoint["script"] = "api/does_not_exist.py"
    (component / "component.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_kit_dashboards_check_reports_a_broken_component(tmp_path: Path) -> None:
    agent = _scaffold(tmp_path)
    component = agent / "dashboards" / "sample"
    shutil.copytree(KIT_DIR / "templates" / "dashboard-list", component)
    ok = _run([str(KIT_PY), "dashboards", "check", str(component), "--agent", str(agent)])
    assert ok.returncode == 0, ok.stdout + ok.stderr

    _break_component(component)
    bad = _run([str(KIT_PY), "dashboards", "check", str(component), "--agent", str(agent), "--json"])
    assert bad.returncode == 1
    problems = json.loads(bad.stdout)["problems"]
    assert {p["code"] for p in problems} == {"script_missing"}


@pytest.mark.parametrize("where", ["dashboards/sample", "skills/triage/dashboards/sample"])
def test_kit_validate_flags_a_broken_component(tmp_path: Path, where: str) -> None:
    agent = _scaffold(tmp_path)
    if where.startswith("skills/"):
        skill = agent / "skills" / "triage"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: triage\ndescription: Triage the inbox.\n---\n\nBody.\n", encoding="utf-8"
        )
    component = agent / where
    shutil.copytree(KIT_DIR / "templates" / "dashboard-list", component)

    clean = _run([str(KIT_PY), "validate", str(agent)])
    assert "script_missing" not in clean.stdout and "does not exist" not in clean.stdout

    _break_component(component)
    result = _run([str(KIT_PY), "validate", str(agent)])
    assert result.returncode != 0, result.stdout
    assert f"{where}/component.json" in result.stdout + result.stderr


def test_kit_validate_flags_a_credential_missing_from_the_manifest(tmp_path: Path) -> None:
    agent = _scaffold(tmp_path)
    component = agent / "dashboards" / "sample"
    shutil.copytree(KIT_DIR / "templates" / "dashboard-list", component)
    path = component / "component.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["requires_credentials"] = ["undeclared-credential"]
    path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run([str(KIT_PY), "validate", str(agent)])
    assert result.returncode != 0, result.stdout
    assert "undeclared-credential" in result.stdout + result.stderr

    code, problems = _check(component, agent)
    assert code == 1
    assert ("dashboards/sample/component.json", "credential_unknown", "requires_credentials") in {
        _triple(p) for p in problems
    }


# ── containment, run limits and runtimes (W2, W3, W4) ─────────────────────

POSIX = os.name == "posix"
COMPONENT = "dashboards/follow-ups"
COMPONENT_JSON = f"{COMPONENT}/component.json"


def _agent_from_valid_list(tmp_path: Path) -> Path:
    agent = tmp_path / "agent"
    shutil.copytree(CASES_DIR / "valid-list", agent)
    (agent / "case.json").unlink()
    return agent


def _edit_component(agent: Path, **changes: object) -> None:
    path = agent / COMPONENT_JSON
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.update(changes)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _write_endpoint(agent: Path, body: str, timeout_s: int | None = None) -> None:
    (agent / COMPONENT / "api" / "items.py").write_text(body, encoding="utf-8")
    spec: dict = {"script": "api/items.py"}
    if timeout_s is not None:
        spec["timeout_s"] = timeout_s
    _edit_component(agent, endpoints={"items": spec})


def _check_env(component: Path, agent: Path, env: dict, *, run: bool = True) -> list[dict]:
    args = [sys.executable, str(CHECK_PY), str(component), "--agent", str(agent), "--json"]
    if run:
        args.append("--run")
    result = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False, env=env)
    assert result.stdout.strip(), result.stderr
    return json.loads(result.stdout)["problems"]


@pytest.mark.skipif(not POSIX, reason="symlinks need POSIX here")
def test_a_symlinked_api_folder_escapes_and_is_never_run(tmp_path: Path) -> None:
    agent = _agent_from_valid_list(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = tmp_path / "ran.txt"
    (outside / "items.py").write_text(
        f"open({str(marker)!r}, 'w').write('ran')\nprint('{{\"items\": []}}')\n", encoding="utf-8"
    )
    shutil.rmtree(agent / COMPONENT / "api")
    (agent / COMPONENT / "api").symlink_to(outside, target_is_directory=True)

    code, problems = _check(agent / COMPONENT, agent, run=True)

    assert code == 1
    assert (COMPONENT_JSON, "symlink_escape", "endpoints.items.script") in {_triple(p) for p in problems}
    assert not marker.exists()


@pytest.mark.skipif(not POSIX, reason="symlinks need POSIX here")
def test_a_symlinked_entry_escapes(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    shutil.copytree(CASES_DIR / "valid-html", agent)
    (agent / "case.json").unlink()
    component = next(p.parent for p in (agent / "dashboards").glob("*/component.json"))
    entry = json.loads((component / "component.json").read_text(encoding="utf-8")).get("entry", "index.html")
    outside = tmp_path / "page.html"
    outside.write_text("<!doctype html><p>outside</p>", encoding="utf-8")
    (component / entry).unlink()
    (component / entry).symlink_to(outside)

    _, problems = _check(component, agent)

    assert ("symlink_escape", "entry") in {(p["code"], p["field"]) for p in problems}


@pytest.mark.skipif(not POSIX, reason="symlinks need POSIX here")
def test_a_source_file_under_a_symlinked_files_folder_escapes(tmp_path: Path) -> None:
    agent = _agent_from_valid_list(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "items.json").write_text('{"items": []}', encoding="utf-8")
    (agent / "files").symlink_to(outside, target_is_directory=True)
    _edit_component(agent, source={"file": "files/items.json"}, endpoints={})

    code, problems = _check(agent / COMPONENT, agent, run=True)

    assert code == 1
    assert (COMPONENT_JSON, "symlink_escape", "source.file") in {_triple(p) for p in problems}


@pytest.mark.skipif(not POSIX, reason="symlinks need POSIX here")
def test_a_symlinked_component_folder_is_refused(tmp_path: Path) -> None:
    agent = _agent_from_valid_list(tmp_path)
    outside = tmp_path / "outside-component"
    shutil.move(str(agent / COMPONENT), outside)
    (agent / COMPONENT).symlink_to(outside, target_is_directory=True)

    whole = _run([str(CHECK_PY), str(agent), "--json"])
    direct_code, direct = _check(agent / COMPONENT, agent)

    whole_problems = json.loads(whole.stdout)["problems"]
    assert whole.returncode == 1
    assert {_triple(p) for p in whole_problems} == {(COMPONENT, "component_symlink", None)}
    assert direct_code == 1
    assert {_triple(p) for p in direct} == {(COMPONENT, "component_symlink", None)}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(not POSIX, reason="process groups are POSIX")
def test_a_timeout_kills_what_the_endpoint_started(tmp_path: Path) -> None:
    import time

    agent = _agent_from_valid_list(tmp_path)
    pid_file = tmp_path / "grandchild.pid"
    _write_endpoint(agent, (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    ), timeout_s=1)

    started = time.monotonic()
    code, problems = _check(agent / COMPONENT, agent, run=True)
    elapsed = time.monotonic() - started

    assert code == 1
    assert any(p["code"] == "run_failed" and "within 1s" in p["message"] for p in problems), problems
    assert elapsed < 60
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while _pid_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _pid_alive(grandchild)


def test_the_stdout_cap_stops_reading(tmp_path: Path) -> None:
    import time

    agent = _agent_from_valid_list(tmp_path)
    _write_endpoint(agent, (
        "import sys\n"
        "chunk = 'x' * 65536\n"
        "while True:\n"
        "    sys.stdout.write(chunk)\n"
    ), timeout_s=120)

    started = time.monotonic()
    code, problems = _check(agent / COMPONENT, agent, run=True)

    assert code == 1
    assert any(p["code"] == "run_output_invalid" and "5 MB" in p["message"] for p in problems), problems
    assert time.monotonic() - started < 60


def test_a_failed_run_keeps_the_stderr_tail(tmp_path: Path) -> None:
    agent = _agent_from_valid_list(tmp_path)
    _write_endpoint(agent, (
        "import sys\n"
        "sys.stderr.write('a' * 5000 + 'THE-END')\n"
        "sys.exit(3)\n"
    ))

    _, problems = _check(agent / COMPONENT, agent, run=True)

    failed = [p for p in problems if p["code"] == "run_failed"]
    assert len(failed) == 1
    assert failed[0]["message"].endswith("THE-END")
    assert "exited with 3" in failed[0]["message"]
    assert len(failed[0]["message"]) < 2100


def _node_endpoint_agent(tmp_path: Path) -> Path:
    agent = _agent_from_valid_list(tmp_path)
    (agent / COMPONENT / "api" / "items.mjs").write_text(
        "process.stdout.write(JSON.stringify({items: []}));\n", encoding="utf-8"
    )
    _edit_component(agent, endpoints={"items": {"script": "api/items.mjs"}})
    return agent


def test_no_node_is_a_warning_and_the_run_is_skipped(tmp_path: Path) -> None:
    agent = _node_endpoint_agent(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "CINNA_NODE"}
    env["PATH"] = str(tmp_path / "empty-bin")

    problems = _check_env(agent / COMPONENT, agent, env)

    assert [p for p in problems if p["severity"] == "error"] == []
    assert any(p["code"] == "runtime_missing" and p["severity"] == "warning" for p in problems)


@pytest.mark.skipif(not POSIX, reason="a shell script stands in for node")
def test_cinna_node_is_used_before_path(tmp_path: Path) -> None:
    agent = _node_endpoint_agent(tmp_path)
    fake_node = tmp_path / "fake-node"
    marker = tmp_path / "fake-node-ran"
    fake_node.write_text(f"#!/bin/sh\n: > '{marker}'\nprintf '{{\"items\": []}}'\n", encoding="utf-8")
    fake_node.chmod(0o755)
    env = {**os.environ, "CINNA_NODE": str(fake_node), "PATH": str(tmp_path / "empty-bin")}

    problems = _check_env(agent / COMPONENT, agent, env)

    assert marker.exists()
    assert not any(p["code"] == "runtime_missing" for p in problems)
    assert [p for p in problems if p["severity"] == "error"] == []
