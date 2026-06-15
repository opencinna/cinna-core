import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.server_config.server_config import (
    DisclaimerPublic,
    ServerConfig,
    ServerConfigUpdate,
)


class ServerConfigService:
    """Manages the singleton server-wide configuration row."""

    @staticmethod
    def get_or_create(session: Session) -> ServerConfig:
        """Return the single ServerConfig row, creating it on first call.

        The first authenticated request after deploy lazily inserts the
        singleton. Reads are ordered deterministically (oldest first) so all
        callers converge on the same row even in the unlikely event a
        concurrent first-hit created two; the IntegrityError guard is
        defense-in-depth around the commit.
        """
        config = session.exec(
            select(ServerConfig).order_by(ServerConfig.updated_at)
        ).first()
        if config is not None:
            return config

        config = ServerConfig()
        session.add(config)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.exec(
                select(ServerConfig).order_by(ServerConfig.updated_at)
            ).first()
            if existing is not None:
                return existing
            raise
        session.refresh(config)
        return config

    @staticmethod
    def update(
        session: Session,
        data: ServerConfigUpdate,
        user_id: uuid.UUID,
    ) -> ServerConfig:
        """Apply changes to the singleton config.

        Increments ``disclaimer_version`` only when the disclaimer content or
        display mode actually changes, so acknowledged users re-see edits.
        """
        config = ServerConfigService.get_or_create(session)
        update_dict = data.model_dump(exclude_unset=True)

        content_changed = (
            "disclaimer_markdown" in update_dict
            and update_dict["disclaimer_markdown"] != config.disclaimer_markdown
        ) or (
            "disclaimer_display_mode" in update_dict
            and update_dict["disclaimer_display_mode"] != config.disclaimer_display_mode
        )

        config.sqlmodel_update(update_dict)
        if content_changed:
            config.disclaimer_version += 1
        config.updated_at = datetime.now(UTC)
        config.updated_by_id = user_id

        session.add(config)
        session.commit()
        session.refresh(config)
        return config

    @staticmethod
    def to_disclaimer_public(config: ServerConfig) -> DisclaimerPublic:
        return DisclaimerPublic(
            enabled=config.disclaimer_enabled,
            markdown=config.disclaimer_markdown,
            display_mode=config.disclaimer_display_mode,
            version=config.disclaimer_version,
        )
