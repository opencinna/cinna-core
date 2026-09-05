"""Resolve the download URL for the current Cinna Desktop release.

The onboarding landing page needs a single "Download for <my platform>" button
that never hard-codes a version. This service answers "which URL should that
button point at?" for a given os/arch/kind.

**Security property: the backend never proxies the installer bytes.** Every
public path through this service ends in a *URL* that the visitor's browser
fetches directly from GitHub (or from the operator's configured mirror). The
backend therefore never becomes a distribution channel for executable content:
it cannot be used to launder an attacker-supplied binary behind a trusted
origin, it cannot be turned into a bandwidth amplifier by an unauthenticated
caller, and a compromised release never transits our infrastructure. It also
means this endpoint stays cheap — one cached GitHub metadata fetch per hour per
worker process, regardless of download volume. (The cache is process-local, so
a deployment running N uvicorn workers makes N fetches an hour, not one.)

The second half of that property is that resolution failure is never fatal: any
error (network, timeout, non-200, malformed payload, no matching asset, a
combination we publish no asset for) degrades to the human-readable releases
page rather than a 5xx, so the visitor always lands somewhere they can finish
the download by hand.
"""
import asyncio
import logging
import re
import time
from typing import Any, Literal

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Matches the mcp_providers convention: a bounded timeout so a slow upstream
# cannot pin a worker. GitHub's release metadata is a small JSON document.
_HTTP_TIMEOUT = 10.0

DesktopOS = Literal["darwin", "linux"]
DesktopArch = Literal["arm64", "x64"]
DesktopKind = Literal["dmg", "appimage", "deb"]


class DesktopReleaseService:
    """Resolves (and caches) the latest cinna-desktop release assets."""

    GITHUB_REPO = "opencinna/cinna-desktop"
    LATEST_RELEASE_API_URL = (
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    )
    RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
    # Every redirect target must sit under this prefix. The URL comes from
    # GitHub's own API for a repo we hard-code, so this is belt-and-braces —
    # but it turns "we only ever redirect to our own release assets" from an
    # assumption about the upstream response into a property of this code, so
    # a public unauthenticated endpoint can never be walked into an open
    # redirect by a tampered-with payload.
    ASSET_URL_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/download/"

    # One hour, per the plan: release metadata changes at most a few times a
    # week, and the anonymous GitHub API allows only 60 requests/hour/IP — a
    # public unauthenticated endpoint must not spend that budget per visitor.
    CACHE_TTL_SECONDS = 3600
    # Failures are cached too, briefly. Without this, a GitHub outage turns
    # every page visit into an upstream request and burns the rate limit
    # exactly when it is least affordable.
    FAILURE_CACHE_TTL_SECONDS = 60

    # (expires_at_monotonic, release payload or None for a cached failure).
    _cache: tuple[float, dict[str, Any] | None] | None = None
    # Single-flight guard for the refresh. Without it, every request that
    # arrives in the instant the TTL expires issues its own outbound GitHub
    # call — one inbound request mapping to one outbound request is exactly
    # the amplification this endpoint is supposed to foreclose, and a burst
    # landing on expiry can exhaust the anonymous 60/hour budget in one go.
    #
    # Built lazily and re-built when the running loop changes: an asyncio.Lock
    # binds to whichever loop first awaits it and then rejects every other one,
    # which would make it a hazard under per-test event loops.
    _lock: asyncio.Lock | None = None
    _lock_loop: asyncio.AbstractEventLoop | None = None

    # The desktop's electron-builder artifact naming, keyed by (os, kind):
    # the file extension, plus the architecture tokens the filename may carry
    # for each architecture we accept in the query — `cinna-desktop-<version>-
    # <token>.<ext>`. Tokens are listed most-preferred first.
    #
    # electron-builder spells Linux architectures the way each packaging world
    # does rather than the way Node does: the published v0.2.11 assets are
    # `cinna-desktop-0.2.11-x86_64.AppImage` and `-amd64.deb`, NOT the `-x64.…`
    # the plan predicted. All three spellings are accepted so a rename in the
    # desktop's build config cannot silently break the download button.
    #
    # The darwin tokens are verified against the same release, which ships
    # `-arm64.dmg` and `-x64.dmg` (plus `.blockmap` sidecars and `-mac.zip`
    # updater artifacts, both of which the anchored extension excludes) and no
    # universal build. Add a `universal` token here if one ever appears.
    #
    # Linux is published for x64 only, so a linux/arm64 request has no asset
    # and is deliberately absent rather than silently served an x64 binary.
    _ASSET_SHAPES: dict[tuple[str, str], tuple[str, dict[str, tuple[str, ...]]]] = {
        ("darwin", "dmg"): ("dmg", {"arm64": ("arm64",), "x64": ("x64",)}),
        ("linux", "appimage"): (
            "AppImage",
            {"x64": ("x86_64", "x64", "amd64")},
        ),
        ("linux", "deb"): ("deb", {"x64": ("amd64", "x64", "x86_64")}),
    }

    # ── Public API ───────────────────────────────────────────────────────

    @classmethod
    async def resolve_download_url(
        cls, *, os: DesktopOS, arch: DesktopArch, kind: DesktopKind
    ) -> str:
        """Return the URL to redirect a download request to.

        Always returns a usable URL. Any failure to identify the exact asset
        falls back to the releases page, so the caller never has to handle an
        error and never returns a 5xx.
        """
        patterns = cls._asset_patterns(os=os, arch=arch, kind=kind)
        if not patterns:
            # A combination we publish no asset for (linux+dmg, darwin+deb,
            # linux+arm64, ...). Each value is individually valid — only the
            # tuple is nonsensical — so this is not a request error; send the
            # visitor somewhere they can pick for themselves. Checked BEFORE
            # the mirror branch on purpose: both modes must answer an
            # impossible request the same way, or the guarantee this module
            # documents holds on only one of its two paths.
            logger.info(
                "No cinna-desktop asset shape for os=%s arch=%s kind=%s; "
                "falling back to the index page",
                os,
                arch,
                kind,
            )
            return cls._fallback_url()

        if settings.DESKTOP_DOWNLOAD_BASE_URL:
            return cls._mirror_url(os=os, arch=arch, kind=kind)

        release = await cls.get_latest_release()
        if release is None:
            return cls._fallback_url()

        assets = [a for a in (release.get("assets") or []) if isinstance(a, dict)]
        # Patterns outer, assets inner: a release carrying two accepted
        # spellings resolves to the preferred one, not to whichever GitHub
        # happened to list first.
        for pattern in patterns:
            for asset in assets:
                name = asset.get("name")
                url = asset.get("browser_download_url")
                if (
                    not isinstance(name, str)
                    or not isinstance(url, str)
                    or not pattern.match(name)
                ):
                    continue
                if not url.startswith(cls.ASSET_URL_PREFIX):
                    logger.warning(
                        "Ignoring cinna-desktop asset %s: download URL %r is "
                        "outside %s",
                        name,
                        url,
                        cls.ASSET_URL_PREFIX,
                    )
                    continue
                return url

        logger.warning(
            "cinna-desktop release %s has no asset matching %s (os=%s arch=%s "
            "kind=%s); falling back to the releases page",
            release.get("tag_name"),
            [p.pattern for p in patterns],
            os,
            arch,
            kind,
        )
        return cls._fallback_url()

    @classmethod
    async def get_latest_release(cls) -> dict[str, Any] | None:
        """Return the cached latest-release payload, fetching it if stale.

        ``None`` means "could not be resolved" — the caller falls back. Never
        raises.
        """
        cached = cls._cache
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]

        async with cls._refresh_lock():
            # Re-check under the lock: the requests queued behind the one that
            # actually refreshed must read its result, not each fetch again.
            cached = cls._cache
            if cached is not None and cached[0] > time.monotonic():
                return cached[1]

            try:
                release = await cls._fetch_latest_release()
            except Exception:
                # Backstop for the never-a-5xx guarantee: _fetch_latest_release
                # handles every failure mode we know of, and this catches the
                # ones we don't. Deliberately Exception, not BaseException, so
                # a cancelled request still cancels.
                logger.exception(
                    "Unexpected failure resolving the cinna-desktop release"
                )
                release = None
            ttl = (
                cls.CACHE_TTL_SECONDS
                if release is not None
                else cls.FAILURE_CACHE_TTL_SECONDS
            )
            cls._cache = (time.monotonic() + ttl, release)
            return release

    @classmethod
    def reset_cache(cls) -> None:
        """Drop the cached release (and the loop-bound lock).

        Written for tests, and nothing in the application calls it — but it is
        ordinary public API, not enforced as test-only, so treat a production
        call as legal rather than impossible. The blast radius is small: the
        next resolve just re-fetches from GitHub.

        The cache is class-level state that outlives a single test, so a suite
        that fakes a release must reset between tests — otherwise the fake
        leaks forward and the next test passes without exercising anything.
        """
        cls._cache = None
        cls._lock = None
        cls._lock_loop = None

    # ── Internals ────────────────────────────────────────────────────────

    @classmethod
    def _refresh_lock(cls) -> asyncio.Lock:
        """The single-flight lock, bound to the currently running loop."""
        loop = asyncio.get_running_loop()
        if cls._lock is None or cls._lock_loop is not loop:
            cls._lock = asyncio.Lock()
            cls._lock_loop = loop
        return cls._lock

    @classmethod
    def _fallback_url(cls) -> str:
        """Where to send a visitor whose exact asset could not be resolved.

        A mirror is configured precisely because GitHub is unreachable, so an
        air-gapped install gets the mirror's own index rather than a github.com
        page it cannot load.
        """
        if settings.DESKTOP_DOWNLOAD_BASE_URL:
            return settings.DESKTOP_DOWNLOAD_BASE_URL.rstrip("/") + "/"
        return cls.RELEASES_PAGE_URL

    @classmethod
    async def _fetch_latest_release(cls) -> dict[str, Any] | None:
        """Fetch and validate the latest release from GitHub.

        Returns ``None`` on any failure — this is the single seam tests patch
        to fake GitHub, and the single place upstream errors are swallowed.
        """
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                response = await client.get(
                    cls.LATEST_RELEASE_API_URL,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        # GitHub rejects API requests without a User-Agent.
                        "User-Agent": "cinna-core-desktop-download-resolver",
                    },
                )
        except httpx.HTTPError as e:
            logger.warning("Could not reach the GitHub releases API: %s", e)
            return None

        if response.status_code != 200:
            logger.warning(
                "GitHub releases API returned %s for %s",
                response.status_code,
                cls.LATEST_RELEASE_API_URL,
            )
            return None

        try:
            release = response.json()
        except ValueError as e:
            logger.warning("GitHub releases API returned malformed JSON: %s", e)
            return None

        if not isinstance(release, dict):
            logger.warning("GitHub releases API returned a non-object payload")
            return None

        # /releases/latest already excludes drafts and pre-releases; assert it
        # defensively so a change upstream (or a mocked/proxied API) can never
        # hand an unreleased build to a first-run user.
        if release.get("draft") or release.get("prerelease"):
            logger.warning(
                "Latest cinna-desktop release %s is a draft/pre-release; ignoring",
                release.get("tag_name"),
            )
            return None

        return release

    @classmethod
    def _asset_patterns(
        cls, *, os: str, arch: str, kind: str
    ) -> list[re.Pattern[str]]:
        """Patterns matching this combination's asset filename, best first.

        Empty when no asset of that shape is published. Matching on the
        arch+extension suffix rather than on a rendered version string keeps
        this correct across version-numbering changes, and anchoring on the
        extension keeps the sidecars out (`…-arm64.dmg.blockmap` never wins).
        """
        shape = cls._ASSET_SHAPES.get((os, kind))
        if shape is None:
            return []
        extension, tokens_by_arch = shape
        return [
            re.compile(
                rf"^cinna-desktop-.+-{re.escape(token)}\.{re.escape(extension)}$",
                re.IGNORECASE,
            )
            for token in tokens_by_arch.get(arch, ())
        ]

    @classmethod
    def _mirror_url(cls, *, os: str, arch: str, kind: str) -> str:
        """Redirect target when an asset mirror is configured.

        Mirror mode exists for air-gapped installs, which by definition cannot
        reach api.github.com — so it must not depend on GitHub for the version.
        The mirror is addressed by *shape* instead, and owns which build that
        shape currently resolves to.
        """
        base = settings.DESKTOP_DOWNLOAD_BASE_URL.rstrip("/")
        return f"{base}/{os}/{arch}/{kind}"
