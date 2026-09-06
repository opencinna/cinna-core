"""A tripwire on outbound HTTP, for the invariants that are about *absence*.

WHY THIS IS NOT A MOCK
----------------------
"No third-party provider is contacted on the account-creation path" is a claim
about code that does not exist. The usual way to test it — patch the provider
client the code happens to use today and assert the mock was not called — tests
the mock. A call added tomorrow through a *different* client, or through the
same client reached by a different import, passes that test silently. This
codebase has already paid for that mistake once (memory:
``project_test_users_send_email_patch_bug`` — a test patched
``app.utils.send_email`` while the route had bound the symbol at import, so a
real SMTP connection was attempted and the test passed anyway), and the
zero-touch-onboarding plan makes it invariant 9.

So the guard is installed one layer below every provider SDK instead: at the
**httpx transport**. Every outbound HTTP call in this backend goes through
``httpx`` — ``model_discovery_service``, ``oauth_credentials_service``,
``agent_env_connector``, the Docker adapter, and the ``openai`` / ``anthropic``
/ ``google-genai`` SDKs, all of which are httpx-based. A provider call
introduced anywhere on the guarded path, by any author, through any client
object, hits ``HTTPTransport.handle_request`` (or its async twin) and raises
:class:`OutboundNetworkCall` — which is not caught by
``AccountProvisioningService``'s nets in a way that hides it, because the guard
also records the attempt in a list the test asserts on afterwards.

``urllib.request.urlopen`` and ``http.client.HTTPConnection.connect`` are
guarded too, so a future ``requests``/``urllib3``/stdlib call is caught as
well.

WHAT IS DELIBERATELY *NOT* GUARDED
----------------------------------
* ``httpx.ASGITransport`` — that is how ``TestClient`` reaches the app. Guarding
  ``httpx.Client.send`` instead of the real-network transports would break
  every request the test makes.
* Raw sockets, so Postgres (``psycopg2``, libpq) and SMTP (``smtplib``) are
  untouched. The claim being tested is about *provider HTTP*, and widening the
  guard to sockets would make it fail for reasons that have nothing to do with
  the invariant — a guard that fails for unrelated reasons gets deleted.

PROVING THE GUARD IS ARMED
--------------------------
:func:`assert_guard_is_armed` makes a deliberate outbound call and requires it
to be refused. Call it inside every ``no_outbound_http`` block: without it a
guard that silently failed to patch anything would make the surrounding test
pass by proving nothing, which is the exact failure mode this module exists to
avoid.
"""
from __future__ import annotations

import http.client
import urllib.request
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import httpx


class OutboundNetworkCall(AssertionError):
    """Raised when guarded code attempts a real outbound HTTP call."""


@contextmanager
def no_outbound_http() -> Iterator[list[str]]:
    """Refuse every real outbound HTTP call made inside the block.

    Yields the list of attempts. It stays empty on a clean run; each entry is a
    ``"<seam>: <target>"`` string naming what tried to dial out, so a failure
    says which call to go and look at rather than only that one happened.
    """
    attempts: list[str] = []

    def _refuse(seam: str, describe):
        def _blocked(*args, **kwargs):
            target = describe(*args, **kwargs)
            attempts.append(f"{seam}: {target}")
            raise OutboundNetworkCall(
                f"Outbound HTTP is forbidden on this path — {seam} → {target}. "
                f"Account creation and login must never wait on a third-party "
                f"provider (zero-touch-onboarding invariant 1). If this is a "
                f"provider call, it belongs in a background task."
            )

        return _blocked

    def _httpx_target(_self, request, *_a, **_kw) -> str:
        return str(getattr(request, "url", request))

    def _urlopen_target(url, *_a, **_kw) -> str:
        return str(getattr(url, "full_url", url))

    def _conn_target(self, *_a, **_kw) -> str:
        return f"{getattr(self, 'host', '?')}:{getattr(self, 'port', '?')}"

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                httpx.HTTPTransport,
                "handle_request",
                _refuse("httpx.HTTPTransport.handle_request", _httpx_target),
            )
        )
        stack.enter_context(
            patch.object(
                httpx.AsyncHTTPTransport,
                "handle_async_request",
                _refuse(
                    "httpx.AsyncHTTPTransport.handle_async_request", _httpx_target
                ),
            )
        )
        stack.enter_context(
            patch.object(
                urllib.request,
                "urlopen",
                _refuse("urllib.request.urlopen", _urlopen_target),
            )
        )
        stack.enter_context(
            patch.object(
                http.client.HTTPConnection,
                "connect",
                _refuse("http.client.HTTPConnection.connect", _conn_target),
            )
        )
        yield attempts


def assert_guard_is_armed(attempts: list[str]) -> None:
    """Make a deliberate outbound call and require the guard to refuse it.

    The control for the whole technique. ``attempts`` is left as it was found,
    so the caller's "nothing dialled out" assertion is unaffected by the probe.
    """
    before = list(attempts)
    try:
        httpx.Client(timeout=0.01).get("https://provider.invalid/v1/models")
    except OutboundNetworkCall:
        del attempts[len(before) :]
        return
    except Exception as exc:  # pragma: no cover - the guard failed to install
        del attempts[len(before) :]
        raise AssertionError(
            "The outbound-HTTP guard is not armed: a deliberate provider call "
            f"raised {type(exc).__name__} instead of OutboundNetworkCall. "
            "Every 'no provider was contacted' assertion under this guard is "
            "vacuous until that is fixed."
        ) from exc
    del attempts[len(before) :]
    raise AssertionError(
        "The outbound-HTTP guard is not armed: a deliberate provider call "
        "succeeded. Every 'no provider was contacted' assertion under this "
        "guard is vacuous."
    )
