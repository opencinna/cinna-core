# Brand Themes - Technical Details

Business rules: [Brand Themes](brand_themes.md).

## Code Map

| Piece | Path |
|-------|------|
| Contract: roles, limits | `backend/app/services/brand_theme/schema.py` |
| Validator | `backend/app/services/brand_theme/validator.py` |
| Color math (hex parsing, contrast, OKLab, OKLCH) | `backend/app/services/brand_theme/colors.py` |
| SVG sanitiser | `backend/app/services/brand_theme/svg_sanitizer.py` |
| Image sniffing, raster facts, readability sample | `backend/app/services/brand_theme/images.py` |
| Package reader (zip, loose files, folder) | `backend/app/services/brand_theme/package.py` |
| Served projection | `backend/app/services/brand_theme/served.py` |
| Storage and publish logic, event emission | `backend/app/services/brand_theme/brand_theme_service.py` |
| Models and API shapes | `backend/app/models/brand_theme/brand_theme.py` |
| Routes | `backend/app/api/routes/brand_theme.py` |
| Shared fixtures | `backend/app/services/brand_theme/fixtures/` |
| Fixture test | `backend/tests/unit/test_brand_theme_fixtures.py` |
| Authoring skill source (`brand-theme`) | `backend/app/services/brand_theme/skill/brand-theme/` |
| Skill archive builder | `backend/app/scripts/build_brand_theme_skill.py` (`make brand-theme-skill`) |
| Skill test (copy sync, scripts, publish gate) | `backend/tests/unit/test_brand_theme_skill.py` |
| Web: parser, role → token map, CSS builder | `frontend/src/components/BrandTheme/brandTheme.ts` |
| Web: scope wrapper, logo | `frontend/src/components/BrandTheme/AuthBrandScope.tsx`, `BrandLogo.tsx` |
| Web: query hook | `frontend/src/hooks/useBrandTheme.ts` |
| Web: skin hand-back inside the scope | `frontend/src/styles/brand-scope.css` |
| Web: hosts | `frontend/src/components/Common/AuthLayout.tsx`; `frontend/src/components/Auth/ConsentShell.tsx`, used by `NativeAuthConsentPage.tsx` (`branded` prop, set by the desktop and mobile consent routes) and `DeviceLoginConsentPage.tsx` (always branded) |
| Web tests | `frontend/tests/brandTheme.test.mjs` (`npm run test:brand-theme:ui`) |
| Admin page route | `frontend/src/routes/_layout/admin/brand-theme.tsx` (menu entry in `Sidebar/AdminMenu.tsx`) |
| Admin page components | `frontend/src/components/Admin/BrandTheme/` (page body, published card, drafts card/row/sheet, upload dialog, preview dialog, grouped issue list, unpublish dialog, queries) |
| Admin preview mock | `frontend/src/components/BrandTheme/BrandThemePreview.tsx` |
| Admin tests | `frontend/tests/brandThemeAdmin.test.mjs` (`npm run test:brand-theme:admin:ui`) |

`schema.py`, `validator.py`, `colors.py`, `images.py`, `package.py`, `served.py` and `svg_sanitizer.py` have no FastAPI or database imports, so they can be copied verbatim into an offline authoring tool; the `brand-theme` skill does exactly that (see [Authoring Skill](#authoring-skill-brand-theme)).

## The Contract: `cinna.brand-theme/1`

`schema.py` is the **only** place the 35 required color roles are defined (`ROLES`, with a one-sentence meaning each in `ROLE_MEANINGS`). Every consumer takes the names from there; this document does not repeat them. A role added in a later `1.x` revision must be optional and go in a separate tuple, never into `ROLES`.

`schema.py` also holds the package shape (`TOP_LEVEL_KEYS`: `schema`, `name`, `modes`, `logo`, `background`, `copy`; `copy.newChatHeading`), the text limits, the asset size and dimension limits, and every contrast or distance threshold. `SCHEMA_MAJOR` (1) is the value discovery advertises.

Package layout: `theme.json` plus `assets/`. Asset paths in `theme.json` are relative to the package root and sit under `assets/`. A `logo` block has both modes; a `background` block, when present, has both keys with `null` meaning flat `surface.base`; omitting it means flat in both modes.

## Validator API

```
check_package(theme_json, assets) -> PackageResult   # errors, warnings, normalised theme, stored assets
validate_package(theme_json, assets) -> list[ValidationError]
```

`theme_json` is a dict, str or bytes; `assets` maps package path (`assets/logo-dark.svg`) to bytes. `PackageResult.ok` is true with no errors; then `theme` is the normalised theme (colors as `#rrggbb`, asset values still package paths) and `assets` maps each referenced path to a `StoredAsset` (mime sniffed from bytes, sha256 of the bytes to store, SVGs sanitised, raster width/height).

**Error shape.** `ValidationError {path, code, message}`: `path` is dotted (`modes.dark.text.muted`, `logo.light`), `code` is stable, `message` says what to change. API form: `BrandThemeIssue`. Warnings use the same shape and never block (`unreferenced_asset`, what the sanitiser removed).

**Codes and tags.** `ERROR_CODES` maps each code to a tag. `unsafe` means Cinna Desktop's safety checks must also catch it; `incompatible` means only core refuses it.

- `unsafe`: `invalid_json`, `invalid_type`, `schema_mismatch`, `missing_key`, `missing_role`, `invalid_color`, `color_alpha`, `invalid_text`, `text_too_long`, `invalid_asset_path`, `missing_asset`, `unsupported_asset_type`, `asset_too_large`, `asset_corrupt`, `svg_encoding`, `svg_entities`, `svg_parse`, `svg_not_svg`, `svg_missing_viewbox`, `svg_too_complex`.
- `incompatible`: `asset_path_nested`, `color_out_of_gamut`, `asset_animated`, `unknown_key`, `name_too_long`, `low_contrast`, `chart_slots_too_similar`, `bubble_indistinct`, `accent_reads_as_danger`, `logo_too_small`, `logo_aspect`, `background_too_small`, `background_unreadable`, `background_modes_swapped`.

Unknown keys are `incompatible` on purpose: the desktop ignores them for forward compatibility, only core refuses them. Asset paths must sit directly in `assets/` (`asset_path_nested` otherwise): a browser folder or file upload keys files by name, so a nested path would pass a zip upload and fail the others. That rule is about packaging, not safety (the served form has no paths), hence `incompatible`.

**Rules in `incompatible`.**
- Contrast, per mode: `text.primary` against `surface.base`, `surface.raised` and `surface.input`, and `text.secondary` against `surface.base` and `surface.raised`, at 4.5; `accent.on` on `accent` and `accent.hover` at 4.5; each `status.<kind>.text` on `surface.base` at 4.5; `text.muted`, `accent`, each `status.<kind>`, the chart categorical slots, `chart.pos` and `chart.neg` against `surface.base` at 3.0; `text.primary` on `bubble.user` at 4.5.
- Chart slots 1-8 are at least OKLab delta E 0.08 apart; `bubble.user` is at least 0.03 from `surface.base`.
- `accent` with OKLCH chroma above 0.1 must be at least 20 degrees of hue away from `status.danger`.
- Logo: SVG up to 256 KiB, raster up to 1 MiB and at least 124 px tall, aspect at most 10:1 (drawn `contain` into 240x62, must stay 24 px or taller).
- Background: SVG up to 512 KiB, raster up to 3 MiB and at least 1600x1000, at most 4096 on a side; with both modes present the dark median luminance must be lower than the light one.
- **Readability.** Each background is reduced to a 64x40 sample (rasters with Pillow, SVGs rasterised with `resvg-py`, imported lazily; the sample is composited over `surface.base`). For `text.primary` and `text.secondary`, at least 95 % of sampled pixels must give a contrast of 4.5 or more; otherwise `background_unreadable`, with advice to lighten or darken the image or the text color. Soft images (mesh, blur) pass, photos and busy patterns usually do not.

Asset type is decided from the bytes, not the file name.

## SVG Sanitiser

`sanitise_svg(data)` returns the bytes core stores and serves (the upload is never kept) and a list of what it removed; it raises `SvgRefused(code, message)` for refusals. Output is re-serialised deterministically and the function is idempotent.

- Refused: not UTF-8, an internal DTD subset or any `<!ENTITY` (`svg_entities`), not well-formed XML, root other than `<svg xmlns="http://www.w3.org/2000/svg">`, no usable `viewBox`, more than 20,000 elements (`svg_too_complex`).
- Element allowlist (`ALLOWED_ELEMENTS`): `svg g path rect circle ellipse line polyline polygon defs linearGradient radialGradient stop clipPath mask title`. Other elements are removed with their subtree (`script`, `foreignObject`, `image`, `use`, `style`, `a`, editor metadata).
- Attributes: only `ALLOWED_ATTRIBUTES` (presentation properties plus geometry and structure attributes); `on*` handlers, `class` and editor namespaces go. `href` survives only as a local `#id`; `url(...)` survives only as `url(#id)`; `style` keeps only allowed presentation properties with safe values.
- Comments, processing instructions, a plain DOCTYPE and text outside `<title>` are dropped.
- A DTD internal subset (including `<!ATTLIST>` defaults) or any entity declaration is refused (`svg_entities`). Detection is done by expat while parsing, not by pattern matching, so a `>` inside a quoted system id or a decoy DOCTYPE in a comment cannot slip past it.
- More than 20,000 elements or nesting deeper than 64 is refused (`svg_too_complex`).

Clients also draw the logo and background only through an image element, never inline.

## Package Reader

`package.py`: `read_zip` (at most 64 entries, per-file cap, 16 MiB total uncompressed, no absolute, backslash or `..` entry names, `theme.json` at the root or inside one top-level folder, OS junk ignored, reads capped regardless of the zip header), `assets_from_files` (loose uploads keyed by base name under `assets/`, with the same 64-file and 16 MiB caps; two files with one name are refused), `read_folder` (for tools and fixtures). Failures raise `PackageError(code, message)` with `invalid_package` or `missing_theme`; the route turns them into an upload result with path `package`.

## Storage

Migration: `backend/app/alembic/versions/f725f8313224_add_brand_theme_tables.py`.

**`brand_theme`** (`BrandTheme`): `id`, `status` (`draft` | `published`), `name`, `theme_json` (JSONB, normalised, asset values are package paths), `asset_map` (JSONB, package path to sha256), `revision` (null until first published), `created_by_id`, `published_by_id` (FK `user.id`, SET NULL), `published_at`, `created_at`, `updated_at`. A partial unique index `uq_brand_theme_one_published` allows at most one row with `status = 'published'`. A new look is a new upload. The only content change a row sees is at publish, when re-validation stores the current sanitiser's and normaliser's output. At most `BRAND_THEME_MAX_DRAFTS` (20) rows are kept.

**`brand_theme_asset`** (`BrandThemeAsset`): `sha256` (PK), `mime`, `bytes` column (attribute `size`), `width`, `height`, `data` (bytea), `created_at`. Content-addressed and shared between drafts; an asset no remaining draft uses is deleted with the draft.

**Revisions** come from the Postgres sequence `brand_theme_revision_seq` on every publish, not from `max(revision) + 1`, so deleting all rows can never make a later publish reuse a revision a client holds as its ETag.

Service behaviour (`BrandThemeService`): creating a draft runs `check_package` and stores nothing on failure; every write (create, delete, publish, unpublish) holds one transaction-level advisory lock, so asset reuse and asset garbage collection never interleave; `publish` re-validates, rewrites the stored theme and assets if the current sanitiser would produce different bytes, demotes the current published row, assigns a new revision, and returns whether anything changed (no-op when the theme is already live and unchanged); deleting a published theme gives 409; `unpublish` returns the previous row or none.

## Routes

Prefix `/api/v1`. Router tag `brand-theme`.

Public, unauthenticated, rate-limited per caller (`BRAND_THEME_RATE_LIMIT_PER_MIN`, default 600, one budget shared by both reads, 429 with `Retry-After`):

| Method and path | Behaviour |
|-----------------|-----------|
| `GET /api/v1/brand-theme` | The served document (`BrandThemePublic`), or 204 when none is published. Headers: `ETag: "<revision>"`, `Cache-Control: no-cache`; a matching `If-None-Match` gets 304. |
| `GET /api/v1/brand-theme/assets/{sha256}` | Stored bytes, 404 unless a 64-hex hash matches. Headers: `Cache-Control: public, max-age=31536000, immutable`, `X-Content-Type-Options: nosniff`, `ETag: "<sha256>"`; SVG also gets `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'`. Serves any stored asset, including a draft's: the admin preview loads them as `<img>`, which cannot send a bearer token. A draft asset is reachable only by its sha256, which cannot be guessed but is visible to whoever sees the admin's network traffic. |

Superuser only (`get_current_active_superuser`):

| Method and path | Behaviour |
|-----------------|-----------|
| `POST /api/v1/admin/brand-theme/drafts` | Multipart: either `package` (zip) or `theme` (+ optional `files`), not both (400). Returns `BrandThemeUploadResult {id, errors, warnings}`; `id` is null and nothing saved when `errors` is non-empty. 413 for oversize bodies; 409 when the draft limit is reached. |
| `GET /api/v1/admin/brand-theme/drafts` | `BrandThemeDraftsPublic {data, count, max_drafts}`, newest first, published included. |
| `GET /api/v1/admin/brand-theme/drafts/{theme_id}` | `BrandThemeDraftPublic`, including `document`, the served projection with asset URLs. |
| `DELETE /api/v1/admin/brand-theme/drafts/{theme_id}` | 204; 409 while published. |
| `POST /api/v1/admin/brand-theme/drafts/{theme_id}/publish` | Re-validates; 409 with `{errors: [...]}` if it fails; returns the draft. |
| `POST /api/v1/admin/brand-theme/unpublish` | 204, idempotent. |

The served document is built by `served.served_document(theme, assets_by_path, revision)`: `{schema, revision, theme, assets}`, where each logo and background path in `theme` is replaced by an asset object `{url, type, bytes, sha256, width?, height?}` (`url` is relative to the server, `/api/v1/brand-theme/assets/<sha256>`) and the same objects are listed under `assets` keyed `logo.dark`, `logo.light`, `background.dark`, `background.light`. A `null` background mode stays `null`.

nginx: `/api/` already reaches the backend, so no proxy change is needed.

## Discovery

`GET /.well-known/cinna-desktop` (`cinna_desktop_discovery` in `backend/app/main.py`) adds `brand_theme: {schema: <SCHEMA_MAJOR>, url: <backend base>/api/v1/brand-theme}` only while a theme is published. Discovery is cached by the desktop for a whole session, so freshness comes from the theme endpoint's ETag, not from discovery. See [Desktop App Authentication](../desktop_auth/desktop_auth.md).

## Event

`EventType.BRAND_THEME_UPDATED` = `brand_theme.updated`, meta `{published: bool, revision: int | null}` (`revision` null after an unpublish). Emitted by `BrandThemeEvents.updated` as a background task after a publish that changed something or an unpublish that removed something; broadcast to all connected clients (the theme is public). It never raises, because the change has already committed. Cinna Desktop polls the endpoint in this release and does not listen. See [Real-Time Event Bus System](../realtime_events/event_bus_system.md).

## Shared Fixtures

`backend/app/services/brand_theme/fixtures/` is part of the contract and is **copied by Cinna Desktop** so its parser and safety checks are tested against what core produces. The layout is described in `backend/app/services/brand_theme/fixtures/README.md`.

`backend/tests/unit/test_brand_theme_fixtures.py` checks every `invalid/` package fails with exactly the errors in its `expect.json`, each tag agrees with `ERROR_CODES`, SVG cases match their goldens, the sanitiser is idempotent, and every `served/` snapshot equals what the projection produces. After an intended change, regenerate the goldens and snapshots:

```
BRAND_THEME_REGEN_FIXTURES=1 python -m pytest tests/unit/test_brand_theme_fixtures.py
```

then review the diff and re-copy the fixtures into Cinna Desktop.

API behaviour (upload, publish, public endpoint, headers) is covered in `backend/tests/api/brand_theme/`.

## Web sign-in pages

`useBrandTheme()` queries `GET /api/v1/brand-theme` under the key `["brandTheme"]` (30 min stale time, 3 s timeout); a 204 is cached as "no theme", while an error or a timeout throws, shows stock meanwhile and is asked again on the next mount or window focus, so a slow first request never leaves the pages unbranded for the stale time. `parseBrandTheme` re-checks every color it uses against `^#[0-9a-f]{6}$` and drops the whole theme on any failure; an asset whose path is not `/api/v1/brand-theme/assets/<sha256>` is dropped alone. Asset paths are relative in the payload and are prefixed with the API base, so split-host deployments work.

`AuthBrandScope` always renders its wrapper element (so the form never remounts when the theme arrives) and, with a theme, sets `data-skin="brand"` on that element, never on `<html>`, plus a `<style>` built only from parsed tokens: light under `[data-skin="brand"]`, dark under `.dark [data-skin="brand"]`. The user's mode class decides which applies.

Role → token map (`BRAND_TOKEN_ROLES`, the only map on the web): `--background` surface.base; `--card`, `--popover` surface.raised; `--muted`, `--secondary` surface.tertiary; `--accent` surface.hover; `--foreground` and the card, popover, secondary and accent foregrounds text.primary; `--muted-foreground` **text.secondary** (body copy on these pages; text.muted is only validated at 3:1); `--primary`, `--ring` accent; `--primary-foreground` accent.on; `--border`, `--input` border; `--destructive` **status.danger.text** (it only paints error text here); `--success`, `--warning` the status roles; `--chart-1..5` chart.1–5. Not mapped: status foregrounds and sidebar tokens.

Placement: in `AuthLayout` the decorative panel (≥ 1024 px only) shows the background as an `<img>` with `object-cover` over `surface.base`, or a flat `surface.raised`, and the logo in a 240×62 `object-contain` box; the form column only changes color. `ConsentShell` (the consent pages: `NativeAuthConsentPage` with `branded`, and `DeviceLoginConsentPage`) puts the background behind the whole page and the logo above the card, and holds its spinner until the theme query settles. A logo or background that fails to load falls back to the stock mark or the flat fill.

Skins: under a skin set on `<html>` (hacker-1980), the brand wins inside the scope. Tier A tokens come from the map; `brand-scope.css` resets `--radius` and the skin's decorative tokens and hands the skin's card, title, button, input and scrollbar rules back to the stock styles with `revert-layer`, keeping the stock focus ring. Portalled menus, toasts and tooltips render outside the scope and keep the stock or skin look.

## Admin Page

`/admin/brand-theme` (superuser) reads `GET /api/v1/admin/brand-theme/drafts`; each draft's `document` is the served shape, so previews need no further request. Mutations invalidate the drafts query and the shared `["brandTheme"]` query.

- **Upload** uses the shared `Common/UploadSourcePicker`: a zip goes as `package`; a folder or loose files go as `theme` (the root `theme.json`) plus `files` (Upload stays disabled without a `theme.json`); a package folder sends only its `assets/*`, so working files (README, preview.html) are not reported as stray assets. A refused upload keeps the dialog open with the issues and, below them, any non-blocking notes; an accepted one opens the new draft's preview with the warnings.
- **Grouped issues** (`brandThemeIssues.ts`): `modes.dark.*`, `logo.dark`, `background.dark` → Dark; the light equivalents → Light; everything else → General; shown in that order with counts, path in monospace and the message. The same list renders a publish refusal (the 409 body's `errors`), after which Publish is disabled.
- **Publish** happens only from the preview dialog; the row menu's Publish opens it. **Unpublish** and **Delete** confirm in an `AlertDialog`; Delete is disabled on the published row.
- **Preview** (`BrandThemePreview`): one scoped element per pane with an explicit mode, so previews ignore the page's `.dark` class and never leak into each other. `buildScopedBrandCss` writes the tokens under a unique `data-brand-preview` id (the id is validated) from the same `BRAND_TOKEN_ROLES` map; the extra roles the mock shows (`PREVIEW_ROLES`: bubble.user, status.*, chart.1–8, …) go through the same hex check, and a draft that fails it shows "This draft can't be previewed" rather than a partial palette. The mock is `inert` with `role="img"`. Its sign-in card is drawn as the real pages draw it (border-only input, hint text in `--muted-foreground` = text.secondary), so the preview never shows worse contrast than users get.

## Authoring Skill (`brand-theme`)

A catalog skill an agent uses to turn a brandbook into a package. Business flow: [Brand Themes](brand_themes.md#an-agent-turns-a-brandbook-into-a-package-brand-theme-skill).

**Layout** (`backend/app/services/brand_theme/skill/brand-theme/`; kept under `backend/app/` so the container's test run sees it):

```
SKILL.md                     the workflow: template → colors → logo → backgrounds → validate → preview + package
template/theme.json          stock Cinna colors adjusted to pass every rule (the valid/minimal fixture palette)
template/assets/             the stock meshes, upscaled to 1652×1000 (the valid/full fixture images)
template/README.md           one sentence per role, from schema.ROLE_MEANINGS
scripts/validate|mesh|preview|package
scripts/_lib/skill_lib       shared helpers; loads the copy below as package cinna_brand_theme
scripts/_lib/brand_theme/    schema, colors, images, svg_sanitizer, validator, package: verbatim copies, no extension
```

**No `.py` files anywhere in the skill.** A core whose reload watch covers published skills restarts when one holds a `.py` file, so the scripts and the copied modules are extensionless and loaded with `SourceFileLoader` (a meta-path finder maps `cinna_brand_theme.<name>` to `scripts/_lib/brand_theme/<name>`, so the copies' relative imports work unchanged). Scripts set `sys.dont_write_bytecode`. Each script is a PEP 723 script with its dependencies inline (Pillow, resvg-py; `mesh` adds numpy) and runs as `uv run --script scripts/<name> …`. Any file under `scripts/` makes the prompt generator's `## Skill Scripts` block name the skill.

**Scripts.**
- `validate PACKAGE [--json]`: a folder, its `theme.json` or a zip (read with `read_folder` / `read_zip`), then `check_package`. One line per issue, `ERROR <path> [<code>]: <message>` / `WARN …`, then `VALID|INVALID: n errors, m warnings.` Exit 0 valid, 1 invalid, 2 unreadable (code `invalid_package` / `missing_theme` from the package reader, else `unreadable`).
- `mesh theme.json --mode dark|light|both [--colors …] [--seed N] [--candidates N] [--softness 0–1] [--strength auto|0–1] [--format webp|svg] [--out DIR]`: 3–6 gaussian blobs from a seeded `random.Random` layout (edges and corners, calm center; one layout for both modes), colors from roles or literals (default `accent,chart.2,chart.3`), averaged in OKLab and mixed toward `surface.base` by coverage × strength. WebP is 2560×1600 with seeded triangular dither (the field is computed at a quarter size and upscaled); SVG is `radialGradient` fills on `rect`s written in the sanitiser's canonical form, so sanitising it changes no byte. `auto` bisects the strength against the **validator itself** (`skill_lib.background_errors` runs `check_package` with only that background; rasters are probed at 1600×1000), keeps 85 % of the strongest readable strength, re-checks the final file, and reports "the text roles, not the image, need changing" when even strength 0.06 is refused as `background_unreadable` (any other refusal, such as a rendering failure, is printed as the validator gives it). Files are `bg-<mode>-s<seed>.<ext>`; with both modes the pair is checked for swapped modes. Exit 0 all readable, 1 a mode failed, 2 bad input.
- `preview PACKAGE [--candidates DIR] [--out FILE]`: self-contained `preview.html` (images inlined): desktop main screen (sidebar, new-chat logo and heading, user and agent bubbles, status chips, chart strip, composer) and the sign-in card, dark and light, one `section.row` per background: the one `theme.json` names, then every `bg-<mode>-s<seed>` group in `assets/`, each with the validator's verdict (a background file `theme.json` names but the package lacks is refused, and rows read "not measured" while `surface.base` or the text roles do not parse).
- `package PACKAGE [--out FILE]`: validates, then zips `theme.json` and only the referenced assets at the zip root with fixed timestamps, reads the zip back through `read_zip` + `check_package`, and writes nothing when invalid.

**Sync check.** `test_brand_theme_skill.py` fails when any file in `scripts/_lib/brand_theme/` differs byte for byte from its core module, or the folder holds anything else. After changing a pure module, copy it again (`cp backend/app/services/brand_theme/<m>.py backend/app/services/brand_theme/skill/brand-theme/scripts/_lib/brand_theme/<m>`) and republish the skill. The same test runs every `invalid/` fixture through the skill's `validate` and expects exactly core's errors, checks mesh determinism, stock roles passing with `auto` in both modes, the weak-`text.secondary` report, the SVG sanitiser round trip, the CLI exit codes, the zip, the preview rows, and that the built archive passes the catalog publish gate (`safe_extract`, `parse_skill_dir`, `SkillCatalogService.validate_entry`).

**Publishing.** The skill is published like any catalog skill, by a superuser (or anyone they choose), as a new package or a new version:
1. `make brand-theme-skill` (or `python3 backend/app/scripts/build_brand_theme_skill.py OUT.tgz`) writes `drafts/brand-theme.tgz`: a deterministic `brand-theme/…` tar.gz without caches or OS junk, the format the desktop publish path takes.
2. Upload it: `POST /api/v1/skills/packages/publish` (multipart `archive`, `meta` JSON such as `{"visibility": "users", "version": "1"}`; a later version adds `package_uuid` and `base_revision_number`), see [Agent Skills](../../agents/agent_skills/agent_skills.md#5c-publishing-a-skill-from-a-desktop). Alternatively copy the folder into a cloud agent's `workspace/skills/brand-theme/` and publish from the agent's Addons tab or `cinna skills publish <agent> brand-theme`.
3. Install it on the agent that will do the authoring; its environment needs `uv` and network access for the scripts' first run.

## Configuration

`BRAND_THEME_RATE_LIMIT_PER_MIN` (`backend/app/core/config.py`, default 600): per-IP budget for the two public reads. Dependency: `resvg-py` (`backend/pyproject.toml`).
