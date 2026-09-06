# Public Landing Page — Technical Details

## File Locations

### Backend
- `backend/app/models/server_config/server_config.py` — the `landing_markdown` column, `LANDING_MARKDOWN_MAX_LENGTH`, the `reject_raw_html` predicate and its regexes, the `ServerConfigUpdate.landing_markdown` field + `_no_raw_html` validator, and the `LandingPagePublic` projection
- `backend/app/api/routes/server_config.py` — `GET /server-config/landing`, sharing the module's single `_access_policy_limiter`
- `backend/app/services/users/access_policy_service.py` — `AccessPolicyService.signup_refusal_reason`, `AccessPolicy.password_signup_available`, and `to_public` carrying the new field
- `backend/app/utils.py` — `generate_new_account_email` now builds `web_link = f"{settings.FRONTEND_HOST}/start"`
- `backend/app/core/config.py` — `ACCESS_POLICY_RATE_LIMIT_PER_MIN` raised 120 → 240 and re-documented as the module-wide anonymous budget
- `backend/app/models/__init__.py` — re-exports `LandingPagePublic`
- `backend/app/alembic/versions/adffe56ef505_add_landing_markdown_to_server_config.py` — migration (down_revision `89878dff89a6`)

### Frontend
- `frontend/src/routes/start.tsx` — **new.** The public `/start` page; no `beforeLoad`
- `frontend/src/components/Landing/LandingMarkdown.tsx` — **new. Security boundary.** The renderer `/start` and the admin preview use
- `frontend/src/hooks/useLandingPage.ts` — **new.** The `["landingPage"]` query
- `frontend/src/components/Admin/LandingPageCard.tsx` — **new.** The admin editor card
- `frontend/src/components/Common/CopyableValue.tsx` — **new.** Shared label + value + copy-button control
- `frontend/src/components/Desktop/DesktopDownloadSection.tsx` — **new by extraction** from `DesktopLandingPage.tsx`; the download offer itself
- `frontend/src/components/Desktop/DesktopLandingPage.tsx` — reduced to the `/desktop` page shell around that section
- `frontend/src/components/Chat/MarkdownRenderer.tsx` — unchanged behaviour; gained the contract comment recording that it now has **no anonymous consumer**
- `frontend/src/hooks/useAccessPolicy.ts` — gained the shared `googleSignInAvailable(source)` helper
- `frontend/src/routes/login/index.tsx`, `frontend/src/routes/signup.tsx`, `frontend/src/routes/accept-invite/index.tsx` — now read `password_signup_available` / `googleSignInAvailable` instead of recombining facts
- `frontend/src/routes/_layout/admin/server-configuration.tsx` — mounts `LandingPageCard` under `AccessPolicyCard` on the `access` tab
- `frontend/src/routes/_layout/admin/ai-credentials.tsx` — **new**, the renamed AI Credentials admin page
- `frontend/src/routes/_layout/admin/llm-providers.tsx` — reduced to a `beforeLoad` redirect stub
- `frontend/src/components/Sidebar/AdminMenu.tsx` — the menu item now reads "AI Credentials" → `/admin/ai-credentials` (icon unchanged: `Sparkles`)
- `frontend/biome.json` — `noRestrictedImports` on `rehype-raw` (a signpost — see below)
- `frontend/package.json` — `rehype-sanitize` added

### Tests (backend only — see [no frontend runner](#there-is-no-frontend-test-runner))
- `backend/tests/api/server_config/landing_page_test.py` — **new**, 10 tests: public read on a fresh instance, read reflects admin edits and leaks no other field, persistence without a `disclaimer_version` bump, omission-vs-empty-string, the length cap at the boundary, HTML-shaped payloads rejected, HTML-shaped **false positives** accepted, the indented-code-block rejection documented as intentional, a rejected write not partially applying alongside another field, and the shared rate-limit budget
- `backend/tests/api/server_config/access_policy_test.py` — `PUBLIC_POLICY_FIELDS` gained `password_signup_available`; a new matrix test proves the field against the real `POST /users/signup` outcome across all four (`registration_mode`, `password_auth_enabled`) combinations
- `backend/tests/api/auth/invitation_email_web_link_test.py` — **new**, 1 test: the **invitation** email's `web_link` stays the bare frontend host and `/start` appears nowhere in the rendered mail
- `backend/tests/api/auth/test_new_account_email.py` — the three existing tests updated for the `/start` suffix
- `backend/tests/utils/server_config.py` — `LANDING_URL`, `get_landing_page(client)`

## Database Model

### `ServerConfig.landing_markdown`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `landing_markdown` | `str` (`Text`) | `""` | Admin-authored welcome copy rendered as Markdown on `/start`. Empty means the page omits the block |

`NOT NULL DEFAULT ''` rather than nullable: the singleton row already exists on every deployed instance, so the server default backfills it and every reader sees `""` for "no welcome written" instead of having to decide what `NULL` means. No index — nothing queries by it; it is only ever read as part of the singleton row.

It does **not** participate in the `disclaimer_version` bump (`ServerConfigService.update` bumps on an explicit two-field check).

### Migration `adffe56ef505`

`down_revision = '89878dff89a6'`. One `op.add_column`. Autogenerate additionally proposed three `alter_column` statements on `cli_device_login_request` stripping `timezone=True`; those are **pre-existing model/DB drift where the model is wrong** and were deliberately not carried into this migration. See the standing false-positive section in [Backend Development](../../development/backend/backend_development_llm.md).

### `ServerConfigUpdate.landing_markdown`

```python
landing_markdown: str | None = Field(
    default=None, max_length=LANDING_MARKDOWN_MAX_LENGTH
)

@field_validator("landing_markdown")
@classmethod
def _no_raw_html(cls, value: str | None) -> str | None:
    if value:
        reject_raw_html(value)
    return value
```

`None` = not being changed; `""` = clear. Neither reaches the HTML check (`if value:`), so the validator never re-interprets the two.

**Both rules live on this class and nowhere else, so the class is their enforcement boundary** — and that holds only because `PUT /admin/server-config` is the sole writer today. A future non-API writer (a service assigning `config.landing_markdown`, a data migration, a seed) would bypass both and put uncapped, unvalidated content into a column `GET /server-config/landing` serves anonymously. Any such writer must route through this model, or call `reject_raw_html` and check the length itself.

### `LandingPagePublic`

| Field | Type | Source |
|-------|------|--------|
| `landing_markdown` | `str` | column, verbatim |

Its own projection and its own endpoint because it is **content**, not policy. `AccessPolicyPublic` is fetched by every login and signup page load under one shared cache key, and none of them render this.

### `AccessPolicyPublic.password_signup_available`

| Field | Type | Source |
|-------|------|--------|
| `password_signup_available` | `bool` | `AccessPolicy.password_signup_available` → `AccessPolicyService.signup_refusal_reason(policy) is None` |

Derived from two fields already projected (`registration_open`, `password_auth_enabled`), so it discloses nothing new. `landing_markdown` is deliberately **not** on this projection.

## The raw-HTML write guard

`reject_raw_html(markdown)` raises `ValueError` — surfaced by Pydantic as a `422` whose `detail[0].msg` the admin card shows verbatim — when HTML-shaped content survives code-region stripping.

**Order of operations.** `_strip_code_regions` blanks fenced code blocks and single-line inline code spans first; what survives is the text a Markdown renderer would emit as prose, which is the only place raw HTML could ever become an element. Three patterns then run over that prose:

| Regex | Refusal reason |
|---|---|
| `_HTML_TAG_RE` = `<[!?/]` or `<[a-zA-Z][a-zA-Z0-9-]*[\s/>]` | "an HTML tag" |
| `_EVENT_HANDLER_RE` = quoted `on…=` attribute values | "an HTML event-handler attribute" |
| `_DANGEROUS_SCHEME_RE` = `javascript:`/`vbscript:` followed by non-space, or `data:text/html` | "a script-bearing URL scheme" |

**Deliberately conservative about false positives** — a rule that refuses an admin's code sample is a bug, not caution. `<` must be followed immediately by a letter or `/!?` to read as a tag, so prose comparisons (`a < b`) pass.

**Fence recognition follows CommonMark, tightly.** Recognising a fence *loosely* is the failure that matters: everything from a bogus opener to the end of the document would be dropped unchecked. So `_FENCE_RE` allows at most three spaces of indent, and a backtick fence's info string may not itself contain a backtick. A closing fence must be the same character, at least as long, and carry no info string. Inline code spans are matched **single-line only**: a run that crosses lines is exactly how a stray backtick either side of a `<script>` would blank it out of the check while a renderer still emitted it as markup.

**Known limitation, tested as intentional** (`test_indented_code_block_is_rejected_by_design`): four-space-indented code blocks are **not** excluded, so one containing HTML is refused. Telling an indented block apart from a list item's continuation line needs a real block parser, and over-excluding hides markup. The refusal message points the admin at a fenced block.

**Failure is whole-request.** Because the check runs as a field validator, a rejected `landing_markdown` fails the entire `PUT` — a valid sibling field on the same payload is not partially applied.

## Service Layer

### `AccessPolicyService.signup_refusal_reason(policy) -> str | None`

The **sole** encoding of the door-level SIGNUP gates — the ones that depend on the instance alone and not on who is knocking:

```python
if not policy.registration_open:
    return REASON_REGISTRATION_CLOSED
if not policy.password_auth_enabled:
    return REASON_PASSWORD_AUTH_DISABLED
return None
```

Two readers consume it and neither re-encodes it:

- `can_register`'s `AccountOrigin.SIGNUP` branch, which then adds the per-address pattern check
- `AccessPolicy.password_signup_available`, which is this returning `None`

A gate added here reaches both at once. That is the point: a third gate added to `can_register` alone would leave a Create-account button advertising a door the API refuses — which is exactly the bug this replaced, in the other direction (`/login` checked `registration_open` alone and offered a Sign up link that `/signup`, which required both, then refused to render a form for).

`can_register`'s non-SIGNUP branch keeps its own `registration_open` + `google_auto_register` checks; only the SIGNUP branch delegates.

## API Endpoints

### `GET /api/v1/server-config/landing`
- **Auth**: none
- **Response**: `LandingPagePublic` — `{"landing_markdown": "<raw markdown>"}`
- **Rate limit**: the **same** `_access_policy_limiter` object as the access-policy read, declared as a `Depends` before `SessionDep` resolves. A second `RateLimiter` would hand one anonymous caller two budgets
- **Query key (frontend)**: `["landingPage"]`, `staleTime` 60s, `retry: false`

### `PUT /api/v1/admin/server-config`
- Unchanged shape; `landing_markdown` joins the payload with the semantics above

### Rate limiting, module-wide

`ACCESS_POLICY_RATE_LIMIT_PER_MIN` is now documented as the budget for **every anonymous endpoint in `api/routes/server_config.py`**, not for one route. Consequences worth restating when adding a public read there:

- It is spent in **requests, not page views**: a `/start` view costs two, a `/login` view one
- `anonymous_caller_key` keys on source IP, so a NAT'd office shares one bucket. `240` is roughly 120 first-time visitors per minute behind one address
- Exhaustion is quiet by design — the pages degrade rather than error — so err generous

## Frontend

### `components/Landing/LandingMarkdown.tsx` — the security boundary

```tsx
<ReactMarkdown
  remarkPlugins={[remarkGfm]}
  rehypePlugins={[rehypeSanitize]}   // rehype-raw must NEVER be added here
  components={components}
>
```

- **Why a separate file rather than a prop on the chat renderer.** The property being protected is *structural*: this plugin list is reachable only from this file. A `rehype-raw` added to `Chat/MarkdownRenderer` for an unrelated chat feature cannot reach `/start`. Passing options into a shared renderer would put both surfaces back on one list and reinstate the exact failure this rules out — stored XSS served to every anonymous visitor, in a diff that never touches `/start`
- **The component overrides are duplicated from the chat renderer on purpose**, not imported. ~20 lines of Tailwind, and a boundary file should be auditable end to end without following imports. Keeping them local also means no shared module exists through which a `components` override (which could reach for `dangerouslySetInnerHTML`) could arrive here from a change aimed at chat
- **Two independent reasons raw HTML is inert**, both load-bearing: `react-markdown` does not parse HTML source without `rehype-raw` (absent), and `rehype-sanitize` runs on every render with the default GitHub schema, dropping `raw` nodes. The second holds in **either plugin order** — before `rehype-raw` it strips the raw nodes before parsing, after it strips the elements they parsed into
- Sanitising is byte-identical to not sanitising for ordinary admin prose. It is deliberately **not** retrofitted onto the chat renderer, whose richer output (syntax-highlight classNames and similar) is a much larger regression surface and a separate decision

**Scope of the guarantee, stated precisely.** `/start` cannot be compromised **by a diff that does not touch `/start`**. Repointing `routes/start.tsx` at the chat renderer, or adding `rehype-raw` to this file's list, still compromises it — but each is a change to `/start` itself and shows up in review as one.

**The `biome.json` `noRestrictedImports` rule on `rehype-raw` is a signpost, not enforcement.** There is no CI in this repository and nothing invokes lint; it fires only for whoever runs `npm run lint` or has the editor integration. Do not count it as a guard. The rule's own message says so.

### `routes/start.tsx`

- Public, **no `beforeLoad`** — same reasoning as `routes/desktop.tsx`
- `projectName = policy?.project_name ?? APP_NAME` — the same reconciliation `accept-invite` makes, so a slow or failed policy read shows the build's name rather than nothing
- `signedIn = isLoggedIn()` — a pure `localStorage` read, never a validation call
- Policy-dependent elements render on **positive answers only**: `passwordSignupAvailable = policy?.password_signup_available === true`, `desktopOffered = policy?.desktop_enabled === true`
- `googleOnly = policy?.password_auth_enabled === false && googleSignInAvailable(policy) === true`. It is keyed on `password_auth_enabled`, **not** on `password_signup_available === false`: the latter is false in two different worlds (password sign-in off, and registration merely closed), and on an invite-only instance that still accepts passwords it would tell an anonymous visitor something untrue about how they sign in
- The welcome block is guarded on `welcome.trim()` — `LandingMarkdown` has no empty-input guard, so an unguarded call ships an empty bordered card to every visitor of an instance whose admin never wrote a welcome
- Links to `/agent-start?format=html` when the Local Agent Kit is enabled, mirroring the login page's link

### `hooks/useLandingPage.ts`

`useQuery<LandingPagePublic>` on `["landingPage"]`, `staleTime: 60_000`, `retry: false` (same anonymous bucket as the policy read). Callers must render sensibly when `data` is `undefined`; on `/start` that is simply nothing — an absent welcome and an unread one look identical, and both are fine.

### `hooks/useAccessPolicy.ts` — `googleSignInAvailable(source)`

```ts
export function googleSignInAvailable(
  source: { google_auth_enabled?: boolean | null } | undefined,
): boolean | undefined {
  if (!import.meta.env.VITE_GOOGLE_CLIENT_ID) return false
  return source?.google_auth_enabled ?? undefined
}
```

- Two independently configured facts must both hold: the backend's client id/secret (reported by the projection) and this build's own `VITE_GOOGLE_CLIENT_ID`, without which `GoogleLoginButton` renders nothing at all
- **`undefined` in, `undefined` out, deliberately** — the callers do not share a degradation. `/login` and `/signup` are the front door and degrade permissively (`?? true`); `/start` is a hub and says nothing until it knows (`=== true`)
- **Structurally typed** rather than taking `AccessPolicyPublic`, because the same fact reaches `/accept-invite` on a different projection (`InvitationLookupPublic.google_auth_enabled`). Narrowing it to one response model is what left that page with a fourth hand-rolled copy

### `components/Admin/LandingPageCard.tsx`

- Reads the server's own cap off the generated schema rather than retyping it: `ServerConfigUpdateSchema.properties.landing_markdown.anyOf[0].maxLength`. The schema regenerates with the client, so the card follows the backend automatically and a change to the field's shape breaks the build instead of going unnoticed
- `landingPageUrl()` = `` `${window.location.origin}/start` `` — the **page** origin, not `resolveServerOrigin()` (which resolves the *API* origin and differs behind a reverse proxy). Same choice as `localAgentKitStartUrl`
- Shares `["serverConfig"]` and `PUT /admin/server-config` with the sibling cards; on success it publishes the returned row before invalidating (so the card does not visibly snap back under a success toast) and additionally invalidates **`["landingPage"]`** — without that, `/start` keeps serving the old copy in the admin's own session for 60s with no error and no way to tell
- `onError: handleError.bind(showErrorToast)` — the shared extractor, which unwraps a 422's `detail[0].msg`, so the server's spelled-out refusal (over the cap, or HTML-shaped) reaches the admin instead of a generic "failed"
- **`loaded = config !== undefined`**, not `!isLoading && !isError`. With React Query's default `networkMode: "online"`, a query that cannot start because the browser is offline sits at `status: "pending"`, `fetchStatus: "paused"` — `isLoading` false, `isError` false, `data` undefined — and that spelling would open the form, seed `""` and wipe live copy on reconnect. The same swap fixes the mirror case: a failed *background* refetch reports `status: "error"` while the last good value is still on screen, which must not deaden the button
- The over-limit state is **measured, never enforced by truncation**: no `maxLength` on the textarea (which would silently swallow the tail of a pasted page); instead `aria-invalid`, an `aria-describedby` count message, and a disabled Save
- The preview renders through `LandingMarkdown`, deliberately the same pipeline as the published page
- **Draft seeding happens in `openEditor`, at the moment the dialog opens** — not in an effect keyed on the query result, which would re-run on every background refetch (window focus, any sibling card's save) and replace a half-typed paragraph with the server's copy mid-sentence. `Dialog.onOpenChange` routes through `openEditor` too, so a future `DialogTrigger` cannot bypass the seeding or the `loaded` guard
- **The trade is last-write-wins** (see the business doc's rule). Recorded as a contained follow-up rather than built: the dialog *could* detect that the server value changed since seeding and offer keep/reload — React Query already holds both values. Rejected as disproportionate for this card, and because a false positive fires on the admin's own save from a sibling card on the same page

### `components/Common/CopyableValue.tsx`

Label + monospace value + copy button, with the tick reverting on a timer cleared on unmount and a failure toast — `navigator.clipboard` is undefined outside a secure context and `writeText` rejects when permission is denied, so the `catch` is not optional. Extracted because it now has two callers (`LandingPageCard`, `ServerChannels/ChannelSetupInstructionsPanel`).

One deliberate exception: `Desktop/DesktopDownloadSection` keeps its own inline control — a different affordance (full-width bordered address block, ghost icon button, *success* toast) on a page an anonymous visitor reads. Any **new** label-plus-value control should use `CopyableValue`.

### `components/Desktop/DesktopDownloadSection.tsx`

The `/desktop` page's `CardContent` body, extracted verbatim: OS/arch detection, the download button, the Linux-arm64 branch, the `cinna://connect` deep link and the copyable origin. `DesktopLandingPage` is now the page shell (full-viewport centring, its own `Monitor` header card) around this section; `/start` renders the same section inside one card among several. `/desktop`'s behaviour is unchanged.

### `components/Chat/MarkdownRenderer.tsx`

Behaviour unchanged; it gained a contract comment recording that it now has **no anonymous consumer** — that coupling was removed structurally when `/start` got its own renderer. The warning it carries still stands on chat's own account: every caller is behind auth, but "behind auth" is not "trusted content" (agent output, tool-call transcripts, other people's messages). If `rehype-raw` is ever genuinely wanted there, pair it with `rehype-sanitize` in the same commit — and do not "simplify" `/start` back onto this renderer to match.

### Admin route rename

| Was | Is |
|---|---|
| `/admin/llm-providers` (UI route) | `/admin/ai-credentials` |
| "LLM Providers" (menu label, page title) | "AI Credentials" |
| `frontend/src/routes/_layout/admin/llm-providers.tsx` (the page) | a `beforeLoad` redirect stub → `/admin/ai-credentials` |

**Unchanged, on purpose:**

- the backend prefix `/api/v1/admin/llm-providers` and the OpenAPI tag `admin-llm-providers` (`backend/app/api/routes/admin_llm_providers.py`)
- the generated `AdminLlmProvidersService`
- `MANAGED_CREDENTIALS_QUERY_PREFIX = ["admin", "llm-providers"]` — a **cache key**, not a route path. Three surfaces (the page, the auto-provision matrix, the invite wizard) depend on producing the *same* string; renaming it splits the cache silently, with no error and two lists that stop agreeing
- the component directory `frontend/src/components/Admin/LlmProviders/`

The redirect stub carries **no auth guards of its own** — the target route runs them, and duplicating them would be a second place to keep them in step.

## Operational note: `rehype-sanitize` is a new npm dependency

`frontend/package.json` +1 (`rehype-sanitize`), `package-lock.json` +49 lines, **two** new packages (`rehype-sanitize` and its `hast-util-sanitize` dependency).

The dev override mounts a **named volume** over the frontend's modules:

```yaml
# docker-compose.override.yml
volumes:
  - ./frontend:/app
  - frontend_node_modules:/app/node_modules
```

Docker seeds a named volume from the image **only when it is first created**. So on a dev environment that already has `frontend_node_modules`, `docker compose build frontend` alone will **not** install the new package — the image gets it, the volume keeps the old contents, and the container reads the volume.

Fix it with either:

```bash
docker compose exec frontend npm install
# or, to re-seed from the image:
docker compose down && docker volume rm <project>_frontend_node_modules && docker compose up -d
```

The failure is **loud** — Vite fails to resolve `rehype-sanitize` — not silent, but it looks like a broken checkout to whoever hits it first.

## There is no frontend test runner

No vitest, no jest. `@playwright/test` is a dev dependency, but there is **no Playwright config, no spec files and nothing invoking it**. Nothing in this repository runs frontend tests, and there is no CI.

The consequences that matter for this feature:

- The render-boundary guarantee rests on **structural isolation** (the plugin list is reachable only from `LandingMarkdown.tsx`) plus the **active sanitiser** — neither of which needs a runner
- The `biome` restricted-import rule is a signpost, for the same reason
- The only automated coverage is the backend suite, which sees the write-path validator and the projections, not the render path

Verification for a change on this surface is therefore: read `LandingMarkdown.tsx`, `npx tsc --noEmit` scoped to the touched files (per `CLAUDE.md`), and the backend tests listed above.

## Configuration

| Setting | Location | Purpose |
|---------|----------|---------|
| `ACCESS_POLICY_RATE_LIMIT_PER_MIN` | `config.py` (default **`240`**, was 120) | Module-wide per-caller budget for **every** anonymous endpoint in `api/routes/server_config.py` — the access-policy read and the landing read share one `RateLimiter` |
| `FRONTEND_HOST` | `.env` / `config.py` | `generate_new_account_email` builds `web_link = f"{FRONTEND_HOST}/start"` |
| `DESKTOP_AUTH_ENABLED` | `config.py` | Surfaced as `desktop_enabled`; gates the `/start` download card's **advertisement** only |
| `PROJECT_NAME` | `config.py` | Surfaced as `project_name`, the `/start` heading (falls back to the build's `APP_NAME`) |

No new setting was added by this phase.

## Deferred, with reasons

Carried over from phase 3 and deliberately **not** built here:

- **"Invited (7d)" countdown in the users-table Status column.** Deferred: it would reopen `UserPublic`, a projection with a history of a new field breaking many producers. The countdown stays in the row's action menu, which fetches that one invitation while the menu is open
- **Pre-disabling the resend menu item during its cooldown.** Deferred — **and the obvious implementation is wrong.** Do *not* put `resend_available_at` on `UserPublic`: it belongs on `UserInvitationPublic`, which `InvitationActions` already fetches lazily per open menu. That shape needs no batching and creates no N+1
- **Browser pre-validation of `allowed_email_patterns` in the invite wizard.** Deferred **and the framing rejected**: a dedicated validation endpoint would be a second, narrower surface for the same policy. If pre-flight is ever wanted, the only correct shape is `POST /admin/server-config/validate` running the same `validate_update` on the merged candidate. Never match the policy client-side

Contained follow-up recorded above: conflict detection in the landing editor's dialog (keep/reload on a changed server value).

---

*Last updated: 2026-09-06*
