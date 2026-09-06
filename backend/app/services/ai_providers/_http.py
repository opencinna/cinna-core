"""Shared HTTP helpers for the provider adapters.

Every adapter that lists models does the same three things: issue a blocking
``httpx`` GET on a worker thread, map a 401/403 to ``invalid_key``, and dedupe
the returned ids. Those live here so the five adapters differ only where the
providers actually differ.

``httpx`` rather than a vendor SDK is a decision the tree already made and
wrote down (see ``_list_openai_models``' original comment): the ``openai``
package is present only transitively today, and the ``anthropic`` package is
not a dependency at all. Google is the one exception — its listing has no
documented REST shape we rely on, so it keeps the ``google-genai`` client it
has always used.
"""
from __future__ import annotations

import anyio
import httpx

from app.services.ai_providers.base import (
    ERROR_INVALID_KEY,
    HTTP_TIMEOUT_SECONDS,
    ProbeResult,
    ProviderAdminError,
)

# Administration calls (create/destroy a key, read a spend limit) get a longer
# budget than a model listing: they are rare, they are initiated by an
# administrator who is watching, and a timeout on a create is the one failure
# that can leak a key we never learned the name of.
ADMIN_TIMEOUT_SECONDS = 30.0


def get_json(url: str, headers: dict[str, str]) -> dict | list:
    """Blocking GET returning parsed JSON. Runs on an ``anyio`` worker thread."""
    resp = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def ids_from_openai_shape(payload) -> list[str]:
    """Extract model ids from the ``{"data": [{"id": ...}]}`` response shape.

    **A payload that is neither an object nor an array raises**, exactly as the
    per-provider listers this replaced did. That is not conservatism for its own
    sake: an empty list is not a neutral value on this path. It is the *success*
    branch, and ``model_discovery_service.discover_models_for_credential`` takes
    it by overwriting
    ``discovered_models`` with ``[]`` **and clearing**
    ``models_discovery_error`` — so a garbled response would wipe a good cached
    model list and erase the record that anything went wrong. ``model_health``
    then reads the wiped cache as "empty but present", skips both its discovery
    branch and its unverified guard, and reports OK. A raise records the
    exception class name against the credential and leaves the previous list
    alone, which is what every other malformed-response case here does.

    The tolerance that *is* deliberate, stated exactly:

    - A **bare array** is accepted where the Anthropic and OpenAI listers would
      have raised. (The openai-compatible lister *looked* like it handled one —
      ``payload.get("data", payload if isinstance(payload, list) else [])`` —
      but ``.get`` on a list raises before the default is evaluated, so that
      branch was dead.)
    - Individual **entries** that are not dicts, or carry no ``id``, are skipped
      rather than raising. This one does yield a short list on the success
      branch, so a response with some unusable entries caches the usable ones.
      It is bounded by there being a well-formed envelope around them.
    """
    if isinstance(payload, dict):
        data = payload.get("data", [])
    elif isinstance(payload, list):
        data = payload
    else:
        raise TypeError(
            f"unexpected model-list payload of type {type(payload).__name__}"
        )
    return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]


def ok_result(models: list[str]) -> ProbeResult:
    """A successful listing, deduped while preserving order."""
    seen: set[str] = set()
    unique = [m for m in models if not (m in seen or seen.add(m))]
    return ProbeResult(ok=True, models=unique, reason=None)


def map_auth_error(exc: httpx.HTTPStatusError) -> ProbeResult | None:
    """``invalid_key`` for a 401/403; ``None`` means "not an auth failure"."""
    if exc.response.status_code in (401, 403):
        return ProbeResult(ok=False, models=[], reason=ERROR_INVALID_KEY)
    return None


async def run_listing(lister, *args) -> ProbeResult:
    """Run a blocking lister on a worker thread and shape its outcome.

    The whole of an adapter's ``list_models`` that is *not* provider-specific:
    offload, map a 401/403 to ``invalid_key``, let every other error propagate
    (the caller records the exception class name), dedupe on success.

    **Known gap, stated once here rather than four times:** only
    ``httpx.HTTPStatusError`` is mapped. A lister that raises a vendor SDK's own
    error type — ``google-genai`` raises ``google.genai.errors.ClientError``,
    which is not an ``httpx`` exception — bypasses the mapping entirely, so a
    rejected key surfaces as a 500 from Test Connection and as the exception's
    class name in the discovery cron rather than as ``invalid_key``. That was
    already true before the adapters existed (the single ``except`` around the
    old dispatch had the same hole) and is preserved deliberately; fixing it is a
    behaviour change that wants its own test.
    """
    try:
        models = await anyio.to_thread.run_sync(lister, *args)
    except httpx.HTTPStatusError as exc:
        auth_failure = map_auth_error(exc)
        if auth_failure is not None:
            return auth_failure
        raise
    return ok_result(models)


# ── Administration calls ────────────────────────────────────────────────────
#
# Separate from the probe helpers above because they fail differently and must
# be *read* differently: a probe maps a 401 to "this user's key is bad", while an
# admin call maps it to "this instance's provider secret is bad", which is an
# operator problem and a different audit event.


def _admin_error(exc: httpx.HTTPStatusError) -> ProviderAdminError:
    """Reduce a provider HTTP failure to a coarse, storable code.

    The 429 case is the one worth spelling out. Two entirely different conditions
    share that status: an ordinary rate limit (retry later, nothing is wrong) and
    a spend cap that has been reached (retrying forever will never help). The
    provider distinguishes them in ``error.code``, and collapsing them would
    produce a converge loop that retries an exhausted budget until it exhausts
    its own attempt count instead.
    """
    status = exc.response.status_code
    code: str | None = None
    try:
        body = exc.response.json()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code = error.get("code")
    except Exception:  # pragma: no cover - a non-JSON error body
        code = None

    if status in (401, 403):
        return ProviderAdminError("invalid_admin_secret")
    if status == 404:
        return ProviderAdminError("project_not_found")
    if status == 429:
        if code == "project_spend_limit_exceeded":
            return ProviderAdminError("project_spend_limit_exceeded")
        if code == "organization_spend_limit_exceeded":
            return ProviderAdminError("organization_spend_limit_exceeded")
        return ProviderAdminError("rate_limited")
    return ProviderAdminError("provider_error", f"HTTP {status}")


def _request_blocking(
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: dict | None,
) -> dict | list | None:
    """One blocking administration request. Runs on an ``anyio`` worker thread."""
    resp = httpx.request(
        method,
        url,
        headers=headers,
        json=json_body,
        timeout=ADMIN_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:  # pragma: no cover - empty/non-JSON success body
        return None


async def admin_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict | None = None,
    absent_on_404: bool = False,
) -> dict | list | None:
    """Issue one administration call, off the event loop, errors coded.

    ``absent_on_404`` turns "there is no such thing" into ``None`` for the one
    caller where absence is an answer rather than a failure: reading a spend
    limit from a project that has none.
    """
    try:
        return await anyio.to_thread.run_sync(
            _request_blocking, method, url, headers, json_body
        )
    except httpx.HTTPStatusError as exc:
        if absent_on_404 and exc.response.status_code == 404:
            return None
        raise _admin_error(exc) from exc
    except httpx.HTTPError as exc:
        # A transport failure — DNS, connect, read timeout. Retryable, and
        # deliberately not conflated with a provider *rejection*.
        raise ProviderAdminError("provider_unreachable", str(type(exc).__name__)) from exc
