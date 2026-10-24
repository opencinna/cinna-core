"""Plugin snapshot hardening: symlinked parents, hostile sha / ref, attempts across syncs.

Written against the K1 review-fix behaviour: a plugin root reached through a
symlinked parent is `unsafe_path`; a malformed `sha` or a `-`-leading `ref`
never reaches git; a resync of a still-pending entry with an unchanged source
keeps its attempt counter so MAX_ATTEMPTS is reachable across syncs.
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.agents.marketplace_skills.marketplace_plugin_snapshots_test import (
    EXT_URL,
    _ext_entry,
    _ext_tree,
    _local_entry,
    _new_market,
    _row,
)
from tests.utils.llm_plugin import (
    claude_repo,
    create_marketplace,
    expire_snapshot_lease,
    stubbed_plugin_fetch,
    sync_marketplace,
)
from tests.utils.marketplace_skills import files_under


def test_local_root_behind_a_symlinked_parent_is_unsafe_path(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    outside = Path(tempfile.mkdtemp(prefix="outside_"))
    (outside / "app" / "commands").mkdir(parents=True)
    (outside / "app" / "commands" / "x.md").write_text("x")

    def populate(root: Path) -> None:
        claude_repo([{"name": "via", "description": "d", "source": "./p/app"}])(root)
        os.symlink(outside, root / "p")

    m = _new_market(client, h, populate)
    row = _row(client, h, m["id"], "via")
    assert row["snapshot_status"] == "failed" and row["snapshot_reason"] == "unsafe_path"
    assert files_under(m["id"]) == []


def test_external_root_behind_a_symlinked_parent_is_unsafe_path(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    outside = Path(tempfile.mkdtemp(prefix="outside_"))
    (outside / "app" / "commands").mkdir(parents=True)
    (outside / "app" / "commands" / "x.md").write_text("x")

    def tree(dest: Path) -> None:
        os.symlink(outside, dest / "p")

    entry = _ext_entry("via")
    entry["source"]["path"] = "p/app"
    with stubbed_plugin_fetch({EXT_URL: tree}):
        m = _new_market(client, h, claude_repo([entry]))
    row = _row(client, h, m["id"], "via")
    assert row["snapshot_status"] == "failed" and row["snapshot_reason"] == "unsafe_path"
    assert files_under(m["id"]) == []


def test_hostile_sha_and_dash_ref_never_reach_git(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    entries = [
        _ext_entry("badsha", sha="--upload-pack=touch /tmp/x"),
        _ext_entry("shortsha", sha="zzzz"),
        _ext_entry("dashref", sha=None, url="https://github.com/acme/d.git"),
    ]
    entries[2]["source"]["ref"] = "--upload-pack=evil"
    with stubbed_plugin_fetch({EXT_URL: _ext_tree(), "https://github.com/acme/d.git": _ext_tree()}) as fetch:
        m = _new_market(client, h, claude_repo(entries))
    assert fetch.fetch_calls == []
    assert all(ref is None or not ref.startswith("-") for _u, ref in fetch.ls_remote_calls)
    for name in ("badsha", "shortsha", "dashref"):
        row = _row(client, h, m["id"], name)
        assert row["snapshot_status"] == "failed", name
        assert row["snapshot_reason"] == "unknown_source", name
        assert row["archive_sha256"] is None


def test_attempts_accumulate_across_resyncs_of_an_unchanged_pending_entry(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    h = superuser_token_headers
    populate = claude_repo([_ext_entry("stuck")])
    with patch.object(settings, "PLUGIN_SNAPSHOT_MAX_ATTEMPTS", 2):
        with stubbed_plugin_fetch({EXT_URL: _ext_tree()}) as fetch:
            fetch.fail_urls.add(EXT_URL)
            m = _new_market(client, h, populate)
            row = _row(client, h, m["id"], "stuck")
            assert row["snapshot_status"] == "pending"
            for i in range(3):
                expire_snapshot_lease(db, row["id"])
                sync_marketplace(client, h, m["id"], populate=populate, commit_hash=f"c0ffee00010{i}")
                if _row(client, h, m["id"], "stuck")["snapshot_status"] == "failed":
                    break
    final = _row(client, h, m["id"], "stuck")
    assert final["snapshot_status"] == "failed" and final["snapshot_reason"] == "fetch_failed"
