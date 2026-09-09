"""Skills catalog API routes.

Two routers, one tag (so the generated client has a single ``SkillsService``):

* ``router`` — ``/skills/...``: the catalog itself. Browse, read a package,
  manage one you published, preview a revision's ``SKILL.md``, and the
  container-facing archive download.
* ``agent_router`` — ``/agents/{agent_id}/skills/...``: the two verbs that act
  on an agent rather than on the catalog — publish one of its skills, and add a
  catalog skill to it. They live here, with the catalog service, rather than in
  ``agent_skills.py`` (which is the Phase-2 read surface over the environment
  cache) because everything they touch is catalog state.

Errors come out of the service as :class:`SkillCatalogError` with a stable
code and are turned into responses by ``http_error_for``, which lives beside
the code → status map in ``app.services.skills.exceptions`` — shared with the
plugins router, so the same refusal cannot answer 400 on one route and 422 on
another.
"""
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from app.api.deps import AgentEnvContextDep, CurrentUser, SessionDep
from app.models import (
    Agent,
    PluginSyncResponse,
    SkillInstallRequest,
    SkillPackageAccessGrantCreate,
    SkillPackageAccessGrantPublic,
    SkillPackageAccessGrantsPublic,
    SkillPackageDetailPublic,
    SkillPackageEntry,
    SkillPackageRevisionPublic,
    SkillPackagesPublic,
    SkillPackageUpdate,
    SkillPublishPreview,
    SkillPublishRequest,
    SkillRevisionContentPublic,
)
from app.models.skills.skill_package import SkillPackage
from app.services.plugins.llm_plugin_service import LLMPluginService
from app.services.skills.exceptions import SkillCatalogError, http_error_for
from app.services.skills.skill_catalog_service import SkillCatalogService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])
agent_router = APIRouter(prefix="/agents", tags=["skills"])


# =============================================================================
# Catalog
# =============================================================================


@router.get("/catalog", response_model=SkillPackagesPublic)
def list_skill_catalog(session: SessionDep, current_user: CurrentUser) -> Any:
    """List every skill package the caller may see.

    Unfiltered on purpose: the four catalog filters (all / public / mine /
    installed) are answerable from the fields on each entry, and the bundle
    catalog sets the precedent of filtering client-side over one fetch.
    """
    entries = SkillCatalogService.list_catalog(session, current_user)
    return SkillPackagesPublic(data=entries, count=len(entries))


@router.get("/packages/{package_id}", response_model=SkillPackageDetailPublic)
def get_skill_package(
    package_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    """One package with its full revision history, newest revision first."""
    try:
        package = SkillCatalogService.get_package(session, package_id, current_user)
    except SkillCatalogError as exc:
        raise http_error_for(exc)
    return SkillCatalogService.package_to_detail(session, package, current_user)


@router.patch("/packages/{package_id}", response_model=SkillPackageEntry)
def update_skill_package(
    package_id: uuid.UUID,
    data: SkillPackageUpdate,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Rename, re-describe, publish or hide a package. Publisher only."""
    try:
        package = SkillCatalogService.get_package(session, package_id, current_user)
        package = SkillCatalogService.update_package(
            session, package, current_user, data
        )
    except SkillCatalogError as exc:
        raise http_error_for(exc)
    return SkillCatalogService.package_to_entry(session, package, current_user)


@router.post("/packages/{package_id}/delist", response_model=SkillPackageEntry)
def delist_skill_package(
    package_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    """Hide a package from the catalog. Superuser only; never a delete.

    Installs that already point at one of its revisions keep working — which is
    exactly why an administrator gets this verb and not a delete button.

    Loaded through ``_load_package``, **without** a visibility check, and
    authorised by ``delist`` itself. Going through
    ``SkillCatalogService.get_package`` would make the one lever an
    administrator has over a harmful package depend on that package being
    visible to them — a ``users`` package is an explicit allowlist an admin is
    not on, so a publisher could put a workspace-exfiltrating skill in front of
    two hundred named colleagues and it could never be pulled from
    circulation. Widening ``user_can_see`` instead would fix this route by
    leaking every ``users`` package into every other superuser read path, which
    is the opposite of what the allowlist is for.

    The role is therefore checked **before** the load, not only inside
    ``delist``: an unconditional load that refused afterwards would answer 403
    for a package that exists and 404 for one that does not, handing any
    authenticated caller an existence oracle over every private package on the
    instance. Refusing first makes a non-administrator's answer identical
    either way, and leaves the 404 for the one caller entitled to tell the
    difference. ``delist`` re-checks the role, so the route cannot be the only
    thing standing between a caller and the verb.
    """
    if not current_user.is_superuser:
        raise http_error_for(
            SkillCatalogError(
                "not_superuser", "Only an administrator can delist a package."
            )
        )
    package = _load_package(session, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Skill package not found")
    try:
        package = SkillCatalogService.delist(session, package, current_user)
    except SkillCatalogError as exc:
        raise http_error_for(exc)
    return SkillCatalogService.package_to_entry(session, package, current_user)


@router.get(
    "/packages/{package_id}/revisions/{revision_number}/content",
    response_model=SkillRevisionContentPublic,
)
def get_skill_package_revision_content(
    package_id: uuid.UUID,
    revision_number: int,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """The published ``SKILL.md`` of one revision — the catalog preview."""
    try:
        package = SkillCatalogService.get_package(session, package_id, current_user)
        revision = SkillCatalogService.get_revision(
            session, package, revision_number
        )
        content, truncated = SkillCatalogService.read_revision_content(revision)
    except SkillCatalogError as exc:
        raise http_error_for(exc)
    return SkillRevisionContentPublic(
        package_id=package.id,
        revision_number=revision.revision_number,
        name=package.name,
        content=content,
        truncated=truncated,
    )


@router.get(
    "/packages/{package_id}/revisions/{revision_number}/archive",
    response_class=Response,
)
def download_skill_package_archive(
    package_id: uuid.UUID,
    revision_number: int,
    session: SessionDep,
    env_context: AgentEnvContextDep,
) -> Response:
    """Serve a revision's tarball to an agent container.

    Authenticated by the scoped env token (``AgentEnvContextDep``) and
    authorised by one question only: does the calling environment's agent hold
    a catalog link for **this** revision? Package visibility deliberately does
    not enter into it — a publisher who flips a package to private must not
    break the agents that already installed it, and an environment can only
    ever ask for a revision its own manifest named.

    ``X-Content-SHA256`` carries the digest the container re-computes before
    extraction; it is the same digest the manifest entry carried, so a
    mismatch means the bytes changed in flight.
    """
    package = _load_package(session, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Skill package not found")

    try:
        revision = SkillCatalogService.get_revision(
            session, package, revision_number
        )
    except SkillCatalogError as exc:
        raise http_error_for(exc)

    if not SkillCatalogService.env_may_download(
        session, agent_id=env_context.agent.id, revision=revision
    ):
        # 403, not 404: the environment authenticated fine, it simply has no
        # install of this revision. Hiding that would send a real
        # misconfiguration down the "package missing" path.
        raise HTTPException(
            status_code=403,
            detail="This environment has no install of that skill revision",
        )

    try:
        data, sha256 = SkillCatalogService.build_archive(revision)
    except SkillCatalogError as exc:
        raise http_error_for(exc)

    logger.info(
        "skill_archive_download env_id=%s package_id=%s revision=%s bytes=%s",
        env_context.environment.id, package.package_id, revision_number, len(data),
    )
    return Response(
        content=data,
        media_type="application/gzip",
        headers={
            "X-Content-SHA256": sha256,
            "Content-Disposition": (
                f'attachment; filename="{package.name}-{revision_number}.tar.gz"'
            ),
        },
    )


# =============================================================================
# Access grants (``visibility='users'``)
# =============================================================================


def _get_managed_package(
    session, package_id: uuid.UUID, user
) -> SkillPackage:
    """Load a package the caller publishes, or raise the coded refusal.

    Two gates in the established order: a package the caller cannot see is a
    404 (its existence is not public information), and one they can see but do
    not publish is a 403. An administrator gets no bypass here — their power
    over a package is :func:`delist_skill_package`, and "hide something harmful"
    must never widen into "hand somebody else's skill to a third party".
    """
    package = SkillCatalogService.get_package(session, package_id, user)
    if not SkillCatalogService.user_can_manage(package, user):
        raise SkillCatalogError(
            "not_publisher",
            "Only the publisher of a skill can manage who it is shared with.",
        )
    return package


@router.get(
    "/packages/{package_id}/grants",
    response_model=SkillPackageAccessGrantsPublic,
)
def list_skill_package_grants(
    package_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    """Who this package is shared with. Publisher only."""
    try:
        package = _get_managed_package(session, package_id, current_user)
    except SkillCatalogError as exc:
        raise http_error_for(exc)
    grants = SkillCatalogService.list_grants_public(session, package)
    return SkillPackageAccessGrantsPublic(data=grants, count=len(grants))


@router.post(
    "/packages/{package_id}/grants",
    response_model=SkillPackageAccessGrantPublic,
)
def add_skill_package_grant(
    package_id: uuid.UUID,
    data: SkillPackageAccessGrantCreate,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Share the package with one more person, by email.

    Idempotent: re-adding somebody who already has access returns their grant
    rather than refusing, because the publisher's intent is already true.
    """
    try:
        package = _get_managed_package(session, package_id, current_user)
        grant = SkillCatalogService.grant_access(
            session, package, data.email, current_user
        )
    except SkillCatalogError as exc:
        raise http_error_for(exc)
    logger.info(
        "skill_package_grant_added package_id=%s user_id=%s by=%s",
        package.package_id, grant.user_id, current_user.id,
    )
    return SkillCatalogService.grant_to_public(session, grant)


@router.delete(
    "/packages/{package_id}/grants/{user_id}",
    status_code=204,
    response_class=Response,
)
def revoke_skill_package_grant(
    package_id: uuid.UUID,
    user_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Response:
    """Stop sharing the package with one person.

    Keyed on the user, not on the grant id — "remove this person" is the verb
    the publisher has in mind. This hides the skill from their catalog; agents
    they already installed it into keep working, because the container's
    archive download is authorised by the install, never by visibility.
    """
    try:
        package = _get_managed_package(session, package_id, current_user)
        SkillCatalogService.revoke_grant(session, package, user_id)
    except SkillCatalogError as exc:
        raise http_error_for(exc)
    logger.info(
        "skill_package_grant_revoked package_id=%s user_id=%s by=%s",
        package.package_id, user_id, current_user.id,
    )
    return Response(status_code=204)


def _load_package(session, package_id: uuid.UUID) -> SkillPackage | None:
    """Load a package without a visibility check.

    For the two routes whose authorisation is not "may this user see it":

    * the archive route, authorised by the calling environment's install;
    * :func:`delist_skill_package`, authorised by the administrator role.

    Every other route goes through ``SkillCatalogService.get_package``, whose
    404-for-invisible is the rule rather than the exception.
    """
    return session.get(SkillPackage, package_id)


# =============================================================================
# Agent-scoped verbs
# =============================================================================


def _get_agent(session, agent_id: uuid.UUID, user) -> Agent:
    """Load an agent the caller owns, or 404.

    A non-owner gets the same answer as a nonexistent id — see
    :meth:`LLMPluginService.verify_agent_access`.
    """
    return LLMPluginService.verify_agent_access(session, agent_id, user)


@agent_router.get(
    "/{agent_id}/skills/{name}/publish-preview",
    response_model=SkillPublishPreview,
)
def preview_agent_skill_publish(
    agent_id: uuid.UUID,
    name: str,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """What publishing this skill would produce — version and package id.

    A read of the same derivations the publish performs, so the Share dialog
    can prefill rather than ask the publisher to invent a version number and a
    reverse-DNS id. Both answers need the server: the version is read out of
    the skill's own ``SKILL.md`` on the publisher's workspace, and the id has
    to be checked for collisions against packages this caller cannot list.

    Same gate and same coded refusals as the publish itself, minus the content
    checks — a skill flagged by the index still previews, and is still refused
    at publish.
    """
    agent = _get_agent(session, agent_id, current_user)
    try:
        return SkillCatalogService.publish_preview(
            session, agent=agent, user=current_user, skill_name=name
        )
    except SkillCatalogError as exc:
        raise http_error_for(exc)


@agent_router.post(
    "/{agent_id}/skills/{name}/publish",
    response_model=SkillPackageRevisionPublic,
)
async def publish_agent_skill(
    agent_id: uuid.UUID,
    name: str,
    data: SkillPublishRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Publish one of the agent's skills to the instance skills catalog.

    Gated by the agent-developer role on an agent that is not a foreign install
    — the same gate as bundle publish, and the same gate the Skills card's
    ``can_publish`` capability reply reports, so the verb can never be offered
    where it would be refused.

    A **suspended** environment publishes normally: the files are read from the
    workspace on disk, not from a running container. An agent with no
    environment, or one whose workspace was never materialised, answers 409
    with a code the dialog can spell out.
    """
    agent = _get_agent(session, agent_id, current_user)
    try:
        revision = await SkillCatalogService.publish_from_agent(
            session,
            agent=agent,
            user=current_user,
            skill_name=name,
            version=data.version,
            release_notes=data.release_notes,
            visibility=data.visibility,
            package_id=data.package_id,
            grant_emails=data.grant_emails,
        )
    except SkillCatalogError as exc:
        raise http_error_for(exc)
    return SkillCatalogService.revision_to_public(revision)


@agent_router.post("/{agent_id}/skills/install", response_model=PluginSyncResponse)
async def install_agent_skill(
    agent_id: uuid.UUID,
    data: SkillInstallRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Add a catalog skill to one of the caller's agents.

    Creates the ``source=catalog`` plugin link, then runs the ordinary plugin
    sync — which wakes a suspended environment and reports per-environment and
    per-plugin outcomes, so the dialog's copy about suspended targets stays
    true without any catalog-specific transport.
    """
    agent = _get_agent(session, agent_id, current_user)
    try:
        package = SkillCatalogService.get_package(
            session, data.package_id, current_user
        )
        revision = SkillCatalogService.resolve_revision(
            session, package, data.revision_number
        )
        link = SkillCatalogService.install_into_agent(
            session,
            agent=agent,
            package=package,
            revision=revision,
            conversation_mode=data.conversation_mode,
            building_mode=data.building_mode,
        )
    except SkillCatalogError as exc:
        raise http_error_for(exc)

    return await LLMPluginService.sync_plugins_to_agent_environments(
        session=session,
        agent_id=agent.id,
        user_id=current_user.id,
        plugin_link=link,
        message_prefix="Skill added.",
    )
