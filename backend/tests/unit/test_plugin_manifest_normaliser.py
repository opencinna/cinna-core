"""The container's plugin-manifest normaliser — env-core, pure filesystem.

Both engines locate a plugin through ``.claude-plugin/plugin.json``. A Codex
repository names its manifest somewhere else and a ``skills``-format repository
has none at all, so ``AgentEnvService._ensure_plugin_manifest`` normalises the
four cases into that one file, in strict precedence — and the first rule of
that precedence is that an **authored** manifest is never touched.

Everything here runs against a real temp workspace: the JSON is really read and
really written, the ``skills``-format copy really goes through
``_ensure_marketplace_plugin``'s clone branch (from a local git repo, no
network). Only the git remote is local rather than remote.

Covered
-------
  * precedence: authored manifest kept; Codex manifest mapped; a bare
    ``SKILL.md`` at the root; nothing at all → a minimal manifest.
  * the Codex field mapping, including ``mcpServers`` copied only when it is
    inline and dropped when it is a path (the ``.mcp.json`` reader owns that),
    and a non-standard ``skills`` path left alone.
  * ``manifest_write_failed`` when the file cannot be written, and the
    ``blocked`` consequence: such a plugin is kept out of ``settings.json``.
  * ``_plugin_type`` degrading an absent / empty / non-string value to
    ``claude`` — the format is the container's only branch on repo shape.
  * a ``skills``-format entry landing at
    ``plugins/<marketplace>/<name>/skills/<name>/SKILL.md``, the same layout a
    catalog install produces.
  * ``subdir`` normalisation: a leading ``./`` is dropped, a leading dot is
    not — ``.config/plugin`` is a directory, not a mistake.

The API-observable half — which rows a marketplace sync produces, the manifest
entries the backend builds and the ``plugin_type`` / ``subdir`` they carry —
lives in ``tests/api/agents/core/agents_marketplace_formats_test.py`` and
``tests/api/agents/core/agents_resilient_plugins_test.py``. The catalog
(archive) install path is ``tests/unit/test_skill_catalog_container_install.py``.
"""
import json
import subprocess
from pathlib import Path

import pytest


def _service(tmp_path: Path):
    """An ``AgentEnvService`` over a throwaway workspace.

    Imported inside the helper because the env-template tree reaches
    ``sys.path`` through this package's conftest, not through the app.
    """
    from core.server.agent_env_service import AgentEnvService

    return AgentEnvService(str(tmp_path / "workspace"))


def _entry(**overrides) -> dict:
    """A manifest entry the way ``build_plugin_manifest`` emits one."""
    entry = {
        "marketplace_name": "codex-tools",
        "plugin_name": "reporter",
        "source": "marketplace",
        "plugin_type": "claude",
        "git": {"url": "https://example.com/x.git", "ref": "main", "subdir": ""},
        "conversation_mode": True,
        "building_mode": True,
        "disabled": False,
        "version": None,
        "commit_hash": None,
    }
    entry.update(overrides)
    return entry


def _plugin_dir(service, name: str = "reporter") -> Path:
    plugin_dir = service.plugins_dir / "codex-tools" / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    return plugin_dir


def _manifest_of(plugin_dir: Path) -> dict:
    return json.loads((plugin_dir / ".claude-plugin" / "plugin.json").read_text())


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ── Precedence rule 1: an authored manifest is never rewritten ─────────────


def test_an_authored_manifest_is_left_exactly_as_its_author_wrote_it(
    tmp_path: Path,
) -> None:
    """The normaliser only ever fills a gap.

    A plugin that ships its own ``.claude-plugin/plugin.json`` is authored
    content — read-modify-writing it would silently drop fields the engine
    understands and we do not. Even a name that disagrees with the directory
    (which Claude Code requires to match) is logged and left alone rather than
    corrected.
    """
    service = _service(tmp_path)
    plugin_dir = _plugin_dir(service)
    authored = {
        "name": "not-the-dir-name",
        "version": "7.7",
        "description": "Authored by hand",
        "commands": ["./commands"],
    }
    _write_json(plugin_dir / ".claude-plugin" / "plugin.json", authored)
    # A Codex manifest sitting beside it must not win: rule 1 comes first.
    _write_json(
        plugin_dir / ".codex-plugin" / "plugin.json",
        {"version": "1.0", "description": "from codex"},
    )

    assert service._ensure_plugin_manifest(plugin_dir, _entry(version="9.9")) is None
    assert _manifest_of(plugin_dir) == authored


# ── Precedence rule 2: the Codex manifest is mapped ────────────────────────


def test_a_codex_manifest_is_mapped_onto_the_shape_the_engines_read(
    tmp_path: Path,
) -> None:
    """
    The Codex file describes the plugin; the Claude file is where the engines
    look. The mapping carries version, description, author and homepage —
    and takes ``name`` from the **directory**, never from the Codex file,
    because Claude Code requires the two to match.
    """
    service = _service(tmp_path)
    plugin_dir = _plugin_dir(service)
    _write_json(
        plugin_dir / ".codex-plugin" / "plugin.json",
        {
            "name": "whatever-the-author-typed",
            "version": " 4.2 ",
            "description": "Writes reports",
            "homepage": "https://example.com/reporter",
            "author": {"name": "Ada", "email": "ada@example.com"},
            "mcpServers": {"reporter": {"command": "node", "args": ["s.js"]}},
        },
    )

    assert service._ensure_plugin_manifest(plugin_dir, _entry()) is None
    assert _manifest_of(plugin_dir) == {
        "name": "reporter",
        "version": "4.2",
        "description": "Writes reports",
        "homepage": "https://example.com/reporter",
        "author": {"name": "Ada", "email": "ada@example.com"},
        "mcpServers": {"reporter": {"command": "node", "args": ["s.js"]}},
    }


def test_an_mcp_servers_path_is_left_to_the_mcp_json_reader(tmp_path: Path) -> None:
    """
    ``mcpServers`` is copied only when the Codex manifest holds it **inline**.
    A path is deliberately dropped here: ``<plugin>/.mcp.json`` is already read
    at session start, and that is the one place the translation lives —
    copying a path string into the Claude manifest would hand the engine a
    value it reads as a server definition.
    """
    service = _service(tmp_path)
    plugin_dir = _plugin_dir(service)
    _write_json(
        plugin_dir / ".codex-plugin" / "plugin.json",
        {"version": "1.0", "mcpServers": "./.mcp.json"},
    )

    assert service._ensure_plugin_manifest(plugin_dir, _entry()) is None
    manifest = _manifest_of(plugin_dir)
    assert "mcpServers" not in manifest
    assert manifest["name"] == "reporter"


def test_a_non_standard_codex_skills_path_still_produces_a_manifest(
    tmp_path: Path,
) -> None:
    """
    Only ``./skills`` is honoured. Anything else is reported as a
    ``plugin_capability_warning`` and left alone — relocating a plugin's tree
    is out of scope — but the plugin is still normalised and installable, just
    without its skills being found.
    """
    service = _service(tmp_path)
    plugin_dir = _plugin_dir(service)
    _write_json(
        plugin_dir / ".codex-plugin" / "plugin.json",
        {"version": "1.0", "skills": "./lib/skills"},
    )

    assert service._ensure_plugin_manifest(plugin_dir, _entry()) is None
    assert _manifest_of(plugin_dir) == {"name": "reporter", "version": "1.0"}


def test_a_codex_manifest_without_a_version_falls_back_to_the_pinned_ref(
    tmp_path: Path,
) -> None:
    """A synthesised version comes from the entry, else the short commit."""
    service = _service(tmp_path)
    plugin_dir = _plugin_dir(service)
    _write_json(plugin_dir / ".codex-plugin" / "plugin.json", {"description": "d"})

    assert (
        service._ensure_plugin_manifest(
            plugin_dir, _entry(version=None), ref="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
        )
        is None
    )
    assert _manifest_of(plugin_dir)["version"] == "a1b2c3d"


# ── Precedence rules 3 and 4: a bare skill, and nothing at all ─────────────


def test_a_bare_skill_directory_gets_a_manifest_naming_the_directory(
    tmp_path: Path,
) -> None:
    """
    A ``skills``-format entry that arrived through an older manifest (no
    ``plugin_type``) lands its ``SKILL.md`` at the plugin root. The dir still
    has to load as a plugin, so a manifest is synthesised for it.
    """
    service = _service(tmp_path)
    plugin_dir = _plugin_dir(service, "data-cleanup")
    (plugin_dir / "SKILL.md").write_text(
        "---\nname: data-cleanup\ndescription: Cleans data.\n---\n\nbody\n",
        encoding="utf-8",
    )

    assert service._ensure_plugin_manifest(plugin_dir, _entry(version="2.0")) is None
    assert _manifest_of(plugin_dir) == {
        "name": "data-cleanup",
        "version": "2.0",
        "description": "Skill 'data-cleanup'",
    }


def test_a_directory_with_no_manifest_at_all_still_gets_a_minimal_one(
    tmp_path: Path,
) -> None:
    """Rule 4: a minimal manifest beats a directory the SDK silently ignores."""
    service = _service(tmp_path)
    plugin_dir = _plugin_dir(service)
    (plugin_dir / "README.md").write_text("hi", encoding="utf-8")

    assert service._ensure_plugin_manifest(plugin_dir, _entry()) is None
    assert _manifest_of(plugin_dir) == {"name": "reporter", "version": "0.0.0"}


# ── The one failure the normaliser can report ─────────────────────────────


def test_a_manifest_that_cannot_be_written_is_reported_not_swallowed(
    tmp_path: Path,
) -> None:
    """
    The write is refused rather than followed when ``.claude-plugin`` is a
    symlink — this code never writes through a link — and the refusal comes
    back as ``manifest_write_failed``, the per-plugin failure the install
    result carries.
    """
    service = _service(tmp_path)
    plugin_dir = _plugin_dir(service)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (plugin_dir / ".claude-plugin").symlink_to(outside, target_is_directory=True)

    assert (
        service._ensure_plugin_manifest(plugin_dir, _entry())
        == service._MANIFEST_WRITE_FAILED
    )
    assert not (outside / "plugin.json").exists(), (
        "nothing may be written through the link"
    )
    assert service._plugin_manifest_is_loadable(plugin_dir) is False


def test_a_plugin_with_no_loadable_manifest_is_kept_out_of_settings(
    tmp_path: Path,
) -> None:
    """
    ``manifest_write_failed`` has a second consequence: files on disk are not
    enough to activate a plugin, because no engine can load a directory
    without a manifest. Its healthy neighbour is unaffected.
    """
    service = _service(tmp_path)
    blocked_dir = _plugin_dir(service, "reporter")
    healthy_dir = _plugin_dir(service, "charting")
    _write_json(
        healthy_dir / ".claude-plugin" / "plugin.json",
        {"name": "charting", "version": "1.0"},
    )

    entries = [_entry(plugin_name="reporter"), _entry(plugin_name="charting")]
    service._regenerate_plugin_settings(
        entries, ["Bash"], blocked={("codex-tools", "reporter")}
    )

    settings = json.loads((service.plugins_dir / "settings.json").read_text())
    assert [p["plugin_name"] for p in settings["active_plugins"]] == ["charting"]
    assert settings["allowed_tools"] == ["Bash"]
    assert blocked_dir.exists(), "the files stay; only the activation is withheld"


# ── The format flag itself ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({}, "claude"),
        ({"plugin_type": None}, "claude"),
        ({"plugin_type": ""}, "claude"),
        ({"plugin_type": "   "}, "claude"),
        ({"plugin_type": 7}, "claude"),
        ({"plugin_type": "codex"}, "codex"),
        ({"plugin_type": " SKILLS "}, "skills"),
    ],
)
def test_a_missing_plugin_type_degrades_to_claude(
    tmp_path: Path, raw: dict, expected: str
) -> None:
    """
    ``plugin_type`` is the container's only branch on repository format, and
    manifests written before it existed do not carry it. An absent, empty or
    non-string value has to mean the layout the engine has always read —
    anything else would relocate an existing plugin's tree on the next sync.
    """
    from core.server.agent_env_service import AgentEnvService

    assert AgentEnvService._plugin_type(raw) == expected


# ── The skills-format copy ────────────────────────────────────────────────


def _git_repo_with_skill(root: Path, skill_name: str) -> str:
    """A local git repo whose root is one skill folder. Returns its URL."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: Does {skill_name} things.\n---\n\nbody\n",
        encoding="utf-8",
    )
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    _git("init", "-q", "-b", "main")
    _git("-c", "user.email=t@example.com", "-c", "user.name=T", "add", ".")
    _git(
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=T",
        "commit",
        "-q",
        "-m",
        "skill",
    )
    return str(root)


def test_a_skills_format_entry_lands_one_level_down_under_skills(
    tmp_path: Path,
) -> None:
    """
    A ``skills``-format marketplace publishes one plugin per skill, so the
    fetched tree IS the skill folder. It has to land at
    ``<plugin_dir>/skills/<plugin>/`` — the layout a catalog install produces
    and the only one the skills index and the OpenCode registration look at —
    and the plugin dir itself gets the synthesised manifest.
    """
    service = _service(tmp_path)
    url = _git_repo_with_skill(tmp_path / "upstream" / "data-cleanup", "data-cleanup")

    plugin_dir = service.plugins_dir / "flat-skills" / "data-cleanup"
    status, error = service._ensure_marketplace_plugin(
        plugin_dir,
        _entry(
            marketplace_name="flat-skills",
            plugin_name="data-cleanup",
            plugin_type="skills",
            version="1.0",
            git={"url": url, "ref": "main", "subdir": ""},
        ),
    )

    assert (status, error) == ("installed", None)
    skill_md = plugin_dir / "skills" / "data-cleanup" / "SKILL.md"
    assert skill_md.is_file(), sorted(p.name for p in plugin_dir.rglob("*"))
    assert "name: data-cleanup" in skill_md.read_text()
    assert (plugin_dir / "skills" / "data-cleanup" / "scripts" / "run.sh").is_file()
    assert not (plugin_dir / "SKILL.md").exists(), (
        "the skill must not also sit at the plugin root — the index looks "
        "exactly one level down"
    )
    assert _manifest_of(plugin_dir) == {"name": "data-cleanup", "version": "1.0"}


def _git_repo_with_plugin_under(root: Path, subdir: str, skill_name: str) -> str:
    """A local git repo holding one plugin at ``subdir``. Returns its URL."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# monorepo\n", encoding="utf-8")
    plugin = root / subdir
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: Does {skill_name} things.\n---\n\nbody\n",
        encoding="utf-8",
    )

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    _git("init", "-q", "-b", "main")
    _git("-c", "user.email=t@example.com", "-c", "user.name=T", "add", ".")
    _git(
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=T",
        "commit",
        "-q",
        "-m",
        "plugin",
    )
    return str(root)


def test_a_subdir_beginning_with_a_dot_keeps_its_dot(tmp_path: Path) -> None:
    """
    ``subdir`` is normalised by dropping a leading ``./`` — and nothing else.

    The container used to do that with ``lstrip("./")``, which strips a
    character *set*: ``.config/plugin`` arrived as ``config/plugin`` and the
    clone step then looked for a directory that does not exist. A leading dot
    is a legitimate first character of a directory name, which is why the
    backend's own normaliser (``_safe_entry_path`` /``_normalize_subdir``)
    says so in as many words — and until a Codex ``git-subdir`` entry made
    non-empty subdirs reachable for url sources, the two halves never had to
    agree.
    """
    service = _service(tmp_path)
    url = _git_repo_with_plugin_under(
        tmp_path / "upstream" / "monorepo", ".config/house-style", "house-style"
    )

    plugin_dir = service.plugins_dir / "codex-mkt" / "house-style"
    status, error = service._ensure_marketplace_plugin(
        plugin_dir,
        _entry(
            marketplace_name="codex-mkt",
            plugin_name="house-style",
            plugin_type="codex",
            git={"url": url, "ref": "main", "subdir": ".config/house-style"},
        ),
    )

    assert (status, error) == ("installed", None)
    assert (plugin_dir / "SKILL.md").is_file(), sorted(
        p.name for p in plugin_dir.rglob("*")
    )
    assert not (plugin_dir / "README.md").exists(), (
        "the subdir is the plugin, not the repository around it"
    )


def test_a_leading_dot_slash_is_still_dropped(tmp_path: Path) -> None:
    """The half of the old behaviour that was right: ``./x`` resolves to ``x``.

    Same repository, same call — only the entry writes the path the way a
    catalog author does.
    """
    service = _service(tmp_path)
    url = _git_repo_with_plugin_under(
        tmp_path / "upstream" / "monorepo", "packages/reporter", "reporter"
    )

    plugin_dir = service.plugins_dir / "codex-mkt" / "reporter"
    status, error = service._ensure_marketplace_plugin(
        plugin_dir,
        _entry(
            marketplace_name="codex-mkt",
            plugin_name="reporter",
            plugin_type="codex",
            git={"url": url, "ref": "main", "subdir": "./packages/reporter"},
        ),
    )

    assert (status, error) == ("installed", None)
    assert (plugin_dir / "SKILL.md").is_file()


def test_a_claude_format_entry_is_the_plugin_directory_itself(
    tmp_path: Path,
) -> None:
    """The counterpart: any other format's tree IS the plugin directory.

    Same repository, same call, only ``plugin_type`` differs — so this pins
    that the ``skills`` branch above is keyed on the format flag and nothing
    else.
    """
    service = _service(tmp_path)
    url = _git_repo_with_skill(tmp_path / "upstream" / "house-style", "house-style")

    plugin_dir = service.plugins_dir / "claude-mkt" / "house-style"
    status, error = service._ensure_marketplace_plugin(
        plugin_dir,
        _entry(
            marketplace_name="claude-mkt",
            plugin_name="house-style",
            plugin_type="claude",
            git={"url": url, "ref": "main", "subdir": ""},
        ),
    )

    assert (status, error) == ("installed", None)
    assert (plugin_dir / "SKILL.md").is_file()
    assert not (plugin_dir / "skills").exists()
    # Rule 3: a bare SKILL.md at the root still gets a manifest of its own.
    assert _manifest_of(plugin_dir)["name"] == "house-style"
