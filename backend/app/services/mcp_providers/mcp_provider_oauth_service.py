"""
MCP Provider OAuth/DCR service (RD-3).

Backend-side OAuth 2.1 client for ``mcp_provider`` credentials with
``auth_mode="oauth_dcr"``. The backend performs Dynamic Client Registration
(RFC 7591), the authorization-code exchange (PKCE S256, RFC 7636), and token
refresh on the user's behalf. ``client_secret`` and ``refresh_token`` never
leave the backend — only the short-lived access token is injected into the
container manifest (mirrors how Google ``oauth_credentials`` keep the refresh
token / client secret backend-only).

Discovery chain (RFC 9728 → RFC 8414), the same contract our own AS publishes
(see ``docs/application/mcp_integration/agent_mcp_architecture.md``):

    GET {endpoint}/.well-known/oauth-protected-resource   → authorization_servers[]
    GET {as}/.well-known/oauth-authorization-server       → {authorize, token, register}

All backend-initiated network calls go through ``egress_guard.assert_url_allowed``
(RD-6) so a private/loopback/link-local target — including via DNS rebinding —
is rejected before any connection is opened.
"""
import base64
import hashlib
import json
import logging
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from sqlmodel import Session

from app.core import security
from app.core.config import settings
from app.models import Credential
from app.models.credentials.credential import CredentialType
from app.services.mcp_providers.egress_guard import (
    EgressBlockedError,
    assert_url_allowed,
)

logger = logging.getLogger(__name__)

# In-memory CSRF + PKCE state store, keyed by random nonce, 10-minute TTL.
# Mirrors the Google OAuth ``_oauth_states`` pattern (single-process; a
# multi-worker deployment would move this to Redis, same caveat as Google OAuth).
_oauth_states: dict[str, dict[str, Any]] = {}
_STATE_TTL_SECONDS = 600

# Network timeout for backend-initiated MCP OAuth calls.
_HTTP_TIMEOUT = 15.0


class MCPProviderOAuthError(Exception):
    """OAuth/DCR error for the MCP-provider flow."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class MCPProviderOAuthService:
    """Backend OAuth 2.1 / DCR client for ``oauth_dcr`` mcp_provider credentials."""

    # ------------------------------------------------------------------ #
    # State store                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _prune_states() -> None:
        now = time.time()
        for k in [k for k, v in _oauth_states.items() if v["expires"] < now]:
            _oauth_states.pop(k, None)

    @staticmethod
    def _put_state(credential_id: uuid.UUID, user_id: uuid.UUID, code_verifier: str) -> str:
        MCPProviderOAuthService._prune_states()
        state = secrets.token_urlsafe(32)
        _oauth_states[state] = {
            "credential_id": str(credential_id),
            "user_id": str(user_id),
            "code_verifier": code_verifier,
            "expires": time.time() + _STATE_TTL_SECONDS,
        }
        return state

    @staticmethod
    def _take_state(state: str) -> dict[str, Any]:
        MCPProviderOAuthService._prune_states()
        data = _oauth_states.pop(state, None)
        if data is None:
            raise MCPProviderOAuthError(
                "Invalid or expired state parameter", status_code=400
            )
        return data

    # ------------------------------------------------------------------ #
    # PKCE                                                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _generate_pkce() -> tuple[str, str]:
        """Return (code_verifier, code_challenge) using S256."""
        verifier = secrets.token_urlsafe(64)[:128]
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return verifier, challenge

    # ------------------------------------------------------------------ #
    # Discovery (RFC 9728 → RFC 8414)                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def discover_authorization_server(endpoint_url: str) -> dict:
        """
        Discover the AS metadata for an MCP endpoint.

        Fetches ``/.well-known/oauth-protected-resource`` on the endpoint origin
        to find the authorization server, then the AS's
        ``/.well-known/oauth-authorization-server`` for the endpoint set.

        Returns the AS metadata dict (carrying ``authorization_endpoint``,
        ``token_endpoint``, optional ``registration_endpoint``, ``issuer``).
        Raises :class:`MCPProviderOAuthError` if discovery fails.
        """
        endpoint_url = assert_url_allowed(endpoint_url)
        origin = MCPProviderOAuthService._origin(endpoint_url)

        prm_url = urljoin(origin + "/", ".well-known/oauth-protected-resource")
        as_base: str | None = None
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            try:
                prm = await MCPProviderOAuthService._get_json(client, prm_url)
                servers = prm.get("authorization_servers")
                if isinstance(servers, list) and servers:
                    as_base = str(servers[0]).rstrip("/")
            except MCPProviderOAuthError:
                # No protected-resource metadata — fall back to the endpoint
                # origin as the AS base (common for servers that co-locate the AS).
                as_base = None

            if not as_base:
                as_base = origin

            as_base = assert_url_allowed(as_base)
            asm_url = urljoin(
                as_base + "/", ".well-known/oauth-authorization-server"
            )
            try:
                metadata = await MCPProviderOAuthService._get_json(client, asm_url)
            except MCPProviderOAuthError as e:
                raise MCPProviderOAuthError(
                    f"Could not discover the authorization server for this MCP "
                    f"endpoint: {e.message}",
                    status_code=400,
                )

        if not metadata.get("authorization_endpoint") or not metadata.get(
            "token_endpoint"
        ):
            raise MCPProviderOAuthError(
                "The authorization server metadata is missing required "
                "endpoints (authorize / token).",
                status_code=400,
            )
        metadata.setdefault("_as_base", as_base)
        return metadata

    # ------------------------------------------------------------------ #
    # DCR (RFC 7591)                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def register_client(
        as_metadata: dict, redirect_uri: str, resource: str
    ) -> dict:
        """
        Dynamic Client Registration against the AS ``registration_endpoint``.

        Returns ``{client_id, client_secret?}``. Raises
        :class:`MCPProviderOAuthError` (400) with a user-actionable message when
        the AS does not support DCR.
        """
        registration_endpoint = as_metadata.get("registration_endpoint")
        if not registration_endpoint:
            raise MCPProviderOAuthError(
                "This server does not support Dynamic Client Registration; "
                "use a fixed token instead.",
                status_code=400,
            )
        registration_endpoint = assert_url_allowed(registration_endpoint)

        body = {
            "client_name": "Cinna MCP Provider Connection",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        }
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            try:
                resp = await client.post(registration_endpoint, json=body)
            except httpx.HTTPError as e:
                raise MCPProviderOAuthError(
                    f"Dynamic Client Registration request failed: {e}",
                    status_code=400,
                )
        if resp.status_code not in (200, 201):
            raise MCPProviderOAuthError(
                "This server does not support Dynamic Client Registration; "
                "use a fixed token instead.",
                status_code=400,
            )
        data = resp.json()
        client_id = data.get("client_id")
        if not client_id:
            raise MCPProviderOAuthError(
                "Dynamic Client Registration returned no client_id.",
                status_code=400,
            )
        return {
            "client_id": client_id,
            "client_secret": data.get("client_secret"),
        }

    # ------------------------------------------------------------------ #
    # Authorization                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def begin_authorization(
        session: Session,
        credential: Credential,
        user_id: uuid.UUID,
    ) -> str:
        """
        Begin the DCR + authorization flow for an ``oauth_dcr`` credential.

        Discovers the AS, performs DCR (storing ``oauth_client_id`` /
        ``oauth_client_secret``), stores the discovered AS endpoints + PKCE
        verifier, and returns the authorization URL the frontend opens.

        Idempotent on re-authorize: reuses an existing ``oauth_client_id`` when
        present (avoids piling up DCR registrations on every Authorize click).
        """
        data = MCPProviderOAuthService._decrypt(credential)
        endpoint_url = data.get("endpoint_url")
        if not endpoint_url:
            raise MCPProviderOAuthError(
                "Credential has no endpoint URL", status_code=400
            )
        resource = data.get("oauth_resource") or endpoint_url

        as_metadata = await MCPProviderOAuthService.discover_authorization_server(
            endpoint_url
        )
        redirect_uri = settings.MCP_PROVIDER_OAUTH_REDIRECT_URI

        client_id = data.get("oauth_client_id")
        client_secret = data.get("oauth_client_secret")
        if not client_id:
            registration = await MCPProviderOAuthService.register_client(
                as_metadata, redirect_uri, resource
            )
            client_id = registration["client_id"]
            client_secret = registration.get("client_secret")

        # Persist client creds + discovered endpoints; clear any stale error.
        data["oauth_client_id"] = client_id
        data["oauth_client_secret"] = client_secret
        data["oauth_authorization_server"] = as_metadata.get("_as_base")
        data["oauth_authorization_endpoint"] = as_metadata["authorization_endpoint"]
        data["oauth_token_endpoint"] = as_metadata["token_endpoint"]
        data["oauth_resource"] = resource
        data.pop("last_error", None)
        MCPProviderOAuthService._store(session, credential, data)

        verifier, challenge = MCPProviderOAuthService._generate_pkce()
        state = MCPProviderOAuthService._put_state(
            credential.id, user_id, verifier
        )

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # RFC 8707 resource indicator — binds the token to this MCP endpoint.
            "resource": resource,
        }
        scope = data.get("oauth_scope")
        if scope:
            params["scope"] = scope

        sep = "&" if "?" in as_metadata["authorization_endpoint"] else "?"
        return f"{as_metadata['authorization_endpoint']}{sep}{urlencode(params)}"

    @staticmethod
    async def handle_callback(
        session: Session, code: str, state: str
    ) -> Credential:
        """
        Authorization-code callback: validate state, exchange the code (PKCE) for
        tokens, store ``token`` / ``oauth_refresh_token`` /
        ``oauth_token_expires_at`` encrypted, and return the updated credential.
        The caller fires the credential-updated sync.
        """
        state_data = MCPProviderOAuthService._take_state(state)
        credential_id = uuid.UUID(state_data["credential_id"])
        code_verifier = state_data["code_verifier"]

        credential = session.get(Credential, credential_id)
        if credential is None or credential.type != CredentialType.MCP_PROVIDER:
            raise MCPProviderOAuthError("Credential not found", status_code=404)

        data = MCPProviderOAuthService._decrypt(credential)
        token_endpoint = data.get("oauth_token_endpoint")
        client_id = data.get("oauth_client_id")
        if not token_endpoint or not client_id:
            raise MCPProviderOAuthError(
                "Authorization was not started for this credential.",
                status_code=400,
            )

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.MCP_PROVIDER_OAUTH_REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": code_verifier,
            "resource": data.get("oauth_resource") or data.get("endpoint_url"),
        }
        client_secret = data.get("oauth_client_secret")
        if client_secret:
            form["client_secret"] = client_secret

        token_data = await MCPProviderOAuthService._token_request(
            token_endpoint, form
        )
        MCPProviderOAuthService._apply_token_response(data, token_data)
        data.pop("last_error", None)
        MCPProviderOAuthService._store(session, credential, data)
        return credential

    # ------------------------------------------------------------------ #
    # Refresh (pre-stream hook + manual)                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def refresh_access_token(
        session: Session, credential: Credential
    ) -> Credential:
        """
        Refresh the access token via the ``refresh_token`` grant.

        Graceful on failure (mirrors Google refresh): records ``last_error`` on
        the credential (→ status ``error``) and re-raises so the pre-stream hook
        can log and continue with the stale token. ``ValueError`` is raised when
        no refresh token is available (re-authorize required).
        """
        data = MCPProviderOAuthService._decrypt(credential)
        refresh_token = data.get("oauth_refresh_token")
        token_endpoint = data.get("oauth_token_endpoint")
        client_id = data.get("oauth_client_id")
        if not refresh_token:
            raise ValueError("Credential has no refresh token")
        if not token_endpoint or not client_id:
            raise ValueError("Credential has no token endpoint / client id")

        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "resource": data.get("oauth_resource") or data.get("endpoint_url"),
        }
        client_secret = data.get("oauth_client_secret")
        if client_secret:
            form["client_secret"] = client_secret

        try:
            token_data = await MCPProviderOAuthService._token_request(
                token_endpoint, form
            )
        except MCPProviderOAuthError as e:
            data["last_error"] = f"Token refresh failed: {e.message}"
            MCPProviderOAuthService._store(session, credential, data)
            raise

        MCPProviderOAuthService._apply_token_response(data, token_data)
        data.pop("last_error", None)
        MCPProviderOAuthService._store(session, credential, data)
        return credential

    # ------------------------------------------------------------------ #
    # Connectivity probe                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def probe(session: Session, credential: Credential) -> dict:
        """
        Best-effort connectivity probe: open an MCP ``initialize`` +
        ``tools/list`` against the endpoint with the current token. Returns
        ``{"ok": bool, "tools": [...], "error": str|None}``. Goes through the
        egress guard (RD-6).
        """
        data = MCPProviderOAuthService._decrypt(credential)
        endpoint_url = data.get("endpoint_url")
        if not endpoint_url:
            return {"ok": False, "tools": [], "error": "No endpoint URL"}

        try:
            endpoint_url = assert_url_allowed(endpoint_url)
        except EgressBlockedError as e:
            return {"ok": False, "tools": [], "error": str(e)}

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        token = data.get("token")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        init_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "cinna-mcp-probe", "version": "1.0.0"},
            },
        }
        list_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                init_resp = await client.post(
                    endpoint_url, json=init_body, headers=headers
                )
                if init_resp.status_code in (401, 403):
                    return {
                        "ok": False,
                        "tools": [],
                        "error": f"Authentication failed (HTTP {init_resp.status_code}).",
                    }
                if init_resp.status_code >= 400:
                    return {
                        "ok": False,
                        "tools": [],
                        "error": f"initialize returned HTTP {init_resp.status_code}.",
                    }
                # Carry the MCP session id if the server issued one.
                mcp_session = init_resp.headers.get("mcp-session-id")
                if mcp_session:
                    headers["mcp-session-id"] = mcp_session

                list_resp = await client.post(
                    endpoint_url, json=list_body, headers=headers
                )
        except httpx.HTTPError as e:
            # httpx connection errors (DNS failure, offline host, refused
            # connection) frequently carry an empty ``str(e)``, which left the
            # toast reading "Connection failed: " with no detail. Fall back to
            # the exception type and surface the underlying OS cause when present.
            detail = str(e).strip()
            cause = e.__cause__
            if cause is not None and str(cause).strip():
                cause_text = str(cause).strip()
                detail = f"{detail} ({cause_text})" if detail else cause_text
            if not detail:
                detail = type(e).__name__
            return {"ok": False, "tools": [], "error": f"Connection failed: {detail}"}

        tools: list[str] = []
        parsed = MCPProviderOAuthService._parse_jsonrpc(list_resp)
        if parsed is not None:
            result = parsed.get("result") or {}
            for t in result.get("tools", []) or []:
                name = t.get("name") if isinstance(t, dict) else None
                if name:
                    tools.append(str(name))
        return {"ok": True, "tools": tools, "error": None}

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _token_request(token_endpoint: str, form: dict) -> dict:
        token_endpoint = assert_url_allowed(token_endpoint)
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            try:
                resp = await client.post(
                    token_endpoint,
                    data=form,
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as e:
                raise MCPProviderOAuthError(
                    f"Token request failed: {e}", status_code=400
                )
        if resp.status_code != 200:
            raise MCPProviderOAuthError(
                f"Token endpoint returned HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=400,
            )
        try:
            return resp.json()
        except json.JSONDecodeError:
            raise MCPProviderOAuthError(
                "Token endpoint returned a non-JSON response", status_code=400
            )

    @staticmethod
    def _apply_token_response(data: dict, token_data: dict) -> None:
        access_token = token_data.get("access_token")
        if not access_token:
            raise MCPProviderOAuthError(
                "No access token in the token response", status_code=400
            )
        data["token"] = access_token
        expires_in = token_data.get("expires_in")
        if isinstance(expires_in, (int, float)):
            data["oauth_token_expires_at"] = int(time.time() + expires_in)
        # Refresh token rotation: keep the old one if none returned.
        new_refresh = token_data.get("refresh_token")
        if new_refresh:
            data["oauth_refresh_token"] = new_refresh
        scope = token_data.get("scope")
        if scope:
            data["oauth_scope"] = scope

    @staticmethod
    def _decrypt(credential: Credential) -> dict:
        if not credential.encrypted_data:
            return {}
        return json.loads(security.decrypt_field(credential.encrypted_data))

    @staticmethod
    def _store(session: Session, credential: Credential, data: dict) -> None:
        credential.encrypted_data = security.encrypt_field(json.dumps(data))
        session.add(credential)
        session.commit()
        session.refresh(credential)

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    @staticmethod
    async def _get_json(client: httpx.AsyncClient, url: str) -> dict:
        url = assert_url_allowed(url)
        try:
            resp = await client.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise MCPProviderOAuthError(f"GET {url} failed: {e}", status_code=400)
        if resp.status_code != 200:
            raise MCPProviderOAuthError(
                f"GET {url} returned HTTP {resp.status_code}", status_code=400
            )
        try:
            return resp.json()
        except json.JSONDecodeError:
            raise MCPProviderOAuthError(
                f"GET {url} returned non-JSON", status_code=400
            )

    @staticmethod
    def _parse_jsonrpc(resp: httpx.Response) -> dict | None:
        """Parse a JSON-RPC response that may be plain JSON or SSE-framed."""
        ctype = resp.headers.get("content-type", "")
        text = resp.text
        if "text/event-stream" in ctype:
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        continue
            return None
        try:
            return resp.json()
        except json.JSONDecodeError:
            return None
