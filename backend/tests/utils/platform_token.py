"""Platform JWT minting helpers for tests that need raw signed tokens.

EXEMPTION — direct ``app.core.security`` import.

Rule 1 in ``backend/tests/README.md`` bans importing ``app.core.security`` from
``tests/api/`` files. This helper module is the single, documented exception: a
handful of tests must mint a *raw, signed* platform JWT with custom claims
(``token_type``/``role``) or a custom (often negative) expiry — for example the
env-console WebSocket auth-boundary tests, which assert close codes for expired
and scoped tokens that no API endpoint will ever hand out.

There is no API surface that issues such tokens, so the security primitive is
imported here once, behind named helpers, instead of in every test file. This
mirrors how ``tests/utils/cli.py`` centralizes CLI-token minting (CLI tokens go
through an API; these do not, hence the security import).

Prefer the API path whenever one exists; only reach for these helpers when the
test genuinely needs a hand-crafted token (wrong claims, expired, etc.).
"""
from datetime import timedelta

from app.core.security import create_access_token


def mint_platform_token(
    subject: str,
    *,
    expires_delta: timedelta = timedelta(hours=1),
    extra_claims: dict | None = None,
) -> str:
    """Mint a signed platform JWT for ``subject`` (a user id).

    ``expires_delta`` may be negative to produce an already-expired token.
    ``extra_claims`` is merged into the token payload (e.g. ``token_type`` /
    ``role`` for scoped-token rejection tests).
    """
    return create_access_token(
        subject=subject,
        expires_delta=expires_delta,
        extra_claims=extra_claims or {},
    )
