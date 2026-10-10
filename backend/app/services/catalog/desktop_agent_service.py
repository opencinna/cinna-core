"""DesktopAgentService — create and remove desktop agents.

A desktop agent is an ``AgentBundle(runs_on='desktop')`` backed 1:1 by a bare
``git_repo``: Cinna Desktop shares it by git push (each accepted push becomes a
revision, see :mod:`app.services.catalog.desktop_agent_revisions`) and gets it
by git clone. Any active signed-in user may create one (U5); the creator holds
``maintain``.

The catalog id (``bundle_id``) is minted here as ``<reversed host>.<8 hex of
the manifest id>`` and never changes; the manifest id itself is bound at create
and enforced by the push policy.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle, BundleRunsOn
from app.models.bundles.agent_git_source import AgentGitSource
from app.models.catalog.catalog_access import (
    CatalogAccess,
    CatalogGrantIn,
    CatalogPrincipalUser,
)
from app.models.catalog.catalog_item import DesktopAgentCreate
from app.models.events.security_event import CATALOG_DESKTOP_AGENT_CREATED
from app.models.library.git_repo import GitRepo, GitRepoKind
from app.models.users.user import User
from app.services.bundles.bundle_id_service import BundleIdService
from app.services.catalog.catalog_access_service import BUNDLE, CatalogAccessService
from app.services.catalog.catalog_events import add_audit_event
from app.services.catalog.exceptions import CatalogConflictError
from app.services.library.git_repo_storage import GitRepoStorage

logger = logging.getLogger(__name__)

MANIFEST_TAKEN_DETAIL = "This agent is already in the catalog. Share changes to it instead."


def slug_taken_detail(slug: str) -> str:
    return f"An agent called {slug} is already in the catalog."


class DesktopAgentService:
    """All methods static and sync (routes run them in the threadpool)."""

    @staticmethod
    def create(
        session: Session, user: User, body: DesktopAgentCreate
    ) -> tuple[AgentBundle, set[uuid.UUID]]:
        """Repo row → bundle row → bare repo → grants → audit → commit, all or
        nothing. Returns the bundle and every other user it reaches through
        its grants (to receive ``catalog.granted``)."""
        if DesktopAgentService._slug_taken(session, body.slug):
            raise CatalogConflictError(slug_taken_detail(body.slug))
        if DesktopAgentService._manifest_taken(session, body.manifest_id):
            raise CatalogConflictError(MANIFEST_TAKEN_DETAIL)

        repo = GitRepo(kind=GitRepoKind.AGENT)
        bundle = AgentBundle(
            bundle_id=DesktopAgentService._mint_bundle_id(session, body.manifest_id),
            runs_on=BundleRunsOn.DESKTOP,
            publisher_user_id=user.id,
            is_listed=True,
            visibility=body.visibility,
            display_name=body.display_name.strip(),
            description=body.description,
            slug=body.slug,
            manifest_id=body.manifest_id,
            git_repo_id=repo.id,
        )
        grants = [
            CatalogGrantIn(
                principal=CatalogPrincipalUser(user_id=user.id), access=CatalogAccess.MAINTAIN
            ),
            *[
                g
                for g in body.grants
                if not (
                    isinstance(g.principal, CatalogPrincipalUser)
                    and g.principal.user_id == user.id
                )
            ],
        ]
        # Validated before anything is written, so a refusal leaves no row or repo.
        grant_rows = CatalogAccessService.validate_grants(session, BUNDLE, bundle, grants, user)

        session.add(repo)
        session.flush()
        session.add(bundle)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise DesktopAgentService._conflict(session, body) from exc

        try:
            GitRepoStorage.init_bare(repo.id, GitRepoKind.AGENT)
        except Exception:
            session.rollback()
            raise

        session.add_all(grant_rows)
        add_audit_event(
            session,
            user.id,
            CATALOG_DESKTOP_AGENT_CREATED,
            {
                "uuid": str(bundle.id),
                "bundle_id": bundle.bundle_id,
                "repo_id": str(repo.id),
                "slug": body.slug,
                "visibility": body.visibility,
                "grants": [
                    {"principal": _principal_label(g), "access": g.access} for g in grants
                ],
            },
        )
        repo_id = repo.id
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            GitRepoStorage.remove(repo_id)
            if isinstance(exc, IntegrityError):
                raise DesktopAgentService._conflict(session, body) from exc
            raise
        session.refresh(bundle)
        readers = CatalogAccessService.readers(session, BUNDLE, bundle) - {user.id}
        return bundle, readers

    @staticmethod
    def detach_publisher(session: Session, user_id: uuid.UUID) -> None:
        """Before a user is deleted: their desktop agents lose their publisher
        (``publisher_user_id`` is RESTRICT) and stay in the catalog; their
        grants cascade with the user and superusers manage the agent (P2).
        Flushes, never commits — the caller's delete does."""
        session.execute(
            update(AgentBundle)
            .where(
                col(AgentBundle.publisher_user_id) == user_id,
                col(AgentBundle.runs_on) == BundleRunsOn.DESKTOP,
            )
            .values(publisher_user_id=None)
        )

    @staticmethod
    def other_holders(session: Session, bundle: AgentBundle, user: User) -> set[uuid.UUID]:
        """People other than ``user`` who hold a cloud agent bound to this
        desktop agent: an ``AgentGitSource`` on its clone URL (made before
        cloud checkouts of desktop agents were refused) or an install row."""
        holders: set[uuid.UUID] = set()
        if bundle.git_repo_id is not None:
            candidates = session.exec(
                select(AgentGitSource.owner_id, AgentGitSource.repo_url).where(
                    col(AgentGitSource.repo_url).ilike(f"%/git/{bundle.git_repo_id}%"),
                    AgentGitSource.owner_id != user.id,
                )
            ).all()
            holders |= {
                owner_id
                for owner_id, repo_url in candidates
                if GitRepoStorage.repo_id_in_url(repo_url) == bundle.git_repo_id
            }
        holders |= set(
            session.exec(
                select(Agent.owner_id).where(
                    Agent.bundle_uuid == bundle.id, Agent.owner_id != user.id
                )
            ).all()
        )
        return holders

    @staticmethod
    def delete(session: Session, bundle: AgentBundle) -> None:
        """Delete the bundle (revisions and grants cascade), then its repo row,
        and commit; the bare repo moves to trash best effort. The caller has
        checked the guard and staged the audit row."""
        repo_id = bundle.git_repo_id
        bundle.latest_revision_id = None
        session.add(bundle)
        session.flush()
        session.delete(bundle)
        session.flush()
        if repo_id is not None:
            repo = session.get(GitRepo, repo_id)
            if repo is not None:
                session.delete(repo)
        session.commit()
        if repo_id is None:
            return
        try:
            GitRepoStorage.remove(repo_id)
        except Exception:
            # The rows are gone, so the repo is unreachable; the operator can
            # purge the directory. Do not fail the request over it.
            logger.exception("Failed to move desktop agent repo %s to trash", repo_id)

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _slug_taken(session: Session, slug: str) -> bool:
        return (
            session.exec(
                select(AgentBundle.id).where(
                    AgentBundle.runs_on == BundleRunsOn.DESKTOP, AgentBundle.slug == slug
                )
            ).first()
            is not None
        )

    @staticmethod
    def _manifest_taken(session: Session, manifest_id: uuid.UUID) -> bool:
        return (
            session.exec(
                select(AgentBundle.id).where(
                    AgentBundle.runs_on == BundleRunsOn.DESKTOP,
                    AgentBundle.manifest_id == manifest_id,
                )
            ).first()
            is not None
        )

    @staticmethod
    def _mint_bundle_id(session: Session, manifest_id: uuid.UUID) -> str:
        prefix = BundleIdService.reversed_host_prefix()
        base = f"{prefix}.{manifest_id.hex[:8]}"
        # ``Agent.bundle_id`` too: every cloud agent carries one before it is
        # ever published, and a later publish would collide with ours.
        taken = set(
            session.exec(
                select(AgentBundle.bundle_id).where(
                    col(AgentBundle.bundle_id).startswith(base, autoescape=True)
                )
            ).all()
        ) | set(
            session.exec(
                select(Agent.bundle_id).where(
                    col(Agent.bundle_id).startswith(base, autoescape=True)
                )
            ).all()
        )
        return BundleIdService.desktop_bundle_id(prefix, manifest_id, taken)

    @staticmethod
    def _conflict(session: Session, body: DesktopAgentCreate) -> CatalogConflictError:
        """The 409 for a lost race at insert: name the slug when it is taken."""
        if DesktopAgentService._slug_taken(session, body.slug):
            return CatalogConflictError(slug_taken_detail(body.slug))
        if DesktopAgentService._manifest_taken(session, body.manifest_id):
            return CatalogConflictError(MANIFEST_TAKEN_DETAIL)
        return CatalogConflictError("This agent was added at the same time; try again.")


def _principal_label(grant: CatalogGrantIn) -> str:
    if isinstance(grant.principal, CatalogPrincipalUser):
        return f"user:{grant.principal.user_id}"
    return f"role:{grant.principal.role}"
