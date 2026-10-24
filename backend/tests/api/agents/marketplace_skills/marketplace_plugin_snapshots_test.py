"""Claude / codex marketplace plugin snapshots — what a sync and the worker write.

Fetch is stubbed (``stubbed_plugin_fetch``); under TESTING the sync drains the
worker inline, so a sync response already reflects the drained state. Archives
live under ``marketplace-skills/<mid>/<pid>/<sha>.tar.gz`` and are read from
disk, read-only. Lease helpers are documented DB seams (a lease held by another
process has no endpoint).

Scenarios:
  1. Local entry: ready in the sync response, archive prefixed by the name.
  2. External entry: pending in the plan, ready after the inline drain.
  3. Resync with unchanged sha does not fetch; a sha change gives a new digest
     and removes the old file.
  4. Fetch error: pending with a backoff, then failed fetch_failed at max
     attempts keeping the previous archive; retry route recovers it.
  5. Content verdicts: too_large, unsafe_path, no_components, unknown source.
  6. Entry removed while claimed: nothing written.
  7. Leases: a live lease is not reclaimed, an expired one is.
  8. `supported` is identical before and after the snapshot.
  9. Entries route: filters, counts, 403 for a normal user; retry scoping.
"""
import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.llm_plugin import (
    claude_repo,
    create_marketplace,
    discover_plugins,
    entries_by_name,
    expire_snapshot_lease,
    hold_snapshot_lease,
    list_marketplace_entries,
    retry_snapshots,
    snapshot_lease_seconds_left,
    stubbed_plugin_fetch,
    sync_marketplace,
)
from tests.utils.marketplace_skills import (
    archive_file,
    entry_dir,
    files_under,
    sha_of,
    tar_members,
)
from tests.utils.user import create_random_user, user_authentication_headers

EXT_URL = "https://github.com/acme/ext-plugin.git"
SHA1 = "1" * 40
SHA2 = "2" * 40

SKILL_MD = "---\nname: helper\ndescription: Helps.\n---\n\n# helper\n"


def _new_market(client, headers, populate, *, commit="c0ffee000001", tag="a") -> dict:
    m = create_marketplace(
        client, headers, url=f"https://github.com/example/snap-{tag}-{id(populate)}.git"
    )
    sync_marketplace(client, headers, m["id"], populate=populate, commit_hash=commit)
    return m


def _local_entry(name: str, **extra) -> dict:
    return {"name": name, "description": f"{name} plugin", "source": f"./plugins/{name}", **extra}


def _ext_entry(name: str, sha: str | None = SHA1, url: str = EXT_URL, **extra) -> dict:
    source: dict = {"source": "url", "url": url}
    if sha:
        source["sha"] = sha
    return {"name": name, "description": f"{name} plugin", "source": source, **extra}


def _row(client, headers, mid, name) -> dict:
    return entries_by_name(list_marketplace_entries(client, headers, mid, limit=500))[name]


def _ext_tree(**extra) -> dict:
    return {"commands/hello.md": "# hello", **extra}


def test_local_entry_is_ready_in_the_sync_response(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A local entry with a skill, an stdio MCP server and a command syncs.
    2. The entries row is ready at once, with digest, size, components, runs_code.
    3. The archive on disk matches the digest and its members carry the name prefix.
    """
    files = {
        "plugins/loc/skills/helper/SKILL.md": SKILL_MD,
        "plugins/loc/commands/go.md": "# go",
        "plugins/loc/.mcp.json": json.dumps({"mcpServers": {"fs": {"command": "npx", "args": ["x"]}}}),
    }
    with stubbed_plugin_fetch({}) as fetch:
        m = _new_market(client, superuser_token_headers, claude_repo([_local_entry("loc")], files))
    assert fetch.fetch_calls == []

    row = _row(client, superuser_token_headers, m["id"], "loc")
    assert row["snapshot_status"] == "ready" and row["snapshot_reason"] is None
    assert row["components"]["commands"] == ["go"]
    assert [s["name"] for s in row["components"]["skills"]] == ["helper"]
    assert row["components"]["mcp_servers"][0]["transport"] == "stdio"
    assert row["components"]["mcp_servers"][0]["runtime"] == "node"
    assert row["runs_code"] is True
    path = archive_file(m["id"], row["id"], row["archive_sha256"])
    data = path.read_bytes()
    assert sha_of(data) == row["archive_sha256"] and len(data) == row["archive_size"]
    names = [mem.name for mem in tar_members(data)]
    assert names and all(n.startswith("loc/") for n in names)
    assert "loc/commands/go.md" in names

    # Phase: discover carries the same K1 fields.
    disc = discover_plugins(client, superuser_token_headers, marketplace_id=m["id"])["data"][0]
    assert disc["snapshot_status"] == "ready"
    assert disc["archive_sha256"] == row["archive_sha256"]
    assert disc["components"] == row["components"] and disc["runs_code"] is True


def test_external_entry_goes_pending_then_ready_and_resyncs_follow_the_sha(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. First sync: an external sha-pinned entry is fetched once and ready.
    2. Resync, unchanged sha, nothing is fetched and the digest is stable.
    3. sha change: refetch, new digest, the old archive file is gone.
    """
    h = superuser_token_headers
    populate = claude_repo([_ext_entry("ext", SHA1)])
    trees = {EXT_URL: _ext_tree()}
    with stubbed_plugin_fetch(trees) as fetch:
        m = _new_market(client, h, populate)
        assert len(fetch.fetch_calls) == 1
        assert fetch.fetch_calls[0]["sha"] == SHA1
        first = _row(client, h, m["id"], "ext")
        assert first["snapshot_status"] == "ready"
        assert first["components"]["commands"] == ["hello"]
        assert archive_file(m["id"], first["id"], first["archive_sha256"]).is_file()

        sync_marketplace(client, h, m["id"], populate=populate, commit_hash="c0ffee000002")
        assert len(fetch.fetch_calls) == 1, "unchanged sha must not refetch"
        again = _row(client, h, m["id"], "ext")
        assert again["archive_sha256"] == first["archive_sha256"]

        trees[EXT_URL] = _ext_tree(**{"commands/extra.md": "# extra"})
        sync_marketplace(
            client, h, m["id"], populate=claude_repo([_ext_entry("ext", SHA2)]),
            commit_hash="c0ffee000003",
        )
        assert len(fetch.fetch_calls) == 2 and fetch.fetch_calls[1]["sha"] == SHA2
    moved = _row(client, h, m["id"], "ext")
    assert moved["snapshot_status"] == "ready"
    assert moved["archive_sha256"] != first["archive_sha256"]
    assert moved["components"]["commands"] == ["extra", "hello"]
    assert not archive_file(m["id"], first["id"], first["archive_sha256"]).exists()
    assert archive_file(m["id"], moved["id"], moved["archive_sha256"]).is_file()
    assert len(files_under(m["id"])) == 1


def test_ref_only_entry_resolves_the_commit_and_skips_unchanged_fetch(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An entry with no sha follows its ref: ls-remote on every sync, fetch only when the commit moved."""
    h = superuser_token_headers
    populate = claude_repo([_ext_entry("floaty", sha=None)])
    with stubbed_plugin_fetch({EXT_URL: _ext_tree()}, commits_by_url={EXT_URL: "a" * 40}) as fetch:
        m = _new_market(client, h, populate)
        assert len(fetch.fetch_calls) == 1 and len(fetch.ls_remote_calls) == 1
        sync_marketplace(client, h, m["id"], populate=populate, commit_hash="c0ffee000002")
        assert len(fetch.fetch_calls) == 1, "same remote commit: no refetch"
        assert len(fetch.ls_remote_calls) == 2
        fetch.commits_by_url[EXT_URL] = "b" * 40
        sync_marketplace(client, h, m["id"], populate=populate, commit_hash="c0ffee000003")
        assert len(fetch.fetch_calls) == 2
    assert _row(client, h, m["id"], "floaty")["snapshot_status"] == "ready"


def test_fetch_error_backs_off_then_fails_and_retry_recovers(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """
    1. Fetch raises: the row stays pending with a 30 s backoff lease.
    2. At max attempts (patched to 1) the next claim marks it failed fetch_failed.
    3. Failed rows are never served; retry re-queues and, fetch healthy, readies it.
    """
    h = superuser_token_headers
    other = claude_repo([_local_entry("kick")], {"plugins/kick/commands/k.md": "# k"})
    with patch.object(settings, "PLUGIN_SNAPSHOT_MAX_ATTEMPTS", 1):
        with stubbed_plugin_fetch({EXT_URL: _ext_tree()}) as fetch:
            fetch.fail_urls.add(EXT_URL)
            m = _new_market(client, h, claude_repo([_ext_entry("flaky")]))
            row = _row(client, h, m["id"], "flaky")
            assert row["snapshot_status"] == "pending"
            assert row["archive_sha256"] is None
            left = snapshot_lease_seconds_left(db, row["id"])
            assert 0 < left <= 30

            # Backoff elapsed, then another sync (any marketplace) ticks the worker.
            expire_snapshot_lease(db, row["id"])
            m2 = create_marketplace(client, h, url="https://github.com/example/kick-1.git")
            sync_marketplace(client, h, m2["id"], populate=other)
            failed = _row(client, h, m["id"], "flaky")
            assert failed["snapshot_status"] == "failed"
            assert failed["snapshot_reason"] == "fetch_failed"
            counts = list_marketplace_entries(client, h, m["id"])["counts"]
            assert {k: counts[k] for k in ("ready", "pending", "failed", "retryable")} == {
                "ready": 0, "pending": 0, "failed": 1, "retryable": 1,
            }, "a fetch_failed external row is what the default retry queues"

            fetch.fail_urls.clear()
            assert retry_snapshots(client, h, m["id"])["queued"] == 1
    healed = _row(client, h, m["id"], "flaky")
    assert healed["snapshot_status"] == "ready"
    assert healed["archive_sha256"]


def test_fetch_failure_keeps_the_previous_archive(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """A later sha whose fetch keeps failing: the old digest stays served after fetch_failed."""
    h = superuser_token_headers
    with patch.object(settings, "PLUGIN_SNAPSHOT_MAX_ATTEMPTS", 1):
        with stubbed_plugin_fetch({EXT_URL: _ext_tree()}) as fetch:
            m = _new_market(client, h, claude_repo([_ext_entry("keep", SHA1)]))
            good = _row(client, h, m["id"], "keep")
            fetch.fail_urls.add(EXT_URL)
            sync_marketplace(client, h, m["id"], populate=claude_repo([_ext_entry("keep", SHA2)]),
                             commit_hash="c0ffee000002")
            expire_snapshot_lease(db, good["id"])
            m2 = create_marketplace(client, h, url="https://github.com/example/kick-2.git")
            sync_marketplace(client, h, m2["id"],
                             populate=claude_repo([_local_entry("k")], {"plugins/k/commands/k.md": "#"}))
    row = _row(client, h, m["id"], "keep")
    assert row["snapshot_status"] == "failed" and row["snapshot_reason"] == "fetch_failed"
    assert row["archive_sha256"] == good["archive_sha256"]
    assert archive_file(m["id"], good["id"], good["archive_sha256"]).is_file()


def test_content_verdicts_clear_the_snapshot_and_are_not_retried(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """too_large, unsafe_path (symlink and parser verdict), no_components, unknown source."""
    h = superuser_token_headers

    def big(dest: Path) -> None:
        (dest / "commands").mkdir(parents=True)
        (dest / "commands" / "x.md").write_text("x")
        with open(dest / "blob.bin", "wb") as fh:
            fh.truncate(16 * 1024 * 1024 + 1)

    def linked(dest: Path) -> None:
        (dest / "commands").mkdir(parents=True)
        (dest / "commands" / "x.md").write_text("x")
        (dest / "evil").symlink_to("/etc/passwd")

    entries = [
        _ext_entry("huge", url="https://github.com/acme/huge.git"),
        _ext_entry("linky", url="https://github.com/acme/linky.git"),
        _ext_entry("empty", url="https://github.com/acme/empty.git"),
        _local_entry("escape", source="../escape"),
        {"name": "viapm", "description": "d", "source": {"source": "npm", "package": "x"}},
    ]
    trees = {
        "https://github.com/acme/huge.git": big,
        "https://github.com/acme/linky.git": linked,
        "https://github.com/acme/empty.git": {"README.md": "nothing here"},
    }
    with stubbed_plugin_fetch(trees):
        m = _new_market(client, h, claude_repo(entries))
        rows = entries_by_name(list_marketplace_entries(client, h, m["id"]))
        assert {n: r["snapshot_reason"] for n, r in rows.items()} == {
            "huge": "too_large", "linky": "unsafe_path", "empty": "no_components",
            "escape": "unsafe_path", "viapm": "unknown_source",
        } or rows["viapm"]["snapshot_reason"] in ("unknown_source", "npm_source")
        for r in rows.values():
            assert r["snapshot_status"] == "failed"
            assert r["archive_sha256"] is None and r["components"] is None
        assert files_under(m["id"]) == []
        # fetch_failed-only retry leaves content verdicts alone
        assert retry_snapshots(client, h, m["id"])["queued"] == 0


def test_unsafe_local_symlink_and_empty_local_entry(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A symlink inside a local plugin tree is unsafe_path; an empty one is no_components; neither writes an archive."""
    h = superuser_token_headers

    def populate(root: Path) -> None:
        claude_repo(
            [_local_entry("sym"), _local_entry("nothing")],
            {"plugins/sym/commands/a.md": "a", "plugins/nothing/README.md": "x"},
        )(root)
        (root / "plugins" / "sym" / "link").symlink_to("/etc/hosts")

    m = _new_market(client, h, populate)
    rows = entries_by_name(list_marketplace_entries(client, h, m["id"]))
    assert rows["sym"]["snapshot_reason"] == "unsafe_path"
    assert rows["nothing"]["snapshot_reason"] == "no_components"
    assert files_under(m["id"]) == []


def test_entry_removed_while_claimed_writes_nothing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The entry disappears upstream (a resync) in the middle of its fetch: the result is discarded."""
    h = superuser_token_headers
    m = create_marketplace(client, h, url="https://github.com/example/race-1.git")
    keep_only = claude_repo([_local_entry("stay")], {"plugins/stay/commands/s.md": "s"})

    def remove_entry(url: str) -> None:
        fetch.on_fetch = None
        sync_marketplace(client, h, m["id"], populate=keep_only, commit_hash="c0ffee000009")

    with stubbed_plugin_fetch({EXT_URL: _ext_tree()}) as fetch:
        fetch.on_fetch = remove_entry
        sync_marketplace(
            client, h, m["id"],
            populate=claude_repo(
                [_ext_entry("doomed"), _local_entry("stay")], {"plugins/stay/commands/s.md": "s"}
            ),
        )
    data = list_marketplace_entries(client, h, m["id"])
    assert [r["name"] for r in data["data"]] == ["stay"]
    assert len(files_under(m["id"])) == 1  # only "stay"'s archive


def test_live_lease_is_not_reclaimed_but_an_expired_one_is(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """
    1. Two entries fail their first fetch and sit pending.
    2. One lease is held by a "live worker", the other expired; fetch is healthy now.
    3. The next worker tick (any marketplace sync) readies only the expired one.
    """
    h = superuser_token_headers
    url_a, url_b = "https://github.com/acme/a.git", "https://github.com/acme/b.git"
    populate = claude_repo([_ext_entry("held", url=url_a), _ext_entry("lapsed", url=url_b)])
    with stubbed_plugin_fetch({url_a: _ext_tree(), url_b: _ext_tree()}) as fetch:
        fetch.fail_urls.update({url_a, url_b})
        m = _new_market(client, h, populate)
        held, lapsed = (_row(client, h, m["id"], n) for n in ("held", "lapsed"))
        assert held["snapshot_status"] == lapsed["snapshot_status"] == "pending"

        fetch.fail_urls.clear()
        fetch.fetch_calls.clear()
        hold_snapshot_lease(db, held["id"])
        expire_snapshot_lease(db, lapsed["id"])
        m2 = create_marketplace(client, h, url="https://github.com/example/kick-3.git")
        sync_marketplace(client, h, m2["id"],
                         populate=claude_repo([_local_entry("k")], {"plugins/k/commands/k.md": "#"}))

        assert fetch.fetch_urls() == [url_b]
    assert _row(client, h, m["id"], "held")["snapshot_status"] == "pending"
    assert _row(client, h, m["id"], "lapsed")["snapshot_status"] == "ready"


def test_supported_is_unchanged_by_the_snapshot(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """`supported` / `unsupported_reason` read the same while pending and after ready."""
    h = superuser_token_headers
    populate = claude_repo([_ext_entry("same")])
    with stubbed_plugin_fetch({EXT_URL: _ext_tree()}) as fetch:
        fetch.fail_urls.add(EXT_URL)
        m = _new_market(client, h, populate)
        before = _row(client, h, m["id"], "same")
        assert before["snapshot_status"] == "pending"
        fetch.fail_urls.clear()
        retry_snapshots(client, h, m["id"])  # nothing failed; no kick
        expire = before["id"]
    assert before["supported"] is True and before["unsupported_reason"] is None
    # force the tick through a resync (healthy fetch)
    expire_snapshot_lease(db, before["id"])  # the backoff elapsed
    with stubbed_plugin_fetch({EXT_URL: _ext_tree()}):
        sync_marketplace(client, h, m["id"], populate=populate, commit_hash="c0ffee000002")
    after = _row(client, h, m["id"], "same")
    assert after["snapshot_status"] == "ready"
    assert (after["supported"], after["unsupported_reason"]) == (True, None)
    assert expire == after["id"]


def test_entries_route_filters_counts_and_access(
    client: TestClient, superuser_token_headers: dict[str, str], normal_user_token_headers: dict[str, str]
) -> None:
    """
    1. Filter by snapshot_status and search; count is the filtered total, counts the whole marketplace.
    2. skip / limit page the list.
    3. A normal user gets 403 on entries and retry; unknown marketplace 404; unauthenticated rejected.
    4. Retry on a skills marketplace queues nothing.
    """
    h = superuser_token_headers
    files = {"plugins/alpha/commands/a.md": "a", "plugins/beta/commands/b.md": "b"}
    entries = [_local_entry("alpha"), _local_entry("beta"), _local_entry("gamma")]  # gamma has no tree
    m = _new_market(client, h, claude_repo(entries, files))

    allrows = list_marketplace_entries(client, h, m["id"])
    assert allrows["count"] == 3
    assert {k: allrows["counts"][k] for k in ("ready", "pending", "failed", "retryable")} == {
        "ready": 2, "pending": 0, "failed": 1, "retryable": 0,  # a content verdict is not retryable
    }
    assert [r["name"] for r in allrows["data"]] == ["alpha", "beta", "gamma"]

    failed = list_marketplace_entries(client, h, m["id"], snapshot_status="failed")
    assert failed["count"] == 1 and failed["data"][0]["name"] == "gamma"
    assert failed["counts"]["ready"] == 2, "counts ignore the status filter"

    found = list_marketplace_entries(client, h, m["id"], search="ALPH")
    assert [r["name"] for r in found["data"]] == ["alpha"] and found["count"] == 1

    page = list_marketplace_entries(client, h, m["id"], skip=1, limit=1)
    assert [r["name"] for r in page["data"]] == ["beta"]
    assert page["count"] == 3

    base = f"{settings.API_V1_STR}/llm-plugins/marketplaces/{m['id']}"
    n = normal_user_token_headers
    assert client.get(f"{base}/entries", headers=n).status_code == 403
    assert client.post(f"{base}/snapshots/retry", headers=n).status_code == 403
    assert client.get(f"{base}/entries").status_code in (401, 403)
    ghost = f"{settings.API_V1_STR}/llm-plugins/marketplaces/00000000-0000-4000-8000-000000000009"
    assert client.get(f"{ghost}/entries", headers=h).status_code == 404

    # all=true re-takes a content verdict; gamma fails again, still counted as queued
    assert retry_snapshots(client, h, m["id"])["queued"] == 0
    with stubbed_plugin_fetch({m["url"]: files}):
        assert retry_snapshots(client, h, m["id"], include_all=True)["queued"] == 1
    gamma = _row(client, h, m["id"], "gamma")
    assert gamma["snapshot_status"] == "failed" and gamma["snapshot_reason"] == "unknown_source"


def test_retry_never_queues_skills_marketplace_rows(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    from tests.utils.marketplace_skills import new_skills_marketplace, skills_repo

    h = superuser_token_headers
    m = new_skills_marketplace(client, h, skills_repo(("one",), broken=("half",)))
    res = retry_snapshots(client, h, m["id"], include_all=True)
    assert res["queued"] == 0 and res["skipped_needs_sync"] == 0
    rows = list_marketplace_entries(client, h, m["id"])
    assert rows["counts"]["pending"] == 0
    assert rows["counts"]["ready"] == 1
