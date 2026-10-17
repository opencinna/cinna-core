"""Desktop agent ``pre-receive`` policy, one real ``git push`` per refusal.

Every refused push must (a) fail on the client, (b) print the S9 sentence in
git's output (``remote: ...``) and (c) leave the agent's ``head_sha`` where
it was. Accepted edge cases (``.env.example``, a newer minor contract) are
asserted to move ``head_sha``.
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.catalog import (
    GitClient,
    create_desktop_agent,
    get_item,
    make_person,
    manifest_id_of,
    repo_id_of,
    server_contract_version,
    write_agent_tree,
)
from unittest.mock import patch


class Shared:
    def __init__(self, client, bridge, tmp_path, owner, repo):
        self.client, self.owner, self.repo = client, owner, repo
        self.repo_id = repo_id_of(repo)
        self.manifest_id = manifest_id_of(repo)
        self.git = GitClient(tmp_path, token=owner.token)
        self.work = tmp_path / "work"
        assert self.git.clone(bridge.repo_url(self.repo_id), self.work).ok
        write_agent_tree(self.work, slug=repo["name"], manifest_id=self.manifest_id)
        self.git.commit_all(self.work, "baseline")
        first = self.git.push(self.work)
        assert first.ok, first.output
        self.baseline = self.head()

    def head(self) -> str | None:
        return get_item(self.client, self.owner.headers, self.repo["uuid"])["desktop"]["head_sha"]

    def commit(self, files: dict | None = None, remove: list[str] | None = None, manifest=None):
        if manifest is not None:
            (self.work / "cinna-agent.json").write_text(manifest)
        for rel, content in (files or {}).items():
            target = self.work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.encode())
        for rel in remove or []:
            (self.work / rel).unlink()
        return self.git.commit_all(self.work, "change")

    def refused(self, sentence: str, *refspec: str) -> None:
        result = self.git.push(self.work, *refspec)
        assert not result.ok, result.output
        assert sentence in result.output, result.output
        assert self.head() == self.baseline


@pytest.fixture()
def shared(client: TestClient, bridge, tmp_path, superuser_token_headers) -> Shared:
    owner = make_person(client, superuser_token_headers)
    repo = create_desktop_agent(client, owner.headers)
    return Shared(client, bridge, tmp_path, owner, repo)


def _manifest(**overrides) -> str:
    import json
    import uuid

    major, minor, patch_ = server_contract_version()
    doc = {
        "id": str(uuid.uuid4()),
        "name": "X",
        "slug": "my-agent",
        "description": "d",
        "contract_version": f"{major}.{minor}.{patch_}",
    }
    doc.update(overrides)
    return json.dumps(doc)


SECRET = "a file that can hold a password or key, so it can't be shared."


@pytest.mark.parametrize(
    "path,sentence",
    [
        (".env", f"This change includes .env, {SECRET}"),
        (".env.local", f"This change includes .env.local, {SECRET}"),
        ("config/prod.env", f"This change includes config/prod.env, {SECRET}"),
        (
            "credentials/token.txt",
            "This change includes the agent's credentials folder, which never leaves your computer.",
        ),
        (
            "app-data/db.sqlite",
            "This change includes the agent's app-data folder (its private working files), "
            "which can't be shared.",
        ),
        (
            "artifacts/q3-dashboard/index.html",
            "This change includes the agent's artifacts folder (what it built to publish), "
            "which stays with you and can't be shared. Share a published artifact from its page instead.",
        ),
    ],
)
def test_refuses_secret_and_never_travels_paths(shared: Shared, path: str, sentence: str) -> None:
    shared.commit({path: "value"})
    shared.refused(sentence)


def test_refuses_secret_that_was_added_then_removed(shared: Shared) -> None:
    """A secret in an intermediate commit still travels in the pack."""
    shared.commit({".env": "KEY=1"})
    shared.commit(remove=[".env"])
    shared.refused("This change includes .env,")


def test_dotenv_example_is_accepted(shared: Shared) -> None:
    shared.commit({".env.example": "KEY=", "x.env.sample": "KEY="})
    result = shared.git.push(shared.work)
    assert result.ok, result.output
    assert shared.head() != shared.baseline


def test_refuses_blob_over_cap(shared: Shared) -> None:
    shared.commit({"big.bin": os.urandom(1_500_000)})
    with patch.object(settings, "GIT_MAX_BLOB_BYTES", 1024 * 1024):
        shared.refused("The file big.bin is larger than 1 MB, too big to share with the team.")


def test_refuses_tree_over_cap(shared: Shared) -> None:
    shared.commit({f"part{i}.bin": os.urandom(400_000) for i in range(4)})
    with patch.object(settings, "GIT_MAX_TREE_BYTES", 1024 * 1024):
        shared.refused("This agent is larger than 1 MB in total, too big to share with the team.")


@pytest.mark.parametrize(
    "content,sentence",
    [
        (None, "This agent has no cinna-agent.json, so it can't be shared."),
        ("not json {", "This agent's cinna-agent.json isn't valid (it isn't readable JSON)"),
        ('{"name": 5}', "This agent's cinna-agent.json isn't valid ("),
    ],
)
def test_refuses_missing_or_invalid_manifest(shared: Shared, content, sentence) -> None:
    if content is None:
        shared.commit(remove=["cinna-agent.json"])
    else:
        shared.commit(manifest=content)
    shared.refused(sentence)


def test_contract_major_mismatch_refused_and_minor_newer_warns(shared: Shared) -> None:
    major, minor, _patch = server_contract_version()

    shared.commit(manifest=_manifest(id=shared.manifest_id, contract_version=f"{major + 1}.0.0"))
    shared.refused(
        "This agent was made with a newer version of Cinna than this server. "
        "Ask your admin to update Cinna."
    )

    shared.commit(manifest=_manifest(id=shared.manifest_id, contract_version=f"{major - 1}.9.0"))
    shared.refused(
        "This agent was made with an older version of Cinna. "
        "Open it in Cinna Desktop to update it, then share again."
    )

    shared.commit(manifest=_manifest(id=shared.manifest_id, contract_version=f"{major}.{minor + 1}.0"))
    result = shared.git.push(shared.work)
    assert result.ok, result.output
    assert "slightly newer version of Cinna" in result.output
    assert shared.head() != shared.baseline


def test_refuses_a_changed_manifest_id(shared: Shared) -> None:
    """The manifest ``id`` bound at create is the agent's identity."""
    shared.commit(manifest=_manifest())
    shared.refused("This agent's identity changed; share it as a new agent instead.")
    shared.commit(manifest=_manifest(id=shared.manifest_id.upper()))
    result = shared.git.push(shared.work)
    assert result.ok, result.output  # the same id in another case is the same agent


def test_refuses_refs_other_than_main(shared: Shared) -> None:
    shared.commit({"x.md": "x"})
    shared.refused(
        "Only the agent's main version can be shared to the catalog.",
        "HEAD:refs/heads/other",
    )
    shared.refused(
        "Only the agent's main version can be shared to the catalog.",
        "HEAD:refs/tags/v1",
    )


def test_policy_is_case_insensitive(shared: Shared) -> None:
    """Credentials/x, APP-DATA/x and .ENV are refused like their lower-case forms."""
    shared.commit({"Credentials/x": "v"})
    shared.refused("credentials folder")
    shared.commit(remove=["Credentials/x"])
    shared.commit({"APP-DATA/x": "v"})
    shared.refused("app-data folder")
    shared.commit(remove=["APP-DATA/x"])
    shared.commit({".ENV": "v"})
    shared.refused("This change includes .ENV,")


def test_symlinks_in_agent_tree_are_refused(shared: Shared) -> None:
    (shared.work / "link").symlink_to("/etc/passwd")
    shared.git.commit_all(shared.work, "link")
    result = shared.git.push(shared.work)
    assert not result.ok, result.output
    assert shared.head() == shared.baseline


def test_receive_pack_is_unavailable_without_the_hook(
    client: TestClient, shared: Shared, tmp_path
) -> None:
    """No hook means no policy: pushes are refused with 503, reads keep working."""
    empty = tmp_path / "no-hooks"
    empty.mkdir()
    headers = {"Authorization": f"Bearer {shared.owner.token}"}
    repo_id = shared.repo_id
    with patch.object(settings, "GIT_HOOKS_DIR", str(empty)):
        r = client.get(
            f"/git/{repo_id}.git/info/refs", params={"service": "git-receive-pack"}, headers=headers
        )
        assert r.status_code == 503
        r = client.post(f"/git/{repo_id}.git/git-receive-pack", headers=headers, content=b"0000")
        assert r.status_code == 503
        r = client.get(
            f"/git/{repo_id}.git/info/refs", params={"service": "git-upload-pack"}, headers=headers
        )
        assert r.status_code == 200


def test_junk_git_protocol_header_is_dropped(client: TestClient, shared: Shared, tmp_path) -> None:
    headers = {
        "Authorization": f"Bearer {shared.owner.token}",
        "Git-Protocol": "version=2; rm -rf /\nevil=1",
    }
    r = client.get(
        f"/git/{shared.repo_id}.git/info/refs",
        params={"service": "git-upload-pack"},
        headers={"Authorization": headers["Authorization"], "Git-Protocol": "x=\x01junk"},
    )
    assert r.status_code == 200


CLAUDE_SETTINGS = (
    "This change includes the agent's Claude permission settings "
    "(.claude/settings.local.json), which never leave your computer."
)


@pytest.mark.parametrize(
    "path",
    [".claude/settings.local.json", ".Claude/Settings.Local.json", ".CLAUDE/SETTINGS.LOCAL.JSON"],
)
def test_refuses_claude_local_settings_file_any_case(shared: Shared, path: str) -> None:
    shared.commit({path: "{}"})
    shared.refused(CLAUDE_SETTINGS)


@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.json",
        ".claude/settings.local.json.example",
        ".claude/settings.local.jsonx",
    ],
)
def test_similarly_named_claude_files_are_accepted(shared: Shared, path: str) -> None:
    """File entries match one exact path, not a prefix."""
    shared.commit({path: "{}"})
    result = shared.git.push(shared.work)
    assert result.ok, result.output
    assert shared.head() != shared.baseline


def test_refuses_claude_settings_added_then_removed(shared: Shared) -> None:
    """Like a secret, the file still travels in the pack of an intermediate commit."""
    shared.commit({".claude/settings.local.json": "{}"})
    shared.commit(remove=[".claude/settings.local.json"])
    shared.refused(CLAUDE_SETTINGS)
