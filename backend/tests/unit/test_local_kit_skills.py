"""Local Agent Kit contract 1.10.0 — skills in `kit.py validate`.

`kit.py` carries a stdlib-only port of the platform's skill validator
(`app.services.agents.skill_manifest`). The port has to agree with the platform
on every rule, or `validate` passes a skill the platform drops (or the
reverse), so most of this module runs the same inputs through both and compares.
The rest pins the 1.10.0 additions end to end: the `catalog_skills[]` pin and
its name clashes, `publications.json` → `skills[]`, and the schema keys.

Kit location follows `test_local_kit_tool.py`: `$LOCAL_AGENT_KIT_DIR`, then the
repo checkout, then the synced snapshot.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.agents import skill_manifest as core


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
KIT_PY = KIT_DIR / "tools" / "kit.py"


def _load_kit():
    spec = importlib.util.spec_from_file_location("cinna_local_kit_skills", KIT_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kit = _load_kit()

SHA = "a" * 64
PIN = {
    "package_id": "io.example.team.skill.pdf-report",
    "package_uuid": "0b5c1f8e-6a43-4d7e-9a51-2f0c7d1e9b34",
    "name": "pdf-report",
    "revision_number": 3,
    "version": "1.2.0",
    "archive_sha256": SHA,
}


def run_kit(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(KIT_PY), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture()
def agent(tmp_path: Path) -> Path:
    result = run_kit("new", "invoice-watcher", "--name", "Invoice Watcher", "--root", str(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    return tmp_path / "Local" / "invoice-watcher"


def validate(agent_dir: Path) -> dict:
    result = run_kit("validate", str(agent_dir), "--json")
    return json.loads(result.stdout)


def write_skill(agent_dir: Path, folder: str, frontmatter: str, body: str = "# Skill\n") -> Path:
    skill = agent_dir / "skills" / folder
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return skill


def set_manifest(agent_dir: Path, **changes: object) -> None:
    path = agent_dir / "cinna-agent.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(changes)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def errors_mentioning(payload: dict, needle: str) -> list[str]:
    return [message for message in payload["errors"] if needle in message]


# --------------------------------------------------------------------------- #
# Parity with the platform's validator
# --------------------------------------------------------------------------- #


def test_the_kit_constants_match_the_platform() -> None:
    assert kit.SKILL_NAME_RE.pattern == core.SKILL_NAME_RE.pattern
    assert kit.SKILL_MAX_NAME_LENGTH == core.MAX_NAME_LENGTH
    assert kit.SKILL_MAX_DESCRIPTION_LENGTH == core.MAX_DESCRIPTION_LENGTH
    assert kit.SKILL_MAX_VERSION_LENGTH == core.MAX_VERSION_LENGTH
    assert kit.SKILL_MAX_BODY_BYTES == core.MAX_BODY_BYTES
    assert kit.SKILL_MAX_COUNT == core.DEFAULT_MAX_SKILLS
    assert kit.SKILL_MAX_TOTAL_BYTES == core.DEFAULT_MAX_TOTAL_BYTES
    assert kit.RESERVED_SKILL_NAMES == core.RESERVED_SKILL_NAMES
    assert kit.DEFAULT_PLATFORM_SKILL_NAMES == core.PLATFORM_SKILL_NAMES
    assert kit.platform_skill_names() == core.PLATFORM_SKILL_NAMES
    assert kit.SKILL_CREDENTIAL_SLOT_RE.pattern == core.SKILL_CREDENTIAL_SLOT_RE.pattern
    assert kit.SKILL_MAX_SLOT_LENGTH == core.MAX_SLOT_LENGTH
    assert kit.SKILL_MAX_CREDENTIALS == core.MAX_SKILL_CREDENTIALS
    assert kit.SKILL_CREDENTIAL_TYPES == core.SKILL_CREDENTIAL_TYPES
    for code, message in kit.SKILL_ISSUE_MESSAGES.items():
        assert core.ISSUE_MESSAGES[code] == message, code


FRONTMATTER_SAMPLES = [
    "---\nname: a\ndescription: b\n---\nbody\n",
    "---\nname: 'quoted'\nversion: 1.0\nflag: yes\nnothing: ~\ncount: 3\n---\n",
    "---\ntools: [a, b, \"c\"]\nempty: []\n---\n",
    "---\ndescription: |\n  line one\n  line two\nfolded: >-\n  a\n  b\n---\n",
    "---\nmetadata:\n  owner: team\n  tier: 2\n---\n",
    "---\ncredentials:\n  - slot: odoo.acme\n    type: odoo\n    description: The ERP\n"
    "  - slot: mail\n    type: email_smtp\n---\n",
    "---\nallowed:\n  - Bash(git:*)\n  - http://host/x\n# comment\nname: x # note\n---\n",
    "---\nname: a\n...\nafter\n",
]


@pytest.mark.parametrize("text", FRONTMATTER_SAMPLES)
def test_the_frontmatter_parser_matches_the_platform(text: str) -> None:
    assert kit.parse_skill_frontmatter(text) == core.parse_frontmatter(text)


@pytest.mark.parametrize("text", ["no fence\n", "---\nname: a\n"])
def test_the_frontmatter_parser_refuses_what_the_platform_refuses(text: str) -> None:
    with pytest.raises(ValueError):
        core.parse_frontmatter(text)
    with pytest.raises(ValueError):
        kit.parse_skill_frontmatter(text)


CREDENTIAL_BLOCKS = [
    None,
    "",
    "not a list",
    [{"slot": f"s{i}", "type": "odoo"} for i in range(21)],
    ["scalar"],
    [{"slot": 8080, "type": "odoo"}],
    [{"type": "odoo"}],
    [{"slot": "x" * 256, "type": "odoo"}],
    [{"slot": "has space", "type": "odoo"}],
    [{"slot": "a", "type": "odoo"}, {"slot": "a", "type": "odoo"}],
    [{"slot": "a", "type": 3}],
    [{"slot": "a"}],
    [{"slot": "a", "type": "mcp_provider"}],
    [{"slot": "a", "type": "carrier_pigeon"}],
    [{"slot": "a", "type": "odoo", "description": 5}],
    [{"slot": "a", "type": "odoo", "description": "d" * 1025}],
    [{"slot": "odoo.acme", "type": "odoo", "description": "ERP", "extra": 1}],
    [{"slot": "a", "type": "api_token"}, {"slot": "user@host:22/x", "type": "ssh_key"}],
]


@pytest.mark.parametrize("block", CREDENTIAL_BLOCKS)
def test_the_credentials_block_rules_match_the_platform(block: object) -> None:
    assert kit.skill_credentials_problem(block) == core.parse_credential_declarations(block)[1]


SKILL_FOLDERS = {
    "good": "name: good\ndescription: Does a thing.\n",
    "mismatch": "name: other\ndescription: d\n",
    "Bad_Name": "name: Bad_Name\ndescription: d\n",
    "no-name": "description: d\n",
    "no-description": "name: no-description\n",
    "run": "name: run\ndescription: d\n",
    "cinna-tasks": "name: cinna-tasks\ndescription: d\n",
    "long-description": "name: long-description\ndescription: " + "d" * 1025 + "\n",
    "bad-credentials": "name: bad-credentials\ndescription: d\ncredentials:\n"
    "  - slot: x\n    type: mcp_provider\n",
}


def test_skill_folder_verdicts_match_the_platform(tmp_path: Path) -> None:
    for folder, frontmatter in SKILL_FOLDERS.items():
        skill = tmp_path / folder
        skill.mkdir()
        (skill / "SKILL.md").write_text(f"---\n{frontmatter}---\nbody\n", encoding="utf-8")
    (tmp_path / "no-skill-md").mkdir()
    (tmp_path / "no-fence").mkdir()
    (tmp_path / "no-fence" / "SKILL.md").write_text("just text\n", encoding="utf-8")

    for skill in sorted(tmp_path.iterdir()):
        entry = core.parse_skill_dir(skill)
        code, detail = kit.skill_issue(skill)
        expected = entry.error.code if entry.error else None
        assert code == expected, skill.name
        if entry.error is not None and entry.error.message != core.ISSUE_MESSAGES[expected]:
            assert detail == entry.error.message, skill.name


def test_catalog_skill_names_are_read_from_the_package_id() -> None:
    assert kit.catalog_skill_name("io.example.team.skill.pdf-report") == "pdf-report"
    assert kit.catalog_skill_name("io.example.skill.pdf-report.1a2b3c4d") == "pdf-report"
    assert kit.catalog_skill_name("io.example.skill.pdf-report.1a2b3c4d-2") == "pdf-report"
    assert kit.catalog_skill_name("io.example.team.pdf-report") is None
    assert kit.catalog_skill_name(None) is None


# --------------------------------------------------------------------------- #
# `kit.py validate`, end to end
# --------------------------------------------------------------------------- #


def test_a_valid_skill_with_credentials_validates_clean(agent: Path) -> None:
    write_skill(
        agent,
        "invoice-sync",
        "name: invoice-sync\ndescription: Sync invoices.\nversion: 1.0.0\ncredentials:\n"
        "  - slot: odoo.acme.example\n    type: odoo\n    description: The ERP\n",
    )
    payload = validate(agent)
    assert payload["ok"] is True, payload["errors"]
    assert not [w for w in payload["warnings"] if "skills/" in w], payload["warnings"]


@pytest.mark.parametrize(
    ("folder", "frontmatter", "needle"),
    [
        ("timeoff", "name: time-off\ndescription: d\n", "does not match the folder name"),
        ("run", "name: run\ndescription: d\n", "reserved by a platform command"),
        ("cinna-tasks", "name: cinna-tasks\ndescription: d\n", "reserved by a platform skill"),
        ("no-description", "name: no-description\n", "has no description"),
        (
            "mcp",
            "name: mcp\ndescription: d\ncredentials:\n  - slot: x\n    type: mcp_provider\n",
            "which a skill cannot declare",
        ),
    ],
)
def test_a_broken_skill_is_an_error(agent: Path, folder: str, frontmatter: str, needle: str) -> None:
    write_skill(agent, folder, frontmatter)
    payload = validate(agent)
    assert payload["ok"] is False
    found = errors_mentioning(payload, f"skills/{folder}/SKILL.md")
    assert found and needle in found[0], payload["errors"]


def test_a_skill_folder_without_skill_md_is_an_error(agent: Path) -> None:
    (agent / "skills" / "empty").mkdir(parents=True)
    payload = validate(agent)
    assert errors_mentioning(payload, "skills/empty/SKILL.md: This folder has no SKILL.md.")


def test_an_oversized_body_and_an_unusable_version_are_warnings(agent: Path) -> None:
    write_skill(agent, "big", "name: big\ndescription: d\nversion: |\n  1\n  2\n", "x" * (65 * 1024))
    payload = validate(agent)
    assert payload["ok"] is True, payload["errors"]
    big = [w for w in payload["warnings"] if "skills/big/SKILL.md" in w]
    assert any("64 KB" in w for w in big), big
    assert any("`version`" in w for w in big), big


def test_skills_past_the_count_budget_are_errors(agent: Path) -> None:
    for i in range(52):
        write_skill(agent, f"s{i:02d}", f"name: s{i:02d}\ndescription: d\n")
    payload = validate(agent)
    over = [e for e in payload["errors"] if "skill budget" in e]
    # Name order decides who falls past the cap, as on the platform.
    assert [e.split(":")[0] for e in over] == ["skills/s50", "skills/s51"]


def test_skills_past_the_size_budget_are_errors(agent: Path) -> None:
    for name in ("a-big", "b-big"):
        skill = write_skill(agent, name, f"name: {name}\ndescription: d\n")
        (skill / "assets").mkdir()
        (skill / "assets" / "blob.bin").write_bytes(b"\0" * (9 * 1024 * 1024))
    payload = validate(agent)
    over = [e for e in payload["errors"] if "skill budget" in e]
    assert [e.split(":")[0] for e in over] == ["skills/b-big"], payload["errors"]


def test_a_well_formed_catalog_pin_validates_clean(agent: Path) -> None:
    set_manifest(agent, catalog_skills=[PIN])
    payload = validate(agent)
    assert payload["ok"] is True, payload["errors"]


def test_a_catalog_pin_named_like_an_authored_skill_is_an_error(agent: Path) -> None:
    write_skill(agent, "pdf-report", "name: pdf-report\ndescription: d\n")
    set_manifest(agent, catalog_skills=[PIN])
    payload = validate(agent)
    found = errors_mentioning(payload, "`catalog_skills[0]`")
    assert found and "skills/pdf-report/" in found[0], payload["errors"]


def test_a_catalog_pin_named_like_a_platform_skill_is_an_error(agent: Path) -> None:
    set_manifest(agent, catalog_skills=[dict(PIN, name="cinna-tasks")])
    payload = validate(agent)
    found = errors_mentioning(payload, "`catalog_skills[0]`")
    assert found and "platform provides" in found[0], payload["errors"]


def test_two_pins_of_one_skill_name_are_an_error(agent: Path) -> None:
    second = dict(
        PIN,
        package_id="io.example.other.skill.report",
        package_uuid="6f1e2d3c-4b5a-4968-8776-655443322110",
    )
    set_manifest(agent, catalog_skills=[PIN, second])
    payload = validate(agent)
    assert errors_mentioning(payload, "`catalog_skills[1].name` pins a second skill")


def test_the_clash_check_reads_the_pin_name_not_the_package_id(agent: Path) -> None:
    write_skill(agent, "pdf-report", "name: pdf-report\ndescription: d\n")
    pin = dict(PIN, package_id="io.example.team.custom-id", name="pdf-report")
    set_manifest(agent, catalog_skills=[pin])
    payload = validate(agent)
    assert errors_mentioning(payload, "`catalog_skills[0]` is the catalog skill `pdf-report`")


def test_a_pin_without_a_name_is_an_error(agent: Path) -> None:
    pin = {key: value for key, value in PIN.items() if key != "name"}
    set_manifest(agent, catalog_skills=[pin])
    payload = validate(agent)
    assert errors_mentioning(payload, "`catalog_skills[0].name`")


def test_a_package_id_that_disagrees_with_the_name_is_a_warning(agent: Path) -> None:
    set_manifest(agent, catalog_skills=[dict(PIN, name="pdf-summary")])
    payload = validate(agent)
    assert payload["ok"] is True, payload["errors"]
    assert [w for w in payload["warnings"] if "`catalog_skills[0].package_id`" in w]


def test_a_version_over_64_characters_is_a_warning(agent: Path) -> None:
    write_skill(agent, "long-version", "name: long-version\ndescription: d\nversion: " + "1" * 70 + "x\n")
    payload = validate(agent)
    assert payload["ok"] is True, payload["errors"]
    assert [w for w in payload["warnings"] if "longer than 64" in w]


MPIN = {
    "entry_uuid": "11111111-2222-4333-8444-555555555555",
    "marketplace_uuid": "66666666-7777-4888-9999-000000000000",
    "marketplace_name": "Team Market",
    "name": "market-report",
    "archive_sha256": SHA,
    "commit_hash": None,
    "version": "1.0.0",
}


def test_a_well_formed_marketplace_pin_validates_clean(agent: Path) -> None:
    set_manifest(agent, marketplace_skills=[MPIN])
    payload = validate(agent)
    assert payload["ok"] is True, payload["errors"]


def test_a_marketplace_pin_named_like_an_authored_skill_is_an_error(agent: Path) -> None:
    write_skill(agent, "market-report", "name: market-report\ndescription: d\n")
    set_manifest(agent, marketplace_skills=[MPIN])
    payload = validate(agent)
    found = errors_mentioning(payload, "marketplace_skills[0]")
    assert found and "skills/market-report/" in " ".join(found), payload["errors"]


def test_a_marketplace_pin_named_like_a_platform_skill_is_an_error(agent: Path) -> None:
    set_manifest(agent, marketplace_skills=[dict(MPIN, name="cinna-tasks")])
    payload = validate(agent)
    found = errors_mentioning(payload, "marketplace_skills[0]")
    assert found and "platform provides" in " ".join(found), payload["errors"]


def test_a_marketplace_pin_named_like_a_catalog_pin_is_an_error(agent: Path) -> None:
    set_manifest(agent, catalog_skills=[PIN], marketplace_skills=[dict(MPIN, name="pdf-report")])
    payload = validate(agent)
    assert errors_mentioning(payload, "marketplace_skills"), payload["errors"]


def test_two_marketplace_pins_of_one_name_or_entry_are_errors(agent: Path) -> None:
    same_name = dict(MPIN, entry_uuid="aaaaaaaa-2222-4333-8444-555555555555")
    set_manifest(agent, marketplace_skills=[MPIN, same_name])
    assert errors_mentioning(validate(agent), "marketplace_skills[1]")
    same_entry = dict(MPIN, name="other-skill")
    set_manifest(agent, marketplace_skills=[MPIN, same_entry])
    assert errors_mentioning(validate(agent), "marketplace_skills[1]")


@pytest.mark.parametrize(
    "change",
    [
        {"entry_uuid": "nope"},
        {"marketplace_uuid": "nope"},
        {"archive_sha256": "xyz"},
        {"name": "Bad Name"},
    ],
)
def test_a_malformed_marketplace_pin_is_an_error(agent: Path, change: dict) -> None:
    set_manifest(agent, marketplace_skills=[dict(MPIN, **change)])
    payload = validate(agent)
    assert payload["ok"] is False
    assert errors_mentioning(payload, "marketplace_skills[0]"), payload["errors"]


def test_a_manifest_with_authored_catalog_and_marketplace_skills_validates_clean(agent: Path) -> None:
    write_skill(agent, "my-own", "name: my-own\ndescription: d\n")
    set_manifest(agent, catalog_skills=[PIN], marketplace_skills=[MPIN])
    payload = validate(agent)
    assert payload["ok"] is True, payload["errors"]


def test_the_manifest_schema_registers_marketplace_skills() -> None:
    props = _schema("cinna-agent.schema.json")["properties"]
    assert "marketplace_skills" in props
    item = props["marketplace_skills"]["items"]
    assert set(item["required"]) == {
        "entry_uuid", "marketplace_uuid", "marketplace_name", "name", "archive_sha256",
    }


def test_publications_skills_entries_are_checked(agent: Path) -> None:
    (agent / "publications.json").write_text(
        json.dumps(
            {
                "publications": [],
                "skills": [
                    {
                        "platform_url": "https://acme.example",
                        "name": "pdf-report",
                        "package_id": "io.example.skill.pdf-report",
                        "package_uuid": PIN["package_uuid"],
                        "revision_number": 2,
                        "content_hash": "abc",
                    },
                    {"platform_url": "https://acme.example", "name": "Bad", "revision_number": 0},
                ],
            }
        ),
        encoding="utf-8",
    )
    payload = validate(agent)
    bad = [e for e in payload["errors"] if "`skills[" in e]
    assert all("skills[1]" in e for e in bad), bad
    assert {e.split("`")[1] for e in bad} == {
        "skills[1].name",
        "skills[1].package_id",
        "skills[1].package_uuid",
        "skills[1].content_hash",
        "skills[1].revision_number",
    }


def test_migrating_a_legacy_ledger_keeps_the_skills_key(tmp_path: Path) -> None:
    skills = [{"platform_url": "https://acme.example", "name": "pdf-report"}]
    (tmp_path / "publications.json").write_text(
        json.dumps({"publications": [], "skills": skills}), encoding="utf-8"
    )
    kit.write_manifest(
        tmp_path,
        {"slug": "a", "cloud": {"platform_url": "https://acme.example", "agent_id": "x"}},
    )
    ledger = json.loads((tmp_path / "publications.json").read_text(encoding="utf-8"))
    assert ledger["skills"] == skills
    assert ledger["publications"][0]["agent_id"] == "x"


# --------------------------------------------------------------------------- #
# Schemas and layout
# --------------------------------------------------------------------------- #


def _schema(name: str) -> dict:
    return json.loads((KIT_DIR / "schema" / name).read_text(encoding="utf-8"))


def test_the_manifest_schema_registers_catalog_skills() -> None:
    prop = _schema("cinna-agent.schema.json")["properties"]["catalog_skills"]
    assert prop["type"] == "array"
    assert prop["x-scope"] == "portable"
    assert prop["x-import"] == "mapped to catalog skill links on cloud import (later)"
    items = prop["items"]
    assert set(items["properties"]) == {
        "package_id",
        "package_uuid",
        "name",
        "revision_number",
        "version",
        "archive_sha256",
    }
    assert set(items["required"]) == {
        "package_id",
        "package_uuid",
        "name",
        "revision_number",
        "archive_sha256",
    }
    for name, sub in items["properties"].items():
        assert "x-scope" in sub and "x-import" in sub, name
    assert items["properties"]["package_id"]["pattern"] == kit.CATALOG_PACKAGE_ID_RE.pattern
    assert items["properties"]["archive_sha256"]["pattern"] == kit.SHA256_HEX_RE.pattern
    assert items["properties"]["name"]["pattern"] == kit.SKILL_NAME_RE.pattern
    assert items["properties"]["name"]["maxLength"] == kit.SKILL_MAX_NAME_LENGTH


def test_the_publications_schema_registers_skills() -> None:
    schema = _schema("publications.schema.json")
    assert schema["required"] == ["publications"]
    prop = schema["properties"]["skills"]
    assert prop["x-scope"] == "portable" and "x-import" in prop
    expected = {"platform_url", "name", "package_id", "package_uuid", "revision_number", "content_hash"}
    assert set(prop["items"]["properties"]) == expected
    assert set(prop["items"]["required"]) == expected
    assert set(kit.SKILL_LEDGER_STRING_KEYS) | {"revision_number"} == expected
