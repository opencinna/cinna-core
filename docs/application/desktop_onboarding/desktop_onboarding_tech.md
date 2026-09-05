# Desktop One-Click Onboarding — Technical Details

## File Locations

### Backend — Routes

- `backend/app/api/routes/desktop_download.py` — public `GET /desktop/download` resolver route; registered in `backend/app/api/main.py` (so it is served under `API_V1_STR`)
- `backend/app/main.py` — `cinna_desktop_discovery()` serves `/.well-known/cinna-desktop` at app root (not under `/api/v1`) and emits the optional `local_dev` block

### Backend — Services

- `backend/app/services/desktop_auth/desktop_release_service.py` — `DesktopReleaseService`, the whole download-resolution surface
- `backend/app/services/cli/cli_service.py` — `CLIService.get_sync_runtime_info()` now also returns `cinna_cli_version`

### Backend — Email

- `backend/app/utils.py` — `generate_new_account_email()` builds `link = f"{settings.FRONTEND_HOST}/desktop"` and `web_link = settings.FRONTEND_HOST`
- `backend/app/email-templates/src/new_account.mjml` — MJML source; button label is "Get Cinna Desktop", followed by a "Prefer the browser? Log in on the web" text row bound to `{{ web_link }}`
- `backend/app/email-templates/build/new_account.html` — the compiled template actually read at runtime

### Backend — Configuration

- `backend/app/core/config.py` — `CINNA_CLI_VERSION`, `DESKTOP_LOCAL_DEV_ENABLED`, `DESKTOP_DOWNLOAD_BASE_URL` (with a startup model validator), plus the pre-existing `MUTAGEN_VERSION`, `DESKTOP_AUTH_ENABLED`, and the `backend_base_url` computed property

### Backend — Tests

- `backend/tests/api/desktop_auth/test_desktop_discovery_local_dev.py` — 4 tests: block present when both gates are on, omitted when `DESKTOP_LOCAL_DEV_ENABLED` is off, omitted when `DESKTOP_AUTH_ENABLED` is off, and `local_dev.cinna_cli_version` matches what `/sync-runtime` reports
- `backend/tests/api/desktop_auth/test_desktop_download.py` — 10 tests: per-platform asset selection, cache isolation, alternate Linux spellings, resolution-failure fallback, draft/pre-release and bad-upstream refusal, out-of-prefix asset refusal, unsupported combinations without calling GitHub, enum rejection (422), public/no-auth, mirror mode by shape
- `backend/tests/api/auth/test_new_account_email.py` — 3 tests: the mail leads with the desktop landing page, both URLs track `FRONTEND_HOST`, and the rendered HTML has no unrendered placeholders

### Frontend

- `frontend/src/routes/desktop.tsx` — public route; deliberately no `beforeLoad` guard (contrast the sibling `desktop-auth/consent` route)
- `frontend/src/components/Desktop/DesktopLandingPage.tsx` — the page itself
- `frontend/src/utils.ts` — `resolveApiBase()`, `resolveServerOrigin()`, `cinnaConnectDeepLink()`
- `frontend/src/components/Auth/NativeAuthConsentPage.tsx` — success state gained a "Return to {appLabel}" button, rendered only when `!isMobile`
- `frontend/src/routeTree.gen.ts` — regenerated for the new route
- `frontend/src/client/{sdk,types,schemas}.gen.ts` — regenerated; the route surfaces as `DesktopDownloadService.downloadDesktop`. **The landing page does not use it** — a redirect to an installer has to be a browser navigation, so the page builds the URL by hand and renders an `<a href>`

### Database

No models, no tables, no migration. Nothing about this feature is persisted.

## API Endpoints

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/.well-known/cinna-desktop` | none | Instance metadata. Six always-present keys (`instance_name`, `authorization_endpoint`, `token_endpoint`, `userinfo_endpoint`, `version`, `desktop_auth_enabled`) plus an optional `local_dev` object |
| GET | `/api/v1/desktop/download` | none | `os=darwin\|linux`, `arch=arm64\|x64`, `kind=dmg\|appimage\|deb`, all required. `302` on every path; `422` only for a value outside the declared `Literal`s |
| POST | `/api/v1/cli/account/setup-tokens` | `CurrentUser` + `require_developer` + `NoCliExchangedSession` | **Pre-existing, unchanged.** Advertised by `local_dev.setup_token_endpoint`; see [Account CLI Workspace](../cinna_cli_integration/account_cli_workspace_tech.md) |

### `local_dev` block

Emitted only when `settings.DESKTOP_AUTH_ENABLED and settings.DESKTOP_LOCAL_DEV_ENABLED`. Fields:

| Field | Source |
|-------|--------|
| `setup_token_endpoint` | `f"{settings.backend_base_url}{settings.API_V1_STR}/cli/account/setup-tokens"` |
| `cinna_cli_version` | `settings.CINNA_CLI_VERSION` |
| `mutagen_version` | `settings.MUTAGEN_VERSION` |

Backward compatible in both directions: a desktop that predates the block ignores unknown keys, and the six existing keys are byte-identical to what they were.

## Services & Key Methods

### `DesktopReleaseService` (`backend/app/services/desktop_auth/desktop_release_service.py`)

Class-level, no instances, no DB session.

- `resolve_download_url(os, arch, kind)` — the only public entry point. Always returns a usable URL; the caller never handles an error.
- `get_latest_release()` — cached-and-single-flighted read of the GitHub payload; returns `None` for "unresolvable" and never raises.
- `reset_cache()` — drops the cached release and the loop-bound lock. Written for tests, but ordinary public API; a production call just forces one re-fetch.
- `_fetch_latest_release()` — the single seam tests patch to fake GitHub, and the single place upstream errors are swallowed. Rejects non-200, malformed JSON, non-object payloads, and drafts/pre-releases.
- `_asset_patterns(os, arch, kind)` — returns the regexes matching this combination's filename, most-preferred first; empty for an unpublished combination.
- `_mirror_url(os, arch, kind)` — `{DESKTOP_DOWNLOAD_BASE_URL}/{os}/{arch}/{kind}`.
- `_fallback_url()` — the mirror's index when a mirror is configured, otherwise the GitHub releases page.

Constants worth knowing:

| Constant | Value / purpose |
|----------|-----------------|
| `GITHUB_REPO` | `opencinna/cinna-desktop` (hard-coded) |
| `ASSET_URL_PREFIX` | `https://github.com/opencinna/cinna-desktop/releases/download/` — every redirect target must start with it, so a tampered upstream payload cannot turn a public endpoint into an open redirect |
| `CACHE_TTL_SECONDS` | `3600` |
| `FAILURE_CACHE_TTL_SECONDS` | `60` |
| `_HTTP_TIMEOUT` | `10.0` s |
| `_cache` / `_lock` / `_lock_loop` | Class-level. The lock is rebuilt when the running event loop changes — an `asyncio.Lock` binds to the first loop that awaits it, which would be a hazard under per-test loops. The cache is class state that outlives one test, so a suite faking a release **must** reset between tests or the fake leaks forward |

### Asset naming — reality, and what the plan predicted

`_ASSET_SHAPES` maps `(os, kind)` → `(extension, {arch: (tokens…)})`, tokens most-preferred first. Matching is on the arch + extension **suffix** (`^cinna-desktop-.+-<token>\.<ext>$`, case-insensitive), not on a rendered version string, so it survives version-numbering changes; anchoring on the extension keeps `.blockmap` sidecars and `-mac.zip` updater artifacts out.

| os | arch | kind | Real asset suffix (v0.2.11) | Tokens accepted |
|----|------|------|------------------------------|-----------------|
| darwin | arm64 | dmg | `-arm64.dmg` | `arm64` |
| darwin | x64 | dmg | `-x64.dmg` | `x64` |
| linux | x64 | appimage | `-x86_64.AppImage` | `x86_64`, `x64`, `amd64` |
| linux | x64 | deb | `-amd64.deb` | `amd64`, `x64`, `x86_64` |

**The plan was wrong here.** `docs/plans/desktop_one_click_onboarding_plan.md` predicted `-x64.AppImage` and `-x64.deb`. electron-builder spells Linux architectures the way each packaging world does, not the way Node does. The shipped code accepts all three spellings per Linux kind so a rename in the desktop's build config cannot silently break the download button; patterns are tried outer-loop so a release carrying two accepted spellings resolves to the preferred one rather than to whichever GitHub listed first.

`linux/arm64` is **absent from the map on purpose** — there is no published asset, and serving an x64 binary instead would be worse than the releases-page fallback. Same for `darwin/*/deb`, `darwin/*/appimage`, `linux/*/dmg`.

### `CLIService.get_sync_runtime_info()` (`backend/app/services/cli/cli_service.py`)

Now returns `{mutagen_version, mutagen_agent_sha256, platform_api_version, cinna_cli_version}`. `cinna_cli_version` comes from the same `settings.CINNA_CLI_VERSION` the discovery block serves, so `cinna doctor` and Cinna Desktop can never be told different pins.

## Frontend Components

### `frontend/src/utils.ts`

Three helpers, and the distinction between the first two is the whole point:

- `resolveApiBase()` — the absolute base the API is served from, **which may carry a path** (`https://app.example.com/api` when `VITE_API_URL=/api`). Use it to build backend route URLs: `${resolveApiBase()}/api/v1/…` mirrors exactly how the generated client prepends `OpenAPI.BASE`. Normalizes three shapes of unvalidated build input: absent/`"undefined"` (a stringified unset variable — `||` alone would treat it as a good base), relative, and trailing-slashed. Scheme-matched with `/^https?:\/\//i`, not `startsWith("http")`.
- `resolveServerOrigin()` — a true origin, `scheme://host[:port]`, no path. This is what a native client gets. `/.well-known/cinna-desktop` is mounted at the **app root**, so handing over the API base with `VITE_API_URL=/api` would make the desktop discover against `…/api/.well-known/cinna-desktop` and 404 while downloads on the same page kept working — nothing would look wrong. Deliberately not `window.location.origin` either: on a split-host deployment the SPA origin has no backend at all.
- `cinnaConnectDeepLink()` — `cinna://connect?${new URLSearchParams({ server: resolveServerOrigin() })}`. `URLSearchParams` **percent-encodes** the `:` and `//`, so the desktop's custom-scheme handler must percent-decode the `server` value. Cross-repo contract with cinna-desktop; change it in both repos together.

### `frontend/src/components/Desktop/DesktopLandingPage.tsx`

- `detectPlatform()` — returns `"darwin" | "linux" | null`. Rules out Android (its UA contains "Linux"), iPhone/iPod, iPadOS 13+ (identifies as "Macintosh"; disambiguated by `navigator.maxTouchPoints > 1`), and ChromeOS (matches "X11 Linux", no build) **before** the positive matches. `null` is a real answer — the page then offers both platforms rather than rendering a button that installs the wrong thing.
- Architecture detection uses `navigator.userAgentData.getHighEntropyValues(["architecture"])` (Chromium-only, async, and rejectable by Permissions-Policy). The page renders the arm64 default first and corrects itself if an answer arrives; `chosenMacArch ?? detectedArch ?? "arm64"` means an explicit click always wins over a late result. The effect is cancellation-guarded.
- `apiBase`, `serverOrigin`, and `deepLink` are each `useMemo`'d once — all three derive from build-time config and the page URL, neither of which changes while the page is open.
- `downloadUrl()` takes the **API base**, not the origin, because it builds a backend route.
- The copy-to-clipboard tick reverts on a timer cleared on unmount.
- Both toggling labels sit in an `aria-live="polite"` span, since the toggle rewrites the button text in place.

### `frontend/src/components/Auth/NativeAuthConsentPage.tsx`

The success state renders a "Return to {appLabel}" button carrying `cinnaConnectDeepLink()`, gated on `!isMobile`: `cinna://` has no mobile handler, so offering it there would advertise a dead link. The desktop app also focuses itself on the OAuth callback, so this is a fallback for browsers that keep the tab in front, not the primary return path. The existing 10 s `window.close()` timer is untouched.

## Configuration

| Setting | Default | Purpose |
|---------|---------|---------|
| `CINNA_CLI_VERSION` | `"0.4.0"` | The cinna-cli release this instance's onboarding flow is pinned to. Served by both the discovery `local_dev` block and `/cli/agents/{id}/sync-runtime`. A pin, not "latest" — bump when a newer CLI is verified here. **0.4.0 is a floor, not just the newest**: the non-interactive surface the desktop drives (`account set-token`, `--no-input`, `--json`, stable exit codes) landed in 0.4.0, and `--no-input` does not exist in 0.3.0 at all — advertising an earlier pin would hand the desktop a CLI that stops at an interactive prompt |
| `DESKTOP_LOCAL_DEV_ENABLED` | `True` | Whether the instance **advertises** the local-dev bootstrap. Gates the `local_dev` block only — see the warning below |
| `DESKTOP_DOWNLOAD_BASE_URL` | `""` | Empty → resolve from GitHub. Set → skip GitHub and redirect to `{base}/{os}/{arch}/{kind}` |
| `DESKTOP_AUTH_ENABLED` | `True` | Pre-existing. The `local_dev` block is gated on this **in addition**, never instead |
| `MUTAGEN_VERSION` | `"0.18.1"` | Pre-existing. Same value in `local_dev` and in `/sync-runtime` |
| `BACKEND_BASE_URL` | — | Pre-existing. Feeds `settings.backend_base_url`, which builds `setup_token_endpoint` |
| `VITE_API_URL` (frontend build) | — | Pre-existing. Feeds `OpenAPI.BASE`, which the landing page's deep link and paste text derive from |

`DESKTOP_DOWNLOAD_BASE_URL` is **scheme-validated at startup** by a model validator: a non-empty value must begin with `http://` or `https://` or the app refuses to boot. This value is served to unauthenticated visitors as a `Location` header, so a scheme-less `cdn.internal/desktop` would redirect relative to the API, and `//host` or a `javascript:` value would pass through verbatim. Failing at startup costs the operator seconds and names the problem; failing at download time costs them a user and names nothing.

### Two knobs, one value

`settings.backend_base_url` and the SPA's `VITE_API_URL` are independent settings for the same value, and a deployment can set them inconsistently with **nothing surfacing the mismatch**. The invariant, the reasoning for recording rather than reconciling it, and the note that the hazard pre-dates this feature are in the [business-logic doc](desktop_onboarding.md#the-two-knobs-hazard-record-not-reconcile).

## Security

- **Public by design**: `/desktop`, `GET /api/v1/desktop/download`, and `/.well-known/cinna-desktop` are all unauthenticated. The visitor has no account session yet when they click the email's button.
- **No binary ever transits the backend.** Redirect-only, with `ASSET_URL_PREFIX` turning "we only redirect to our own release assets" from an assumption about the upstream response into a property of this code.
- **Open-redirect closed on both paths**: GitHub-resolved URLs are prefix-checked; mirror URLs are built from a startup-validated absolute base.
- **No new token types, no new mint route.** Plan §3.6's leave-alone list was verified untouched by code review: the mint route, the exchange route, `NoCliExchangedSession`, `RoleService.require_developer`, the `AccountCLIService` mint/exchange paths, and `MUTAGEN_VERSION`.
- **`DESKTOP_LOCAL_DEV_ENABLED` gates advertisement only.** Turning it off does **not** close `POST /api/v1/cli/account/setup-tokens`, which stays reachable by any client that knows the URL and is governed solely by `require_developer` + `NoCliExchangedSession`. It is not a kill switch, and treating it as one is a security-shaped mistake.
- **`local_dev` is not a capability grant.** It is served to every anonymous visitor; an `agent-user` desktop that reads it and calls the endpoint gets a `403`. Role gating lives in the capability reply, not in the discovery document.
- **Rate-limit hygiene**: the one-hour cache plus the single-flight lock keep an anonymous endpoint from mapping one inbound request to one outbound GitHub call — exactly the amplification the redirect-only design forecloses.

## MJML build trap

The committed `backend/app/email-templates/build/*.html` files are **mjml 4.x** output. `npx mjml` now resolves to **5.4.0**, which reformats the whole document — rebuilding `new_account.html` from source churns roughly 181 chunks of unrelated diff.

`new_account.html` was therefore **hand-edited**, and verified by building the old and the new `.mjml` sources under 5.4.0 and diffing those two outputs against each other: the delta is exactly the button label plus the new text row, and the inserted row is copied from the sibling row mjml already emits in `confirm_email.html`.

Neither `backend/README.md` (the "Email Templates" section, which points at the VS Code MJML extension) nor `docs/application/system_notifications/system_notifications_tech.md` (the `npx mjml … -o …` snippet) pins a version. **Both now under-specify**: following either instruction today produces a whole-file reformat rather than a reviewable diff. Anyone regenerating a build template should either pin mjml 4.x or accept — and separate — the reformat commit.
