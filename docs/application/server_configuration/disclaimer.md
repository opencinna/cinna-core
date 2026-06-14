# Disclaimer

## Purpose

Allow a superuser to display a server-wide notice to users at login — rendered as Markdown in a blocking modal that must be acknowledged before the user can proceed. Common uses include acceptable-use policies, data-handling notices, and platform-specific guidance that every user must read.

## Core Concepts

- **Disclaimer Modal** — A non-dismissible dialog rendered at login. No outside-click, Escape key, or close button; the user must click "I Understand" to proceed
- **Display Mode** — Controls when the modal re-appears:
  - **New User Only** (`new_users`) — shown once and remembered in `localStorage`; the user never sees it again on the same browser unless the disclaimer content or mode changes
  - **Every Login** (`every_login`) — shown once per browser session via `sessionStorage`; reappears on every new tab or fresh browser start
- **Versioned Acknowledgement** — The backend tracks a `disclaimer_version` integer. Any change to the Markdown content or display mode increments the version. Because storage keys are versioned (`disclaimer_ack_v{version}` / `disclaimer_session_v{version}`), all previous acknowledgements are automatically invalidated and users see the updated disclaimer
- **Singleton Config** — One `ServerConfig` row holds all settings. There is no per-user or per-agent configuration; the same disclaimer applies to everyone on the instance
- **Browser-Storage Only** — No per-user DB record tracks acknowledgement. Dismissal is tracked entirely in client-side browser storage

## Admin User Stories / Flows

### Enabling the Disclaimer

1. Superuser opens **Admin → Server Configuration → Interface**
2. Finds the **Disclaimer** card
3. Clicks **"Edit Disclaimer Message"** — a dialog opens with a split-pane editor showing a Markdown textarea on the left and a live rendered preview on the right
4. Writes or pastes the disclaimer text (supports full Markdown: headings, bold, lists, links, code blocks)
5. Clicks **Save** — the message is stored; if content differs from what was previously saved, `disclaimer_version` increments automatically
6. Toggles the **Enable/Disable** switch in the card header to turn the disclaimer on
7. Optionally changes **"Show disclaimer"** from "New User Only" to "Every Login" (or vice versa); changing this also increments the version

### Disabling the Disclaimer

1. Superuser flips the enable switch to off
2. The modal immediately stops appearing for all users on next page load (no cache delay)

### Editing an Existing Disclaimer

1. Superuser reopens **Edit Disclaimer Message**, makes changes, saves
2. `disclaimer_version` increments if the text or mode changed
3. All users — including those who had previously acknowledged — will be shown the updated disclaimer again on their next visit

## End-User Stories / Flows

### First Login — Disclaimer Active

1. User logs in or opens the Dashboard
2. Dashboard fetches the disclaimer projection (`GET /server-config/disclaimer`)
3. If `enabled` is true, the markdown is non-empty, and the user has not yet acknowledged this version in browser storage, `DisclaimerModal` opens immediately
4. User reads the Markdown-rendered notice (scrollable if long)
5. User clicks **"I Understand"** — the acknowledgement is written to browser storage
6. If the user had just completed API-key onboarding and the Getting Started Modal was pending, it opens now; otherwise the Dashboard proceeds normally

### New User — Disclaimer Followed by Getting Started Modal

When both the disclaimer and the Getting Started Modal would show for a new user:
1. Disclaimer appears first (blocking)
2. User acknowledges the disclaimer
3. Getting Started Modal opens immediately after
4. The two modals never overlap

### Every Login Mode

1. User closes the browser (session storage cleared)
2. On next login the disclaimer storage key is absent from `sessionStorage`
3. Disclaimer modal appears again regardless of previous acknowledgements
4. User clicks "I Understand" to proceed

## Business Rules

- **Superuser only** — Only users with `is_superuser=true` can read or write the full `ServerConfig`. Any authenticated user can read the `DisclaimerPublic` projection (needed so the Dashboard can evaluate the gate client-side)
- **Non-dismissible** — Outside-click, Escape key, and the dialog's built-in close button are all suppressed. The only dismissal path is the "I Understand" button
- **Disclaimer precedes onboarding** — When the Getting Started Modal would auto-open (after API-key onboarding), it is deferred until the disclaimer is acknowledged. The two cannot appear simultaneously
- **Version bump triggers re-display** — `disclaimer_version` increments only when `disclaimer_markdown` or `disclaimer_display_mode` actually changes, not on every save. Toggling `disclaimer_enabled` alone does not bump the version
- **Empty markdown guard** — A disclaimer with no markdown text is never shown even when `enabled=true` (the client checks `disclaimer.markdown.trim()`)
- **No DB migration required for users** — Acknowledgement state lives entirely in browser storage; there is no server-side per-user record to migrate or purge
- **Singleton lazily created** — The `server_config` table row is created on the first authenticated request if it does not yet exist (default: `disclaimer_enabled=false`)

## Integration Points

- **Getting Started / Onboarding** — The dashboard defers the Getting Started Modal until after the disclaimer is acknowledged. The ordering is enforced by the `shouldShowDisclaimer` flag and the `pendingGettingStarted` state variable. See [Getting Started](../getting_started/getting_started.md)
- **Authentication** — The disclaimer gate runs on the Dashboard after the user is already authenticated. It is not part of the login flow itself — users complete authentication first, then see the disclaimer
- **Admin Agent Environments** — Lives in the same admin surface (`Admin →`) as the superuser-only routes; uses the same `is_superuser` guard pattern. See [Admin Agent Environments](../admin_agent_environments/admin_agent_environments.md)

---

*Last updated: 2026-06-14*
