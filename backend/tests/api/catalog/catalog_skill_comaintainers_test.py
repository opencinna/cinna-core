"""Skill co-maintainers (core C1): stale-base refusal, editable-package hint,
version lineage in the record.

A person who may ``edit`` someone else's skill shares their changes as the next
version of that package. Core refuses a share whose copy is not the latest
revision (S5), refuses a first share that would duplicate a package the caller
may edit (S6), and records who shared each version and what it was based on
(S7). Archives are built in memory with ``tarfile``, as in
``catalog_skill_upload_publish_test.py``; none carries a ``.py`` file.

Scenarios:
  1. An editor shares from the latest base → revision 2, base recorded,
     ``published_by`` the editor; the author's share from base 1 → 409
     ``stale_base`` (latest number, version, who) and nothing written; no
     base → accepted (back-compat); the preview answers ``stale`` both ways;
     the package detail carries ``published_by`` + ``base_revision_number``.
  2. Two concurrent shares from the same base: exactly one wins, the other
     is ``stale_base`` (the per-package publish lock).
  3. A first share while a same-name package the caller may ``edit`` exists
     → 409 ``editable_package_exists`` with that one candidate (an invisible
     same-name package never appears); ``use`` only → own package as today;
     ``new_package: true`` → own package; the preview lists the candidates.
  4. The cloud agent path records the latest it published over as the base.
  5. Listing ``latest.content_hash`` (skills only; ``None`` for an agent) and
     the O1/O2 pin: ``content_hash`` == the tree hash of the unpacked
     archive (paths ``skills/<name>/…`` + bytes, as the desktop computes it).
"""
import asyncio
import contextlib
import hashlib
import io
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import (
    publish_agent_bundle,
    publish_skill_package,
)
from tests.utils.catalog import get_item, list_items, make_person, user_grant
from tests.utils.skill_catalog import (
    error_code,
    get_skill_package,
    make_developer,
    publish_skill,
    write_skill,
)

API = settings.API_V1_STR
PUBLISH = f"{API}/skills/packages/publish"
PREVIEW = f"{API}/skills/packages/publish-preview"


# ── Archives and calls ─────────────────────────────────────────────────────


def _skill_tgz(name: str, body: str = "Step one.", files: dict[str, bytes] | None = None) -> bytes:
    skill_md = (
        f"---\nname: {name}\ndescription: Does {name} things.\n---\n\n# {name}\n\n{body}\n"
    )
    content = {"SKILL.md": skill_md.encode(), **(files or {})}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        root = tarfile.TarInfo(name)
        root.type, root.mode = tarfile.DIRTYPE, 0o755
        tar.addfile(root)
        for rel, data in content.items():
            info = tarfile.TarInfo(f"{name}/{rel}")
            info.size = len(data)
            info.mode = 0o755 if rel.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _publish(client: TestClient, headers: dict, archive: bytes, meta: dict | None = None):
    return client.post(
        PUBLISH,
        headers=headers,
        files={"archive": ("skill.tgz", archive, "application/gzip")},
        data={"meta": json.dumps(meta or {})},
    )


def _preview(client: TestClient, headers: dict, name: str, **fields):
    return client.post(PREVIEW, headers=headers, json={"name": name, **fields})


def _ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def _refused(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    assert error_code(response.json()) == code, response.text
    return response.json()["detail"]


def _tree_hash_of_archive(archive: bytes) -> str:
    """The desktop's tree hash of an unpacked archive: sorted POSIX paths
    (``skills/<name>/…``), each ``path \\0 bytes \\0``. Recomputed here rather
    than imported, so the pin is on the wire contract."""
    files: list[tuple[str, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                source = tar.extractfile(member)
                assert source is not None
                files.append((member.name, source.read()))
    h = hashlib.sha256()
    for path, data in sorted(files):
        h.update(path.encode("utf-8") + b"\0" + data + b"\0")
    return h.hexdigest()


def _shared_package(client: TestClient, su: dict, name: str):
    """Author A publishes ``name`` (users) with B at ``edit``; returns
    ``(a, b, package_uuid)``."""
    a = make_person(client, su)
    b = make_person(client, su)
    rev = _ok(_publish(client, a.headers, _skill_tgz(name), {
        "visibility": "users", "grants": [user_grant(b.id, "edit")],
    }))
    return a, b, rev["package_id"]


# ── Scenario 1: stale base, lineage, preview, detail ───────────────────────


def test_editor_shares_from_the_latest_base_and_a_stale_share_is_refused(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A publishes ``tips`` (B ``edit``); B previews with base 1 → not stale;
       B shares from base 1 → revision 2, base 1, published by B
    2. A shares from base 1 → 409 ``stale_base`` naming revision 2, its
       version and B; nothing written (latest still 2); A's preview with
       base 1 → stale, latest 2 by B
    3. A shares with base 2 → revision 3; with no base → revision 4 (not
       checked), base null
    4. The package detail: every revision has ``published_by`` with a display
       name and its ``base_revision_number``
    """
    su = superuser_token_headers
    a, b, pkg = _shared_package(client, su, "tips")

    # ── Phase 1 ───────────────────────────────────────────────────────
    fresh = _ok(_preview(client, b.headers, "tips", package_uuid=pkg, base_revision_number=1))
    assert fresh["stale"] is False
    assert fresh["latest_revision_number"] == 1
    assert fresh["editable_packages"] == []
    rev2 = _ok(_publish(
        client, b.headers, _skill_tgz("tips", body="B's change."),
        {"package_uuid": pkg, "base_revision_number": 1, "release_notes": "by B"},
    ))
    assert rev2["revision_number"] == 2
    assert rev2["base_revision_number"] == 1
    assert rev2["published_by_user_id"] == b.id
    assert rev2["release_notes"] == "by B"

    # ── Phase 2 ───────────────────────────────────────────────────────
    stale = _refused(_publish(
        client, a.headers, _skill_tgz("tips", body="A's old copy."),
        {"package_uuid": pkg, "base_revision_number": 1},
    ), 409, "stale_base")
    assert stale["latest_revision_number"] == 2
    assert stale["latest_version"] == rev2["version"] == "1.0.1"
    assert stale["published_by"]["user_id"] == b.id
    assert stale["published_by"]["display_name"]
    assert "Version 2" in stale["message"]
    assert get_item(client, a.headers, pkg)["latest"]["revision_number"] == 2
    assert [r["revision_number"] for r in get_skill_package(client, a.headers, pkg)["revisions"]] == [2, 1]
    seen = _ok(_preview(client, a.headers, "tips", package_uuid=pkg, base_revision_number=1))
    assert seen["stale"] is True
    assert seen["latest_revision_number"] == 2
    assert seen["latest_version"] == "1.0.1"
    assert seen["latest_published_by"]["user_id"] == b.id
    # No base on the preview: nothing asked, nothing answered.
    assert _ok(_preview(client, a.headers, "tips", package_uuid=pkg))["stale"] is None

    # ── Phase 3 ───────────────────────────────────────────────────────
    rev3 = _ok(_publish(
        client, a.headers, _skill_tgz("tips", body="A, after getting 2."),
        {"package_uuid": pkg, "base_revision_number": 2},
    ))
    assert rev3["revision_number"] == 3 and rev3["base_revision_number"] == 2
    rev4 = _ok(_publish(
        client, a.headers, _skill_tgz("tips", body="An older client."), {"package_uuid": pkg},
    ))
    assert rev4["revision_number"] == 4 and rev4["base_revision_number"] is None

    # ── Phase 4 ───────────────────────────────────────────────────────
    detail = get_skill_package(client, b.headers, pkg)
    rows = {r["revision_number"]: r for r in detail["revisions"]}
    assert {n: r["base_revision_number"] for n, r in rows.items()} == {
        4: None, 3: 2, 2: 1, 1: None,
    }
    assert {n: r["published_by"]["user_id"] for n, r in rows.items()} == {
        4: a.id, 3: a.id, 2: b.id, 1: a.id,
    }
    assert all(r["published_by"]["display_name"] for r in rows.values())


# ── Scenario 2: concurrent shares from the same base ──────────────────────


def test_two_concurrent_shares_from_the_same_base_one_wins(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A publishes ``race`` (B ``edit``)
    2. A and B upload from base 1 at once; each is held at the publish
       lock's door until the other arrives, so both pass every pre-lock
       step before either writes
    3. Exactly one is revision 2; the other is 409 ``stale_base`` naming the
       winner; the package has two revisions
    """
    su = superuser_token_headers
    a, b, pkg = _shared_package(client, su, "race")

    # ── Phase 2 ───────────────────────────────────────────────────────
    # Held at the lock's door: each request has run every step in front of
    # the lock before either goes in, so a check placed outside the lock
    # would pass for both.
    patcher = patch("app.services.skills.skill_upload._publish_lock_for")
    gate = patcher.start()
    original = patcher.temp_original
    arrived: list[str] = []

    @contextlib.asynccontextmanager
    async def held(key: str):
        arrived.append(key)
        for _ in range(1000):
            if len(arrived) >= 2:
                break
            await asyncio.sleep(0.01)
        async with original(key):
            yield

    gate.side_effect = held
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    _publish, client, person.headers,
                    _skill_tgz("race", body=f"{label}'s change."),
                    {"package_uuid": pkg, "base_revision_number": 1},
                )
                for person, label in ((a, "A"), (b, "B"))
            ]
            responses = [f.result(timeout=60) for f in futures]
    finally:
        patcher.stop()
    assert arrived == [f"package:{pkg}"] * 2

    # ── Phase 3 ───────────────────────────────────────────────────────
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], [r.text for r in responses]
    won = next(r for r in responses if r.status_code == 200).json()
    lost = _refused(next(r for r in responses if r.status_code == 409), 409, "stale_base")
    assert won["revision_number"] == 2 and won["base_revision_number"] == 1
    assert lost["latest_revision_number"] == 2
    assert lost["published_by"]["user_id"] == won["published_by_user_id"]
    revisions = get_skill_package(client, a.headers, pkg)["revisions"]
    assert [r["revision_number"] for r in revisions] == [2, 1]


# ── Scenario 3: a first share never duplicates an editable package ────────


def test_first_share_while_an_editable_same_name_package_exists(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A publishes ``notes`` (users; B ``edit``, C ``use``); D publishes a
       private ``notes`` nobody else sees
    2. B's first share of ``notes`` → 409 ``editable_package_exists`` with
       exactly A's package (publisher, ``edit``, latest 1); nothing created.
       B's preview lists the same candidate
    3. C (``use`` only) → no candidate, own package created as today
    4. B with ``new_package: true`` → B's own package; afterwards a share
       without ``package_uuid`` is the usual ``package_exists`` for it
    5. A base on a first publish is ignored
    """
    su = superuser_token_headers
    a = make_person(client, su)
    b = make_person(client, su)
    c = make_person(client, su)
    d = make_person(client, su)

    # ── Phase 1 ───────────────────────────────────────────────────────
    a_pkg = _ok(_publish(client, a.headers, _skill_tgz("notes"), {
        "visibility": "users",
        "grants": [user_grant(b.id, "edit"), user_grant(c.id, "use")],
    }))["package_id"]
    d_pkg = _ok(_publish(client, d.headers, _skill_tgz("notes")))["package_id"]
    b_skills_before = {i["uuid"] for i in list_items(client, b.headers, kind="skill")}
    assert d_pkg not in b_skills_before

    # ── Phase 2 ───────────────────────────────────────────────────────
    exists = _refused(
        _publish(client, b.headers, _skill_tgz("notes", body="B's fork.")),
        409, "editable_package_exists",
    )
    assert [cand["package_uuid"] for cand in exists["candidates"]] == [a_pkg]
    candidate = exists["candidates"][0]
    assert candidate["publisher"]["user_id"] == a.id
    assert candidate["publisher"]["display_name"]
    assert candidate["access"] == "edit"
    assert candidate["latest_revision_number"] == 1
    assert candidate["latest_version"] == "1.0.0"
    assert candidate["package_id"]
    assert {i["uuid"] for i in list_items(client, b.headers, kind="skill")} == b_skills_before
    preview = _ok(_preview(client, b.headers, "notes"))
    assert preview["is_republish"] is False
    assert [cand["package_uuid"] for cand in preview["editable_packages"]] == [a_pkg]

    # ── Phase 3 ───────────────────────────────────────────────────────
    assert _ok(_preview(client, c.headers, "notes"))["editable_packages"] == []
    c_rev = _ok(_publish(client, c.headers, _skill_tgz("notes", body="C's own.")))
    assert c_rev["package_id"] not in {a_pkg, d_pkg}
    assert c_rev["revision_number"] == 1

    # ── Phase 4 ───────────────────────────────────────────────────────
    b_rev = _ok(_publish(
        client, b.headers, _skill_tgz("notes", body="B's own."), {"new_package": True},
    ))
    b_pkg = b_rev["package_id"]
    assert b_pkg not in {a_pkg, c_rev["package_id"], d_pkg}
    assert get_item(client, b.headers, b_pkg)["publisher"]["user_id"] == b.id
    assert get_item(client, b.headers, a_pkg)["latest"]["revision_number"] == 1
    again = _refused(_publish(client, b.headers, _skill_tgz("notes")), 409, "package_exists")
    assert again["package_uuid"] == b_pkg

    # ── Phase 5 ───────────────────────────────────────────────────────
    e = make_person(client, su)
    first = _ok(_publish(client, e.headers, _skill_tgz("solo"), {"base_revision_number": 7}))
    assert first["revision_number"] == 1 and first["base_revision_number"] is None


# ── Scenario 4: the cloud agent path records its base ─────────────────────


def test_agent_path_records_the_latest_it_published_over(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A developer publishes ``agent-tips`` from an agent → revision 1, no base
    2. Publishes it again → revision 2 with base 1
    """
    _, headers = make_developer(client, superuser_token_headers)

    # ── Phase 1 ───────────────────────────────────────────────────────
    published = publish_skill_package(client, headers, "agent-tips")
    pkg = published["package_uuid"]
    first = get_skill_package(client, headers, pkg)["revisions"]
    assert [(r["revision_number"], r["base_revision_number"]) for r in first] == [(1, None)]

    # ── Phase 2 ───────────────────────────────────────────────────────
    write_skill(published["env_id"], "agent-tips", body="Second cut.")
    rev2 = publish_skill(client, headers, published["agent_id"], "agent-tips")
    assert rev2["revision_number"] == 2 and rev2["base_revision_number"] == 1


# ── Scenario 5: listing content_hash and the O1/O2 pin ────────────────────


def test_listing_content_hash_equals_the_unpacked_archive_tree_hash(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Publish a skill with nested files, an executable and an empty file
    2. Its catalog item ``latest.content_hash`` == the revision's
       ``content_hash`` == the tree hash of the downloaded archive, unpacked
       (version write-back included)
    3. A second revision moves the listing hash with it
    4. An agent item's ``latest.content_hash`` is ``None``
    """
    su = superuser_token_headers
    a = make_person(client, su)

    # ── Phase 1 ───────────────────────────────────────────────────────
    files = {
        "scripts/run.sh": b"#!/bin/sh\necho hi\n",
        "docs/a-b.md": b"# a-b\n",
        "docs/résumé.md": "accents\n".encode(),
        "empty.txt": b"",
    }
    rev = _ok(_publish(client, a.headers, _skill_tgz("hashy", files=files)))
    pkg = rev["package_id"]

    # ── Phase 2 ───────────────────────────────────────────────────────
    listed = next(i for i in list_items(client, a.headers, kind="skill") if i["uuid"] == pkg)
    assert listed["latest"]["content_hash"] == rev["content_hash"]
    assert get_item(client, a.headers, pkg)["latest"]["content_hash"] == rev["content_hash"]
    download = client.get(
        f"{API}/skills/packages/{pkg}/revisions/1/download", headers=a.headers
    )
    assert download.status_code == 200, download.text
    assert _tree_hash_of_archive(download.content) == rev["content_hash"]

    # ── Phase 3 ───────────────────────────────────────────────────────
    rev2 = _ok(_publish(
        client, a.headers, _skill_tgz("hashy", body="Changed.", files=files),
        {"package_uuid": pkg, "base_revision_number": 1},
    ))
    assert rev2["content_hash"] != rev["content_hash"]
    assert get_item(client, a.headers, pkg)["latest"]["content_hash"] == rev2["content_hash"]
    download2 = client.get(
        f"{API}/skills/packages/{pkg}/revisions/2/download", headers=a.headers
    )
    assert _tree_hash_of_archive(download2.content) == rev2["content_hash"]

    # ── Phase 4 ───────────────────────────────────────────────────────
    _, dev_headers = make_developer(client, su)
    bundle = publish_agent_bundle(client, dev_headers, "hash-agent")
    agent_item = get_item(client, dev_headers, bundle["bundle_uuid"])
    assert agent_item["kind"] == "agent"
    assert agent_item["latest"]["content_hash"] is None
