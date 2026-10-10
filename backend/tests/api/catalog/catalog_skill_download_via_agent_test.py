"""C4 — U7: a skill's files open to whoever can use a desktop agent pinning it.

A private skill uploaded from a desktop is pinned by a desktop agent
(``catalog_skills[]`` in the pushed manifest). Someone who may ``use`` that
agent but holds no grant on the skill can take exactly the pinned revision —
``/download``, ``/content`` and ``/files`` — and nothing else. The skill's own
catalog item never opens this way. Pushes go over real git through the bridge
(see README "Real git, honestly").

Scenarios:
  1. A uploads ``pin-tool`` (private) and a desktop agent pinning revision 1,
     shared with C (``use``). C downloads rev 1 (hash matches the pin),
     reads ``/content`` and ``/files``; ``GET /catalog/items/{uuid}`` stays
     404 for C; a bystander gets 404 everywhere.
  2. A uploads revision 2: C gets 404 for it (not pinned). A pushes an agent
     version pinning 2: C gets 2, revision 1 is now 404 (latest only). A
     revision that does not exist is 404.
  3. A pushes an agent version without the pin: C gets 404 again.
  4. Access to the agent decides: going private → 404, back to ``users`` →
     200, removing C's grant → 404.
"""
import hashlib
import io
import json
import tarfile
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import make_person as make_developer
from tests.utils.catalog import (
    GitClient,
    create_desktop_agent,
    get_item,
    make_person,
    manifest_id_of,
    patch_item,
    put_grants,
    repo_id_of,
    server_contract_version,
    user_grant,
    write_agent_tree,
)

API = settings.API_V1_STR


class _Share:
    """A clone of a desktop agent's repo; each ``push`` is one agent version."""

    def __init__(self, bridge, tmp_path, owner, item) -> None:
        self.item = item
        self.git = GitClient(tmp_path, token=owner.headers["Authorization"].removeprefix("Bearer "))
        self.work = tmp_path / f"work-{uuid.uuid4().hex[:6]}"
        assert self.git.clone(bridge.repo_url(repo_id_of(item)), self.work).ok
        self.n = 0

    def push(self, catalog_skills: list[dict]) -> None:
        major, minor, patch = server_contract_version()
        manifest = {
            "id": manifest_id_of(self.item),
            "name": "Pinning Agent",
            "slug": self.item["name"],
            "description": "Pins catalog skills",
            "contract_version": f"{major}.{minor}.{patch}",
            "catalog_skills": catalog_skills,
        }
        write_agent_tree(
            self.work, slug=self.item["name"], manifest=manifest,
            manifest_id=manifest_id_of(self.item),
        )
        self.n += 1
        (self.work / f"step-{self.n}.md").write_text(str(self.n))
        self.git.commit_all(self.work, f"version {self.n}")
        result = self.git.push(self.work)
        assert result.ok, result.output


def _skill_tgz(name: str, files: dict[str, str] | None = None, body: str = "Step one.") -> bytes:
    """``tar -C skills -czf x.tgz <name>`` built in memory."""
    content = {
        "SKILL.md": f"---\nname: {name}\ndescription: Does {name} things.\n---\n\n{body}\n",
        **(files or {}),
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, text in content.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{name}/{rel}")
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _publish(client, headers, archive: bytes, meta: dict | None = None):
    return client.post(
        f"{API}/skills/packages/publish",
        headers=headers,
        files={"archive": ("skill.tgz", archive, "application/gzip")},
        data={"meta": json.dumps(meta or {})},
    )


def _rev_url(pkg: str, n: int, tail: str) -> str:
    return f"{API}/skills/packages/{pkg}/revisions/{n}/{tail}"


def _status(client, headers, pkg: str, n: int, tail: str = "download") -> int:
    return client.get(_rev_url(pkg, n, tail), headers=headers).status_code


def _not_found(client, headers, pkg: str, n: int) -> None:
    for tail in ("download", "content", "files"):
        r = client.get(_rev_url(pkg, n, tail), headers=headers)
        assert r.status_code == 404, f"{tail} rev {n}: {r.status_code} {r.text}"
        assert r.json()["detail"]["code"] == "package_not_found", r.text


def test_pinned_skill_revision_downloads_through_a_usable_desktop_agent(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    su = superuser_token_headers
    a = make_developer(client, su)
    c = make_person(client, su)
    bystander = make_person(client, su)

    # ── Phase 1: rev 1 pinned, C reaches it through the agent ─────────
    rev1 = _publish(client, a.headers, _skill_tgz("pin-tool", {"ref/notes.md": "# notes\n"}))
    assert rev1.status_code == 200, rev1.text
    pkg = rev1.json()["package_id"]
    skill_item = get_item(client, a.headers, pkg)
    assert skill_item["visibility"] == "private"
    sha1 = skill_item["skill"]["archive_sha256"]

    def pin(n: int, sha: str) -> dict:
        return {
            "package_id": skill_item["id"], "package_uuid": pkg, "name": "pin-tool",
            "revision_number": n, "archive_sha256": sha,
        }

    agent = create_desktop_agent(
        client, a.headers, visibility="users", grants=[user_grant(c.id, "use")]
    )
    share = _Share(bridge, tmp_path, a, agent)
    _not_found(client, c.headers, pkg, 1)  # no agent version pins it yet
    share.push([pin(1, sha1)])
    assert get_item(client, c.headers, agent["uuid"])["latest"]["revision_number"] == 1

    download = client.get(_rev_url(pkg, 1, "download"), headers=c.headers)
    assert download.status_code == 200, download.text
    assert download.headers["x-content-sha256"] == sha1
    assert hashlib.sha256(download.content).hexdigest() == sha1
    content = client.get(_rev_url(pkg, 1, "content"), headers=c.headers)
    assert content.status_code == 200, content.text
    assert "name: pin-tool" in content.text
    files = client.get(_rev_url(pkg, 1, "files"), headers=c.headers)
    assert files.status_code == 200, files.text
    assert files.json()["count"] == 2

    # the skill's own catalog item does not open through the agent
    assert client.get(f"{API}/catalog/items/{pkg}", headers=c.headers).status_code == 404
    assert client.get(f"{API}/skills/packages/{pkg}", headers=c.headers).status_code == 404
    _not_found(client, bystander.headers, pkg, 1)

    # ── Phase 2: only the latest agent version's exact revision ───────
    rev2 = _publish(client, a.headers, _skill_tgz("pin-tool", body="Two."), {"package_uuid": pkg})
    assert rev2.status_code == 200, rev2.text
    assert rev2.json()["revision_number"] == 2
    _not_found(client, c.headers, pkg, 2)
    assert _status(client, c.headers, pkg, 1) == 200

    sha2 = get_item(client, a.headers, pkg)["skill"]["archive_sha256"]
    assert sha2 != sha1
    share.push([pin(2, sha2)])
    second = client.get(_rev_url(pkg, 2, "download"), headers=c.headers)
    assert second.status_code == 200, second.text
    assert second.headers["x-content-sha256"] == sha2
    _not_found(client, c.headers, pkg, 1)
    _not_found(client, c.headers, pkg, 99)

    # ── Phase 3: the pin removed in a later push ──────────────────────
    share.push([])
    _not_found(client, c.headers, pkg, 2)
    share.push([pin(2, sha2)])
    assert _status(client, c.headers, pkg, 2) == 200

    # ── Phase 4: access to the agent decides ──────────────────────────
    patch_item(client, a.headers, agent["uuid"], visibility="private")
    _not_found(client, c.headers, pkg, 2)
    patch_item(client, a.headers, agent["uuid"], visibility="users")
    assert _status(client, c.headers, pkg, 2) == 200
    put_grants(client, a.headers, agent["uuid"], [user_grant(a.id, "maintain")])
    _not_found(client, c.headers, pkg, 2)
    assert client.get(f"{API}/catalog/items/{agent['uuid']}", headers=c.headers).status_code == 404

    # the publisher reads every revision regardless
    assert _status(client, a.headers, pkg, 1) == 200
    assert _status(client, a.headers, pkg, 2) == 200
