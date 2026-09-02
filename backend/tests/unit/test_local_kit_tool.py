"""Local Agent Kit tool (`docs/local_agent_kit/tools/kit.py`) — pure unit tests.

The kit is shipped content, not backend code: `kit.py` is stdlib-only Python that
runs on the *user's* machine next to a scaffolded agent. These tests guarantee the
shipped scaffold still validates against the shipped tool, that an export never
carries secrets to the cloud, and that the tar extraction used by `kit.py refresh`
refuses to escape its destination.

Every CLI command is invoked as a subprocess with `sys.executable`, so the
stdlib-only constraint is genuinely exercised (a third-party import would fail the
run rather than pass silently through the test's own environment).

Kit location: `docs/` is not mounted into the backend container, so inside Docker
this module skips until `make sync-platform-knowledge` has copied the kit into
`app/env-templates/platform-knowledge-env/.../knowledge/local-kit/` (a later
phase). Set `LOCAL_AGENT_KIT_DIR` to point it at any other copy.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


def _find_kit_dir() -> Path | None:
    """Locate the kit source: env override, repo checkout, then the synced snapshot."""
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
    pytest.skip(
        "Local Agent Kit source not found — expected docs/local_agent_kit/ (repo checkout) "
        "or the synced knowledge/local-kit/ snapshot, or $LOCAL_AGENT_KIT_DIR.",
        allow_module_level=True,
    )

KIT_PY = KIT_DIR / "tools" / "kit.py"

if not KIT_PY.is_file():
    pytest.skip(f"kit.py missing from the kit at {KIT_DIR}", allow_module_level=True)


# Tokens the *platform* renders when it serves the kit. In a repo checkout they are
# still literal, so the scaffold may legitimately carry them; the lowercase
# `{{name}}` / `{{slug}}` scaffold tokens must never survive `kit.py new`.
PLATFORM_TOKEN_RE = re.compile(r"^\{\{[A-Z][A-Z0-9_]*\}\}$")
ANY_TOKEN_RE = re.compile(r"\{\{[^{}]*\}\}")


def run_kit(*args: str) -> subprocess.CompletedProcess:
    """Run `kit.py` in a fresh interpreter, exactly as a user's assistant would."""
    return subprocess.run(
        [sys.executable, str(KIT_PY), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def load_kit_module():
    """Import `kit.py` as a module (only stdlib imports may be involved)."""
    spec = importlib.util.spec_from_file_location("cinna_local_kit_tool", KIT_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def kit_module():
    return load_kit_module()


def make_cloud_ready(agent_dir: Path) -> None:
    """Finish the scaffold the way guides 01/02 require before a cloud import."""
    manifest_path = agent_dir / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["description"] = "Watches the billing inbox and flags invoices missing a PO number."
    manifest["example_prompts"] = ["check invoices from last week", "list invoices without a PO"]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


@pytest.fixture()
def cloud_ready_agent(scaffolded_agent: Path) -> Path:
    make_cloud_ready(scaffolded_agent)
    return scaffolded_agent


@pytest.fixture()
def scaffolded_agent(tmp_path: Path) -> Path:
    result = run_kit("new", "invoice-watcher", "--name", "Invoice Watcher", "--root", str(tmp_path))
    assert result.returncode == 0, f"kit.py new failed:\n{result.stdout}\n{result.stderr}"
    agent_dir = tmp_path / "Local" / "invoice-watcher"
    assert agent_dir.is_dir()
    return agent_dir


# --------------------------------------------------------------------------- #
# new + validate
# --------------------------------------------------------------------------- #


def test_new_restores_the_dot_on_every_scaffold_ignore_file(
    scaffolded_agent: Path,
) -> None:
    """The templates ship `gitignore`; the created agent must get `.gitignore`.

    A path-excluding `.gitignore` cannot ship dotted: inside `templates/agent/`
    it applies to the repository that stores the kit as much as to the user's
    machine, so it hides `app-data/` from `git add` and a fresh clone syncs a
    kit that is missing files. Shipping them dotless moves responsibility for
    the dot into `kit.py new`, where it is testable.
    """
    for relative in (".gitignore", "app-data/cache/.gitignore"):
        assert (scaffolded_agent / relative).is_file(), relative
        dotless = (scaffolded_agent / relative).with_name("gitignore")
        assert not dotless.exists(), f"{dotless} was left behind"

    root_ignore = (scaffolded_agent / ".gitignore").read_text(encoding="utf-8")
    assert "credentials/.env" in root_ignore
    # `*` + `!.gitignore` only re-includes itself once the dot is back.
    cache_ignore = (scaffolded_agent / "app-data" / "cache" / ".gitignore").read_text(
        encoding="utf-8"
    )
    assert "!.gitignore" in cache_ignore

    # Same convention for the root template, installed by the assistant by hand.
    assert (KIT_DIR / "templates" / "root" / "gitignore").is_file()


def test_the_kit_ships_no_ignore_rule_that_hides_its_own_content() -> None:
    """Close the bug class, not just today's instances of it.

    Any `.gitignore` under the kit is a live rule in whichever repository stores
    the kit — this one, and the synced snapshot under the knowledge template. A
    new one, or a new file underneath an existing one, would silently drop
    scaffold files from `git add`, and nothing else notices: the sync copies
    whatever is on disk, so the served kit would differ per checkout.

    The single allowed exception names only files no repository should track, so
    there it is doing the right thing rather than hiding content.
    """
    allowed = {"templates/agent/credentials/.gitignore"}
    found = {
        path.relative_to(KIT_DIR).as_posix() for path in KIT_DIR.rglob(".gitignore")
    }

    assert found <= allowed, (
        f"unexpected .gitignore in the kit: {sorted(found - allowed)} — ship it "
        "as a dotless `gitignore` and add it to kit.py's SCAFFOLD_IGNORE_FILES "
        "so `kit.py new` restores the dot"
    )


def test_new_scaffolds_and_validate_exits_zero(scaffolded_agent: Path) -> None:
    manifest = json.loads((scaffolded_agent / "cinna-agent.json").read_text(encoding="utf-8"))
    assert manifest["slug"] == "invoice-watcher"
    assert manifest["name"] == "Invoice Watcher"
    assert manifest["schema_version"] == 1

    result = run_kit("validate", str(scaffolded_agent))
    assert result.returncode == 0, f"fresh scaffold must validate:\n{result.stdout}\n{result.stderr}"
    assert "ERROR" not in result.stdout
    # The tool always points at the authoritative schema for full validation.
    assert "cinna-agent.schema.json" in result.stdout


def test_validate_json_report_is_machine_readable(scaffolded_agent: Path) -> None:
    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["slug"] == "invoice-watcher"
    assert payload["errors"] == []
    assert payload["schema"].endswith("cinna-agent.schema.json")


def test_new_refuses_to_overwrite_an_existing_agent(tmp_path: Path) -> None:
    assert run_kit("new", "dup-agent", "--root", str(tmp_path)).returncode == 0
    second = run_kit("new", "dup-agent", "--root", str(tmp_path))
    assert second.returncode == 1
    assert "already exists" in second.stderr


def test_new_rejects_an_invalid_slug(tmp_path: Path) -> None:
    result = run_kit("new", "Invalid Slug", "--root", str(tmp_path))
    assert result.returncode == 1
    assert "slug" in result.stderr


def test_no_unfilled_scaffold_tokens_remain(scaffolded_agent: Path) -> None:
    leftovers: dict[str, list[str]] = {}
    for path in sorted(scaffolded_agent.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        tokens = [t for t in ANY_TOKEN_RE.findall(text) if not PLATFORM_TOKEN_RE.match(t)]
        if tokens:
            leftovers[str(path.relative_to(scaffolded_agent))] = tokens
    assert leftovers == {}, f"unsubstituted scaffold tokens remain: {leftovers}"


# --------------------------------------------------------------------------- #
# validate — failure modes
# --------------------------------------------------------------------------- #


def test_validate_fails_on_empty_workflow_prompt(scaffolded_agent: Path) -> None:
    (scaffolded_agent / "docs" / "WORKFLOW_PROMPT.md").write_text("", encoding="utf-8")
    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 1
    errors = json.loads(result.stdout)["errors"]
    assert any(
        "WORKFLOW_PROMPT.md" in error and "is empty" in error for error in errors
    ), errors


def test_validate_fails_when_slug_does_not_match_the_folder(scaffolded_agent: Path) -> None:
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["slug"] = "some-other-slug"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert any("slug" in error for error in payload["errors"])


def test_validate_fails_on_an_env_file_outside_credentials(scaffolded_agent: Path) -> None:
    (scaffolded_agent / "leaked.env").write_text("TOKEN=super-secret-value\n", encoding="utf-8")
    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any("leaked.env" in error for error in payload["errors"])
    # The finding names the file, never its contents.
    assert "super-secret-value" not in result.stdout


def test_validate_warnings_are_not_vacuous(scaffolded_agent: Path) -> None:
    """Each advisory check must actually fire when its condition is met (exit stays 0)."""
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schedules"] = [
        {
            "name": "Weekday check",
            "cron_string": "0 6 * * 1-5",
            "schedule_type": "static_prompt",
            "prompt": "Check what arrived since yesterday.",
        }
    ]
    manifest["status_refresh_command"] = None
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    cli_commands = scaffolded_agent / "docs" / "CLI_COMMANDS.yaml"
    cli_commands.write_text(
        cli_commands.read_text(encoding="utf-8").replace("- name: status", "- name: fetch"), encoding="utf-8"
    )
    (scaffolded_agent / "scripts" / "uncatalogued.py").write_text("print('hi')\n", encoding="utf-8")

    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 0, "advisory findings must never fail the run"
    warnings = json.loads(result.stdout)["warnings"]
    assert any("no matching Makefile target" in warning for warning in warnings)
    assert any("schedules but no `status` command" in warning for warning in warnings)
    assert any("uncatalogued.py" in warning for warning in warnings)


def test_validate_fix_regenerates_workspace_requirements(scaffolded_agent: Path) -> None:
    pyproject = scaffolded_agent / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace("dependencies = []", 'dependencies = ["requests>=2.31"]'),
        encoding="utf-8",
    )
    assert run_kit("validate", str(scaffolded_agent)).returncode == 1

    fixed = run_kit("validate", str(scaffolded_agent), "--fix")
    assert fixed.returncode == 0
    requirements = scaffolded_agent / "workspace_requirements.txt"
    assert "requests>=2.31" in requirements.read_text(encoding="utf-8")

    # And --fix must also create the file outright when it is missing entirely.
    requirements.unlink()
    assert run_kit("validate", str(scaffolded_agent)).returncode == 1
    assert run_kit("validate", str(scaffolded_agent), "--fix").returncode == 0
    assert "requests>=2.31" in requirements.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #


def test_export_excludes_local_only_paths_and_clears_the_cloud_block(
    cloud_ready_agent: Path, tmp_path: Path
) -> None:
    scaffolded_agent = cloud_ready_agent
    (scaffolded_agent / "credentials" / ".env").write_text("SECRET=nope\n", encoding="utf-8")
    venv_marker = scaffolded_agent / ".venv" / "pyvenv.cfg"
    venv_marker.parent.mkdir(parents=True, exist_ok=True)
    venv_marker.write_text("home = /nowhere\n", encoding="utf-8")

    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cloud"] = {
        "platform_url": "https://example.invalid",
        "agent_id": "11111111-1111-1111-1111-111111111111",
        "imported_at": "2026-01-01T00:00:00Z",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    destination = tmp_path / "Cloud" / "agents" / "invoice-watcher" / "workspace"
    result = run_kit("export", str(scaffolded_agent), "--to", str(destination))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    assert not (destination / "credentials").exists()
    # These four are load-bearing: they are excluded by kit.json's list alone.
    # (`.venv/`, `.git/` and `__pycache__/` are additionally never walked, so an
    # assertion about them would hold even with an empty exclude list.)
    assert not (destination / "AGENTS.md").exists()
    assert not (destination / "CLAUDE.md").exists()
    assert not (destination / ".claude").exists()
    assert not (destination / "app-data").exists()
    assert not (destination / ".venv").exists()

    # The tool names what it left behind — nothing disappears silently.
    for expected in ("AGENTS.md", "CLAUDE.md", "credentials/.env", ".claude/settings.local.json"):
        assert expected in result.stdout, result.stdout

    # The definitional content does travel.
    assert (destination / "docs" / "WORKFLOW_PROMPT.md").is_file()
    assert (destination / "scripts" / "README.md").is_file()
    assert (destination / "workspace_requirements.txt").is_file()

    exported = json.loads((destination / "cinna-agent.json").read_text(encoding="utf-8"))
    assert exported["cloud"] == {"platform_url": None, "agent_id": None, "imported_at": None}

    # No secret value anywhere in the exported tree or in the tool's output.
    assert "nope" not in result.stdout
    for path in destination.rglob("*"):
        if path.is_file():
            assert "SECRET=nope" not in path.read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize("stray", ["prod.env", ".env.local", "config/settings.env"])
def test_export_never_carries_a_stray_env_file(
    cloud_ready_agent: Path, tmp_path: Path, stray: str
) -> None:
    scaffolded_agent = cloud_ready_agent
    """The cloud-import tree is the thing that gets pushed — the secret gate sits here."""
    leaked = scaffolded_agent / stray
    leaked.parent.mkdir(parents=True, exist_ok=True)
    leaked.write_text("API_TOKEN=leaked-value\n", encoding="utf-8")

    validated = run_kit("validate", str(scaffolded_agent), "--json")
    assert validated.returncode == 1
    assert any(stray in error for error in json.loads(validated.stdout)["errors"])

    destination = tmp_path / "out"
    refused = run_kit("export", str(scaffolded_agent), "--to", str(destination))
    assert refused.returncode == 1, "export must refuse an agent that does not validate"
    assert "leaked-value" not in refused.stdout + refused.stderr

    # Even when the user overrides the gate, the env file itself never travels.
    forced = run_kit("export", str(scaffolded_agent), "--to", str(destination), "--force")
    assert forced.returncode == 0, f"{forced.stdout}\n{forced.stderr}"
    assert not (destination / stray).exists()
    for path in destination.rglob("*"):
        if path.is_file():
            assert "leaked-value" not in path.read_text(encoding="utf-8", errors="replace")


def test_export_refuses_a_destination_that_contains_the_agent(
    cloud_ready_agent: Path, tmp_path: Path
) -> None:
    scaffolded_agent = cloud_ready_agent
    result = run_kit("export", str(scaffolded_agent), "--to", str(scaffolded_agent.parent))
    assert result.returncode == 1
    assert "scatter" in result.stderr


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def test_list_reports_the_scaffolded_agent(scaffolded_agent: Path, tmp_path: Path) -> None:
    result = run_kit("list", "--root", str(tmp_path))
    assert result.returncode == 0
    assert "invoice-watcher" in result.stdout
    assert "Invoice Watcher" in result.stdout


# --------------------------------------------------------------------------- #
# Shipped kit content
# --------------------------------------------------------------------------- #


def test_every_ladder_doc_exists() -> None:
    config = json.loads((KIT_DIR / "kit.json").read_text(encoding="utf-8"))
    ladder = config["ladder"]
    assert ladder, "kit.json must declare the capability ladder"
    missing = [rung["doc"] for rung in ladder if not (KIT_DIR / rung["doc"]).is_file()]
    assert missing == [], f"ladder documents missing from the kit: {missing}"


def test_kit_json_declares_the_paths_the_tool_uses() -> None:
    config = json.loads((KIT_DIR / "kit.json").read_text(encoding="utf-8"))
    for key in ("manifest_schema", "agent_template", "root_template", "tool", "entry", "index"):
        assert (KIT_DIR / config[key]).exists(), f"kit.json {key} points at a missing path"


def test_template_manifest_matches_the_shipped_schema(kit_module) -> None:
    manifest = json.loads((KIT_DIR / "templates" / "agent" / "cinna-agent.json").read_text(encoding="utf-8"))
    schema = json.loads((KIT_DIR / "schema" / "cinna-agent.schema.json").read_text(encoding="utf-8"))

    # importorskip, not try/except: losing jsonschema must show as a skip rather
    # than quietly reducing this test to the stdlib subset check below.
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(instance=manifest, schema=schema)

    # The stdlib subset validator the user's machine actually runs must agree.
    report = kit_module.Report()
    kit_module.validate_manifest(manifest, report)
    assert report.errors == []


# --------------------------------------------------------------------------- #
# Safe tar extraction (used by `kit.py refresh`)
# --------------------------------------------------------------------------- #


def _add_member(
    tar: tarfile.TarFile,
    member_name: str,
    *,
    member_type: bytes | None = None,
    linkname: str | None = None,
) -> None:
    payload = b"x"
    info = tarfile.TarInfo(name=member_name)
    if member_type is not None:
        info.type = member_type
        if linkname is not None:
            info.linkname = linkname
        tar.addfile(info)
        return
    info.size = len(payload)
    tar.addfile(info, io.BytesIO(payload))


def _tar_with_member(path: Path, member_name: str, *, symlink_to: str | None = None) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        if symlink_to is not None:
            _add_member(tar, member_name, member_type=tarfile.SYMTYPE, linkname=symlink_to)
        else:
            _add_member(tar, member_name)
    return path


@pytest.mark.parametrize(
    "member_name",
    ["../escape.txt", "kit/../../escape.txt", "/etc/escape.txt"],
)
def test_safe_extract_rejects_path_traversal(kit_module, tmp_path: Path, member_name: str) -> None:
    archive = _tar_with_member(tmp_path / "evil.tar.gz", member_name)
    destination = tmp_path / "dest"
    destination.mkdir()

    with tarfile.open(archive, "r:*") as tar:
        with pytest.raises(kit_module.KitError):
            kit_module.safe_extract(tar, destination)

    assert list(destination.iterdir()) == []
    assert not (tmp_path / "escape.txt").exists()
    assert not Path("/etc/escape.txt").exists()


def test_safe_extract_rejects_symlink_members(kit_module, tmp_path: Path) -> None:
    archive = _tar_with_member(tmp_path / "link.tar.gz", "link.txt", symlink_to="/etc/passwd")
    destination = tmp_path / "dest"
    destination.mkdir()

    with tarfile.open(archive, "r:*") as tar:
        with pytest.raises(kit_module.KitError):
            kit_module.safe_extract(tar, destination)

    assert list(destination.iterdir()) == []


@pytest.mark.parametrize(
    "member_type",
    [tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE],
)
def test_safe_extract_rejects_non_regular_members(
    kit_module, tmp_path: Path, member_type: bytes
) -> None:
    archive = tmp_path / "special.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        _add_member(tar, "odd", member_type=member_type, linkname="/etc/passwd")
    destination = tmp_path / "dest"
    destination.mkdir()

    with tarfile.open(archive, "r:*") as tar:
        with pytest.raises(kit_module.KitError):
            kit_module.safe_extract(tar, destination)

    assert list(destination.iterdir()) == []


def test_safe_extract_rejects_the_whole_archive_before_writing_anything(
    kit_module, tmp_path: Path
) -> None:
    """One bad member must abort the extraction, not leave the good ones behind."""
    archive = tmp_path / "mixed.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        _add_member(tar, "kit/VERSION")
        _add_member(tar, "kit/README.md")
        _add_member(tar, "../escape.txt")
    destination = tmp_path / "dest"
    destination.mkdir()

    with tarfile.open(archive, "r:*") as tar:
        with pytest.raises(kit_module.KitError):
            kit_module.safe_extract(tar, destination)

    assert list(destination.iterdir()) == []


def test_safe_extract_tolerates_a_root_directory_member(kit_module, tmp_path: Path) -> None:
    """A `./` archive-root entry carries nothing and must not break the extraction."""
    archive = tmp_path / "rooted.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        _add_member(tar, ".", member_type=tarfile.DIRTYPE)
        _add_member(tar, "./kit/VERSION")
    destination = tmp_path / "dest"
    destination.mkdir()

    with tarfile.open(archive, "r:*") as tar:
        kit_module.safe_extract(tar, destination)

    assert (destination / "kit" / "VERSION").is_file()


def test_safe_extract_accepts_a_well_formed_archive(kit_module, tmp_path: Path) -> None:
    archive = _tar_with_member(tmp_path / "good.tar.gz", "kit/VERSION")
    destination = tmp_path / "dest"
    destination.mkdir()

    with tarfile.open(archive, "r:*") as tar:
        kit_module.safe_extract(tar, destination)

    assert (destination / "kit" / "VERSION").is_file()


# --------------------------------------------------------------------------- #
# refresh — "network error is a warning, never a blocked session"
# --------------------------------------------------------------------------- #


@pytest.fixture()
def kit_copy(tmp_path: Path) -> Path:
    """A writable copy of the kit, laid out the way a user's machine has it."""
    destination = tmp_path / "workshop" / ".cinna-kit"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(KIT_DIR, destination, ignore=shutil.ignore_patterns("__pycache__"))
    (destination / "VERSION").write_text("local-test-version\n", encoding="utf-8")
    return destination


def _set_kit_base_url(kit_dir: Path, base_url: str) -> None:
    kit_json = kit_dir / "kit.json"
    config = json.loads(kit_json.read_text(encoding="utf-8"))
    config["kit_base_url"] = base_url
    kit_json.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _run_kit_copy(kit_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(kit_dir / "tools" / "kit.py"), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_refresh_check_survives_an_unreachable_platform(kit_copy: Path) -> None:
    # Port 9 (discard) refuses fast and is never a real kit host.
    _set_kit_base_url(kit_copy, "http://127.0.0.1:9/api/agent-start")
    result = _run_kit_copy(kit_copy, "refresh", "--check")

    assert result.returncode == 0, "an offline machine must not block the session"
    assert "warning" in result.stdout.lower()
    assert (kit_copy / "VERSION").read_text(encoding="utf-8").strip() == "local-test-version"
    # The freshness stamp is written even on the failed path, so the 7-day rule
    # does not re-fire every session while offline.
    assert (kit_copy / ".last_refresh_check").is_file()


def test_refresh_refuses_a_non_http_kit_url(kit_copy: Path, tmp_path: Path) -> None:
    version_file = tmp_path / "version"
    version_file.write_text("someone-elses-version\n", encoding="utf-8")
    _set_kit_base_url(kit_copy, tmp_path.as_uri())

    result = _run_kit_copy(kit_copy, "refresh", "--check")

    assert result.returncode == 0
    assert "warning" in result.stdout.lower()
    assert "someone-elses-version" not in result.stdout
    assert (kit_copy / "VERSION").read_text(encoding="utf-8").strip() == "local-test-version"


# --------------------------------------------------------------------------- #
# The go-cloud gate (--cloud-ready)
# --------------------------------------------------------------------------- #


def test_cloud_ready_promotes_readiness_advice_to_errors(scaffolded_agent: Path) -> None:
    """Plain validate keeps a fresh scaffold green; the go-cloud gate does not."""
    assert run_kit("validate", str(scaffolded_agent)).returncode == 0

    gated = run_kit("validate", str(scaffolded_agent), "--cloud-ready", "--json")
    assert gated.returncode == 1
    errors = json.loads(gated.stdout)["errors"]
    assert any("example_prompts" in error for error in errors), errors
    assert any("scaffold placeholder" in error for error in errors), errors

    make_cloud_ready(scaffolded_agent)
    assert run_kit("validate", str(scaffolded_agent), "--cloud-ready").returncode == 0


def test_export_applies_the_cloud_ready_gate(scaffolded_agent: Path, tmp_path: Path) -> None:
    refused = run_kit("export", str(scaffolded_agent), "--to", str(tmp_path / "out"))
    assert refused.returncode == 1
    assert "does not validate" in refused.stderr

    make_cloud_ready(scaffolded_agent)
    accepted = run_kit("export", str(scaffolded_agent), "--to", str(tmp_path / "out"))
    assert accepted.returncode == 0, f"{accepted.stdout}\n{accepted.stderr}"


def test_cli_command_names_are_neither_dropped_nor_invented(
    kit_module, scaffolded_agent: Path
) -> None:
    """A line scan must still see an invalid name, and must ignore nested `name:` keys."""
    cli_commands = scaffolded_agent / "docs" / "CLI_COMMANDS.yaml"
    cli_commands.write_text(
        "commands:\n"
        "  - name: status\n"
        "    command: python scripts/update_status.py\n"
        '  - name: "check invoices"\n'
        "    command: python scripts/x.py\n"
        "  - name: sync  # trailing comment\n"
        "    command: python scripts/z.py\n"
        "    args:\n"
        "      - name: since\n",
        encoding="utf-8",
    )
    assert kit_module.cli_command_names(cli_commands) == ["status", "check invoices", "sync"]

    result = run_kit("validate", str(scaffolded_agent), "--json")
    errors = json.loads(result.stdout)["errors"]
    assert any("check invoices" in error for error in errors), errors


def test_only_the_declared_prompts_are_required(scaffolded_agent: Path) -> None:
    """The schema makes each prompt optional; the tool must not invent requirements."""
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["prompts"] = {"workflow": "docs/WORKFLOW_PROMPT.md"}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (scaffolded_agent / "docs" / "ENTRYPOINT_PROMPT.md").unlink()
    (scaffolded_agent / "docs" / "REFINER_PROMPT.md").unlink()

    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 0
    assert json.loads(result.stdout)["errors"] == []


def test_kit_tool_declares_its_runtime_for_uv() -> None:
    """The kit runs on macOS whose system python3 is 3.9. `uv run` reads the PEP 723
    block to provision a 3.10+ interpreter, and a too-old bare python3 must point
    at uv rather than dead-end — the two halves of the same guarantee."""
    source = KIT_PY.read_text(encoding="utf-8")
    header = source.split('"""', 1)[0]
    assert "# /// script" in header
    assert 'requires-python = ">=3.10"' in header
    assert "# ///" in header
    assert "uv run .cinna-kit/tools/kit.py" in source
    assert "astral.sh/uv/install.sh" in source
