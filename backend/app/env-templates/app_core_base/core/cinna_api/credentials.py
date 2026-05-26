"""
Fresh-read credentials accessor for the agent REST API SDK.

The powerful upstream credential (ERP key, broad OAuth scope, ...) lives only in
the producer container, synced by the platform into
``/app/workspace/credentials/credentials.json``. This accessor centralises
reading it.

CRITICAL: the file is read **fresh on every access**. The serving child is a
long-running process; if we cached the parsed credentials at import time we would
serve stale secrets across an OAuth refresh or a credential resync (the old
subprocess-per-request webapp model got freshness for free — this one must not
regress it).
"""
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_CREDENTIALS_PATH = Path(
    os.getenv("CINNA_CREDENTIALS_PATH", "/app/workspace/credentials/credentials.json")
)


class _Credentials:
    """Typed accessor over credentials.json. Reads the file fresh on each call."""

    def _load(self) -> list[dict]:
        """Read + parse credentials.json. Returns [] when missing/unreadable."""
        try:
            if not _CREDENTIALS_PATH.is_file():
                return []
            with open(_CREDENTIALS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            # Tolerate a {"credentials": [...]} envelope just in case.
            if isinstance(data, dict) and isinstance(data.get("credentials"), list):
                return data["credentials"]
            logger.warning("credentials.json has unexpected shape: %s", type(data).__name__)
            return []
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to read credentials.json: %s", e)
            return []

    def all(self) -> list[dict]:
        """Return every credential dict (read fresh)."""
        return self._load()

    def get(self, credential_id: str) -> dict | None:
        """
        Return the credential whose ``id`` (or ``name``) matches, or None.

        Reads the file fresh so a just-refreshed token is picked up.
        """
        for cred in self._load():
            if str(cred.get("id")) == str(credential_id) or cred.get("name") == credential_id:
                return cred
        return None

    def by_type(self, credential_type: str) -> dict | None:
        """
        Return the first credential of the given ``type`` (e.g. "odoo",
        "email_imap"), or None. Reads the file fresh.
        """
        for cred in self._load():
            if cred.get("type") == credential_type:
                return cred
        return None

    def all_by_type(self, credential_type: str) -> list[dict]:
        """Return every credential of the given ``type`` (read fresh)."""
        return [c for c in self._load() if c.get("type") == credential_type]


# Singleton accessor — stateless (no caching), safe to import once.
credentials = _Credentials()
