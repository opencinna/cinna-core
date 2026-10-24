"""The addons directory version counter (``server_config.addons_directory_version``).

Kept apart from :mod:`addon_directory_service` on purpose: the writers that
bump it (marketplace sync, the snapshot worker, catalog publish, the MCP
directory) are imported *by* that service, and a dependency the other way would
be a cycle.
"""
from sqlalchemy import update
from sqlmodel import Session, select

from app.models.server_config.server_config import ServerConfig


class AddonsDirectoryVersion:
    """Bump on every write that can change a directory answer; read to key the cache."""

    @staticmethod
    def bump(session: Session) -> None:
        """Increment the counter inside the caller's transaction (no commit).

        A bare ``UPDATE`` rather than ``ServerConfigService.get_or_create``:
        creating the singleton commits, which would commit the caller's
        half-done work. With no row yet there is nothing cached under a
        version either, and the first read creates the row.
        """
        session.execute(
            update(ServerConfig).values(
                addons_directory_version=ServerConfig.addons_directory_version + 1
            )
        )

    @staticmethod
    def current(session: Session) -> int:
        """The counter, read as a column so a session that already holds the
        row (tests share one) never answers from its identity map."""
        value = session.exec(
            select(ServerConfig.addons_directory_version).order_by(
                ServerConfig.updated_at
            )
        ).first()
        if value is not None:
            return int(value)
        from app.services.server_config.server_config_service import (
            ServerConfigService,
        )

        return int(ServerConfigService.get_or_create(session).addons_directory_version or 0)
