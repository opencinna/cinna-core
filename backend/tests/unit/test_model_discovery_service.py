"""Unit tests for model_discovery_service.py — service logic with mocked HTTP.

No real HTTP calls are made. Provider lister functions are patched to return
fixed lists or raise exceptions, so the logic in discover_models_for_credential
and refresh_all_credentials is tested in isolation.

Coverage:
  1. Anthropic happy path  — discovered_models, models_discovered_at set; error cleared
  2. OpenAI happy path     — same persistence assertions
  3. Google happy path     — name strip ("models/" prefix) handled by the lister
  4. OpenAI-compatible happy path — base_url present → models populated
  5. OAuth skip            — sk-ant-oat* → models_discovery_error="oauth_token_unsupported"
  6. MiniMax skip          — type minimax → models_discovery_error="no_list_endpoint"
  7. OpenAI-compatible no base_url → "no_base_url" error; prior list unchanged
  8. 401/403 HTTP error    → models_discovery_error="invalid_key"; prior list unchanged
  9. Dedup of duplicate model ids
  10. refresh_all_credentials batch isolation — one raising credential doesn't abort others
  11. refresh_all_credentials returns success count (only cleared-error credentials counted)
  12. dispatch_model_deprecation_notifications transition tracker (_warned_env_ids)

probe_models coverage (added with the Test-Connection feature):
  13. probe_models anthropic happy path → ok=True, reason=None, models deduped
  14. probe_models OAuth token → ok=True, reason=oauth_token_unsupported, models=[]
  15. probe_models minimax → ok=True, reason=no_list_endpoint, models=[]
  16. probe_models openai_compatible no base_url → ok=True, reason=no_base_url, models=[]
  17. probe_models 401/403 → ok=False, reason=invalid_key, models=[]
  18. probe_models non-auth HTTP error propagates
  19. ProbeResult.is_skip property — True for skip reasons, False for ok/invalid_key
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.models.credentials.ai_credential import AICredential, AICredentialType
from app.services.credentials.model_discovery_service import (
    OAUTH_TOKEN_UNSUPPORTED,
    ERROR_INVALID_KEY,
    SKIP_REASONS,
    ProbeResult,
    discover_models_for_credential,
    probe_models,
    refresh_all_credentials,
)


# ---------------------------------------------------------------------------
# Test helpers — build minimal AICredential-like objects without a real DB
# ---------------------------------------------------------------------------

def _make_credential(
    cred_type: AICredentialType,
    api_key: str = "sk-test-key-000",
    base_url: str | None = None,
    model: str | None = None,
    discovered_models: list[str] | None = None,
    models_discovery_error: str | None = None,
) -> MagicMock:
    """Build a mock AICredential for unit testing.

    SQLModel table models cannot be constructed via __new__ without a live DB
    session (SQLAlchemy instrumentation requires _sa_instance_state). We use
    MagicMock(spec=AICredential) instead, which satisfies attribute access
    checks while remaining independent of the database.

    The service reads .type, calls ai_credentials_service.decrypt_credential
    (patched per-test), and mutates discovered_models / models_discovered_at /
    models_discovery_error on the same object. All three are set as real
    attributes here so the service's assignment is observable.
    """
    cred = MagicMock(spec=AICredential)
    cred.id = uuid.uuid4()
    cred.type = cred_type
    cred.encrypted_data = "placeholder-not-used-due-to-mock"
    cred.discovered_models = discovered_models
    cred.models_discovered_at = None
    cred.models_discovery_error = models_discovery_error
    cred.owner_id = uuid.uuid4()
    cred.name = "test-cred"
    cred.is_default = False
    cred.expiry_notification_date = None
    return cred


def _mock_session():
    """Return a MagicMock that silently accepts .add() and .exec()."""
    session = MagicMock()
    session.exec.return_value = MagicMock()
    return session


def _decrypt_returning(api_key: str, base_url: str | None = None, model: str | None = None):
    """Return a patcher for ai_credentials_service.decrypt_credential."""
    data = MagicMock()
    data.api_key = api_key
    data.base_url = base_url
    data.model = model
    return patch(
        "app.services.credentials.model_discovery_service.ai_credentials_service.decrypt_credential",
        return_value=data,
    )


def _run(coro):
    """Run a coroutine synchronously in tests.

    Uses asyncio.run() rather than get_event_loop().run_until_complete() so
    the helper works correctly in Python 3.10+ where get_event_loop() no
    longer creates a new loop implicitly when called from a non-async context.
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Happy-path tests per provider
# ---------------------------------------------------------------------------

class TestDiscoverModelsHappyPath:
    """discover_models_for_credential succeeds and persists the three columns."""

    def test_anthropic_happy_path(self):
        """Anthropic key returns list; discovered_models/at set; error cleared."""
        cred = _make_credential(
            AICredentialType.ANTHROPIC,
            api_key="sk-ant-api03-legit",
            models_discovery_error="previous-error",  # must be cleared on success
        )
        session = _mock_session()
        model_list = ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4"]

        with (
            _decrypt_returning("sk-ant-api03-legit"),
            patch(
                "app.services.credentials.model_discovery_service._list_anthropic_models",
                return_value=model_list,
            ),
            patch("anyio.to_thread.run_sync", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a))),
        ):
            result = _run(discover_models_for_credential(session, cred))

        assert result == model_list
        assert cred.discovered_models == model_list
        assert cred.models_discovered_at is not None
        assert cred.models_discovery_error is None
        session.add.assert_called_with(cred)

    def test_openai_happy_path(self):
        """OpenAI key returns list; columns set."""
        cred = _make_credential(AICredentialType.OPENAI, api_key="sk-openai-test")
        session = _mock_session()
        model_list = ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-5"]

        with (
            _decrypt_returning("sk-openai-test"),
            patch(
                "app.services.credentials.model_discovery_service._list_openai_models",
                return_value=model_list,
            ),
            patch("anyio.to_thread.run_sync", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a))),
        ):
            result = _run(discover_models_for_credential(session, cred))

        assert result == model_list
        assert cred.discovered_models == model_list
        assert cred.models_discovered_at is not None
        assert cred.models_discovery_error is None

    def test_google_happy_path(self):
        """Google key returns list with stripped 'models/' prefix; columns set."""
        cred = _make_credential(AICredentialType.GOOGLE, api_key="AIza-google-key")
        session = _mock_session()
        # After the lister strips "models/" prefix:
        model_list = ["gemini-2.5-flash", "gemini-2.5-pro"]

        with (
            _decrypt_returning("AIza-google-key"),
            patch(
                "app.services.credentials.model_discovery_service._list_google_models",
                return_value=model_list,
            ),
            patch("anyio.to_thread.run_sync", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a))),
        ):
            result = _run(discover_models_for_credential(session, cred))

        assert result == model_list
        assert cred.discovered_models == model_list
        assert cred.models_discovery_error is None

    def test_openai_compatible_with_base_url(self):
        """openai_compatible with base_url → list populated."""
        cred = _make_credential(
            AICredentialType.OPENAI_COMPATIBLE,
            api_key="sk-compat-key",
            base_url="https://api.mymodel.com/v1",
        )
        session = _mock_session()
        model_list = ["my-custom-model-v1", "my-custom-model-v2"]

        with (
            _decrypt_returning("sk-compat-key", base_url="https://api.mymodel.com/v1"),
            patch(
                "app.services.credentials.model_discovery_service._list_openai_compatible_models",
                return_value=model_list,
            ),
            patch("anyio.to_thread.run_sync", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a))),
        ):
            result = _run(discover_models_for_credential(session, cred))

        assert result == model_list
        assert cred.discovered_models == model_list
        assert cred.models_discovery_error is None


# ---------------------------------------------------------------------------
# Skip / benign-error paths
# ---------------------------------------------------------------------------

class TestDiscoverModelsSkipPaths:
    """Intentional skips record a coarse reason code; prior discovered_models unchanged."""

    def test_oauth_token_skipped(self):
        """sk-ant-oat* key → OAUTH_TOKEN_UNSUPPORTED; discovered_models unchanged."""
        prior_list = ["claude-sonnet-4-6"]
        cred = _make_credential(
            AICredentialType.ANTHROPIC,
            api_key="sk-ant-oat01-some-oauth-token",
            discovered_models=prior_list,
        )
        session = _mock_session()

        with _decrypt_returning("sk-ant-oat01-some-oauth-token"):
            result = _run(discover_models_for_credential(session, cred))

        assert cred.models_discovery_error == OAUTH_TOKEN_UNSUPPORTED
        assert cred.discovered_models == prior_list  # unchanged
        assert result == prior_list
        session.add.assert_called_with(cred)

    def test_minimax_skipped_no_list_endpoint(self):
        """MiniMax has no list endpoint → error='no_list_endpoint'."""
        cred = _make_credential(AICredentialType.MINIMAX, api_key="mm-test-key")
        session = _mock_session()

        with _decrypt_returning("mm-test-key"):
            result = _run(discover_models_for_credential(session, cred))

        assert cred.models_discovery_error == "no_list_endpoint"
        assert cred.discovered_models is None  # no prior data; unchanged
        assert result == []

    def test_openai_compatible_no_base_url_skipped(self):
        """openai_compatible with no base_url → 'no_base_url' error; prior list preserved."""
        prior_list = ["my-model"]
        cred = _make_credential(
            AICredentialType.OPENAI_COMPATIBLE,
            api_key="sk-key",
            discovered_models=prior_list,
        )
        session = _mock_session()

        with _decrypt_returning("sk-key", base_url=None):
            result = _run(discover_models_for_credential(session, cred))

        assert cred.models_discovery_error == "no_base_url"
        assert cred.discovered_models == prior_list  # unchanged
        assert result == prior_list


# ---------------------------------------------------------------------------
# HTTP error paths
# ---------------------------------------------------------------------------

class TestDiscoverModelsHttpErrors:
    """401/403 HTTP errors → 'invalid_key'; other HTTP errors propagate up."""

    def _make_http_status_error(self, status_code: int) -> httpx.HTTPStatusError:
        request = httpx.Request("GET", "https://api.anthropic.com/v1/models")
        response = httpx.Response(status_code, request=request)
        return httpx.HTTPStatusError(
            f"Client error '{status_code}'", request=request, response=response
        )

    def test_401_records_invalid_key(self):
        """401 response → models_discovery_error='invalid_key'; prior list unchanged."""
        prior_list = ["claude-sonnet-4-6"]
        cred = _make_credential(
            AICredentialType.ANTHROPIC,
            api_key="sk-ant-api03-bad-key",
            discovered_models=prior_list,
        )
        session = _mock_session()
        exc = self._make_http_status_error(401)

        with (
            _decrypt_returning("sk-ant-api03-bad-key"),
            patch(
                "app.services.credentials.model_discovery_service._list_anthropic_models",
                side_effect=exc,
            ),
            patch("anyio.to_thread.run_sync", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a))),
        ):
            result = _run(discover_models_for_credential(session, cred))

        assert cred.models_discovery_error == "invalid_key"
        assert cred.discovered_models == prior_list
        assert result == prior_list

    def test_403_records_invalid_key(self):
        """403 response → models_discovery_error='invalid_key'."""
        cred = _make_credential(AICredentialType.OPENAI, api_key="sk-openai-forbidden")
        session = _mock_session()
        exc = self._make_http_status_error(403)

        with (
            _decrypt_returning("sk-openai-forbidden"),
            patch(
                "app.services.credentials.model_discovery_service._list_openai_models",
                side_effect=exc,
            ),
            patch("anyio.to_thread.run_sync", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a))),
        ):
            result = _run(discover_models_for_credential(session, cred))

        assert cred.models_discovery_error == "invalid_key"

    def test_non_auth_http_error_propagates(self):
        """Non-401/403 HTTP errors (e.g. 500) propagate to the caller."""
        cred = _make_credential(AICredentialType.ANTHROPIC, api_key="sk-ant-api03-server-error")
        session = _mock_session()
        exc = self._make_http_status_error(500)

        with (
            _decrypt_returning("sk-ant-api03-server-error"),
            patch(
                "app.services.credentials.model_discovery_service._list_anthropic_models",
                side_effect=exc,
            ),
            patch("anyio.to_thread.run_sync", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a))),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                _run(discover_models_for_credential(session, cred))


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

class TestDiscoverModelsDedup:
    """Duplicate model IDs in a provider response are deduplicated while preserving order."""

    def test_duplicate_ids_are_deduplicated(self):
        cred = _make_credential(AICredentialType.ANTHROPIC, api_key="sk-ant-api03-test")
        session = _mock_session()
        raw_list = ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4"]

        with (
            _decrypt_returning("sk-ant-api03-test"),
            patch(
                "app.services.credentials.model_discovery_service._list_anthropic_models",
                return_value=raw_list,
            ),
            patch("anyio.to_thread.run_sync", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a))),
        ):
            result = _run(discover_models_for_credential(session, cred))

        assert result == ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4"]
        assert cred.discovered_models == result


# ---------------------------------------------------------------------------
# refresh_all_credentials — batch isolation
# ---------------------------------------------------------------------------

class TestRefreshAllCredentials:
    """refresh_all_credentials is failure-isolated: one bad credential doesn't abort others."""

    def _make_select_result(self, credentials: list[AICredential]):
        """Mock the session.exec(select(AICredential)).all() call chain."""
        exec_result = MagicMock()
        exec_result.all.return_value = credentials
        return exec_result

    def test_batch_isolation_one_failure(self):
        """One credential that raises an unexpected error does not abort the batch.

        Both credentials are attempted; the failing one gets a coarse error recorded
        and the successful one gets its models persisted.
        """
        cred_ok = _make_credential(AICredentialType.OPENAI, api_key="sk-openai-ok")
        cred_fail = _make_credential(AICredentialType.ANTHROPIC, api_key="sk-ant-api03-will-fail")

        session = MagicMock()
        session.exec.return_value.all.return_value = [cred_fail, cred_ok]
        session.get.return_value = cred_fail  # used in the error-recovery path

        ok_models = ["gpt-5.4-nano"]

        def _decrypt(cred):
            data = MagicMock()
            if cred is cred_ok:
                data.api_key = "sk-openai-ok"
            else:
                data.api_key = "sk-ant-api03-will-fail"
            data.base_url = None
            data.model = None
            return data

        def _lister_openai(api_key):
            return ok_models

        def _lister_anthropic(api_key):
            raise RuntimeError("Network failure!")

        with (
            patch(
                "app.services.credentials.model_discovery_service.ai_credentials_service.decrypt_credential",
                side_effect=_decrypt,
            ),
            patch(
                "app.services.credentials.model_discovery_service._list_anthropic_models",
                side_effect=_lister_anthropic,
            ),
            patch(
                "app.services.credentials.model_discovery_service._list_openai_models",
                side_effect=_lister_openai,
            ),
            patch(
                "anyio.to_thread.run_sync",
                new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a)),
            ),
        ):
            success_count = _run(refresh_all_credentials(session))

        # The failing credential should not prevent the success being counted.
        assert success_count >= 1

        # The successful credential should have its models persisted.
        assert cred_ok.discovered_models == ok_models
        assert cred_ok.models_discovery_error is None

        # The failing credential gets a coarse error reason code (class name).
        assert cred_fail.models_discovery_error == "RuntimeError"

    def test_empty_credentials_returns_zero(self):
        """No credentials → returns 0 without error."""
        session = MagicMock()
        session.exec.return_value.all.return_value = []

        result = _run(refresh_all_credentials(session))
        assert result == 0

    def test_all_success_returns_full_count(self):
        """When all credentials discover successfully, count equals len(credentials)."""
        cred_a = _make_credential(AICredentialType.OPENAI, api_key="sk-openai-a")
        cred_b = _make_credential(AICredentialType.OPENAI, api_key="sk-openai-b")

        session = MagicMock()
        session.exec.return_value.all.return_value = [cred_a, cred_b]

        def _decrypt(cred):
            data = MagicMock()
            data.api_key = "sk-openai-valid"
            data.base_url = None
            data.model = None
            return data

        with (
            patch(
                "app.services.credentials.model_discovery_service.ai_credentials_service.decrypt_credential",
                side_effect=_decrypt,
            ),
            patch(
                "app.services.credentials.model_discovery_service._list_openai_models",
                return_value=["gpt-5"],
            ),
            patch(
                "anyio.to_thread.run_sync",
                new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a)),
            ),
        ):
            count = _run(refresh_all_credentials(session))

        assert count == 2


# ---------------------------------------------------------------------------
# dispatch_model_deprecation_notifications — transition tracker
# ---------------------------------------------------------------------------

class TestDispatchModelDeprecationNotificationsTransition:
    """_warned_env_ids ensures notifications fire only on transition into warning state."""

    def test_notification_fires_on_first_warning_not_on_repeat(self):
        """An environment that is newly warned triggers a notification.
        A second call with the same env still in warning state does NOT re-fire.
        """
        import app.services.credentials.model_discovery_service as _mod

        env_id = str(uuid.uuid4())
        agent_id = uuid.uuid4()
        owner_id = uuid.uuid4()

        # Clear the transition tracker so this test is independent.
        _mod._warned_env_ids.clear()

        env = MagicMock()
        env.id = uuid.UUID(env_id)
        env.agent_id = agent_id
        env.instance_name = "prod"

        agent = MagicMock()
        agent.id = agent_id
        agent.owner_id = owner_id
        agent.name = "My Agent"

        # Build a health object that has_warning=True
        from app.models.environments.environment import ModelHealthPublic, ModelHealthMode

        warned_health = ModelHealthPublic(
            has_warning=True,
            modes=[
                ModelHealthMode(
                    mode="building",
                    model="claude-3-7-sonnet-20250219",
                    status="retired_override",
                    cause="frozen_override",
                    cta="Edit or clear the model override, then restart.",
                )
            ],
        )

        session = MagicMock()
        session.exec.return_value.all.return_value = [env]
        session.get.return_value = agent

        notify_mock = AsyncMock(return_value=None)

        # SystemNotificationService and evaluate_environment are imported locally
        # inside dispatch_model_deprecation_notifications (to break circular
        # imports), so we patch at their defining module paths.
        with (
            patch(
                "app.services.environments.model_health_service.evaluate_environment",
                return_value=warned_health,
            ),
            patch(
                "app.services.notifications.notification_service.SystemNotificationService.notify",
                notify_mock,
            ),
        ):
            # First run — should fire.
            dispatched_first = _run(
                _mod.dispatch_model_deprecation_notifications(session)
            )
            # Second run — env still warned; should NOT re-fire.
            dispatched_second = _run(
                _mod.dispatch_model_deprecation_notifications(session)
            )

        assert dispatched_first == 1
        assert dispatched_second == 0
        assert notify_mock.call_count == 1

        # Cleanup: reset module-level state so other tests are not affected.
        _mod._warned_env_ids.clear()

    def test_warning_cleared_resets_transition(self):
        """Once an environment's health clears, _warned_env_ids removes it,
        allowing the notification to fire again if the env re-enters a warning."""
        import app.services.credentials.model_discovery_service as _mod

        env_id = str(uuid.uuid4())

        env = MagicMock()
        env.id = uuid.UUID(env_id)
        env.agent_id = uuid.uuid4()
        env.instance_name = "staging"

        agent = MagicMock()
        agent.id = env.agent_id
        agent.owner_id = uuid.uuid4()
        agent.name = "Agent"

        from app.models.environments.environment import ModelHealthPublic, ModelHealthMode

        warned_health = ModelHealthPublic(
            has_warning=True,
            modes=[
                ModelHealthMode(
                    mode="conversation", model="gpt-4-0314", status="retired_override",
                    cause="frozen_override",
                )
            ],
        )
        healthy = ModelHealthPublic(has_warning=False, modes=[])

        session = MagicMock()
        session.exec.return_value.all.return_value = [env]
        session.get.return_value = agent

        notify_mock = AsyncMock(return_value=None)
        _mod._warned_env_ids.clear()

        # Patch at the defining module paths (these functions are imported locally
        # inside dispatch_model_deprecation_notifications to break circular imports).
        with patch(
            "app.services.notifications.notification_service.SystemNotificationService.notify",
            notify_mock,
        ):
            # First run: warned → fires + adds to tracker.
            with patch(
                "app.services.environments.model_health_service.evaluate_environment",
                return_value=warned_health,
            ):
                _run(_mod.dispatch_model_deprecation_notifications(session))

            # Second run: health cleared → removes from tracker.
            with patch(
                "app.services.environments.model_health_service.evaluate_environment",
                return_value=healthy,
            ):
                _run(_mod.dispatch_model_deprecation_notifications(session))

            # Third run: warned again → fires again (transition detected).
            with patch(
                "app.services.environments.model_health_service.evaluate_environment",
                return_value=warned_health,
            ):
                _run(_mod.dispatch_model_deprecation_notifications(session))

        # Should have fired twice (first and third runs).
        assert notify_mock.call_count == 2

        _mod._warned_env_ids.clear()


# ---------------------------------------------------------------------------
# probe_models — DB-free provider probe (Test Connection shared path)
# ---------------------------------------------------------------------------

class TestProbeModels:
    """probe_models dispatches to the right lister and maps HTTP errors correctly.

    All blocking I/O is intercepted by patching the private lister functions
    and anyio.to_thread.run_sync (the latter routes calls synchronously, as in
    the other tests above).
    """

    def _anyio_sync(self):
        """Patch anyio.to_thread.run_sync to call functions synchronously."""
        return patch(
            "anyio.to_thread.run_sync",
            new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a)),
        )

    def _make_http_status_error(self, status_code: int) -> httpx.HTTPStatusError:
        request = httpx.Request("GET", "https://api.anthropic.com/v1/models")
        response = httpx.Response(status_code, request=request)
        return httpx.HTTPStatusError(
            f"Client error '{status_code}'", request=request, response=response
        )

    # ── Happy paths ───────────────────────────────────────────────────────

    def test_anthropic_happy_path(self):
        """Anthropic probe: ok=True, reason=None, models returned (deduped)."""
        raw = ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-sonnet-4-6"]  # dup
        with (
            patch(
                "app.services.credentials.model_discovery_service._list_anthropic_models",
                return_value=raw,
            ),
            self._anyio_sync(),
        ):
            result = _run(probe_models(AICredentialType.ANTHROPIC, "sk-ant-api03-test"))

        assert result.ok is True
        assert result.reason is None
        assert result.models == ["claude-sonnet-4-6", "claude-haiku-4-5"]  # deduped
        assert result.is_skip is False

    def test_openai_happy_path(self):
        """OpenAI probe: ok=True, models returned."""
        with (
            patch(
                "app.services.credentials.model_discovery_service._list_openai_models",
                return_value=["gpt-5.4-nano", "gpt-5.4-mini"],
            ),
            self._anyio_sync(),
        ):
            result = _run(probe_models(AICredentialType.OPENAI, "sk-openai-test"))

        assert result.ok is True
        assert result.reason is None
        assert result.models == ["gpt-5.4-nano", "gpt-5.4-mini"]

    def test_google_happy_path(self):
        """Google probe: ok=True, models returned (prefix stripping is in the lister)."""
        with (
            patch(
                "app.services.credentials.model_discovery_service._list_google_models",
                return_value=["gemini-2.5-flash", "gemini-2.5-pro"],
            ),
            self._anyio_sync(),
        ):
            result = _run(probe_models(AICredentialType.GOOGLE, "AIza-test-key"))

        assert result.ok is True
        assert result.models == ["gemini-2.5-flash", "gemini-2.5-pro"]

    def test_openai_compatible_with_base_url(self):
        """openai_compatible probe with base_url: ok=True."""
        with (
            patch(
                "app.services.credentials.model_discovery_service._list_openai_compatible_models",
                return_value=["custom-model-v1"],
            ),
            self._anyio_sync(),
        ):
            result = _run(
                probe_models(
                    AICredentialType.OPENAI_COMPATIBLE, "sk-compat",
                    base_url="https://api.example.com/v1",
                )
            )

        assert result.ok is True
        assert result.models == ["custom-model-v1"]
        assert result.reason is None

    # ── Skip paths ────────────────────────────────────────────────────────

    def test_oauth_token_skip(self):
        """sk-ant-oat* → ok=True, reason=oauth_token_unsupported, models=[], is_skip=True."""
        result = _run(
            probe_models(AICredentialType.ANTHROPIC, "sk-ant-oat01-oauth-token")
        )
        assert result.ok is True
        assert result.reason == OAUTH_TOKEN_UNSUPPORTED
        assert result.models == []
        assert result.is_skip is True

    def test_minimax_skip(self):
        """MiniMax → ok=True, reason=no_list_endpoint, is_skip=True."""
        result = _run(probe_models(AICredentialType.MINIMAX, "mm-test-key"))
        assert result.ok is True
        assert result.reason == "no_list_endpoint"
        assert result.models == []
        assert result.is_skip is True

    def test_openai_compatible_no_base_url_skip(self):
        """openai_compatible with no base_url → ok=True, reason=no_base_url."""
        result = _run(
            probe_models(AICredentialType.OPENAI_COMPATIBLE, "sk-compat", base_url=None)
        )
        assert result.ok is True
        assert result.reason == "no_base_url"
        assert result.models == []
        assert result.is_skip is True

    def test_openai_compatible_empty_base_url_skip(self):
        """Empty string base_url is falsy → same as no base_url."""
        result = _run(
            probe_models(AICredentialType.OPENAI_COMPATIBLE, "sk-compat", base_url="")
        )
        assert result.ok is True
        assert result.reason == "no_base_url"

    # ── HTTP auth failures ────────────────────────────────────────────────

    def test_401_maps_to_invalid_key(self):
        """HTTP 401 → ok=False, reason=invalid_key, is_skip=False."""
        exc = self._make_http_status_error(401)
        with (
            patch(
                "app.services.credentials.model_discovery_service._list_anthropic_models",
                side_effect=exc,
            ),
            self._anyio_sync(),
        ):
            result = _run(probe_models(AICredentialType.ANTHROPIC, "sk-ant-api03-bad"))

        assert result.ok is False
        assert result.reason == ERROR_INVALID_KEY
        assert result.models == []
        assert result.is_skip is False

    def test_403_maps_to_invalid_key(self):
        """HTTP 403 → ok=False, reason=invalid_key."""
        exc = self._make_http_status_error(403)
        with (
            patch(
                "app.services.credentials.model_discovery_service._list_openai_models",
                side_effect=exc,
            ),
            self._anyio_sync(),
        ):
            result = _run(probe_models(AICredentialType.OPENAI, "sk-openai-forbidden"))

        assert result.ok is False
        assert result.reason == ERROR_INVALID_KEY

    def test_non_auth_http_error_propagates(self):
        """HTTP 500 is not caught by probe_models — propagates to the caller."""
        exc = self._make_http_status_error(500)
        with (
            patch(
                "app.services.credentials.model_discovery_service._list_openai_models",
                side_effect=exc,
            ),
            self._anyio_sync(),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                _run(probe_models(AICredentialType.OPENAI, "sk-openai-server-error"))

    # ── ProbeResult contract ──────────────────────────────────────────────

    def test_probe_result_is_skip_true_for_all_skip_reasons(self):
        """is_skip is True for every SKIP_REASONS value when ok=True."""
        for reason in SKIP_REASONS:
            pr = ProbeResult(ok=True, models=[], reason=reason)
            assert pr.is_skip is True, f"Expected is_skip=True for reason={reason!r}"

    def test_probe_result_is_skip_false_for_invalid_key(self):
        """is_skip is False when ok=False (invalid_key is not a skip)."""
        pr = ProbeResult(ok=False, models=[], reason=ERROR_INVALID_KEY)
        assert pr.is_skip is False

    def test_probe_result_is_skip_false_for_clean_success(self):
        """is_skip is False when ok=True with reason=None (clean success)."""
        pr = ProbeResult(ok=True, models=["model-a"], reason=None)
        assert pr.is_skip is False
