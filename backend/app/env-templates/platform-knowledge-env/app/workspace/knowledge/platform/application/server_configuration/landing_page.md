# Public Landing Page (`/start`)

## Purpose

Give an instance **one stable address anyone can open without an account** — pasted into an internal wiki, printed in an onboarding deck, or sent as the "just use the browser" link in the new-account email. `/start` states what this deployment actually offers (log in, create an account if self-service registration is open, install Cinna Desktop) and shows an **admin-authored welcome message** written in Markdown from the Server Configuration page.

It is the last of the four zero-touch-onboarding phases' front-door surfaces: [Access Policy](access_policy.md) decides who may get in, [Invitations](../auth/auth.md#invitations) let an admin hand out accounts, and `/start` is where someone who was told nothing more than "go here" arrives.

## Core Concepts

- **`/start`** — a public SPA route with **no `beforeLoad` guard**, exactly like `/desktop`. The visitor has not signed in anywhere yet, so any session check that redirects would put a login wall in front of the page whose entire job is to be reachable without one
- **Welcome message (`ServerConfig.landing_markdown`)** — superuser-authored Markdown stored on the singleton `ServerConfig` row. Empty is normal and means the page simply omits the block
- **Landing projection** — `GET /api/v1/server-config/landing`, public, unauthenticated, rate-limited. **Deliberately a separate endpoint from the access-policy projection**, not a field on it (see [Why the landing copy is not on `AccessPolicyPublic`](#why-the-landing-copy-is-not-on-accesspolicypublic))
- **`password_signup_available`** — a new field on the public access-policy projection: the server's single **answer** to "may someone self-register with a password here?", rather than the two ingredients (`registration_open`, `password_auth_enabled`) each page used to recombine for itself
- **Render boundary (`LandingMarkdown`)** — `/start` renders the welcome copy through its own renderer with its own plugin list, separate from the chat renderer. This is the security boundary of the feature (see [The security boundary](#the-security-boundary))
- **Advertisement, not availability** — `desktop_enabled` hides or shows the desktop card on `/start`. `/desktop` keeps rendering and the download resolver keeps resolving either way, so links in already-sent emails never break

## Admin User Stories / Flows

### Writing the welcome message

1. Superuser opens **Admin → Server Configuration → Access** (`/admin/server-configuration#access`). Below the Access Policy card sits **Public landing page**
2. The card shows the landing page URL — `{page origin}/start` — with a copy button and the caption "Paste this into your internal wiki"
3. **Edit** opens a two-column dialog: a Markdown textarea on the left, a live preview on the right. The preview uses the **same renderer `/start` uses**, so what is previewed and what is published cannot drift
4. Save writes only `landing_markdown` through the shared `PUT /admin/server-config` endpoint. The card refreshes both the admin config and the public `["landingPage"]` cache, so the admin's own `/start` tab is not left showing the old copy
5. Saving an empty textarea **clears** the message; the page then omits the block entirely

### Sharing the address

The URL on the card is built from the **page origin** (`window.location.origin`), not from the API origin — behind a reverse proxy those differ, and the address a person pastes into a wiki has to be the one a browser opens. This is the same choice `localAgentKitStartUrl` makes for `/agent-start`.

### Renaming the AI credentials admin page

Not part of the landing page, but shipped in the same phase: the admin surface formerly at **Admin → LLM Providers** is now **Admin → AI Credentials** at `/admin/ai-credentials`. This is a **UI rename only** — the backend prefix `/api/v1/admin/llm-providers`, its OpenAPI tag and the generated `AdminLlmProvidersService` are unchanged. The old route survives as a redirect stub so bookmarks and older documentation keep working. See [Admin-Provisioned AI Credentials](../ai_credentials/admin_ai_credential_provisioning.md).

## End-User Stories / Flows

### Arriving at `/start`

1. The page reads two public projections: the access policy and the landing copy
2. The welcome message renders in a card at the top, when one is set and non-blank
3. **Log in** is always offered. It depends on no policy fact at all, so no failed read, rate limit or misconfiguration can hide the way in
4. **Create account** appears only when `password_signup_available` is true
5. When the instance signs people in with Google only, a one-line note says so and points at the log-in page
6. **Get Cinna Desktop** appears when `desktop_enabled` is true, and embeds the same OS/arch detection, download button and `cinna://connect` deep link that `/desktop` shows
7. A visitor who is already signed in on that browser sees a "You're signed in on this browser" strip with a shortcut to the dashboard. This is a pure `localStorage` read — never a token-validation call — so a stale token shows the banner and the ordinary `_layout` guard sorts it out on arrival

### Positive answers only

Every policy-dependent element on `/start` renders on a **positive** answer (`=== true`). Loading, a rate-limit refusal, a network failure and a policy that says "no" all render the same thing: nothing. `/start` is a hub, not the only way in, so silence is available to it — unlike `/login` and `/signup`, which degrade *permissively* (`?? true`) because a wrong guess there costs one rejected request rather than an unreachable server.

## Business Rules

- **`landing_markdown` omission semantics.** On `PUT /admin/server-config`, `null` / an absent key means **not being changed**; `""` means **clear the welcome copy**. Both must stay expressible, which is why the length cap and the HTML refusal live on the field rather than in a write-site check that would have to re-decide which of the two an absent key was
- **Landing edits never bump `disclaimer_version`.** The welcome message is never acknowledged, so writing it must not force every user on the instance to re-accept an unchanged disclaimer
- **16 KiB cap.** The first length cap on any Markdown column on `ServerConfig`, and it exists because this is the first one served to **anonymous** callers — an uncapped blob behind nothing but a rate limit is an amplification primitive. The admin card measures the draft and refuses the save with the count on screen; it deliberately does **not** put `maxLength` on the textarea, which would silently swallow the tail of a pasted wiki page
- **Raw HTML is refused on write.** A `@field_validator` on `ServerConfigUpdate.landing_markdown` rejects HTML-shaped input — tags, event-handler attributes, and script-bearing URL schemes — and fails the **whole request**, so a rejected write never partially applies alongside an otherwise-valid field on the same payload
- **Known, deliberate limitation of that validator: four-space-indented code blocks containing HTML are rejected.** Fenced blocks and inline code spans are excluded from the check; indented blocks are not, because telling one apart from a list item's continuation line needs a real block parser, and over-excluding is the direction that *hides* markup. The refusal message points the admin at a fenced block, which does work
- **Concurrent edits are last-write-wins, deliberately.** The dialog seeds its draft once, when it opens, so a background refetch cannot overwrite a half-typed paragraph. The mirror case — another admin saving a different welcome message while this dialog is open — is accepted: `ServerConfigUpdate` is a field-wise partial merge with no version or ETag, so a stale write is not detectable client-side even in principle without first adding a server-side concurrency token. Only this one field is sent, so a concurrent change to any *other* server setting is untouched, and the two outcomes are not symmetric — a discarded remote edit is one text box away from being retyped, while the draft being typed right now is gone for good
- **`desktop_enabled` gates advertisement only.** `/start` hides its download card when `DESKTOP_AUTH_ENABLED` is off; `/desktop` still renders and `GET /api/v1/desktop/download` stays ungated. Turning the setting off stops *promoting* the desktop client; it does not stop serving it
- **`password_signup_available` is "the door exists", never "you may walk through it".** It is deliberately not `can_register`, which also consults the email pattern list and therefore needs an address. A projection with no viewer may only say what the instance offers; whether a given address is allowed stays a server-side check at signup
- **A `/start` view costs two requests from the shared anonymous budget.** See [The rate-limit consequence](#the-rate-limit-consequence-of-a-separate-endpoint)
- **The invitation email's browser link was deliberately *not* moved to `/start`.** Only the **new-account** email's secondary "Prefer the browser?" link now points at `{FRONTEND_HOST}/start`. The invitation mail's sentence reads "Or open it in the browser: {url}" *about the invitation* — and `/start` carries no token and cannot open one. Repointing it would turn a true sentence into a false one. A regression test pins this

## Why the landing copy is not on `AccessPolicyPublic`

`AccessPolicyPublic` is a handful of small scalars, cached under a single `["accessPolicy"]` query key that **`/login` and `/signup` both read on every page load**. The welcome message is an admin-pasted document up to 16 KiB that only `/start` renders. Folding it into the policy projection would put a landing page on the wire for every login-page view, on the one endpoint an unauthenticated caller can reach.

So the split is: **policy is policy, content is content**, served by `GET /server-config/landing` on its own. The cost of that decision is one extra request, described next.

### The rate-limit consequence of a separate endpoint

Both public reads in `api/routes/server_config.py` share **one** `RateLimiter` object on purpose — a per-route limiter would hand one anonymous caller a fresh budget for every new public read added to that module. The consequence is that the budget is spent in **requests, not page views**, and the exchange rate is not 1:1: a single `/start` view costs **two** (policy + landing), while a `/login` view costs one.

`anonymous_caller_key` keys on source IP, so a NAT'd office shares one bucket. `ACCESS_POLICY_RATE_LIMIT_PER_MIN` was therefore raised **120 → 240** when the landing read joined the limiter, which keeps roughly the same effective capacity (~120 first-time visitors per minute behind one address).

**Exhaustion is silent by design.** `/start` renders only positive answers, so a 429 makes the page look like an instance that offers no signup, no Google and no desktop — a correct-looking page stating something false. That is the accepted trade (the alternative is an error page on the front door), and it is the reason the budget is set generously rather than tightly.

## The security boundary

> The security boundary for public landing copy is the render path in `frontend/src/components/Landing/LandingMarkdown.tsx`: `/start` renders through a dedicated renderer with its own explicit plugin list that omits `rehype-raw` and applies `rehype-sanitize` on every render, so raw HTML in `ServerConfig.landing_markdown` is inert as an **asserted property of that file** rather than as a side effect of the shared chat renderer's configuration.

The asymmetry this exists for: the **author is trusted** (a superuser) but the **audience is the whole internet**. Before this phase, `/start` would have rendered through `Chat/MarkdownRenderer`, where raw HTML is inert only because `react-markdown` does not parse it without `rehype-raw`. That is a *default*, not a configuration — so adding that plugin for an unrelated chat feature (agent-authored messages, tool-call transcripts) would have turned admin-authored landing copy into stored XSS served to every anonymous visitor, arriving in a diff that never touched `/start`.

Three qualifications, each correcting a way this is otherwise read wrong:

1. **The server-side validator is not merely redundant defence in depth — it covers a different consumer set.** `GET /server-config/landing` returns **raw Markdown over JSON**. Any non-browser consumer — the desktop app, a CLI, an email that embeds the copy, anything that renders the string itself — gets it without the frontend's sanitiser. The render path is the boundary **for `/start`**; `reject_raw_html` on `ServerConfigUpdate` is the only guard those other consumers have. It guards something real
2. **The isolation property, stated precisely:** `/start` cannot be compromised **by a diff that does not touch `/start`**. Someone can still repoint `routes/start.tsx` at the chat renderer, or add `rehype-raw` to `LandingMarkdown`'s own list — but either is a change to `/start` itself, visible in review as such. This is not an unqualified "cannot happen"
3. **The biome `noRestrictedImports` rule for `rehype-raw` is a signpost, not enforcement.** This repository has **no CI and nothing invokes lint**, so the rule fires only for someone who happens to run `npm run lint` or has the editor integration on. Do not count it as a guard

Two independent reasons make raw HTML inert on the `/start` path, and both are load-bearing:

- `react-markdown` does not parse HTML source into elements at all without `rehype-raw`. It is absent, and must stay absent
- `rehype-sanitize` runs on **every** render with the default (GitHub) schema, dropping the `raw` nodes that carry HTML source. This is the *active* guarantee: it holds even if the first is violated, in either plugin order

Sanitising costs nothing on this surface: against ordinary admin prose — headings, lists, links, emphasis, blockquotes, GFM tables, task lists, images, fenced code with `language-*` classes — the sanitised output is byte-identical to the unsanitised output. It is deliberately **not** retrofitted onto the chat renderer, whose richer output is a far larger regression surface and a separate decision.

### Nothing runs frontend tests in this repository

There is no vitest and no jest. `@playwright/test` is a dev dependency, but there is **no Playwright config, no spec files, and nothing that invokes it**. The render-boundary guarantee therefore rests on two things that need no test runner — **structural isolation** (the plugin list is reachable only from `LandingMarkdown.tsx`) and an **active sanitiser** — not on a test. The backend suite covers the write-path validator, which is the half a Python test can see.

## Architecture Overview

```
Admin → Server Configuration → Access → "Public landing page" card
        │  PUT /admin/server-config { landing_markdown }
        │     └─ ServerConfigUpdate: 16 KiB cap + reject_raw_html (whole request fails)
        ▼
   server_config.landing_markdown  (Text NOT NULL DEFAULT '')
        │
        ├──► GET /server-config/landing → LandingPagePublic     ┐ shared anonymous
        └──► GET /server-config/access-policy → AccessPolicyPublic ┘ rate-limit bucket
                                              (+ password_signup_available)
                     │
                     ▼
              SPA /start  (public, no beforeLoad)
                     ├─ LandingMarkdown  ← security boundary (no rehype-raw, + rehype-sanitize)
                     ├─ Log in            (unconditional)
                     ├─ Create account    (password_signup_available === true)
                     ├─ Google-only note  (password_auth_enabled === false && Google usable)
                     └─ DesktopDownloadSection (desktop_enabled === true)
                            └─ shared verbatim with /desktop
```

## Integration Points

- **[Access Policy](access_policy.md)** — owns the public projection `/start` reads, and gained `password_signup_available` in this phase. Both cards live on the same **Access** tab, share the `["serverConfig"]` query key and the single `PUT /admin/server-config` endpoint
- **[Disclaimer](disclaimer.md)** — the other Markdown column on the same singleton row. Landing edits never touch `disclaimer_version`, and disclaimer edits never touch the landing copy
- **[Desktop One-Click Onboarding](../desktop_onboarding/desktop_onboarding.md)** — `/start` embeds `DesktopDownloadSection`, extracted from the `/desktop` page so both surfaces show one implementation of the download offer. The new-account email's secondary browser link now points at `/start`
- **[Authentication](../auth/auth.md)** — `/login` and `/signup` render from the same projection, and both now read `password_signup_available` instead of recombining two facts. The **invitation** email's browser link deliberately stays on the bare host
- **[Local Agent Kit](../local_agent_kit/local_agent_kit.md)** — the other unauthenticated onboarding surface and the source of the shared anonymous rate-limiter helper. `/start` links to `/agent-start?format=html` when the kit is enabled, mirroring the login page's link
- **[Admin-Provisioned AI Credentials](../ai_credentials/admin_ai_credential_provisioning.md)** — the admin page renamed to **AI Credentials** in the same phase (UI only)

---

*Last updated: 2026-09-06*
