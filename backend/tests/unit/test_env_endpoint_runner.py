"""
Unit tests for env-core's endpoint runner (``core/server/endpoint_runner.py``).

Covers the hardening the dashboards plan (C2 P1) requires: EPIPE tolerance,
the stdout cap, timeout + process-group kill, stderr tail, extension table and
runtime choice. Real subprocesses, small timeouts.

Run:
    cd backend && python -m pytest tests/unit/test_env_endpoint_runner.py -q
"""

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

# sys.path to app_core_base is set by tests/unit/conftest.py
from core.server.endpoint_runner import (
    MAX_STDOUT_BYTES,
    STDERR_TAIL_CHARS,
    UnsupportedScriptError,
    clamp_timeout,
    command_for,
    run_script,
)


def _script(tmp_path: Path, body: str, name: str = "s.py") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def _run(script: Path, params: dict | None = None, timeout_s: int = 5, cwd: Path | None = None):
    return asyncio.run(run_script(script, params or {}, timeout_s=timeout_s, cwd=cwd or script.parent))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed-but-unreaped child shows as a zombie; treat that as dead.
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        return "State:\tZ" not in status
    except OSError:
        return True


def _wait_dead(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


GRANDCHILD = (
    "import subprocess, sys\n"
    "p = subprocess.Popen(['sleep', '60'])\n"
    "open(sys.argv[0] + '.pid', 'w').write(str(p.pid))\n"
)


class TestOutcomes:
    def test_ok_returns_stdout_bytes_and_params_on_stdin(self, tmp_path):
        s = _script(tmp_path, "import sys, json\nd = json.load(sys.stdin)\nprint(json.dumps({'got': d['x']}))\n")
        r = _run(s, {"x": 7})
        assert r.ok and r.reason == "ok" and r.exit_code == 0
        assert r.value_bytes.strip() == b'{"got": 7}'

    def test_nonzero_exit_is_failed(self, tmp_path):
        s = _script(tmp_path, "import sys\nsys.exit(3)\n")
        r = _run(s)
        assert not r.ok and r.reason == "failed" and r.exit_code == 3

    def test_non_json_stdout_is_bad_json(self, tmp_path):
        s = _script(tmp_path, "print('not json')\n")
        r = _run(s)
        assert not r.ok and r.reason == "bad_json" and r.exit_code == 0
        assert r.value_bytes.strip() == b"not json"

    def test_missing_cwd_is_start_error(self, tmp_path):
        s = _script(tmp_path, "print(1)\n")
        r = _run(s, cwd=tmp_path / "nope")
        assert not r.ok and r.reason == "failed" and r.start_error


class TestStdin:
    def test_script_that_never_reads_stdin_is_not_an_error(self, tmp_path):
        s = _script(tmp_path, "import time\ntime.sleep(0.3)\nprint('1')\n")
        big = {"blob": "x" * 2_000_000}
        r = _run(s, big, timeout_s=10)
        assert r.ok, (r.reason, r.stderr_tail)
        assert r.value_bytes.strip() == b"1"

    def test_script_that_closes_stdin_at_once(self, tmp_path):
        s = _script(tmp_path, "import os\nos.close(0)\nprint('2')\n")
        r = _run(s, {"blob": "x" * 2_000_000}, timeout_s=10)
        assert r.ok and r.value_bytes.strip() == b"2"


class TestStdoutCap:
    def test_exact_cap_is_accepted(self, tmp_path):
        # '"' + payload + '"' is one JSON string of exactly MAX_STDOUT_BYTES.
        s = _script(
            tmp_path,
            "import sys\nsys.stdout.write('\"' + 'a' * (%d - 2) + '\"')\n" % MAX_STDOUT_BYTES,
        )
        r = _run(s, timeout_s=20)
        assert r.ok and len(r.value_bytes) == MAX_STDOUT_BYTES

    def test_one_byte_over_is_too_large_and_group_is_killed(self, tmp_path):
        s = _script(
            tmp_path,
            GRANDCHILD
            + "import time\n"
            + "sys.stdout.buffer.write(b'a' * %d)\n" % (MAX_STDOUT_BYTES + 1)
            + "sys.stdout.flush()\n"
            + "time.sleep(60)\n",
        )
        started = time.time()
        r = _run(s, timeout_s=20)
        assert r.reason == "too_large" and not r.ok and r.value_bytes == b""
        assert time.time() - started < 15
        pid = int(Path(str(s) + ".pid").read_text())
        assert _wait_dead(pid), "grandchild survived the oversize kill"


class TestTimeout:
    def test_timeout_kills_the_grandchild(self, tmp_path):
        s = _script(tmp_path, GRANDCHILD + "import time\ntime.sleep(60)\n")
        started = time.time()
        r = _run(s, timeout_s=1)
        assert r.reason == "timeout" and not r.ok and r.exit_code is None
        assert time.time() - started < 10
        pid = int(Path(str(s) + ".pid").read_text())
        assert _wait_dead(pid), "grandchild survived the timeout"

    def test_grandchild_left_after_normal_exit_is_killed(self, tmp_path):
        s = _script(tmp_path, GRANDCHILD + "print('1')\n")
        r = _run(s, timeout_s=5)
        assert r.ok
        pid = int(Path(str(s) + ".pid").read_text())
        assert _wait_dead(pid)

    def test_escaped_grandchild_holding_stdout_does_not_hang(self, tmp_path):
        s = _script(
            tmp_path,
            "import subprocess, sys, time\n"
            "p = subprocess.Popen(['sleep', '30'], start_new_session=True)\n"
            "open(sys.argv[0] + '.pid', 'w').write(str(p.pid))\n"
            "time.sleep(60)\n",
        )
        started = time.time()
        try:
            r = _run(s, timeout_s=1)
            elapsed = time.time() - started
            assert r.reason == "timeout" and not r.ok
            assert elapsed < 13, f"run hung for {elapsed:.1f}s"
        finally:
            pidfile = Path(str(s) + ".pid")
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), 9)
                except OSError:
                    pass

    def test_clamp_timeout(self):
        assert clamp_timeout(0) == 1
        assert clamp_timeout(10_000) == 300
        assert clamp_timeout(None) == 60
        assert clamp_timeout(True) == 60
        assert clamp_timeout("5") == 60
        assert clamp_timeout(7.9) == 7


class TestStderrTail:
    def test_tail_is_last_2000_chars(self, tmp_path):
        s = _script(
            tmp_path,
            "import sys\nsys.stderr.write('a' * 3000 + 'b' * 1998 + 'Z')\nsys.exit(1)\n",
        )
        r = _run(s)
        assert r.reason == "failed"
        assert len(r.stderr_tail) == STDERR_TAIL_CHARS == 2000
        assert r.stderr_tail.endswith("bZ") and r.stderr_tail.startswith("a")
        assert r.stderr_tail.count("a") == 1

    def test_short_stderr_is_kept_whole(self, tmp_path):
        s = _script(tmp_path, "import sys\nsys.stderr.write('boom')\nprint('1')\n")
        r = _run(s)
        assert r.ok and r.stderr_tail == "boom"


class TestRuntimeChoice:
    @pytest.mark.parametrize("name", ["x.ts", "x.sh", "x", "x.PY", "x.tsx"])
    def test_unsupported_extensions_refused(self, tmp_path, name):
        with pytest.raises(UnsupportedScriptError):
            command_for(tmp_path / name, tmp_path)

    def test_run_script_refuses_ts_without_running(self, tmp_path):
        s = _script(tmp_path, "console.log(1)", name="x.ts")
        with pytest.raises(UnsupportedScriptError):
            _run(s)

    def test_python_without_pyproject_uses_plain_python(self, tmp_path):
        assert command_for(tmp_path / "a.py", tmp_path) == ["python", str(tmp_path / "a.py")]

    def test_python_with_pyproject_uses_uv_run(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        cmd = command_for(tmp_path / "a.py", tmp_path)
        assert cmd[:2] == ["uv", "run"] and "python" in cmd and cmd[-1] == str(tmp_path / "a.py")

    def test_use_uv_false_never_uses_uv(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert command_for(tmp_path / "a.py", tmp_path, use_uv=False)[0] == "python"

    def test_pyproject_is_looked_for_in_cwd_not_script_folder(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "pyproject.toml").write_text("")
        assert command_for(tmp_path / "a.py", tmp_path)[0] == "python"

    @pytest.mark.parametrize("name", ["a.js", "a.mjs", "a.cjs"])
    def test_node_extensions(self, tmp_path, name):
        assert command_for(tmp_path / name, tmp_path) == ["node", str(tmp_path / name)]

    @pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed in this container")
    def test_node_script_runs_with_params_and_env(self, tmp_path):
        s = _script(
            tmp_path,
            "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>"
            "console.log(JSON.stringify({x:JSON.parse(d).x,n:process.env.CINNA_NODE})));\n",
            name="s.js",
        )
        r = _run(s, {"x": 5})
        assert r.ok, r.stderr_tail
        assert r.value_bytes.strip() == b'{"x":5,"n":"node"}'
