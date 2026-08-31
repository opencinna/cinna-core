import imaplib
import logging
import smtplib
import ssl
import uuid
from datetime import UTC, datetime

from sqlmodel import Session, select, func

from app.core.security import encrypt_field, decrypt_field
from app.models.email.mail_server_config import (
    MailServerConfig,
    MailServerConfigCreate,
    MailServerConfigUpdate,
    MailServerConfigPublic,
    MailServerConfigsPublic,
    MailServerChannelUsage,
    MailServerDeletionImpact,
    MailServerType,
    EncryptionType,
)
from app.models.server_channels.server_channel import ServerChannel

logger = logging.getLogger(__name__)

#: Channel ``config`` keys that hold a mail-server id, and the role each plays.
_MAIL_SERVER_CONFIG_KEYS: tuple[tuple[str, str], ...] = (
    ("incoming_server_id", "incoming"),
    ("outgoing_server_id", "outgoing"),
)


class MailServerInUseError(Exception):
    """A mail server still referenced by a channel cannot be deleted.

    Carries the :class:`MailServerDeletionImpact` so the route can answer 409
    with the list of channels that must be detached first — the same shape the
    credential-deletion guard uses.
    """

    def __init__(self, impact: MailServerDeletionImpact):
        self.impact = impact
        super().__init__("Mail server is referenced by one or more channels")


class MailServerService:

    @staticmethod
    def create_mail_server(
        session: Session,
        data: MailServerConfigCreate,
    ) -> MailServerConfig:
        server = MailServerConfig(
            name=data.name,
            server_type=data.server_type,
            host=data.host,
            port=data.port,
            encryption_type=data.encryption_type,
            username=data.username,
            encrypted_password=encrypt_field(data.password),
        )
        session.add(server)
        session.commit()
        session.refresh(server)
        return server

    @staticmethod
    def list_mail_servers(
        session: Session,
        server_type: MailServerType | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> MailServerConfigsPublic:
        """List every configured mail server (server-scoped, superuser-only)."""
        query = select(MailServerConfig)
        count_query = select(func.count()).select_from(MailServerConfig)
        if server_type:
            query = query.where(MailServerConfig.server_type == server_type)
            count_query = count_query.where(
                MailServerConfig.server_type == server_type
            )

        count = session.exec(count_query).one()

        servers = session.exec(
            query.order_by(MailServerConfig.created_at.desc())
            .offset(skip)
            .limit(limit)
        ).all()

        return MailServerConfigsPublic(
            data=[MailServerService._to_public(s) for s in servers],
            count=count,
        )

    @staticmethod
    def get_mail_server(
        session: Session,
        server_id: uuid.UUID,
    ) -> MailServerConfig | None:
        return session.get(MailServerConfig, server_id)

    @staticmethod
    def get_mail_server_with_credentials(
        session: Session,
        server_id: uuid.UUID,
    ) -> tuple[MailServerConfig, str] | None:
        """Return server config and decrypted password. Internal use only."""
        server = session.get(MailServerConfig, server_id)
        if not server:
            return None
        password = decrypt_field(server.encrypted_password)
        return server, password

    @staticmethod
    def update_mail_server(
        session: Session,
        server: MailServerConfig,
        data: MailServerConfigUpdate,
    ) -> MailServerConfig:
        update_dict = data.model_dump(exclude_unset=True)

        # Handle password separately (needs encryption)
        password = update_dict.pop("password", None)
        if password:
            server.encrypted_password = encrypt_field(password)

        server.sqlmodel_update(update_dict)
        server.updated_at = datetime.now(UTC)
        session.add(server)
        session.commit()
        session.refresh(server)
        return server

    @staticmethod
    def get_deletion_impact(
        session: Session,
        server_id: uuid.UUID,
    ) -> MailServerDeletionImpact:
        """Find every channel that references this mail server.

        Written as an explicit scan over channel rows rather than a JSON
        operator query: ``ServerChannel.config`` is a plain JSON column, the
        set of channels is admin-sized, and a readable Python loop survives a
        config-key rename in a way a hand-written JSON path does not.

        The stored id is compared by **parsing it into a UUID**, not by string
        equality. ``config`` is free-form JSON that no adapter validates for
        these keys, so the value can legitimately arrive in any spelling a UUID
        has — uppercase hex, ``{braces}``, ``urn:uuid:``, hyphenless. Every one
        of those compares unequal as a string, which would report an empty
        impact and let the delete through, leaving the channel pointing at a
        dead id: precisely the failure this guard exists to prevent. A value
        that is not a UUID at all is simply not a reference, and is skipped.
        """
        usages: list[MailServerChannelUsage] = []

        for channel in session.exec(select(ServerChannel)).all():
            config = channel.config or {}
            for key, role in _MAIL_SERVER_CONFIG_KEYS:
                raw = config.get(key)
                if raw is None:
                    continue
                try:
                    if uuid.UUID(str(raw)) != server_id:
                        continue
                except (TypeError, ValueError):
                    continue
                usages.append(
                    MailServerChannelUsage(
                        channel_id=channel.id,
                        channel_name=channel.name,
                        role=role,
                    )
                )

        return MailServerDeletionImpact(channel_usages=usages)

    @staticmethod
    def delete_mail_server(
        session: Session,
        server: MailServerConfig,
    ) -> None:
        """Delete a mail server, unless a channel still references it.

        The referencing keys live in ``ServerChannel.config`` with no FK behind
        them, so an unguarded delete succeeds and leaves the channel pointing
        at nothing — inbound mail simply stops. Raises
        :class:`MailServerInUseError` (route → 409) instead.
        """
        impact = MailServerService.get_deletion_impact(session, server.id)
        if impact.channel_usages:
            raise MailServerInUseError(impact)

        session.delete(server)
        session.commit()

    @staticmethod
    def test_connection(
        session: Session,
        server_id: uuid.UUID,
    ) -> str:
        """Test IMAP or SMTP connection. Returns success message or raises ValueError."""
        result = MailServerService.get_mail_server_with_credentials(session, server_id)
        if not result:
            raise ValueError("Mail server not found")

        server, password = result

        if server.server_type == MailServerType.IMAP:
            return MailServerService._test_imap(server, password)
        else:
            return MailServerService._test_smtp(server, password)

    @staticmethod
    def _test_imap(server: MailServerConfig, password: str) -> str:
        conn = None
        try:
            context = ssl.create_default_context()
            if server.encryption_type in (EncryptionType.SSL, EncryptionType.TLS):
                conn = imaplib.IMAP4_SSL(server.host, server.port, ssl_context=context)
            else:
                conn = imaplib.IMAP4(server.host, server.port)
                if server.encryption_type == EncryptionType.STARTTLS:
                    conn.starttls(ssl_context=context)

            conn.login(server.username, password)
            return "IMAP connection successful"
        except imaplib.IMAP4.error as e:
            raise ValueError(f"IMAP authentication failed: {e}")
        except Exception as e:
            raise ValueError(f"IMAP connection failed: {e}")
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    try:
                        conn.shutdown()
                    except Exception:
                        pass

    @staticmethod
    def _test_smtp(server: MailServerConfig, password: str) -> str:
        conn = None
        try:
            context = ssl.create_default_context()
            if server.encryption_type in (EncryptionType.SSL, EncryptionType.TLS):
                conn = smtplib.SMTP_SSL(server.host, server.port, timeout=15, context=context)
            else:
                conn = smtplib.SMTP(server.host, server.port, timeout=15)
                if server.encryption_type == EncryptionType.STARTTLS:
                    conn.starttls(context=context)

            conn.login(server.username, password)
            return "SMTP connection successful"
        except smtplib.SMTPAuthenticationError as e:
            raise ValueError(f"SMTP authentication failed: {e}")
        except Exception as e:
            raise ValueError(f"SMTP connection failed: {e}")
        finally:
            if conn is not None:
                try:
                    conn.quit()
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass

    @staticmethod
    def _to_public(server: MailServerConfig) -> MailServerConfigPublic:
        return MailServerConfigPublic(
            id=server.id,
            name=server.name,
            server_type=server.server_type,
            host=server.host,
            port=server.port,
            encryption_type=server.encryption_type,
            username=server.username,
            has_password=bool(server.encrypted_password),
            created_at=server.created_at,
            updated_at=server.updated_at,
        )
