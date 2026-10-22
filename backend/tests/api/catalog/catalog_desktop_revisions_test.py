"""C2 — what a share revision records from a real ``git push`` (plan §2.4).

``catalog_desktop_git_test.py`` covers the round trip and one commit per
push; this file pins what the revision reads from the pushed tree and history:

Scenarios:
  1. First push (two commits): Version 1 with release notes = both commit
     messages (newest first), ``required_credential_specs`` from manifest
     ``credentials[]`` (``provided_by='user'``), ``skills`` = valid authored
     folders under ``skills/`` + ``catalog_skills[]`` pins (named from the
     catalog, carrying ``package_id`` / ``package_uuid``), ``desktop.engine``
     from ``runtime.engine``.
  2. Second push (two more commits, one slot and the pin dropped): Version 2's
     notes cover only the commits since Version 1; item-level specs and
     skills follow the latest version; Version 1's notes are unchanged.
  3. A refused push (changed manifest id) records no version.
"""
import json
import uuid

from fastapi.testclient import TestClient

from tests.api.catalog.catalog_helpers import (
    make_person as make_developer,
    publish_skill_package,
)
from app.services.cli.platform_knowledge_assets import local_kit_dir
from tests.utils.catalog import (
    GitClient,
    put_grants,
    user_grant,
    create_desktop_agent,
    get_item,
    manifest_id_of,
    repo_id_of,
    server_contract_version,
    write_agent_tree,
)

SKILL_MD = "---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nDo it.\n"


def _manifest(item: dict, *, credentials=None, catalog_skills=None, **extra) -> dict:
    major, minor, patch = server_contract_version()
    document = {
        "id": manifest_id_of(item),
        "name": "Revision Agent",
        "slug": item["name"],
        "description": "Reads the tree",
        "contract_version": f"{major}.{minor}.{patch}",
        **extra,
    }
    if credentials is not None:
        document["credentials"] = credentials
    if catalog_skills is not None:
        document["catalog_skills"] = catalog_skills
    return document


def _token(person) -> str:
    return person.headers["Authorization"].removeprefix("Bearer ")


def test_push_records_notes_specs_and_skills(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    su = superuser_token_headers
    owner = make_developer(client, su)
    package = publish_skill_package(client, owner.headers, "pdf-report")
    pkg_item = get_item(client, owner.headers, package["package_uuid"])
    ghost_uuid = str(uuid.uuid4())
    pin = {
        "package_id": package["package_id"],
        "package_uuid": package["package_uuid"],
        "name": "pinned-alias",
        "revision_number": 1,
        "archive_sha256": pkg_item["skill"]["archive_sha256"],
    }
    ghost_pin = {
        "package_id": "io.example.skill.ghost",
        "package_uuid": ghost_uuid,
        "name": "ghost-skill",
        "revision_number": 3,
        "archive_sha256": "0" * 64,
    }
    credentials = [
        {"name": "Mailbox", "type": "email_imap", "description": "The team inbox"},
        {"name": "Odoo", "type": "odoo", "service_uri": "https://odoo.example.com"},
    ]

    item = create_desktop_agent(client, owner.headers)
    git = GitClient(tmp_path, token=_token(owner))
    work = tmp_path / "work"
    assert git.clone(bridge.repo_url(repo_id_of(item)), work).ok

    # ── Version 1: two commits in one push ────────────────────────────
    write_agent_tree(
        work,
        manifest=_manifest(
            item, credentials=credentials, catalog_skills=[pin, ghost_pin],
            runtime={"engine": "opencode"},
        ),
    )
    git.commit_all(work, "init")
    files = {
        "skills/report-writer/SKILL.md": SKILL_MD.format(
            name="report-writer", description="Writes weekly reports."
        ),
        # Folder and frontmatter name disagree: not a valid skill, left out.
        "skills/broken/SKILL.md": SKILL_MD.format(name="other", description="Nope."),
    }
    for rel, content in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(content)
    sha1 = git.commit_all(work, "add skills\n\nWith a body line.")
    pushed = git.push(work)
    assert pushed.ok, pushed.output

    got = get_item(client, owner.headers, item["uuid"])
    assert got["latest"]["revision_number"] == 1
    assert got["latest"]["release_notes"] == "add skills\n\nWith a body line.\n\ninit"
    assert got["revisions"][0]["git_commit_sha"] == sha1
    assert got["revisions"][0]["origin"] == "share"
    assert got["desktop"]["engine"] == "opencode"
    assert got["desktop"]["head_sha"] == sha1

    specs = {s["name"]: s for s in got["required_credential_specs"]}
    assert set(specs) == {"Mailbox", "Odoo"}
    assert specs["Mailbox"]["type"] == "email_imap"
    assert specs["Mailbox"]["provided_by"] == "user"
    assert specs["Mailbox"]["description"] == "The team inbox"
    assert specs["Odoo"]["type"] == "odoo"
    assert specs["Odoo"]["service_uri"] == "https://odoo.example.com"
    assert specs["Odoo"]["provided_by"] == "user"
    assert all(s.get("publisher_credential_id") is None for s in specs.values())

    skills = {s["name"]: s for s in got["skills"]}
    assert set(skills) == {"report-writer", "pdf-report", "ghost-skill"}
    authored = skills["report-writer"]
    assert authored["source"] == "authored"
    assert authored["description"] == "Writes weekly reports."
    assert authored["package_id"] is None and authored["package_uuid"] is None
    pinned = skills["pdf-report"]  # named from the catalog, not the pin
    assert pinned["source"] == "catalog"
    assert pinned["package_id"] == package["package_id"]
    assert pinned["package_uuid"] == package["package_uuid"]
    ghost = skills["ghost-skill"]  # unknown package: the pin's own name
    assert ghost["source"] == "catalog" and ghost["package_uuid"] == ghost_uuid

    # ── Version 2: notes only since Version 1, specs/skills follow ────
    write_agent_tree(work, manifest=_manifest(item, credentials=credentials[1:]))
    git.commit_all(work, "drop mailbox")
    (work / "skills" / "broken").joinpath("SKILL.md").unlink()
    sha2 = git.commit_all(work, "remove broken skill")
    pushed = git.push(work)
    assert pushed.ok, pushed.output

    got = get_item(client, owner.headers, item["uuid"])
    assert [r["revision_number"] for r in got["revisions"]] == [2, 1]
    assert got["latest"]["release_notes"] == "remove broken skill\n\ndrop mailbox"
    assert got["revisions"][0]["git_commit_sha"] == sha2
    assert got["revisions"][1]["release_notes"] == (
        "add skills\n\nWith a body line.\n\ninit"
    )
    assert [s["name"] for s in got["required_credential_specs"]] == ["Odoo"]
    assert [(s["name"], s["source"]) for s in got["skills"]] == [
        ("report-writer", "authored")
    ]
    assert got["desktop"]["engine"] is None

    # ── A refused push records nothing ────────────────────────────────
    bad = _manifest(item)
    bad["id"] = str(uuid.uuid4())
    (work / "cinna-agent.json").write_text(json.dumps(bad))
    git.commit_all(work, "new identity")
    refused = git.push(work)
    assert not refused.ok
    assert "This agent's identity changed; share it as a new agent instead." in refused.output
    after = get_item(client, owner.headers, item["uuid"])
    assert [r["revision_number"] for r in after["revisions"]] == [2, 1]
    assert after["desktop"]["head_sha"] == sha2


def test_marketplace_skill_pins_round_trip_byte_for_byte(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    """M7 round trip.

    1. Vacuity guard: the mirrored schema knows ``marketplace_skills``, contract >= 1.15.0
    2. Owner pushes a manifest with a pin carrying an unknown extra key -> accepted
    3. A ``use`` holder clones it: ``cinna-agent.json`` is byte-for-byte what was written
    4. A pin with ``archive_sha256: "xyz"`` is refused by the hook (mirror enforces)
    """
    schema = (local_kit_dir() / "schema" / "cinna-agent.schema.json").read_text()
    assert '"marketplace_skills"' in schema
    assert server_contract_version() >= (1, 15, 0)

    su = superuser_token_headers
    owner = make_developer(client, su)
    reader = make_developer(client, su)
    item = create_desktop_agent(client, owner.headers, visibility="users")
    put_grants(
        client, owner.headers, item["uuid"],
        [user_grant(owner.id, "maintain"), user_grant(reader.id, "use")],
    )
    pin = {
        "entry_uuid": str(uuid.uuid4()),
        "marketplace_uuid": str(uuid.uuid4()),
        "marketplace_name": "Team Market",
        "name": "market-report",
        "archive_sha256": "ab" * 32,
        "commit_hash": None,
        "version": "2.0.1",
        "future_key": {"kept": True},
    }
    git = GitClient(tmp_path, token=_token(owner))
    work = tmp_path / "work"
    assert git.clone(bridge.repo_url(repo_id_of(item)), work).ok
    write_agent_tree(work, manifest=_manifest(item, marketplace_skills=[pin]))
    written = (work / "cinna-agent.json").read_bytes()
    git.commit_all(work, "pin marketplace skill")
    pushed = git.push(work)
    assert pushed.ok, pushed.output

    got = get_item(client, owner.headers, item["uuid"])
    assert got["latest"]["revision_number"] == 1

    rgit = GitClient(tmp_path, token=_token(reader))
    assert rgit.clone(bridge.repo_url(repo_id_of(item)), tmp_path / "reader").ok
    assert (tmp_path / "reader" / "cinna-agent.json").read_bytes() == written
    assert json.loads(written)["marketplace_skills"] == [pin]

    bad = dict(pin, archive_sha256="xyz")
    write_agent_tree(work, manifest=_manifest(item, marketplace_skills=[bad]))
    git.commit_all(work, "bad pin")
    refused = git.push(work)
    assert not refused.ok
    after = get_item(client, owner.headers, item["uuid"])
    assert [r["revision_number"] for r in after["revisions"]] == [1]
