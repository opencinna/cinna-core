"""Regression guard: the invitation email's `web_link` must NOT move to `/start`.

Phase 4 repointed the *new-account* welcome mail's secondary link to `/start`
(`app/utils.py::generate_new_account_email`, covered in
`test_new_account_email.py`). The phase-4 delta is explicit that the
*invitation* mail's `web_link` (`invitation_service.py`,
`generate_invitation_email`) stays exactly what it was: the invite template's
sentence reads "Or open it in the browser: {{ web_link }}", printing the URL
as its own visible text, in a sentence about *the invitation* — and `/start`
cannot open an invitation, only `accept_link` (built from the token) can.
Repointing `web_link` here would turn a true sentence into a false one and
print a URL nobody asked for.

Before phase 4, nothing asserted this URL at all, so a future repoint would be
silent. This file is that assertion.
"""
import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.invitation import invitation_email_patched, invite_user
from tests.utils.utils import random_email

# The invite template's escape-hatch sentence: the URL appears twice, once as
# the href and once as its own visible text, with no other attributes on the
# tag (see `email-templates/build/invite.html`).
_WEB_LINK_RE = re.compile(
    r'Or open it in the browser: <a href="([^"]*)">([^<]*)</a>'
)


def test_invitation_email_web_link_stays_the_bare_frontend_host(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Invite a user with mail enabled and inspect the rendered "open in the
    browser" sentence:
      1. It links to the bare frontend host — not `/start`
      2. It prints that same URL as its own visible text (the template shows
         the link, so a wrong href would also show a wrong URL to the reader)
      3. `/start` appears nowhere in the rendered mail at all
    """
    email = random_email()
    host = "https://cinna-invite.example.test"

    with (
        patch.object(settings, "FRONTEND_HOST", host),
        invitation_email_patched() as mock_send,
    ):
        invite_user(
            client,
            superuser_token_headers,
            email=email,
            send_email=True,
        )

    assert mock_send.call_count == 1, mock_send.call_args_list
    html = mock_send.call_args.kwargs["html_content"]

    match = _WEB_LINK_RE.search(html)
    assert match is not None, (
        'No "Or open it in the browser: <a ...>...</a>" sentence found in the '
        "rendered invite mail. Either the copy changed or "
        "email-templates/build/invite.html is stale relative to "
        "src/invite.mjml — in either case this test cannot tell whether "
        "web_link moved, which is itself worth flagging."
    )
    href, visible_text = match.group(1), match.group(2)

    assert href == host, href
    assert visible_text == host, visible_text
    assert "/start" not in html
