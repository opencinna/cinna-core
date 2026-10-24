"""Marketplace plugin snapshots: the sync-time planner and the fetch worker.

Every ``claude`` / ``codex`` entry gets one content snapshot: an archive of its
plugin tree plus the component analysis of :mod:`plugin_components`.

* **Local entries** (a folder of the marketplace repository) are snapshotted by
  :class:`PluginSnapshotPlanner` *inside* the sync, while the clone exists.
* **External entries** (``url`` / ``git-subdir`` / ``github`` sources) are
  marked ``pending`` by the planner and fetched later by
  :class:`PluginSnapshotWorker`. The sync never waits on an external clone.

The queue is the plugin rows themselves (``snapshot_status='pending'``), so
nothing is lost to a ``--reload`` or a restart. A tick claims rows with
``FOR UPDATE SKIP LOCKED`` under a lease a heartbeat renews while the work is
in flight; a killed process's rows return when their lease lapses. Results are
written compare-and-set on ``(snapshot_claim_id, snapshot_source_key)``: a row
that a concurrent sync re-planned, or deleted, discards the work. The archive
is promoted only once that row lock is held.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
from sqlmodel import Session, select

from app.core import db as core_db
from app.core.config import settings
from app.models.plugins.llm_plugin import (
    LLMPluginMarketplace,
    LLMPluginMarketplacePlugin,
)
from app.services.common.egress_guard import EgressBlockedError
from app.services.knowledge import git_operations
from app.services.plugins.marketplace_skill_snapshots import (
    MarketplaceSkillSnapshots,
    StagedArchive,
)
from app.services.plugins.plugin_components import analyse_plugin_tree
from app.services.plugins.plugin_snapshot_source import (
    SnapshotSource,
    Unsupported,
    https_url,
    resolve_source,
    row_fields,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC, the form the ``DateTime`` columns store."""
    return datetime.now(UTC).replace(tzinfo=None)


def _archive_prefix_ok(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..")


def _analyse_and_stage(
    checkout: Path,
    subdir: str,
    entry_config: dict | None,
    name: str,
    fmt: str,
    staging_dir: Path,
) -> tuple[str | None, dict | None, bool | None, StagedArchive | None]:
    """``(reason, components, runs_code, staged)`` for ``subdir`` of a checkout.

    The one entry point for the planner (local entries, the marketplace
    clone) and the worker (external checkouts): containment of the plugin
    root inside the checkout is enforced by ``analyse_plugin_tree``.
    """
    if not _archive_prefix_ok(name):
        return "unsafe_path", None, None, None
    config = dict(entry_config) if isinstance(entry_config, dict) else {}
    config["name"] = name
    root = checkout / subdir if subdir else checkout
    analysis = analyse_plugin_tree(root, config, fmt, checkout=checkout)
    if analysis.reason is not None:
        return analysis.reason, None, None, None
    staged = MarketplaceSkillSnapshots.stage_tree(staging_dir, analysis.files, name)
    return None, analysis.components, analysis.runs_code, staged


# =============================================================================
# Planner (inside the sync)
# =============================================================================


class PluginSnapshotPlanner:
    @staticmethod
    def plan(
        *,
        clone_dir: str,
        fmt: str,
        plugins_data: list[dict],
        existing_rows: dict[str, LLMPluginMarketplacePlugin],
        staging_dir: Path,
        commit_hash: str | None,
    ) -> dict[str, StagedArchive]:
        """Set the ``snapshot_*`` keys on every entry dict; stage local archives.

        Returns the staged archives by entry name, to be promoted in the
        upsert's ``before_commit`` hook.
        """
        staged: dict[str, StagedArchive] = {}
        now = _utcnow()
        for entry in plugins_data:
            name = entry["name"]
            src = resolve_source(entry, fmt)
            if isinstance(src, Unsupported):
                PluginSnapshotPlanner._set_failed(entry, src.reason, None, now)
                continue
            if src.kind == "local":
                reason, components, runs_code, archive = _analyse_and_stage(
                    Path(clone_dir), src.subdir, entry.get("config"), name, fmt, staging_dir
                )
                if reason is not None:
                    PluginSnapshotPlanner._set_failed(entry, reason, src.key, now)
                    entry["snapshot_commit"] = commit_hash
                    continue
                staged[name] = archive
                entry.update(
                    archive_sha256=archive.sha256,
                    archive_size=archive.size,
                    snapshot_status="ready",
                    snapshot_reason=None,
                    snapshot_source_key=src.key,
                    snapshot_commit=commit_hash,
                    snapshot_ref=None,
                    snapshot_at=now,
                    snapshot_attempts=0,
                    snapshot_claim_id=None,
                    snapshot_lease_until=None,
                    components=components,
                    runs_code=runs_code,
                )
                continue
            PluginSnapshotPlanner._plan_external(entry, src, existing_rows.get(name))
        return staged

    @staticmethod
    def _set_failed(entry: dict, reason: str, key: str | None, now: datetime) -> None:
        entry.update(
            archive_sha256=None,
            archive_size=None,
            snapshot_status="failed",
            snapshot_reason=reason,
            snapshot_source_key=key,
            snapshot_commit=None,
            snapshot_ref=None,
            snapshot_at=now,
            snapshot_attempts=0,
            snapshot_claim_id=None,
            snapshot_lease_until=None,
            components=None,
            runs_code=None,
        )

    @staticmethod
    def _plan_external(
        entry: dict, src: SnapshotSource, existing: LLMPluginMarketplacePlugin | None
    ) -> None:
        same_source = existing is not None and existing.snapshot_source_key == src.key
        if same_source and existing.snapshot_status == "pending":
            # Already queued (or in flight) for this source: leave attempts,
            # claim and lease alone so MAX_ATTEMPTS still bites and the work
            # in flight is kept. The worker reads the target from the row's
            # config, which this sync updates.
            return
        if src.sha and same_source and existing.snapshot_commit == src.sha:
            if (
                existing.snapshot_status == "ready"
                and existing.archive_sha256
                and MarketplaceSkillSnapshots.existing_archive(
                    existing.marketplace_id, existing.id, existing.archive_sha256
                )
            ):
                return  # unchanged: no keys, nothing queued
            if (
                existing.snapshot_status == "failed"
                and existing.snapshot_reason != "fetch_failed"
            ):
                return  # same content, same verdict
        entry.update(
            snapshot_status="pending",
            snapshot_reason=None,
            snapshot_source_key=src.key,
            snapshot_ref=src.ref,
            snapshot_attempts=0,
            snapshot_claim_id=None,
            snapshot_lease_until=None,
        )
        if not same_source:
            # The kept commit describes another source; the worker must not
            # short-circuit on it.
            entry["snapshot_commit"] = None


# =============================================================================
# Worker
# =============================================================================


SessionFactory = Callable[[], AbstractContextManager[Session]]


@dataclass
class _Job:
    plugin_id: uuid.UUID
    marketplace_id: uuid.UUID
    name: str
    fmt: str
    fields: dict[str, Any]
    claimed_key: str | None
    snapshot_commit: str | None
    archive_sha256: str | None
    commit_hash: str | None
    marketplace_url: str | None
    marketplace_has_key: bool
    attempts: int


@dataclass
class _Group:
    url: str
    sha: str | None
    ref: str | None
    items: list[tuple[_Job, SnapshotSource]] = field(default_factory=list)


_EXHAUST_SQL = text(
    """
    UPDATE llm_plugin_marketplace_plugin
    SET snapshot_status = 'failed', snapshot_reason = 'fetch_failed',
        snapshot_claim_id = NULL, snapshot_lease_until = NULL,
        snapshot_at = (now() AT TIME ZONE 'utc')
    WHERE snapshot_status = 'pending'
      AND snapshot_attempts >= :max_attempts
      AND (snapshot_lease_until IS NULL
           OR snapshot_lease_until < (now() AT TIME ZONE 'utc'))
    RETURNING id
    """
)

_CLAIM_SQL = text(
    """
    UPDATE llm_plugin_marketplace_plugin
    SET snapshot_claim_id = :claim,
        snapshot_lease_until = (now() AT TIME ZONE 'utc') + make_interval(secs => :lease),
        snapshot_attempts = snapshot_attempts + 1
    WHERE id IN (
        SELECT id FROM llm_plugin_marketplace_plugin
        WHERE snapshot_status = 'pending'
          AND plugin_type IN ('claude', 'codex')
          AND (snapshot_lease_until IS NULL
               OR snapshot_lease_until < (now() AT TIME ZONE 'utc'))
        ORDER BY snapshot_source_key NULLS LAST, id
        LIMIT :n
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id
    """
)

_HEARTBEAT_SQL = text(
    """
    UPDATE llm_plugin_marketplace_plugin
    SET snapshot_lease_until = (now() AT TIME ZONE 'utc') + make_interval(secs => :lease)
    WHERE snapshot_claim_id = ANY(:claims) AND snapshot_status = 'pending'
    """
)


def _default_session_factory() -> AbstractContextManager[Session]:
    return core_db.create_session()


class PluginSnapshotWorker:
    _scheduler: BackgroundScheduler | None = None
    _executor: ThreadPoolExecutor | None = None
    _state_lock = threading.Lock()
    _tick_lock = threading.Lock()
    #: claim id → groups of that claim still queued or running.
    _inflight: dict[uuid.UUID, int] = {}

    # -- lifecycle -------------------------------------------------------------

    @classmethod
    def start(cls) -> None:
        cls._executor = ThreadPoolExecutor(
            max_workers=settings.PLUGIN_SNAPSHOT_WORKERS,
            thread_name_prefix="plugin-snapshot",
        )
        cls._scheduler = BackgroundScheduler()
        cls._scheduler.add_job(
            cls.tick,
            "interval",
            seconds=settings.PLUGIN_SNAPSHOT_TICK_SECONDS,
            id="plugin_snapshot_tick",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(UTC) + timedelta(seconds=5),
        )
        cls._scheduler.add_job(
            cls.heartbeat,
            "interval",
            seconds=settings.PLUGIN_SNAPSHOT_HEARTBEAT_SECONDS,
            id="plugin_snapshot_heartbeat",
            max_instances=1,
            coalesce=True,
        )
        cls._scheduler.start()
        logger.info(
            "Plugin snapshot worker started (%d workers, tick %ds)",
            settings.PLUGIN_SNAPSHOT_WORKERS,
            settings.PLUGIN_SNAPSHOT_TICK_SECONDS,
        )

    @classmethod
    def shutdown(cls) -> None:
        if cls._scheduler is not None and cls._scheduler.running:
            cls._scheduler.shutdown(wait=False)
        if cls._executor is not None:
            cls._executor.shutdown(wait=False, cancel_futures=True)
        cls._scheduler = None
        cls._executor = None
        with cls._state_lock:
            cls._inflight.clear()
        logger.info("Plugin snapshot worker stopped")

    @classmethod
    def kick(cls, session: Session | None = None) -> None:
        """Run a tick now (off the caller's thread); inline under TESTING."""
        if settings.TESTING:
            if session is not None:
                cls.drain_inline(session)
            return
        if cls._executor is None:
            return
        threading.Thread(target=cls.tick, name="plugin-snapshot-kick", daemon=True).start()

    # -- scheduling ------------------------------------------------------------

    @classmethod
    def _inflight_groups(cls) -> int:
        with cls._state_lock:
            return sum(cls._inflight.values())

    @classmethod
    def tick(cls) -> None:
        """Claim while the pool has room and submit the work. Never raises."""
        executor = cls._executor
        if executor is None or not cls._tick_lock.acquire(blocking=False):
            return
        try:
            while cls._inflight_groups() < settings.PLUGIN_SNAPSHOT_WORKERS:
                with _default_session_factory() as session:
                    claim_id, groups, immediate = cls._claim(
                        session, settings.PLUGIN_SNAPSHOT_BATCH
                    )
                if claim_id is None:
                    break
                for job, outcome in immediate:
                    cls._finish(_default_session_factory, job, claim_id, outcome)
                if not groups:
                    continue
                with cls._state_lock:
                    cls._inflight[claim_id] = cls._inflight.get(claim_id, 0) + len(groups)
                for group in groups:
                    executor.submit(cls._run_pooled, group, claim_id)
        except Exception as exc:
            logger.error("Plugin snapshot tick failed: %s", exc, exc_info=True)
        finally:
            cls._tick_lock.release()

    @classmethod
    def _run_pooled(cls, group: _Group, claim_id: uuid.UUID) -> None:
        try:
            cls._run_group(_default_session_factory, group, claim_id)
        except Exception as exc:
            logger.error("Plugin snapshot group %s failed: %s", group.url, exc, exc_info=True)
            for job, _src in group.items:
                cls._release(_default_session_factory, job, claim_id)
        finally:
            with cls._state_lock:
                left = cls._inflight.get(claim_id, 1) - 1
                if left <= 0:
                    cls._inflight.pop(claim_id, None)
                else:
                    cls._inflight[claim_id] = left
            cls.kick()

    @classmethod
    def heartbeat(cls) -> None:
        with cls._state_lock:
            claims = list(cls._inflight)
        if not claims:
            return
        try:
            with _default_session_factory() as session:
                session.execute(
                    _HEARTBEAT_SQL,
                    {"claims": claims, "lease": settings.PLUGIN_SNAPSHOT_LEASE_SECONDS},
                )
                session.commit()
        except Exception as exc:
            logger.error("Plugin snapshot heartbeat failed: %s", exc, exc_info=True)

    @classmethod
    def drain_inline(cls, session: Session) -> None:
        """TESTING only: claim and run everything in the caller's thread."""
        factory: SessionFactory = lambda: nullcontext(session)  # noqa: E731
        while True:
            claim_id, groups, immediate = cls._claim(session, settings.PLUGIN_SNAPSHOT_BATCH)
            if claim_id is None:
                return
            for job, outcome in immediate:
                cls._finish(factory, job, claim_id, outcome)
            for group in groups:
                try:
                    cls._run_group(factory, group, claim_id)
                except Exception:
                    logger.exception("Inline plugin snapshot group failed")
                    for job, _src in group.items:
                        cls._release(factory, job, claim_id)

    # -- claim -----------------------------------------------------------------

    @classmethod
    def _claim(
        cls, session: Session, n: int
    ) -> tuple[uuid.UUID | None, list[_Group], list[tuple[_Job, dict]]]:
        """Claim up to ``n`` rows; return the fetch groups and the rows that
        resolve to a verdict without fetching (``immediate``)."""
        session.execute(
            _EXHAUST_SQL, {"max_attempts": settings.PLUGIN_SNAPSHOT_MAX_ATTEMPTS}
        )
        claim_id = uuid.uuid4()
        ids = [
            row[0]
            for row in session.execute(
                _CLAIM_SQL,
                {"claim": claim_id, "lease": settings.PLUGIN_SNAPSHOT_LEASE_SECONDS, "n": n},
            ).all()
        ]
        session.commit()
        if not ids:
            return None, [], []
        pairs = session.exec(
            select(LLMPluginMarketplacePlugin, LLMPluginMarketplace)
            .join(
                LLMPluginMarketplace,
                LLMPluginMarketplace.id == LLMPluginMarketplacePlugin.marketplace_id,
            )
            .where(LLMPluginMarketplacePlugin.id.in_(ids))
        ).all()
        groups: dict[tuple[str, str | None, str | None], _Group] = {}
        immediate: list[tuple[_Job, dict]] = []
        for row, marketplace in pairs:
            job = _Job(
                plugin_id=row.id,
                marketplace_id=row.marketplace_id,
                name=row.name,
                fmt=row.plugin_type,
                fields=row_fields(row),
                claimed_key=row.snapshot_source_key,
                snapshot_commit=row.snapshot_commit,
                archive_sha256=row.archive_sha256,
                commit_hash=row.commit_hash,
                marketplace_url=marketplace.url,
                marketplace_has_key=marketplace.ssh_key_id is not None,
                attempts=row.snapshot_attempts,
            )
            src = resolve_source(job.fields, job.fmt)
            if isinstance(src, Unsupported):
                immediate.append((job, {"status": "failed", "reason": src.reason}))
                continue
            if src.kind == "local":
                # Only rows queued before the planner ran (the migration's
                # bootstrap) reach here: fetch the marketplace repository at
                # the commit the row was parsed from.
                url = None if job.marketplace_has_key else https_url(job.marketplace_url)
                if url is None or not job.commit_hash:
                    immediate.append((job, {"status": "failed", "reason": "fetch_failed", "key": src.key}))
                    continue
                gkey = (url, job.commit_hash, None)
                sha, ref = job.commit_hash, None
            else:
                url, sha, ref = src.url, src.sha, src.ref
                gkey = (url, sha, None if sha else ref)
            groups.setdefault(gkey, _Group(url=url, sha=sha, ref=None if sha else ref)).items.append(
                (job, src)
            )
        session.commit()
        return claim_id, list(groups.values()), immediate

    # -- work ------------------------------------------------------------------

    @classmethod
    def _run_group(cls, factory: SessionFactory, group: _Group, claim_id: uuid.UUID) -> None:
        started = time.monotonic()
        try:
            target = group.sha or git_operations.ls_remote_commit(group.url, group.ref)
        except EgressBlockedError:
            for job, src in group.items:
                cls._finish(factory, job, claim_id, {"status": "failed", "reason": "fetch_failed", "key": src.key}, started)
            return
        except Exception as exc:
            logger.info("plugin_snapshot ls-remote %s failed: %s", group.url, exc)
            for job, _src in group.items:
                cls._release(factory, job, claim_id)
            return

        to_fetch: list[tuple[_Job, SnapshotSource]] = []
        for job, src in group.items:
            if (
                job.snapshot_commit == target
                and job.claimed_key == src.key
                and job.archive_sha256
                and MarketplaceSkillSnapshots.existing_archive(
                    job.marketplace_id, job.plugin_id, job.archive_sha256
                )
            ):
                cls._finish(factory, job, claim_id, {"status": "unchanged", "key": src.key, "commit": target}, started)
            else:
                to_fetch.append((job, src))
        if not to_fetch:
            return

        subdirs = [src.subdir for _job, src in to_fetch]
        tmp = tempfile.mkdtemp(prefix="cinna_plugin_snapshot_")
        try:
            try:
                commit = git_operations.fetch_tree(
                    group.url,
                    tmp,
                    sha=target,
                    subdirs=None if "" in subdirs else subdirs,
                )
            except EgressBlockedError:
                for job, src in to_fetch:
                    cls._finish(factory, job, claim_id, {"status": "failed", "reason": "fetch_failed", "key": src.key}, started)
                return
            except git_operations.GitFetchTooLargeError:
                # The checkout outgrew the cap: a content verdict, not a retry.
                for job, src in to_fetch:
                    cls._finish(factory, job, claim_id, {"status": "failed", "reason": "too_large", "key": src.key, "commit": target}, started)
                return
            except Exception as exc:
                logger.info("plugin_snapshot fetch %s@%s failed: %s", group.url, target, exc)
                for job, _src in to_fetch:
                    cls._release(factory, job, claim_id)
                return
            for job, src in to_fetch:
                cls._snapshot_one(factory, job, src, claim_id, Path(tmp), commit, started)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @classmethod
    def _snapshot_one(
        cls,
        factory: SessionFactory,
        job: _Job,
        src: SnapshotSource,
        claim_id: uuid.UUID,
        checkout: Path,
        commit: str,
        started: float,
    ) -> None:
        staging = MarketplaceSkillSnapshots.new_staging_dir(job.marketplace_id)
        try:
            reason, components, runs_code, staged = _analyse_and_stage(
                checkout, src.subdir, (job.fields.get("config") or {}), job.name, job.fmt, staging
            )
            if reason is not None:
                outcome = {"status": "failed", "reason": reason, "key": src.key, "commit": commit}
            else:
                outcome = {
                    "status": "ready",
                    "key": src.key,
                    "commit": commit,
                    "staged": staged,
                    "components": components,
                    "runs_code": runs_code,
                }
            cls._finish(factory, job, claim_id, outcome, started)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            try:
                staging.parent.rmdir()
            except OSError:
                pass

    # -- writes ----------------------------------------------------------------

    @staticmethod
    def _locked_row(
        session: Session, job: _Job, claim_id: uuid.UUID
    ) -> LLMPluginMarketplacePlugin | None:
        """The row under ``FOR UPDATE`` when this claim still owns it (D3)."""
        row = session.exec(
            select(LLMPluginMarketplacePlugin)
            .where(LLMPluginMarketplacePlugin.id == job.plugin_id)
            .with_for_update()
        ).first()
        if (
            row is None
            or row.snapshot_claim_id != claim_id
            or row.snapshot_source_key != job.claimed_key
            or row.snapshot_status != "pending"
        ):
            return None
        return row

    @classmethod
    def _finish(
        cls,
        factory: SessionFactory,
        job: _Job,
        claim_id: uuid.UUID,
        outcome: dict,
        started: float | None = None,
    ) -> None:
        status = outcome["status"]
        staged: StagedArchive | None = outcome.get("staged")

        prune = False
        with factory() as session:
            row = cls._locked_row(session, job, claim_id)
            if row is None:
                session.rollback()
                logger.info("plugin_snapshot plugin_id=%s discarded (claim lost)", job.plugin_id)
                return
            row.snapshot_claim_id = None
            row.snapshot_lease_until = None
            row.snapshot_at = _utcnow()
            if "key" in outcome:
                row.snapshot_source_key = outcome["key"]
            if "commit" in outcome:
                row.snapshot_commit = outcome["commit"]
            if status == "unchanged":
                row.snapshot_status = "ready"
                row.snapshot_reason = None
            elif status == "ready":
                MarketplaceSkillSnapshots.promote(staged, job.marketplace_id, job.plugin_id)
                row.snapshot_status = "ready"
                row.snapshot_reason = None
                row.archive_sha256 = staged.sha256
                row.archive_size = staged.size
                row.components = outcome["components"]
                row.runs_code = outcome["runs_code"]
                prune = True
            else:
                row.snapshot_status = "failed"
                row.snapshot_reason = outcome["reason"]
                if outcome["reason"] != "fetch_failed":
                    # The content itself is not deliverable: nothing to serve.
                    row.archive_sha256 = None
                    row.archive_size = None
                    row.components = None
                    row.runs_code = None
                    prune = True
            session.add(row)
            session.commit()
        if prune:
            prune_to_current(factory, job.plugin_id)
        ms = int((time.monotonic() - started) * 1000) if started else 0
        logger.info(
            "plugin_snapshot plugin_id=%s status=%s reason=%s bytes=%s ms=%s",
            job.plugin_id,
            "ready" if status == "unchanged" else status,
            outcome.get("reason"),
            staged.size if staged else None,
            ms,
        )

    @classmethod
    def _release(cls, factory: SessionFactory, job: _Job, claim_id: uuid.UUID) -> None:
        """A transient failure: back to the queue after a short backoff."""
        try:
            with factory() as session:
                row = cls._locked_row(session, job, claim_id)
                if row is None:
                    session.rollback()
                    return
                row.snapshot_claim_id = None
                row.snapshot_lease_until = _utcnow() + timedelta(
                    seconds=30 * max(1, row.snapshot_attempts)
                )
                session.add(row)
                session.commit()
            logger.info(
                "plugin_snapshot plugin_id=%s status=pending reason=retry attempt=%s",
                job.plugin_id,
                job.attempts,
            )
        except Exception as exc:
            logger.error("Could not release plugin snapshot %s: %s", job.plugin_id, exc)


def prune_to_current(factory: SessionFactory, plugin_id: uuid.UUID) -> None:
    """Drop every archive of a plugin entry but the one its row names *now*.

    Runs under the row lock and re-reads the digest there, so a concurrent
    writer (which promotes its file while holding the same lock) can never
    have the file its committed row points at deleted. Skills-format rows keep
    their history and are never pruned. Best effort, never raises.
    """
    with factory() as session:
        try:
            row = session.exec(
                select(LLMPluginMarketplacePlugin)
                .where(LLMPluginMarketplacePlugin.id == plugin_id)
                .with_for_update()
            ).first()
            if row is not None and row.plugin_type != "skills":
                MarketplaceSkillSnapshots.prune_except(
                    row.marketplace_id, row.id, row.archive_sha256
                )
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.warning("Could not prune snapshots of plugin %s: %s", plugin_id, exc)


def start_scheduler() -> None:
    """Start the snapshot worker (app startup; never under TESTING)."""
    if settings.TESTING:
        return
    PluginSnapshotWorker.start()


def shutdown_scheduler() -> None:
    PluginSnapshotWorker.shutdown()
