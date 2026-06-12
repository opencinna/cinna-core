"""
Unit tests for the Account CLI API-proxy exclusion policy.

Pure predicate logic — ``assert_api_proxy_allowed`` and its helpers operate
on strings with no DB, no HTTP, and no background tasks. The end-to-end /
API-observable path (proxy happy path, audit events, rate limiting, response
guards) is covered in ``tests/api/cli/test_account_cli.py`` (see Scenarios
17–20).

Coverage:
  A. Denylist — every EXCLUDED_PREFIX is denied; segment-boundary discipline
     (``users-search`` NOT blocked by ``users``); streaming deny prefixes.
  B. User carve-outs — exact-match allow: GET users/me, GET users/search;
     POST users/me is still blocked; non-exact GET users/anything is blocked.
  C. Method allowlist — ALLOWED_METHODS pass; OPTIONS/HEAD/TRACE denied.
  D. Malformed paths — ``..'', leading non-/api segment, embedded query
     string, backslash, bare non-api path.
  E. Path normalization — trailing slash stripped; caller-included /api/v1
     prefix handled idempotently; single leading slash ensured.
  F. Idempotent normalization smoke test (``_normalize_path`` directly).
  G. Segment boundary helper (``_segment_prefix_match``).
"""
import pytest

from app.core.config import settings
from app.services.cli.account_api_proxy_policy import (
    ALLOWED_METHODS,
    EXCLUDED_PREFIXES,
    STREAMING_DENY,
    USER_PATH_ALLOW_EXACT,
    ApiProxyDenied,
    _segment_prefix_match,
    assert_api_proxy_allowed,
)
from app.services.cli.account_api_proxy_service import AccountApiProxyService

_BASE = settings.API_V1_STR  # /api/v1


def _allowed(method: str, path: str) -> None:
    """Assert that assert_api_proxy_allowed does NOT raise for this pair."""
    assert_api_proxy_allowed(method, path)


def _denied(method: str, path: str, reason: str | None = None) -> ApiProxyDenied:
    """Assert that assert_api_proxy_allowed raises ApiProxyDenied and return it."""
    with pytest.raises(ApiProxyDenied) as exc_info:
        assert_api_proxy_allowed(method, path)
    exc = exc_info.value
    if reason is not None:
        assert exc.reason == reason, (
            f"Expected reason={reason!r}, got {exc.reason!r} for {method} {path}"
        )
    return exc


# ────────────────────────────────────────────────────────────────────────────
# A. Denylist — every EXCLUDED_PREFIX must deny
# ────────────────────────────────────────────────────────────────────────────


class TestExcludedPrefixes:
    """One test per denylist entry; ensures the list is exhaustive."""

    def test_credentials_blocked(self) -> None:
        exc = _denied("GET", f"{_BASE}/credentials", reason="excluded_path")
        assert "credentials" in exc.message.lower() or "excluded" in exc.message.lower()

    def test_credentials_child_blocked(self) -> None:
        _denied("GET", f"{_BASE}/credentials/some-id/reveal", reason="excluded_path")

    def test_ai_credentials_blocked(self) -> None:
        _denied("GET", f"{_BASE}/ai-credentials", reason="excluded_path")

    def test_ai_credentials_child_blocked(self) -> None:
        _denied("POST", f"{_BASE}/ai-credentials/foo/set-default", reason="excluded_path")

    def test_oauth_credentials_blocked(self) -> None:
        _denied("GET", f"{_BASE}/oauth-credentials", reason="excluded_path")

    def test_credential_shares_blocked(self) -> None:
        _denied("POST", f"{_BASE}/credential-shares", reason="excluded_path")

    def test_users_blocked_root(self) -> None:
        _denied("GET", f"{_BASE}/users", reason="excluded_path")

    def test_users_subpath_blocked(self) -> None:
        _denied("DELETE", f"{_BASE}/users/some-id", reason="excluded_path")

    def test_admin_blocked(self) -> None:
        _denied("GET", f"{_BASE}/admin", reason="excluded_path")

    def test_admin_child_blocked(self) -> None:
        _denied("GET", f"{_BASE}/admin/users", reason="excluded_path")

    def test_admin_environments_blocked(self) -> None:
        _denied("GET", f"{_BASE}/admin-environments", reason="excluded_path")

    def test_private_blocked(self) -> None:
        _denied("GET", f"{_BASE}/private", reason="excluded_path")

    def test_private_child_blocked(self) -> None:
        _denied("GET", f"{_BASE}/private/internal", reason="excluded_path")

    def test_cli_blocked_root(self) -> None:
        _denied("POST", f"{_BASE}/cli", reason="excluded_path")

    def test_cli_child_blocked(self) -> None:
        # Recursion prevention: /cli/account/api-proxy blocked
        _denied("POST", f"{_BASE}/cli/account/api-proxy", reason="excluded_path")

    def test_cli_account_tokens_blocked(self) -> None:
        _denied("GET", f"{_BASE}/cli/account/tokens", reason="excluded_path")

    def test_desktop_auth_blocked(self) -> None:
        _denied("POST", f"{_BASE}/desktop-auth", reason="excluded_path")

    def test_desktop_auth_child_blocked(self) -> None:
        _denied("POST", f"{_BASE}/desktop-auth/token", reason="excluded_path")

    def test_app_auth_blocked(self) -> None:
        _denied("POST", f"{_BASE}/app-auth", reason="excluded_path")

    def test_app_sync_blocked(self) -> None:
        _denied("POST", f"{_BASE}/app-sync", reason="excluded_path")

    def test_mfa_blocked(self) -> None:
        _denied("POST", f"{_BASE}/mfa", reason="excluded_path")

    def test_mfa_child_blocked(self) -> None:
        _denied("POST", f"{_BASE}/mfa/totp/setup", reason="excluded_path")

    def test_security_events_blocked(self) -> None:
        _denied("GET", f"{_BASE}/security-events", reason="excluded_path")

    def test_security_events_child_blocked(self) -> None:
        _denied("GET", f"{_BASE}/security-events/123", reason="excluded_path")

    def test_login_blocked(self) -> None:
        _denied("POST", f"{_BASE}/login", reason="excluded_path")

    def test_login_child_blocked(self) -> None:
        _denied("POST", f"{_BASE}/login/access-token", reason="excluded_path")

    def test_oauth_blocked(self) -> None:
        _denied("GET", f"{_BASE}/oauth", reason="excluded_path")

    def test_auth_blocked(self) -> None:
        _denied("GET", f"{_BASE}/auth", reason="excluded_path")

    def test_token_blocked(self) -> None:
        _denied("POST", f"{_BASE}/token", reason="excluded_path")


class TestStreamingDeny:
    """Streaming / create-flow routes are blocked via excluded_method reason."""

    def test_create_flow_stream_blocked(self) -> None:
        exc = _denied("POST", f"{_BASE}/agents/create-flow-stream", reason="excluded_method")
        assert "streaming" in exc.message.lower() or "create-flow" in exc.message.lower()

    def test_create_flow_blocked(self) -> None:
        _denied("POST", f"{_BASE}/agents/create-flow", reason="excluded_method")

    def test_create_flow_child_blocked(self) -> None:
        _denied("POST", f"{_BASE}/agents/create-flow/start", reason="excluded_method")


# ────────────────────────────────────────────────────────────────────────────
# B. Segment boundary — prefix must not bleed across segment boundaries
# ────────────────────────────────────────────────────────────────────────────


class TestSegmentBoundary:
    """A denylist prefix must NOT block a sibling path that STARTS WITH the same
    characters but is a DIFFERENT segment (e.g. ``users-search`` is not
    ``users``)."""

    def test_users_search_sibling_not_blocked(self) -> None:
        """``/users-search`` (hypothetical) must NOT be blocked by the ``users`` prefix."""
        # There is no real /api/v1/users-search route, but the segment boundary
        # discipline must hold. Note: the path must match _SAFE_PATH_RE.
        _allowed("GET", f"{_BASE}/users-search")

    def test_admin_environments_not_blocked_by_admin_via_boundary(self) -> None:
        """``/admin-environments`` is a separate denylist entry, not a child of ``admin``."""
        # It IS on the denylist independently.
        _denied("GET", f"{_BASE}/admin-environments", reason="excluded_path")

    def test_credentials_provider_not_blocked(self) -> None:
        """``/credentials-provider`` (hypothetical sibling) must NOT be blocked by ``credentials``."""
        _allowed("GET", f"{_BASE}/credentials-provider")

    def test_cli_tools_not_blocked_by_cli(self) -> None:
        """``/cli-tools`` (hypothetical sibling) must NOT be blocked by ``cli``."""
        _allowed("GET", f"{_BASE}/cli-tools")

    def test_token_refresh_not_blocked_by_token(self) -> None:
        """``/token-refresh`` (hypothetical sibling) must NOT be blocked by ``token``."""
        _allowed("GET", f"{_BASE}/token-refresh")

    def test_mfa_settings_sibling_not_blocked(self) -> None:
        """``/mfa-settings`` (hypothetical sibling) must NOT be blocked by ``mfa``."""
        _allowed("GET", f"{_BASE}/mfa-settings")


# ────────────────────────────────────────────────────────────────────────────
# C. User carve-outs (exact allow-list carved back in from users-prefix deny)
# ────────────────────────────────────────────────────────────────────────────


class TestUserCarveOuts:
    """GET users/me and GET users/search are allowed; everything else under
    users is still blocked."""

    def test_get_users_me_allowed(self) -> None:
        _allowed("GET", f"{_BASE}/users/me")

    def test_get_users_search_allowed(self) -> None:
        _allowed("GET", f"{_BASE}/users/search")

    def test_post_users_me_blocked(self) -> None:
        """POST /users/me is NOT in the exact allow list → blocked."""
        _denied("POST", f"{_BASE}/users/me", reason="excluded_path")

    def test_patch_users_me_blocked(self) -> None:
        _denied("PATCH", f"{_BASE}/users/me", reason="excluded_path")

    def test_delete_users_me_blocked(self) -> None:
        _denied("DELETE", f"{_BASE}/users/me", reason="excluded_path")

    def test_put_users_search_blocked(self) -> None:
        _denied("PUT", f"{_BASE}/users/search", reason="excluded_path")

    def test_get_users_other_id_blocked(self) -> None:
        """GET /users/<some-uuid> is not in the exact list → blocked by users prefix."""
        import uuid
        _denied("GET", f"{_BASE}/users/{uuid.uuid4()}", reason="excluded_path")

    def test_get_users_root_blocked(self) -> None:
        _denied("GET", f"{_BASE}/users", reason="excluded_path")

    def test_get_users_signup_blocked(self) -> None:
        _denied("POST", f"{_BASE}/users/signup", reason="excluded_path")

    def test_carve_out_const_covers_all_pairs(self) -> None:
        """Smoke check: the USER_PATH_ALLOW_EXACT constant covers exactly the two pairs."""
        methods = {method for method, _ in USER_PATH_ALLOW_EXACT}
        paths = {path for _, path in USER_PATH_ALLOW_EXACT}
        assert methods == {"GET"}
        assert "users/me" in paths
        assert "users/search" in paths
        assert len(USER_PATH_ALLOW_EXACT) == 2


# ────────────────────────────────────────────────────────────────────────────
# D. Method allowlist
# ────────────────────────────────────────────────────────────────────────────


class TestMethodAllowlist:
    """Only GET/POST/PUT/PATCH/DELETE pass the method gate."""

    @pytest.mark.parametrize("method", sorted(ALLOWED_METHODS))
    def test_allowed_method(self, method: str) -> None:
        # Use a definitely-allowed path so the only gate in play is the method.
        _allowed(method, f"{_BASE}/agents")

    def test_options_blocked(self) -> None:
        exc = _denied("OPTIONS", f"{_BASE}/agents", reason="excluded_method")
        assert "options" in exc.message.lower() or "not supported" in exc.message.lower()

    def test_head_blocked(self) -> None:
        _denied("HEAD", f"{_BASE}/agents", reason="excluded_method")

    def test_trace_blocked(self) -> None:
        _denied("TRACE", f"{_BASE}/agents", reason="excluded_method")

    def test_connect_blocked(self) -> None:
        _denied("CONNECT", f"{_BASE}/agents", reason="excluded_method")

    def test_allowed_methods_constant_has_expected_verbs(self) -> None:
        assert ALLOWED_METHODS == frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


# ────────────────────────────────────────────────────────────────────────────
# E. Malformed paths
# ────────────────────────────────────────────────────────────────────────────


class TestMalformedPaths:
    """Paths that are structurally invalid are rejected with malformed_path."""

    def test_dotdot_in_path_blocked(self) -> None:
        """Any ``..`` segment is malformed."""
        _denied("GET", f"{_BASE}/agents/../credentials", reason="malformed_path")

    def test_dotdot_at_root(self) -> None:
        _denied("GET", "/api/../v1/agents", reason="malformed_path")

    def test_bare_dotdot(self) -> None:
        _denied("GET", "..", reason="malformed_path")

    def test_path_without_api_v1_prefix_blocked(self) -> None:
        """A path that doesn't start with /api/v1 is malformed."""
        _denied("GET", "/some/other/path", reason="malformed_path")

    def test_relative_path_without_slash_blocked(self) -> None:
        """A bare string like 'agents' (no leading slash, not normalized) is malformed."""
        _denied("GET", "agents", reason="malformed_path")

    def test_scheme_in_path_blocked(self) -> None:
        """A URL-like path is malformed."""
        _denied("GET", "http://example.com/api/v1/agents", reason="malformed_path")

    def test_allowed_path_passes_shape_check(self) -> None:
        """Sanity: a well-formed /api/v1/agents path passes the shape check."""
        _allowed("GET", f"{_BASE}/agents")

    def test_allowed_path_with_uuid_segment(self) -> None:
        """UUID segments use hex chars + hyphens — allowed."""
        import uuid
        _allowed("GET", f"{_BASE}/agents/{uuid.uuid4()}")

    def test_trailing_slash_is_allowed_shape(self) -> None:
        """Trailing slash is allowed (the caller may send it; rstrip happens inside)."""
        _allowed("GET", f"{_BASE}/agents/")


# ────────────────────────────────────────────────────────────────────────────
# F. Default-allow: a well-known safe path is not denied
# ────────────────────────────────────────────────────────────────────────────


class TestDefaultAllow:
    """Paths not on the denylist and with a valid method must pass through."""

    def test_agents_list_allowed(self) -> None:
        _allowed("GET", f"{_BASE}/agents")

    def test_agents_with_trailing_slash_allowed(self) -> None:
        _allowed("GET", f"{_BASE}/agents/")

    def test_agents_by_id_allowed(self) -> None:
        import uuid
        _allowed("GET", f"{_BASE}/agents/{uuid.uuid4()}")

    def test_knowledge_sources_allowed(self) -> None:
        _allowed("GET", f"{_BASE}/knowledge-sources")

    def test_workspaces_allowed(self) -> None:
        _allowed("GET", f"{_BASE}/workspaces")

    def test_mcp_providers_allowed(self) -> None:
        _allowed("GET", f"{_BASE}/mcp-providers")

    def test_sessions_allowed(self) -> None:
        _allowed("GET", f"{_BASE}/sessions")

    def test_input_tasks_allowed(self) -> None:
        _allowed("POST", f"{_BASE}/input-tasks")


# ────────────────────────────────────────────────────────────────────────────
# G. _segment_prefix_match helper
# ────────────────────────────────────────────────────────────────────────────


class TestSegmentPrefixMatch:
    """Direct tests for the internal segment-boundary helper."""

    def test_exact_match_is_true(self) -> None:
        assert _segment_prefix_match("/api/v1/users", "/api/v1/users") is True

    def test_child_segment_is_true(self) -> None:
        assert _segment_prefix_match("/api/v1/users/me", "/api/v1/users") is True

    def test_deeper_child_is_true(self) -> None:
        assert _segment_prefix_match("/api/v1/users/me/photo", "/api/v1/users") is True

    def test_sibling_with_suffix_is_false(self) -> None:
        """``/api/v1/users-search`` must NOT match ``/api/v1/users``."""
        assert _segment_prefix_match("/api/v1/users-search", "/api/v1/users") is False

    def test_prefix_longer_than_path_is_false(self) -> None:
        assert _segment_prefix_match("/api/v1/use", "/api/v1/users") is False

    def test_unrelated_path_is_false(self) -> None:
        assert _segment_prefix_match("/api/v1/agents", "/api/v1/users") is False


# ────────────────────────────────────────────────────────────────────────────
# H. _normalize_path via AccountApiProxyService (service normalization logic)
# ────────────────────────────────────────────────────────────────────────────


class TestNormalizePath:
    """AccountApiProxyService._normalize_path must produce /api/v1/<path>."""

    def _norm(self, raw: str) -> str:
        return AccountApiProxyService._normalize_path(raw)

    def test_bare_segment_gets_prefix(self) -> None:
        result = self._norm("agents")
        assert result == f"{_BASE}/agents"

    def test_leading_slash_normalized(self) -> None:
        result = self._norm("/agents")
        assert result == f"{_BASE}/agents"

    def test_caller_included_prefix_idempotent(self) -> None:
        """A caller that already included /api/v1 must not get double-prefix."""
        result = self._norm(f"{_BASE}/agents")
        assert result == f"{_BASE}/agents"

    def test_caller_prefix_with_leading_slash(self) -> None:
        result = self._norm(f"/{_BASE.lstrip('/')}/agents")
        assert result == f"{_BASE}/agents"

    def test_double_slash_collapsed(self) -> None:
        result = self._norm("//agents")
        assert result == f"{_BASE}/agents"

    def test_trailing_slash_preserved_in_normalization(self) -> None:
        """_normalize_path does NOT strip trailing slash — the policy gate does."""
        result = self._norm("agents/")
        assert result == f"{_BASE}/agents/"

    def test_dotdot_raises_api_proxy_denied(self) -> None:
        with pytest.raises(ApiProxyDenied) as exc_info:
            self._norm("../credentials")
        assert exc_info.value.reason == "malformed_path"

    def test_query_string_in_path_raises(self) -> None:
        with pytest.raises(ApiProxyDenied) as exc_info:
            self._norm("agents?foo=bar")
        assert exc_info.value.reason == "malformed_path"

    def test_backslash_raises(self) -> None:
        with pytest.raises(ApiProxyDenied) as exc_info:
            self._norm("agents\\credentials")
        assert exc_info.value.reason == "malformed_path"

    def test_fragment_in_path_raises(self) -> None:
        with pytest.raises(ApiProxyDenied) as exc_info:
            self._norm("agents#section")
        assert exc_info.value.reason == "malformed_path"


# ────────────────────────────────────────────────────────────────────────────
# I. Constants sanity checks (assert module-level expectations)
# ────────────────────────────────────────────────────────────────────────────


class TestConstants:
    """Smoke tests that guard the denylist constants haven't been silently trimmed."""

    def test_excluded_prefixes_non_empty(self) -> None:
        assert len(EXCLUDED_PREFIXES) >= 14, (
            "EXCLUDED_PREFIXES should cover at least 14 categories; "
            f"found {len(EXCLUDED_PREFIXES)}: {EXCLUDED_PREFIXES}"
        )

    def test_streaming_deny_non_empty(self) -> None:
        assert len(STREAMING_DENY) >= 1

    def test_all_excluded_prefixes_are_strings(self) -> None:
        for p in EXCLUDED_PREFIXES:
            assert isinstance(p, str) and p, f"Prefix must be non-empty string, got {p!r}"

    def test_credentials_in_excluded(self) -> None:
        assert "credentials" in EXCLUDED_PREFIXES

    def test_cli_in_excluded(self) -> None:
        assert "cli" in EXCLUDED_PREFIXES

    def test_users_in_excluded(self) -> None:
        assert "users" in EXCLUDED_PREFIXES

    def test_mfa_in_excluded(self) -> None:
        assert "mfa" in EXCLUDED_PREFIXES
