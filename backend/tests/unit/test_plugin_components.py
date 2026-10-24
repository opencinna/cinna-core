"""Plugin tree analyser — what a checked-out plugin contains. Pure, no DB.

The API-observable path (components on sync / entries / discover rows) is
covered in ``tests/api/agents/marketplace_skills/marketplace_plugin_snapshots_test.py``.
"""
import json
from pathlib import Path

import pytest

from app.services.agents.skill_manifest import DEFAULT_MAX_TOTAL_BYTES
from app.services.plugins.plugin_components import analyse_plugin_tree


def _put(root: Path, files: dict) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            content if isinstance(content, str) else json.dumps(content), encoding="utf-8"
        )
    return root


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: Does {name}.\n---\n\n# {name}\n"


def _mcp(servers: dict) -> dict:
    return {".mcp.json": {"mcpServers": servers}}


def _server(analysis, name: str) -> dict:
    return next(s for s in analysis.components["mcp_servers"] if s["name"] == name)


def test_manifest_default_folders_skills_commands_agents_and_hooks(tmp_path: Path) -> None:
    """Default folders + manifest-declared dirs; nested commands as sub:stem; hook events sorted."""
    _put(
        tmp_path,
        {
            ".claude-plugin/plugin.json": {"name": "p", "skills": "./extra-skills"},
            "skills/alpha/SKILL.md": _skill_md("alpha"),
            "extra-skills/beta/SKILL.md": _skill_md("beta"),
            "commands/deploy.md": "# deploy",
            "commands/git/commit.md": "# commit",
            "agents/reviewer.md": "# reviewer",
            "hooks/hooks.json": {"hooks": {"Stop": [], "PreToolUse": []}},
        },
    )
    a = analyse_plugin_tree(tmp_path, {"name": "p"}, "claude")
    assert a.reason is None
    assert sorted(s["name"] for s in a.components["skills"]) == ["alpha", "beta"]
    assert a.components["commands"] == ["deploy", "git:commit"]
    assert a.components["agents"] == ["reviewer"]
    assert a.components["hooks"] == {"events": ["PreToolUse", "Stop"]}
    assert a.runs_code is True
    assert a.total_bytes > 0
    assert [rel for rel, _ in a.files] == sorted(rel for rel, _ in a.files)


def test_mcp_transport_and_runtime_matrix(tmp_path: Path) -> None:
    """stdio per command basename, explicit type wins, streamable-http -> http."""
    _put(
        tmp_path,
        _mcp(
            {
                "n": {"command": "npx", "args": ["-y", "pkg"]},
                "u": {"command": "uvx", "args": ["pkg"]},
                "d": {"command": "docker", "args": ["run", "img"]},
                "o": {"command": "/usr/local/bin/mytool"},
                "h": {"url": "https://x.test/mcp"},
                "s": {"type": "sse", "url": "https://x.test/sse"},
                "sh": {"type": "streamable-http", "url": "https://x.test/s"},
            }
        ),
    )
    a = analyse_plugin_tree(tmp_path, {"name": "p"}, "claude")
    got = {s["name"]: (s["transport"], s["runtime"]) for s in a.components["mcp_servers"]}
    assert got == {
        "n": ("stdio", "node"),
        "u": ("stdio", "python"),
        "d": ("stdio", "docker"),
        "o": ("stdio", None),
        "h": ("http", None),
        "s": ("sse", None),
        "sh": ("http", None),
    }


def test_flat_mcp_json_without_wrapper_key(tmp_path: Path) -> None:
    _put(tmp_path, {".mcp.json": {"solo": {"command": "node", "args": ["x.js"]}}})
    a = analyse_plugin_tree(tmp_path, {"name": "p"}, "claude")
    assert [s["name"] for s in a.components["mcp_servers"]] == ["solo"]


def test_env_placeholders_secrets_and_engine_variables(tmp_path: Path) -> None:
    """${VAR} and KEY/TOKEN names are secret; a value that is only ${CLAUDE_PLUGIN_*} is skipped."""
    _put(
        tmp_path,
        _mcp(
            {
                "srv": {
                    "command": "npx",
                    "args": ["${CLAUDE_PLUGIN_ROOT}/bin"],
                    "env": {
                        "API_KEY": "${MY_API_KEY}",
                        "REGION": "us-east-1",
                        "DB_PASSWORD": "literal",
                        "CFG": "${CLAUDE_PLUGIN_ROOT}/cfg.json",
                        "CLAUDE_PLUGIN_DATA": "x",
                    },
                },
                "remote": {
                    "url": "https://x.test/mcp",
                    "headers": {"Authorization": "Bearer ${REMOTE_TOKEN}"},
                },
            }
        ),
    )
    a = analyse_plugin_tree(tmp_path, {"name": "p"}, "claude")
    env = {e["name"]: e["secret"] for e in _server(a, "srv")["env"]}
    assert env["API_KEY"] is True
    assert env["REGION"] is False
    assert env["DB_PASSWORD"] is True
    assert "CFG" not in env and "CLAUDE_PLUGIN_DATA" not in env
    assert not any(n.startswith("CLAUDE_PLUGIN_") for n in env)
    assert _server(a, "srv")["auth"] == "env"
    remote = _server(a, "remote")
    assert remote["auth"] == "bearer"
    assert {"name": "REMOTE_TOKEN", "secret": True} in remote["env"]


def test_oauth_http_server_and_open_http_server_auth(tmp_path: Path) -> None:
    _put(
        tmp_path,
        _mcp(
            {
                "oa": {"type": "http", "url": "https://x.test", "oauth": {"clientId": "c"}},
                "open": {"type": "http", "url": "https://y.test"},
                "bare": {"command": "uvx", "args": ["p"]},
            }
        ),
    )
    a = analyse_plugin_tree(tmp_path, {"name": "p"}, "claude")
    assert _server(a, "oa")["auth"] == "oauth"
    assert _server(a, "open")["auth"] == "none"
    assert _server(a, "bare")["auth"] == "none"


def test_inline_strict_false_entry_components_are_merged(tmp_path: Path) -> None:
    """A strict:false catalog entry declares components the tree has no manifest for."""
    _put(tmp_path, {"cmds/hello.md": "# hello", "README.md": "x"})
    entry = {
        "name": "inline",
        "strict": False,
        "commands": ["./cmds/"],
        "mcpServers": {"s": {"type": "http", "url": "https://x.test"}},
        "lspServers": {"go": {"command": "gopls"}},
        "hooks": {"PostToolUse": []},
    }
    a = analyse_plugin_tree(tmp_path, entry, "claude")
    assert a.reason is None
    assert a.components["commands"] == ["hello"]
    assert [s["name"] for s in a.components["mcp_servers"]] == ["s"]
    assert a.components["lsp_servers"] == ["go"]
    assert a.components["hooks"] == {"events": ["PostToolUse"]}


def test_root_skill_md_is_one_skill_unless_skills_folder_exists(tmp_path: Path) -> None:
    _put(tmp_path, {"SKILL.md": _skill_md("rooty")})
    a = analyse_plugin_tree(tmp_path, {"name": "rooty"}, "claude")
    assert [s["name"] for s in a.components["skills"]] == ["rooty"]
    assert a.reason is None

    other = tmp_path / "other"
    _put(other, {"SKILL.md": _skill_md("rooty"), "skills/inner/SKILL.md": _skill_md("inner")})
    b = analyse_plugin_tree(other, {"name": "rooty"}, "claude")
    assert [s["name"] for s in b.components["skills"]] == ["inner"]


def test_lsp_servers_from_lsp_json(tmp_path: Path) -> None:
    _put(tmp_path, {".lsp.json": {"py": {"command": "pyright"}, "ts": {"command": "tsserver"}}})
    a = analyse_plugin_tree(tmp_path, {"name": "p"}, "claude")
    assert a.components["lsp_servers"] == ["py", "ts"]
    assert a.runs_code is True


def test_codex_manifest_is_read_for_codex_format(tmp_path: Path) -> None:
    _put(
        tmp_path,
        {".codex-plugin/plugin.json": {"name": "c", "skills": "./sk"}, "sk/one/SKILL.md": _skill_md("one")},
    )
    a = analyse_plugin_tree(tmp_path, {"name": "c"}, "codex")
    assert [s["name"] for s in a.components["skills"]] == ["one"]


def test_tree_without_components_reports_no_components(tmp_path: Path) -> None:
    _put(tmp_path, {"README.md": "hi", "src/main.py": "print(1)"})
    a = analyse_plugin_tree(tmp_path, {"name": "p"}, "claude")
    assert a.reason == "no_components"
    assert a.runs_code is False
    assert a.components["skills"] == [] and a.components["hooks"] is None


def test_symlink_anywhere_is_unsafe_path(tmp_path: Path) -> None:
    _put(tmp_path, {"skills/a/SKILL.md": _skill_md("a")})
    (tmp_path / "skills" / "a" / "link").symlink_to("/etc/passwd")
    a = analyse_plugin_tree(tmp_path, {"name": "p"}, "claude")
    assert a.reason == "unsafe_path"
    assert a.files == []

    d = tmp_path / "dirlink"
    d.parent.mkdir(exist_ok=True)
    clean = tmp_path.parent / (tmp_path.name + "_dirlink")
    _put(clean, {"commands/x.md": "x"})
    (clean / "ln").symlink_to(clean / "commands")
    assert analyse_plugin_tree(clean, {"name": "p"}, "claude").reason == "unsafe_path"


def test_declared_path_escaping_the_root_is_unsafe_path(tmp_path: Path) -> None:
    _put(tmp_path, {".claude-plugin/plugin.json": {"commands": "../outside"}})
    a = analyse_plugin_tree(tmp_path, {"name": "p"}, "claude")
    assert a.reason == "unsafe_path"


def test_tree_over_the_cap_is_too_large(tmp_path: Path) -> None:
    _put(tmp_path, {"commands/x.md": "x"})
    with open(tmp_path / "big.bin", "wb") as fh:
        fh.truncate(DEFAULT_MAX_TOTAL_BYTES + 1)
    a = analyse_plugin_tree(tmp_path, {"name": "p"}, "claude")
    assert a.reason == "too_large"


@pytest.mark.parametrize(
    "files,expected",
    [
        ({"skills/a/SKILL.md": _skill_md("a")}, False),
        ({"skills/a/SKILL.md": _skill_md("a"), "skills/a/scripts/run.sh": "echo"}, True),
        ({"commands/c.md": "c"}, False),
        ({"agents/a.md": "a"}, False),
        (_mcp({"h": {"type": "http", "url": "https://x.test"}}), False),
        (_mcp({"s": {"command": "npx"}}), True),
        ({"hooks/hooks.json": {"hooks": {"Stop": []}}}, True),
        ({".lsp.json": {"x": {"command": "x"}}}, True),
    ],
    ids=[
        "skill-no-scripts", "skill-scripts", "commands", "agents",
        "http-mcp", "stdio-mcp", "hooks", "lsp",
    ],
)
def test_runs_code_truth_table(tmp_path: Path, files: dict, expected: bool) -> None:
    _put(tmp_path, files)
    a = analyse_plugin_tree(tmp_path, {"name": "p"}, "claude")
    assert a.reason is None
    assert a.runs_code is expected
