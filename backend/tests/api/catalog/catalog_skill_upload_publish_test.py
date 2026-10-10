"""C4 — skills published from a desktop (``/skills/packages/publish[-preview]``).

The desktop sends ``tar -C skills -czf x.tgz <name>`` plus a ``meta`` form
field holding JSON (P11, §0.3-4). Archives are built in memory with
``tarfile``; nothing is read from an agent workspace.

Scenarios:
  1. Valid first publish by a plain ``agent-user`` (U5): revision 1, version
     ``1.0.0`` written back into the stored ``SKILL.md`` (visible in the
     download, ``/content`` and the revision frontmatter), download hash ==
     ``X-Content-SHA256`` == the catalog item's ``archive_sha256``; a
     frontmatter version is honoured; ``meta.version`` wins.
  2. Credential slots from ``frontmatter.credentials`` → specs, all
     ``provided_by='user'``; an invalid block is ``skill_invalid``.
  3. Preview: first vs republish, credentials, and its refusals (bad name,
     unknown / invisible package, ``use`` only, name mismatch).
  4. Republish needs ``edit``: a second publish without ``package_uuid`` is
     409 ``package_exists`` (body carries the uuid); with it, the maintainer
     and an ``edit`` grantee publish; ``use`` 403, stranger 404, another
     skill's folder 409 ``skill_name_mismatch``; sharing changes by an editor
     403.
  5. Grants and visibility on publish: additive, ``use`` under non-``users``
     visibility 409, invalid grants 422, bad ``meta`` 422; events
     ``catalog.updated {kind:'skill'}`` and ``catalog.granted``.
  6. Content refusals: invalid ``SKILL.md``, a secret file — 422, nothing
     written.
  7. Oversize: compressed > 16 MiB 413, uncompressed > 16 MiB 400.
  8. Unsafe archives, one per refusal: absolute path, ``..``, symlink,
     hardlink, fifo, two top-level folders, a loose top-level file, > 2000
     members, not a gzip, empty — 400 ``skill_archive_invalid``.
"""
import hashlib
import io
import json
import tarfile

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import get_item_grants, grant_levels
from tests.utils.catalog import (
    get_item,
    list_items,
    make_person,
    role_grant,
    user_grant,
)
from tests.utils.skill_catalog import error_code

API = settings.API_V1_STR
PUBLISH = f"{API}/skills/packages/publish"
PREVIEW = f"{API}/skills/packages/publish-preview"
MiB = 1024 * 1024


# ── Archive builders ───────────────────────────────────────────────────────


def _skill_md(name: str, *, extra_front: str = "", body: str = "Step one.") -> str:
    front = f"name: {name}\ndescription: Does {name} things.\n{extra_front}"
    return f"---\n{front.rstrip()}\n---\n\n# {name}\n\n{body}\n"


def _member(tar: tarfile.TarFile, name: str, data: bytes | None = None, **attrs) -> None:
    info = tarfile.TarInfo(name)
    for key, value in attrs.items():
        setattr(info, key, value)
    if data is not None and info.type == tarfile.REGTYPE:
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    else:
        tar.addfile(info)


def _tgz(build) -> bytes:
    """A tar.gz whose members ``build(tar)`` adds."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        build(tar)
    return buf.getvalue()


def _skill_tgz(name: str, files: dict[str, str | bytes] | None = None, **md) -> bytes:
    """``<name>/`` with ``SKILL.md`` (unless ``files`` overrides it) + files."""
    content: dict[str, str | bytes] = {"SKILL.md": _skill_md(name, **md), **(files or {})}

    def build(tar: tarfile.TarFile) -> None:
        _member(tar, name, type=tarfile.DIRTYPE, mode=0o755)
        for rel, data in content.items():
            raw = data.encode() if isinstance(data, str) else data
            _member(tar, f"{name}/{rel}", raw, mode=0o644)

    return _tgz(build)


def _publish(client, headers, archive: bytes, meta: dict | str | None = None):
    form = meta if isinstance(meta, str) else json.dumps(meta or {})
    return client.post(
        PUBLISH,
        headers=headers,
        files={"archive": ("skill.tgz", archive, "application/gzip")},
        data={"meta": form},
    )


def _ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def _refused(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    assert error_code(response.json()) == code, response.text
    return response.json()["detail"]


def _preview(client, headers, name: str, frontmatter: dict | None = None, package_uuid=None):
    body: dict = {"name": name, "frontmatter": frontmatter or {"name": name}}
    if package_uuid is not None:
        body["package_uuid"] = package_uuid
    return client.post(PREVIEW, headers=headers, json=body)


def _download(client, headers, package_uuid: str, n: int):
    return client.get(
        f"{API}/skills/packages/{package_uuid}/revisions/{n}/download", headers=headers
    )


def _archived_skill_md(content: bytes, name: str) -> str:
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
        source = tar.extractfile(f"skills/{name}/SKILL.md")
        assert source is not None
        return source.read().decode()


def _skill_uuids(client, headers) -> set[str]:
    return {i["uuid"] for i in list_items(client, headers, kind="skill")}


# ── Scenario 1: valid first publish, version write-back ───────────────────


def test_first_upload_publish_writes_version_back_into_the_stored_skill(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. An agent-user (no developer role) uploads ``csv-tool`` (no version)
       → revision 1, version 1.0.0, frontmatter carries it, publisher = caller
    2. The catalog item: kind skill, ``maintain``, ``archive_sha256`` ==
       download ``X-Content-SHA256`` == sha256(bytes); the archived SKILL.md
       says ``version: 1.0.0``, extra files survive; ``/content`` agrees
    3. A republish without a header version → 1.0.1; with ``meta.version``
       4.2.0 → 4.2.0 in the archive
    4. A different skill whose header says 2.3.0 publishes as 2.3.0
    5. Anonymous → 401
    """
    su = superuser_token_headers
    desk = make_person(client, su)

    # ── Phase 1 ───────────────────────────────────────────────────────
    rev = _ok(_publish(
        client, desk.headers,
        _skill_tgz("csv-tool", {"scripts/run.py": "print('hi')\n"}),
        {"release_notes": "First cut"},
    ))
    pkg = rev["package_id"]
    assert rev["revision_number"] == 1
    assert rev["version"] == "1.0.0"
    assert rev["frontmatter"]["version"] == "1.0.0"
    assert rev["frontmatter"]["name"] == "csv-tool"
    assert rev["release_notes"] == "First cut"
    assert rev["published_by_user_id"] == desk.id
    assert rev["required_credentials"] == []
    assert rev["size_bytes"] > 0 and rev["content_hash"]

    # ── Phase 2 ───────────────────────────────────────────────────────
    item = get_item(client, desk.headers, pkg)
    assert item["kind"] == "skill" and item["name"] == "csv-tool"
    assert item["access"] == "maintain"
    assert item["latest"]["revision_number"] == 1
    download = _download(client, desk.headers, pkg, 1)
    assert download.status_code == 200, download.text
    sha = download.headers["x-content-sha256"]
    assert sha == hashlib.sha256(download.content).hexdigest()
    assert sha == item["skill"]["archive_sha256"]
    skill_md = _archived_skill_md(download.content, "csv-tool")
    assert "version: 1.0.0" in skill_md
    with tarfile.open(fileobj=io.BytesIO(download.content), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert names == ["skills/csv-tool/SKILL.md", "skills/csv-tool/scripts/run.py"]
    content = client.get(
        f"{API}/skills/packages/{pkg}/revisions/1/content", headers=desk.headers
    )
    assert content.status_code == 200, content.text
    assert "version: 1.0.0" in content.text

    # ── Phase 3 ───────────────────────────────────────────────────────
    rev2 = _ok(_publish(
        client, desk.headers, _skill_tgz("csv-tool", body="Two."), {"package_uuid": pkg}
    ))
    assert (rev2["revision_number"], rev2["version"]) == (2, "1.0.1")
    rev3 = _ok(_publish(
        client, desk.headers, _skill_tgz("csv-tool", body="Three."),
        {"package_uuid": pkg, "version": "4.2.0"},
    ))
    assert (rev3["revision_number"], rev3["version"]) == (3, "4.2.0")
    third = _download(client, desk.headers, pkg, 3)
    assert "version: 4.2.0" in _archived_skill_md(third.content, "csv-tool")
    # revision 1's stored bytes never change
    assert _download(client, desk.headers, pkg, 1).headers["x-content-sha256"] == sha

    # ── Phase 4 ───────────────────────────────────────────────────────
    versioned = _ok(_publish(
        client, desk.headers, _skill_tgz("pinned-ver", extra_front="version: 2.3.0")
    ))
    assert versioned["version"] == "2.3.0"
    assert versioned["package_id"] != pkg

    # ── Phase 5 ───────────────────────────────────────────────────────
    anon = client.post(
        PUBLISH, files={"archive": ("s.tgz", _skill_tgz("anon-skill"), "application/gzip")}
    )
    assert anon.status_code == 401


# ── Scenario 2: credential slots ──────────────────────────────────────────


def test_upload_credential_declarations_become_user_provided_specs(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Two declared slots → ``required_credentials`` both ``provided_by=user``,
       on the revision and on the package detail
    2. An invalid ``credentials:`` block → 422 ``skill_invalid``, no package
    """
    desk = make_person(client, superuser_token_headers)
    creds = (
        "credentials:\n"
        "  - slot: crm-api\n    type: api_token\n    description: CRM token\n"
        "  - slot: mail-api\n    type: api_token\n"
    )
    rev = _ok(_publish(client, desk.headers, _skill_tgz("cred-tool", extra_front=creds)))
    required = sorted(rev["required_credentials"], key=lambda c: c["slot"])
    assert [(c["slot"], c["type"], c["provided_by"]) for c in required] == [
        ("crm-api", "api_token", "user"), ("mail-api", "api_token", "user"),
    ]
    assert required[0]["description"] == "CRM token"
    assert all(c["publisher_credential_id"] is None for c in required)

    detail = client.get(f"{API}/skills/packages/{rev['package_id']}", headers=desk.headers)
    assert detail.status_code == 200, detail.text
    assert {
        c["slot"]: c["provided_by"]
        for c in detail.json()["revisions"][0]["required_credentials"]
    } == {"crm-api": "user", "mail-api": "user"}

    before = _skill_uuids(client, desk.headers)
    bad = _publish(
        client, desk.headers,
        _skill_tgz("bad-creds", extra_front="credentials:\n  - type: api_token\n"),
    )
    _refused(bad, 422, "skill_invalid")
    assert _skill_uuids(client, desk.headers) == before


# ── Scenario 3: preview ───────────────────────────────────────────────────


def test_publish_preview_first_republish_and_refusals(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. First preview: not a republish, no uuid, version from the header,
       revision 1, credentials ``user``
    2. After publishing: preview with and without ``package_uuid`` → the
       republish (uuid, revision 2, version 1.0.1); an editor's preview with
       the uuid works too
    3. Refusals: invalid name 422, unknown uuid 404, a stranger 404, ``use``
       grantee 403 ``not_publisher``, another skill's name 409
    """
    su = superuser_token_headers
    desk = make_person(client, su)
    editor = make_person(client, su)
    user = make_person(client, su)
    stranger = make_person(client, su)

    # ── Phase 1 ───────────────────────────────────────────────────────
    first = _preview(client, desk.headers, "prev-tool", {
        "name": "prev-tool", "version": "0.3.0",
        "credentials": [{"slot": "svc-api", "type": "api_token"}],
    })
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["is_republish"] is False and body["package_uuid"] is None
    assert body["version"] == "0.3.0" and body["header_version"] == "0.3.0"
    assert body["next_revision_number"] == 1 and body["package_id"]
    assert [(c["slot"], c["provided_by"]) for c in body["credentials"]] == [
        ("svc-api", "user")
    ]

    # ── Phase 2 ───────────────────────────────────────────────────────
    rev = _ok(_publish(client, desk.headers, _skill_tgz("prev-tool"), {
        "visibility": "users",
        "grants": [user_grant(editor.id, "edit"), user_grant(user.id, "use")],
    }))
    pkg = rev["package_id"]
    for kwargs in ({"package_uuid": pkg}, {}):
        again = _preview(client, desk.headers, "prev-tool", **kwargs)
        assert again.status_code == 200, again.text
        assert again.json()["is_republish"] is True
        assert again.json()["package_uuid"] == pkg
        assert again.json()["next_revision_number"] == 2
        assert again.json()["version"] == "1.0.1"
        assert again.json()["latest_published_version"] == "1.0.0"
        assert again.json()["package_id"] == body["package_id"]
    by_editor = _preview(client, editor.headers, "prev-tool", package_uuid=pkg)
    assert by_editor.status_code == 200, by_editor.text
    assert by_editor.json()["package_uuid"] == pkg

    # ── Phase 3 ───────────────────────────────────────────────────────
    _refused(_preview(client, desk.headers, "Bad_Name"), 422, "skill_invalid")
    _refused(
        _preview(client, desk.headers, "prev-tool", package_uuid="00000000-0000-0000-0000-000000000001"),
        404, "package_not_found",
    )
    _refused(_preview(client, stranger.headers, "prev-tool", package_uuid=pkg), 404, "package_not_found")
    _refused(_preview(client, user.headers, "prev-tool", package_uuid=pkg), 403, "not_publisher")
    _refused(_preview(client, desk.headers, "other-tool", package_uuid=pkg), 409, "skill_name_mismatch")


# ── Scenario 4: republish needs edit ──────────────────────────────────────


def test_republish_needs_edit_and_the_package_uuid(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A publishes ``team-tool`` (users, editor ``edit``, user ``use``)
    2. A again without ``package_uuid`` → 409 ``package_exists`` + uuid
    3. A with the uuid → revision 2; the editor → revision 3, published by them
    4. ``use`` → 403 ``not_publisher``; stranger → 404; unknown uuid → 404;
       another skill's folder into this package → 409 ``skill_name_mismatch``
    5. The editor may not change visibility or grants (403); the same
       visibility they already have is fine
    """
    su = superuser_token_headers
    a = make_person(client, su)
    editor = make_person(client, su)
    user = make_person(client, su)
    stranger = make_person(client, su)
    archive = _skill_tgz("team-tool")

    # ── Phase 1 ───────────────────────────────────────────────────────
    rev1 = _ok(_publish(client, a.headers, archive, {
        "visibility": "users",
        "grants": [user_grant(editor.id, "edit"), user_grant(user.id, "use")],
    }))
    pkg = rev1["package_id"]

    # ── Phase 2 ───────────────────────────────────────────────────────
    exists = _refused(_publish(client, a.headers, archive), 409, "package_exists")
    assert exists["package_uuid"] == pkg

    # ── Phase 3 ───────────────────────────────────────────────────────
    rev2 = _ok(_publish(client, a.headers, archive, {"package_uuid": pkg}))
    assert rev2["revision_number"] == 2 and rev2["package_id"] == pkg
    rev3 = _ok(_publish(
        client, editor.headers, _skill_tgz("team-tool", body="Editor change."),
        {"package_uuid": pkg, "release_notes": "by editor"},
    ))
    assert rev3["revision_number"] == 3
    assert rev3["published_by_user_id"] == editor.id
    assert get_item(client, a.headers, pkg)["latest"]["revision_number"] == 3

    # ── Phase 4 ───────────────────────────────────────────────────────
    meta = {"package_uuid": pkg}
    _refused(_publish(client, user.headers, archive, meta), 403, "not_publisher")
    _refused(_publish(client, stranger.headers, archive, meta), 404, "package_not_found")
    _refused(
        _publish(client, a.headers, archive, {"package_uuid": "00000000-0000-0000-0000-000000000002"}),
        404, "package_not_found",
    )
    _refused(
        _publish(client, a.headers, _skill_tgz("other-tool"), meta), 409, "skill_name_mismatch"
    )

    # ── Phase 5 ───────────────────────────────────────────────────────
    _refused(
        _publish(client, editor.headers, archive, {**meta, "visibility": "public"}),
        403, "not_publisher",
    )
    _refused(
        _publish(client, editor.headers, archive, {
            **meta, "grants": [user_grant(stranger.id, "use")],
        }),
        403, "not_publisher",
    )
    same = _ok(_publish(client, editor.headers, archive, {**meta, "visibility": "users"}))
    assert same["revision_number"] == 4
    assert get_item(client, a.headers, pkg)["latest"]["revision_number"] == 4
    assert client.get(f"{API}/catalog/items/{pkg}", headers=stranger.headers).status_code == 404


# ── Scenario 5: grants, visibility, meta, events ──────────────────────────


def test_publish_grants_are_additive_validated_and_announced(
    client: TestClient, superuser_token_headers: dict[str, str], socket_stub
) -> None:
    """
    1. First publish (users, X ``use``) → X sees it; ``catalog.updated``
       {kind: skill} reaches A and X; ``catalog.granted`` reaches X only
    2. Republish granting Y ``edit`` keeps X's ``use`` (additive);
       ``catalog.granted`` to Y only
    3. ``use`` grants on a private publish → 409
       ``grants_require_users_visibility``; a role ``maintain`` → 422
       ``invalid_grants``; meta not JSON / version with a newline / unknown
       visibility → 422; none of these writes anything
    """
    su = superuser_token_headers
    a = make_person(client, su)
    x = make_person(client, su)
    y = make_person(client, su)

    # ── Phase 1 ───────────────────────────────────────────────────────
    socket_stub.emitted_events.clear()
    rev = _ok(_publish(client, a.headers, _skill_tgz("event-tool"), {
        "visibility": "users", "grants": [user_grant(x.id, "use")],
    }))
    pkg = rev["package_id"]
    assert get_item(client, x.headers, pkg)["access"] == "use"
    package_id = get_item(client, a.headers, pkg)["id"]

    def emits(event_type: str) -> list[dict]:
        return [
            e for e in socket_stub.emitted_events
            if e["event"] == "event" and e["data"]["type"] == event_type
        ]

    updated = emits("catalog.updated")
    assert {e["room"] for e in updated} == {f"user_{a.id}", f"user_{x.id}"}
    assert all(e["data"]["meta"] == {
        "kind": "skill", "uuid": pkg, "id": package_id, "revision_number": 1,
    } for e in updated)
    granted = emits("catalog.granted")
    assert {e["room"] for e in granted} == {f"user_{x.id}"}
    assert granted[0]["data"]["meta"] == {"kind": "skill", "uuid": pkg}

    # ── Phase 2 ───────────────────────────────────────────────────────
    socket_stub.emitted_events.clear()
    _ok(_publish(client, a.headers, _skill_tgz("event-tool"), {
        "package_uuid": pkg, "grants": [user_grant(y.id, "edit")],
    }))
    levels = grant_levels(get_item_grants(client, a.headers, pkg))
    assert levels == {
        f"user:{a.id}": "maintain", f"user:{x.id}": "use", f"user:{y.id}": "edit",
    }
    assert {e["room"] for e in emits("catalog.granted")} == {f"user_{y.id}"}
    assert {e["data"]["meta"]["revision_number"] for e in emits("catalog.updated")} == {2}

    # ── Phase 3 ───────────────────────────────────────────────────────
    before = _skill_uuids(client, a.headers)
    archive = _skill_tgz("grant-tool")
    _refused(
        _publish(client, a.headers, archive, {"grants": [user_grant(x.id, "use")]}),
        409, "grants_require_users_visibility",
    )
    _refused(
        _publish(client, a.headers, archive, {
            "visibility": "public", "grants": [user_grant(x.id, "use")],
        }),
        409, "grants_require_users_visibility",
    )
    bad_role = _publish(client, a.headers, archive, {
        "visibility": "users", "grants": [role_grant("agent-user", "maintain")],
    })
    assert bad_role.status_code == 422, bad_role.text
    for meta in ("{not json", json.dumps({"version": "1.0\n2"}), json.dumps({"grants": "x"})):
        assert _publish(client, a.headers, archive, meta).status_code == 422, meta
    _refused(
        _publish(client, a.headers, archive, {"visibility": "everyone"}), 422, "invalid_visibility"
    )
    assert _skill_uuids(client, a.headers) == before
    assert grant_levels(get_item_grants(client, a.headers, pkg)) == levels


# ── Scenario 6: content refusals ──────────────────────────────────────────


def test_invalid_and_secret_bearing_skills_are_refused_and_write_nothing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. SKILL.md name ≠ folder → 422 ``skill_invalid``
    2. No SKILL.md → 422 ``skill_invalid``
    3. A ``.env`` with a key → 422 ``skill_contains_secrets`` naming the file
    4. Nothing was published
    """
    desk = make_person(client, superuser_token_headers)
    before = _skill_uuids(client, desk.headers)

    mismatched = _skill_tgz("mismatch", {"SKILL.md": _skill_md("something-else")})
    _refused(_publish(client, desk.headers, mismatched), 422, "skill_invalid")

    def no_skill_md(tar: tarfile.TarFile) -> None:
        _member(tar, "empty-skill", type=tarfile.DIRTYPE, mode=0o755)
        _member(tar, "empty-skill/notes.md", b"# notes\n")

    _refused(_publish(client, desk.headers, _tgz(no_skill_md)), 422, "skill_invalid")

    leaky = _skill_tgz("leaky", {".env": "OPENAI_API_KEY=sk-verysecret\n"})
    detail = _refused(_publish(client, desk.headers, leaky), 422, "skill_contains_secrets")
    assert any(p.endswith(".env") for p in detail["paths"]), detail

    assert _skill_uuids(client, desk.headers) == before


# ── Scenario 7: oversize ──────────────────────────────────────────────────


def test_oversize_archives_are_refused(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. More than 16 MiB of compressed bytes → 413 ``skill_too_large``
    2. A small gzip that unpacks past 16 MiB → 400 ``skill_archive_invalid``
    """
    desk = make_person(client, superuser_token_headers)
    before = _skill_uuids(client, desk.headers)

    compressed = bytes(16 * MiB + 1)
    _refused(_publish(client, desk.headers, compressed), 413, "skill_too_large")

    bomb = _skill_tgz("big-skill", {"blob.bin": bytes(16 * MiB + 1)})
    assert len(bomb) < MiB
    detail = _refused(_publish(client, desk.headers, bomb), 400, "skill_archive_invalid")
    assert "MB" in detail["message"]

    assert _skill_uuids(client, desk.headers) == before


# ── Scenario 8: unsafe archives ───────────────────────────────────────────


def _with_skill(extra):
    """A valid ``safe-skill`` folder plus whatever ``extra(tar)`` adds."""
    def build(tar: tarfile.TarFile) -> None:
        _member(tar, "safe-skill", type=tarfile.DIRTYPE, mode=0o755)
        _member(tar, "safe-skill/SKILL.md", _skill_md("safe-skill").encode())
        extra(tar)
    return _tgz(build)


def _many_members(tar: tarfile.TarFile) -> None:
    for i in range(2000):
        _member(tar, f"safe-skill/f{i}.txt", b"x")


UNSAFE = {
    "absolute path": lambda t: _member(t, "/safe-skill/abs.txt", b"x"),
    "dot-dot": lambda t: _member(t, "safe-skill/../escape.txt", b"x"),
    "symlink": lambda t: _member(
        t, "safe-skill/link", type=tarfile.SYMTYPE, linkname="../../etc/passwd"
    ),
    "hardlink": lambda t: _member(
        t, "safe-skill/hard", type=tarfile.LNKTYPE, linkname="safe-skill/SKILL.md"
    ),
    "fifo": lambda t: _member(t, "safe-skill/pipe", type=tarfile.FIFOTYPE),
    "two top-level folders": lambda t: _member(t, "other-skill/SKILL.md", b"x"),
    "loose top-level file": lambda t: _member(t, "README.md", b"x"),
    "more than 2000 members": _many_members,
}


def test_unsafe_archives_are_refused_before_anything_is_written(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Each unsafe shape → 400 ``skill_archive_invalid``: absolute path, ``..``,
    symlink, hardlink, fifo, two top-level folders, a loose top-level file,
    > 2000 members, bytes that are not a gzip, an empty archive. The same
    folder without the extra member publishes fine afterwards.
    """
    desk = make_person(client, superuser_token_headers)
    before = _skill_uuids(client, desk.headers)

    for label, extra in UNSAFE.items():
        response = _publish(client, desk.headers, _with_skill(extra))
        assert response.status_code == 400, f"{label}: {response.text}"
        assert error_code(response.json()) == "skill_archive_invalid", label

    _refused(_publish(client, desk.headers, b"not an archive"), 400, "skill_archive_invalid")
    _refused(_publish(client, desk.headers, _tgz(lambda t: None)), 400, "skill_archive_invalid")
    assert _skill_uuids(client, desk.headers) == before

    rev = _ok(_publish(client, desk.headers, _with_skill(lambda t: None)))
    assert rev["revision_number"] == 1
