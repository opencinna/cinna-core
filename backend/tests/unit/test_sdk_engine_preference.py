"""Unit tests for ``sdk_for_engine_preference`` (manifest ``runtime.engine`` → SDK id).

The API-observable path (POST /cli/account/agents with ``engine``) is covered in
tests/api/cli/test_account_cli.py::test_account_create_agent_engine_preference.
"""
import pytest

from app.services.environments.sdk_constants import (
    SDK_TO_CREDENTIAL_TYPE,
    is_valid_sdk,
    sdk_for_engine_preference,
)


@pytest.mark.parametrize(
    ("engine", "default_sdk", "expected"),
    [
        # Absent / blank → no override
        (None, "claude-code/anthropic", None),
        ("", "opencode/openai", None),
        ("   ", "opencode/openai", None),
        # claude → claude-code; provider kept when claude-code accepts it
        ("claude", "claude-code/anthropic", "claude-code/anthropic"),
        ("claude", "claude-code/minimax", "claude-code/minimax"),
        ("claude", "opencode/anthropic", "claude-code/anthropic"),
        # ... else anthropic
        ("claude", "opencode/openai", "claude-code/anthropic"),
        ("claude", "opencode/google", "claude-code/anthropic"),
        ("claude", "opencode/openai_compatible", "claude-code/anthropic"),
        # opencode → opencode; provider kept when opencode accepts it
        ("opencode", "opencode/openai", "opencode/openai"),
        ("opencode", "opencode/google", "opencode/google"),
        ("opencode", "claude-code/anthropic", "opencode/anthropic"),
        # minimax is claude-code only → anthropic
        ("opencode", "claude-code/minimax", "opencode/anthropic"),
        # Any other non-empty engine (incl. codex) → opencode
        ("codex", "claude-code/anthropic", "opencode/anthropic"),
        ("codex", "opencode/openai", "opencode/openai"),
        ("codex", "claude-code/minimax", "opencode/anthropic"),
        ("some-future-engine", "opencode/google", "opencode/google"),
        # Whitespace is ignored; case is not (same as kit.py and the desktop)
        (" claude ", "claude-code/anthropic", "claude-code/anthropic"),
        ("Claude", "claude-code/anthropic", "opencode/anthropic"),
        ("OPENCODE", "opencode/openai", "opencode/openai"),
        # No / bare / unknown default → anthropic
        ("claude", None, "claude-code/anthropic"),
        ("codex", "", "opencode/anthropic"),
        ("codex", "opencode", "opencode/anthropic"),
        ("claude", "claude-code/nonsense", "claude-code/anthropic"),
    ],
)
def test_sdk_for_engine_preference(engine, default_sdk, expected):
    assert sdk_for_engine_preference(engine, default_sdk) == expected


@pytest.mark.parametrize("engine", ["claude", "opencode", "codex", "other"])
@pytest.mark.parametrize(
    "default_sdk",
    [None, "claude-code/anthropic", "claude-code/minimax", "opencode/anthropic",
     "opencode/openai", "opencode/openai_compatible", "opencode/google"],
)
def test_mapped_sdk_is_a_known_sdk(engine, default_sdk):
    """Every mapped id is valid and has a credential type (so creation can
    resolve and validate its key)."""
    sdk = sdk_for_engine_preference(engine, default_sdk)
    assert is_valid_sdk(sdk)
    assert sdk in SDK_TO_CREDENTIAL_TYPE
