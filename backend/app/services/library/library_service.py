"""LibraryService — the team library API (``/api/v1/library``).

Builds the README §4.2 wire shapes (``LibraryAgent``, ``LibraryListing``,
``LibraryGrantPublic``, ``LibraryAgentAdmin``) and runs create / grant
replacement / delete. Access rules live in :class:`LibraryAccessService`, bare
repos in :class:`GitRepoStorage`. Raises :mod:`app.services.library.exceptions`
errors; the route maps them to HTTP.

Visibility: a repo the caller cannot read is reported as not found (never
advertised), except to a superuser, who manages every repo.
"""
from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from app.core.config import settings
from app.models.bundles.agent_git_source import AgentGitSource
from app.models.events.security_event import (
    LIBRARY_AGENT_CREATED,
    LIBRARY_AGENT_DELETED,
    LIBRARY_GRANTS_CHANGED,
)
from app.models.library.git_repo import (
    EVERYONE_PRINCIPAL_ID,
    GitRepo,
    GitRepoGrant,
    GitRepoKind,
    LibraryAccess,
    LibraryAgent,
    LibraryAgentAdmin,
    LibraryAgentCreate,
    LibraryGrantIn,
    LibraryGrantPublic,
    LibraryListing,
    LibraryMaintainer,
    LibraryManifest,
    LibraryOwner,
    LibraryPrincipalKind,
    LibraryPrincipalRole,
    LibraryPrincipalUser,
)
from app.models.users.user import User
from app.services.library.exceptions import (
    LibraryAccessDeniedError,
    LibraryConflictError,
    LibraryNotFoundError,
    LibraryValidationError,
)
from app.services.library.git_repo_storage import GitRepoStorage
from app.services.library.library_access_service import (
    LibraryAccessService,
    display_name,
)
from app.services.library.library_events import add_audit_event

logger = logging.getLogger(__name__)

NOT_FOUND_DETAIL = "This agent isn't in the team library."
EVERYONE_DISPLAY_NAME = "Everyone"
PEOPLE_ONLY_MAINTAIN_DETAIL = "Only a person can be a maintainer."
_CLONE_URL_SUFFIX_RE = re.compile(
    r"/git/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:\.git)?/?$"
)
_PRINCIPAL_ORDER = {
    LibraryPrincipalKind.USER: 0,
    LibraryPrincipalKind.ROLE: 1,
    LibraryPrincipalKind.EVERYONE: 2,
}


class LibraryService:
    """All methods static and sync (routes run them in the threadpool).
    Mutations commit and return the users to notify; the route sends
    ``library.granted`` after the response via ``LibraryEvents.granted``."""

    # ── URLs ───────────────────────────────────────────────────────

    @staticmethod
    def clone_url(repo_id: uuid.UUID) -> str:
        return f"{settings.backend_base_url}/git/{repo_id}.git"

    @staticmethod
    def repo_id_in_url(url: str) -> uuid.UUID | None:
        """The library repo an ``AgentGitSource.repo_url`` points at, matched on
        the ``/git/<repo_id>[.git][/]`` suffix so a changed ``backend_base_url``
        (or an api./bare-domain origin) still matches."""
        match = _CLONE_URL_SUFFIX_RE.search(url or "")
        if not match:
            return None
        try:
            return uuid.UUID(match.group(1))
        except ValueError:
            return None

    # ── Reads ──────────────────────────────────────────────────────

    @staticmethod
    def list_library(session: Session, user: User) -> LibraryListing:
        pairs = LibraryAccessService.accessible_repos(session, user, kind=GitRepoKind.AGENT)
        repos = [repo for repo, _access in pairs]
        maintainers = LibraryService._maintainers_by_repo(session, repos)
        installs = LibraryService._cloud_installs(session, user, repos)
        agents = [
            LibraryService._to_agent(repo, access, maintainers.get(repo.id, []), installs.get(repo.id))
            for repo, access in pairs
        ]
        return LibraryListing(agents=agents, mirrors=[])

    @staticmethod
    def get_agent(session: Session, user: User, repo_id: uuid.UUID) -> LibraryAgent:
        repo = LibraryService._get_agent_repo(session, repo_id)
        access = LibraryAccessService.effective_access(session, repo, user)
        if access is None:
            raise LibraryNotFoundError(NOT_FOUND_DETAIL)
        maintainers = LibraryService._maintainers_by_repo(session, [repo])
        installs = LibraryService._cloud_installs(session, user, [repo])
        return LibraryService._to_agent(
            repo, access, maintainers.get(repo.id, []), installs.get(repo.id)
        )

    @staticmethod
    def list_grants(session: Session, user: User, repo_id: uuid.UUID) -> list[LibraryGrantPublic]:
        repo = LibraryService._get_managed_repo(session, user, repo_id)
        return LibraryService._grants_by_repo(session, [repo]).get(repo.id, [])

    @staticmethod
    def list_admin_agents(session: Session) -> list[LibraryAgentAdmin]:
        repos = list(
            session.exec(
                select(GitRepo)
                .where(GitRepo.kind == GitRepoKind.AGENT)
                .order_by(func.lower(GitRepo.name), col(GitRepo.id))
            ).all()
        )
        maintainers = LibraryService._maintainers_by_repo(session, repos)
        grants = LibraryService._grants_by_repo(session, repos)
        owner_ids = {repo.owner_user_id for repo in repos if repo.owner_user_id}
        owners = LibraryService._users_by_id(session, owner_ids)
        result: list[LibraryAgentAdmin] = []
        for repo in repos:
            owner = owners.get(repo.owner_user_id) if repo.owner_user_id else None
            result.append(
                LibraryAgentAdmin(
                    **LibraryService._agent_fields(repo, maintainers.get(repo.id, [])),
                    owner=LibraryOwner(user_id=owner.id, display_name=display_name(owner))
                    if owner
                    else None,
                    grants=grants.get(repo.id, []),
                )
            )
        return result

    # ── Mutations ──────────────────────────────────────────────────

    @staticmethod
    def create_agent(
        session: Session, user: User, data: LibraryAgentCreate
    ) -> tuple[LibraryAgent, set[uuid.UUID]]:
        """New agent repo: row → bare repo → grants (creator ``maintain``,
        ``everyone: read``) → commit. Returns the agent and every other reader
        (to receive ``library.granted``)."""
        if LibraryService._slug_taken(session, data.slug):
            raise LibraryConflictError(LibraryService._duplicate_detail(data.slug))

        repo = GitRepo(
            kind=GitRepoKind.AGENT,
            slug=data.slug,
            name=data.name.strip(),
            description=data.description,
            owner_user_id=user.id,
        )
        session.add(repo)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise LibraryConflictError(LibraryService._duplicate_detail(data.slug)) from exc

        try:
            GitRepoStorage.init_bare(repo.id, GitRepoKind.AGENT)
        except Exception:
            session.rollback()
            raise

        session.add(
            GitRepoGrant(
                repo_id=repo.id,
                principal_kind=LibraryPrincipalKind.USER,
                principal_id=str(user.id),
                user_id=user.id,
                access=LibraryAccess.MAINTAIN,
                granted_by_user_id=user.id,
            )
        )
        session.add(
            GitRepoGrant(
                repo_id=repo.id,
                principal_kind=LibraryPrincipalKind.EVERYONE,
                principal_id=EVERYONE_PRINCIPAL_ID,
                access=LibraryAccess.READ,
                granted_by_user_id=user.id,
            )
        )
        add_audit_event(
            session,
            user.id,
            LIBRARY_AGENT_CREATED,
            {"repo_id": str(repo.id), "slug": repo.slug},
        )
        repo_id = repo.id
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            GitRepoStorage.remove(repo_id)
            if isinstance(exc, IntegrityError):
                raise LibraryConflictError(LibraryService._duplicate_detail(data.slug)) from exc
            raise
        session.refresh(repo)

        readers = LibraryAccessService.readers(session, repo) - {user.id}
        return LibraryService.get_agent(session, user, repo.id), readers

    @staticmethod
    def replace_grants(
        session: Session,
        user: User,
        repo_id: uuid.UUID,
        grants: list[LibraryGrantIn],
    ) -> tuple[list[LibraryGrantPublic], set[uuid.UUID]]:
        """Full replace of one agent's grants (maintainer or superuser).
        Returns the new grants and the users who just gained access."""
        repo = LibraryService._get_managed_repo(session, user, repo_id)
        for grant in grants:
            if grant.access == LibraryAccess.MAINTAIN and not isinstance(
                grant.principal, LibraryPrincipalUser
            ):
                raise LibraryValidationError(PEOPLE_ONLY_MAINTAIN_DETAIL)

        before, after = LibraryAccessService.replace_grants(session, repo, grants, user)
        add_audit_event(
            session,
            user.id,
            LIBRARY_GRANTS_CHANGED,
            {
                "repo_id": str(repo.id),
                "slug": repo.slug,
                "grants": [
                    {"principal": _principal_label(g), "access": g.access} for g in grants
                ],
            },
        )
        session.commit()

        return LibraryService._grants_by_repo(session, [repo]).get(repo.id, []), after - before

    @staticmethod
    def delete_agent(session: Session, user: User, repo_id: uuid.UUID) -> None:
        """Remove an agent; refused while another person has it in Cinna."""
        repo = LibraryService._get_managed_repo(session, user, repo_id)
        candidates = session.exec(
            select(AgentGitSource.owner_id, AgentGitSource.repo_url).where(
                col(AgentGitSource.repo_url).ilike(f"%/git/{repo.id}%"),
                AgentGitSource.owner_id != user.id,
            )
        ).all()
        holders = {
            owner_id
            for owner_id, repo_url in candidates
            if LibraryService.repo_id_in_url(repo_url) == repo.id
        }
        if holders:
            raise LibraryConflictError(_holders_detail(len(holders)))

        repo_id = repo.id
        add_audit_event(
            session,
            user.id,
            LIBRARY_AGENT_DELETED,
            {"repo_id": str(repo_id), "slug": repo.slug},
        )
        session.delete(repo)  # grants cascade in the database
        session.commit()
        try:
            GitRepoStorage.remove(repo_id)
        except Exception:
            # The row is gone, so the repo is unreachable; the operator can
            # purge the directory. Do not fail the request over it.
            logger.exception("Failed to move library repo %s to trash", repo_id)

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _get_agent_repo(session: Session, repo_id: uuid.UUID) -> GitRepo:
        repo = session.get(GitRepo, repo_id)
        if repo is None or repo.kind != GitRepoKind.AGENT:
            raise LibraryNotFoundError(NOT_FOUND_DETAIL)
        return repo

    @staticmethod
    def _get_managed_repo(session: Session, user: User, repo_id: uuid.UUID) -> GitRepo:
        """The repo when ``user`` may manage it; 404 when they can't even read it."""
        repo = LibraryService._get_agent_repo(session, repo_id)
        access = LibraryAccessService.effective_access(session, repo, user)
        if LibraryAccessService.can_manage(repo, user, access):
            return repo
        if access is None:
            raise LibraryNotFoundError(NOT_FOUND_DETAIL)
        raise LibraryAccessDeniedError("Only a maintainer of this agent can do that.")

    @staticmethod
    def _slug_taken(session: Session, slug: str) -> bool:
        return (
            session.exec(
                select(GitRepo.id).where(GitRepo.kind == GitRepoKind.AGENT, GitRepo.slug == slug)
            ).first()
            is not None
        )

    @staticmethod
    def _duplicate_detail(slug: str) -> str:
        return f"An agent called {slug} is already in the team library."

    @staticmethod
    def _agent_fields(repo: GitRepo, maintainers: list[LibraryMaintainer]) -> dict:
        manifest = (
            LibraryManifest.model_validate(repo.manifest_cache) if repo.manifest_cache else None
        )
        return {
            "repo_id": repo.id,
            "slug": repo.slug,
            "name": repo.name,
            "description": repo.description,
            "clone_url": LibraryService.clone_url(repo.id),
            "default_branch": repo.default_branch,
            "head_sha": repo.head_sha,
            "updated_at": repo.updated_at,
            "maintainers": maintainers,
            "manifest": manifest,
        }

    @staticmethod
    def _to_agent(
        repo: GitRepo,
        access: str,
        maintainers: list[LibraryMaintainer],
        cloud_install_id: uuid.UUID | None,
    ) -> LibraryAgent:
        return LibraryAgent(
            **LibraryService._agent_fields(repo, maintainers),
            access=access,
            cloud_install_id=cloud_install_id,
        )

    @staticmethod
    def _users_by_id(session: Session, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, User]:
        if not user_ids:
            return {}
        users = session.exec(select(User).where(col(User.id).in_(user_ids))).all()
        return {u.id: u for u in users}

    @staticmethod
    def _maintainers_by_repo(
        session: Session, repos: list[GitRepo]
    ) -> dict[uuid.UUID, list[LibraryMaintainer]]:
        """Direct ``maintain`` user grants for many repos in one query."""
        if not repos:
            return {}
        rows = session.exec(
            select(GitRepoGrant.repo_id, User)
            .join(User, col(User.id) == col(GitRepoGrant.user_id))
            .where(
                col(GitRepoGrant.repo_id).in_([r.id for r in repos]),
                GitRepoGrant.principal_kind == LibraryPrincipalKind.USER,
                GitRepoGrant.access == LibraryAccess.MAINTAIN,
            )
        ).all()
        result: dict[uuid.UUID, list[LibraryMaintainer]] = defaultdict(list)
        for repo_id, person in rows:
            result[repo_id].append(
                LibraryMaintainer(user_id=person.id, display_name=display_name(person))
            )
        for maintainers in result.values():
            maintainers.sort(key=lambda m: m.display_name.lower())
        return dict(result)

    @staticmethod
    def _grants_by_repo(
        session: Session, repos: list[GitRepo]
    ) -> dict[uuid.UUID, list[LibraryGrantPublic]]:
        """Every grant of many repos as ``LibraryGrantPublic`` (two queries)."""
        if not repos:
            return {}
        rows = session.exec(
            select(GitRepoGrant).where(col(GitRepoGrant.repo_id).in_([r.id for r in repos]))
        ).all()
        people = LibraryService._users_by_id(
            session,
            {g.user_id for g in rows if g.user_id} | {g.granted_by_user_id for g in rows if g.granted_by_user_id},
        )
        result: dict[uuid.UUID, list[LibraryGrantPublic]] = defaultdict(list)
        for grant in rows:
            principal, name = _principal_and_name(grant, people)
            granted_by = people.get(grant.granted_by_user_id) if grant.granted_by_user_id else None
            result[grant.repo_id].append(
                LibraryGrantPublic(
                    principal=principal,
                    access=grant.access,
                    display_name=name,
                    granted_by=display_name(granted_by) if granted_by else None,
                    created_at=grant.created_at,
                )
            )
        for grants in result.values():
            grants.sort(
                key=lambda g: (
                    -LibraryAccess.rank(g.access),
                    _PRINCIPAL_ORDER.get(_kind_of(g.principal), 9),
                    g.display_name.lower(),
                )
            )
        return dict(result)

    @staticmethod
    def _cloud_installs(
        session: Session, user: User, repos: list[GitRepo]
    ) -> dict[uuid.UUID, uuid.UUID]:
        """The caller's own cloud agent bound to each repo (one query)."""
        if not repos:
            return {}
        wanted = {repo.id for repo in repos}
        rows = session.exec(
            select(AgentGitSource.repo_url, AgentGitSource.agent_id).where(
                AgentGitSource.owner_id == user.id,
                col(AgentGitSource.repo_url).like("%/git/%"),
            )
        ).all()
        result: dict[uuid.UUID, uuid.UUID] = {}
        for repo_url, agent_id in rows:
            repo_id = LibraryService.repo_id_in_url(repo_url)
            if repo_id in wanted:
                result.setdefault(repo_id, agent_id)
        return result


def _kind_of(principal: object) -> str:
    if isinstance(principal, LibraryPrincipalUser):
        return LibraryPrincipalKind.USER
    if isinstance(principal, LibraryPrincipalRole):
        return LibraryPrincipalKind.ROLE
    return LibraryPrincipalKind.EVERYONE


def _principal_and_name(
    grant: GitRepoGrant, people: dict[uuid.UUID, User]
) -> tuple[LibraryPrincipalUser | LibraryPrincipalRole | str, str]:
    if grant.principal_kind == LibraryPrincipalKind.USER and grant.user_id:
        person = people.get(grant.user_id)
        name = display_name(person) if person else str(grant.user_id)
        return LibraryPrincipalUser(user_id=grant.user_id), name
    if grant.principal_kind == LibraryPrincipalKind.ROLE:
        return LibraryPrincipalRole(role=grant.principal_id), grant.principal_id
    return LibraryPrincipalKind.EVERYONE, EVERYONE_DISPLAY_NAME


def _principal_label(grant: LibraryGrantIn) -> str:
    if isinstance(grant.principal, LibraryPrincipalUser):
        return f"user:{grant.principal.user_id}"
    if isinstance(grant.principal, LibraryPrincipalRole):
        return f"role:{grant.principal.role}"
    return LibraryPrincipalKind.EVERYONE


def _holders_detail(count: int) -> str:
    if count == 1:
        return "1 person uses this agent in Cinna, so it can't be removed until they remove it."
    return f"{count} people use this agent in Cinna, so it can't be removed until they remove it."
