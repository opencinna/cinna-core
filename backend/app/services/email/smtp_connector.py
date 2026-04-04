"""
Injectable SMTP connector - wraps smtplib for testability.

Production code uses the module-level `smtp_connector` instance.
Tests swap it with a stub that captures sent emails.
"""
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart

from app.models.email.mail_server_config import MailServerConfig, EncryptionType


class SMTPConnector:
    """Wraps smtplib for SMTP connections. Injectable for testing."""

    def send(
        self,
        server: MailServerConfig,
        password: str,
        from_address: str,
        to_address: str,
        msg: MIMEMultipart,
    ) -> None:
        """Connect to SMTP and send email."""
        context = ssl.create_default_context()
        if server.encryption_type in (EncryptionType.SSL, EncryptionType.TLS):
            conn = smtplib.SMTP_SSL(server.host, server.port, timeout=30, context=context)
        else:
            conn = smtplib.SMTP(server.host, server.port, timeout=30)
            if server.encryption_type == EncryptionType.STARTTLS:
                conn.starttls(context=context)

        try:
            conn.login(server.username, password)
            conn.sendmail(from_address, [to_address], msg.as_string())
        finally:
            try:
                conn.quit()
            except Exception:
                pass


# Module-level default instance
smtp_connector = SMTPConnector()
