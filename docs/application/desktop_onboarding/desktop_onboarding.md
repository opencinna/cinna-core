# Desktop One-Click Onboarding

## Overview

The path from "an admin created your account" to "Cinna Desktop is running and set up" with no terminal, no invite token, and exactly one sign-in. The new-account email's primary button opens a **public** landing page at `/desktop`; the visitor downloads the correct build for their machine in one click, installs it, clicks **Open Cinna Desktop** (a `cinna://connect?server=…` deep link), authorizes once in the browser through the **pre-existing** desktop OAuth flow, and the desktop then prepares its cinna-cli account workspace by itself against the **pre-existing** `POST /api/v1/cli/account/setup-tokens`.

Nothing about authentication or token minting is new here. This feature adds three things around the existing machinery: a way for a browser to find the right installer, a way for a person to hand the desktop a server address, and a way for the desktop to discover that this instance offers the local-dev bootstrap at all.

## Core Concepts

- **Landing page (`/desktop`)** — Public SPA route, no auth guard. Deliberately unguarded: it is the link in a welcome email, so the visitor has not signed in anywhere yet and a login wall would sit in front of a download button.
- **Download resolver** — `GET /api/v1/desktop/download?os=…&arch=…&kind=…`, public. Resolves the current `opencinna/cinna-desktop` GitHub release and **302-redirects** to the asset. It never proxies installer bytes.
- **`local_dev` discovery block** — An optional object in `/.well-known/cinna-desktop` telling a desktop client where the account-workspace setup-token endpoint lives and which cinna-cli / Mutagen versions this instance is pinned to.
- **`cinna://connect?server=<origin>` deep link** — The one value the browser hands the native app: this instance's **server origin**, the root the desktop resolves `/.well-known/cinna-desktop` against.
- **Server origin vs. API base** — Two different things on a reverse-proxy deployment. The API base may carry a path (`https://app.example.com/api`); the server origin never does. Backend URLs are built from the base; the desktop is handed the origin.

## User Flows

### First run, from the welcome email

1. An admin creates the user. `generate_new_account_email` sends the welcome mail with the primary button **Get Cinna Desktop** pointing at `{FRONTEND_HOST}/desktop`, and a secondary line — *"Prefer the browser? Log in on the web"* — pointing at the bare SPA origin.
2. The visitor opens `/desktop`. The page detects their OS from the user agent and (on Chromium) their CPU architecture from `navigator.userAgentData.getHighEntropyValues(["architecture"])`.
3. They click the single download button. The browser hits the resolver, which 302s to the matching GitHub release asset; the file downloads directly from GitHub.
4. They install and launch the app, then return to the page and click **Open Cinna Desktop**. The `cinna://` deep link hands the app this instance's server origin.
5. The desktop runs the ordinary OAuth + PKCE flow (see [Desktop App Authentication](../desktop_auth/desktop_auth.md)). The visitor approves once in the browser.
6. On the consent success screen the SPA now offers **Return to Cinna Desktop** — the same deep link — for browsers that keep the tab in front rather than refocusing the app.
7. The desktop fetches `/.well-known/cinna-desktop`, reads `local_dev.setup_token_endpoint`, and calls it with its own OAuth bearer to bootstrap a cinna-cli account workspace (see [Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md)).

### Detection failed, or an unsupported machine

- OS undetectable (or a mobile/ChromeOS user agent, which is ruled out on purpose) → the page shows both platforms and asks the visitor to pick.
- macOS → defaults to Apple Silicon with an **Intel Mac?** toggle; the architecture hint corrects the default asynchronously when the browser supplies it, and an explicit click always wins over a late detection result.
- Linux → defaults to the AppImage with a **.deb** toggle.
- Linux on arm64 → **no download button at all**. There is no published arm64 Linux asset, so the page links to the releases index instead. Offering an x86-64 button next to the words "x86-64 only" would put the wrong binary one click away.

### Already installed, deep link does not fire

The page always shows the resolved server origin as selectable, copyable text under the button. That is the escape hatch: the person types (or pastes) the address into the app by hand. It is by construction the same string the deep link carries, so the two paths cannot resolve to different servers.

## Business Rules

- **The resolver only ever redirects.** The backend never becomes a distribution channel for executable content — it cannot launder a binary behind a trusted origin, cannot be turned into a bandwidth amplifier by an anonymous caller, and a compromised release never transits platform infrastructure. Every redirect target must sit under `https://github.com/opencinna/cinna-desktop/releases/download/`; anything else in the upstream payload is ignored.
- **Failure is never a 5xx.** Network error, timeout, non-200, malformed payload, no matching asset — every one of them degrades to the human-readable releases page, so the visitor always lands somewhere they can finish by hand.
- **Exactly four supported combinations**: `darwin/arm64/dmg`, `darwin/x64/dmg`, `linux/x64/appimage`, `linux/x64/deb`.
  - An **invalid enum value** (`os=windows`) is a malformed request → `422`, before any resolution runs.
  - A **valid-but-unpublished combination** (`linux/arm64/*`, `linux/*/dmg`, `darwin/*/deb`) is a resolution miss, not a request error → `302` to the releases page.
- **Release metadata is cached one hour per worker process** (failures for 60 s) behind a single-flight lock. The anonymous GitHub API allows 60 requests/hour/IP; a public unauthenticated endpoint must not spend that budget per visitor. A deployment running N uvicorn workers makes N metadata fetches an hour, not one.
- **Drafts and pre-releases are refused** even if GitHub were to return one, so a first-run user is never handed an unreleased build.
- **Mirror mode addresses assets by shape, not by version.** When `DESKTOP_DOWNLOAD_BASE_URL` is set the resolver skips GitHub entirely and redirects to `{base}/{os}/{arch}/{kind}`, letting the mirror decide which build that shape currently resolves to. This is deliberate: the use case is an air-gapped install, which by definition cannot reach `api.github.com`, so mirror mode must not need a version. In mirror mode the fallback is the mirror's own index, not a github.com page the host cannot load. The unsupported-combination check runs **before** the mirror branch, so both modes answer an impossible request identically.
- **The new-account email's button is the desktop landing page**, not the dashboard. The bare SPA origin survives as the secondary web-login link.

## The two-knobs hazard (record, not reconcile)

**Invariant:** `VITE_API_URL` (or the SPA origin when it is unset) and `BACKEND_BASE_URL` must resolve to the same origin.

The backend and the SPA hold **two independent settings for the same value**:

- `settings.backend_base_url` (`backend/app/core/config.py`) resolves `BACKEND_BASE_URL` → `WEBHOOK_BASE_URL` → `FRONTEND_HOST` → `https://localhost`. Discovery's `setup_token_endpoint` is built from it.
- The SPA's build-time `VITE_API_URL` (via `OpenAPI.BASE`, falling back to the page origin) is what the landing page's `cinna://` deep link and paste text carry.

A deployment can set them inconsistently. The desktop then receives a server address that disagrees with what discovery advertises, and **nothing on either side surfaces the mismatch** — no error, no warning, no failed request on the page itself. The user just sees "the desktop won't connect."

**We chose to record this rather than reconcile it.** The landing page's use of `VITE_API_URL` is the correct choice for its job: it is the value the browser has *proven reachable* by loading the page from it, and it is the value shown to a human in the paste box — which is the escape hatch when everything else fails. Deriving the displayed address from `backend_base_url` instead would put an unverifiable server-side string in front of a person and call it the answer.

The hazard **pre-dates this feature.** `authorization_endpoint`, `token_endpoint`, and `userinfo_endpoint` have always been built from `backend_base_url` (`backend/app/main.py`). This feature adds a fourth consumer of the same knob, not a new class of problem.

## `DESKTOP_LOCAL_DEV_ENABLED` is not a kill switch

Turning `DESKTOP_LOCAL_DEV_ENABLED` off removes the `local_dev` block from the discovery document, so a well-behaved desktop stops offering local dev. **That is all it does.** `POST /api/v1/cli/account/setup-tokens` stays fully reachable by any client that knows the URL, and is governed solely by its own guards — `RoleService.require_developer` plus the `NoCliExchangedSession` dependency. Those guards, not this setting, decide who may mint a setup token.

Believing otherwise is security-shaped: an operator who flips this flag expecting the mint route to close has closed nothing.

## `local_dev`'s presence is not a permission

The discovery document is served to **every anonymous visitor**, before any user is known. Its `local_dev` block therefore says "this instance offers the bootstrap", never "you may use it". A desktop whose signed-in user holds the `agent-user` role will read the block, call the endpoint, and get a `403` from `require_developer` — and that is the intended shape. Role gating belongs in the capability reply, not in a document served to nobody in particular. This is a cross-repo contract point with cinna-desktop: the desktop must treat a `403` there as a normal, expected answer rather than a broken instance.

See [User Roles](../user_roles/user_roles.md) for the role tiers.

## Cross-repo contract with cinna-desktop

1. **`server` is percent-encoded.** The deep link is built with `URLSearchParams`, which encodes the `:` and `//` of the origin. The desktop's `cinna://` handler **must percent-decode** the `server` value. Change this in both repos together.
2. **`local_dev` is optional and additive.** The six pre-existing discovery keys are unchanged; a desktop that does not understand `local_dev` ignores it. Adding the block is backward compatible.
3. **`cinna_cli_version` is a pin, not "latest".** It is the CLI release this instance was verified against, served identically from discovery and from `/api/v1/cli/agents/{id}/sync-runtime`, so the two cannot drift.
4. **A `403` from the setup-token endpoint is a role answer, not an outage** (see above).

## Architecture Overview

```
Admin creates user
  → new-account email  ("Get Cinna Desktop" → {FRONTEND_HOST}/desktop)
      → SPA /desktop (public)
          ├─ GET /api/v1/desktop/download?os&arch&kind
          │     → DesktopReleaseService → GitHub releases API (cached 1h)
          │     → 302 → github.com/opencinna/cinna-desktop/releases/download/…
          │        (or {DESKTOP_DOWNLOAD_BASE_URL}/{os}/{arch}/{kind})
          ├─ cinna://connect?server=<origin>   → Cinna Desktop
          └─ copyable <origin>                  → typed into Cinna Desktop
      → Cinna Desktop
          → GET /.well-known/cinna-desktop           (instance metadata + local_dev)
          → desktop OAuth + PKCE (existing)          → consent page → tokens
          → POST /api/v1/cli/account/setup-tokens    (existing, developer-gated)
             → cinna-cli account workspace
```

## Integration Points

- [Desktop App Authentication](../desktop_auth/desktop_auth.md) — owns `/.well-known/cinna-desktop`, the OAuth + PKCE flow, and the consent page this feature extends with a "Return to Cinna Desktop" button. The discovery `local_dev` block is gated on `DESKTOP_AUTH_ENABLED` **in addition to** `DESKTOP_LOCAL_DEV_ENABLED`, never instead of it.
- [Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md) — owns `POST /api/v1/cli/account/setup-tokens`, the route `local_dev` advertises. Untouched by this feature.
- [Cinna CLI Integration](../cinna_cli_integration/cinna_cli_integration.md) — `CLIService.get_sync_runtime_info` now also returns `cinna_cli_version`, from the same setting discovery serves.
- [User Roles](../user_roles/user_roles.md) — `require_developer` is what actually decides whether the desktop's bootstrap call succeeds; `agent-user` gets a `403`.
- [Local Agent Kit](../local_agent_kit/local_agent_kit.md) — the other unauthenticated onboarding surface. Distinct audiences: the kit serves a local coding assistant with no Cinna account; this page serves a person who has just been given one.
- [System Notifications](../system_notifications/system_notifications.md) — shares the MJML email-template build convention (see the mjml-version note in the tech doc); the new-account email itself is not a catalog notification.
- [Nginx Setup](../../infrastructure/nginx_setup.md) — `/.well-known/cinna-desktop` must reach the backend at origin root through the reverse proxy for any of this to work.
