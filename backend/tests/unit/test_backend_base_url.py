"""`settings.backend_base_url` — the single origin for outward-facing API URLs.

Every absolute URL the platform hands to something outside it — inbound
webhook URLs, the consumer-facing Agent REST API base, signed A2A attachment
links, native-client OAuth discovery endpoints — resolves through this one
property. It exists because building them from ``FRONTEND_HOST`` 404s on any
deployment that serves the SPA and the API on different origins.

The fallback chain is the fiddly part and the reason for these tests: the
setting was first introduced as ``WEBHOOK_BASE_URL``, then outgrew that name,
so the former name must keep working for deployments that already set it.
"""
from app.core.config import Settings

_FRONTEND = "https://dashboard.example.com"


def _settings(**overrides) -> Settings:
    """Build Settings with the env-file/environment influence pinned out.

    Both names are passed explicitly on every construction so a value present
    in the ambient environment (a developer's tunnel, CI) cannot decide the
    result of a precedence test.
    """
    base = {"FRONTEND_HOST": _FRONTEND, "BACKEND_BASE_URL": "", "WEBHOOK_BASE_URL": ""}
    base.update(overrides)
    return Settings(**base)


def test_falls_back_to_frontend_host_when_unset() -> None:
    """Single-origin deployments must behave exactly as they did before this
    setting existed — an unset value is not an error."""
    assert _settings().backend_base_url == _FRONTEND


def test_backend_base_url_overrides_frontend_host() -> None:
    assert (
        _settings(BACKEND_BASE_URL="https://api.example.com").backend_base_url
        == "https://api.example.com"
    )


def test_former_webhook_name_is_still_honoured() -> None:
    """A deployment that set the original name keeps working untouched."""
    assert (
        _settings(WEBHOOK_BASE_URL="https://api.example.com").backend_base_url
        == "https://api.example.com"
    )


def test_new_name_wins_when_both_are_set() -> None:
    """Precedence must be unambiguous — during a migration both can be present,
    and the current name is the one an operator expects to take effect."""
    resolved = _settings(
        BACKEND_BASE_URL="https://new.example.com",
        WEBHOOK_BASE_URL="https://old.example.com",
    ).backend_base_url
    assert resolved == "https://new.example.com"


def test_trailing_slash_is_stripped() -> None:
    """Call sites concatenate a path directly, so a trailing slash would
    produce a double slash in every URL built from it."""
    assert (
        _settings(BACKEND_BASE_URL="https://api.example.com/").backend_base_url
        == "https://api.example.com"
    )


def test_webhook_alias_agrees_with_backend_base_url() -> None:
    """`webhook_base_url` is kept as an alias for the webhook call sites; if the
    two ever diverge, webhook URLs silently drift from every other API URL."""
    settings = _settings(BACKEND_BASE_URL="https://api.example.com")
    assert settings.webhook_base_url == settings.backend_base_url
