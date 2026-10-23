"""Every backend HTTP response tells search engines not to index it.

Capability URLs (artifact content, agent-start, discovery) carry their
credential in the path, so the backend stamps ``X-Robots-Tag`` on every
response, errors and middleware-written responses included. The directive is
spelled out here (not imported) so a change to it is a visible contract change:
frontend/index.html and the nginx ``location /`` blocks carry the same string.
"""

from fastapi.testclient import TestClient

from app.core.config import settings

DIRECTIVE = "noindex, nofollow, noarchive, nosnippet, noimageindex"
_BOGUS_CAP = "not-a-real-capability-token"


def _assert_noindex(r, expected_status: int | tuple[int, ...]) -> None:
    statuses = (
        expected_status if isinstance(expected_status, tuple) else (expected_status,)
    )
    assert r.status_code in statuses, (r.status_code, r.text[:200])
    assert r.headers.get("x-robots-tag") == DIRECTIVE
    # Exactly one header line — inner values are overwritten, never appended.
    assert len(r.headers.get_list("x-robots-tag")) == 1


def test_every_backend_surface_carries_noindex(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    """
    One request per backend surface, success and failure alike:
      1. /api/v1 success (authed) and the public health check
      2. /api/v1 401 (no token) and 404 (unknown route)
      3. /a/ artifact content: 404 for a bogus capability, OPTIONS preflight
         (answered by ArtifactContentMiddleware itself)
      4. /agent-start (root) and its /api/agent-start alias
      5. /.well-known discovery
      6. mounted sub-apps: /mcp and the Socket.IO /ws mount
    """
    api = settings.API_V1_STR

    # ── Phase 1: /api/v1 success ──────────────────────────────────────
    _assert_noindex(
        client.get(f"{api}/users/me", headers=normal_user_token_headers), 200
    )
    _assert_noindex(client.get(f"{api}/utils/health-check/"), 200)

    # ── Phase 2: /api/v1 errors ───────────────────────────────────────
    _assert_noindex(client.get(f"{api}/users/me"), 401)
    _assert_noindex(client.get(f"{api}/this-route-does-not-exist"), 404)

    # ── Phase 3: /a/ artifact content ─────────────────────────────────
    _assert_noindex(client.get(f"/a/v/{_BOGUS_CAP}/index.html"), 404)
    _assert_noindex(client.get(f"/a/z/{_BOGUS_CAP}"), 404)
    preflight = client.options(
        f"/a/v/{_BOGUS_CAP}/index.html",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "range",
        },
    )
    _assert_noindex(preflight, 204)
    # The /a/ CORS rewrite still holds next to the robots header.
    assert preflight.headers["access-control-allow-origin"] == "*"

    # ── Phase 4: Local Agent Kit entrypoint ───────────────────────────
    _assert_noindex(client.get("/agent-start"), 200)
    _assert_noindex(client.get("/api/agent-start/version"), 200)

    # ── Phase 5: /.well-known discovery ───────────────────────────────
    _assert_noindex(client.get("/.well-known/cinna-desktop"), 200)

    # ── Phase 6: mounted sub-apps ─────────────────────────────────────
    # Whatever the mount answers (no session / no auth), the header is there.
    _assert_noindex(client.get("/mcp/not-a-connector/mcp"), tuple(range(200, 600)))
    _assert_noindex(
        client.get("/ws/socket.io/?EIO=4&transport=polling"), tuple(range(200, 600))
    )
