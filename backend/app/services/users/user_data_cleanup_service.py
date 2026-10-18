"""Remove what a deleted account leaves outside the database.

Both user-deletion routes are a bare ``session.delete(user)`` that relies on
database ``ON DELETE CASCADE``: the user's agents, environments, app-data
volumes and file uploads go with the row, but their containers and folders
do not. The routes snapshot what the user owns before the delete and run
:meth:`UserDataCleanupService.run` after the commit, so nothing is removed
for a deletion that did not happen.

Cleaned, keyed by the deleted rows:

- every environment of every agent the user owned: its container (compose
  down) and ``<ENV_INSTANCES_DIR>/<env_id>/``;
- ``<APP_DATA_STORAGE_DIR>/<user_id>/`` (all the user's app-data volumes);
- ``<UPLOAD_BASE_PATH>/<user_id>/`` (all the user's file uploads);
- ``<ARTIFACT_STORAGE_DIR>/<artifact_id>/`` for every artifact the user owned;
- ``<SKILL_STORAGE_DIR>/marketplace-skills/<marketplace_id>/`` (skill archive
  snapshots) for every plugin marketplace the user owned.

Deliberately left: skill packages the user published (``publisher_user_id``
is SET NULL, the package lives on, so does ``SKILL_STORAGE_DIR/<uuid>/``),
and desktop agents (detached from their publisher, kept in the catalog).
"""
import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from sqlmodel import Session, col, select

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.artifacts.artifact import Artifact
from app.models.environments.environment import AgentEnvironment
from app.models.plugins.llm_plugin import LLMPluginMarketplace
from app.services.artifacts.artifact_storage import ArtifactStorage
from app.services.common.data_dirs import remove_data_subdir
from app.services.plugins.marketplace_skill_snapshots import (
    MarketplaceSkillSnapshots,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserDataCleanup:
    """What to remove once the deletion of ``user_id`` has committed."""

    user_id: uuid.UUID
    environment_ids: list[uuid.UUID] = field(default_factory=list)
    artifact_ids: list[uuid.UUID] = field(default_factory=list)
    marketplace_ids: list[uuid.UUID] = field(default_factory=list)


class UserDataCleanupService:
    @staticmethod
    def snapshot(session: Session, user_id: uuid.UUID) -> UserDataCleanup:
        """Collect, before the delete, what the cascade will orphan."""
        env_ids = session.exec(
            select(AgentEnvironment.id)
            .join(Agent, col(Agent.id) == col(AgentEnvironment.agent_id))
            .where(Agent.owner_id == user_id)
        ).all()
        artifact_ids = session.exec(
            select(Artifact.id).where(Artifact.owner_id == user_id)
        ).all()
        marketplace_ids = session.exec(
            select(LLMPluginMarketplace.id).where(
                LLMPluginMarketplace.user_id == user_id
            )
        ).all()
        return UserDataCleanup(
            user_id=user_id,
            environment_ids=list(env_ids),
            artifact_ids=list(artifact_ids),
            marketplace_ids=list(marketplace_ids),
        )

    @staticmethod
    async def run(cleanup: UserDataCleanup) -> None:
        """Best effort; logs every failure and never raises."""
        from app.services.environments.environment_service import EnvironmentService

        lifecycle = EnvironmentService.get_lifecycle_manager()
        for env_id in cleanup.environment_ids:
            try:
                await lifecycle.discard_instance(env_id)
            except Exception as e:
                logger.warning(
                    "Failed to remove environment %s of deleted user %s: %s",
                    env_id, cleanup.user_id, e,
                )
        user_dir = str(cleanup.user_id)
        await asyncio.to_thread(
            remove_data_subdir, settings.APP_DATA_STORAGE_DIR, user_dir, what="app-data"
        )
        await asyncio.to_thread(
            remove_data_subdir, settings.UPLOAD_BASE_PATH, user_dir, what="uploads"
        )
        for artifact_id in cleanup.artifact_ids:
            await asyncio.to_thread(ArtifactStorage.remove_artifact, artifact_id)
        for marketplace_id in cleanup.marketplace_ids:
            await asyncio.to_thread(
                MarketplaceSkillSnapshots.remove_marketplace, marketplace_id
            )
