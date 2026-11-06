---
feature: brand_themes
domain: application
one_liner: "Lets a server admin publish one brand theme - colors for both modes, a logo, backgrounds and a line of copy - that core validates on save and serves publicly for Cinna Desktop to apply."
docs:
  tech: brand_themes_tech.md
affects: [desktop_auth]
---
# Brand Themes

## Overview

A **brand theme** is the look of a Cinna server: a color for each of a fixed set of named roles in both dark and light mode, an optional logo and background image per mode, and an optional line of copy. An admin publishes one theme; every Cinna Desktop signed in to that server can then look like the brand. A server with no published theme looks like stock Cinna.

Themes are not meant to be written by hand. They are produced as a package (`theme.json` plus an `assets/` folder, or a zip of both), and core is the gatekeeper: a package that is incomplete, unreadable or unsafe is refused when it is uploaded, with errors specific enough for the author (a person or an agent) to fix and retry.

Core's web app applies the theme to its sign-in pages (login, MFA, signup, password recovery and reset, email confirmation, invitation acceptance, and the browser consent pages for Cinna Desktop, Cinna Mobile and `cinna login`). The rest of core's web app keeps its own look. Superusers manage themes on the **Admin → Brand Theme** page.

## Core Capabilities

- **One theme per server.** At most one theme is published at a time. There are no per-team or per-user themes, and users cannot opt out.
- **Drafts.** Every package that passes validation is stored as a draft. A package that fails is never stored: the upload response lists the problems. A bounded number of drafts is kept; deleting one frees its slot.
- **Publish and unpublish.** Publishing a draft makes it the server's theme and returns the previously published one to draft. Unpublishing returns everyone to stock Cinna. Publishing the theme that is already live with unchanged content is a no-op and does not change its revision.
- **Validation on save.** The same checks run at upload and again at publish, because the rules may have tightened since the upload. A failing re-check refuses the publish with the list of errors.
- **Public endpoint.** The published theme is readable without authentication so that sign-in screens can be branded before anyone has a token. Clients revalidate cheaply with the theme's revision.
- **Discovery.** When a theme is published, the desktop discovery document announces it, so a desktop knows to fetch it.
- **Branded sign-in pages.** The web sign-in pages take the theme's colors for the visitor's own light or dark mode. On wide screens the decorative panel beside the form shows the brand logo over the brand background (or a flat brand surface); the consent pages (Cinna Desktop, Cinna Mobile, `cinna login` device authorization) show the logo above their card with the background behind the whole page. Nothing moves when the theme arrives: only colors and images change, and with no theme, or a theme that fails the page's own safety check, the pages look exactly as before. Menus, toasts and tooltips opened from these pages keep the stock look.

## What Counts as a Valid Theme

- Both modes are present and every required color role is set in each; colors are literal values only (no CSS functions, variables or URLs).
- Text, accent, status and chart colors meet contrast thresholds against the surfaces they sit on; chart slots are distinguishable from each other; the user's chat bubble stands out; the accent does not read as the error color.
- A logo, when given, is one file per mode (SVG, PNG, JPEG or WebP), large enough, and not so wide it would be drawn too small.
- A background, when given, has both keys (an image or a deliberate "no image"), is large enough, and keeps the main text readable over almost all of the image. The dark background must be darker than the light one.
- SVG files are sanitised: anything that could run code or load something external is removed or the file refused. What the server stores and serves is the sanitised file.
- Unknown keys are refused so a typo cannot silently disappear.

The exact rules, thresholds and error codes are in [Brand Themes - Technical Details](brand_themes_tech.md).

## User Flows

### Admin publishes a theme
1. On **Admin → Brand Theme**, a superuser opens **Upload** and drops a zip, a package folder, or `theme.json` with its asset files.
2. If it is invalid, nothing is saved and the dialog stays open with every problem grouped as General, Dark and Light: the dotted path (`modes.dark.text.muted`) and a sentence saying what to change. The admin fixes the package and uploads again.
3. If it is valid, a draft is created and its preview opens: a mock sign-in card and app chrome (sidebar, user bubble, status chips, chart colors) over the theme's background, dark and light side by side, whatever mode the admin's own page is in. Non-blocking warnings (for example what the SVG sanitiser removed) are shown there, once.
4. The admin publishes from the preview. Choosing **Publish** in a draft's menu also opens the preview first, so a theme is never published unseen. Core re-validates it; if the rules have tightened since the upload, the grouped problems appear instead and nothing changes. Otherwise it gets a new revision, the previous theme returns to draft, and a `brand_theme.updated` event is broadcast.
5. The **Published theme** card shows what is live: name, revision, who published it and when.

The drafts list keeps up to 20 themes, newest first (five on the card, the rest under **Show all**). A draft can be deleted; the published one must be unpublished first.

### An agent turns a brandbook into a package (`brand-theme` skill)
Core ships a catalog skill, `brand-theme`, that an agent uses with a company brandbook. A superuser publishes it to the server's skills catalog once (see the technical details); after that any agent it is installed on can produce a package.
1. The agent copies the skill's template (stock Cinna colors that already pass every rule, plus the two stock backgrounds) and reads the role list, one sentence per role.
2. It picks a color for every role in both modes from the brandbook, and exports the logo if there is one.
3. It takes the backgrounds from the brandbook, or generates soft gradient meshes from the theme's own colors. The generator makes the blobs as strong as readability allows and says so; if even a faint mesh is unreadable, it says the text colors, not the image, must change. The same seed always gives the same image, so an admin can ask for "candidate 3 again, a bit softer".
4. It runs the skill's validator, which is the server's own validator copied verbatim, fixes what it names and repeats until the package is valid. A package that passes there passes on upload.
5. It writes a preview page (a mock of the desktop's main screen and the sign-in card in both modes, one row per background candidate, each marked accepted or refused) and a zip holding only what the theme uses, and hands both to the admin, who uploads the zip.

A soft, low-contrast image (a mesh, a blur, a subtle texture) passes the readability rule; a photo or a busy pattern usually does not.

### Admin unpublishes
The superuser chooses **Unpublish** on the Published theme card or the published draft's menu and confirms; the server returns to stock Cinna and a `brand_theme.updated` event (published false) is broadcast. Repeating it is harmless.

### Desktop applies the theme
Discovery shows a `brand_theme` entry, the desktop fetches the theme endpoint, revalidates with the revision on later visits, and downloads the logo and background files through content-addressed URLs. A server with no theme answers "no content" and the desktop shows stock Cinna. In this release the desktop learns about changes by revalidating, not by listening to the event.

## Business Rules

- Only superusers manage themes; the read endpoints are public and rate-limited per caller.
- Drafts are immutable apart from their status: a new look is a new upload.
- A theme version that was handed to a client is never reused: revisions only increase, even if all themes are deleted.
- A published theme cannot be deleted; unpublish it first.
- Users on the same server always share the same theme.

## Integration Points

- [Desktop App Authentication](../desktop_auth/desktop_auth.md): the `/.well-known/cinna-desktop` discovery document gains the `brand_theme` entry when a theme is published.
- [Real-Time Event Bus System](../realtime_events/event_bus_system.md): publish and unpublish broadcast `brand_theme.updated`.
- [Server Configuration](../server_configuration/server_configuration.md): the other admin-managed, server-wide public settings (disclaimer, landing page); a brand theme is stored separately from that singleton.
- Cinna Desktop copies the shared test fixtures that ship with the validator, so its parser and safety checks are tested against what core really produces.

## Not Yet

- Applying the theme inside the rest of core's web app (only the sign-in and consent pages are branded).
- Fonts, radius or density, and app icons are outside the scheme.

## Changelog

- Backend (first phase): schema and validator, storage, admin API, public endpoint, discovery field, shared fixtures.
- Web sign-in pages adopt the published theme.
- The `brand-theme` authoring skill: template, validator copy, mesh background generator, preview and packager, publishable to the skills catalog.
- Admin → Brand Theme page: upload with grouped errors, drafts, two-mode preview, publish, unpublish, delete.
