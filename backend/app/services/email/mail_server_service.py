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
    MailServerType,
    EncryptionType,
)

logger = logging.getLogger(__name__)


class MailServerService:

    @staticmethod
    def create_mail_server(
        session: Session,
        user_id: uuid.UUID,
        data: MailServerConfigCreate,
    ) -> MailServerConfig:
        server = MailServerConfig(
            user_id=user_id,
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
    def get_user_mail_servers(
        session: Session,
        user_id: uuid.UUID,
        server_type: MailServerType | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> MailServerConfigsPublic:
        base_where = MailServerConfig.user_id == user_id
        if server_type:
            base_where = base_where & (MailServerConfig.server_type == server_type)

        count = session.exec(
            select(func.count()).select_from(MailServerConfig).where(base_where)
        ).one()

        servers = session.exec(
            select(MailServerConfig)
            .where(base_where)
            .order_by(MailServerConfig.created_at.desc())
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
    def delete_mail_server(
        session: Session,
        server: MailServerConfig,
    ) -> None:
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
        try:
            context = ssl.create_default_context()
            if server.encryption_type in (EncryptionType.SSL, EncryptionType.TLS):
                conn = imaplib.IMAP4_SSL(server.host, server.port, ssl_context=context)
            else:
                conn = imaplib.IMAP4(server.host, server.port)
                if server.encryption_type == EncryptionType.STARTTLS:
                    conn.starttls(ssl_context=context)

            conn.login(server.username, password)
            conn.logout()
            return "IMAP connection successful"
        except imaplib.IMAP4.error as e:
            raise ValueError(f"IMAP authentication failed: {e}")
        except Exception as e:
            raise ValueError(f"IMAP connection failed: {e}")

    @staticmethod
    def _test_smtp(server: MailServerConfig, password: str) -> str:
        try:
            context = ssl.create_default_context()
            if server.encryption_type in (EncryptionType.SSL, EncryptionType.TLS):
                conn = smtplib.SMTP_SSL(server.host, server.port, timeout=15, context=context)
            else:
                conn = smtplib.SMTP(server.host, server.port, timeout=15)
                if server.encryption_type == EncryptionType.STARTTLS:
                    conn.starttls(context=context)

            conn.login(server.username, password)
            conn.quit()
            return "SMTP connection successful"
        except smtplib.SMTPAuthenticationError as e:
            raise ValueError(f"SMTP authentication failed: {e}")
        except Exception as e:
            raise ValueError(f"SMTP connection failed: {e}")

    @staticmethod
    def _to_public(server: MailServerConfig) -> MailServerConfigPublic:
        return MailServerConfigPublic(
            id=server.id,
            user_id=server.user_id,
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
