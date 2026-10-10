"""DesktopAgentRevisions — every accepted share of a desktop agent is a revision.

:meth:`DesktopAgentRevisions.after_receive` runs as a Starlette
``BackgroundTask`` after a ``git-receive-pack`` stream ends. receive-pack exits
0 even when the ``pre-receive`` hook refuses, so the ref on disk is the truth:
when ``refs/heads/main`` now points at the SHA this push asked for, in one
transaction

- the ``git_repo`` row's ``head_sha`` / ``manifest_cache`` /
  ``last_pushed_by_user_id`` / ``updated_at`` follow (compare-and-set on the
  previous head);
- an ``AgentBundleRevision(origin='share')`` is written for the new head: its
  release notes are the commit messages since the previous revision, its
  manifest / credential specs / skills summary are read from the pushed tree;
- the bundle's ``latest_revision_id`` moves and a
  ``CATALOG_DESKTOP_PUSH_ACCEPTED`` audit row is written.

After commit, ``catalog.updated`` goes to every user reached through the
bundle's grants (P9). A push whose bookkeeping lost the race to a later one
gets no revision of its own: the later revision's notes cover both.
"""
from __future__ import annotations

import json
import logging
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, update
from sqlmodel import Session, col, select
from starlette.concurrency import run_in_threadpool

from app.core.db import engine
from app.models.bundles.agent_bundle import AgentBundle
from app.models.bundles.agent_bundle_revision import (
    REVISION_ORIGIN_SHARE,
    AgentBundleRevision,
)
from app.models.events.security_event import CATALOG_DESKTOP_PUSH_ACCEPTED
from app.models.library.git_repo import GitRepo
from app.models.skills.skill_package import SkillPackage
from app.services.catalog.catalog_access_service import BUNDLE, CatalogAccessService
from app.services.catalog.catalog_events import CatalogEvents, add_audit_event
from app.services.library.git_repo_storage import MANIFEST_PATH, GitRepoStorage

logger = logging.getLogger(__name__)

#: What a share revision reads from the pushed tree.
_EXPORTED_PATHS = [MANIFEST_PATH, "skills"]


@dataclass
class _Recorded:
    bundle_uuid: uuid.UUID
    bundle_id: str
    revision_number: int
    head_sha: str
    readers: set[uuid.UUID]


class DesktopAgentRevisions:
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
            recorded = await run_in_threadpool(
                DesktopAgentRevisions._record_push, repo_id, before_sha, pusher_id, pushed_new
            )
        except Exception:
            logger.exception("Desktop agent push bookkeeping failed for repo %s", repo_id)
            return
        if recorded is None:
            return
        await CatalogEvents.updated(
            recorded.bundle_uuid,
            recorded.readers,
            {
                "kind": BUNDLE.wire_kind,
                "uuid": str(recorded.bundle_uuid),
                "id": recorded.bundle_id,
                "revision_number": recorded.revision_number,
                "head_sha": recorded.head_sha,
            },
        )

    @staticmethod
    def _record_push(
        repo_id: uuid.UUID,
        before_sha: str | None,
        pusher_id: uuid.UUID,
        pushed_new: str | None,
    ) -> _Recorded | None:
        new_sha = GitRepoStorage.head_sha(repo_id)
        if new_sha is None or new_sha == before_sha or not pushed_new or new_sha != pushed_new:
            return None  # refused, a no-op, or main moved by someone else's push
        manifest_cache = GitRepoStorage.read_manifest_at(repo_id, new_sha)
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
                    manifest_cache=manifest_cache,
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
                if current != new_sha:
                    # A later push moved main on; its bookkeeping writes the
                    # revision that covers this one too.
                    session.commit()
                    return None

            bundle = session.exec(
                select(AgentBundle)
                .where(AgentBundle.git_repo_id == repo_id)
                .with_for_update()
            ).first()
            if bundle is None:
                session.commit()
                return None
            prev = session.exec(
                select(AgentBundleRevision)
                .where(AgentBundleRevision.bundle_id == bundle.id)
                .order_by(col(AgentBundleRevision.revision_number).desc())
                .limit(1)
            ).first()
            if prev is not None and prev.git_commit_sha == new_sha:
                session.commit()
                return None  # already recorded

            revision = DesktopAgentRevisions._build_revision(
                session, repo_id, bundle, prev, new_sha, pusher_id, now
            )
            session.add(revision)
            session.flush()
            bundle.latest_revision_id = revision.id
            bundle.updated_at = now
            session.add(bundle)
            add_audit_event(
                session,
                pusher_id,
                CATALOG_DESKTOP_PUSH_ACCEPTED,
                {
                    "uuid": str(bundle.id),
                    "repo_id": str(repo_id),
                    "slug": bundle.slug,
                    "before": before_sha,
                    "after": new_sha,
                    "revision_number": revision.revision_number,
                },
            )
            readers = CatalogAccessService.readers(session, BUNDLE, bundle)
            recorded = _Recorded(
                bundle_uuid=bundle.id,
                bundle_id=bundle.bundle_id,
                revision_number=revision.revision_number,
                head_sha=new_sha,
                readers=readers,
            )
            session.commit()
        return recorded

    @staticmethod
    def _build_revision(
        session: Session,
        repo_id: uuid.UUID,
        bundle: AgentBundle,
        prev: AgentBundleRevision | None,
        new_sha: str,
        pusher_id: uuid.UUID,
        now: datetime,
    ) -> AgentBundleRevision:
        next_number = (
            session.exec(
                select(func.max(AgentBundleRevision.revision_number)).where(
                    AgentBundleRevision.bundle_id == bundle.id
                )
            ).one()
            or 0
        ) + 1
        release_notes = GitRepoStorage.commit_messages(
            repo_id, prev.git_commit_sha if prev else None, new_sha
        )
        with tempfile.TemporaryDirectory(prefix="cinna-share-") as tmp:
            tree = GitRepoStorage.export_paths(repo_id, new_sha, _EXPORTED_PATHS, Path(tmp))
            data = _read_manifest(tree / MANIFEST_PATH)
            skills_summary = _authored_skills(tree / "skills")
        skills_summary += _catalog_skills(session, data.get("catalog_skills"))
        description = data.get("description")
        return AgentBundleRevision(
            bundle_id=bundle.id,
            revision_number=next_number,
            origin=REVISION_ORIGIN_SHARE,
            git_commit_sha=new_sha,
            content_hash=GitRepoStorage.tree_id(repo_id, new_sha) or new_sha,
            snapshot_path=None,
            manifest=project_revision_manifest(data),
            required_credential_specs=_credential_specs(session, data.get("credentials")),
            skills_summary=skills_summary,
            description=description if isinstance(description, str) else None,
            published_by_user_id=pusher_id,
            published_at=now,
            release_notes=release_notes,
        )


# ── Projections of the pushed tree ─────────────────────────────────


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _read_manifest(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def project_revision_manifest(data: dict) -> dict:
    """The part of ``cinna-agent.json`` a share revision keeps:
    ``{id, name, slug, description, contract_version, runtime: {engine, model},
    credentials: [{name, type, service_uri}], catalog_skills: [...]}``. Only
    strings (and the pins' own JSON objects) are kept, so a hostile manifest
    cannot push arbitrary JSON into the catalog."""
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    credentials = [
        {
            "name": _text(c.get("name")),
            "type": _text(c.get("type")),
            "service_uri": _text(c.get("service_uri")),
        }
        for c in data.get("credentials") or []
        if isinstance(c, dict)
    ]
    pins = [p for p in data.get("catalog_skills") or [] if isinstance(p, dict)]
    return {
        "id": _text(data.get("id")),
        "name": _text(data.get("name")),
        "slug": _text(data.get("slug")),
        "description": _text(data.get("description")),
        "contract_version": _text(data.get("contract_version")),
        "runtime": {
            "engine": _text(runtime.get("engine")),
            "model": _text(runtime.get("model")),
        },
        "credentials": credentials,
        "catalog_skills": pins,
    }


def _credential_specs(session: Session, credentials: Any) -> list[dict]:
    """One ``user`` spec per manifest credential slot; a bad entry is logged
    and skipped."""
    from app.services.bundles.credential_spec import build_spec

    specs: list[dict] = []
    for entry in credentials or []:
        try:
            if not isinstance(entry, dict):
                raise ValueError("not an object")
            name, kind = _text(entry.get("name")), _text(entry.get("type"))
            if not name or not kind:
                raise ValueError("name and type are required")
            specs.append(
                build_spec(
                    session,
                    credential=None,
                    credential_type=kind,
                    provided_by="user",
                    name=name,
                    service_uri=_text(entry.get("service_uri")),
                    description=_text(entry.get("description")),
                )
            )
        except Exception as exc:  # noqa: BLE001 — one bad slot never blocks the revision
            logger.warning("Skipping credential slot %r of a desktop share: %s", entry, exc)
    return specs


def _authored_skills(skills_root: Path) -> list[dict]:
    from app.services.agents.skill_manifest import scan_skills_root

    if not skills_root.is_dir():
        return []
    return [
        {
            "name": entry.name,
            "description": entry.description,
            "has_scripts": entry.has_scripts,
            "source": "authored",
        }
        for entry in scan_skills_root(skills_root)
        if entry.is_valid
    ]


def _catalog_skills(session: Session, pins: Any) -> list[dict]:
    """``catalog_skills[]`` pins as summary entries, named from the catalog."""
    entries = [p for p in pins or [] if isinstance(p, dict)]
    uuids: set[uuid.UUID] = set()
    for pin in entries:
        try:
            uuids.add(uuid.UUID(str(pin.get("package_uuid"))))
        except ValueError:
            continue
    names: dict[uuid.UUID, str] = {}
    if uuids:
        names = {
            pkg_uuid: name
            for pkg_uuid, name in session.exec(
                select(SkillPackage.id, SkillPackage.name).where(col(SkillPackage.id).in_(uuids))
            ).all()
        }
    result: list[dict] = []
    for pin in entries:
        try:
            pkg_uuid: uuid.UUID | None = uuid.UUID(str(pin.get("package_uuid")))
        except ValueError:
            pkg_uuid = None
        result.append(
            {
                "name": (names.get(pkg_uuid) if pkg_uuid else None) or _text(pin.get("name")),
                "package_id": _text(pin.get("package_id")),
                "package_uuid": str(pkg_uuid) if pkg_uuid else None,
                "revision_number": pin.get("revision_number")
                if isinstance(pin.get("revision_number"), int)
                else None,
                "source": "catalog",
            }
        )
    return result
