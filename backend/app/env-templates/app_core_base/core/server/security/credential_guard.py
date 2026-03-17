"""
Credential Guard — Output Redaction Pipeline (Phase 2)

Holds the set of known sensitive credential values for this environment and
provides output redaction scanning. Any agent output that contains a known
sensitive value has that value replaced with ***REDACTED*** before it reaches
the SSE stream and the user.

This module mirrors SENSITIVE_FIELDS from CredentialsService in the backend.
Both copies must be kept in sync when new credential types are added.

Usage:
    # Module-level singleton (initialized empty)
    credential_guard = CredentialGuard()

    # Called by AgentEnvService.update_credentials() after each credential sync
    credential_guard.update_values(credentials_data)

    # Called by _redacted_stream() in routes.py for each SSE event
    redacted_text, was_redacted = credential_guard.redact(event_content)
"""
import logging

logger = logging.getLogger(__name__)


class CredentialGuard:
    """
    Holds the set of known sensitive credential values for this environment.
    Provides output redaction scanning for the SSE stream.

    Thread safety: This singleton is updated by update_credentials() (sync HTTP
    handler) and read by _redacted_stream() (async generator). In practice these
    don't interleave problematically — credentials are synced before a session
    starts and rarely during it. For production hardening, a read-write lock
    could be added.
    """

    # Mirror of CredentialsService.SENSITIVE_FIELDS in backend/app/services/credentials_service.py
    # These are the fields whose values must be redacted from agent output.
    # Keep in sync with the backend when new credential types are added.
    SENSITIVE_FIELDS: dict[str, list[str]] = {
        "email_imap": ["password"],
        "email_smtp": ["password"],
        "odoo": ["api_token"],
        "gmail_oauth": ["access_token", "refresh_token"],
        "gmail_oauth_readonly": ["access_token", "refresh_token"],
        "gdrive_oauth": ["access_token", "refresh_token"],
        "gdrive_oauth_readonly": ["access_token", "refresh_token"],
        "gcalendar_oauth": ["access_token", "refresh_token"],
        "gcalendar_oauth_readonly": ["access_token", "refresh_token"],
        "api_token": ["http_header_value"],
        "google_service_account": ["private_key", "private_key_id"],
    }

    # Minimum character length for a value to be tracked.
    # Short values (ports, "true", "Bearer") cause too many false positives.
    MIN_VALUE_LENGTH = 8

    def __init__(self) -> None:
        self._sensitive_values: set[str] = set()

    def update_values(self, credentials_data: list[dict]) -> None:
        """
        Extract sensitive values from credentials data using SENSITIVE_FIELDS.

        Called whenever credentials are synced to the container (by
        AgentEnvService.update_credentials()). Replaces the previous value set
        from scratch — old values are purged on each call.

        Args:
            credentials_data: List of credential dicts in the same format as
                credentials.json:
                [
                    {
                        "type": "email_imap",
                        "credential_data": {"host": "...", "password": "secret", ...}
                    },
                    ...
                ]
        """
        new_values: set[str] = set()

        for cred in credentials_data:
            cred_type = cred.get("type", "")
            cred_data = cred.get("credential_data", {})
            sensitive_fields = self.SENSITIVE_FIELDS.get(cred_type, [])

            for field in sensitive_fields:
                value = cred_data.get(field)
                if isinstance(value, str) and len(value) >= self.MIN_VALUE_LENGTH:
                    new_values.add(value)

        self._sensitive_values = new_values
        logger.debug(
            "CredentialGuard updated: tracking %d sensitive values",
            len(new_values),
        )

    def redact(self, text: str) -> tuple[str, bool]:
        """
        Scan text for sensitive values and replace matches with ***REDACTED***.

        O(n * m) where n = len(text), m = len(_sensitive_values). For typical
        credential counts (<20 values) and message sizes (<10KB) this is fast.

        Args:
            text: Content to scan (agent output, tool result, etc.)

        Returns:
            Tuple of (redacted_text, was_redacted).
            was_redacted is True if at least one substitution was made.
        """
        if not self._sensitive_values:
            return text, False

        was_redacted = False
        for value in self._sensitive_values:
            if value in text:
                text = text.replace(value, "***REDACTED***")
                was_redacted = True

        return text, was_redacted


# Module-level singleton — shared across all requests within this container process
credential_guard = CredentialGuard()
