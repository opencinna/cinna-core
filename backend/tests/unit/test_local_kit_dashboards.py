"""Local Agent Kit dashboard components (contract 1.19.0, 1.19.1) - pure unit tests.

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
    assert len(CASES) >= 33


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



@pytest.mark.skipif(not POSIX, reason="a shell script stands in for node")
def test_cinna_node_is_run_with_the_script_as_its_only_argument(tmp_path: Path) -> None:
    """CINNA_NODE may be a shim, not a real node: never `--version`, a flag or a probe."""
    agent = _node_endpoint_agent(tmp_path)
    fake_node = tmp_path / "fake-node"
    calls = tmp_path / "fake-node-calls"
    fake_node.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$#:$*\" >> '{calls}'\n"
        "printf '{\"items\": []}'\n",
        encoding="utf-8",
    )
    fake_node.chmod(0o755)
    env = {**os.environ, "CINNA_NODE": str(fake_node), "PATH": str(tmp_path / "empty-bin")}

    problems = _check_env(agent / COMPONENT, agent, env)

    assert [p for p in problems if p["severity"] == "error"] == []
    assert calls.read_text(encoding="utf-8").splitlines() == [f"1:{COMPONENT}/api/items.mjs"]


# ── web-view content scans (1.19.1) ───────────────────────────────────────

SCAN_CODES = {"root_relative_path", "base_tag", "fetch_endpoint", "sandbox_unsupported",
              "network_reference"}


def _html_agent(tmp_path: Path, files: dict[str, str]) -> Path:
    agent = _agent_from_valid_list(tmp_path)
    _edit_component(agent, view="html")
    page = {"index.html": '<!doctype html><html><body><script src="./app.js"></script></body></html>\n',
            "app.js": "console.log(1);\n"}
    page.update(files)
    for name, text in page.items():
        target = agent / COMPONENT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return agent


def _scan_findings(agent: Path) -> set[tuple]:
    _, problems = _check(agent / COMPONENT, agent)
    return {(p["path"], p["code"], p.get("field"), p["severity"])
            for p in problems if p["code"] in SCAN_CODES}


def test_content_scans_report_each_trigger_with_its_field(tmp_path: Path) -> None:
    agent = _html_agent(tmp_path, {
        "index.html": (
            '<!doctype html><html><head><base href="./"><link rel="stylesheet" href="/s.css">'
            '<script src="/app.js"></script></head><body><iframe></iframe></body></html>\n'
        ),
        "app.js": (
            'import { x } from "/lib.mjs";\n'
            'fetch("api/items.py");\n'
            'const xhr = new XMLHttpRequest(); xhr.open("GET", "./api/items.py");\n'
            'eval("1"); new Function("return 1"); new Worker("w.js");\n'
            'WebAssembly.instantiate; localStorage.x; sessionStorage.x; indexedDB.open("d");\n'
            'document.cookie; window.open("https://example.com");\n'
            'fetch("api/other.py");\n'
            'fetch("/data.json");\n'
        ),
        "style.css": 'body { background: url("/bg.png"); }\n',
    })
    html, js, css = (f"{COMPONENT}/{n}" for n in ("index.html", "app.js", "style.css"))

    found = _scan_findings(agent)

    assert found == {
        (html, "base_tag", None, "error"),
        (html, "root_relative_path", "href", "error"),
        (html, "root_relative_path", "src", "error"),
        (html, "sandbox_unsupported", "iframe", "warning"),
        (js, "root_relative_path", "import", "error"),
        (js, "root_relative_path", "fetch", "error"),
        (js, "fetch_endpoint", "fetch", "error"),
        (js, "fetch_endpoint", "XMLHttpRequest", "error"),
        *((js, "sandbox_unsupported", field, "warning") for field in (
            "eval", "Function", "Worker", "WebAssembly", "localStorage", "sessionStorage",
            "indexedDB", "document.cookie", "window.open")),
        (css, "root_relative_path", "url", "error"),
    }


def test_content_scans_skip_lookalikes(tmp_path: Path) -> None:
    agent = _html_agent(tmp_path, {
        "index.html": (
            '<!doctype html><html><head><basefont size="3"><link rel="stylesheet" href="./s.css">'
            '<script src="app.js"></script></head><body><a href="#top">top</a></body></html>\n'
        ),
        "app.js": (
            "function retrieval(x) { return x; } retrieval(1); evaluate(2); myEval(3);\n"
            "const myLocalStorage = {}; const indexedDBName = 'x'; const u = new URL('/x', base);\n"
            'fetch("./data.json"); fetch("apis/x"); xhr.open("GET", "./data.json");\n'
            "const re = /api/; const half = a / b; obj.window.openDoor();\n"
            "var src = /^data:/; var href = /^#/; const isLink = (href) => /x/.test(href);\n"
        ),
        "s.css": 'body { background: url("./bg.png"); }\n',
    })

    assert _scan_findings(agent) == set()


def test_a_protocol_relative_url_is_network_reference_only(tmp_path: Path) -> None:
    agent = _html_agent(tmp_path, {
        "index.html": '<html><head><script src="//cdn.example.com/x.js"></script></head></html>\n',
    })

    assert _scan_findings(agent) == {
        (f"{COMPONENT}/index.html", "network_reference", None, "warning"),
    }


def test_content_scans_skip_api_and_non_html_views(tmp_path: Path) -> None:
    agent = _html_agent(tmp_path, {"api/helper.js": "eval('x'); localStorage.y;\n"})
    assert _scan_findings(agent) == set()

    listed = _agent_from_valid_list(tmp_path / "list")
    (listed / COMPONENT / "notes.js").write_text("eval('x');\n", encoding="utf-8")
    assert _scan_findings(listed) == set()


# ── the reference bridge (protocols/dashboards-bridge.js, 1.19.1) ─────────

BRIDGE_JS = KIT_DIR / "protocols" / "dashboards-bridge.js"


@pytest.mark.skipif(not BRIDGE_JS.is_file(), reason="kit has no reference bridge (pre-1.19.1)")
def test_the_reference_bridge_is_plain_sandbox_safe_and_host_agnostic(tmp_path: Path) -> None:
    import re

    text = BRIDGE_JS.read_text(encoding="utf-8")

    if HAVE_NODE:
        checked = subprocess.run(["node", "--check", str(BRIDGE_JS)], capture_output=True,
                                 text=True, timeout=60, check=False)
        assert checked.returncode == 0, checked.stderr
    assert not re.search(r"(?<![\w$])eval\s*\(", text)
    assert not re.search(r"\bnew\s+Function\b", text)
    assert not re.search(r"setTimeout\s*\(\s*[\"'`]", text)
    assert not re.search(r"https?://|wss?://|[\"'`]//", text)
    assert not re.search(r"\b(?:import|export)\b\s*[{*\"'\w]", text)  # a plain script
    assert not re.search(
        r"(?i)electron|desktop|ipcRenderer|preload|cinna-core|cinna_core|workflow-runner|\bcore\b",
        text,
    )
    for method in ("ui/initialize", "ui/notifications/initialized", "tools/call",
                   "cinna/params-changed", "ui/update-model-context", "ui/message",
                   "ui/open-link", "ui/notifications/tool-result",
                   "ui/notifications/host-context-changed"):
        assert f'"{method}"' in text, method

    # A host may copy it into a frame next to the page: the page scans must stay quiet.
    agent = _html_agent(tmp_path, {
        "index.html": ('<!doctype html><html><body><script src="./bridge.js"></script>'
                       '<script src="./app.js"></script></body></html>\n'),
        "bridge.js": text,
    })
    assert _scan_findings(agent) == set()


_BRIDGE_HARNESS = r"""
const listeners = [];
const sent = [];
const timers = [];
const warnings = [];
global.setTimeout = (fn, ms) => { timers.push(fn); return timers.length; };
global.clearTimeout = () => {};
console.warn = (...args) => warnings.push(args.join(" "));
const parent = { postMessage: (m) => sent.push(JSON.parse(JSON.stringify(m))) };
global.window = { parent, addEventListener: (name, fn) => listeners.push(fn) };
global.document = { documentElement: { style: { setProperty() {} }, setAttribute() {} } };
require(process.argv[2]);
const fire = (data) => listeners.forEach((fn) => fn({ source: parent, data }));
(async () => {
  window.cinna.ask("dropped");          // times out while still queued: never posted
  timers[0]();
  window.cinna.setContext({ summary: "kept" });
  fire({ jsonrpc: "2.0", id: 1, result: { hostContext: { theme: "light" } } });  // no nonce
  await window.cinna.ready;
  window.cinna.setParams({ month: "2026-11" });
  fire({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed",
         params: { theme: "dark", _meta: { cinna: { nonce: "n2" } } } });
  window.cinna.setParams({ month: "2026-12" });
  fire({ jsonrpc: "2.0", id: 77, method: "unknown/thing" });
  process.stdout.write(JSON.stringify({ sent, warnings }));
})();
"""


@pytest.mark.skipif(not (BRIDGE_JS.is_file() and HAVE_NODE), reason="needs node and the bridge")
def test_the_bridge_proceeds_without_a_nonce_and_drops_timed_out_queued_requests(
    tmp_path: Path,
) -> None:
    harness = tmp_path / "harness.js"
    harness.write_text(_BRIDGE_HARNESS, encoding="utf-8")
    result = subprocess.run(["node", str(harness), str(BRIDGE_JS)], capture_output=True,
                            text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    sent = out["sent"]

    methods = [m.get("method") for m in sent]
    assert methods[:4] == ["ui/initialize", "ui/notifications/initialized",
                           "ui/update-model-context", "cinna/params-changed"]
    assert "ui/message" not in methods
    assert len(out["warnings"]) == 1
    for message in sent[1:4]:
        assert "cinna" not in message["params"]["_meta"]
    assert sent[4]["params"]["_meta"]["cinna"] == {"nonce": "n2"}
    assert sent[5]["id"] == 77
    assert sent[5]["error"]["data"]["_meta"]["cinna"] == {"nonce": "n2"}


# ── tile tools and highlight (contract 1.20.0) ────────────────────────────

TOOLS_SCHEMA = KIT_DIR / "protocols" / "dashboards-tools.schema.json"
DASHBOARDS_SCHEMA = KIT_DIR / "protocols" / "dashboards.schema.json"
_needs_tools = pytest.mark.skipif(not TOOLS_SCHEMA.is_file(), reason="kit has no tile tools (pre-1.20.0)")


def _tools_doc() -> dict:
    return json.loads(TOOLS_SCHEMA.read_text(encoding="utf-8"))


def _tool_validator(schema: dict):
    """A validator for one tool's inputSchema/result, resolving `#/$defs/...` in the file."""
    jsonschema = pytest.importorskip("jsonschema")
    doc = _tools_doc()
    wrapped = {"$schema": doc["$schema"], "$defs": doc["$defs"], **schema}
    jsonschema.Draft202012Validator.check_schema(wrapped)
    return jsonschema.Draft202012Validator(wrapped)


@_needs_tools
def test_the_tile_tools_schema_is_a_cinna_binding_like_artifacts() -> None:
    doc = _tools_doc()
    assert doc["server"] == "cinna"
    assert doc["protocol_version"] == 1
    assert set(doc["tools"]) == {"dashboards_tiles", "dashboards_update_tile"}
    for name, spec in doc["tools"].items():
        assert set(spec) == {"description", "inputSchema", "result"}, name
        schema = spec["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False, name
        # Engines refuse a combinator at the top of a tool's input schema.
        assert not {"anyOf", "oneOf", "allOf", "not", "if"} & set(schema), name
        # The descriptions say where the tools exist, as artifacts_publish does.
        assert "Cinna Desktop" in spec["description"] and "cloud" in spec["description"], name
        _tool_validator(schema)
        _tool_validator(spec["result"])
    codes = doc["$defs"]["error"]["properties"]["error"]["properties"]["code"]["enum"]
    assert set(codes) == {"not_found", "invalid_input", "rate_limited", "no_dashboard"}
    assert set(doc["errors"]) - {"description"} == set(codes)


@_needs_tools
def test_the_tile_tools_accept_and_answer_the_documented_shapes() -> None:
    tools = _tools_doc()["tools"]
    tiles_result = _tool_validator(tools["dashboards_tiles"]["result"])
    update_input = _tool_validator(tools["dashboards_update_tile"]["inputSchema"])
    update_result = _tool_validator(tools["dashboards_update_tile"]["result"])
    error = _tool_validator(_tools_doc()["$defs"]["error"])

    assert not list(_tool_validator(tools["dashboards_tiles"]["inputSchema"]).iter_errors({}))
    assert not list(tiles_result.iter_errors({
        "dashboard": {"id": "d1", "name": "Sales"},
        "tiles": [{
            "tile_id": "t1", "component": "deals", "skill": "crm", "title": "Deals pipeline",
            "view": "list", "params": {"stage": "open", "month": "2026-11"},
            "param_specs": {"stage": {"type": "enum", "values": ["open", "won"], "default": "open"}},
            "can_highlight": True, "selection": ["d-12"], "visible": ["d-12", "d-14"],
        }],
    }))
    assert list(tiles_result.iter_errors({"dashboard": {"id": "d1", "name": "x"},
                                          "tiles": [{"tile_id": "t1"}]}))

    assert not list(update_input.iter_errors({"tile_id": "t1", "params": {"stage": "won", "month": None}}))
    assert not list(update_input.iter_errors({"tile_id": "t1", "highlight": []}))
    for bad in ({"params": {}}, {"tile_id": "t1", "params": {"x": [1]}},
                {"tile_id": "t1", "highlight": [""]}, {"tile_id": "t1", "extra": 1}):
        assert list(update_input.iter_errors(bad)), bad

    assert not list(update_result.iter_errors(
        {"tile_id": "t1", "params": {"stage": "won"}, "recomputing": True, "highlighted": False}))
    assert not list(error.iter_errors(
        {"error": {"code": "rate_limited", "message": "Try again in 20 s.", "retry_after_s": 20}}))
    assert list(error.iter_errors({"error": {"code": "forbidden", "message": "no"}}))


@_needs_tools
def test_the_kit_bundle_exports_the_tile_tools() -> None:
    from app.services.cli.local_agent_kit_service import LocalAgentKitService

    kit = json.loads((KIT_DIR / "kit.json").read_text(encoding="utf-8"))
    assert kit["protocols"]["dashboards_tools"] == "protocols/dashboards-tools.schema.json"
    for rel in kit["protocols"].values():
        assert (KIT_DIR / rel).is_file(), rel
        assert LocalAgentKitService._is_contract_member(rel), rel
    snapshot = (Path(__file__).resolve().parents[2] / "app" / "env-templates" / "platform-knowledge-env"
                / "app" / "workspace" / "knowledge" / "local-kit")
    if snapshot.is_dir():
        for rel in ("protocols/dashboards-tools.schema.json", "protocols/dashboards-bridge.js",
                    "protocols/dashboards.schema.json", "kit.json"):
            assert (snapshot / rel).read_bytes() == (KIT_DIR / rel).read_bytes(), (
                f"{rel} differs from docs/local_agent_kit — run `make sync-platform-knowledge`")


@_needs_tools
def test_x_bridge_documents_highlight_and_collect_context() -> None:
    bridge = json.loads(DASHBOARDS_SCHEMA.read_text(encoding="utf-8"))["x-web-view"]["bridge"]
    host = bridge["host_contract"]
    assert "cinna/highlight" in bridge["cinna.onHighlight(cb)"]
    assert "cinna/highlight-ready" in host["notifications_sent"]
    assert "cinna/highlight" in host["notifications_received"]
    assert set(host["cinna/collect-context"]) == {"description", "params", "result"}
    text = BRIDGE_JS.read_text(encoding="utf-8")
    for method in ("cinna/highlight", "cinna/highlight-ready", "cinna/collect-context"):
        assert f'"{method}"' in text, method
    assert "onHighlight:" in text


_HIGHLIGHT_HARNESS = r"""
const listeners = [];
const sent = [];
const parent = { postMessage: (m) => sent.push(JSON.parse(JSON.stringify(m))) };
global.setTimeout = () => 0;
global.clearTimeout = () => {};
console.warn = () => {};

// A minimal DOM: two microdata rows, one on screen and touched by the text selection.
function node(tag, attrs, text, kids) {
  const n = { tagName: tag.toUpperCase(), attrs, textContent: text || "", children: kids || [],
              parentElement: null, rect: { width: 10, height: 10, top: 0, left: 0, bottom: 10, right: 10 } };
  n.children.forEach((c) => { c.parentElement = n; });
  n.getAttribute = (k) => (k in n.attrs ? n.attrs[k] : null);
  n.hasAttribute = (k) => k in n.attrs;
  n.getBoundingClientRect = () => n.rect;
  n.closest = (sel) => {
    for (let at = n; at; at = at.parentElement) if (sel === "[itemscope]" && at.hasAttribute("itemscope")) return at;
    return null;
  };
  n.querySelectorAll = (sel) => {
    const key = sel.slice(1, -1), out = [];
    const walk = (x) => x.children.forEach((c) => { if (c.hasAttribute(key)) out.push(c); walk(c); });
    walk(n);
    return out;
  };
  return n;
}
const rowA = node("li", { itemscope: "", itemid: "d-12", itemtype: "Deal" }, "", [
  node("span", { itemprop: "name" }, "  Acme \n renewal "),
  node("data", { itemprop: "value", value: "1200" }, "1,200 EUR"),
]);
const rowB = node("li", { itemscope: "", itemid: "d-14" }, "", [node("span", { itemprop: "name" }, "Globex")]);
rowB.rect = { width: 10, height: 10, top: 900, left: 0, bottom: 910, right: 10 };
const body = node("body", {}, "", [rowA, rowB]);
global.document = {
  title: "Deals", documentElement: { style: { setProperty() {} }, setAttribute() {}, clientHeight: 600, clientWidth: 800 },
  querySelectorAll: (sel) => body.querySelectorAll(sel),
};
global.window = {
  parent, innerHeight: 600, innerWidth: 800,
  addEventListener: (name, fn) => listeners.push(fn),
  getSelection: () => ({ isCollapsed: false, toString: () => "Acme   renewal", containsNode: (n) => n === rowA }),
};
require(process.argv[2]);
const fire = (data) => listeners.forEach((fn) => fn({ source: parent, data }));
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  const seen = { early: [], late: [] };
  fire({ jsonrpc: "2.0", method: "cinna/highlight", params: { ids: ["w1"] } });   // before any listener
  fire({ jsonrpc: "2.0", method: "cinna/highlight", params: { ids: ["w2", 3, ""] } });  // last one wins
  window.cinna.onHighlight((ids) => seen.early.push(ids));   // before the handshake: ready is queued
  window.cinna.onHighlight(() => {});                         // a second listener sends nothing more
  fire({ jsonrpc: "2.0", id: 1, result: { _meta: { cinna: { nonce: "n1" } }, hostContext: { theme: "light" } } });
  await window.cinna.ready;
  await tick();
  fire({ jsonrpc: "2.0", method: "cinna/highlight", params: { ids: "w9" } });  // not a list: ignored
  fire({ jsonrpc: "2.0", method: "cinna/highlight", params: { ids: [] } });    // clears
  const off = window.cinna.onHighlight((ids) => seen.late.push(ids));
  await tick();
  off();
  fire({ jsonrpc: "2.0", method: "cinna/highlight", params: { ids: ["w4"] } });
  fire({ jsonrpc: "2.0", id: 50, method: "cinna/collect-context", params: {} });
  window.cinna.setContext({ summary: "Two deals", selection: ["d-14"], visible: ["d-12", "d-14"] });
  fire({ jsonrpc: "2.0", id: 51, method: "cinna/collect-context", params: {} });
  process.stdout.write(JSON.stringify({ sent, seen }));
})();
"""


@pytest.mark.skipif(not (TOOLS_SCHEMA.is_file() and HAVE_NODE), reason="needs node and 1.20.0")
def test_the_bridge_replays_highlights_signals_once_and_collects_context(tmp_path: Path) -> None:
    harness = tmp_path / "harness.js"
    harness.write_text(_HIGHLIGHT_HARNESS, encoding="utf-8")
    result = subprocess.run(["node", str(harness), str(BRIDGE_JS)], capture_output=True,
                            text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    sent, seen = out["sent"], out["seen"]

    # The page signals once, after the handshake, with the nonce.
    methods = [m.get("method") for m in sent]
    assert methods[:3] == ["ui/initialize", "ui/notifications/initialized", "cinna/highlight-ready"]
    assert methods.count("cinna/highlight-ready") == 1
    assert sent[2]["params"]["_meta"]["cinna"] == {"nonce": "n1"}
    assert "id" not in sent[2]

    # Early highlight replayed (last wins, bad ids dropped); a non-list is ignored; [] clears;
    # a late listener gets the last value; an unsubscribed one gets nothing more.
    assert seen["early"] == [["w2"], [], ["w4"]]
    assert seen["late"] == [[]]

    replies = {m["id"]: m for m in sent if m.get("id") in (50, 51)}
    collected = replies[50]["result"]
    assert collected["_meta"]["cinna"] == {"nonce": "n1"}
    assert collected["source"] == "collected"
    assert collected["summary"] == "Deals"
    assert collected["selected_text"] == "Acme renewal"
    assert collected["selection"] == ["d-12"]
    assert collected["visible"] == ["d-12"]
    assert collected["microdata"] == [
        {"id": "d-12", "type": "Deal", "properties": {"name": "Acme renewal", "value": "1200"}},
        {"id": "d-14", "properties": {"name": "Globex"}},
    ]
    from_page = replies[51]["result"]
    assert from_page["source"] == "set_context"
    assert from_page["summary"] == "Two deals"
    assert from_page["selection"] == ["d-14"]
    assert from_page["microdata"] == collected["microdata"]
