"""LibraryEvents — bookkeeping, realtime hints and audit for the team library.

- :meth:`LibraryEvents.after_receive` runs as a Starlette ``BackgroundTask``
  after a ``git-receive-pack`` stream ends. receive-pack exits 0 even when the
  ``pre-receive`` hook refuses, so the ref on disk is the truth: when
  ``refs/heads/main`` now points at the SHA this push asked for, the row's ``head_sha`` / ``manifest_cache`` /
  ``last_pushed_by_user_id`` / ``updated_at`` follow, a ``LIBRARY_PUSH_ACCEPTED`` audit row is written and
  ``library.updated`` goes to every reader's ``user_{id}`` room.
- :meth:`LibraryEvents.granted` sends ``library.granted`` to users who just
  gained access.

Events are refresh hints only (Socket.IO has no cross-worker manager);
``GET /api/v1/library`` is the recovery path. They are never broadcast without
a room, which would reach guest and webapp sockets.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import update
from sqlmodel import Session, col
from starlette.concurrency import run_in_threadpool

from app.core.db import engine
from app.models.events.event import EventType
from app.models.events.security_event import LIBRARY_PUSH_ACCEPTED, SecurityEvent
from app.models.library.git_repo import GitRepo
from app.models.users.user import User
from app.services.events.event_service import event_service
from app.services.library.git_repo_storage import GitRepoStorage
from app.services.library.library_access_service import (
    LibraryAccessService,
    display_name,
)

logger = logging.getLogger(__name__)


def add_audit_event(
    session: Session,
    user_id: uuid.UUID,
    event_type: str,
    details: dict,
) -> None:
    """Stage a low-severity library ``SecurityEvent`` (caller commits)."""
    session.add(
        SecurityEvent(
            user_id=user_id,
            event_type=event_type,
            severity="low",
            details=json.dumps(details, default=str),
        )
    )


class _AcceptedPush:
    def __init__(self, head_sha: str, readers: set[uuid.UUID], pushed_by: dict) -> None:
        self.head_sha = head_sha
        self.readers = readers
        self.pushed_by = pushed_by


class LibraryEvents:
    """Static entry points; safe to call from routes and background tasks."""

    @staticmethod
    async def after_receive(
        repo_id: uuid.UUID,
        before_sha: str | None,
        pusher_id: uuid.UUID,
        pushed_new: str | None,
    ) -> None:
        """``pushed_new`` is the SHA this push asked ``refs/heads/main`` to
        become (parsed from the request); the push counts as accepted only
        when main now points there, so a refused racer is never credited."""
        try:
            accepted = await run_in_threadpool(
                LibraryEvents._record_push, repo_id, before_sha, pusher_id, pushed_new
            )
        except Exception:
            logger.exception("Library push bookkeeping failed for %s", repo_id)
            return
        if accepted is None:
            return
        meta = {
            "repo_id": str(repo_id),
            "head_sha": accepted.head_sha,
            "pushed_by": accepted.pushed_by,
        }
        await LibraryEvents._emit_each(EventType.LIBRARY_UPDATED, repo_id, accepted.readers, meta)

    @staticmethod
    async def granted(repo_id: uuid.UUID, user_ids: Iterable[uuid.UUID]) -> None:
        """``library.granted {repo_id}`` to each user who just gained access."""
        await LibraryEvents._emit_each(
            EventType.LIBRARY_GRANTED, repo_id, set(user_ids), {"repo_id": str(repo_id)}
        )

    @staticmethod
    async def _emit_each(
        event_type: str, repo_id: uuid.UUID, user_ids: set[uuid.UUID], meta: dict
    ) -> None:
        for user_id in sorted(user_ids, key=str):
            try:
                await event_service.emit_event(
                    event_type=event_type,
                    model_id=repo_id,
                    user_id=user_id,
                    meta=meta,
                )
            except Exception:
                logger.exception("Failed to emit %s to user %s", event_type, user_id)

    @staticmethod
    def _record_push(
        repo_id: uuid.UUID,
        before_sha: str | None,
        pusher_id: uuid.UUID,
        pushed_new: str | None,
    ) -> _AcceptedPush | None:
        new_sha = GitRepoStorage.head_sha(repo_id)
        if new_sha is None or new_sha == before_sha or not pushed_new or new_sha != pushed_new:
            return None  # refused, a no-op, or main moved by someone else's push
        manifest = GitRepoStorage.read_manifest_at(repo_id, new_sha)
        now = datetime.now(UTC)

        with Session(engine) as session:
            result = session.execute(
                update(GitRepo)
                .where(
                    col(GitRepo.id) == repo_id,
                    col(GitRepo.head_sha).is_not_distinct_from(before_sha),
                )
                .values(
                    head_sha=new_sha,
                    manifest_cache=manifest,
                    last_pushed_by_user_id=pusher_id,
                    updated_at=now,
                )
            )
            if result.rowcount == 0:
                # Lost a race with another push's bookkeeping, or an earlier
                # one never ran: the disk is the truth, so re-read and store it.
                repo = session.get(GitRepo, repo_id)
                if repo is None:
                    session.rollback()
                    return None
                current = GitRepoStorage.head_sha(repo_id)
                if current and current != repo.head_sha:
                    repo.head_sha = current
                    repo.manifest_cache = GitRepoStorage.read_manifest_at(repo_id, current)
                    if current == new_sha:
                        repo.last_pushed_by_user_id = pusher_id
                    repo.updated_at = now
                    session.add(repo)
                new_sha = current or new_sha

            repo = session.get(GitRepo, repo_id)
            if repo is None:
                session.rollback()
                return None
            pusher = session.get(User, pusher_id)
            pushed_by = {
                "user_id": str(pusher_id),
                "display_name": display_name(pusher) if pusher else None,
            }
            add_audit_event(
                session,
                pusher_id,
                LIBRARY_PUSH_ACCEPTED,
                {
                    "repo_id": str(repo_id),
                    "slug": repo.slug,
                    "before": before_sha,
                    "after": new_sha,
                },
            )
            readers = LibraryAccessService.readers(session, repo)
            session.commit()

        return _AcceptedPush(head_sha=new_sha, readers=readers, pushed_by=pushed_by)
