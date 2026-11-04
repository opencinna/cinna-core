"""
Unit tests for env-core's dashboards routes (``core/server/dashboards_routes.py``),
the ``/dashboards/`` access-log filter in ``core/main.py`` and the
``webapp_data_api`` error shapes after its move to the shared runner.

The router is mounted on a bare FastAPI app (no auth) with the module-level
hooks monkeypatched to a temporary workspace.

Run:
    cd backend && python -m pytest tests/unit/test_env_dashboards_routes.py -q
"""

import json
import logging
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# sys.path to app_core_base is set by tests/unit/conftest.py
from core.server import dashboards_routes as dr
from core.server import routes as env_routes
from core.server.endpoint_runner import RunResult

CHECKER = (
    Path(__file__).parents[2]
    / "app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit"
    / "skills/cinna-dashboards/scripts/check_component.py"
)

ECHO = "import sys, json\nprint(json.dumps({'echo': json.load(sys.stdin)}))\n"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(dr, "WORKSPACE_DIR", root)
    monkeypatch.setattr(dr, "CHECK_COMPONENT_PATH", CHECKER)
    monkeypatch.setattr(dr, "resolve_skill_dirs", lambda workspace: [])
    return root


@pytest.fixture
def client(ws):
    app = FastAPI()
    app.include_router(dr.router)
    return TestClient(app)


def make_component(owner: Path, name: str = "sales", doc: dict | None = None, files: dict | None = None) -> Path:
    folder = owner / "dashboards" / name
    folder.mkdir(parents=True)
    if doc is None:
        doc = {"schema_version": 1, "view": "page", "title": "Sales"}
    (folder / "component.json").write_text(json.dumps(doc))
    for rel, content in (files or {}).items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return folder


class TestListing:
    def test_lists_agent_component(self, client, ws):
        make_component(ws, files={"index.html": "<p>hi</p>", "api/data.py": "x" * 10})
        body = client.post("/dashboards/components", json={}).json()
        [c] = body["components"]
        assert c["name"] == "sales" and c["skill"] is None
        assert c["component_json"]["title"] == "Sales"
        assert c["size_bytes"] > 10
        assert isinstance(c["findings"], list)

    def test_skill_component_listed_with_findings(self, client, ws, monkeypatch):
        skill = ws / "skills" / "my-skill"
        make_component(skill, "tile", doc={"schema_version": 1, "view": "nope", "title": "T"})
        monkeypatch.setattr(dr, "resolve_skill_dirs", lambda workspace: [("my-skill", skill)])
        comps = client.post("/dashboards/components", json={}).json()["components"]
        [c] = comps
        assert (c["name"], c["skill"]) == ("tile", "my-skill")
        codes = {f["code"] for f in c["findings"]}
        assert "invalid_value" in codes
        assert set(c["findings"][0]) == {"path", "code", "field", "severity", "message"}

    def test_invalid_json_component_has_null_component_json(self, client, ws):
        folder = make_component(ws)
        (folder / "component.json").write_text("{nope")
        [c] = client.post("/dashboards/components", json={}).json()["components"]
        assert c["component_json"] is None

    def test_no_dashboards_folder_lists_nothing(self, client):
        assert client.post("/dashboards/components", json={}).json() == {"components": []}

    def test_symlinked_component_folder_not_listed(self, client, ws, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "component.json").write_text("{}")
        (ws / "dashboards").mkdir()
        (ws / "dashboards" / "evil").symlink_to(outside)
        assert client.post("/dashboards/components", json={}).json()["components"] == []

    def test_responses_are_no_store(self, client):
        assert client.post("/dashboards/components", json={}).headers["cache-control"] == "no-store"


class TestFileContainment:
    @pytest.mark.parametrize(
        "path",
        ["api/data.py", "api/x", "../secret", "a/../b", "./index.html", ".hidden", "a/.x", "/etc/passwd",
         "a//b", "a\\b", "", "C:/x"],
    )
    def test_refused_paths(self, client, ws, path):
        make_component(ws, files={"index.html": "ok", "api/data.py": "x", "b": "b"})
        (ws / "secret").write_text("s")
        r = client.post("/dashboards/file", json={"component": "sales", "path": path})
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "invalid_input" and r.json()["ok"] is False

    def test_serves_file_with_content_type(self, client, ws):
        make_component(ws, files={"index.html": "<p>hi</p>", "css/a.css": "a{}"})
        r = client.post("/dashboards/file", json={"component": "sales", "path": "index.html"})
        assert r.status_code == 200 and r.content == b"<p>hi</p>"
        assert r.headers["content-type"].startswith("text/html")
        assert client.post("/dashboards/file",
                           json={"component": "sales", "path": "css/a.css"}).headers["content-type"].startswith("text/css")

    def test_api_as_non_first_segment_is_allowed(self, client, ws):
        make_component(ws, files={"pages/api/x.txt": "ok"})
        r = client.post("/dashboards/file", json={"component": "sales", "path": "pages/api/x.txt"})
        assert r.status_code == 200

    def test_symlink_escape_refused(self, client, ws, tmp_path):
        folder = make_component(ws)
        secret = tmp_path / "secret.txt"
        secret.write_text("top secret")
        (folder / "link.txt").symlink_to(secret)
        r = client.post("/dashboards/file", json={"component": "sales", "path": "link.txt"})
        assert r.status_code == 400 and r.json()["code"] == "invalid_input"
        assert b"top secret" not in r.content

    def test_symlinked_directory_escape_refused(self, client, ws, tmp_path):
        folder = make_component(ws)
        outside = tmp_path / "outdir"
        outside.mkdir()
        (outside / "f.txt").write_text("leak")
        (folder / "d").symlink_to(outside)
        r = client.post("/dashboards/file", json={"component": "sales", "path": "d/f.txt"})
        assert r.status_code == 400

    def test_symlink_to_api_folder_refused(self, client, ws):
        folder = make_component(ws, files={"api/data.py": "SECRET_SOURCE"})
        (folder / "www").symlink_to(folder / "api")
        r = client.post("/dashboards/file", json={"component": "sales", "path": "www/data.py"})
        assert r.status_code == 400 and b"SECRET_SOURCE" not in r.content

    def test_symlinked_dashboards_folder_refused(self, client, ws, tmp_path):
        real = tmp_path / "elsewhere"
        make_component(real, files={"index.html": "x"})
        (ws / "dashboards").symlink_to(real / "dashboards")
        r = client.post("/dashboards/file", json={"component": "sales", "path": "index.html"})
        assert r.status_code in (400, 404)
        assert client.post("/dashboards/components", json={}).json()["components"] == []

    def test_missing_file_and_component_are_not_found(self, client, ws):
        make_component(ws)
        r = client.post("/dashboards/file", json={"component": "sales", "path": "nope.html"})
        assert r.status_code == 404 and r.json()["code"] == "not_found"
        r = client.post("/dashboards/file", json={"component": "ghost", "path": "a.html"})
        assert r.status_code == 404
        r = client.post("/dashboards/file", json={"component": "sales", "skill": "ghost", "path": "a.html"})
        assert r.status_code == 404

    def test_bad_component_name_is_invalid_input(self, client, ws):
        r = client.post("/dashboards/file", json={"component": "../x", "path": "a.html"})
        assert r.status_code == 400

    def test_skill_component_file_served(self, client, ws, monkeypatch):
        skill = ws / "skills" / "s1"
        make_component(skill, "tile", files={"index.html": "skill page"})
        monkeypatch.setattr(dr, "resolve_skill_dirs", lambda workspace: [("s1", skill)])
        r = client.post("/dashboards/file", json={"component": "tile", "skill": "s1", "path": "index.html"})
        assert r.status_code == 200 and r.content == b"skill page"

    def test_malformed_body_is_422(self, client):
        assert client.post("/dashboards/file", json={"component": "x"}).status_code == 422

    def test_get_is_not_allowed(self, client):
        assert client.get("/dashboards/components").status_code == 405


class TestRun:
    def _doc(self, **endpoint):
        return {"schema_version": 1, "view": "page", "title": "T",
                "endpoints": {"data": {"script": "api/data.py", **endpoint}}}

    def test_runs_declared_endpoint_with_params(self, client, ws):
        make_component(ws, doc=self._doc(), files={"api/data.py": ECHO})
        r = client.post("/dashboards/run", json={"component": "sales", "endpoint": "data", "params": {"a": 1}})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "value": {"echo": {"a": 1}}}

    def test_runs_with_agent_root_as_cwd(self, client, ws):
        make_component(ws, doc=self._doc(),
                       files={"api/data.py": "import os, json\nprint(json.dumps(os.getcwd()))\n"})
        v = client.post("/dashboards/run", json={"component": "sales", "endpoint": "data"}).json()["value"]
        assert Path(v).resolve() == ws.resolve()

    def test_undeclared_endpoint_is_not_found_even_if_script_exists(self, client, ws):
        make_component(ws, doc=self._doc(), files={"api/data.py": ECHO, "api/other.py": ECHO})
        r = client.post("/dashboards/run", json={"component": "sales", "endpoint": "other"})
        assert r.status_code == 200
        assert r.json()["ok"] is False and r.json()["code"] == "not_found"

    def test_unknown_component_is_not_found(self, client, ws):
        r = client.post("/dashboards/run", json={"component": "ghost", "endpoint": "data"})
        assert r.json()["code"] == "not_found"

    def test_script_escaping_component_is_invalid(self, client, ws, tmp_path):
        doc = self._doc()
        doc["endpoints"]["data"]["script"] = "../x.py"
        make_component(ws, doc=doc)
        r = client.post("/dashboards/run", json={"component": "sales", "endpoint": "data"})
        assert r.json()["code"] == "invalid_input"

    def test_symlinked_script_escape_is_invalid(self, client, ws, tmp_path):
        folder = make_component(ws, doc=self._doc())
        outside = tmp_path / "evil.py"
        outside.write_text("print(1)\n")
        (folder / "api").mkdir()
        (folder / "api" / "data.py").symlink_to(outside)
        r = client.post("/dashboards/run", json={"component": "sales", "endpoint": "data"})
        assert r.json()["code"] == "invalid_input"

    def test_ts_script_is_invalid_input(self, client, ws):
        doc = self._doc()
        doc["endpoints"]["data"]["script"] = "api/data.ts"
        make_component(ws, doc=doc, files={"api/data.ts": "x"})
        r = client.post("/dashboards/run", json={"component": "sales", "endpoint": "data"})
        assert r.json()["code"] == "invalid_input"

    def test_failed_script_is_call_failed_with_stderr_tail(self, client, ws):
        make_component(ws, doc=self._doc(),
                       files={"api/data.py": "import sys\nsys.stderr.write('kaboom')\nsys.exit(2)\n"})
        j = client.post("/dashboards/run", json={"component": "sales", "endpoint": "data"}).json()
        assert j["ok"] is False and j["code"] == "call_failed"
        assert "2" in j["message"] and j["stderr_tail"] == "kaboom"

    def test_bad_json_is_call_failed(self, client, ws):
        make_component(ws, doc=self._doc(), files={"api/data.py": "print('nope')\n"})
        j = client.post("/dashboards/run", json={"component": "sales", "endpoint": "data"}).json()
        assert j["code"] == "call_failed" and "JSON" in j["message"]

    def test_body_timeout_wins_and_is_clamped_small(self, client, ws):
        make_component(ws, doc=self._doc(timeout_s=30),
                       files={"api/data.py": "import time\ntime.sleep(30)\n"})
        j = client.post("/dashboards/run",
                        json={"component": "sales", "endpoint": "data", "timeout": 1}).json()
        assert j["code"] == "call_failed" and "1s" in j["message"]

    def test_endpoint_timeout_used_when_body_has_none(self, client, ws):
        make_component(ws, doc=self._doc(timeout_s=1),
                       files={"api/data.py": "import time\ntime.sleep(30)\n"})
        j = client.post("/dashboards/run", json={"component": "sales", "endpoint": "data"}).json()
        assert j["code"] == "call_failed" and "1s" in j["message"]


class TestSourceFile:
    def _doc(self, file):
        return {"schema_version": 1, "view": "page", "title": "T", "source": {"file": file}}

    def test_serves_app_data_and_files(self, client, ws):
        make_component(ws, doc=self._doc("app-data/report.md"))
        (ws / "app-data").mkdir()
        (ws / "app-data" / "report.md").write_text("# hi")
        r = client.post("/dashboards/source-file", json={"component": "sales"})
        assert r.status_code == 200 and r.content == b"# hi"
        assert r.headers["content-type"].startswith("text/markdown")

        make_component(ws, "other", doc=self._doc("files/d.csv"))
        (ws / "files").mkdir()
        (ws / "files" / "d.csv").write_text("a,b")
        r = client.post("/dashboards/source-file", json={"component": "other"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")

    @pytest.mark.parametrize("value", ["docs/x.md", "app-data", "app-data/../docs/x.md", "/app-data/x",
                                       "dashboards/sales/component.json", "app-data/.hidden"])
    def test_prefix_rule_and_traversal_refused(self, client, ws, value):
        make_component(ws, doc=self._doc(value))
        (ws / "docs").mkdir()
        (ws / "docs" / "x.md").write_text("no")
        r = client.post("/dashboards/source-file", json={"component": "sales"})
        assert r.status_code == 400 and r.json()["code"] == "invalid_input"

    def test_symlink_out_of_workspace_refused(self, client, ws, tmp_path):
        make_component(ws, doc=self._doc("files/leak.md"))
        (ws / "files").mkdir()
        outside = tmp_path / "leak.md"
        outside.write_text("leak")
        (ws / "files" / "leak.md").symlink_to(outside)
        r = client.post("/dashboards/source-file", json={"component": "sales"})
        assert r.status_code == 400 and b"leak" not in r.content.replace(b"leak.md", b"")

    def test_source_root_symlinked_elsewhere_in_workspace_refused(self, client, ws):
        make_component(ws, doc=self._doc("files/x.md"))
        (ws / "docs").mkdir()
        (ws / "docs" / "x.md").write_text("private")
        (ws / "files").symlink_to(ws / "docs")
        r = client.post("/dashboards/source-file", json={"component": "sales"})
        assert r.status_code == 400 and r.content != b"private"

    def test_missing_source_file_is_404_and_no_source_is_400(self, client, ws):
        make_component(ws, doc=self._doc("files/none.md"))
        assert client.post("/dashboards/source-file", json={"component": "sales"}).status_code == 404
        make_component(ws, "bare")
        assert client.post("/dashboards/source-file", json={"component": "bare"}).status_code == 400


class TestAccessLogFilter:
    def _record(self, path):
        return logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:5", "POST", path, "1.1", 200), None,
        )

    def test_filter_drops_dashboards_paths_only(self):
        from core.main import _QuietPathsAccessFilter

        f = _QuietPathsAccessFilter()
        assert f.filter(self._record("/dashboards/run")) is False
        assert f.filter(self._record("/dashboards/file")) is False
        assert f.filter(self._record("/webapp/api/x")) is True
        assert f.filter(self._record("/health")) is True

    def test_filter_tolerates_odd_records(self):
        from core.main import _QuietPathsAccessFilter

        rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "plain", None, None)
        assert _QuietPathsAccessFilter().filter(rec) is True

    def test_filter_is_installed_on_uvicorn_access(self):
        import core.main  # noqa: F401

        names = [type(f).__name__ for f in logging.getLogger("uvicorn.access").filters]
        assert "_QuietPathsAccessFilter" in names


class TestWebappDataApi:
    @pytest.fixture
    def web(self, tmp_path, monkeypatch):
        webapp = tmp_path / "webapp"
        (webapp / "api").mkdir(parents=True)
        (webapp / "api" / "stats.py").write_text("print(1)\n")
        monkeypatch.setattr(env_routes, "WEBAPP_DIR", webapp)
        app = FastAPI()
        app.include_router(env_routes.router)
        app.dependency_overrides[env_routes.verify_auth_token] = lambda: None
        return TestClient(app)

    def _fake(self, monkeypatch, result, seen=None):
        async def fake(script, params, timeout_s=60, cwd=None, use_uv=True):
            if seen is not None:
                seen.update(script=script, params=params, timeout_s=timeout_s, cwd=cwd, use_uv=use_uv)
            return result
        monkeypatch.setattr(env_routes, "run_script", fake)

    def test_success_passes_stdout_through(self, web, monkeypatch, tmp_path):
        seen = {}
        self._fake(monkeypatch, RunResult(True, b'{"a": 1}', 0, "", "ok"), seen)
        r = web.post("/webapp/api/stats", json={"params": {"q": 1}, "timeout": 7})
        assert r.status_code == 200 and r.json() == {"a": 1}
        assert seen["params"] == {"q": 1} and seen["timeout_s"] == 7
        assert seen["cwd"] == tmp_path
        assert seen["use_uv"] is False

    def test_failed_keeps_500_error_and_stderr(self, web, monkeypatch):
        self._fake(monkeypatch, RunResult(False, b"", 4, "trace", "failed"))
        r = web.post("/webapp/api/stats", json={})
        assert r.status_code == 500
        assert r.json() == {"error": "Script exited with code 4", "stderr": "trace"}

    def test_too_large_is_500_with_error_and_stderr(self, web, monkeypatch):
        self._fake(monkeypatch, RunResult(False, b"", None, "e", "too_large"))
        r = web.post("/webapp/api/stats", json={})
        assert r.status_code == 500
        assert r.json() == {"error": "Script output exceeded 5242880 bytes", "stderr": "e"}

    def test_timeout_is_504(self, web, monkeypatch):
        self._fake(monkeypatch, RunResult(False, b"", None, "", "timeout"))
        r = web.post("/webapp/api/stats", json={"timeout": 2})
        assert r.status_code == 504 and r.json() == {"detail": "Script timed out after 2s"}

    def test_non_json_stdout_passes_through(self, web, monkeypatch):
        self._fake(monkeypatch, RunResult(False, b"plain", 0, "", "bad_json"))
        r = web.post("/webapp/api/stats", json={})
        assert r.status_code == 200 and r.content == b"plain"

    def test_unknown_and_invalid_endpoint(self, web):
        assert web.post("/webapp/api/ghost", json={}).status_code == 404
        assert web.post("/webapp/api/a-b", json={}).status_code == 400

    def test_real_runner_end_to_end(self, web, tmp_path):
        (tmp_path / "webapp" / "api" / "real.py").write_text(
            "import sys, json\nprint(json.dumps(json.load(sys.stdin)))\n")
        r = web.post("/webapp/api/real", json={"params": {"k": "v"}})
        assert r.status_code == 200 and r.json() == {"k": "v"}
