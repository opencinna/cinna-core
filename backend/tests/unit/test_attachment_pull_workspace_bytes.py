"""Unit tests for ``AttachmentMaterializationService._pull_workspace_bytes``.

Focus: the Docker host-volume fast path is only valid for the *main* workspace
volume. Files under a separately-mounted sub-volume — notably ``app-data/`` (the
per-user App Data volume mounted at ``/app/workspace/app-data``) — are absent
from the workspace host dir, so ``get_local_workspace_file_path`` returns None.
The reader must then fall back to the in-container
``fetch_workspace_item_with_meta`` HTTP path (the same one ``AgentStatusService``
uses to read ``app-data/storage/STATUS.md``).

Regression for: agent attachments under ``app-data/...`` silently rejected
because the host read missed the separate mount → no attachment, no badge.

No database, no HTTP — fake adapters only.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.services.files.attachment_materialization_service import (
    AttachmentMaterializationService,
)


def _run(coro):
    # Use asyncio.run (fresh loop per call) — the repo convention for async unit
    # tests. asyncio.get_event_loop() picks up a loop closed by a prior API test
    # when the suites run together, which fails.
    return asyncio.run(coro)


class _Meta:
    def __init__(self, exists: bool):
        self.exists = exists


async def _byte_stream(data: bytes):
    yield data


class _HostHitAdapter:
    """Docker adapter where the host path exists (main workspace file)."""

    def __init__(self, host_path):
        self._host_path = host_path
        self.fetch_calls = 0

    def get_local_workspace_file_path(self, relative_path: str):
        return self._host_path

    async def fetch_workspace_item_with_meta(self, path: str):
        self.fetch_calls += 1
        raise AssertionError("container fetch must not be used when host read works")


class _AppDataAdapter:
    """Docker adapter where the host read misses (separate app-data mount),
    but the container fetch succeeds."""

    def __init__(self, content: bytes):
        self._content = content
        self.host_calls = 0
        self.fetch_calls = 0
        self.fetched_path = None

    def get_local_workspace_file_path(self, relative_path: str):
        self.host_calls += 1
        return None  # not under the workspace host dir

    async def fetch_workspace_item_with_meta(self, path: str):
        self.fetch_calls += 1
        self.fetched_path = path
        return _Meta(True), _byte_stream(self._content)


class _MissingEverywhereAdapter:
    def get_local_workspace_file_path(self, relative_path: str):
        return None

    async def fetch_workspace_item_with_meta(self, path: str):
        return _Meta(False), _byte_stream(b"")


def test_host_path_hit_short_circuits_without_container_fetch(tmp_path: Path):
    f = tmp_path / "report.txt"
    f.write_bytes(b"hello main workspace")
    adapter = _HostHitAdapter(f)

    result = _run(
        AttachmentMaterializationService._pull_workspace_bytes(adapter, "files/report.txt")
    )
    assert result is not None
    content, mime = result
    assert content == b"hello main workspace"
    assert adapter.fetch_calls == 0  # host read was sufficient


def test_app_data_path_falls_back_to_container_fetch():
    # The exact failure mode from the bug: file lives under app-data/, which is a
    # separate mount, so the host read returns None and we must fetch via container.
    adapter = _AppDataAdapter(b"line1\nline2\n")

    result = _run(
        AttachmentMaterializationService._pull_workspace_bytes(
            adapter, "app-data/uploads/random_lines.txt"
        )
    )
    assert result is not None
    content, mime = result
    assert content == b"line1\nline2\n"
    assert adapter.host_calls == 1          # tried the fast path first
    assert adapter.fetch_calls == 1         # then fell back to the container
    assert adapter.fetched_path == "app-data/uploads/random_lines.txt"
    assert mime == "text/plain"             # sniffed from .txt


def test_missing_everywhere_returns_none():
    adapter = _MissingEverywhereAdapter()
    result = _run(
        AttachmentMaterializationService._pull_workspace_bytes(
            adapter, "app-data/uploads/nope.txt"
        )
    )
    assert result is None
