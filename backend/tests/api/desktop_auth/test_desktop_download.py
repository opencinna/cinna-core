"""Backend tests for the public Cinna Desktop download resolver.

Covers commit d7777951 ("desktop download: resolve the installer, never serve
it") — GET /api/v1/desktop/download?os=&arch=&kind=:

  1. Asset selection for every supported (os, arch, kind), against a realistic
     release payload including the sidecars electron-builder ships
  2. The accepted alternate spellings (-x86_64/-x64/-amd64) and their
     preference order
  3. Fallback to the releases page on every resolution failure: fetch returns
     None, fetch raises, no matching asset — plus the draft/pre-release guard
     and the non-200 / malformed-JSON paths, exercised one layer lower
  4. Unsupported-but-individually-valid combinations → fallback, never a 5xx
  5. Invalid enum values → 422 from FastAPI's own validation
  6. The route is public — no credentials required
  7. ASSET_URL_PREFIX: an asset whose download URL points off our release
     prefix is refused, so a tampered payload cannot walk a public endpoint
     into an open redirect
  8. Mirror mode (DESKTOP_DOWNLOAD_BASE_URL) addresses the mirror by shape and
     never touches GitHub

Notes:
  - ``DesktopReleaseService._cache`` is class-level and outlives a test, so the
    autouse ``_reset_release_cache`` fixture below resets it around every test.
    ``test_cache_isolation_between_tests_is_pinned`` is a canary that fails if
    that fixture is ever removed — see its docstring.
  - ``_fetch_latest_release`` is the single seam where HTTP happens; it is
    patched with a plain ``AsyncMock`` (Mock is not a descriptor, so the
    ``cls._fetch_latest_release()`` call site reaches the mock directly).
  - The route always 302s, so every request here uses
    ``follow_redirects=False`` and asserts on the ``Location`` header.
"""
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.desktop_auth.desktop_release_service import DesktopReleaseService

_URL = f"{settings.API_V1_STR}/desktop/download"
_FETCH_SEAM = (
    "app.services.desktop_auth.desktop_release_service"
    ".DesktopReleaseService._fetch_latest_release"
)

_RELEASES_PAGE = DesktopReleaseService.RELEASES_PAGE_URL
_PREFIX = DesktopReleaseService.ASSET_URL_PREFIX

_VERSION = "0.9.7"
_TAG = f"v{_VERSION}"


# ── Fake release payloads ───────────────────────────────────────────────────


def _asset(name: str, url: str | None = None) -> dict[str, Any]:
    return {"name": name, "browser_download_url": url or f"{_PREFIX}{_TAG}/{name}"}


def _release(assets: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tag_name": _TAG,
        "draft": False,
        "prerelease": False,
        "assets": assets,
    }
    payload.update(overrides)
    return payload


def _published_release() -> dict[str, Any]:
    """The real published asset set, sidecars and updater artifacts included.

    The published names are ``-x86_64.AppImage`` and ``-amd64.deb``, not the
    ``-x64.*`` the plan predicted; the darwin builds are ``-arm64.dmg`` and
    ``-x64.dmg`` with ``.blockmap`` sidecars and a ``-mac.zip`` updater
    artifact alongside. All of those decoys must be excluded by the anchored
    extension match.
    """
    return _release(
        [
            _asset(f"cinna-desktop-{_VERSION}-arm64.dmg.blockmap"),
            _asset(f"cinna-desktop-{_VERSION}-arm64.dmg"),
            _asset(f"cinna-desktop-{_VERSION}-x64.dmg.blockmap"),
            _asset(f"cinna-desktop-{_VERSION}-x64.dmg"),
            _asset(f"cinna-desktop-{_VERSION}-arm64-mac.zip"),
            _asset(f"cinna-desktop-{_VERSION}-x86_64.AppImage"),
            _asset(f"cinna-desktop-{_VERSION}-amd64.deb"),
            _asset("latest-mac.yml"),
            _asset("latest-linux.yml"),
        ]
    )


def _download(
    client: TestClient, *, os: str, arch: str, kind: str, **kwargs: Any
) -> httpx.Response:
    return client.get(
        _URL,
        params={"os": os, "arch": arch, "kind": kind},
        follow_redirects=False,
        **kwargs,
    )


# ── Cache isolation ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_release_cache():
    """Drop the class-level release cache around every test in this module.

    ``DesktopReleaseService._cache`` holds a successful fetch for an hour and a
    failed one for a minute, on the class object — it survives the test
    transaction, the ``client`` fixture, and the process. Without this reset a
    faked release leaks into every later test in the same worker and they pass
    vacuously, never reaching the seam they claim to exercise.
    """
    DesktopReleaseService.reset_cache()
    yield
    DesktopReleaseService.reset_cache()


# ── Scenario 1: asset selection per (os, arch, kind) ────────────────────────


def test_download_redirects_to_the_matching_asset_for_each_platform(
    client: TestClient,
) -> None:
    """
    Asset resolution against the real published naming:
      1. Every supported combination 302s to its own asset
      2. Each redirect points at exactly one asset and NOT at a sibling —
         darwin/arm64 must never be served the x64 build
      3. Sidecars (.blockmap), updater artifacts (-mac.zip) and the electron
         -builder .yml manifests are never selected
      4. Every target sits under the release-download prefix

    Side effect: this test leaves a fake release in the class-level cache,
    which is exactly what the next test is a canary for.
    """
    expected = {
        ("darwin", "arm64", "dmg"): f"cinna-desktop-{_VERSION}-arm64.dmg",
        ("darwin", "x64", "dmg"): f"cinna-desktop-{_VERSION}-x64.dmg",
        ("linux", "x64", "appimage"): f"cinna-desktop-{_VERSION}-x86_64.AppImage",
        ("linux", "x64", "deb"): f"cinna-desktop-{_VERSION}-amd64.deb",
    }

    fetch = AsyncMock(return_value=_published_release())
    with patch(_FETCH_SEAM, new=fetch):
        for (os_, arch, kind), asset_name in expected.items():
            r = _download(client, os=os_, arch=arch, kind=kind)

            # ── Phase 1: It is a redirect, not a proxied body ─────────────
            assert r.status_code == 302, (
                f"{os_}/{arch}/{kind}: expected 302, got {r.status_code}: {r.text}"
            )
            location = r.headers["location"]

            # ── Phase 2: The right asset, discriminatingly ────────────────
            assert location == f"{_PREFIX}{_TAG}/{asset_name}", (
                f"{os_}/{arch}/{kind} resolved to {location!r}, expected {asset_name!r}"
            )
            for other_name in expected.values():
                if other_name != asset_name:
                    assert not location.endswith(f"/{other_name}"), (
                        f"{os_}/{arch}/{kind} was served the sibling asset "
                        f"{other_name!r}"
                    )

            # ── Phase 3: No sidecars or updater artifacts ─────────────────
            assert not location.endswith((".blockmap", ".zip", ".yml")), location

            # ── Phase 4: Under our own release prefix ─────────────────────
            assert location.startswith(_PREFIX), location
            assert location != _RELEASES_PAGE

    # The resolver went to the seam rather than short-circuiting somewhere.
    assert fetch.await_count >= 1


def test_cache_isolation_between_tests_is_pinned(client: TestClient) -> None:
    """Canary for the autouse ``_reset_release_cache`` fixture itself.

    The test directly above caches a fake release on the class-level
    ``_cache`` with a one-hour TTL. If the autouse reset fixture were removed,
    that fake would still be cached here: ``_fetch_latest_release`` would never
    be awaited and the request below would 302 to the fake dmg instead of the
    releases page. Both assertions below fail in that world — which is the
    point of writing it as a test rather than as a comment.
    """
    fetch = AsyncMock(return_value=None)
    with patch(_FETCH_SEAM, new=fetch):
        r = _download(client, os="darwin", arch="arm64", kind="dmg")

    assert r.status_code == 302
    assert r.headers["location"] == _RELEASES_PAGE, (
        "A release cached by an earlier test leaked into this one — the "
        "_reset_release_cache fixture is not doing its job"
    )
    fetch.assert_awaited_once()


# ── Scenario 2: alternate spellings and their preference order ──────────────


def test_download_accepts_the_alternate_linux_asset_spellings(
    client: TestClient,
) -> None:
    """
    All three architecture spellings are accepted, preferred order first:
      1. AppImage published as -x64.AppImage still resolves
      2. AppImage published as -amd64.AppImage still resolves
      3. deb published as -x86_64.deb still resolves
      4. When several accepted spellings are present, the preferred one wins
         (AppImage → x86_64; deb → amd64), not whichever GitHub listed first
    """
    cases: list[tuple[str, list[str], str]] = [
        # (kind, published asset names, expected winner)
        ("appimage", [f"cinna-desktop-{_VERSION}-x64.AppImage"],
         f"cinna-desktop-{_VERSION}-x64.AppImage"),
        ("appimage", [f"cinna-desktop-{_VERSION}-amd64.AppImage"],
         f"cinna-desktop-{_VERSION}-amd64.AppImage"),
        ("deb", [f"cinna-desktop-{_VERSION}-x86_64.deb"],
         f"cinna-desktop-{_VERSION}-x86_64.deb"),
        # Preference order: listed last in the payload, still preferred.
        ("appimage",
         [f"cinna-desktop-{_VERSION}-amd64.AppImage",
          f"cinna-desktop-{_VERSION}-x64.AppImage",
          f"cinna-desktop-{_VERSION}-x86_64.AppImage"],
         f"cinna-desktop-{_VERSION}-x86_64.AppImage"),
        ("deb",
         [f"cinna-desktop-{_VERSION}-x86_64.deb",
          f"cinna-desktop-{_VERSION}-x64.deb",
          f"cinna-desktop-{_VERSION}-amd64.deb"],
         f"cinna-desktop-{_VERSION}-amd64.deb"),
    ]

    for kind, names, winner in cases:
        DesktopReleaseService.reset_cache()
        release = _release([_asset(n) for n in names])
        with patch(_FETCH_SEAM, new=AsyncMock(return_value=release)):
            r = _download(client, os="linux", arch="x64", kind=kind)

        assert r.status_code == 302, r.text
        assert r.headers["location"] == f"{_PREFIX}{_TAG}/{winner}", (
            f"kind={kind} published as {names} resolved to "
            f"{r.headers['location']!r}, expected {winner!r}"
        )


# ── Scenario 3: resolution failure always degrades to the releases page ─────


def test_download_falls_back_to_the_releases_page_on_resolution_failure(
    client: TestClient,
) -> None:
    """
    Resolution failure is never a 5xx:
      1. The fetch seam returns None (upstream unreachable / non-200 / bad JSON)
      2. The fetch seam raises — the except-Exception backstop catches it
      3. The release carries no asset matching this combination
      4. The release carries only decoys (blockmap sidecars for the asset we want)
    """
    no_match = _release(
        [
            _asset("latest-mac.yml"),
            _asset(f"cinna-desktop-{_VERSION}-x86_64.AppImage"),
        ]
    )
    decoys_only = _release(
        [
            _asset(f"cinna-desktop-{_VERSION}-arm64.dmg.blockmap"),
            _asset(f"cinna-desktop-{_VERSION}-arm64-mac.zip"),
        ]
    )

    cases: list[tuple[str, AsyncMock]] = [
        ("fetch returned None", AsyncMock(return_value=None)),
        ("fetch raised", AsyncMock(side_effect=RuntimeError("boom"))),
        ("no matching asset", AsyncMock(return_value=no_match)),
        ("decoy assets only", AsyncMock(return_value=decoys_only)),
    ]

    for label, fetch in cases:
        DesktopReleaseService.reset_cache()
        with patch(_FETCH_SEAM, new=fetch):
            r = _download(client, os="darwin", arch="arm64", kind="dmg")

        assert r.status_code == 302, f"{label}: expected 302, got {r.status_code}"
        assert r.status_code < 500
        assert r.headers["location"] == _RELEASES_PAGE, (
            f"{label}: expected the releases page, got {r.headers['location']!r}"
        )
        fetch.assert_awaited()


def test_download_refuses_a_draft_or_prerelease_and_a_bad_upstream_response(
    client: TestClient,
) -> None:
    """
    The guards that live *inside* the fetch seam, exercised one layer lower by
    faking ``httpx.AsyncClient`` instead:
      1. A draft payload never reaches a first-run user → releases page
      2. A pre-release payload likewise → releases page
      3. A non-200 upstream response → releases page
      4. Malformed JSON → releases page
      5. A transport error → releases page
      6. Control: the same fake transport serving a real release DOES resolve,
         so the five assertions above are not passing because the fake client
         is simply broken
    """
    good = _published_release()
    expected_asset = f"{_PREFIX}{_TAG}/cinna-desktop-{_VERSION}-arm64.dmg"

    class _FakeResponse:
        def __init__(self, status_code: int, payload: Any, bad_json: bool = False):
            self.status_code = status_code
            self._payload = payload
            self._bad_json = bad_json

        def json(self) -> Any:
            if self._bad_json:
                raise ValueError("not json")
            return self._payload

    def _fake_client(response: Any = None, error: Exception | None = None):
        class _FakeAsyncClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "_FakeAsyncClient":
                return self

            async def __aexit__(self, *args: Any) -> bool:
                return False

            async def get(self, *args: Any, **kwargs: Any) -> Any:
                if error is not None:
                    raise error
                return response

        return _FakeAsyncClient

    cases: list[tuple[str, Any, str]] = [
        ("draft", _fake_client(_FakeResponse(200, _release(good["assets"], draft=True))),
         _RELEASES_PAGE),
        ("prerelease",
         _fake_client(_FakeResponse(200, _release(good["assets"], prerelease=True))),
         _RELEASES_PAGE),
        ("non-200", _fake_client(_FakeResponse(503, None)), _RELEASES_PAGE),
        ("malformed json",
         _fake_client(_FakeResponse(200, None, bad_json=True)), _RELEASES_PAGE),
        ("transport error",
         _fake_client(error=httpx.ConnectError("no route")), _RELEASES_PAGE),
        # Control — proves the fake transport can succeed.
        ("published release", _fake_client(_FakeResponse(200, good)), expected_asset),
    ]

    for label, fake_client_cls, expected in cases:
        DesktopReleaseService.reset_cache()
        with patch("httpx.AsyncClient", fake_client_cls):
            r = _download(client, os="darwin", arch="arm64", kind="dmg")

        assert r.status_code == 302, f"{label}: expected 302, got {r.status_code}"
        assert r.headers["location"] == expected, (
            f"{label}: expected {expected!r}, got {r.headers['location']!r}"
        )


# ── Scenario 4: the ASSET_URL_PREFIX open-redirect guard ────────────────────


def test_download_refuses_an_asset_hosted_outside_the_release_prefix(
    client: TestClient,
) -> None:
    """
    Security property: a tampered payload cannot turn this public endpoint into
    an open redirect.
      1. A correctly-named asset whose download URL points at another host is
         ignored → releases page, not the attacker's host
      2. A look-alike prefix (same host, neighbouring repo) is ignored too —
         the guard is a prefix match on the full release-download URL
      3. A non-string download URL is ignored rather than crashing
      4. Discriminating: when the off-prefix asset is the PREFERRED spelling
         and a legitimate alternate spelling is also published, the legitimate
         one is served — the guard skips the bad asset, it does not abandon
         resolution
    """
    evil = "https://evil.example.test/releases/download/v9/cinna-desktop.dmg"
    lookalike = (
        "https://github.com/opencinna/cinna-desktop-evil/releases/download/"
        f"{_TAG}/cinna-desktop-{_VERSION}-arm64.dmg"
    )

    # ── Phase 1: Off-host URL → fallback ──────────────────────────────────
    DesktopReleaseService.reset_cache()
    release = _release([_asset(f"cinna-desktop-{_VERSION}-arm64.dmg", url=evil)])
    with patch(_FETCH_SEAM, new=AsyncMock(return_value=release)):
        r = _download(client, os="darwin", arch="arm64", kind="dmg")
    assert r.status_code == 302
    assert r.headers["location"] == _RELEASES_PAGE
    assert "evil.example.test" not in r.headers["location"]

    # ── Phase 2: Look-alike repo on github.com → fallback ─────────────────
    DesktopReleaseService.reset_cache()
    release = _release([_asset(f"cinna-desktop-{_VERSION}-arm64.dmg", url=lookalike)])
    with patch(_FETCH_SEAM, new=AsyncMock(return_value=release)):
        r = _download(client, os="darwin", arch="arm64", kind="dmg")
    assert r.status_code == 302
    assert r.headers["location"] == _RELEASES_PAGE
    assert "cinna-desktop-evil" not in r.headers["location"]

    # ── Phase 3: Non-string download URL → fallback, not a 500 ────────────
    DesktopReleaseService.reset_cache()
    release = _release(
        [
            {
                "name": f"cinna-desktop-{_VERSION}-arm64.dmg",
                "browser_download_url": None,
            },
            "not-even-a-dict",
        ]
    )
    with patch(_FETCH_SEAM, new=AsyncMock(return_value=release)):
        r = _download(client, os="darwin", arch="arm64", kind="dmg")
    assert r.status_code == 302
    assert r.headers["location"] == _RELEASES_PAGE

    # ── Phase 4: The guard skips the bad asset, it does not give up ───────
    DesktopReleaseService.reset_cache()
    good_name = f"cinna-desktop-{_VERSION}-x64.AppImage"
    release = _release(
        [
            # Preferred spelling, but hosted off-prefix.
            _asset(
                f"cinna-desktop-{_VERSION}-x86_64.AppImage",
                url="https://evil.example.test/x86_64.AppImage",
            ),
            # Accepted alternate spelling, legitimately hosted.
            _asset(good_name),
        ]
    )
    with patch(_FETCH_SEAM, new=AsyncMock(return_value=release)):
        r = _download(client, os="linux", arch="x64", kind="appimage")
    assert r.status_code == 302
    assert r.headers["location"] == f"{_PREFIX}{_TAG}/{good_name}", (
        "The prefix guard must skip the off-prefix asset and keep resolving, "
        f"got {r.headers['location']!r}"
    )


# ── Scenario 5: unsupported combinations and invalid values ─────────────────


def test_download_falls_back_for_unsupported_combinations_without_calling_github(
    client: TestClient,
) -> None:
    """
    A tuple of individually-valid values that we publish no asset for is a
    resolution miss, not a request error:
      1. linux/arm64 (appimage and deb) → releases page
      2. linux/x64/dmg → releases page
      3. darwin/*/deb and darwin/*/appimage → releases page
      4. None of these reach the GitHub seam at all — the shape check runs
         before the fetch
    """
    unsupported = [
        ("linux", "arm64", "appimage"),
        ("linux", "arm64", "deb"),
        ("linux", "arm64", "dmg"),
        ("linux", "x64", "dmg"),
        ("darwin", "arm64", "deb"),
        ("darwin", "x64", "deb"),
        ("darwin", "arm64", "appimage"),
        ("darwin", "x64", "appimage"),
    ]

    fetch = AsyncMock(return_value=_published_release())
    with patch(_FETCH_SEAM, new=fetch):
        for os_, arch, kind in unsupported:
            r = _download(client, os=os_, arch=arch, kind=kind)
            assert r.status_code == 302, (
                f"{os_}/{arch}/{kind}: expected a 302 fallback, got {r.status_code}"
            )
            assert r.headers["location"] == _RELEASES_PAGE, (
                f"{os_}/{arch}/{kind} resolved to {r.headers['location']!r}"
            )

    fetch.assert_not_awaited()


def test_download_rejects_values_outside_the_declared_enums(
    client: TestClient,
) -> None:
    """
    An unrecognised value is a malformed request, and says so:
      1. os=windows → 422
      2. arch=x86 → 422
      3. kind=exe → 422
      4. A missing parameter → 422
      5. None of these reach the GitHub seam
    """
    invalid = [
        {"os": "windows", "arch": "x64", "kind": "exe"},
        {"os": "windows", "arch": "x64", "kind": "dmg"},
        {"os": "darwin", "arch": "x86", "kind": "dmg"},
        {"os": "darwin", "arch": "arm64", "kind": "exe"},
        {"os": "darwin", "arch": "arm64"},
        {"arch": "arm64", "kind": "dmg"},
    ]

    fetch = AsyncMock(return_value=_published_release())
    with patch(_FETCH_SEAM, new=fetch):
        for params in invalid:
            r = client.get(_URL, params=params, follow_redirects=False)
            assert r.status_code == 422, (
                f"{params} should be rejected by validation, got {r.status_code}: "
                f"{r.text}"
            )

    fetch.assert_not_awaited()


# ── Scenario 6: the endpoint is public by design ────────────────────────────


def test_download_is_public_and_requires_no_authentication(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The visitor has not signed in yet when they click the download button:
      1. No Authorization header at all → 302 to the asset
      2. A garbage bearer token does not turn it into a 401 — the route has no
         auth dependency to fail
      3. A valid session works identically, so nothing about the answer depends
         on who is asking
    """
    fetch = AsyncMock(return_value=_published_release())
    expected = f"{_PREFIX}{_TAG}/cinna-desktop-{_VERSION}-arm64.dmg"

    with patch(_FETCH_SEAM, new=fetch):
        # ── Phase 1: Anonymous ────────────────────────────────────────────
        anon = _download(client, os="darwin", arch="arm64", kind="dmg")
        assert anon.status_code == 302, anon.text
        assert anon.headers["location"] == expected

        # ── Phase 2: Garbage credentials are simply ignored ───────────────
        bogus = _download(
            client,
            os="darwin",
            arch="arm64",
            kind="dmg",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert bogus.status_code == 302, (
            f"A public endpoint must not 401 on a bad token, got {bogus.status_code}"
        )
        assert bogus.headers["location"] == expected

        # ── Phase 3: Authenticated caller gets the same answer ────────────
        authed = _download(
            client,
            os="darwin",
            arch="arm64",
            kind="dmg",
            headers=superuser_token_headers,
        )
        assert authed.status_code == 302
        assert authed.headers["location"] == expected


# ── Scenario 7: mirror mode ─────────────────────────────────────────────────


def test_download_mirror_mode_addresses_the_mirror_by_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    DESKTOP_DOWNLOAD_BASE_URL set (the air-gapped case):
      1. Every supported combination redirects to {base}/{os}/{arch}/{kind}
      2. GitHub is never contacted — an air-gapped install has no route to it
      3. An unsupported combination answers the same way as in GitHub mode:
         a fallback, here to the mirror's own index {base}/
      4. Invalid enum values are still 422 in mirror mode
      5. A trailing slash on the configured base is normalised away

    This test is also the proof that patching ``settings`` reaches the service:
    the field defaults to "" (GitHub mode), so a no-op patch would send Phase 1
    to github.com and fail.
    """
    base = "https://mirror.internal.test/cinna-desktop"
    monkeypatch.setattr(settings, "DESKTOP_DOWNLOAD_BASE_URL", base)

    fetch = AsyncMock(return_value=_published_release())
    with patch(_FETCH_SEAM, new=fetch):
        # ── Phase 1: Addressed by shape, not by filename ──────────────────
        for os_, arch, kind in [
            ("darwin", "arm64", "dmg"),
            ("darwin", "x64", "dmg"),
            ("linux", "x64", "appimage"),
            ("linux", "x64", "deb"),
        ]:
            r = _download(client, os=os_, arch=arch, kind=kind)
            assert r.status_code == 302, r.text
            assert r.headers["location"] == f"{base}/{os_}/{arch}/{kind}", (
                f"{os_}/{arch}/{kind} → {r.headers['location']!r}"
            )
            assert "github.com" not in r.headers["location"]

        # ── Phase 3: Unsupported combos fall back to the mirror index ─────
        for os_, arch, kind in [
            ("linux", "arm64", "appimage"),
            ("linux", "x64", "dmg"),
            ("darwin", "arm64", "deb"),
        ]:
            r = _download(client, os=os_, arch=arch, kind=kind)
            assert r.status_code == 302
            assert r.headers["location"] == f"{base}/", (
                f"{os_}/{arch}/{kind} → {r.headers['location']!r}; an air-gapped "
                f"install must not be sent to github.com"
            )

        # ── Phase 4: Validation is unchanged in mirror mode ───────────────
        r = client.get(
            _URL,
            params={"os": "windows", "arch": "x64", "kind": "exe"},
            follow_redirects=False,
        )
        assert r.status_code == 422

    # ── Phase 2: GitHub was never contacted ───────────────────────────────
    fetch.assert_not_awaited()

    # ── Phase 5: A trailing slash on the base is normalised ───────────────
    monkeypatch.setattr(settings, "DESKTOP_DOWNLOAD_BASE_URL", base + "/")
    DesktopReleaseService.reset_cache()
    with patch(_FETCH_SEAM, new=AsyncMock(return_value=_published_release())):
        r = _download(client, os="linux", arch="x64", kind="deb")
    assert r.status_code == 302
    assert r.headers["location"] == f"{base}/linux/x64/deb"
