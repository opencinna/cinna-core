"""``GET /llm-plugins/plugins/{id}/skill-archive`` — visibility, digests, errors.

Visibility is exactly the `/discover` rule (own marketplace or
``public_discovery``), with no superuser bypass.
"""
import pytest
from fastapi.testclient import TestClient

from tests.utils.llm_plugin import PLUGINS_BASE, create_marketplace, sync_marketplace, update_marketplace
from tests.utils.marketplace_skills import (
    create_second_superuser,
    get_archive,
    new_skills_marketplace,
    rows_of,
    sha_of,
    skills_repo,
    tar_members,
    write_skill,
)
from tests.utils.user import create_random_user_with_headers


def _code(r) -> str:
    return r.json()["detail"]["code"]


def test_owner_downloads_the_current_snapshot(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """200, gzip, body hashes to the row digest and the header, members under skills/<name>/."""
    m = new_skills_marketplace(client, superuser_token_headers, skills_repo(), public_discovery=False)
    row = rows_of(client, superuser_token_headers, m["id"])["alpha"]

    r = get_archive(client, superuser_token_headers, row["id"])

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/gzip")
    assert r.headers["x-archive-sha256"] == row["skill_archive_sha256"]
    assert sha_of(r.content) == row["skill_archive_sha256"]
    assert len(r.content) == row["skill_archive_size"]
    names = [t.name for t in tar_members(r.content) if t.isfile()]
    assert "skills/alpha/SKILL.md" in names
    assert all(n.startswith("skills/alpha/") for n in names)

    pinned = get_archive(client, superuser_token_headers, row["id"], sha256=row["skill_archive_sha256"])
    assert pinned.status_code == 200 and pinned.content == r.content


def test_private_marketplace_is_invisible_to_everyone_but_its_owner(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Private marketplace: a regular user and a non-owner superuser get 404 not_found.
    2. Flipping public_discovery on makes both succeed.
    """
    m = new_skills_marketplace(client, superuser_token_headers, skills_repo(), public_discovery=False)
    row = rows_of(client, superuser_token_headers, m["id"])["alpha"]
    _, user_headers = create_random_user_with_headers(client)
    _, other_su = create_second_superuser(client, superuser_token_headers)

    for headers in (user_headers, other_su):
        r = get_archive(client, headers, row["id"])
        assert r.status_code == 404, r.text
        assert _code(r) == "not_found"
        r = get_archive(client, headers, row["id"], sha256=row["skill_archive_sha256"])
        assert r.status_code == 404 and _code(r) == "not_found"

    update_marketplace(client, superuser_token_headers, m["id"], {"public_discovery": True})
    for headers in (user_headers, other_su):
        r = get_archive(client, headers, row["id"])
        assert r.status_code == 200
        assert sha_of(r.content) == row["skill_archive_sha256"]


def test_old_digest_still_resolves_and_unknown_digest_is_snapshot_missing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. After an edit-resync the old digest still serves its old bytes;
       no sha256 serves the new one.
    2. A well-formed unknown digest → 404 snapshot_missing.
    3. Malformed / traversal sha256 → 422 before any file access.
    """
    m = new_skills_marketplace(client, superuser_token_headers, skills_repo())
    old = rows_of(client, superuser_token_headers, m["id"])["alpha"]
    old_bytes = get_archive(client, superuser_token_headers, old["id"]).content
    sync_marketplace(
        client, superuser_token_headers, m["id"],
        populate=skills_repo(bodies={"alpha": "edited"}), commit_hash="c0ffee000002",
    )
    new = rows_of(client, superuser_token_headers, m["id"])["alpha"]
    assert new["skill_archive_sha256"] != old["skill_archive_sha256"]

    r_old = get_archive(client, superuser_token_headers, old["id"], sha256=old["skill_archive_sha256"])
    assert r_old.status_code == 200 and r_old.content == old_bytes
    r_cur = get_archive(client, superuser_token_headers, old["id"])
    assert r_cur.headers["x-archive-sha256"] == new["skill_archive_sha256"]

    r = get_archive(client, superuser_token_headers, old["id"], sha256="0" * 64)
    assert r.status_code == 404 and _code(r) == "snapshot_missing"

    for bad in ("../../etc/passwd", "ABCDEF" + "0" * 58, "0" * 63, "0" * 65, "zz" * 32, "%2e%2e%2f"):
        r = get_archive(client, superuser_token_headers, old["id"], sha256=bad)
        assert r.status_code == 422, (bad, r.text)


def test_non_skill_and_unsupported_entries_are_not_a_skill(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A too_large skill and a claude-format plugin → 409 not_a_skill."""
    def populate(root) -> None:
        skills_repo(("small",), broken=())(root)
        write_skill(root / "skills" / "huge", "huge")
        (root / "skills" / "huge" / "blob.bin").write_bytes(b"\0" * (17 * 1024 * 1024))

    m = new_skills_marketplace(client, superuser_token_headers, populate)
    huge = rows_of(client, superuser_token_headers, m["id"])["huge"]
    r = get_archive(client, superuser_token_headers, huge["id"])
    assert r.status_code == 409 and _code(r) == "not_a_skill"

    from tests.utils.llm_plugin import seed_marketplace_plugin

    claude = create_marketplace(client, superuser_token_headers, url="https://example.com/claude-x.git")
    plugin = seed_marketplace_plugin(client, superuser_token_headers, claude["id"], name="claude-thing")
    r = get_archive(client, superuser_token_headers, plugin["id"])
    assert r.status_code == 409 and _code(r) == "not_a_skill"


def test_unknown_plugin_id_is_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    r = get_archive(client, superuser_token_headers, "00000000-0000-4000-8000-000000000000")
    assert r.status_code == 404 and _code(r) == "not_found"
