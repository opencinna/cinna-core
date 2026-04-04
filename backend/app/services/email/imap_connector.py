"""
Injectable IMAP connector - wraps imaplib for testability.

Production code uses the module-level `imap_connector` instance.
Tests swap it with a stub that returns preconfigured emails.
"""
import imaplib
import ssl

from app.models.email.mail_server_config import MailServerConfig, EncryptionType


class IMAPConnector:
    """Wraps imaplib for IMAP connections. Injectable for testing."""

    def connect(self, server: MailServerConfig, password: str) -> imaplib.IMAP4:
        """Create authenticated IMAP connection."""
        context = ssl.create_default_context()
        if server.encryption_type in (EncryptionType.SSL, EncryptionType.TLS):
            conn = imaplib.IMAP4_SSL(server.host, server.port, ssl_context=context)
        else:
            conn = imaplib.IMAP4(server.host, server.port)
            if server.encryption_type == EncryptionType.STARTTLS:
                conn.starttls(ssl_context=context)

        conn.login(server.username, password)
        return conn


# Module-level default instance
imap_connector = IMAPConnector()
