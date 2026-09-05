"""
Public Local Agent Kit surface — ``GET /agent-start`` and ``GET /api/agent-start``.

Unauthenticated by design, read-only, and static: every response is rendered
snapshot content, identical for every caller. The only database read on the
whole surface is the instance's ``local_agent_kit_enabled`` flag.

Two mounts, one router (see ``app.main``):

* ``/agent-start`` — the canonical, pasteable URL. It lives at the origin root, so a
  reverse proxy needs an explicit ``location /agent-start`` block to reach the backend
  instead of the SPA (``frontend/nginx.conf``, ``docs/infrastructure/nginx_setup.md``).
* ``/api/agent-start`` — the alias every deployment already proxies via the universal
  ``/api/`` rule. All kit-internal links use this one, so an instance whose proxy
  was never updated still serves a working kit.

Disabled instances return **404**, not 403: the response must not advertise that
the feature exists here at all.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app.api.deps import SessionDep
from app.core.config import settings
from app.services.cli.local_agent_kit_service import (
    CONTRACT_TARBALL_FILENAME,
    HTML_CSP,
    INDEX_MEMBER,
    KIT_VERSION_HEADER,
    START_MEMBER,
    TARBALL_FILENAME,
    LocalAgentKitService,
)
from app.services.common.rate_limiter import RateLimiter, anonymous_caller_key

logger = logging.getLogger(__name__)

# Keyed by source IP — the surface is anonymous, so the caller's address is the
# only identity there is. Process-local, like every other limiter here: a
# backstop against tarball hammering, not a billing control.
_kit_rate_limiter = RateLimiter()

# Intermediaries may cache: responses carry no user data and vary only with the
# deployed snapshot.
_CACHE_CONTROL = "public, max-age=300"


def _rate_limit_guard(request: Request) -> None:
    """Per-caller backstop against hammering an anonymous, cacheable surface.

    Listed **before** the enabled check so a throttled request never resolves
    ``SessionDep``: FastAPI solves router dependencies in order and aborts on the
    first raise, so a flood costs no pool connection and no query. The cost is
    that a flooder against a disabled instance sees 429 rather than 404 — a much
    smaller signal than handing an anonymous caller a database round-trip per
    request.
    """
    retry_after = _kit_rate_limiter.check(
        anonymous_caller_key(request),
        settings.LOCAL_AGENT_KIT_RATE_LIMIT_PER_MIN,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(int(retry_after))},
        )


def _enabled_guard(session: SessionDep) -> None:
    """404 (never 403) on an instance that opted out of publishing the kit."""
    if not LocalAgentKitService.is_enabled(session):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


start_router = APIRouter(
    tags=["local-agent-kit"],
    include_in_schema=False,
    dependencies=[Depends(_rate_limit_guard), Depends(_enabled_guard)],
)


# ---------------------------------------------------------------------------
# Shared response plumbing
# ---------------------------------------------------------------------------


def _etag(version: str, representation: str) -> str:
    """A validator that identifies one representation, not the whole kit.

    ``X-Kit-Version`` is the kit-wide content version and is the same on every
    response — which is exactly why it cannot also be the ETag. A client that
    carries a validator between URLs (or a CDN keyed loosely) would otherwise be
    told that a file it never fetched is unchanged. Folding the representation
    into the tag keeps one content version while making each validator answer
    for one resource.
    """
    scope = hashlib.sha256(representation.encode("utf-8")).hexdigest()[:8]
    return f'"{version}-{scope}"'


def _kit_headers(
    version: str, representation: str, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """Caching, versioning and hardening headers carried by every kit response."""
    headers = {
        "ETag": _etag(version, representation),
        "Cache-Control": _CACHE_CONTROL,
        # The mount root serves two bodies at one URL under a public cache
        # directive, so an intermediary must key on Accept or it will hand the
        # HTML landing page to a curl-piping assistant.
        "Vary": "Accept",
        KIT_VERSION_HEADER: version,
        # Public static content; a browser-hosted assistant may fetch it.
        # No credentials, no other methods. Both of these are overwritten by
        # the app-wide CORSMiddleware whenever the request carries an Origin
        # (it echoes the origin and substitutes its own expose-list), so they
        # only take effect on origin-less callers — curl, kit.py, an assistant's
        # HTTP client. The cross-origin case is why KIT_VERSION_HEADER is also
        # in the middleware's expose_headers in app.main.
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": KIT_VERSION_HEADER,
        "X-Content-Type-Options": "nosniff",
    }
    if extra:
        headers.update(extra)
    return headers


def _not_modified(
    request: Request, version: str, representation: str
) -> Response | None:
    """A 304 when the caller already holds *this* representation, else ``None``."""
    header = request.headers.get("if-none-match")
    if not header:
        return None
    expected = _etag(version, representation)
    candidates = {tag.strip().removeprefix("W/").strip() for tag in header.split(",")}
    if expected in candidates or "*" in candidates:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers=_kit_headers(version, representation),
        )
    return None


def _serve_tarball(
    request: Request,
    version: str,
    tarball: bytes,
    representation: str,
    filename: str,
) -> Response:
    """Serve one prebuilt archive with the standard headers, or a 304.

    Both archives are the same kind of response — an already-packed, already-
    versioned blob offered as a download — so they share one body here as well
    as the header/validator plumbing. A future change (a ``Content-Length``
    nuance, ranges, a different media type) then lands in one place.
    """
    cached = _not_modified(request, version, representation)
    if cached is not None:
        return cached

    return Response(
        content=tarball,
        media_type="application/tar+gzip",
        headers=_kit_headers(
            version,
            representation,
            {"Content-Disposition": f'attachment; filename="{filename}"'},
        ),
    )


def _wants_html(request: Request, format_param: str | None) -> bool:
    """Decide markdown vs HTML for the mount root.

    ``?format=`` wins outright. Otherwise HTML is chosen only when the caller
    asked for it *and* ranked it above markdown/plain text — a bare
    ``Accept: */*`` (curl, most assistants) gets markdown. Both variants embed
    the complete START.md, so a mis-negotiation never hides instructions.
    """
    if format_param:
        return format_param.lower() == "html"

    accept = request.headers.get("accept", "")
    if not accept:
        return False

    best: dict[str, float] = {}
    for part in accept.split(","):
        segments = part.split(";")
        media = segments[0].strip().lower()
        quality = 1.0
        for segment in segments[1:]:
            key, _, value = segment.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        best[media] = max(best.get(media, 0.0), quality)

    html_q = best.get("text/html", 0.0)
    if html_q <= 0.0:
        return False
    text_q = max(best.get("text/markdown", 0.0), best.get("text/plain", 0.0))
    return html_q > text_q


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@start_router.get("")
@start_router.get("/")
def get_start(
    request: Request,
    format: Annotated[str | None, Query()] = None,
) -> Response:
    """The kit entry document — markdown for assistants, HTML for browsers.

    Registered on both ``""`` and ``"/"`` so ``/agent-start`` and ``/agent-start/`` both
    resolve without a redirect (a 307 to the other spelling would be a needless
    hop for a curl-piped assistant).

    ``format`` is deliberately unvalidated: only ``html`` selects the landing
    page and anything else falls through to markdown. Rejecting a typo with a
    422 would hand the assistant a validation error where the whole design rule
    is that no spelling of this URL may hide the instructions.
    """
    wants_html = _wants_html(request, format)
    # The two variants share a URL, so they must not share a validator.
    representation = "start.html" if wants_html else "start.md"

    version = LocalAgentKitService.get_version()
    cached = _not_modified(request, version, representation)
    if cached is not None:
        return cached

    if wants_html:
        return HTMLResponse(
            content=LocalAgentKitService.get_start_html(),
            headers=_kit_headers(
                version, representation, {"Content-Security-Policy": HTML_CSP}
            ),
        )
    return PlainTextResponse(
        content=LocalAgentKitService.get_start_markdown(),
        media_type="text/markdown; charset=utf-8",
        headers=_kit_headers(version, representation),
    )


@start_router.get("/START.md")
def get_start_markdown(request: Request) -> Response:
    """The entry document, always raw markdown regardless of ``Accept``."""
    return _serve_member(request, START_MEMBER)


@start_router.get("/version")
def get_kit_version(request: Request) -> Response:
    """Content version + instance coordinates — what ``kit.py refresh`` polls."""
    payload: dict[str, Any] = LocalAgentKitService.get_version_payload()
    version = payload["kit_version"]
    cached = _not_modified(request, version, "version")
    if cached is not None:
        return cached
    return JSONResponse(content=payload, headers=_kit_headers(version, "version"))


@start_router.get("/kit.json")
def get_kit_index(request: Request) -> Response:
    """The machine-readable index (same bytes as ``/kit/kit.json``)."""
    return _serve_member(request, INDEX_MEMBER)


@start_router.get("/kit.tar.gz")
def get_kit_tarball(request: Request) -> Response:
    """The whole rendered kit, rooted at ``cinna-kit/``."""
    version, tarball = LocalAgentKitService.get_versioned_tarball()
    return _serve_tarball(request, version, tarball, "kit.tar.gz", TARBALL_FILENAME)


# The contract — the machine-readable subset of the kit (``kit.json``,
# ``layout.json``, ``CONTRACT_VERSION``, ``CHANGELOG.md``, ``schema/``,
# ``templates/``) a non-kit host needs to create and validate agent folders.
#
# Both contract representations are validated by **``kit_version``**, the
# content hash — deliberately NOT by ``contract_version``. ``contract_version``
# is hand-maintained and does not move when a template or the schema changes,
# so an ETag (or ``X-Kit-Version``) keyed on it would answer "unchanged" to a
# client that is in fact holding a stale contract, which is the one thing a
# validator promises cannot happen. ``kit_version`` moves on any content
# change. Do not "simplify" these to key on the version they carry in the body.
#
# Both also degrade together: an incoherent snapshot 503s BOTH of them and
# neither of the kit routes above (D16 amended — the boundary and its rationale
# live on ``LocalAgentKitService._require_serviceable_contract``). Nothing in
# this file decides that; both handlers inherit it from the service accessor
# they already call.


@start_router.get("/contract/version")
def get_contract_version(request: Request) -> Response:
    """Contract version + instance coordinates, for a host that consumes only
    the contract (Cinna Desktop) rather than the whole kit.

    The ``/version`` envelope plus ``contract_version``.
    """
    payload: dict[str, Any] = LocalAgentKitService.get_contract_version_payload()
    version = payload["kit_version"]
    cached = _not_modified(request, version, "contract/version")
    if cached is not None:
        return cached
    return JSONResponse(
        content=payload, headers=_kit_headers(version, "contract/version")
    )


@start_router.get("/contract.tar.gz")
def get_contract_tarball(request: Request) -> Response:
    """The contract subset of the rendered kit, rooted at ``cinna-contract/``."""
    version, tarball = LocalAgentKitService.get_versioned_contract_tarball()
    return _serve_tarball(
        request, version, tarball, "contract.tar.gz", CONTRACT_TARBALL_FILENAME
    )


@start_router.get("/kit/{path:path}")
def get_kit_file(request: Request, path: str) -> Response:
    """One file from the rendered kit.

    The lookup is an exact key hit against the in-memory rendered tree, so
    traversal (``..``), absolute paths and symlinks cannot resolve to anything
    outside the kit — they are simply not keys. Unknown path → 404.
    """
    return _serve_member(request, path)


def _serve_member(request: Request, rel_path: str) -> Response:
    """Serve one rendered kit member with the standard headers, or 404.

    Existence is resolved *before* ``If-None-Match``: a stale ETag must not turn
    an unknown path into a 304, which would read to the caller as "unchanged"
    for a file that was never there.
    """
    version, found = LocalAgentKitService.get_versioned_file(rel_path)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    cached = _not_modified(request, version, rel_path)
    if cached is not None:
        return cached

    content, media_type = found
    return Response(
        content=content,
        media_type=media_type,
        headers=_kit_headers(version, rel_path),
    )
