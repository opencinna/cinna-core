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
this module reads the kit from the synced snapshot at
`app/env-templates/platform-knowledge-env/.../knowledge/local-kit/`, which
`make sync-platform-knowledge` writes. Set `LOCAL_AGENT_KIT_DIR` to point it at
any other copy — `_find_kit_dir` reads it, so naming that variable IS positive
evidence about which bytes a run exercised (unlike the backend service path,
which never reads it). A run against a stale snapshot reports green about a kit
nobody edited, so say which copy was tested.

Two capabilities this module needs are not present in every environment, and
both are detected by PROVOKING them rather than inferred from `os.geteuid()` or
a platform name — an absence of complaints is evidence only once you have shown
the tool would have complained:

* **file permissions.** The backend container runs as root, where `chmod 000`
  does not block a read, so every permission case here would pass while testing
  nothing. `require_enforced_permissions` makes an unreadable file and tries to
  read it, and skips when the read succeeds.
* **loopback sockets.** The `chat` scenarios bind `127.0.0.1:0`.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
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


ANY_TOKEN_RE = re.compile(r"\{\{[^{}]*\}\}")
UPPER_SNAKE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# A guard that stopped guarding WITHOUT failing, and the reason it is written this
# way rather than as a regex.
#
# This slot used to hold `PLATFORM_TOKEN_RE = ^\{\{[A-Z][A-Z0-9_]*\}\}$`, used to
# EXEMPT "platform-rendered" tokens from the leftover-scaffold-token scan on the
# pre-Phase-6 premise that UPPER_SNAKE meant platform. Phase 6 made the scaffold
# tokens UPPER_SNAKE too, so an unsubstituted `{{NAME}}` in a scaffolded agent
# matched the exemption and PASSED the check written precisely to catch it. It did
# not go red; it silently stopped matching, which is why it needed finding rather
# than fixing.
#
# Shape cannot separate the two classes, and neither can membership. `kit.py`'s
# `SCAFFOLD_TOKENS` comment is the upstream authority and says so in as many
# words: "A list cannot classify a member it contains, and this one contains
# `KIT_VERSION`. What settles the question is PROVENANCE." `KIT_VERSION` is in
# both sets on purpose — the platform fills it when it renders a kit for
# download, and `new` fills it when the kit was never rendered.
#
# So this scan asks the provenance question instead of a shape question, and asks
# it of `kit.py` rather than restating its answer:
#
#   * the six tokens `new` alone fills must never survive `kit.py new`;
#   * `KIT_VERSION`'s fate depends on where THIS kit came from — `kit_version()`
#     returns None for an unrendered checkout, and `substitute_tokens` documents
#     that a None value is left unsubstituted so "a kit that never learned its own
#     version ships a visible KIT_VERSION token rather than a plausible wrong one";
#   * any other token found must be one the kit itself still carries unrendered,
#     i.e. the platform's to fill — never something `new` invented.
#
# Do not reintroduce a regex here. A pattern over the token's spelling is exactly
# the instrument this class of defect defeats.


def scaffold_tokens_new_must_fill(kit_module) -> set[str]:
    """The literal `{{TOKEN}}`s `kit.py new` is responsible for on THIS kit.

    Derived from `kit_module`, never restated: an edit to `SCAFFOLD_TOKENS` is an
    edit to this set.
    """
    tokens = {kit_module.scaffold_token(name) for name in kit_module.SCAFFOLD_TOKENS}
    if kit_module.kit_version() is None:
        # Unrendered kit (a repo checkout): KIT_VERSION is the platform's here,
        # and `new` deliberately leaves it visible rather than guessing.
        tokens.discard(kit_module.scaffold_token("KIT_VERSION"))
    return tokens


def tokens_in_tree(root: Path) -> dict[str, list[str]]:
    """Every `{{...}}` sequence under `root`, keyed by root-relative path."""
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        tokens = ANY_TOKEN_RE.findall(text)
        if tokens:
            found[path.relative_to(root).as_posix()] = tokens
    return found


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
    """A fresh scaffold is contract-stamped, not schema_version-stamped.

    This used to assert `manifest["schema_version"] == 1`. Phase 7 replaced that
    gate wholesale — `_validate_identity` "replaced the old `schema_version` gate
    ... nothing branches on its value" — and the template dropped the key, so the
    assertion became a `KeyError`.

    The replacement is the OPPOSITE assertion rather than a deletion: the key is
    absent AND the three identity fields that now carry the gate are present and
    well formed. Deleting the line would have left a scaffold free to reintroduce
    a legacy `schema_version` — which `_validate_identity` treats as a pre-1.0.0
    folder when it appears without `contract_version` and `id`, i.e. exactly the
    warning path a NEW agent must never be born on.
    """
    manifest = json.loads((scaffolded_agent / "cinna-agent.json").read_text(encoding="utf-8"))
    assert manifest["slug"] == "invoice-watcher"
    assert manifest["name"] == "Invoice Watcher"
    assert "schema_version" not in manifest, (
        "the legacy gate is gone; a new scaffold carrying it would be read as a "
        "pre-1.0.0 folder by `_validate_identity`"
    )
    assert manifest["contract_version"] == (KIT_DIR / "CONTRACT_VERSION").read_text(
        encoding="utf-8"
    ).strip()
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", manifest["id"]
    ), manifest["id"]
    assert manifest["created_at"].endswith("Z")
    # The ledger keys R4 moved out never start life in a scaffold.
    assert "publications" not in manifest
    assert "cloud" not in manifest

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


def test_no_unfilled_scaffold_tokens_remain(kit_module, scaffolded_agent: Path) -> None:
    """No token `kit.py new` owns survives `kit.py new`.

    Classified by PROVENANCE, never by shape — see the long note beside
    `scaffold_tokens_new_must_fill`. The guard this replaces exempted every
    UPPER_SNAKE token, which after Phase 6 is every scaffold token too, so an
    unsubstituted `{{NAME}}` passed it.
    """
    must_be_gone = scaffold_tokens_new_must_fill(kit_module)
    assert must_be_gone, "the scaffold-token set cannot be empty"

    found = tokens_in_tree(scaffolded_agent)
    leftovers = {
        relative: [token for token in tokens if token in must_be_gone]
        for relative, tokens in found.items()
    }
    leftovers = {relative: tokens for relative, tokens in leftovers.items() if tokens}
    assert leftovers == {}, f"unsubstituted scaffold tokens remain: {leftovers}"

    # Whatever DID survive must have come from the template `new` copies, and
    # never be something `new` invented on the way through.
    template_tokens = {
        token
        for tokens in tokens_in_tree(KIT_DIR / "templates" / "agent").values()
        for token in tokens
    }
    survivors = {token for tokens in found.values() for token in tokens}
    invented = sorted(survivors - template_tokens)
    assert invented == [], f"`new` produced tokens the template does not carry: {invented}"


def test_the_scaffold_token_guard_cannot_be_written_as_a_shape_test(kit_module) -> None:
    r"""Why the guard above asks `kit.py`, and why a regex here would be vacuous.

    Two concrete artefacts settle provenance without anyone having to be told:

    * `kit.json` is rendered by the PLATFORM before delivery, so every token in
      it is a platform token;
    * `templates/agent/cinna-agent.json` is what `kit.py new` copies and fills,
      so every token in it is (at least) a scaffold token.

    Three facts, each asserted rather than asserted-about, so a future editor who
    reaches for `^\{\{[A-Z][A-Z0-9_]*\}\}$` sees this fail first:

    1. every scaffold token is UPPER_SNAKE, so a shape test cannot exclude them;
    2. every platform token is UPPER_SNAKE too, so a shape test cannot single
       THEM out either — the two classes are shape-identical;
    3. `KIT_VERSION` sits in BOTH artefacts, so membership of `SCAFFOLD_TOKENS`
       does not classify it. Only provenance does.
    """
    assert all(UPPER_SNAKE_RE.match(name) for name in kit_module.SCAFFOLD_TOKENS)

    platform_tokens = set(ANY_TOKEN_RE.findall((KIT_DIR / "kit.json").read_text(encoding="utf-8")))
    scaffold_literals = {kit_module.scaffold_token(name) for name in kit_module.SCAFFOLD_TOKENS}
    template_tokens = set(
        ANY_TOKEN_RE.findall(
            (KIT_DIR / "templates" / "agent" / "cinna-agent.json").read_text(encoding="utf-8")
        )
    )

    assert platform_tokens, "kit.json must still carry unrendered platform tokens"
    assert all(UPPER_SNAKE_RE.match(token[2:-2]) for token in platform_tokens), sorted(
        platform_tokens
    )
    assert template_tokens <= scaffold_literals, sorted(template_tokens - scaffold_literals)

    # (3) is the one that closes the loop: the overlap is a real token in two
    # real files, not an assertion about a list.
    kit_version_token = kit_module.scaffold_token("KIT_VERSION")
    assert "KIT_VERSION" in kit_module.SCAFFOLD_TOKENS
    assert kit_version_token in platform_tokens
    assert kit_version_token in template_tokens


# --------------------------------------------------------------------------- #
# validate — failure modes
# --------------------------------------------------------------------------- #


def test_validate_demotes_an_empty_workflow_prompt_to_a_warning(
    scaffolded_agent: Path,
) -> None:
    """§9.2 severity parity: an empty prompt is a WARNING, and the finding still fires.

    This used to assert exit 1 and an entry in `errors`. Phase 7 demoted it, with
    the reason stated at the check: "the desktop treats an empty prompt as a
    warning and has no placeholder check at all ... calling it broken here would
    refuse a folder the desktop opens without comment."

    Not weakened to "exit 0": the severity IS the assertion. The finding must
    still be raised, it must be in `warnings`, and — the half that catches a
    silent re-promotion — it must NOT be in `errors`. A demotion that quietly
    dropped the check would pass an exit-code-only test.
    """
    (scaffolded_agent / "docs" / "WORKFLOW_PROMPT.md").write_text("", encoding="utf-8")
    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 0, "a kit-only finding must not fail a folder the desktop opens"
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert any(
        "WORKFLOW_PROMPT.md" in warning and "is empty" in warning
        for warning in payload["warnings"]
    ), payload["warnings"]
    assert not any("WORKFLOW_PROMPT.md" in error for error in payload["errors"]), payload[
        "errors"
    ]


def test_validate_demotes_an_unfilled_prompt_placeholder_to_a_warning(
    scaffolded_agent: Path,
) -> None:
    """The second half of the same demotion, and it has its own message."""
    (scaffolded_agent / "docs" / "WORKFLOW_PROMPT.md").write_text(
        "Do the thing for {{CUSTOMER}}.\n", encoding="utf-8"
    )
    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert any(
        "WORKFLOW_PROMPT.md" in warning and "placeholder" in warning
        for warning in payload["warnings"]
    ), payload["warnings"]
    assert payload["errors"] == []


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
    """The repair still repairs; the finding that triggers it is now a WARNING.

    This used to assert exit 1 on both un-fixed runs. `_validate_requirements` is
    now advisory throughout — "the desktop's validator deliberately does not port
    this check, so every finding here is a warning ... it is the one check with a
    repair, which is why demoting it costs nothing."

    Not weakened: the two exit-1 assertions become severity assertions on the
    same two conditions (drifted file, missing file), each naming the dependency,
    each checked absent from `errors`. The `--fix` half — the behaviour that
    actually matters — is unchanged and gained an assertion on the `fixed` entry,
    because a `--fix` that repaired silently would look identical to one that did
    nothing at all.
    """
    pyproject = scaffolded_agent / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace("dependencies = []", 'dependencies = ["requests>=2.31"]'),
        encoding="utf-8",
    )
    drifted = run_kit("validate", str(scaffolded_agent), "--json")
    assert drifted.returncode == 0, "an advisory check must never fail the run"
    payload = json.loads(drifted.stdout)
    assert any(
        "workspace_requirements.txt" in warning and "requests>=2.31" in warning
        for warning in payload["warnings"]
    ), payload["warnings"]
    assert not any("workspace_requirements.txt" in error for error in payload["errors"])

    fixed = run_kit("validate", str(scaffolded_agent), "--fix", "--json")
    assert fixed.returncode == 0
    repair = json.loads(fixed.stdout)
    assert any("workspace_requirements.txt" in item for item in repair["fixed"]), repair["fixed"]
    requirements = scaffolded_agent / "workspace_requirements.txt"
    assert "requests>=2.31" in requirements.read_text(encoding="utf-8")

    # And --fix must also create the file outright when it is missing entirely.
    requirements.unlink()
    missing = run_kit("validate", str(scaffolded_agent), "--json")
    assert missing.returncode == 0
    absent = json.loads(missing.stdout)
    assert any(
        "workspace_requirements.txt is missing" in warning for warning in absent["warnings"]
    ), absent["warnings"]
    assert absent["errors"] == []
    recreated = run_kit("validate", str(scaffolded_agent), "--fix", "--json")
    assert recreated.returncode == 0
    assert any(
        "workspace_requirements.txt created" in item
        for item in json.loads(recreated.stdout)["fixed"]
    )
    assert "requests>=2.31" in requirements.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #


def test_export_excludes_local_only_paths_and_copies_the_manifest_unchanged(
    cloud_ready_agent: Path, tmp_path: Path
) -> None:
    """C1: the assertion inverted with R4, and the NAME had to invert with it.

    This test was `..._clears_the_cloud_block`, and it asserted the export-time
    manifest rewrite R4 constraint 1 forbids: it wrote a populated `cloud` block
    into the source and asserted the exported copy came out nulled. `cmd_export`
    now carries a comment forbidding exactly that — "Export does NOT write the
    manifest, and must not be made to ... Do not reintroduce a rewrite to strip a
    deprecated `cloud` key from the copy either" — so the assertion becomes the
    opposite one: the exported manifest is BYTE-identical to the source.

    The rename is not cosmetic and is part of the fix. A test still named
    `..._clears_the_cloud_block` sends a future reader hunting for behaviour we
    deliberately removed; they find the removal comment in `cmd_export` and have
    two artefacts that disagree, with no way to tell which is current.

    This branch is the CLOUD-PRESENT one, and it can only ever exercise the
    branch where the key exists, because it writes the key in as setup (C3). The
    cloud-ABSENT branch — the one F6 lived in — is
    `test_export_of_an_unpublished_agent_keeps_the_manifest_byte_identical`.
    """
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

    # BYTE-identical, not merely equal-as-JSON: key order and whitespace are the
    # contract with a host that uploads the folder verbatim, and an equality over
    # parsed dicts would pass a re-serialisation that reordered the keys.
    assert (destination / "cinna-agent.json").read_bytes() == manifest_path.read_bytes()
    exported = json.loads((destination / "cinna-agent.json").read_text(encoding="utf-8"))
    assert exported["cloud"] == {
        "platform_url": "https://example.invalid",
        "agent_id": "11111111-1111-1111-1111-111111111111",
        "imported_at": "2026-01-01T00:00:00Z",
    }
    # The summary says so too, so the command and this test cannot drift apart.
    assert "the manifest is copied unchanged" in result.stdout

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


def test_template_manifest_matches_the_shipped_schema(
    kit_module, scaffolded_agent: Path
) -> None:
    """The template is now a TEMPLATE, so the schema check moved to its output.

    Phase 6 replaced the template's literal values with `{{SLUG}}`, `{{ID}}` and
    friends, so validating the raw template against the shipped schema now fails
    on the very patterns that make it a schema (`'{{SLUG}}' does not match
    '^[a-z0-9][a-z0-9-]{1,62}$'`).

    Not weakened by loosening the schema or skipping the check: the assertion
    moves one step downstream, from the template to the thing the template
    exists to produce. That is strictly MORE than before — it now covers both
    the template's shape and `kit.py new`'s substitution, and a `new` that filled
    a token with an invalid value would fail here where the old test could not
    have seen it.

    The raw template keeps a check of its own below, over the two properties a
    rendered instance cannot carry back: the key set, and the tokens' placement.
    """
    schema = json.loads((KIT_DIR / "schema" / "cinna-agent.schema.json").read_text(encoding="utf-8"))
    rendered = json.loads((scaffolded_agent / "cinna-agent.json").read_text(encoding="utf-8"))

    # importorskip, not try/except: losing jsonschema must show as a skip rather
    # than quietly reducing this test to the stdlib subset check below.
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(instance=rendered, schema=schema)

    # The stdlib subset validator the user's machine actually runs must agree.
    report = kit_module.Report()
    kit_module.validate_manifest(rendered, report)
    assert report.errors == [], report.errors


def test_template_manifest_declares_only_keys_the_schema_knows(kit_module) -> None:
    """The raw template's own contract: key set, and every token in place.

    `additionalProperties` is true in the shipped schema, so jsonschema alone
    would accept a template key nobody reads. This is the assertion that catches
    a key added to the template and to no schema — including a ledger key, which
    `cmd_new` also refuses at runtime.
    """
    template_path = KIT_DIR / "templates" / "agent" / "cinna-agent.json"
    raw = template_path.read_text(encoding="utf-8")
    template = json.loads(raw)
    schema = json.loads((KIT_DIR / "schema" / "cinna-agent.schema.json").read_text(encoding="utf-8"))

    unknown = sorted(set(template) - set(schema["properties"]))
    assert unknown == [], f"template keys the schema does not describe: {unknown}"

    # R4: the ledger keys are not schema properties any more, and `cmd_new`
    # raises rather than repairing if the template ever carries one again.
    assert "publications" not in template
    assert "publications" not in schema["properties"]
    assert "cloud" not in template

    # Every scaffold token `new` fills has a home in this file, and each is a
    # whole JSON string value rather than embedded in one — the desktop's
    # `buildManifest` sets these fields on the parsed object, so a token spliced
    # into a longer string would render differently on the two hosts.
    for name in kit_module.SCAFFOLD_TOKENS:
        token = kit_module.scaffold_token(name)
        if token not in raw:
            continue
        assert f'"{token}"' in raw, f"{token} must be a whole JSON string value"
    # Serialisation parity: 2-space indent, exactly one trailing newline.
    assert raw == kit_module._serialise(template)


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


# --------------------------------------------------------------------------- #
# `kit.py chat` — token secrecy and token egress (Phase 12, BLOCKING)
# --------------------------------------------------------------------------- #
#
# READ THIS BEFORE SIMPLIFYING ANYTHING BELOW.
#
# The two egress tests are written as DIFFERENTIALS — the guard removed from a
# scratch copy of `kit.py`, then the guard in place — and not as happy-path
# assertions, because **a test that only asserts the current code path passes
# proves nothing about the default it exists to defend against.**
#
# The defaults in question are library defaults, not bugs in this file:
#
# * `urllib`'s `HTTPRedirectHandler.redirect_request` copies the request headers
#   onto the redirected request, dropping only `Content-Length` and
#   `Content-Type` — so `Authorization: Bearer <agent_token>` survives a
#   cross-origin 302, and for a POST 301/302/303 are auto-followed as a GET;
# * a default opener honours `http_proxy` / `https_proxy` / `all_proxy` from the
#   environment, and `no_proxy` does not reliably cover loopback.
#
# A test that drives `chat` at a well-behaved stub and checks the answer passes
# **identically** with `_NoChatRedirect()` and `ProxyHandler({})` deleted from
# `_chat_opener`. That deletion is exactly the edit these tests exist to stop, so
# a run that cannot distinguish the two states is not evidence about either.
#
# The leak's symptom is the reason this outranks the rest of the file: with the
# redirect guard removed, `chat` does not fail — it exits 0 and prints the
# foreign host's answer. A silent, successful-looking run that has posted a live
# bearer token to somebody else's server.
#
# The shape of each differential, twice per guard against ONE stub topology:
#   guard removed ⇒ the recording foreign host / proxy RECEIVES the bearer token;
#   guard present ⇒ exit non-zero, and the recording host receives no request at
#                   all — "nothing was sent", asserted on the recorder, not on
#                   the absence of the token from our own output.
#
# `_kit_copy_without_guard` asserts the anchor was found and that the text
# actually changed before the copy is used: a reverted-copy differential that
# measured the unmodified file would report a comfortable zero-leaks result.


# A value that exists nowhere else in this repo, so finding it in a recorder or
# in captured output is unambiguous.
MARKER_TOKEN = "kitpy-egress-marker-8f3a1c5d9e"
# Every other `desktop.json` value, each equally distinctive. D3: a tool never
# prints the state file's contents, and these are contents.
MARKER_CHAT_PATH = "/kitpy-chatpath-4b7e"
MARKER_EXTRA_VALUES = {
    "window_title": "kitpy-desktop-title-1a2b",
    "user_email": "kitpy-desktop-user-3c4d@example.invalid",
    "workspace_id": "kitpy-desktop-workspace-5e6f",
}


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Records the request, then hands the response to the stub's responder."""

    protocol_version = "HTTP/1.0"

    def _handle(self) -> None:
        stub = self.server.stub  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        stub.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body.decode("utf-8", "replace"),
            }
        )
        stub.respond(self)

    do_POST = _handle
    do_GET = _handle
    do_CONNECT = _handle

    def log_message(self, *args) -> None:  # noqa: ARG002 - keep pytest output clean
        return


class _Stub:
    """A recording loopback HTTP server. `requests` is the evidence."""

    def __init__(self, responder) -> None:
        self.requests: list[dict] = []
        self._responder = responder
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.server.stub = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def respond(self, handler: _StubHandler) -> None:
        self._responder(handler)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@contextlib.contextmanager
def stub_server(responder):
    stub = _Stub(responder)
    try:
        yield stub
    finally:
        stub.close()


def _send(handler: _StubHandler, status: int, body: bytes, content_type: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def ndjson_responder(*lines: dict):
    """Answer 200 with an NDJSON stream — the shape D10 describes."""

    def respond(handler: _StubHandler) -> None:
        body = "".join(json.dumps(line) + "\n" for line in lines).encode("utf-8")
        _send(handler, 200, body, "application/x-ndjson")

    return respond


def raw_responder(status: int, text: str, content_type: str = "application/x-ndjson"):
    def respond(handler: _StubHandler) -> None:
        _send(handler, status, text.encode("utf-8"), content_type)

    return respond


def redirect_responder(location):
    """302 to wherever `location()` says — the cross-origin hop."""

    def respond(handler: _StubHandler) -> None:
        handler.send_response(302)
        handler.send_header("Location", location())
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return respond


def closed_port() -> int:
    """A port nothing is listening on: bound, read back, released."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def write_desktop_state(
    kit_module,
    agent_dir: Path,
    *,
    base_url: str,
    token: str | None = MARKER_TOKEN,
    chat_path: str | None = MARKER_CHAT_PATH,
) -> Path:
    """The desktop-owned state file, holding a marker token and marker extras."""
    relative = kit_module.desktop_state_file()
    assert relative is not None, "this build cannot locate the desktop state file"
    path = agent_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict = {kit_module.DESKTOP_BASE_URL_KEY: base_url}
    if token is not None:
        document[kit_module.DESKTOP_TOKEN_KEY] = token
    if chat_path is not None:
        document[kit_module.DESKTOP_CHAT_PATH_KEY] = chat_path
    document.update(MARKER_EXTRA_VALUES)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def assert_state_file_never_printed(result: subprocess.CompletedProcess, base_url: str) -> None:
    """The three assertions §0 Bucket 20b owes a committed home, over stdout+stderr.

    All three, not one. The third — the `api_base_url` itself — is the one a
    reader drops: D3 says a tool never prints the state file's contents, and the
    URL *is* contents. Implement "never print the token" alone and the URL goes
    out in the next error line with the rule believed kept.
    """
    combined = result.stdout + result.stderr
    assert MARKER_TOKEN not in combined, combined
    for label, value in MARKER_EXTRA_VALUES.items():
        assert value not in combined, f"{label} leaked: {combined}"
    assert MARKER_CHAT_PATH not in combined, combined
    assert base_url not in combined, combined
    # The host:port on its own, in case only the path were stripped.
    assert base_url.split("//", 1)[1] not in combined, combined


def _kit_copy_without_guard(tmp_path: Path, name: str, anchor: str, replacement: str) -> Path:
    """A scratch kit whose `kit.py` has exactly one guard removed.

    Returns the copy's `kit.py`. The working tree is never edited.

    The three assertions are the instrument, not ceremony: §0 Bucket 21f records
    a reverted-copy differential that threw before writing, measured the
    UNMODIFIED file, and reported a comfortable zero-crashes result. **A
    reverted-copy differential must prove the revert happened before it may
    report what the revert caused.**
    """
    root = tmp_path / name
    shutil.copytree(KIT_DIR, root, ignore=shutil.ignore_patterns("__pycache__"))
    target = root / "tools" / "kit.py"
    source = target.read_text(encoding="utf-8")
    assert source.count(anchor) == 1, f"anchor not found exactly once: {anchor!r}"
    patched = source.replace(anchor, replacement)
    assert patched != source, "the guard removal changed nothing"
    target.write_text(patched, encoding="utf-8")
    readback = target.read_text(encoding="utf-8")
    assert anchor not in readback, "the guard is still present in the scratch copy"
    assert replacement in readback, "the patched opener is not in the scratch copy"
    return target


OPENER_ANCHOR = "urllib.request.ProxyHandler({}), _NoChatRedirect()"


def run_kit_py(kit_py: Path, *args: str, env: dict[str, str] | None = None):
    environment = dict(os.environ)
    environment.pop("http_proxy", None)
    environment.pop("HTTP_PROXY", None)
    environment.pop("https_proxy", None)
    environment.pop("HTTPS_PROXY", None)
    environment.pop("all_proxy", None)
    environment.pop("ALL_PROXY", None)
    environment.pop("no_proxy", None)
    environment.pop("NO_PROXY", None)
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, str(kit_py), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environment,
    )


@pytest.fixture()
def chat_agent(kit_module, scaffolded_agent: Path) -> Path:
    """A scaffolded agent, ready to have a desktop state file written into it."""
    return scaffolded_agent


def test_chat_refuses_a_cross_origin_redirect_and_the_guard_is_load_bearing(
    kit_module, chat_agent: Path, tmp_path: Path
) -> None:
    """Differential — `_NoChatRedirect` removed, then present, one topology.

    See the section note above for why this is not a happy-path assertion. The
    reverted half is the whole evidence: without it, this file would assert only
    that a well-behaved stub gets a good answer, which stays true after the guard
    is deleted.
    """
    foreign_answer = "answer-from-the-foreign-host-b7d2"
    with stub_server(ndjson_responder({"text": foreign_answer})) as foreign:
        with stub_server(redirect_responder(lambda: f"{foreign.base_url}{MARKER_CHAT_PATH}")) as home:
            write_desktop_state(kit_module, chat_agent, base_url=home.base_url)

            # --- guard REMOVED -------------------------------------------------
            leaky = _kit_copy_without_guard(
                tmp_path, "kit-no-redirect-guard", OPENER_ANCHOR, "urllib.request.ProxyHandler({})"
            )
            leaked = run_kit_py(leaky, "chat", str(chat_agent), "ping")

            assert len(foreign.requests) == 1, (
                "without the guard urllib must follow the 302 — if it did not, this "
                "differential is measuring nothing"
            )
            authorization = foreign.requests[0]["headers"].get("authorization", "")
            assert MARKER_TOKEN in authorization, foreign.requests[0]["headers"]
            # Sharper than "it leaks": the leaking run LOOKS like it worked.
            assert leaked.returncode == 0, f"{leaked.stdout}\n{leaked.stderr}"
            assert foreign_answer in leaked.stdout

            # --- guard PRESENT (the shipped kit) --------------------------------
            foreign.requests.clear()
            guarded = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping")

            assert guarded.returncode != 0, f"{guarded.stdout}\n{guarded.stderr}"
            assert foreign.requests == [], "the foreign host must receive nothing at all"
            assert foreign_answer not in guarded.stdout
            assert "302" in guarded.stderr, guarded.stderr
            assert_state_file_never_printed(guarded, home.base_url)


def test_chat_ignores_an_environment_proxy_and_the_guard_is_load_bearing(
    kit_module, chat_agent: Path, tmp_path: Path
) -> None:
    """Differential — `ProxyHandler({})` removed, then present, one topology.

    The target is a closed loopback port, so "guard present" is a refused
    connection: exit non-zero with nothing sent anywhere. "Guard removed" routes
    the same call through the recording proxy, token and all.
    """
    with stub_server(ndjson_responder({"text": "answer-through-the-proxy-3e91"})) as proxy:
        dead = closed_port()
        base_url = f"http://127.0.0.1:{dead}"
        write_desktop_state(kit_module, chat_agent, base_url=base_url)
        proxy_env = {"http_proxy": proxy.base_url, "HTTP_PROXY": proxy.base_url}

        # --- guard REMOVED -----------------------------------------------------
        leaky = _kit_copy_without_guard(
            tmp_path, "kit-no-proxy-guard", OPENER_ANCHOR, "_NoChatRedirect()"
        )
        leaked = run_kit_py(leaky, "chat", str(chat_agent), "ping", env=proxy_env)

        assert len(proxy.requests) == 1, (
            "without the guard urllib must honour http_proxy — if it did not, this "
            "differential is measuring nothing"
        )
        authorization = proxy.requests[0]["headers"].get("authorization", "")
        assert MARKER_TOKEN in authorization, proxy.requests[0]["headers"]
        assert leaked.returncode == 0, f"{leaked.stdout}\n{leaked.stderr}"

        # --- guard PRESENT (the shipped kit) -----------------------------------
        proxy.requests.clear()
        guarded = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping", env=proxy_env)

        assert guarded.returncode != 0, f"{guarded.stdout}\n{guarded.stderr}"
        assert proxy.requests == [], "the proxy must receive nothing at all"
        assert_state_file_never_printed(guarded, base_url)


def test_chat_streams_a_well_formed_answer(kit_module, chat_agent: Path) -> None:
    """Item 12 happy path — and, on its own, NOT evidence about either guard."""
    with stub_server(ndjson_responder({"text": "Invoices "}, {"delta": "checked."})) as desktop:
        write_desktop_state(kit_module, chat_agent, base_url=desktop.base_url)
        result = run_kit_py(KIT_PY, "chat", str(chat_agent), "check the invoices")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stdout == "Invoices checked.\n"
    assert len(desktop.requests) == 1
    sent = desktop.requests[0]
    assert sent["method"] == "POST"
    assert sent["path"] == MARKER_CHAT_PATH
    assert json.loads(sent["body"]) == {"prompt": "check the invoices"}
    assert sent["headers"]["authorization"] == f"Bearer {MARKER_TOKEN}"
    # Even a successful run prints none of the state file.
    assert_state_file_never_printed(result, desktop.base_url)


@pytest.mark.parametrize(
    "line",
    [
        {"type": "error", "message": "the agent is not running"},
        {"type": "error"},
    ],
)
def test_chat_fails_the_run_on_an_error_line(kit_module, chat_agent: Path, line: dict) -> None:
    """D10 step 3: `{"type": "error"}` goes to stderr and fails the run."""
    with stub_server(ndjson_responder(line)) as desktop:
        write_desktop_state(kit_module, chat_agent, base_url=desktop.base_url)
        result = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping")

    assert result.returncode == 1
    assert "error:" in result.stderr
    assert_state_file_never_printed(result, desktop.base_url)


def test_chat_reports_a_401_with_the_reissue_hint_only(kit_module, chat_agent: Path) -> None:
    with stub_server(raw_responder(401, "nope")) as desktop:
        write_desktop_state(kit_module, chat_agent, base_url=desktop.base_url)
        unauthorized = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping")
    assert unauthorized.returncode == 1
    assert "401" in unauthorized.stderr
    assert "toggle Connected off and on" in unauthorized.stderr
    assert_state_file_never_printed(unauthorized, desktop.base_url)

    # The hint is attached to 401 ONLY — advice that is wrong most of the times
    # it appears is worse than none.
    with stub_server(raw_responder(500, "boom")) as desktop:
        write_desktop_state(kit_module, chat_agent, base_url=desktop.base_url)
        broken = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping")
    assert broken.returncode == 1
    assert "500" in broken.stderr
    assert "toggle Connected off and on" not in broken.stderr
    assert_state_file_never_printed(broken, desktop.base_url)


def test_chat_reports_a_refused_connection_without_a_traceback(
    kit_module, chat_agent: Path
) -> None:
    base_url = f"http://127.0.0.1:{closed_port()}"
    write_desktop_state(kit_module, chat_agent, base_url=base_url)
    result = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping")

    assert result.returncode == 1
    assert "could not reach the desktop's local API" in result.stderr
    assert "Traceback" not in result.stderr
    assert_state_file_never_printed(result, base_url)


def test_chat_refuses_when_the_desktop_state_file_is_unusable(
    kit_module, chat_agent: Path
) -> None:
    """D10 step 1, each way the file can fail — one line, naming file and key only."""
    relative = kit_module.desktop_state_file()
    assert relative is not None
    state = chat_agent / relative
    state.parent.mkdir(parents=True, exist_ok=True)
    base_url = "http://127.0.0.1:59999"

    # (a) absent
    missing = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping")
    assert missing.returncode == 1
    assert "Cinna Desktop is not connected" in missing.stderr
    assert relative in missing.stderr
    assert "no such file" in missing.stderr

    # (b) not JSON — the parse position and the bytes must not appear
    state.write_text('{"' + kit_module.DESKTOP_TOKEN_KEY + '": ' + MARKER_TOKEN, encoding="utf-8")
    broken = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping")
    assert broken.returncode == 1
    assert "unreadable, or not valid JSON" in broken.stderr
    assert MARKER_TOKEN not in broken.stdout + broken.stderr
    assert "Traceback" not in broken.stderr

    # (c) an empty token is refused exactly like a missing one — an asymmetry
    #     here is how a blank token becomes `Authorization: Bearer `.
    write_desktop_state(kit_module, chat_agent, base_url=base_url, token="   ")
    blank = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping")
    assert blank.returncode == 1
    assert kit_module.DESKTOP_TOKEN_KEY in blank.stderr
    assert_state_file_never_printed(blank, base_url)

    # (d) a `chat_path` that is present but unusable REFUSES rather than
    #     defaulting: a 200 from the wrong endpoint is a failure nobody notices.
    write_desktop_state(kit_module, chat_agent, base_url=base_url, chat_path="")
    bad_path = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping")
    assert bad_path.returncode == 1
    assert kit_module.DESKTOP_CHAT_PATH_KEY in bad_path.stderr

    # (e) a non-http base_url is refused before `Request()` can quote it back
    write_desktop_state(kit_module, chat_agent, base_url="file:///etc/passwd")
    scheme = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping")
    assert scheme.returncode == 1
    assert kit_module.DESKTOP_BASE_URL_KEY in scheme.stderr
    assert "/etc/passwd" not in scheme.stdout + scheme.stderr


def test_chat_fails_loudly_when_no_line_carried_a_recognised_text_key(
    kit_module, chat_agent: Path
) -> None:
    """The tolerance seam's own alarm — a silent empty success is the worst answer."""
    with stub_server(ndjson_responder({"message": "hello"}, {"chunk": "there"})) as desktop:
        write_desktop_state(kit_module, chat_agent, base_url=desktop.base_url)
        result = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "none carrying a recognised text key" in result.stderr
    assert "message" in result.stderr and "chunk" in result.stderr
    assert_state_file_never_printed(result, desktop.base_url)


def test_chat_fails_the_run_on_an_unparseable_line_rather_than_skipping_it(
    kit_module, chat_agent: Path
) -> None:
    """A skipped line prints a shorter answer that still looks complete."""
    with stub_server(raw_responder(200, '{"text": "half "}\nnot-json\n')) as desktop:
        write_desktop_state(kit_module, chat_agent, base_url=desktop.base_url)
        result = run_kit_py(KIT_PY, "chat", str(chat_agent), "ping")

    assert result.returncode == 1
    assert "is not JSON" in result.stderr
    assert_state_file_never_printed(result, desktop.base_url)


def test_chat_refuses_an_empty_prompt_before_touching_the_network(
    kit_module, chat_agent: Path
) -> None:
    with stub_server(ndjson_responder({"text": "should never be reached"})) as desktop:
        write_desktop_state(kit_module, chat_agent, base_url=desktop.base_url)
        result = run_kit_py(KIT_PY, "chat", str(chat_agent), "   ")
        assert result.returncode == 1
        assert "prompt is empty" in result.stderr
        assert desktop.requests == []


def test_list_reports_desktop_connectivity_without_sending_anything(
    kit_module, chat_agent: Path, tmp_path: Path
) -> None:
    """Phase 9's DESKTOP column is `cmd_chat`'s own preflight, and posts nothing."""
    with stub_server(ndjson_responder({"text": "unused"})) as desktop:
        write_desktop_state(kit_module, chat_agent, base_url=desktop.base_url)
        listed = run_kit_py(KIT_PY, "list", "--root", str(tmp_path))
        assert listed.returncode == 0
        assert "DESKTOP" in listed.stdout
        assert desktop.requests == [], "`list` must never POST anywhere"
        assert_state_file_never_printed(listed, desktop.base_url)


# --------------------------------------------------------------------------- #
# Pattern semantics — `is_excluded` / `matches_pattern` (Phase 12 item 1)
# --------------------------------------------------------------------------- #
#
# Table-driven and mandatory: both hosts hash the file set these patterns select,
# and a one-file difference makes the two digests disagree forever while every
# individual step still looks like it worked. Nothing else in this file would
# notice, which is what "this fails silently otherwise" means.

PATTERN_CASES = [
    # (pattern, path, expected, why)
    ("README.md", "README.md", True, "root anchoring: the agent's own README"),
    ("README.md", "docs/README.md", False, "root anchoring: never a nested README"),
    ("README.md", "scripts/README.md", False, "root anchoring: never a nested README"),
    ("app-data/", "app-data", True, "directory pattern matches the directory itself"),
    ("app-data/", "app-data/storage/x", True, "directory pattern matches the subtree"),
    ("app-data/", "app-datax", False, "directory pattern is not a prefix match"),
    ("app-data", "app-data/storage/x", False, "no trailing slash: no subtree"),
    ("app-data", "app-data", True, "no trailing slash: the entry itself"),
    ("**/.env", ".env", True, "`**` spans ZERO segments"),
    ("**/.env", "a/b/.env", True, "`**` spans many segments"),
    ("**/.env", "a/.envx", False, "`**` does not loosen the final segment"),
    ("**/*.env", "config/settings.env", True, "`*` inside the final segment"),
    ("*.md", "notes.md", True, "`*` matches inside one segment"),
    ("*.md", "docs/notes.md", False, "`*` never crosses a separator"),
    ("docs/*.md", "docs/notes.md", True, "`*` in a middle-anchored pattern"),
    ("docs/*.md", "docs/deep/notes.md", False, "`*` never crosses a separator"),
    ("a?.md", "ab.md", True, "`?` matches one character"),
    ("a?.md", "a/.md", False, "`?` never matches a separator"),
    ("a?.md", "abc.md", False, "`?` matches exactly one character"),
    ("./README.md", "README.md", True, "`./` is normalised off the PATTERN"),
    ("README.md", "./README.md", True, "`./` is normalised off the PATH"),
    ("docs\\README.md", "docs/README.md", True, "backslashes are normalised in the pattern"),
    ("docs/README.md", "docs\\README.md", True, "backslashes are normalised in the path"),
    ("/README.md", "README.md", True, "a leading `/` is normalised off"),
    ("./app-data/", "app-data/storage/x", True, "the directory branch is chosen from the RAW pattern"),
    ("./app-data/", "app-data", True, "…while the body is matched after normalisation"),
    ("**/", "a/b", True, "a bare `**/` directory pattern reaches every subtree"),
]


@pytest.mark.parametrize(
    ("pattern", "path", "expected", "why"),
    PATTERN_CASES,
    ids=[f"{p}~{q}" for p, q, _, _ in PATTERN_CASES],
)
def test_is_excluded_pattern_semantics(kit_module, pattern, path, expected, why) -> None:
    assert kit_module.is_excluded(path, [pattern]) is expected, why
    # `matches_pattern` is the single-pattern primitive `is_excluded` ORs over;
    # asserting both keeps a future `is_excluded` rewrite honest.
    assert kit_module.matches_pattern(pattern, path) is expected, why


def test_trailing_whitespace_selects_the_directory_branch_but_not_the_body(
    kit_module,
) -> None:
    """A recorded ASYMMETRY in `matches_pattern`, asserted so it cannot drift silently.

    The directory branch is selected by `pattern.rstrip().endswith("/")`, which
    tolerates trailing whitespace; `normalize_rel_path` strips only `/`, so the
    whitespace survives into the pattern BODY as a segment of its own. A pattern
    written `"app-data/ "` therefore takes the directory branch and then matches
    nothing at all.

    This is latent rather than live — no shipped `cloud_import_excludes` entry has
    trailing whitespace, and this test asserts that too — but the two halves of
    one decision disagree about what "trailing" means, so a hand-edited contract
    would silently drop a directory from the exclude set and change the hash.
    Reported, not repaired: this file does not own `kit.py`.
    """
    assert kit_module.matches_pattern("app-data/ ", "app-data/storage/x") is False
    assert kit_module.matches_pattern("app-data/", "app-data/storage/x") is True

    layout = json.loads((KIT_DIR / "layout.json").read_text(encoding="utf-8"))
    untrimmed = [p for p in layout["cloud_import_excludes"] if p != p.strip()]
    assert untrimmed == [], f"contract patterns with stray whitespace: {untrimmed}"


def test_pattern_matching_is_anchored_by_fullmatch_not_by_dollar(kit_module) -> None:
    """A filename ending in a newline is legal, and `$` would drop it here only.

    Python's `$` also matches just before a trailing newline; JavaScript's does
    not. With `$`, `README.md` would drop `README.md\\n` on this host and keep it
    on the desktop — different file sets, different `content_hash`, drift that no
    publish can clear.
    """
    assert kit_module.matches_pattern("README.md", "README.md\n") is False


def test_question_mark_consumes_one_utf16_code_unit(kit_module) -> None:
    """`?` compiles to JavaScript's `[^/]`, which is ONE UTF-16 code unit.

    A non-BMP character is two units there and one code point here, so a
    code-point-based `?` would disagree with the desktop about a single file.
    """
    assert kit_module.matches_pattern("??.md", "\U0001F600.md") is True
    assert kit_module.matches_pattern("?.md", "\U0001F600.md") is False


# --------------------------------------------------------------------------- #
# content_hash / hash_export_files (Phase 12 item 2, C4)
# --------------------------------------------------------------------------- #


def expected_digest(agent_dir: Path, relatives: list[str]) -> str:
    """The D7 digest, computed from the SPEC rather than from `kit.py`.

    One line per file, `<relpath>` NUL `<sha256 hex of the bytes>` LF, in UTF-16
    code-unit order, fed into one running SHA-256. Written out here on purpose:
    asserting against `hash_export_files` would only prove the function equals
    itself.
    """
    digest = hashlib.sha256()
    for relative in sorted(relatives, key=lambda text: text.encode("utf-16-be", "surrogatepass")):
        entry = hashlib.sha256((agent_dir / relative).read_bytes()).hexdigest()
        digest.update(f"{relative}\0{entry}\n".encode("utf-8", "surrogatepass"))
    return f"sha256:{digest.hexdigest()}"


@pytest.fixture()
def hash_tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    return root


def test_content_hash_of_an_empty_tree(kit_module, hash_tree: Path) -> None:
    assert kit_module.content_hash(hash_tree, []) == f"sha256:{hashlib.sha256().hexdigest()}"


def test_content_hash_of_one_file(kit_module, hash_tree: Path) -> None:
    (hash_tree / "a.txt").write_text("hello\n", encoding="utf-8")
    assert kit_module.content_hash(hash_tree, []) == expected_digest(hash_tree, ["a.txt"])


def test_content_hash_sorts_in_utf16_code_unit_order(kit_module, hash_tree: Path) -> None:
    """The sort order is part of the hash, and it is JavaScript's, not Python's.

    `\\U0001F600` is one code point above `\\uFF21` but its first UTF-16 code unit
    (`\\uD83D`) is below it, so the two orders disagree on exactly this pair. Plain
    `sorted()` would reorder two lines and change the digest, and the only symptom
    would be a host reporting "unpublished changes" forever.
    """
    emoji = "\U0001F600.txt"
    fullwidth = "Ａ.txt"
    (hash_tree / emoji).write_text("one\n", encoding="utf-8")
    (hash_tree / fullwidth).write_text("two\n", encoding="utf-8")

    assert sorted([emoji, fullwidth]) == [fullwidth, emoji]
    assert sorted(
        [emoji, fullwidth], key=lambda t: t.encode("utf-16-be", "surrogatepass")
    ) == [emoji, fullwidth]

    assert kit_module.content_hash(hash_tree, []) == expected_digest(
        hash_tree, [emoji, fullwidth]
    )


def test_content_hash_never_covers_an_excluded_directory(kit_module, hash_tree: Path) -> None:
    (hash_tree / "keep.txt").write_text("keep\n", encoding="utf-8")
    (hash_tree / "credentials").mkdir()
    (hash_tree / "credentials" / "secret.txt").write_text("SECRET\n", encoding="utf-8")

    patterns = kit_module._with_always_excluded(list(kit_module.DEFAULT_EXCLUDES))
    assert kit_module.collect_export_files(hash_tree, patterns) == ["keep.txt"]
    assert kit_module.content_hash(hash_tree, patterns) == expected_digest(hash_tree, ["keep.txt"])


def test_content_hash_neither_follows_nor_lists_a_symlink(kit_module, hash_tree: Path) -> None:
    """A folder that travels must not be able to reach outside itself."""
    (hash_tree / "keep.txt").write_text("keep\n", encoding="utf-8")
    outside = hash_tree.parent / "outside.txt"
    outside.write_text("OUTSIDE\n", encoding="utf-8")
    try:
        (hash_tree / "link.txt").symlink_to(outside)
        (hash_tree / "linkdir").symlink_to(hash_tree.parent, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
        pytest.skip("this filesystem does not support symlinks")

    assert kit_module.collect_export_files(hash_tree, []) == ["keep.txt"]
    assert kit_module.content_hash(hash_tree, []) == expected_digest(hash_tree, ["keep.txt"])


def test_always_exclude_changes_no_files_fate(kit_module) -> None:
    """Item 3: the `ALWAYS_EXCLUDE` append must be a no-op against the shipped contract.

    This is the guard that keeps the EXPORTED set and the HASHED set identical.
    A pattern here that *added* something would make the exported tree a different
    set from the hashed one, and the hash is what tells a host whether the cloud
    copy is current.
    """
    contract = kit_module.contract_exclude_patterns()
    assert contract is not None, "the shipped layout.json must provide cloud_import_excludes"
    for pattern in kit_module.ALWAYS_EXCLUDE:
        assert pattern in contract, f"{pattern} is not covered by the shipped contract list"
    assert kit_module._with_always_excluded(contract) == contract


def test_default_excludes_is_content_identical_to_layout_json(kit_module) -> None:
    """Item 5: same patterns, same ORDER. A diverging fallback is a silent hash break."""
    layout = json.loads((KIT_DIR / "layout.json").read_text(encoding="utf-8"))
    assert kit_module.DEFAULT_EXCLUDES == layout["cloud_import_excludes"]


@pytest.mark.parametrize(
    ("relative", "travels", "why"),
    [
        (".env.example", True, "§1 correction 4: an example file carries no value"),
        ("docs/.env.example", True, "the `unless` clause is basename-only, at any depth"),
        ("prod.env", False, "§1 correction 5: `<name>.env` is a dotenv shape"),
        (".env.local", False, "a suffixed dotenv is still a dotenv"),
        ("config/settings.env", False, "at any depth"),
        (".env", False, "the plain case"),
    ],
)
def test_which_dotenv_shapes_travel(
    kit_module, hash_tree: Path, relative: str, travels: bool, why: str
) -> None:
    """Item 4, both halves. Globs cannot express this rule; `secret_files` can."""
    target = hash_tree / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("TOKEN=x\n", encoding="utf-8")

    patterns = kit_module._with_always_excluded(list(kit_module.DEFAULT_EXCLUDES))
    files = kit_module.collect_export_files(hash_tree, patterns)
    assert (relative in files) is travels, f"{why}: {files}"


def test_version_authority_is_one_value_in_three_places(kit_module) -> None:
    """Item 6: `CONTRACT_VERSION`, `kit.json` and `layout.json` must agree."""
    stamped = (KIT_DIR / "CONTRACT_VERSION").read_text(encoding="utf-8").strip()
    kit_json = json.loads((KIT_DIR / "kit.json").read_text(encoding="utf-8"))
    layout = json.loads((KIT_DIR / "layout.json").read_text(encoding="utf-8"))

    assert stamped == "1.0.0"
    assert kit_json["contract_version"] == stamped
    assert layout["contract_version"] == stamped
    assert kit_module.contract_version() == stamped
    # D17: `kit.json` no longer carries the vestigial integer.
    assert "schema_version" not in kit_json


# --------------------------------------------------------------------------- #
# Manifest serialisation parity (Phase 12 item 7)
# --------------------------------------------------------------------------- #


def test_serialisation_matches_json_stringify_null_2(kit_module, tmp_path: Path) -> None:
    """2-space indent, one trailing newline, insertion order, `ensure_ascii=False`.

    Key order is part of the byte-identity contract with the desktop, so every
    writer in `kit.py` goes through `_serialise` rather than calling `json.dumps`
    with its own arguments.
    """
    document = {"zebra": 1, "alpha": {"inner": "café ✅"}, "list": [1, 2]}
    text = kit_module._serialise(document)

    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert '\n  "zebra": 1,' in text
    assert "café ✅" in text, "ensure_ascii=False: non-ASCII stays raw"
    assert list(json.loads(text)) == ["zebra", "alpha", "list"], "insertion order, not sorted"


def test_write_manifest_round_trips_unknown_keys_and_order(
    kit_module, scaffolded_agent: Path
) -> None:
    """Unknown keys survive a read/write round trip, in place."""
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["a_future_key"] = {"kept": True}
    order_before = list(manifest)

    kit_module.write_manifest(scaffolded_agent, manifest)

    written = manifest_path.read_text(encoding="utf-8")
    assert list(json.loads(written)) == order_before
    assert json.loads(written)["a_future_key"] == {"kept": True}
    # Idempotent: a second write of what was read changes no bytes.
    kit_module.write_manifest(scaffolded_agent, json.loads(written))
    assert manifest_path.read_text(encoding="utf-8") == written


# --------------------------------------------------------------------------- #
# `new --json` (Phase 12 item 8)
# --------------------------------------------------------------------------- #


def test_new_json_emits_the_declared_fields_and_nothing_else(tmp_path: Path) -> None:
    result = run_kit("new", "json-agent", "--json", "--root", str(tmp_path))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)  # the whole of stdout, not a scrape
    assert sorted(payload) == ["contract_version", "id", "path", "slug"]
    assert payload["slug"] == "json-agent"
    assert payload["path"] == str(tmp_path / "Local" / "json-agent")
    assert payload["contract_version"] == (KIT_DIR / "CONTRACT_VERSION").read_text(
        encoding="utf-8"
    ).strip()


def test_two_scaffolds_differ_only_in_id_and_created_at(tmp_path: Path) -> None:
    """The §9.1 property, testable on our side alone.

    Everything a second host would have to reproduce byte-for-byte is identical;
    only the two fields that are *supposed* to be per-instance differ.
    """
    volatile = {"id", "created_at"}
    manifests = []
    for index in (1, 2):
        root = tmp_path / f"run{index}"
        result = run_kit(
            "new", "twin-agent", "--name", "Twin", "--description", "Same text.",
            "--json", "--root", str(root),
        )
        assert result.returncode == 0, result.stderr
        agent = Path(json.loads(result.stdout)["path"])
        manifests.append((agent, json.loads((agent / "cinna-agent.json").read_text(encoding="utf-8"))))

    (first_dir, first), (second_dir, second) = manifests
    assert list(first) == list(second), "key order is part of the contract"
    assert first["id"] != second["id"]
    assert set(first) - volatile == set(second) - volatile
    for key in set(first) - volatile:
        assert first[key] == second[key], key

    # And every other scaffolded file is byte-identical between the two runs.
    for path in sorted(first_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(first_dir)
        if relative.as_posix() == "cinna-agent.json":
            continue
        assert path.read_bytes() == (second_dir / relative).read_bytes(), relative


# --------------------------------------------------------------------------- #
# The contract gate (Phase 12 item 9)
# --------------------------------------------------------------------------- #


def stamp_contract_version(agent_dir: Path, value) -> None:
    manifest_path = agent_dir / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if value is None:
        manifest.pop("contract_version", None)
    else:
        manifest["contract_version"] = value
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def test_contract_gate_refuses_a_newer_major(scaffolded_agent: Path) -> None:
    stamp_contract_version(scaffolded_agent, "2.0.0")
    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any("needs contract 2.x" in error for error in payload["errors"]), payload["errors"]
    assert not any("needs contract 2.x" in warning for warning in payload["warnings"])


def test_contract_gate_warns_on_an_older_major(scaffolded_agent: Path) -> None:
    stamp_contract_version(scaffolded_agent, "0.9.0")
    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 0, "an older major is migratable, not broken"
    payload = json.loads(result.stdout)
    assert any("can be migrated" in warning for warning in payload["warnings"]), payload["warnings"]
    assert payload["errors"] == []


def test_contract_gate_is_silent_on_a_minor_or_patch_difference(scaffolded_agent: Path) -> None:
    """Only the MAJOR decides — minor releases are additive by definition."""
    for version in ("1.4.0", "1.0.7", "1.9.9-rc.1"):
        stamp_contract_version(scaffolded_agent, version)
        result = run_kit("validate", str(scaffolded_agent), "--json")
        assert result.returncode == 0, version
        payload = json.loads(result.stdout)
        compat = [
            message
            for message in payload["errors"] + payload["warnings"] + payload["info"]
            if "contract" in message and ("runs on" in message or "migrated" in message)
        ]
        assert compat == [], f"{version} must be silent: {compat}"


def test_a_legacy_folder_is_warned_and_re_stampable_not_rejected(scaffolded_agent: Path) -> None:
    """`schema_version` alone ⇒ warning + exit 0. It is read, reported, never rejected."""
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("contract_version", None)
    manifest.pop("id", None)
    legacy = {"schema_version": 1}
    legacy.update(manifest)
    manifest_path.write_text(json.dumps(legacy, indent=2), encoding="utf-8")

    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert any("predates contract 1.0.0" in warning for warning in payload["warnings"]), payload
    # The early return exists so a legacy folder does NOT collect "required" errors
    # for the very keys the branch excuses.
    assert payload["errors"] == [], payload["errors"]


def test_an_unparseable_contract_version_is_an_error(scaffolded_agent: Path) -> None:
    stamp_contract_version(scaffolded_agent, "one-point-oh")
    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 1
    assert any(
        "must be a semantic version" in error for error in json.loads(result.stdout)["errors"]
    )


def test_a_legacy_key_beside_a_contract_version_is_INFO_only(scaffolded_agent: Path) -> None:
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert any("`schema_version` is legacy" in item for item in payload["info"]), payload["info"]
    assert not any("schema_version" in item for item in payload["errors"] + payload["warnings"])


# --------------------------------------------------------------------------- #
# `runtime.credential` secret lookalikes (Phase 12 item 10)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "credential",
    [
        "sk-" + "A" * 24,
        "sk_" + "A" * 24,
        "ghp_" + "A" * 24,
        "gho_" + "A" * 24,
        "xoxb-" + "A" * 24,
        "xoxa-" + "A" * 24,
        "xoxp-" + "A" * 24,
        "xoxr-" + "A" * 24,
        "xoxs-" + "A" * 24,
        "AIza" + "A" * 24,
        "AKIA" + "A" * 16,
        "x" * 201,
    ],
)
def test_runtime_credential_secret_lookalike_is_an_error(
    scaffolded_agent: Path, credential: str
) -> None:
    """Every prefix, and the >200-character rule.

    Deliberately stricter than the desktop's validator, and the comment argues
    it: "demoting a secret-leak guard to buy severity parity is the wrong trade,
    because the cost of the false positive is an edit and the cost of the false
    negative is a published key."
    """
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"] = {"model": "claude", "credential": credential}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any("looks like a secret value" in error for error in payload["errors"]), payload
    # The finding names the field, never the value.
    assert credential not in result.stdout


def test_a_plain_credential_reference_is_accepted(scaffolded_agent: Path) -> None:
    """The check judges shape, so it needs a negative case or it proves nothing."""
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"] = {"model": "claude", "credential": "anthropic"}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["errors"] == []


@pytest.mark.parametrize(
    "forbidden",
    [
        "value", "values", "secret", "password", "token", "api_key",
        "apikey", "client_secret", "private_key", "credential_data",
    ],
)
def test_a_credential_slot_may_never_carry_a_value_key(
    scaffolded_agent: Path, forbidden: str
) -> None:
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["credentials"] = [
        {"name": "inbox", "type": "api_key", forbidden: "pasted-secret-value-9c1f"}
    ]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 1
    assert any(
        f"contains a `{forbidden}` key" in error for error in json.loads(result.stdout)["errors"]
    )
    assert "pasted-secret-value-9c1f" not in result.stdout


# --------------------------------------------------------------------------- #
# Severity agreement — the whole point of §9.2 (Phase 12 item 11)
# --------------------------------------------------------------------------- #
#
# Each case asserts the SEVERITY, not merely that a finding fired. A demotion
# that silently dropped its check, and a re-promotion that silently failed a
# folder the desktop opens, both pass an exit-code-only test.


def test_an_unrecognised_credential_type_is_a_warning_not_an_error(
    scaffolded_agent: Path,
) -> None:
    """A closed enum would retroactively invalidate every folder using the first
    type the platform adds, on a machine that may be offline."""
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["credentials"] = [{"name": "inbox", "type": "a_type_from_next_year"}]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert any("does not recognise" in warning for warning in payload["warnings"]), payload
    assert payload["errors"] == []


def test_duplicate_credential_slot_names_are_a_warning(scaffolded_agent: Path) -> None:
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["credentials"] = [
        {"name": "inbox", "type": "api_key"},
        {"name": "inbox", "type": "api_key"},
    ]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert any("both named `inbox`" in warning for warning in payload["warnings"]), payload
    assert payload["errors"] == []


def test_a_misplaced_env_file_stays_an_ERROR(scaffolded_agent: Path) -> None:
    """The demotions are severity parity; the secret gate is not one of them."""
    (scaffolded_agent / "leaked.env").write_text("TOKEN=x\n", encoding="utf-8")
    result = run_kit("validate", str(scaffolded_agent), "--json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any("leaked.env" in error for error in payload["errors"]), payload
    assert not any("leaked.env" in warning for warning in payload["warnings"])


# --------------------------------------------------------------------------- #
# C2 — the cloud-ABSENT export branch, and C4 — the digest comes from the SOURCE
# --------------------------------------------------------------------------- #


def test_export_of_an_unpublished_agent_keeps_the_manifest_byte_identical(
    cloud_ready_agent: Path, tmp_path: Path
) -> None:
    """C2: the branch F6 lived in, which the cloud-PRESENT test structurally cannot reach.

    Its sibling writes a `cloud` key in as setup, so it could only ever exercise
    the key-present path. Both halves are asserted here — `cloud` is ABSENT from
    the export, and the exported KEY ORDER is unchanged. An absence assertion
    alone would have passed against a correctly-ordered reinsertion, which is
    exactly what F6 did.
    """
    manifest_path = cloud_ready_agent / "cinna-agent.json"
    source_bytes = manifest_path.read_bytes()
    source = json.loads(source_bytes)
    assert "cloud" not in source, "fixture precondition: this agent was never published"

    destination = tmp_path / "out"
    result = run_kit("export", str(cloud_ready_agent), "--to", str(destination))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    exported_bytes = (destination / "cinna-agent.json").read_bytes()
    exported = json.loads(exported_bytes)
    assert "cloud" not in exported
    assert list(exported) == list(source), "key order is the byte-identity contract"
    assert exported_bytes == source_bytes


def test_export_hashes_the_source_before_any_copy(
    kit_module, cloud_ready_agent: Path, tmp_path: Path
) -> None:
    """C4: the single property that makes our digest agree with the desktop's.

    `cmd_export` regenerates `workspace_requirements.txt` in the DESTINATION
    after copying, so a digest taken below the copy would cover different bytes.
    Nothing objected to that before this test existed — the property held only
    because the code happened to be written in that order.

    The probe: make the two trees provably differ on a travelling file. The
    printed digest must equal the SOURCE's, and must NOT equal the destination's.
    """
    pyproject = cloud_ready_agent / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            "dependencies = []", 'dependencies = ["requests>=2.31"]'
        ),
        encoding="utf-8",
    )
    # Left deliberately stale, so source and destination disagree after the copy.
    (cloud_ready_agent / "workspace_requirements.txt").write_text("", encoding="utf-8")

    destination = tmp_path / "out"
    result = run_kit(
        "export", str(cloud_ready_agent), "--to", str(destination), "--force", "--hash"
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    printed = result.stdout.strip().splitlines()[-1]
    assert printed.startswith("sha256:")

    patterns = kit_module._with_always_excluded(kit_module.contract_exclude_patterns())
    rules = kit_module.secret_file_rules()
    source_files = kit_module.collect_export_files(cloud_ready_agent, patterns, rules)
    assert printed == kit_module.content_hash(cloud_ready_agent, patterns, source_files, rules)

    regenerated = (destination / "workspace_requirements.txt").read_text(encoding="utf-8")
    assert "requests>=2.31" in regenerated, "precondition: the destination WAS edited"
    assert printed != kit_module.content_hash(destination, patterns, None, rules), (
        "the digest must describe the source, not the destination the command then edits"
    )


def test_export_hash_line_is_bare_and_last(cloud_ready_agent: Path, tmp_path: Path) -> None:
    """A conformance harness compares this without parsing the summary."""
    result = run_kit(
        "export", str(cloud_ready_agent), "--to", str(tmp_path / "out"), "--hash"
    )
    assert result.returncode == 0, result.stderr
    last = result.stdout.rstrip("\n").splitlines()[-1]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", last), last


# --------------------------------------------------------------------------- #
# C5 — `publications.json`, the ledger's own file
# --------------------------------------------------------------------------- #


LEDGER_ENTRY = {
    "platform_url": "https://cinna.example",
    "agent_id": "22222222-2222-2222-2222-222222222222",
    "imported_at": "2026-01-01T00:00:00Z",
}


def test_write_manifest_moves_the_content_hash_exactly_once(
    kit_module, scaffolded_agent: Path
) -> None:
    """The migration moves the folder's hash ONCE and is then stable.

    A single visible, explainable move is the opposite of the perpetual drift
    that storing the hash inside the hashed file produced.
    """
    manifest_path = scaffolded_agent / "cinna-agent.json"
    ledger_path = scaffolded_agent / "publications.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cloud"] = dict(LEDGER_ENTRY)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    kit_module.write_manifest(scaffolded_agent, json.loads(manifest_path.read_text("utf-8")))
    after_first = (manifest_path.read_bytes(), ledger_path.read_bytes())

    assert "cloud" not in json.loads(after_first[0])
    assert json.loads(after_first[1])["publications"] == [LEDGER_ENTRY]

    # Stable: a second migration of the migrated folder changes neither file.
    kit_module.write_manifest(scaffolded_agent, json.loads(manifest_path.read_text("utf-8")))
    assert (manifest_path.read_bytes(), ledger_path.read_bytes()) == after_first


def test_write_manifest_refuses_an_unreadable_ledger_and_writes_NEITHER_file(
    kit_module, scaffolded_agent: Path
) -> None:
    """A half-migration that reported success is the failure shape this removes."""
    manifest_path = scaffolded_agent / "cinna-agent.json"
    ledger_path = scaffolded_agent / "publications.json"
    ledger_path.write_text("{ this is not json", encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cloud"] = dict(LEDGER_ENTRY)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    manifest_before = manifest_path.read_bytes()
    ledger_before = ledger_path.read_bytes()

    with pytest.raises(kit_module.KitError) as raised:
        kit_module.write_manifest(scaffolded_agent, json.loads(manifest_before))
    assert "could not be read as a publication ledger" in str(raised.value)

    assert manifest_path.read_bytes() == manifest_before, "the manifest must not be written"
    assert ledger_path.read_bytes() == ledger_before, "the ledger must never be overwritten"


def test_a_write_that_absorbs_nothing_leaves_a_hand_edited_ledger_byte_unchanged(
    kit_module, scaffolded_agent: Path
) -> None:
    """Otherwise every `new`/`validate --fix` would reformat somebody's file."""
    ledger_path = scaffolded_agent / "publications.json"
    hand_written = '{\n    "publications": [\n        ' + json.dumps(LEDGER_ENTRY) + "\n    ]\n}\n"
    ledger_path.write_text(hand_written, encoding="utf-8")

    manifest_path = scaffolded_agent / "cinna-agent.json"
    kit_module.write_manifest(scaffolded_agent, json.loads(manifest_path.read_text("utf-8")))

    assert ledger_path.read_text(encoding="utf-8") == hand_written


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("cloud", {"platform_url": "https://cinna.example"}),  # placeable=False: no agent_id
        ("cloud", {"platform_url": "https://cinna.example", "agent_id": 7}),
        ("publications", [dict(LEDGER_ENTRY), {"platform_url": "https://x.example"}]),
        ("publications", "not-an-array"),
    ],
)
def test_a_ledger_value_this_build_cannot_place_is_LEFT_IN_PLACE(
    kit_module, scaffolded_agent: Path, key: str, value
) -> None:
    """Migration is all-or-nothing per key, and the fail-safe is derived.

    Discarding data a host cannot interpret is unrecoverable; leaving a
    deprecated key is visible, because `validate` names it on every run. The
    `del`s used to be conditional on the value's TYPE and never on the
    migration's OUTCOME, so a `cloud` block with no `platform_url` was deleted
    from the manifest while the absorb declined to record it — the `agent_id`
    simply ceased to exist.
    """
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[key] = value
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    kit_module.write_manifest(scaffolded_agent, json.loads(manifest_path.read_text("utf-8")))

    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written[key] == value, "nothing may be discarded"
    assert not (scaffolded_agent / "publications.json").exists()


def test_manifest_side_publications_is_a_WARNING_and_ledger_entries_are_ERRORS(
    scaffolded_agent: Path,
) -> None:
    """C5's severity half — the two sides are deliberately not the same level.

    Manifest-side: a WARNING. No other host's validator has the check, and §9.2
    forbids a kit-only error — a folder this kit calls broken that the desktop
    happily runs is the parity failure in the worse direction.

    File-side: ERRORS, matching the desktop's `manifest.publications.*`. R4 moved
    WHERE the entries live; it did not make checking them a kit-only concern.
    """
    manifest_path = scaffolded_agent / "cinna-agent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["publications"] = [dict(LEDGER_ENTRY)]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    manifest_side = run_kit("validate", str(scaffolded_agent), "--json")
    assert manifest_side.returncode == 0
    payload = json.loads(manifest_side.stdout)
    assert any(
        "`publications` no longer belongs in the manifest" in warning
        for warning in payload["warnings"]
    ), payload["warnings"]
    assert payload["errors"] == []

    # Now the same data one file over.
    manifest.pop("publications")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (scaffolded_agent / "publications.json").write_text(
        json.dumps({"publications": [{"platform_url": "https://cinna.example"}]}, indent=2),
        encoding="utf-8",
    )
    file_side = run_kit("validate", str(scaffolded_agent), "--json")
    assert file_side.returncode == 1
    errors = json.loads(file_side.stdout)["errors"]
    assert any("`publications[0].agent_id` is required" in error for error in errors), errors


@pytest.mark.parametrize(
    ("document", "expect_error", "needle"),
    [
        ("[]", True, "must contain a JSON object"),
        ("{ not json", True, "is not valid JSON"),
        ('{"publications": "x"}', True, "must be an array"),
        ('{"publications": [1]}', True, "must be an object"),
        ('{"publications": [{"platform_url": "u", "agent_id": "a", "imported_at": 5}]}',
         True, "must be a string or null"),
        ('{"other": 1}', False, "no `publications` array"),
    ],
)
def test_publications_file_findings_and_their_severities(
    scaffolded_agent: Path, document: str, expect_error: bool, needle: str
) -> None:
    (scaffolded_agent / "publications.json").write_text(document, encoding="utf-8")
    result = run_kit("validate", str(scaffolded_agent), "--json")
    payload = json.loads(result.stdout)
    bucket = payload["errors"] if expect_error else payload["warnings"]
    other = payload["warnings"] if expect_error else payload["errors"]
    assert any(needle in message for message in bucket), payload
    assert not any(needle in message for message in other), payload
    assert result.returncode == (1 if expect_error else 0)


def test_a_ledger_this_build_cannot_read_still_answers_is_published(
    kit_module, scaffolded_agent: Path
) -> None:
    """"Yes" is imprecise; "no" would be confidently wrong. The file only exists
    because something published."""
    manifest = json.loads((scaffolded_agent / "cinna-agent.json").read_text(encoding="utf-8"))
    assert kit_module.is_published(scaffolded_agent, manifest) is False

    (scaffolded_agent / "publications.json").write_text("{ broken", encoding="utf-8")
    assert kit_module.read_publications(scaffolded_agent) is None
    assert kit_module.is_published(scaffolded_agent, manifest) is True


def test_publications_json_never_travels(cloud_ready_agent: Path, tmp_path: Path) -> None:
    """The ledger is where the hash of the exported tree is recorded, so it cannot
    be inside that tree."""
    (cloud_ready_agent / "publications.json").write_text(
        json.dumps({"publications": [LEDGER_ENTRY]}, indent=2), encoding="utf-8"
    )
    destination = tmp_path / "out"
    result = run_kit("export", str(cloud_ready_agent), "--to", str(destination))
    assert result.returncode == 0, result.stderr
    assert not (destination / "publications.json").exists()
    assert "publications.json is excluded" in result.stdout


def test_cmd_new_refuses_a_template_carrying_a_ledger_key(kit_module, tmp_path: Path) -> None:
    """An ASSERTION about the KIT, standing where a repair would otherwise stand.

    A repair here would land silently on every `new` for as long as the template
    stayed wrong, and would not help the hosts that do not route through
    `write_manifest` — the desktop parses this same template and preserves
    unknown keys, so a ledger key left in it is one this repair strips and their
    scaffold keeps.
    """
    kit_copy = tmp_path / "kit"
    shutil.copytree(KIT_DIR, kit_copy, ignore=shutil.ignore_patterns("__pycache__"))
    template = kit_copy / "templates" / "agent" / "cinna-agent.json"
    document = json.loads(template.read_text(encoding="utf-8"))
    document["publications"] = []
    template.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    result = run_kit_py(kit_copy / "tools" / "kit.py", "new", "tainted", "--root", str(tmp_path))
    assert result.returncode == 1
    assert "publication-ledger key" in result.stderr
    assert "Traceback" not in result.stderr


# --------------------------------------------------------------------------- #
# Permission handling (the `PermissionError` fix round, §0 Bucket 21)
# --------------------------------------------------------------------------- #
#
# Three conditions, three lists, three messages, ONE policy — refuse, and
# `--force` waives none:
#
#   1. a FILE whose bytes will not read      → `hash_export_files`' list
#   2. a DIRECTORY the export walk cannot scan → `collect_export_tree`'s list
#   3. a DIRECTORY `validate` reads that the export never touches → this is the
#      one that did not exist, and (3) cannot be folded into (2) because (2)'s
#      walk never descends into an EXCLUDED directory — which is precisely what
#      makes an unreadable `credentials/` cost the export nothing.
#
# Every case below is guarded by `require_enforced_permissions`, which PROVOKES a
# `PermissionError` rather than inferring one from `os.geteuid()`. The backend
# container runs as root, where `chmod 000` does not block a read: without the
# guard each of these would pass while testing nothing — the exclude-list
# analogue of the stale-mirror trap, and the exact instrument failure §0 Bucket
# 21f records (a probe that `chmod 000`'d a file that does not travel and
# reported an empty unreadable list: a passing test of nothing).


def require_enforced_permissions(tmp_path: Path) -> None:
    """Skip unless this process is actually stopped by mode bits."""
    probe = tmp_path / ".permission-probe"
    probe.write_text("x", encoding="utf-8")
    os.chmod(probe, 0)
    try:
        probe.read_text(encoding="utf-8")
    except PermissionError:
        return
    finally:
        os.chmod(probe, 0o644)
        probe.unlink()
    pytest.skip(
        "this process is not stopped by file permissions (running as root?), so every "
        "permission case here would pass without testing anything"
    )


@contextlib.contextmanager
def unreadable(path: Path):
    """`chmod 000` for the duration, restored even on failure."""
    original = path.stat().st_mode
    os.chmod(path, 0)
    try:
        yield path
    finally:
        os.chmod(path, original)


@pytest.mark.parametrize(
    ("relative", "kind"),
    [
        ("scripts", "a TRAVELLING directory"),
        ("credentials", "EXCLUDED from the export, but READ by validate"),
        ("app-data", "EXCLUDED from the export, but holds the status file"),
        (".claude", "neither required nor travelling — but `_validate_secrets` walks it"),
    ],
)
def test_validate_refuses_an_unreadable_directory_with_no_traceback(
    scaffolded_agent: Path, tmp_path: Path, relative: str, kind: str
) -> None:
    """One named ERROR, exit 1, and ZERO traceback lines — for all three classes.

    `.claude/` is the case that outranks the crashes and is the cleanest instance
    in this feature of a filter standing where an assertion belongs: `os.walk`
    yields NOTHING for a directory it cannot enter, with no error and no marker,
    so the one check whose job is "there is no secret material in this tree"
    reported a clean bill of health over a subtree it never saw — and exited 0.
    """
    require_enforced_permissions(tmp_path)
    target = scaffolded_agent / relative
    target.mkdir(parents=True, exist_ok=True)

    with unreadable(target):
        result = run_kit("validate", str(scaffolded_agent))

    assert result.returncode == 1, f"{kind}: {result.stdout}\n{result.stderr}"
    assert f"{relative}/ could not be read" in result.stdout, result.stdout
    assert "nothing inside it could be checked" in result.stdout
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined, combined
    assert "Errno 13" not in combined, "a bare errno is the backstop, not a diagnosis"


def test_the_directory_fix_stands_without_mains_oserror_backstop(
    scaffolded_agent: Path, tmp_path: Path
) -> None:
    """Assert THE FIX, not the backstop that would otherwise tidy it away.

    `main()` catches `(OSError, json.JSONDecodeError)` and turns any escape into
    a neat one-liner — which is the thing most easily mistaken for a diagnosis.
    Narrowing that clause in a scratch copy is what proves the pre-scan is doing
    the work: with the backstop gone, an unhandled `PermissionError` would come
    out as a traceback.
    """
    require_enforced_permissions(tmp_path)
    narrowed = _kit_copy_without_guard(
        tmp_path,
        "kit-narrow-backstop",
        "except (OSError, json.JSONDecodeError) as exc:",
        "except (RuntimeError,) as exc:",
    )

    for relative in ("scripts", "credentials", "app-data", ".claude"):
        target = scaffolded_agent / relative
        target.mkdir(parents=True, exist_ok=True)
        with unreadable(target):
            result = run_kit_py(narrowed, "validate", str(scaffolded_agent))
        assert result.returncode == 1, relative
        assert f"{relative}/ could not be read" in result.stdout, relative
        assert "Traceback" not in result.stdout + result.stderr, (
            f"{relative}: the pre-scan is not doing the work — the backstop was"
        )


def test_two_unreadable_files_read_by_one_check_yield_ONE_error(
    scaffolded_agent: Path, tmp_path: Path
) -> None:
    """The declined granularity, asserted so nobody "fixes" it unknowingly.

    Report-and-continue is at CHECK granularity, not file granularity: one check
    is lost per failure and the others still run, but two unreadable files read
    by the SAME check yield one ERROR naming the first, and the second appears on
    the next run. Closing that means editing seven call sites for a diagnostic
    nicety; the user simply re-runs `validate`. **Recorded as a ruling, not an
    omission** — an undocumented limitation invites a later editor to "fix" it
    without knowing the trade was deliberate.
    """
    require_enforced_permissions(tmp_path)
    catalogue = scaffolded_agent / "docs" / "CLI_COMMANDS.yaml"
    makefile = scaffolded_agent / "Makefile"
    assert catalogue.is_file() and makefile.is_file()

    with unreadable(catalogue), unreadable(makefile):
        result = run_kit("validate", str(scaffolded_agent), "--json")

    assert result.returncode == 1
    errors = json.loads(result.stdout)["errors"]
    unreadable_errors = [error for error in errors if "could not be read" in error]
    assert len(unreadable_errors) == 1, unreadable_errors
    assert "CLI_COMMANDS.yaml" in unreadable_errors[0]
    assert "the check that reads it was skipped" in unreadable_errors[0]
    assert "Makefile" not in unreadable_errors[0], (
        "`_validate_commands` reaches the YAML first, so the Makefile is masked"
    )


def test_an_unreadable_required_file_reports_and_continues(
    scaffolded_agent: Path, tmp_path: Path
) -> None:
    """Bucket 21h: missing and unreadable are the same class of fact semantically
    and NOT structurally, so the handler sits at the check boundary.

    Returning `""` from the read helper would have reported an unreadable
    WORKFLOW_PROMPT.md as an EMPTY one — the fix manufacturing a plausible
    finding rather than dropping one, which moves the reader from neutral to
    wrong.
    """
    require_enforced_permissions(tmp_path)
    prompt = scaffolded_agent / "docs" / "WORKFLOW_PROMPT.md"
    with unreadable(prompt):
        result = run_kit("validate", str(scaffolded_agent), "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any(
        "WORKFLOW_PROMPT.md could not be read" in error for error in payload["errors"]
    ), payload["errors"]
    # The needle names BOTH the file and the finding: `example_prompts is empty`
    # is a legitimate warning from a different check, and a bare "is empty"
    # substring matches it — a probe that fails on the wrong hit.
    assert not any(
        "WORKFLOW_PROMPT.md" in warning and "is empty" in warning
        for warning in payload["warnings"]
    ), "an unreadable file must never be reported as an empty one"
    assert not any(
        "WORKFLOW_PROMPT.md" in warning and "placeholder" in warning
        for warning in payload["warnings"]
    ), "nor as one still holding a placeholder"

    # Report-and-CONTINUE: the checks that do not read this file still ran.
    assert any("example_prompts" in warning for warning in payload["warnings"]), payload
    assert any("description" in warning for warning in payload["warnings"]), payload


def test_content_hash_refuses_an_unreadable_travelling_file_both_ways(
    kit_module, cloud_ready_agent: Path, tmp_path: Path
) -> None:
    """D11's amendment: unevaluable ⇒ refuse to emit a `content_hash` at all.

    Both entry shapes refuse — `files=None` (walk it yourself) and an explicit
    list — because "an explicit list is a claim about which files travel, never a
    claim that they could be read". Meanwhile `hash_export_files` still returns
    the D7-parity digest plus the list, so nothing about desktop parity moved:
    only the convenience wrapper refuses.

    Non-vacuity: the file chosen must actually TRAVEL. §0 Bucket 21f's first
    attempt `chmod 000`'d `README.md`, which the contract excludes, and got an
    empty unreadable list — a passing test of nothing.
    """
    require_enforced_permissions(tmp_path)
    patterns = kit_module._with_always_excluded(kit_module.contract_exclude_patterns())
    rules = kit_module.secret_file_rules()
    travelling = kit_module.collect_export_files(cloud_ready_agent, patterns, rules)
    assert "cinna-agent.json" in travelling, "precondition: the chosen file travels"
    assert "README.md" not in travelling, "the contract excludes it — do not probe with it"

    with unreadable(cloud_ready_agent / "cinna-agent.json"):
        with pytest.raises(kit_module.KitError) as walked:
            kit_module.content_hash(cloud_ready_agent, patterns, None, rules)
        with pytest.raises(kit_module.KitError) as listed:
            kit_module.content_hash(cloud_ready_agent, patterns, travelling, rules)

        digest, unreadable_files = kit_module.hash_export_files(cloud_ready_agent, travelling)

    assert "could not be read" in str(walked.value)
    assert "cinna-agent.json" in str(listed.value)
    assert unreadable_files == ["cinna-agent.json"]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), digest


def test_export_content_hash_is_unchanged_when_an_EXCLUDED_directory_is_unreadable(
    kit_module, cloud_ready_agent: Path, tmp_path: Path
) -> None:
    """The property that keeps condition (3) out of condition (2).

    (2)'s walk never descends into an excluded directory, so an unreadable
    `credentials/`, `app-data/` or `.claude/` costs the export nothing and the
    digest is identical each time. Widening (2) to reach (3) would break exactly
    this.
    """
    require_enforced_permissions(tmp_path)
    patterns = kit_module._with_always_excluded(kit_module.contract_exclude_patterns())
    rules = kit_module.secret_file_rules()
    baseline = kit_module.content_hash(cloud_ready_agent, patterns, None, rules)

    for relative in ("credentials", "app-data", ".claude"):
        target = cloud_ready_agent / relative
        target.mkdir(parents=True, exist_ok=True)
        with unreadable(target):
            files, unscannable = kit_module.collect_export_tree(cloud_ready_agent, patterns, rules)
            assert unscannable == [], f"{relative} is excluded: the walk never enters it"
            assert kit_module.content_hash(cloud_ready_agent, patterns, None, rules) == baseline


def test_export_refuses_an_unreadable_TRAVELLING_directory_and_force_waives_nothing(
    cloud_ready_agent: Path, tmp_path: Path
) -> None:
    require_enforced_permissions(tmp_path)
    destination = tmp_path / "out"
    with unreadable(cloud_ready_agent / "scripts"):
        refused = run_kit("export", str(cloud_ready_agent), "--to", str(destination))
        forced = run_kit("export", str(cloud_ready_agent), "--to", str(destination), "--force")

    for result in (refused, forced):
        assert result.returncode == 1, f"{result.stdout}\n{result.stderr}"
        assert "Traceback" not in result.stdout + result.stderr
    assert "scripts/" in forced.stderr
    assert "--force does not cover this" in forced.stderr
    assert not destination.exists(), "a refusal must leave no half-written export behind"


# --------------------------------------------------------------------------- #
# The token guard's own differential, and the audit results this file records
# --------------------------------------------------------------------------- #


def test_the_scaffold_token_guard_fires_on_an_unfilled_token(
    kit_module, scaffolded_agent: Path
) -> None:
    """The committed proof that the guard guards — and that its predecessor did not.

    `test_no_unfilled_scaffold_tokens_remain` passes on a correct scaffold, and
    so did the version it replaces. The only way to tell a working guard from a
    vacuous one is to hand both the defect and see which notices, so that
    differential lives here rather than in a scratch session.

    The retired heuristic was `^\\{\\{[A-Z][A-Z0-9_]*\\}\\}$`, used as an EXEMPTION.
    An unsubstituted `{{NAME}}` matches it, so the retired guard was blind to
    exactly the thing it was written for.
    """
    injected = scaffolded_agent / "docs" / "WORKFLOW_PROMPT.md"
    injected.write_text("Greet {{NAME}} on arrival.\n", encoding="utf-8")

    must_be_gone = scaffold_tokens_new_must_fill(kit_module)
    found = tokens_in_tree(scaffolded_agent)
    caught = {
        relative: [token for token in tokens if token in must_be_gone]
        for relative, tokens in found.items()
    }
    caught = {relative: tokens for relative, tokens in caught.items() if tokens}

    assert caught == {"docs/WORKFLOW_PROMPT.md": ["{{NAME}}"]}, caught

    # And the same defect, run past the heuristic this replaced: it survives.
    retired_exemption = re.compile(r"^\{\{[A-Z][A-Z0-9_]*\}\}$")
    assert retired_exemption.match("{{NAME}}"), (
        "the retired guard EXEMPTED this token — which is why it went vacuous "
        "without ever going red"
    )


def test_no_assertion_in_this_module_depends_on_a_retired_token_shape(kit_module) -> None:
    """The sweep (Phase 12 item 14, widened), asserted rather than reported.

    Four things this run changed, each of which a pattern in this file could have
    been silently coupled to:

    1. **scaffold-token CASE** — Phase 6 made them UPPER_SNAKE. `PLATFORM_TOKEN_RE`
       was the instance; it is gone, and this asserts it stays gone. The module
       docstring's claim that the scaffold tokens are lowercase is gone with it.
    2. **the token SET** — every user now derives it from `kit_module`.
    3. **`schema_version` EXISTING** — the manifest template dropped it and
       `kit.json` dropped it; both are now asserted ABSENT rather than read.
    4. **the manifest carrying `publications`** — R4 moved it out, and the schema
       no longer declares the property.

    The sweep found ONE instance beyond the three the plan named — this module's
    `PLATFORM_TOKEN_RE` — and no others. Recording the null result is the point:
    without it the count is a running total of whatever happened to surface, and
    the next reader cannot tell a completed sweep from an abandoned one.
    """
    # (1) the retired heuristic must not come back as a module-level constant.
    #
    # Checked over the module's CODE, via `ast`, and not with a grep over its
    # bytes. The first draft of this assertion grepped for the retired name and
    # failed on its own explanation of why the name is retired — the documented
    # false-positive where the correcting text quotes the wording it retires.
    # Reading the hit is what settles it; an AST walk cannot see prose at all.
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    compiled: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            continue
        function = value.func
        is_compile = (
            isinstance(function, ast.Attribute)
            and function.attr == "compile"
            and isinstance(function.value, ast.Name)
            and function.value.id == "re"
        )
        if is_compile and value.args and isinstance(value.args[0], ast.Constant):
            compiled[target.id] = str(value.args[0].value)

    brace_patterns = {name: p for name, p in compiled.items() if "{" in p}
    assert set(brace_patterns) == {"ANY_TOKEN_RE"}, (
        "the only module-level regex over `{{...}}` may be the one that finds "
        f"tokens, never one that classifies them: {sorted(brace_patterns)}"
    )
    assert "[A-Z]" not in brace_patterns["ANY_TOKEN_RE"], (
        "ANY_TOKEN_RE must stay case-blind — a case-sensitive finder would go "
        "vacuous the same way the exemption did"
    )

    # (2) no test may hardcode the token set; `SCAFFOLD_TOKENS` is the authority.
    assert set(kit_module.SCAFFOLD_TOKENS) == {
        "SLUG", "NAME", "DESCRIPTION", "ID", "CONTRACT_VERSION", "KIT_VERSION", "CREATED_AT",
    }, (
        "this assertion exists to FAIL when the set changes, so that whoever "
        "changes it re-reads the provenance note above rather than adding a member "
        "silently — it is the one place a literal set is deliberate"
    )

    # (3) and (4): the absences, asserted at their sources.
    kit_json = json.loads((KIT_DIR / "kit.json").read_text(encoding="utf-8"))
    template = json.loads(
        (KIT_DIR / "templates" / "agent" / "cinna-agent.json").read_text(encoding="utf-8")
    )
    schema = json.loads((KIT_DIR / "schema" / "cinna-agent.schema.json").read_text(encoding="utf-8"))
    assert "schema_version" not in kit_json
    assert "schema_version" not in template
    assert "publications" not in template
    assert "publications" not in schema["properties"]


def test_the_two_ignore_file_traps_still_hold(scaffolded_agent: Path) -> None:
    """Phase 12 item 13, restated as a pointer rather than a copy.

    `test_new_restores_the_dot_on_every_scaffold_ignore_file` and
    `test_the_kit_ships_no_ignore_rule_that_hides_its_own_content` must keep
    passing UNCHANGED; if either needs editing, the trap was tripped. This test
    asserts the invariant they rest on — the shipped set is dotless except for
    the one deliberate exception — so a reader meeting either of them knows why
    editing it is the wrong move.
    """
    assert (KIT_DIR / "templates" / "agent" / "credentials" / ".gitignore").is_file(), (
        "the one deliberate dotted exception: it names only files no repository "
        "should track, so there it is doing the right thing rather than hiding content"
    )
    assert (KIT_DIR / "templates" / "root" / "gitignore").is_file()
    assert (KIT_DIR / "templates" / "agent" / "gitignore").is_file()
    assert (scaffolded_agent / ".gitignore").is_file()


def test_the_root_gitignore_covers_both_cloud_workspace_layouts() -> None:
    """§0 Bucket 26a: `Cloud/*/.cinna/` cannot match `Cloud/.cinna/`.

    A `*` segment never matches an empty one, so the single-pattern version left
    `.cinna/account.json` — which holds an account bearer token — trackable in
    exactly the legacy flat workshops the back-compatibility promise exists to
    serve. Both patterns ship; this asserts both, because collapsing them back to
    one is a change nothing else would notice.
    """
    ignore = (KIT_DIR / "templates" / "root" / "gitignore").read_text(encoding="utf-8")
    lines = {line.strip() for line in ignore.splitlines()}
    assert "Cloud/.cinna/" in lines, "the legacy flat layout"
    assert "Cloud/*/.cinna/" in lines, "the per-instance layout"
