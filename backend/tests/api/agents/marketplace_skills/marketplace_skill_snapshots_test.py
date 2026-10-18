"""Marketplace skill snapshots — what a skills-marketplace sync writes to disk.

A sync of a ``skills`` marketplace archives each supported skill folder into
``<SKILL_STORAGE_DIR>/marketplace-skills/<marketplace_id>/<plugin_id>/<sha>.tar.gz``
(the root is redirected to a tmp tree by the agents conftest). Sync and
discover are driven through the API; the snapshot folder has no endpoint, so
it is inspected on disk, read-only.

Scenarios:
  1. One archive per supported entry; discover rows carry matching digest/size.
  2. Unchanged resync writes nothing.
  3. Editing one SKILL.md moves only that digest; old file kept.
  4. Oversize → unsupported ``too_large``, fields cleared; shrinking recovers.
  5. Symlinked skill folders / files are never archived.
  6. Archives are byte-identical across two marketplaces.
  7. Entry removed upstream → row, dir and archive route are gone.
  8. Entry turned unsupported clears its fields.
  9. Marketplace delete removes its snapshot dir.
 10. A failing promote leaves the previous state untouched.
"""
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.utils.llm_plugin import (
    PLUGINS_BASE,
    delete_marketplace,
    stubbed_clone,
    sync_marketplace,
)
from tests.utils.marketplace_skills import (
    archive_file,
    entry_dir,
    files_under,
    get_archive,
    new_skills_marketplace,
    rows_of,
    sha_of,
    snapshots_root,
    skills_repo,
    tar_members,
    write_skill,
)


def _resync(client, headers, mid, populate, commit):
    return sync_marketplace(client, headers, mid, populate=populate, commit_hash=commit)


def test_sync_writes_one_archive_per_supported_skill(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. 3 valid skills + 1 folder without SKILL.md are synced.
    2. Exactly 3 archive files exist, at the content-addressed paths.
    3. Each supported row carries the file's sha256 and byte size; the broken
       row has nulls and no directory.
    """
    m = new_skills_marketplace(client, superuser_token_headers, skills_repo())
    rows = rows_of(client, superuser_token_headers, m["id"])
    assert set(rows) == {"alpha", "beta", "gamma", "half-done"}

    files = files_under(m["id"])
    assert len(files) == 3
    for name in ("alpha", "beta", "gamma"):
        row = rows[name]
        assert row["supported"] is True
        path = archive_file(m["id"], row["id"], row["skill_archive_sha256"])
        assert path.is_file()
        assert sha_of(path.read_bytes()) == row["skill_archive_sha256"]
        assert path.stat().st_size == row["skill_archive_size"]
    broken = rows["half-done"]
    assert broken["supported"] is False
    assert broken["skill_archive_sha256"] is None
    assert broken["skill_archive_size"] is None
    assert not entry_dir(m["id"], broken["id"]).exists()


def test_unchanged_resync_writes_nothing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Sync, record digests, file list, inodes and mtimes.
    2. Resync the same content under a new commit hash.
    3. Digests, files, inodes and mtimes are identical; `.staging/` is empty.
    """
    populate = skills_repo()
    m = new_skills_marketplace(client, superuser_token_headers, populate)
    before_rows = rows_of(client, superuser_token_headers, m["id"])
    before = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in files_under(m["id"])}

    _resync(client, superuser_token_headers, m["id"], populate, "c0ffee000002")

    after_rows = rows_of(client, superuser_token_headers, m["id"])
    assert {n: r["skill_archive_sha256"] for n, r in after_rows.items()} == {
        n: r["skill_archive_sha256"] for n, r in before_rows.items()
    }
    after = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in files_under(m["id"])}
    assert after == before
    assert not list(snapshots_root().rglob(".staging"))
    assert all(p.is_dir() for p in (snapshots_root() / m["id"]).iterdir())


def test_editing_one_skill_moves_only_its_digest(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Sync; edit alpha's SKILL.md; resync.
    2. Only alpha's digest changes; beta and gamma keep theirs.
    3. Alpha's directory holds both the old and the new file.
    """
    m = new_skills_marketplace(client, superuser_token_headers, skills_repo())
    before = rows_of(client, superuser_token_headers, m["id"])

    _resync(
        client, superuser_token_headers, m["id"],
        skills_repo(bodies={"alpha": "A different body."}), "c0ffee000002",
    )
    after = rows_of(client, superuser_token_headers, m["id"])

    assert after["alpha"]["id"] == before["alpha"]["id"]
    assert after["alpha"]["skill_archive_sha256"] != before["alpha"]["skill_archive_sha256"]
    for name in ("beta", "gamma"):
        assert after[name]["skill_archive_sha256"] == before[name]["skill_archive_sha256"]
    kept = {p.name for p in entry_dir(m["id"], after["alpha"]["id"]).iterdir()}
    assert kept == {
        f"{before['alpha']['skill_archive_sha256']}.tar.gz",
        f"{after['alpha']['skill_archive_sha256']}.tar.gz",
    }


def test_oversize_skill_is_unsupported_and_recovers_when_shrunk(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A 17 MB skill: supported=false, unsupported_reason=too_large, nulls, no file.
    2. Shrunk and resynced: supported again with a digest and a file.
    """
    def big(root: Path) -> None:
        skills_repo(("small",), broken=())(root)
        write_skill(root / "skills" / "huge", "huge")
        (root / "skills" / "huge" / "blob.bin").write_bytes(b"\0" * (17 * 1024 * 1024))

    m = new_skills_marketplace(client, superuser_token_headers, big)
    rows = rows_of(client, superuser_token_headers, m["id"])
    huge = rows["huge"]
    assert huge["supported"] is False
    assert huge["unsupported_reason"] == "too_large"
    assert huge["skill_archive_sha256"] is None
    assert huge["skill_archive_size"] is None
    assert not entry_dir(m["id"], huge["id"]).exists()
    assert rows["small"]["supported"] is True

    _resync(
        client, superuser_token_headers, m["id"],
        skills_repo(("small", "huge"), broken=()), "c0ffee000002",
    )
    huge = rows_of(client, superuser_token_headers, m["id"])["huge"]
    assert huge["supported"] is True
    assert huge["unsupported_reason"] is None
    assert archive_file(m["id"], huge["id"], huge["skill_archive_sha256"]).is_file()


def test_symlinks_are_never_archived(
    client: TestClient, superuser_token_headers: dict[str, str], tmp_path: Path
) -> None:
    """
    1. skills/evil -> an outside dir gets no row and no archive.
    2. Inside a real skill, a symlinked file and a symlinked dir are absent
       from the tar's member list; real files are present.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    write_skill(outside, "evil")

    def populate(root: Path) -> None:
        skills_repo(("good",), broken=())(root)
        good = root / "skills" / "good"
        os.symlink(outside / "secret.txt", good / "linked.txt")
        os.symlink(outside, good / "linked_dir")
        os.symlink(outside, root / "skills" / "evil")

    m = new_skills_marketplace(client, superuser_token_headers, populate)
    rows = rows_of(client, superuser_token_headers, m["id"])
    assert "evil" not in rows
    good = rows["good"]
    data = archive_file(m["id"], good["id"], good["skill_archive_sha256"]).read_bytes()
    names = [t.name for t in tar_members(data)]
    assert "skills/good/SKILL.md" in names
    assert not any("linked" in n or "secret" in n for n in names)
    assert all(t.isfile() or t.isdir() for t in tar_members(data))
    assert len(files_under(m["id"])) == 1


def test_archive_bytes_are_identical_across_two_marketplaces(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The same fixture synced into two marketplaces gives byte-equal archives."""
    first = new_skills_marketplace(client, superuser_token_headers, skills_repo())
    second = new_skills_marketplace(client, superuser_token_headers, skills_repo())
    a = rows_of(client, superuser_token_headers, first["id"])
    b = rows_of(client, superuser_token_headers, second["id"])
    for name in ("alpha", "beta", "gamma"):
        assert a[name]["id"] != b[name]["id"]
        assert a[name]["skill_archive_sha256"] == b[name]["skill_archive_sha256"]
        assert (
            archive_file(first["id"], a[name]["id"], a[name]["skill_archive_sha256"]).read_bytes()
            == archive_file(second["id"], b[name]["id"], b[name]["skill_archive_sha256"]).read_bytes()
        )


def test_entry_removed_upstream_loses_row_dir_and_archive_route(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Sync alpha+beta; remove beta upstream and resync.
    2. beta's row and snapshot dir are gone; alpha's remain.
    3. The archive route for beta's old id answers 404 not_found.
    """
    m = new_skills_marketplace(client, superuser_token_headers, skills_repo(("alpha", "beta"), broken=()))
    before = rows_of(client, superuser_token_headers, m["id"])
    beta = before["beta"]

    _resync(client, superuser_token_headers, m["id"], skills_repo(("alpha",), broken=()), "c0ffee000002")

    after = rows_of(client, superuser_token_headers, m["id"])
    assert set(after) == {"alpha"}
    assert not entry_dir(m["id"], beta["id"]).exists()
    assert entry_dir(m["id"], before["alpha"]["id"]).is_dir()
    r = get_archive(client, superuser_token_headers, beta["id"])
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"


def test_entry_turned_unsupported_clears_its_fields(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A skill that loses its SKILL.md becomes no_skill_md with nulls; route says not_a_skill."""
    m = new_skills_marketplace(client, superuser_token_headers, skills_repo(("alpha", "beta"), broken=()))
    before = rows_of(client, superuser_token_headers, m["id"])

    def break_beta(root: Path) -> None:
        skills_repo(("alpha",), broken=("beta",))(root)

    _resync(client, superuser_token_headers, m["id"], break_beta, "c0ffee000002")
    beta = rows_of(client, superuser_token_headers, m["id"])["beta"]
    assert beta["id"] == before["beta"]["id"]
    assert beta["supported"] is False
    assert beta["unsupported_reason"] == "no_skill_md"
    assert beta["skill_archive_sha256"] is None
    assert beta["skill_archive_size"] is None
    r = get_archive(client, superuser_token_headers, beta["id"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_a_skill"


def test_deleting_the_marketplace_removes_its_snapshots(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Marketplace delete removes `<mid>/` but leaves another marketplace's files."""
    gone = new_skills_marketplace(client, superuser_token_headers, skills_repo())
    kept = new_skills_marketplace(client, superuser_token_headers, skills_repo())
    assert (snapshots_root() / gone["id"]).is_dir()

    delete_marketplace(client, superuser_token_headers, gone["id"])

    assert not (snapshots_root() / gone["id"]).exists()
    assert len(files_under(kept["id"])) == 3


def test_failed_promote_leaves_rows_and_files_unchanged(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Sync a baseline, then resync an edited alpha with promote raising OSError.
    2. The sync fails; alpha's digest and the file set are as before.
    """
    m = new_skills_marketplace(client, superuser_token_headers, skills_repo())
    rows_before = rows_of(client, superuser_token_headers, m["id"])
    files_before = files_under(m["id"])

    with (
        patch(
            "app.services.plugins.marketplace_skill_snapshots."
            "MarketplaceSkillSnapshots.promote",
            side_effect=OSError("disk full"),
        ),
        stubbed_clone(skills_repo(bodies={"alpha": "changed"}), commit_hash="c0ffee000002"),
    ):
        r = client.post(
            f"{PLUGINS_BASE}/marketplaces/{m['id']}/sync", headers=superuser_token_headers
        )
    assert r.status_code >= 500 or r.json().get("status") == "error", r.text

    rows_after = rows_of(client, superuser_token_headers, m["id"])
    assert {n: x["skill_archive_sha256"] for n, x in rows_after.items()} == {
        n: x["skill_archive_sha256"] for n, x in rows_before.items()
    }
    assert files_under(m["id"]) == files_before
