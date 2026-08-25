"""App MCP helpers — mint a real access token, then ask the verifier about it.

Two halves, and they are deliberately different in kind.

``obtain_app_mcp_access_token`` is ordinary Rule-1 test setup: it drives the
real OAuth dance over HTTP (DCR → authorize → consent → token exchange) and
returns the ``access_token`` an MCP client would hold. Nothing is faked; the
row it produces in ``app_mcp_token`` is the row a real client would have.

``verify_app_mcp_token`` is a **documented Rule-1 exemption**, the same shape
as ``tests/utils/platform_token.py`` and ``flush_pending_bindings`` in
``tests/utils/server_channel.py``: it calls ``AppMCPTokenVerifier.verify_token``
directly. The reason it has to is structural — the verifier is not behind an
API route. It is the ``token_verifier`` the MCP ASGI app is constructed with,
so the only HTTP path to it is a full StreamableHTTP session against
``/mcp/app/mcp``, whose 4xx surface is dominated by protocol negotiation rather
than by the one authorization decision under test. Asserting "this token is
usable / this token is refused" against the verifier itself is both the exact
question and the only unambiguous way to ask it.

What that exemption is *not* allowed to do is fake anything. The token comes
from the real flow above, the channel is switched on and off through the real
admin routes, and grants are issued through the real grant route — so every
input to the decision is API-produced and only the final call is direct.
"""
import asyncio
import uuid

from fastapi.testclient import TestClient

from tests.utils.mcp import MCP_BASE_URL, approve_consent, generate_pkce_pair

#: The App MCP resource URL, always rooted at ``MCP_BASE_URL``.
APP_MCP_RESOURCE = f"{MCP_BASE_URL}/app/mcp"

_REDIRECT_URI = "http://localhost:3000/callback"


def obtain_app_mcp_access_token(
    client: TestClient,
    token_headers: dict[str, str],
    *,
    client_name: str | None = None,
) -> str:
    """Run the full App MCP OAuth flow and return the bearer access token.

    The token belongs to whoever ``token_headers`` authenticates, because that
    is who approves the consent step — which is exactly the binding the
    availability check is keyed on.

    Mirrors ``tests/api/app_mcp/app_mcp_oauth_flow_test.py``'s phases; that
    file owns proving the flow itself works, this one just needs its output.
    """
    name = client_name or f"App MCP Test Client {uuid.uuid4().hex[:8]}"
    code_verifier, code_challenge = generate_pkce_pair()

    registration = client.post(
        "/mcp/oauth/register",
        json={
            "client_name": name,
            "redirect_uris": [_REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "resource": APP_MCP_RESOURCE,
        },
    )
    assert registration.status_code == 201, registration.text
    oauth_client = registration.json()

    authorize = client.get(
        "/mcp/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": oauth_client["client_id"],
            "redirect_uri": _REDIRECT_URI,
            "scope": "mcp:tools",
            "state": "app-mcp-availability",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "resource": APP_MCP_RESOURCE,
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 302, authorize.text
    location = authorize.headers.get("location", "")
    assert "nonce=" in location, location
    nonce = location.split("nonce=")[1].split("&")[0]

    approval = approve_consent(client, token_headers, nonce)
    redirect_url = approval["redirect_url"]
    assert "code=" in redirect_url, redirect_url
    auth_code = redirect_url.split("code=")[1].split("&")[0]

    exchange = client.post(
        "/mcp/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": _REDIRECT_URI,
            "client_id": oauth_client["client_id"],
            "client_secret": oauth_client["client_secret"],
            "code_verifier": code_verifier,
            "resource": APP_MCP_RESOURCE,
        },
    )
    assert exchange.status_code == 200, exchange.text
    access_token = exchange.json()["access_token"]
    assert access_token
    return access_token


def verify_app_mcp_token(token: str) -> bool:
    """Whether the App MCP server would accept ``token`` right now.

    Documented Rule-1 exemption — see the module docstring for why there is no
    HTTP path that asks this question cleanly.

    ``True`` means the verifier returned an ``AccessToken``. ``False`` covers
    every refusal the verifier has, and that conflation is the *product*
    behaviour rather than a shortcut in the helper: an invalid token and a
    token whose owner may not use App MCP are deliberately indistinguishable,
    or the response becomes an oracle for the server's channel configuration.
    """
    from app.mcp.app_token_verifier import AppMCPTokenVerifier

    async def _run():
        return await AppMCPTokenVerifier().verify_token(token)

    return asyncio.run(_run()) is not None


def reset_app_mcp_availability_cache() -> None:
    """Drop the verifier's per-user availability cache.

    Process-global state, like the channel debug buffer: without a reset a
    decision cached by one test would answer another test's question. Tests
    that assert on a *revocation* should also set the TTL to ``0`` (see
    ``settings.APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS``) rather than rely on
    this — clearing proves nothing about whether the TTL path works.
    """
    from app.mcp.app_token_verifier import reset_availability_cache

    reset_availability_cache()


__all__ = [
    "APP_MCP_RESOURCE",
    "obtain_app_mcp_access_token",
    "verify_app_mcp_token",
    "reset_app_mcp_availability_cache",
]
