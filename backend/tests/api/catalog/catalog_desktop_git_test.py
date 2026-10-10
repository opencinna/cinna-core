"""Desktop agents over git smart HTTP, driven by a real ``git`` binary.

``git clone`` / ``commit`` / ``push`` / ``pull`` run as subprocesses against a
loopback bridge (``tests/utils/git_http_bridge.py``) that forwards to the app
under test, so route registration, auth, the catalog access check, ``git
upload-pack``/``receive-pack``, the ``pre-receive`` hook and the post-push
revision all execute for real. See ``tests/api/catalog/README.md``.

Scenarios:
  1. Round trip: clone empty -> push (Version 1) -> clone elsewhere -> second
     push (Version 2, notes = commit message) -> pull, over protocol v0 and v2.
  2. Non-fast-forward and branch delete are refused; head_sha is untouched.
  3. Auth: 401 + Basic challenge, Basic ``x-access-token``, every accepted
     token kind and every rejected one.
  4. Access matrix (use/edit/maintain x user/role, public visibility) over the wire.
  5. Real git honours the grants (user push 403, stranger clone 403).
  6. gzip request body on upload-pack.
  7. An accepted push sends ``catalog.updated`` to grant holders and audits;
     a refused one does neither.
"""
import base64
import gzip

import pytest
from fastapi.testclient import TestClient

from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.cli import (
    bootstrap_account_token,
    cli_auth_headers,
    create_setup_token,
    exchange_setup_token,
    list_cli_tokens,
    revoke_cli_token,
)
from tests.utils.desktop_auth import obtain_desktop_tokens, revoke_desktop_client
from tests.utils.catalog import (
    GitClient,
    create_desktop_agent,
    get_item,
    list_items,
    make_person,
    manifest_id_of,
    patch_item,
    put_grants,
    repo_id_of,
    role_grant,
    user_grant,
    write_agent_tree,
)
from tests.utils.platform_token import mint_platform_token
from tests.utils.security_event import events_of_type


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _basic(token: str) -> dict[str, str]:
    raw = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _refs(client: TestClient, repo_id: str, service: str, headers=None):
    return client.get(
        f"/git/{repo_id}.git/info/refs", params={"service": service}, headers=headers or {}
    )


def _head_sha(client: TestClient, headers: dict, item: dict) -> str | None:
    return get_item(client, headers, item["uuid"])["desktop"]["head_sha"]


def _pkt(payload: bytes) -> bytes:
    return b"%04x" % (len(payload) + 4) + payload


def _seed_first_push(client, bridge, tmp_path, owner, repo) -> tuple[GitClient, object, str]:
    """Clone the empty repo as ``owner``, push a valid agent, return (git, work, sha)."""
    git = GitClient(tmp_path, token=owner.token)
    work = tmp_path / "work-a"
    assert git.clone(bridge.repo_url(repo_id_of(repo)), work).ok
    write_agent_tree(work, slug=repo["name"], manifest_id=manifest_id_of(repo))
    sha = git.commit_all(work, "first")
    result = git.push(work)
    assert result.ok, result.output
    return git, work, sha


def test_real_git_round_trip_over_protocol_v0_and_v2(
    client: TestClient, bridge, tmp_path, superuser_token_headers
) -> None:
    """
    1. Owner creates a desktop agent and clones the empty repo
    2. Owner commits a valid agent and pushes -> Version 1 with head_sha + manifest
    3. A second person clones it (protocol v0 and v2) and sees the files
    4. Owner pushes a second commit -> Version 2; the second person pulls it
    """
    owner = make_person(client, superuser_token_headers)
    reader = make_person(client, superuser_token_headers)
    repo = create_desktop_agent(
        client, owner.headers, display_name="Round Trip", visibility="users",
        grants=[user_grant(reader.id, "use")],
    )
    repo_id = repo_id_of(repo)
    assert repo["desktop"]["clone_url"].endswith(f"/git/{repo_id}.git")
    assert repo["desktop"]["head_sha"] is None and repo["latest"] is None
    assert repo["runs_on"] == "desktop" and repo["access"] == "maintain"

    # ── Phase 1-2: clone empty, push ──────────────────────────────────
    git, work, sha1 = _seed_first_push(client, bridge, tmp_path, owner, repo)
    got = get_item(client, owner.headers, repo["uuid"])
    assert got["desktop"]["head_sha"] == sha1
    assert got["desktop"]["contract_version"]
    assert got["latest"]["revision_number"] == 1
    assert got["latest"]["published_by"]["user_id"] == owner.id
    assert got["latest"]["release_notes"] == "first"
    assert [r["git_commit_sha"] for r in got["revisions"]] == [sha1]
    assert repo["uuid"] in {i["uuid"] for i in list_items(client, reader.headers, kind="agent")}

    # ── Phase 3: another person clones, over both wire protocols ──────
    for protocol in ("0", "2"):
        rgit = GitClient(tmp_path, token=reader.token, protocol=protocol)
        dest = tmp_path / f"clone-v{protocol}"
        result = rgit.clone(bridge.repo_url(repo_id), dest)
        assert result.ok, f"protocol v{protocol}: {result.output}"
        assert (dest / "cinna-agent.json").exists()
        assert (dest / "AGENTS.md").read_text() == "# My agent\n"
        assert rgit.must("rev-parse", "HEAD", cwd=dest).output.strip() == sha1

    # ── Phase 4: second push, then the reader pulls it ────────────────
    (work / "notes.md").write_text("second\n")
    sha2 = git.commit_all(work, "second")
    assert git.push(work).ok
    assert _head_sha(client, owner.headers, repo) == sha2
    latest = get_item(client, owner.headers, repo["uuid"])["latest"]
    assert latest["revision_number"] == 2 and latest["release_notes"] == "second"
    pulled = rgit.run("pull", "--ff-only", "origin", "main", cwd=tmp_path / "clone-v2")
    assert pulled.ok, pulled.output
    assert (tmp_path / "clone-v2" / "notes.md").read_text() == "second\n"
    assert rgit.must("fetch", "origin", cwd=tmp_path / "clone-v0").ok


def test_non_fast_forward_and_branch_delete_are_refused(
    client: TestClient, bridge, tmp_path, superuser_token_headers
) -> None:
    """
    1. Two clones of the same agent; the first pushes -> accepted
    2. The second pushes a diverging commit -> refused (non-fast-forward)
    3. Deleting main is refused
    4. After a rebase the second push goes through; head_sha follows
    """
    owner = make_person(client, superuser_token_headers)
    repo = create_desktop_agent(client, owner.headers)
    repo_id = repo_id_of(repo)
    git, work_a, _ = _seed_first_push(client, bridge, tmp_path, owner, repo)
    work_b = tmp_path / "work-b"
    assert git.clone(bridge.repo_url(repo_id), work_b).ok

    (work_a / "a.md").write_text("a\n")
    sha_a = git.commit_all(work_a, "from a")
    assert git.push(work_a).ok

    # ── Phase 2: diverged push ────────────────────────────────────────
    (work_b / "b.md").write_text("b\n")
    sha_b = git.commit_all(work_b, "from b")
    rejected = git.push(work_b)
    assert not rejected.ok
    assert "rejected" in rejected.output.lower()
    assert _head_sha(client, owner.headers, repo) == sha_a

    # ── Phase 3: forced overwrite and delete are refused too ──────────
    forced = git.push(work_b, "--force", "HEAD:refs/heads/main")
    assert not forced.ok
    assert _head_sha(client, owner.headers, repo) == sha_a
    deleted = git.push(work_b, ":main")
    assert not deleted.ok
    assert _head_sha(client, owner.headers, repo) == sha_a

    # ── Phase 4: rebase and push ──────────────────────────────────────
    assert git.run("pull", "--rebase", "origin", "main", cwd=work_b).ok
    assert git.push(work_b).ok
    new_head = _head_sha(client, owner.headers, repo)
    assert new_head not in (sha_a, sha_b, None)
    fresh = tmp_path / "fresh"
    assert git.clone(bridge.repo_url(repo_id), fresh).ok
    assert (fresh / "a.md").exists() and (fresh / "b.md").exists()


def test_authentication_over_http(
    client: TestClient, bridge, tmp_path, superuser_token_headers
) -> None:
    """
    1. No token / garbage token -> 401 with Basic realm="cinna" (every endpoint)
    2. Bearer and Basic ``x-access-token`` both work
    3. Unknown repo and malformed repo path -> 404; unsupported service -> 403
    4. A real git without credentials cannot clone
    """
    owner = make_person(client, superuser_token_headers)
    repo = create_desktop_agent(client, owner.headers)
    repo_id = repo_id_of(repo)

    # ── Phase 1: unauthenticated ──────────────────────────────────────
    for service in ("git-upload-pack", "git-receive-pack"):
        for headers in (None, _bearer("not-a-jwt"), {"Authorization": "Basic !!!"}):
            r = _refs(client, repo_id, service, headers)
            assert r.status_code == 401, (service, headers, r.text)
            assert r.headers["WWW-Authenticate"] == 'Basic realm="cinna"'
    for service in ("git-upload-pack", "git-receive-pack"):
        r = client.post(f"/git/{repo_id}.git/{service}", content=b"0000")
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"] == 'Basic realm="cinna"'

    # ── Phase 2: Bearer and Basic ─────────────────────────────────────
    for headers in (_bearer(owner.token), _basic(owner.token)):
        r = _refs(client, repo_id, "git-upload-pack", headers)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/x-git-upload-pack-advertisement"
        assert r.content.startswith(b"001e# service=git-upload-pack\n")
        assert "no-cache" in r.headers["cache-control"]
        r = _refs(client, repo_id, "git-receive-pack", headers)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/x-git-receive-pack-advertisement"

    # ── Phase 3: 404s and unsupported service ─────────────────────────
    missing = "00000000-0000-0000-0000-000000000000"
    assert _refs(client, missing, "git-upload-pack", _bearer(owner.token)).status_code == 404
    assert _refs(client, "not-a-uuid", "git-upload-pack", _bearer(owner.token)).status_code == 404
    assert _refs(client, repo_id, "", _bearer(owner.token)).status_code == 403  # dumb protocol
    # Unknown repo does not leak through auth order: bad token still 401.
    assert _refs(client, missing, "git-upload-pack").status_code == 401

    # ── Phase 4: real git, no credentials ─────────────────────────────
    anon = GitClient(tmp_path)
    cloned = anon.clone(bridge.repo_url(repo_id), tmp_path / "anon")
    assert not cloned.ok
    assert not (tmp_path / "anon" / ".git").exists() or not any((tmp_path / "anon").iterdir())


def test_every_accepted_and_rejected_token_kind(
    client: TestClient, bridge, tmp_path, superuser_token_headers
) -> None:
    """
    Accepted: web JWT, desktop access token, account CLI token, per-agent CLI
    token (resolves to the agent's owner). Rejected: revoked desktop client,
    revoked CLI token, agent-env token, expired token.
    """
    dev = make_person(client, superuser_token_headers, role="agent-developer")
    repo = create_desktop_agent(client, dev.headers)
    repo_id = repo_id_of(repo)
    url = bridge.repo_url(repo_id)

    def upload(headers):
        return _refs(client, repo_id, "git-upload-pack", headers).status_code

    def receive(headers):
        return _refs(client, repo_id, "git-receive-pack", headers).status_code

    # ── Accepted ──────────────────────────────────────────────────────
    assert receive(_bearer(dev.token)) == 200

    desktop = obtain_desktop_tokens(client, dev.headers)
    assert receive(_bearer(desktop["access_token"])) == 200
    assert receive(_basic(desktop["access_token"])) == 200

    account_jwt, _account_token_id = bootstrap_account_token(client, dev.headers)
    assert receive(_bearer(account_jwt)) == 200

    create_random_ai_credential(client, dev.headers, set_default=True)
    agent = create_agent_via_api(client, dev.headers)
    setup = create_setup_token(client, dev.headers, agent["id"])
    cli_jwt = exchange_setup_token(client, setup["token"])["cli_token"]
    assert receive(cli_auth_headers(cli_jwt)) == 200
    assert receive(_basic(cli_jwt)) == 200

    # A real clone with each of the non-web tokens.
    for i, token in enumerate((desktop["access_token"], account_jwt, cli_jwt)):
        result = GitClient(tmp_path, token=token, auth="basic").clone(url, tmp_path / f"c{i}")
        assert result.ok, result.output

    # ── Rejected ──────────────────────────────────────────────────────
    revoke_desktop_client(client, dev.headers, desktop["client_id"])
    assert upload(_bearer(desktop["access_token"])) == 401

    token_id = list_cli_tokens(client, dev.headers, agent["id"])[0]["id"]
    revoke_cli_token(client, dev.headers, token_id)
    assert upload(cli_auth_headers(cli_jwt)) == 401
    assert upload(cli_auth_headers(account_jwt)) == 200  # unrelated token unaffected

    env_token = mint_platform_token(dev.id, extra_claims={"token_type": "agent_env"})
    assert upload(_bearer(env_token)) == 401
    from datetime import timedelta

    expired = mint_platform_token(dev.id, expires_delta=timedelta(minutes=-5))
    assert upload(_bearer(expired)) == 401
    assert not GitClient(tmp_path, token=desktop["access_token"]).clone(
        url, tmp_path / "revoked"
    ).ok


def test_access_matrix_over_the_wire(
    client: TestClient, tmp_path, superuser_token_headers
) -> None:
    """
    For every (principal kind x level) the matching subject can clone
    (upload-pack advertisement) and, from ``edit`` up, push (receive-pack); a
    non-matching person gets 403 both ways. ``use`` counts only while the agent
    is ``visibility='users'``; ``public`` gives everyone ``use``. Effective
    access is the max over grants; a superuser may clone any agent but needs
    ``edit`` to push.
    """
    owner = make_person(client, superuser_token_headers)
    plain = make_person(client, superuser_token_headers)
    other = make_person(client, superuser_token_headers)
    developer = make_person(client, superuser_token_headers, role="agent-developer")
    item = create_desktop_agent(client, owner.headers, visibility="users")
    repo_id = repo_id_of(item)
    owner_grant = user_grant(owner.id, "maintain")

    def codes(person_headers):
        return (
            _refs(client, repo_id, "git-upload-pack", person_headers).status_code,
            _refs(client, repo_id, "git-receive-pack", person_headers).status_code,
        )

    expected = {"use": (200, 403), "edit": (200, 200), "maintain": (200, 200)}
    cases = [
        ("user", plain, lambda a: user_grant(plain.id, a), ("use", "edit", "maintain")),
        ("role", developer, lambda a: role_grant("agent-developer", a), ("use", "edit")),
    ]
    for kind, subject, make_grant, levels in cases:
        for level in levels:
            put_grants(client, owner.headers, item["uuid"], [owner_grant, make_grant(level)])
            assert codes(_bearer(subject.token)) == expected[level], (kind, level)
            assert codes(_bearer(other.token)) == (403, 403), (kind, level)

    # ``use`` stops counting once the agent is private again.
    put_grants(client, owner.headers, item["uuid"], [owner_grant, user_grant(plain.id, "use")])
    patch_item(client, owner.headers, item["uuid"], visibility="private")
    assert codes(_bearer(plain.token)) == (403, 403)

    # Public reaches any person, for cloning only.
    patch_item(client, owner.headers, item["uuid"], visibility="public")
    assert codes(_bearer(other.token)) == (200, 403)

    # Max over several matching grants: user use + role edit -> edit.
    put_grants(
        client, owner.headers, item["uuid"],
        [owner_grant, user_grant(developer.id, "use"), role_grant("agent-developer", "edit")],
    )
    assert codes(_bearer(developer.token)) == (200, 200)

    # Narrowed back to the owner and private: everyone else is out.
    patch_item(client, owner.headers, item["uuid"], visibility="private")
    put_grants(client, owner.headers, item["uuid"], [owner_grant])
    assert codes(_bearer(plain.token)) == (403, 403)
    assert codes(_bearer(owner.token)) == (200, 200)

    # Superuser: may clone any repo, but pushing still needs ``edit``.
    assert codes(superuser_token_headers) == (200, 403)
    put_grants(client, owner.headers, item["uuid"], [owner_grant, role_grant("admin", "edit")])
    assert codes(superuser_token_headers) == (200, 200)


def test_real_git_honours_grants(
    client: TestClient, bridge, tmp_path, superuser_token_headers
) -> None:
    """
    1. A ``use`` holder clones but cannot push (403)
    2. An ``edit`` holder (role) pushes; the new version names them; the owner's clone pulls it
    3. Narrowed to the owner: the former user can no longer clone (403)
    """
    owner = make_person(client, superuser_token_headers)
    reader = make_person(client, superuser_token_headers)
    writer = make_person(client, superuser_token_headers, role="agent-developer")
    repo = create_desktop_agent(client, owner.headers, visibility="users")
    repo_id = repo_id_of(repo)
    url = bridge.repo_url(repo_id)
    ogit, owork, sha1 = _seed_first_push(client, bridge, tmp_path, owner, repo)
    owner_grant = user_grant(owner.id, "maintain")
    put_grants(
        client, owner.headers, repo["uuid"],
        [owner_grant, user_grant(reader.id, "use"), role_grant("agent-developer", "edit")],
    )

    # ── Phase 1: reader ───────────────────────────────────────────────
    rgit = GitClient(tmp_path, token=reader.token)
    rwork = tmp_path / "reader"
    assert rgit.clone(url, rwork).ok
    (rwork / "r.md").write_text("r\n")
    rgit.commit_all(rwork, "reader change")
    denied = rgit.push(rwork)
    assert not denied.ok and "403" in denied.output
    assert _head_sha(client, owner.headers, repo) == sha1

    # ── Phase 2: writer ───────────────────────────────────────────────
    wgit = GitClient(tmp_path, token=writer.token)
    wwork = tmp_path / "writer"
    assert wgit.clone(url, wwork).ok
    (wwork / "w.md").write_text("w\n")
    sha_w = wgit.commit_all(wwork, "writer change")
    pushed = wgit.push(wwork)
    assert pushed.ok, pushed.output
    assert _head_sha(client, owner.headers, repo) == sha_w
    latest = get_item(client, owner.headers, repo["uuid"])["latest"]
    assert latest["published_by"]["user_id"] == writer.id
    assert latest["release_notes"] == "writer change"
    assert ogit.run("pull", "--ff-only", "origin", "main", cwd=owork).ok
    assert (owork / "w.md").exists()

    # ── Phase 3: access removed ───────────────────────────────────────
    put_grants(client, owner.headers, repo["uuid"], [owner_grant])
    gone = rgit.clone(url, tmp_path / "reader2")
    assert not gone.ok and "403" in gone.output
    assert not wgit.run("fetch", "origin", cwd=wwork).ok


def test_gzip_request_body_is_accepted_by_upload_pack(
    client: TestClient, bridge, tmp_path, superuser_token_headers
) -> None:
    """A raw POST of gzip-compressed pkt-lines (what git sends for big wants) -> 200 + pack."""
    owner = make_person(client, superuser_token_headers)
    repo = create_desktop_agent(client, owner.headers)
    _git, _work, sha = _seed_first_push(client, bridge, tmp_path, owner, repo)
    url = f"/git/{repo_id_of(repo)}.git/git-upload-pack"
    request = _pkt(f"want {sha} ofs-delta agent=test\n".encode()) + b"0000" + _pkt(b"done\n")
    headers = {
        **_bearer(owner.token),
        "Content-Type": "application/x-git-upload-pack-request",
    }

    plain = client.post(url, headers=headers, content=request)
    assert plain.status_code == 200
    assert plain.headers["content-type"] == "application/x-git-upload-pack-result"
    assert b"PACK" in plain.content

    zipped = client.post(
        url, headers={**headers, "Content-Encoding": "gzip"}, content=gzip.compress(request)
    )
    assert zipped.status_code == 200
    assert b"PACK" in zipped.content
    assert zipped.content[:8] == plain.content[:8]  # same NAK preamble

    # A body that claims gzip but is not gzip does not hang or 500 the server.
    bad = client.post(
        url, headers={**headers, "Content-Encoding": "gzip"}, content=b"not gzip at all"
    )
    assert bad.status_code in (200, 400, 413)
    assert b"PACK" not in bad.content


def _emits(stub, event_type: str) -> list[dict]:
    return [
        e for e in stub.emitted_events
        if e["event"] == "event" and e["data"]["type"] == event_type
    ]


def test_push_events_audit_and_refused_push_silence(
    client: TestClient, bridge, tmp_path, superuser_token_headers, socket_stub
) -> None:
    """
    1. Owner shares with one ``use`` holder only (a bystander has no grant)
    2. Accepted push -> catalog.updated to owner + holder rooms, not bystander;
       a CATALOG_DESKTOP_PUSH_ACCEPTED audit row
    3. Refused push -> no further catalog.updated, no further audit row
    """
    owner = make_person(client, superuser_token_headers)
    reader = make_person(client, superuser_token_headers)
    bystander = make_person(client, superuser_token_headers)
    item = create_desktop_agent(
        client, owner.headers, visibility="users", grants=[user_grant(reader.id, "use")]
    )

    git = GitClient(tmp_path, token=owner.token)
    work = tmp_path / "w"
    assert git.clone(bridge.repo_url(repo_id_of(item)), work).ok
    write_agent_tree(work, slug=item["name"], manifest_id=manifest_id_of(item))
    sha = git.commit_all(work, "first")
    socket_stub.emitted_events.clear()
    assert git.push(work).ok

    updated = _emits(socket_stub, "catalog.updated")
    rooms = {e["room"] for e in updated}
    assert rooms == {f"user_{owner.id}", f"user_{reader.id}"}
    assert f"user_{bystander.id}" not in rooms
    assert all(e["room"] for e in socket_stub.emitted_events)  # never a no-room broadcast
    meta = updated[0]["data"]["meta"]
    assert meta == {
        "kind": "agent",
        "uuid": item["uuid"],
        "id": item["id"],
        "revision_number": 1,
        "head_sha": sha,
    }

    audit = events_of_type(client, owner.headers, "CATALOG_DESKTOP_PUSH_ACCEPTED")
    assert len(audit) == 1
    assert audit[0]["details"]["slug"] == item["name"]
    assert audit[0]["details"]["after"] == sha and audit[0]["details"]["before"] is None
    assert events_of_type(client, owner.headers, "CATALOG_DESKTOP_AGENT_CREATED")

    # ── refused push ──────────────────────────────────────────────────
    (work / ".env").write_text("K=1")
    git.commit_all(work, "secret")
    socket_stub.emitted_events.clear()
    assert not git.push(work).ok
    assert _emits(socket_stub, "catalog.updated") == []
    assert len(events_of_type(client, owner.headers, "CATALOG_DESKTOP_PUSH_ACCEPTED")) == 1
