import logging
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import settings
from app.models.server_config.server_config import (
    DisclaimerPublic,
    ServerConfig,
    ServerConfigUpdate,
)
from app.models.users.user import User
from app.services.users.access_policy_service import AccessPolicyService

logger = logging.getLogger(__name__)


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

        A brand new row picks up its access policy from the retired env
        settings (see ``_first_boot_seed``), so an instance upgrading from an
        env-configured deployment keeps behaving the way its ``.env`` says.
        """
        config = session.exec(
            select(ServerConfig).order_by(ServerConfig.updated_at)
        ).first()
        if config is not None:
            return config

        config = ServerConfig(**ServerConfigService._first_boot_seed())
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
    def _first_boot_seed() -> dict[str, str]:
        """Access-policy values for a row that does not exist yet.

        ``AUTH_WHITELIST_USER_DOMAINS`` and ``DEFAULT_USER_ROLE`` are read
        **here and in the alembic migration only**. Once the row exists the
        database is the sole authority, so an operator editing ``.env`` later
        sees no change — which is why startup warns about it. The domain list
        is converted into the glob syntax the policy speaks:
        ``acme.com`` becomes ``*@acme.com``.
        """
        domains = [
            d.strip().lower()
            for d in (settings.AUTH_WHITELIST_USER_DOMAINS or "").split(",")
            if d.strip()
        ]
        return {
            "allowed_email_patterns": ", ".join(f"*@{d}" for d in domains),
            "default_user_role": settings.DEFAULT_USER_ROLE,
        }

    @staticmethod
    def update(
        session: Session,
        data: ServerConfigUpdate,
        acting_user: User,
    ) -> ServerConfig:
        """Apply changes to the singleton config.

        Increments ``disclaimer_version`` only when the disclaimer content or
        display mode actually changes, so acknowledged users re-see edits.
        Every other field — ``local_agent_kit_enabled`` included — is copied
        without touching the version: bumping it would force every user to
        re-acknowledge an unchanged disclaimer.

        Raises:
            AccessPolicyValidationError: if the merged result would be
                malformed or would lock every administrator out.
        """
        config = ServerConfigService.get_or_create(session)

        # Validate BEFORE anything is copied onto the row: lockout prevention
        # is a server-side rule, not a UI courtesy, and a half-applied update
        # is exactly the state an admin cannot recover from.
        AccessPolicyService.validate_update(session, config, data, acting_user)

        # ``None`` means "not being changed", never "set this column to NULL":
        # every column behind this payload is NOT NULL, so a client sending an
        # explicit ``null`` would otherwise turn a no-op into a 500.
        update_dict = {
            k: v
            for k, v in data.model_dump(exclude_unset=True).items()
            if v is not None
        }

        # Store the pattern list in canonical form. Validation deliberately
        # tolerates blank entries (a trailing comma is not an error worth
        # refusing an admin's save over), but the readers downstream disagree
        # about what a string of nothing but separators means — see
        # ``AccessPolicyService.normalize_email_patterns``. Canonicalising here
        # means they never get the chance to.
        if "allowed_email_patterns" in update_dict:
            update_dict["allowed_email_patterns"] = (
                AccessPolicyService.normalize_email_patterns(
                    update_dict["allowed_email_patterns"]
                )
            )

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
        config.updated_by_id = acting_user.id

        session.add(config)
        session.commit()
        session.refresh(config)

        # Field NAMES only, never values: an allowed-email pattern list names
        # customer domains, and this line ends up in shared log aggregators.
        if update_dict:
            logger.info(
                "Server config updated (fields=%s, by=%s)",
                ",".join(sorted(update_dict)),
                acting_user.id,
            )
        return config

    @staticmethod
    def to_disclaimer_public(config: ServerConfig) -> DisclaimerPublic:
        return DisclaimerPublic(
            enabled=config.disclaimer_enabled,
            markdown=config.disclaimer_markdown,
            display_mode=config.disclaimer_display_mode,
            version=config.disclaimer_version,
        )
