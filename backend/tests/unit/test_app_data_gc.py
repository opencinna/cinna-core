"""Unit tests for AppDataService on-disk orphan garbage collection.

Covers the diff logic in ``find_orphan_dirs`` / ``purge_orphan_dirs``:
- a deleted user's whole tree is reclaimed
- a live user's bundle dir with a matching volume row is kept
- a live user's bundle dir with no matching row is reclaimed
- the consumer ``_<catalog_type>`` slot keeps its parent bundle dir alive
- non-UUID top-level dirs are never touched
- the grace window protects freshly modified dirs (in-flight installs)
- purge actually rmtrees and reports counts

No DB or filesystem fixtures from the root conftest are needed — a fake
session feeds queued query results and ``tmp_path`` backs the storage root.

Run:
    cd backend && python -m pytest tests/unit/test_app_data_gc.py -v
"""
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.bundles.app_data_service import AppDataService


# ---------------------------------------------------------------------------
# Fakes — find_orphan_dirs calls session.exec(...).all() twice, in order:
#   1) select(User.id)        -> user-id rows
#   2) select(AppDataVolume)  -> volume rows (only .host_path is read)
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, *queued):
        self._queue = list(queued)

    def exec(self, _stmt):
        return _Result(self._queue.pop(0))


class _Vol:
    """Stand-in for AppDataVolume — only ``host_path`` is consumed."""

    def __init__(self, host_path: Path):
        self.host_path = str(host_path)


@pytest.fixture(autouse=True)
def _plain_storage(tmp_path, monkeypatch):
    """Point the storage root at tmp_path with no Docker-in-Docker translation."""
    monkeypatch.setattr(
        "app.services.bundles.app_data_service.settings.APP_DATA_STORAGE_DIR",
        str(tmp_path),
    )
    monkeypatch.setattr(
        "app.services.bundles.app_data_service.settings.HOST_APP_DATA_DIR",
        "",
    )
    return tmp_path


def _mk(*parts: str, root: Path) -> Path:
    p = root.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


NO_GRACE = timedelta(0)


def test_deleted_user_tree_is_orphaned(_plain_storage):
    root = _plain_storage
    live_user = uuid.uuid4()
    dead_user = uuid.uuid4()
    _mk(str(live_user), "io.cinna.keep", root=root)
    dead_dir = _mk(str(dead_user), "io.cinna.gone", root=root)

    session = _FakeSession(
        [live_user],  # only the live user still exists
        [_Vol(root / str(live_user) / "io.cinna.keep")],
    )

    orphans = AppDataService.find_orphan_dirs(session, grace=NO_GRACE)

    assert orphans == [dead_dir.parent]  # the whole <dead_user> dir


def test_live_user_bundle_without_row_is_orphaned(_plain_storage):
    root = _plain_storage
    user = uuid.uuid4()
    keep = _mk(str(user), "io.cinna.keep", root=root)
    stale = _mk(str(user), "io.cinna.stale", root=root)

    session = _FakeSession([user], [_Vol(keep)])

    orphans = AppDataService.find_orphan_dirs(session, grace=NO_GRACE)

    assert orphans == [stale]


def test_consumer_slot_keeps_parent_bundle_dir(_plain_storage):
    root = _plain_storage
    user = uuid.uuid4()
    bundle = _mk(str(user), "io.cinna.app", root=root)
    server_slot = _mk(str(user), "io.cinna.app", "_server", root=root)

    # Only the consumer (_server) slot has a row — its path nests under bundle.
    session = _FakeSession([user], [_Vol(server_slot)])

    orphans = AppDataService.find_orphan_dirs(session, grace=NO_GRACE)

    assert bundle not in orphans
    assert orphans == []


def test_non_uuid_toplevel_dir_is_skipped(_plain_storage):
    root = _plain_storage
    _mk("not-a-user-dir", root=root)

    session = _FakeSession([], [])  # no users, no volumes

    orphans = AppDataService.find_orphan_dirs(session, grace=NO_GRACE)

    assert orphans == []


def test_grace_window_protects_fresh_dirs(_plain_storage):
    root = _plain_storage
    dead_user = uuid.uuid4()
    _mk(str(dead_user), "io.cinna.fresh", root=root)

    session = _FakeSession([], [])  # user gone, dir freshly created

    # Default 1-day grace: the just-created dir must be left alone.
    orphans = AppDataService.find_orphan_dirs(session)

    assert orphans == []


def test_purge_removes_orphans_and_counts(_plain_storage):
    root = _plain_storage
    dead_user = uuid.uuid4()
    dead_dir = _mk(str(dead_user), "io.cinna.gone", root=root)
    (dead_dir / "storage").mkdir()
    (dead_dir / "storage" / "f.txt").write_text("data")

    session = _FakeSession([], [])

    removed, failed = AppDataService.purge_orphan_dirs(session, grace=NO_GRACE)

    assert (removed, failed) == (1, 0)
    assert not (root / str(dead_user)).exists()
