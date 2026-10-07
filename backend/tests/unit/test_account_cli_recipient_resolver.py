"""Unit tests for ``AccountCLIService._resolve_recipient`` (issued keys, C4/C17).

Case-only duplicate accounts cannot be created through the API, so the
``ambiguous_recipient`` branch is exercised against a stubbed lookup.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.agent_api.agent_api_service import AgentApiError
from app.services.cli.account_cli_service import AccountCLIService

_LOOKUP = "app.services.users.user_service.UserService.find_active_users_by_email_ci"


def _user(n: str):
    return SimpleNamespace(id=n, email=f"{n}@example.com")


def test_blank_email_resolves_to_the_caller() -> None:
    caller = _user("me")
    assert AccountCLIService._resolve_recipient(None, caller, None) is caller
    assert AccountCLIService._resolve_recipient(None, caller, "  ") is caller


def test_single_match_is_returned() -> None:
    match = _user("a")
    with patch(_LOOKUP, return_value=[match]):
        assert AccountCLIService._resolve_recipient(None, _user("me"), "A@x.com") is match


def test_no_match_is_recipient_not_found() -> None:
    with patch(_LOOKUP, return_value=[]):
        with pytest.raises(AgentApiError) as exc:
            AccountCLIService._resolve_recipient(None, _user("me"), "x@x.com")
    assert exc.value.status_code == 404 and exc.value.code == "recipient_not_found"


def test_case_duplicates_are_ambiguous_recipient() -> None:
    with patch(_LOOKUP, return_value=[_user("a"), _user("b")]):
        with pytest.raises(AgentApiError) as exc:
            AccountCLIService._resolve_recipient(None, _user("me"), "x@x.com")
    assert exc.value.status_code == 409 and exc.value.code == "ambiguous_recipient"
