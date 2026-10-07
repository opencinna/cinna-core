"""
Fresh-read credentials accessor for agent scripts and the agent REST API SDK.

The platform syncs every credential linked to the agent into
``/app/workspace/credentials/credentials.json``. This accessor centralises
reading it — for a producer's ``agent_api`` handlers and for any workspace or
skill script.

CRITICAL: the file is read **fresh on every access**. The serving child is a
long-running process; if we cached the parsed credentials at import time we would
serve stale secrets across an OAuth refresh or a credential resync (the old
subprocess-per-request webapp model got freshness for free — this one must not
regress it).

Slots
-----
A skill finds its credential by **slot**: the credential's non-secret
``service_uri``, carried top-level on every real entry together with
``is_placeholder``. :meth:`_Credentials.require_slot` raises
:class:`CredentialMissing` with a message that names the slot and the fix, so a
script can fail in a way the agent can relay to the user verbatim.

OAuth access tokens
-------------------
:meth:`_Credentials.access_token` returns a Google OAuth access token that is
valid for at least ``min_ttl`` seconds: the synced token when it has enough
life left, otherwise one fetched (and refreshed if needed) from the platform's
``POST /api/v1/agent/credentials/{id}/access-token``. Failures raise
:class:`CredentialRefreshError`, whose message is written for a person.

This module must stay importable with no network and without ``requests``:
:class:`AgentApiSession` is defined on first use; the token endpoint is called
with stdlib ``urllib``.
"""
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_CREDENTIALS_PATH = Path(
    os.getenv("CINNA_CREDENTIALS_PATH", "/app/workspace/credentials/credentials.json")
)

#: ``type`` markers of the synthetic entries the platform appends to
#: credentials.json. They are not credentials and never satisfy a slot.
#: Mirrors ``user_details_service.CURRENT_USER_TYPE`` and
#: ``agent_api_identity_service.OWNER_IDENTITY_TYPE`` (guarded by a unit test).
_CURRENT_USER_TYPE = "current_user"
_OWNER_IDENTITY_TYPE = "owner_identity_token"
_SYNTHETIC_TYPES = frozenset({_CURRENT_USER_TYPE, _OWNER_IDENTITY_TYPE})

_AGENT_API_TYPE = "agent_api"


#: Human words for an issued agent_api key the platform synced token-less
#: (the entry's ``unavailable_reason``). Mirrors the Local Agent Kit hints.
_UNAVAILABLE_REASON_WORDS = {
    "key_revoked": "the key was revoked by its issuer or its producer is gone",
    "key_expired": "the key has expired",
    "key_paused": "the producer has paused API access",
}

_UNAVAILABLE_REASON_FIXES = {
    "key_revoked": "ask the producer's owner for a new key.",
    "key_expired": "ask the producer's owner for a new key.",
    "key_paused": "it works again by itself once the producer's owner resumes access.",
}


class CredentialMissing(Exception):
    """A slot the script needs has no usable credential.

    ``reason`` is ``"not_linked"`` (no credential with this slot is linked to
    the agent), ``"not_configured"`` (one is linked but not filled in yet), or,
    for an issued ``agent_api`` key the platform synced without its token,
    ``"key_revoked"``, ``"key_expired"`` or ``"key_paused"`` — then
    ``key_status`` carries the detailed status (e.g. ``"revoked_by_issuer"``).
    ``str(exc)`` is stable and written for a person: relay it verbatim.
    """

    def __init__(self, slot: str, reason: str, key_status: str | None = None):
        # ``args`` must be the constructor arguments so the exception
        # round-trips through pickle; ``__str__`` builds the message.
        super().__init__(slot, reason, key_status)
        self.slot = slot
        self.reason = reason
        self.key_status = key_status

    def __str__(self) -> str:
        if self.reason in _UNAVAILABLE_REASON_WORDS:
            detail = f", {self.key_status}" if self.key_status else ""
            return (
                f"credential_unavailable: the credential for slot '{self.slot}' "
                f"is unavailable: {_UNAVAILABLE_REASON_WORDS[self.reason]} "
                f"({self.reason}{detail}). Fix: "
                f"{_UNAVAILABLE_REASON_FIXES[self.reason]}"
            )
        if self.reason not in ("not_linked", "not_configured"):
            detail = f", {self.key_status}" if self.key_status else ""
            return (
                f"credential_unavailable: the credential for slot '{self.slot}' "
                f"is unavailable ({self.reason}{detail})."
            )
        if self.reason == "not_configured":
            return (
                f"credential_missing: the credential for slot '{self.slot}' is "
                "linked but not filled in yet. Fix: open the agent's Credentials "
                "tab and complete it."
            )
        return (
            f"credential_missing: no credential for slot '{self.slot}' is linked "
            "to this agent. Fix: open the agent's Credentials tab and link a "
            f"credential whose service URI (slot) is '{self.slot}'."
        )


_REFRESH_ERROR_MESSAGES = {
    "credential_not_linked": (
        "credential '{cid}' is not linked to this agent. Fix: open the agent's "
        "Credentials tab and link it."
    ),
    "not_refreshable": (
        "credential '{cid}' is not a Google OAuth credential, so it has no "
        "access token to fetch."
    ),
    "reauthorization_required": (
        "Google no longer accepts the authorization for credential '{cid}'. "
        "Fix: ask the user to re-authorize this credential in the platform's "
        "credential settings."
    ),
    "provider_error": (
        "Google could not issue a new access token for credential '{cid}' right "
        "now. Try again in a minute."
    ),
    "oauth_not_configured": (
        "Google OAuth is not configured on the platform, so the token for "
        "credential '{cid}' cannot be refreshed. Ask a platform administrator."
    ),
    "refresh_in_progress": (
        "another refresh of credential '{cid}' is in progress and the synced "
        "token has expired. Try again in a few seconds."
    ),
    "unauthorized": (
        "the platform rejected this environment's credentials while fetching "
        "a token for credential '{cid}'. Fix: restart (or rebuild) the "
        "environment."
    ),
    "unavailable": (
        "the platform could not be reached to refresh credential '{cid}' and "
        "the synced token has expired. Try again shortly."
    ),
}

_TOKEN_REQUEST_TIMEOUT_SECONDS = 20
#: Mirrors the platform's ``OAUTH_ON_DEMAND_MAX_MIN_TTL_SECONDS`` default.
_MAX_MIN_TTL_SECONDS = 1800
_IN_PROGRESS_RETRY_DELAY_SECONDS = 1.5


class CredentialRefreshError(Exception):
    """An OAuth access token could not be obtained.

    ``code`` is one of ``credential_not_linked``, ``not_refreshable``,
    ``reauthorization_required``, ``provider_error``, ``refresh_in_progress``,
    ``oauth_not_configured``, ``unauthorized`` or ``unavailable``. ``str(exc)`` is written for a person: relay it verbatim.
    """

    def __init__(self, code: str, credential_id: str):
        super().__init__(code, credential_id)
        self.code = code
        self.credential_id = credential_id

    def __str__(self) -> str:
        template = _REFRESH_ERROR_MESSAGES.get(
            self.code, "could not get an access token for credential '{cid}' ({code})."
        )
        return f"{self.code}: " + template.format(cid=self.credential_id, code=self.code)


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """``(scheme, host, effective port)`` of an absolute URL, lowercased.

    ``None`` when the URL has no host or an unparseable port, which never
    equals any origin.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    scheme = parts.scheme.lower()
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    return scheme, parts.hostname.lower(), port


_agent_api_session_class: type | None = None


def _get_agent_api_session_class() -> type:
    """Define :class:`AgentApiSession` on first use, so importing this module
    never imports ``requests``."""
    global _agent_api_session_class
    if _agent_api_session_class is not None:
        return _agent_api_session_class

    import requests
    from requests.structures import CaseInsensitiveDict

    class AgentApiSession(requests.Session):
        """A ``requests.Session`` pre-authorised for one ``agent_api`` connection.

        A relative ``url`` is joined onto :attr:`base_url` with exactly one
        ``/``; an absolute ``http(s)://`` URL is sent as given.

        Credentials only go to the producer. The ``Authorization`` and
        caller-identity headers are sent only when the request URL has the same
        origin as :attr:`base_url`: scheme, host and effective port, with scheme
        and host compared case-insensitively. A relative URL always gets them.
        An absolute URL to any other origin is sent without them. A header the
        caller passes explicitly for that one request is still sent. The
        session's own headers are never changed.

        Redirects follow the same rule: a redirect whose target origin is not
        :attr:`base_url`'s origin (a different host, scheme or port, so also a
        same-host http→https upgrade) loses both headers before it is sent.

        Only the verb methods (``get``, ``post``, ... and ``request``) are
        guarded. ``session.send(session.prepare_request(...))`` bypasses
        :meth:`request` and sends the session headers to any URL, so scripts
        should use the verb methods.
        """

        def __init__(self, base_url: str, spec_url: str | None):
            super().__init__()
            self.base_url = base_url
            self.spec_url = spec_url
            self._credential_header_names: list[str] = []

        def set_credential_header(self, name: str, value: str) -> None:
            """Set a session header sent only to :attr:`base_url`'s origin."""
            self.headers[name] = value
            self._credential_header_names.append(name)

        def request(self, method, url, *args, **kwargs):  # type: ignore[override]
            target = str(url)
            if not target.lower().startswith(("http://", "https://")):
                target = f"{self.base_url.rstrip('/')}/{target.lstrip('/')}"
            elif not self._is_producer_origin(target):
                # ``headers`` is the third positional after method and url.
                if len(args) > 2:
                    args = (*args[:2], self._without_credentials(args[2]), *args[3:])
                else:
                    kwargs["headers"] = self._without_credentials(
                        kwargs.get("headers")
                    )
            return super().request(method, target, *args, **kwargs)

        def _is_producer_origin(self, url: str) -> bool:
            origin = _origin(url)
            return origin is not None and origin == _origin(self.base_url)

        def _without_credentials(self, headers: Any) -> CaseInsensitiveDict:
            # requests drops a merged header whose per-request value is None.
            merged = CaseInsensitiveDict(headers or {})
            for name in self._credential_header_names:
                if name not in merged:
                    merged[name] = None
            return merged

        def rebuild_auth(self, prepared_request, response):  # type: ignore[override]
            # requests strips only ``Authorization``, and only on a host
            # change; the caller-identity header would otherwise follow a
            # redirect anywhere.
            super().rebuild_auth(prepared_request, response)
            if not self._is_producer_origin(str(prepared_request.url)):
                for name in self._credential_header_names:
                    if name in prepared_request.headers:
                        del prepared_request.headers[name]

    _agent_api_session_class = AgentApiSession
    return AgentApiSession


class _Credentials:
    """Typed accessor over credentials.json. Reads the file fresh on each call."""

    def _load(self) -> list[dict]:
        """Read + parse credentials.json. Returns [] when missing/unreadable."""
        try:
            if not _CREDENTIALS_PATH.is_file():
                return []
            with open(_CREDENTIALS_PATH, encoding="utf-8") as f:
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

    def access_token(self, credential_id: str, *, min_ttl: int = 300) -> str:
        """
        Return a Google OAuth access token valid for at least ``min_ttl`` seconds.

        Uses the synced token when it has enough life left (no network);
        otherwise asks the platform, which refreshes it if needed. Call it again
        right before a long batch instead of holding one token. Never logs the
        token.

        Raises :class:`CredentialRefreshError` (relay ``str(exc)`` verbatim).
        """
        cid = str(credential_id)
        min_ttl = max(0, min(int(min_ttl), _MAX_MIN_TTL_SECONDS))
        entry = self.get(cid)
        if entry is None:
            raise CredentialRefreshError("credential_not_linked", cid)
        data = entry.get("credential_data") or {}
        local_token = data.get("access_token")
        expires_at = data.get("expires_at")
        remaining = (
            expires_at - time.time() if isinstance(expires_at, (int, float)) else None
        )
        if local_token and remaining is not None and remaining > min_ttl:
            return local_token

        real_id = str(entry.get("id") or cid)
        known_expires_at = (
            int(expires_at) if isinstance(expires_at, (int, float)) else None
        )
        try:
            try:
                return self._fetch_access_token(real_id, min_ttl, known_expires_at)
            except CredentialRefreshError as e:
                if e.code != "refresh_in_progress":
                    raise
                # Another worker is refreshing right now: one short retry.
                time.sleep(_IN_PROGRESS_RETRY_DELAY_SECONDS)
                return self._fetch_access_token(real_id, min_ttl, known_expires_at)
        except _PlatformUnavailable as e:
            if local_token and remaining is not None and remaining > 0:
                logger.warning(
                    "Could not refresh credential %s via the platform (%s); "
                    "using the synced token (%.0f s left)",
                    real_id, e, remaining,
                )
                return local_token
            raise CredentialRefreshError("unavailable", real_id) from None

    @staticmethod
    def _fetch_access_token(
        credential_id: str, min_ttl: int, known_expires_at: int | None
    ) -> str:
        """POST to the platform's access-token endpoint (env vars read now)."""
        backend_url = os.getenv("BACKEND_URL")
        auth_token = os.getenv("AGENT_AUTH_TOKEN")
        env_id = os.getenv("ENV_ID")
        if not (backend_url and auth_token and env_id):
            raise _PlatformUnavailable("platform connection settings are missing")

        url = (
            f"{backend_url.rstrip('/')}/api/v1/agent/credentials/"
            f"{credential_id}/access-token"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(
                {"min_ttl": int(min_ttl), "known_expires_at": known_expires_at}
            ).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {auth_token}",
                "X-Agent-Env-Id": env_id,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=_TOKEN_REQUEST_TIMEOUT_SECONDS
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise CredentialRefreshError("unauthorized", credential_id) from None
            code = _error_code(e)
            if code is None:
                raise _PlatformUnavailable(f"HTTP {e.code}") from None
            raise CredentialRefreshError(code, credential_id) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise _PlatformUnavailable(type(e).__name__) from None

        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            raise _PlatformUnavailable("response had no access token")
        return token

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

    def by_slot(self, slot: str) -> dict | None:
        """
        Return a real credential whose ``service_uri`` equals ``slot``, or
        None. Prefer an available, filled credential over a token-less
        (``unavailable_reason``) one and over any remaining placeholder, so
        linking a replacement makes the slot usable immediately. Synthetic
        entries never match. Reads the file fresh.
        """
        return self._find_slot(self._load(), slot)

    def require_slot(self, slot: str) -> dict:
        """
        Return the credential for ``slot``, or raise :class:`CredentialMissing`:
        ``not_linked`` when none is linked, ``not_configured`` when the linked
        one is still a placeholder, or the entry's ``unavailable_reason``
        (``key_revoked`` / ``key_expired`` / ``key_paused``, with
        ``.key_status``) for an issued key synced without its token. Reads the
        file fresh.
        """
        return self._require_slot_in(self._load(), slot)

    def agent_api_session(self, slot: str) -> Any:
        """
        Return an ``AgentApiSession`` (a ``requests.Session``) for the
        ``agent_api`` credential in ``slot`` (a connection or an issued key):
        ``Authorization: Bearer <token>`` plus the caller-identity header when
        the platform provided one (an issued key's identity is bound to the key;
        the producer ignores the header for it), and ``.base_url`` /
        ``.spec_url`` from the credential.

        Raises :class:`CredentialMissing` like :meth:`require_slot` (including
        ``key_revoked`` / ``key_expired`` / ``key_paused``), and
        ``ValueError`` when the slot holds a credential of another type.
        """
        entries = self._load()
        entry = self._require_slot_in(entries, slot)
        if entry.get("type") != _AGENT_API_TYPE:
            raise ValueError(
                f"The credential for slot '{slot}' is of type "
                f"'{entry.get('type')}', not '{_AGENT_API_TYPE}'."
            )

        data = entry.get("credential_data") or {}
        session = _get_agent_api_session_class()(
            base_url=str(data.get("base_url") or ""),
            spec_url=data.get("spec_url"),
        )
        session.set_credential_header(
            "Authorization", f"Bearer {data.get('token') or ''}"
        )

        identity = next(
            (e for e in entries if e.get("type") == _OWNER_IDENTITY_TYPE), None
        )
        if identity is not None:
            identity_data = identity.get("credential_data") or {}
            header, token = identity_data.get("header"), identity_data.get("token")
            if header and token:
                session.set_credential_header(header, token)
        return session

    @staticmethod
    def _find_slot(entries: list[dict], slot: str) -> dict | None:
        """The best entry for ``slot``: an available, filled one first, then an
        unavailable (token-less issued key) one, then a placeholder."""
        unavailable = None
        placeholder = None
        for cred in entries:
            if cred.get("type") in _SYNTHETIC_TYPES:
                continue
            if cred.get("service_uri") != slot:
                continue
            if cred.get("is_placeholder") is True:
                if placeholder is None:
                    placeholder = cred
            elif cred.get("unavailable_reason"):
                if unavailable is None:
                    unavailable = cred
            else:
                return cred
        return unavailable or placeholder

    @classmethod
    def _require_slot_in(cls, entries: list[dict], slot: str) -> dict:
        entry = cls._find_slot(entries, slot)
        if entry is None:
            raise CredentialMissing(slot, "not_linked")
        if entry.get("is_placeholder") is True:
            raise CredentialMissing(slot, "not_configured")
        if entry.get("unavailable_reason"):
            raise CredentialMissing(
                slot, str(entry["unavailable_reason"]), entry.get("key_status")
            )
        return entry


class _PlatformUnavailable(Exception):
    """The platform endpoint could not be used (internal; never raised to callers)."""


def _error_code(error: "urllib.error.HTTPError") -> str | None:
    """``detail.code`` from a platform error response, or None."""
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, str) else None


# Singleton accessor — stateless (no caching), safe to import once.
credentials = _Credentials()
