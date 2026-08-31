"""``GoogleCertsUnavailable`` — the fail-open-shaped regression guard.

Load-bearing invariant, quoted directly from the class's own docstring in
``app/core/security.py``: ``verify_google_signed_jwt`` catches
``(JoseError, ValueError)`` at that module's line ~201 to convert Authlib's
bare ``ValueError`` (an unrecognized ``kid``, an oversized header — see
``tests/api/server_channels/server_channels_security_invariants_test.py::
test_malformed_jwt_unknown_kid_returns_403_and_writes_the_audit_row``) into
"this token is invalid".

If ``GoogleCertsUnavailable`` — raised when Google's JWKS genuinely can't be
fetched, i.e. "we cannot verify right now" — were ever re-parented under
``ValueError`` (a plausible-looking tidy-up: it reads like a validation
error), that same ``except (JoseError, ValueError)`` would swallow it too,
and a JWKS outage would start rendering as confident per-message rejection
instead of the distinct "cannot verify" case the channel webhook (and
Google OAuth login) depend on. Two halves:

  1. Structural — the class itself must never become a ``ValueError``
     subclass. Blunt on purpose: it fails the instant someone re-parents it,
     with no need to exercise any code path.
  2. Behavioral — with the JWKS fetch made to fail, the two real callers
     must still diverge exactly as designed: ``verify_google_signed_jwt``
     propagates ``GoogleCertsUnavailable`` (the channel webhook's distinct
     "cannot verify" deny — see ``ChannelInboundService.handle_inbound``'s
     ``ChannelVerificationError`` mapping, which logs "JWKS fetch failed"
     separately from "invalid signature"), while ``verify_google_token``
     catches it and returns ``None`` — the contract ``auth_service.py`` and
     ``oauth_credentials_service.py`` both depend on to degrade gracefully
     (fall back to the userinfo endpoint / report an invalid token) instead
     of 500ing on a Google outage.

No real network calls: only the JWKS *fetch* (``httpx.AsyncClient.get``) is
mocked, so ``_get_google_certs``'s own response-shape validation genuinely
runs. Each behavioral test uses its own throwaway ``certs_url`` to avoid
colliding with the process-local JWKS cache another test may have populated.
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.core.security import (
    GoogleCertsUnavailable,
    verify_google_signed_jwt,
    verify_google_token,
)

_TEST_ISSUER = "https://accounts.google.com"


def _fresh_certs_url() -> str:
    # Unique per test so `_get_google_certs`'s module-level cache never
    # short-circuits the mocked failure with a result cached by another test.
    return f"https://example-jwks.invalid/{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# 1. Structural
# ---------------------------------------------------------------------------


def test_google_certs_unavailable_is_not_a_value_error() -> None:
    # Load-bearing: verify_google_signed_jwt's `except (JoseError, ValueError)`
    # (app/core/security.py, ~line 201) would swallow this if it became a
    # ValueError subclass, and "cannot verify" would be reported as "invalid
    # signature" — collapsing the exact distinction this class exists for.
    assert not issubclass(GoogleCertsUnavailable, ValueError)


def test_google_certs_unavailable_is_still_a_plain_exception() -> None:
    # Sanity check on the other side of the guard: it must still be
    # catchable as a bare Exception (verify_google_token's
    # `except GoogleCertsUnavailable` relies on this).
    assert issubclass(GoogleCertsUnavailable, Exception)


# ---------------------------------------------------------------------------
# 2. Behavioral — the two callers must diverge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, get_side_effect, get_return_value",
    [
        ("unreachable", httpx.ConnectError("connection refused"), None),
        ("non-JSON body", None, "not-json-{{{"),
        ("no keys in body", None, {"unrelated": "shape"}),
    ],
)
def test_verify_google_signed_jwt_propagates_certs_unavailable(
    label: str, get_side_effect, get_return_value
) -> None:
    """The channel webhook / any caller of `verify_google_signed_jwt` sees a
    distinct `GoogleCertsUnavailable`, never a silent None or a ValueError."""
    certs_url = _fresh_certs_url()

    async def _run() -> None:
        with _mocked_jwks_fetch(get_side_effect, get_return_value):
            with pytest.raises(GoogleCertsUnavailable):
                await verify_google_signed_jwt(
                    "irrelevant-token-never-reached",
                    audience="irrelevant",
                    issuers=[_TEST_ISSUER],
                    certs_url=certs_url,
                )

    asyncio.run(_run())


@pytest.mark.parametrize(
    "label, get_side_effect, get_return_value",
    [
        ("unreachable", httpx.ConnectError("connection refused"), None),
        ("non-JSON body", None, "not-json-{{{"),
        ("no keys in body", None, {"unrelated": "shape"}),
    ],
)
def test_verify_google_token_degrades_to_none_on_the_same_failure(
    label: str, get_side_effect, get_return_value
) -> None:
    """The exact same JWKS failure that `verify_google_signed_jwt` propagates
    must be caught and degraded to `None` by `verify_google_token` — the
    contract Google OAuth login and the credential-refresh callback rely on
    to avoid 500ing on a Google outage."""
    certs_url = _fresh_certs_url()

    async def _run() -> None:
        with _mocked_jwks_fetch(get_side_effect, get_return_value), patch(
            "app.core.security.GOOGLE_OAUTH_CERTS_URL", certs_url
        ):
            result = await verify_google_token(
                "irrelevant-token-never-reached", client_id="irrelevant"
            )
        assert result is None

    asyncio.run(_run())


def _mocked_jwks_fetch(side_effect, return_value):
    """Patch httpx.AsyncClient.get so `_get_google_certs`'s own
    response-shape validation runs for real against a fake response."""
    if side_effect is not None:
        mock_get = AsyncMock(side_effect=side_effect)
    else:
        response = httpx.Response(
            200,
            request=httpx.Request("GET", "https://example-jwks.invalid/"),
            content=(
                return_value.encode()
                if isinstance(return_value, str)
                else json.dumps(return_value).encode()
            ),
        )
        mock_get = AsyncMock(return_value=response)
    return patch("httpx.AsyncClient.get", mock_get)
