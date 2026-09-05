"""Backend tests for the welcome mail sent by POST /api/v1/users/.

Covers commit a44b5804 ("new-account email: point the primary link at the
desktop landing page") — ``app.utils.generate_new_account_email`` plus the
``new_account.html`` build it renders:

  1. The primary call to action is the desktop landing page: the button's
     ``href`` is ``{FRONTEND_HOST}/desktop`` and its label is
     "Get Cinna Desktop" (the old "Go to Dashboard" is gone)
  2. The secondary "Prefer the browser? Log in on the web" row survives and
     points at the bare ``FRONTEND_HOST``
  3. The two URLs are distinct, so a refactor cannot collapse ``link`` and
     ``web_link`` back into one value without failing here
  4. Both URLs track ``settings.FRONTEND_HOST`` — asserted against two
     different hosts, so nothing here can pass on a hardcoded origin
  5. The rendered mail carries no unsubstituted ``{{`` placeholder, and every
     context key the generator passes really lands in the output

Notes:
  - These are API-level tests (Rule 1): the mail is captured by patching
    ``send_email`` at the route's import site with a ``MagicMock`` and reading
    the ``html_content`` kwarg — the same seam
    ``tests/api/auth/test_email_confirmation.py`` uses. Nothing imports
    ``app.utils.generate_new_account_email`` directly.
  - ``POST /users/`` sends *two* mails when ``emails_enabled``: the new-account
    mail (via ``app.api.routes.users.send_email``) and a confirmation mail (via
    ``app.services.users.email_confirmation_service.send_email``). Only the
    first import site is captured, so ``call_count == 1`` is itself a check
    that we read the new-account mail and not the confirmation one; the second
    site is patched separately purely to keep SMTP out of the test.
  - ``emails_enabled`` is a computed property over ``SMTP_HOST`` and
    ``EMAILS_FROM_EMAIL``; both are patched so the send branch is entered
    regardless of the container's mail configuration.
  - Assertion 5 (no ``{{``) is the guard against a stale
    ``email-templates/build/new_account.html``: the committed build was
    produced by mjml 4.x while ``npx mjml`` now resolves to 5.4.0, so
    regenerating churns the whole document and "edit the .mjml, forget the
    build" is a realistic future regression. The href/label assertions above
    are what actually fire on that mistake — see the docstring of
    ``test_new_account_email_has_no_unrendered_placeholders`` for what the
    ``{{`` check does and does not catch.
"""
import re
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_email, random_lower_string

_USERS_URL = f"{settings.API_V1_STR}/users/"

# Two unrelated origins: every URL assertion is made against both, so no
# assertion can be satisfied by a value baked into the template or the
# container's own FRONTEND_HOST.
_HOST_A = "https://cinna-a.example.test"
_HOST_B = "https://cinna-b.example.test:8443"

# The button mjml emits is `<a href="..." style="...">Get Cinna Desktop</a>`;
# the web-login row is a bare `<a href="...">Log in on the web</a>`.
_BUTTON_RE = re.compile(r'<a\s[^>]*href="([^"]*)"[^>]*>\s*Get Cinna Desktop\s*</a>')
_WEB_LINK_RE = re.compile(r'<a\s[^>]*href="([^"]*)"[^>]*>\s*Log in on the web\s*</a>')


def _create_user_capturing_welcome_mail(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    *,
    frontend_host: str,
) -> dict[str, str]:
    """Create a user as superuser and return the captured new-account mail.

    Returns ``{"email", "password", "subject", "html"}``.
    """
    email = random_email()
    password = random_lower_string()
    mock_send = MagicMock(return_value=None)

    with (
        patch("app.core.config.settings.FRONTEND_HOST", frontend_host),
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "noreply@example.com"),
        patch("app.api.routes.users.send_email", mock_send),
        patch(
            "app.services.users.email_confirmation_service.send_email",
            MagicMock(return_value=None),
        ),
    ):
        r = client.post(
            _USERS_URL,
            headers=superuser_token_headers,
            json={"email": email, "password": password},
        )

    assert r.status_code == 200, r.text
    assert r.json()["email"] == email

    # Exactly one send through the route's import site — the new-account mail.
    assert mock_send.call_count == 1, (
        f"Expected exactly 1 new-account mail, got {mock_send.call_count}"
    )
    kwargs = mock_send.call_args.kwargs
    assert kwargs["email_to"] == email

    return {
        "email": email,
        "password": password,
        "subject": kwargs["subject"],
        "html": kwargs["html_content"],
    }


def test_new_account_email_leads_with_the_desktop_landing_page(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The welcome mail's call to action:
      1. Superuser creates a user → a new-account mail is rendered and sent
      2. The button links to {FRONTEND_HOST}/desktop, labelled "Get Cinna Desktop"
      3. The retired "Go to Dashboard" label is gone
      4. The secondary web-login row survives and points at the bare origin
      5. The two URLs are distinct — link and web_link cannot collapse into one
    """
    # ── Phase 1: Create the user and capture the mail ─────────────────────
    mail = _create_user_capturing_welcome_mail(
        client, superuser_token_headers, frontend_host=_HOST_A
    )
    html = mail["html"]

    assert mail["subject"] == (
        f"{settings.PROJECT_NAME} - New account for user {mail['email']}"
    )

    # ── Phase 2: The button is the desktop landing page ───────────────────
    button = _BUTTON_RE.search(html)
    assert button is not None, (
        'No <a ...>Get Cinna Desktop</a> in the rendered mail. Either the '
        'button label regressed or email-templates/build/new_account.html is '
        'stale relative to src/new_account.mjml.'
    )
    button_href = button.group(1)
    assert button_href == f"{_HOST_A}/desktop", button_href

    # ── Phase 3: The old dashboard call to action is gone ─────────────────
    assert "Go to Dashboard" not in html

    # ── Phase 4: The web-login escape hatch survives ──────────────────────
    assert "Prefer the browser?" in html
    web = _WEB_LINK_RE.search(html)
    assert web is not None, (
        'No <a ...>Log in on the web</a> in the rendered mail — the secondary '
        'web-login row was dropped from the template or the build is stale.'
    )
    web_href = web.group(1)
    assert web_href == _HOST_A, web_href

    # ── Phase 5: The two URLs are distinct ────────────────────────────────
    # A refactor that collapses `link` and `web_link` back into one value makes
    # the button and the web row point at the same place; that must fail here.
    assert button_href != web_href, (
        f"link and web_link collapsed onto the same URL: {button_href}"
    )
    assert button_href.startswith(web_href)


def test_new_account_email_urls_track_frontend_host(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Both URLs are derived from settings, not hardcoded:
      1. Render the mail against host A → both URLs use host A
      2. Render it again against a different host B → both URLs use host B
      3. Neither host leaks into the other rendering
    """
    for host, other in ((_HOST_A, _HOST_B), (_HOST_B, _HOST_A)):
        mail = _create_user_capturing_welcome_mail(
            client, superuser_token_headers, frontend_host=host
        )
        html = mail["html"]

        button = _BUTTON_RE.search(html)
        web = _WEB_LINK_RE.search(html)
        assert button is not None and web is not None, host
        assert button.group(1) == f"{host}/desktop"
        assert web.group(1) == host

        # The other origin appears nowhere — nothing is baked in.
        assert other not in html


def test_new_account_email_has_no_unrendered_placeholders(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The rendered mail is fully substituted:
      1. No `{{` survives rendering
      2. Every context key the generator passes lands in the output
         (project_name, username, password, link, web_link)

    Scope of (1): Jinja renders an *unknown* placeholder to the empty string,
    so a build that references a variable the generator does not pass will not
    leave `{{` behind — assertion (2) and the href assertions in
    ``test_new_account_email_leads_with_the_desktop_landing_page`` are what
    catch that case. What (1) does catch is a build whose delimiters Jinja
    never consumed at all — an escaped, doubled, or otherwise mangled
    placeholder emitted by a different mjml major, which is exactly the risk of
    regenerating ``build/new_account.html`` with mjml 5.4.0 when the committed
    file came from 4.x.
    """
    mail = _create_user_capturing_welcome_mail(
        client, superuser_token_headers, frontend_host=_HOST_A
    )
    html = mail["html"]

    # ── Phase 1: Nothing left for Jinja to render ─────────────────────────
    assert "{{" not in html, (
        "Unrendered Jinja placeholder in the new-account mail — "
        "email-templates/build/new_account.html is out of sync with "
        "email-templates/src/new_account.mjml (regenerate the build)."
    )

    # ── Phase 2: Every context key actually landed ────────────────────────
    assert settings.PROJECT_NAME in html          # project_name
    assert mail["email"] in html                  # username / email
    assert mail["password"] in html               # password (temp credential)
    assert f"{_HOST_A}/desktop" in html           # link
    assert f'href="{_HOST_A}"' in html            # web_link
