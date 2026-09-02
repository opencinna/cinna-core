# Local Agent Kit — Technical Details

See [business logic](local_agent_kit.md) for the feature overview, flows and
business rules. This doc covers the routes, service, rendering, storage, kit
content sync, frontend surfaces, and tests.

## File Locations

### Backend

- `backend/app/api/routes/local_agent_kit.py` — `start_router`, the public
  `/agent-start` surface (mounted twice — see **Mounting**)
- `backend/app/services/cli/local_agent_kit_service.py` — `LocalAgentKitService`:
  render, hash, cache, tarball, HTML landing page
- `backend/app/services/cli/platform_knowledge_assets.py` — `local_kit_dir()`,
  `LOCAL_KIT_SUBDIR`, `snapshot_cache_key()` (shared with `ContextPackageService`)
- `backend/app/services/cli/context_package_service.py` — packages the kit's
  rendered tree under `context/local-kit/` in the account context package
- `backend/app/models/server_config/server_config.py` — `ServerConfig.local_agent_kit_enabled`,
  `ServerConfigUpdate.local_agent_kit_enabled`
- `backend/app/services/server_config/server_config_service.py` — unchanged
  `update()` path; the new field rides the existing partial-update / no-version-bump
  logic (only disclaimer content/mode bump `disclaimer_version`)
- `backend/app/core/config.py` — `LOCAL_AGENT_KIT_RATE_LIMIT_PER_MIN` (default
  `120`), `CINNA_CLI_INSTALL_SPEC` (default `"cinna-cli"`); reuses
  `FRONTEND_HOST`, `backend_base_url`, `PROJECT_NAME`, `MINIMUM_CLI_VERSION`
- `backend/app/services/common/rate_limiter.py` — existing `RateLimiter`, a
  fresh instance owned by the route module (`_kit_rate_limiter`)
- `backend/app/main.py` — mounts `local_agent_kit_router` at `/agent-start` and
  `/api/agent-start`; `X-Kit-Version` added to the app-wide CORS `expose_headers`
- `backend/app/alembic/versions/7de57f5d4b8f_add_local_agent_kit_enabled_to_server_.py` —
  migration (down-revision `1367eb81ac66`)

### Kit content (shipped product, not repo documentation)

- `docs/local_agent_kit/` — the kit's source of truth: `START.md`, `README.md`
  (index + capability ladder), `kit.json`, `VERSION` (placeholder), `CHANGELOG.md`,
  `guides/01`–`12-*.md`, `assistants/{claude-code,codex,other}.md`,
  `schema/cinna-agent.schema.json`, `templates/root/*`, `templates/agent/*`
  (the scaffold), `tools/kit.py`
- `.cinna-core-kit/scripts/sync_platform_knowledge.py` — step 3, copies
  `docs/local_agent_kit/` → the knowledge-template snapshot below
- `backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit/` —
  the synced snapshot; the **only** copy of the kit present inside the backend
  container at runtime (`docs/` is not shipped in the image)

### Frontend

- `frontend/src/hooks/useLocalAgentKit.ts` — `useLocalAgentKitAvailable()`
- `frontend/src/components/Common/CopyPromptSnippet.tsx` — `localAgentKitStartUrl()`,
  `localAgentKitPrompt()`, `<CopyPromptSnippet />`
- `frontend/src/components/Admin/LocalAgentKitCard.tsx` — admin toggle card
- `frontend/src/components/Onboarding/GettingStartedModal.tsx` — `local-first`
  article
- `frontend/src/components/Common/RotatingHints.tsx` — conditional hint entry
- `frontend/src/components/UserSettings/LocalDevelopmentCard.tsx` — collapsed
  "starting from scratch" hint
- `frontend/src/routes/login/index.tsx` — login-page link
- `frontend/src/routes/_layout/admin/server-configuration.tsx` — mounts
  `<LocalAgentKitCard />` on the Interface tab
- `frontend/nginx.conf` — `location ~ ^/agent-start(/|$)` proxy block

### Tests

- `backend/tests/api/cli/test_local_agent_kit.py` — the serving side (routes,
  headers, negotiation, rate limit, caching, instance toggle, missing snapshot)
- `backend/tests/unit/test_local_kit_tool.py` — `kit.py` itself, run as a
  subprocess against the shipped kit source (or the synced snapshot, or
  `$LOCAL_AGENT_KIT_DIR`); skips the module if no kit source is found
- `backend/tests/api/server_config/test_server_config.py` — `ServerConfig` /
  `ServerConfigUpdate` round-trip including `local_agent_kit_enabled`

## Database Model

### `ServerConfig` (addition)

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `local_agent_kit_enabled` | `bool` | `True` | Instance-level opt-out of the public `/agent-start` surface. |

### `ServerConfigUpdate` (addition)

| Field | Type |
|-------|------|
| `local_agent_kit_enabled` | `bool \| None` |

`ServerConfigService.update()` copies the field through the existing partial-update
path unchanged; the `content_changed` computation that drives `disclaimer_version`
only compares `disclaimer_markdown` / `disclaimer_display_mode`, so toggling this
flag alone never bumps the disclaimer version.

### Migration `7de57f5d4b8f`

```python
op.add_column(
    "server_config",
    sa.Column("local_agent_kit_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
)
```

Down-revision `1367eb81ac66`. Downgrade drops the column. No indexes, no FKs —
single-row table, server default keeps the existing row valid. (Autogenerate
also proposed re-typing three unrelated `cli_device_login_request` timestamp
columns from `TIMESTAMP WITH TIME ZONE` to naive `DateTime` — a known false
positive removed by hand; the DB columns are correct, the model side is not.)

## Public Routes (`start_router`, `backend/app/api/routes/local_agent_kit.py`)

`APIRouter(tags=["local-agent-kit"], include_in_schema=False, dependencies=[Depends(_rate_limit_guard), Depends(_enabled_guard)])` —
excluded from the OpenAPI schema (static content for strangers, not part of the
API contract), so there is no generated client method; the frontend hook uses a
plain `fetch`.

### Mounting (`backend/app/main.py`)

```python
app.include_router(local_agent_kit_router, prefix="/agent-start")       # canonical
app.include_router(local_agent_kit_router, prefix="/api/agent-start")   # alias
```

Two mounts of **one router** (never two copies that could drift):

- `/agent-start` — the pasteable URL. Sits at the origin root next to the SPA, so it
  needs its own reverse-proxy block (see [Nginx Setup](../../infrastructure/nginx_setup.md)).
- `/api/agent-start` — proxied by every deployment's universal `/api/` block already.
  Every kit-internal link (`KIT_BASE_URL` placeholder) points at this one, so an
  instance whose proxy was never updated for `/agent-start` still serves a fully
  working kit — only the pretty URL is affected.

### Endpoints

| Method | Path (relative to mount) | Response | Notes |
|--------|---------------------------|----------|-------|
| `GET` | `` / `/` | `text/markdown` (START.md) or `text/html` (landing page) | Content negotiation — see below. Registered on both `""` and `"/"` so `/agent-start` and `/agent-start/` resolve without a redirect. |
| `GET` | `/START.md` | `text/markdown` | Always raw, ignores `Accept`. |
| `GET` | `/version` | JSON: `kit_version`, `schema_version`, `platform_url`, `kit_base_url`, `start_url`, `instance_name`, `cli.{install_spec,min_version}` | What `kit.py refresh` polls. |
| `GET` | `/kit.json` | JSON | Same bytes as `/kit/kit.json`. |
| `GET` | `/kit.tar.gz` | `application/tar+gzip`, `Content-Disposition: attachment; filename="cinna-kit.tar.gz"` | Whole rendered kit, rooted at `cinna-kit/`. |
| `GET` | `/kit/{path:path}` | file | Exact-key lookup in the in-memory rendered tree; unknown → 404. |

Every response carries: `ETag` (per-representation, see below), `Cache-Control:
public, max-age=300`, `Vary: Accept` (mount root only), `X-Kit-Version`,
`Access-Control-Allow-Origin: *`, `Access-Control-Expose-Headers: X-Kit-Version`,
`X-Content-Type-Options: nosniff`. `If-None-Match` → `304`.

### Router-level guards (in order)

1. **`_rate_limit_guard`** — runs *before* the enabled check, deliberately: a
   throttled request never resolves `SessionDep`, so a flood against a disabled
   instance costs no DB round-trip (it does leak a 429 instead of a 404 to a
   flooder — an accepted, smaller signal).
2. **`_enabled_guard`** — reads `LocalAgentKitService.is_enabled(session)`
   (`ServerConfigService.get_or_create(session).local_agent_kit_enabled`,
   defaulting `True` via `getattr` for a pre-migration schema); **404**, never
   403, on a disabled instance.

### Content negotiation (`_wants_html`)

`?format=html|md` wins outright over `Accept` (deliberately unvalidated — any
other value falls through to markdown rather than 422ing, since no spelling of
this URL may hide the instructions). Otherwise HTML is chosen only when
`text/html` is present in `Accept` **and** ranked (by `q`) above
`text/markdown`/`text/plain` — a bare `Accept: */*` (curl, most assistants)
gets markdown. Both variants embed the **complete** `START.md` text (the HTML
page wraps it, escaped, in a `<pre>`), so a mis-negotiation never hides
instructions.

### ETag scoping (`_etag`)

`X-Kit-Version` is the same on every response (the kit-wide content version)
and therefore cannot double as the ETag: a client carrying one validator across
URLs, or a loosely-keyed CDN, would be told a file it never fetched is
unchanged. The ETag folds in a hash of the **representation name**
(`"start.md"`, `"start.html"`, `"version"`, `"kit.tar.gz"`, or the kit-relative
path) so each validator answers for exactly one resource:
`'"{kit_version}-{sha256(representation)[:8]}"'`. `_not_modified` resolves
existence *before* checking `If-None-Match`, so a stale ETag on an unknown path
returns 404, not a misleading 304.

### Rate limiting (`_limiter_key`)

Keyed by the **socket peer**, not `app.utils.client_ip` (that helper trusts the
first `X-Forwarded-For` hop unconditionally — fine for an audit log line, fatal
as the only control an anonymous surface has, since a caller can mint an
unbounded number of distinct keys and starve the limiter's key ceiling for
everyone):

- Socket peer is a **public** address → the backend is exposed directly,
  `X-Forwarded-For` is pure caller input → the peer itself is the key.
- Socket peer is **private/loopback** → the request arrived through the local
  reverse proxy, whose `$proxy_add_x_forwarded_for` *appends* the address it
  saw. The client owns every earlier hop but not the last one, so the **last**
  hop is the key (not the first, which would re-open the bypass).
- An unparseable peer (the test transport) is treated as untrusted — same as
  "directly exposed."

Behind two or more appending proxies, every caller behind the outer one shares
one bucket (the inner proxy's own address) — fails closed (over-throttling),
which is the right direction for availability. Limit:
`settings.LOCAL_AGENT_KIT_RATE_LIMIT_PER_MIN` (default 120/min); over limit →
`429` + `Retry-After`.

## Service (`LocalAgentKitService`, `backend/app/services/cli/local_agent_kit_service.py`)

### Rendering

Deliberately **plain string substitution**, not a template engine (the kit is
full of fenced shell/JSON with literal braces). `placeholders()` returns a
fixed dict from **settings only, never the request** (the `Host` header is
never read — reflecting it would let an attacker point the go-cloud guide's
`cinna login` target at themselves):

| Token | Value |
|-------|-------|
| `PLATFORM_URL` | `settings.FRONTEND_HOST` (trailing slash stripped) |
| `KIT_BASE_URL` | `{backend_base_url}/api/agent-start` — always the `/api/` alias |
| `START_URL` | `{FRONTEND_HOST}/agent-start` |
| `SIGNUP_URL` / `LOGIN_URL` | `{FRONTEND_HOST}/signup` \| `/login` |
| `INSTANCE_NAME` | `settings.PROJECT_NAME` |
| `CLI_INSTALL_SPEC` | `settings.CINNA_CLI_INSTALL_SPEC` |
| `MIN_CLI_VERSION` | `settings.MINIMUM_CLI_VERSION` |
| `KIT_VERSION` | resolved *after* hashing (see below) — absent from `placeholders()` |

`.json` members are rendered with **JSON-escaped** substitution
(`json.dumps(value)[1:-1]`) instead of a raw splice — `PROJECT_NAME` is
operator text and may legitimately contain a quote, which a naive splice into
`"instance_name": "{{INSTANCE_NAME}}"` would turn into invalid JSON. Every other
extension is spliced raw. Only the fixed token set is substituted; any other
`{{...}}` — notably the lowercase `{{name}}` / `{{slug}}` scaffold tokens inside
`templates/agent/**` — is left verbatim for `kit.py new` to fill in later, on
the user's machine.

### Content version

`kit_version` = first 16 hex chars of `sha256` over the sorted
`(relative path, sha256(rendered bytes))` pairs, computed **with
`{{KIT_VERSION}}` still literal** (the hash cannot depend on itself), then
spliced into the `VERSION` member and `kit.json`'s `kit_version` field
afterwards. Independent of mtimes, so a redeploy shipping byte-identical
content produces the same version. `kit.py refresh` compares this against the
locally stored `VERSION`.

### Caching (`_build_or_cached`)

Process-local memo: `(cache_key, kit_version, rendered_tree, tarball)` behind a
`threading.Lock`, double-checked inside the lock. `cache_key` =
`snapshot_cache_key(local_kit_dir())` — the **shared** helper in
`platform_knowledge_assets.py` (newest mtime + file count across the given
dirs), the same shape `ContextPackageService` uses so both invalidate on
exactly the same redeploy signal. A pure deletion (max mtime unchanged) is
still caught because file count is folded in.

### Reading the snapshot (`_read_snapshot`)

Walks `local_kit_dir()` (sorted), skipping `__pycache__` / `.git` /
`.pytest_cache` directories and any symlink (never followed — the runtime skip
must agree with the sync script's skip, or a link out of the kit would be
served). Accumulates size **before** reading each file and raises `503` past
`MAX_RENDERED_BYTES` (5 MiB — the kit is text; a bloated snapshot is a build
defect) rather than after loading it all. Missing or empty snapshot dir → `503`
("the image was built without `make sync-platform-knowledge`"), never an empty
`200`.

### Tarball (`_build_tarball`)

Members are added in sorted path order, rooted at `cinna-kit/`; **fixed mtime
(`0`) on every member and the gzip header itself**, so two workers building the
same rendered tree produce byte-identical tarballs — required for one strong
ETag to mean what it promises. File mode `0o755` for `*.py`, else `0o644`.

### `LocalAgentKitService` public surface

| Method | Returns |
|--------|---------|
| `is_enabled(session)` | `bool` — the `ServerConfig` flag |
| `get_version()` / `get_version_payload()` | content version / the `/version` JSON body |
| `get_versioned_file(rel_path)` | `(version, (bytes, media_type) \| None)` — one build for both |
| `get_file(rel_path)` | `(bytes, media_type) \| None` |
| `get_start_markdown()` / `get_start_html()` | rendered `START.md` / the landing page |
| `get_rendered_tree()` | `(version, {rel_path: bytes})` — consumed by `ContextPackageService` |
| `get_tarball()` / `get_versioned_tarball()` | the gzip tarball |
| `media_type_for(rel_path)` | by extension: `.md`→`text/markdown`, `.json`→`application/json`, `.yaml`/`.yml`/`.py`/`.txt`/`.toml`/`.example`/`.cfg`/`.ini`/`.sh`/extension-less (incl. bare dotfiles)→`text/plain`, else `application/octet-stream` |

### HTML landing page

A single self-contained document (`_LANDING_TEMPLATE`): no external assets, one
inline stylesheet, one inline copy-button script with no dynamic content.
`Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline';
script-src 'unsafe-inline'` is set **only** on this response (`HTML_CSP`). All
interpolated text is `html.escape`d. Embeds the complete rendered `START.md`
inside a `<pre>`.

## Kit content sync (`.cinna-core-kit/scripts/sync_platform_knowledge.py`)

Step `[3/3]`, `sync_local_agent_kit()`:

- Clears (`rmtree`) and rebuilds only `knowledge/local-kit/` — `knowledge/platform/`
  (steps 1–2) and the hand-authored `knowledge/guides/` are untouched.
- Copies **every file type**, dotfiles included (`templates/agent/.claude/settings.local.json`,
  `templates/agent/credentials/.gitignore`, `credentials/.env.example`,
  `tools/kit.py`) — the kit is served byte-for-byte (after placeholder
  rendering), so a filtered copy would ship a broken scaffold.
- Skips directories `__pycache__`, `.git`, `.pytest_cache`, `.venv`,
  `node_modules`; never follows a symlink (`is_symlink()` check, matching the
  runtime service's own skip).
- **Publishability filter** (`_is_publishable`) — every file under
  `docs/local_agent_kit/` is served to *anonymous* callers, so the sync refuses
  to copy anything that looks like an accidental secret next to the kit's
  legitimate `.env.example`: denies `KIT_DENY_NAMES` (`.DS_Store`,
  `Thumbs.db`), `KIT_DENY_SUFFIXES` (`.key`, `.pem`, `.p12`, `.pfx`, `.crt`,
  `.swp`, `.orig`), and any filename containing `.env` that does **not** end in
  `.example`.
- Fails loud (`SystemExit`) if `docs/local_agent_kit/` is missing or the copy
  yields zero files.

### Scaffold ignore-file convention

The scaffold's own path-excluding ignore rules (`app-data/`, `.venv/`, etc.)
ship **dotless** — `templates/agent/gitignore`, `templates/agent/app-data/cache/gitignore`,
`templates/root/gitignore` — because a dotted `.gitignore` inside
`templates/agent/**` would be a *live* ignore rule in whichever repository
stores the kit (this repo, and the synced snapshot), hiding scaffold files
(the three `app-data/` placeholders, `.claude/settings.local.json`) from
`git add` in *this* repository. `kit.py new` (`restore_scaffold_ignore_files`)
renames each to `.gitignore` in the newly scaffolded agent.
`templates/agent/credentials/.gitignore` is the **one exception** and keeps its
dot — it names `.env` / `credentials.json`, which no repository (including this
one) should ever track, so there a live rule is correct.
`SCAFFOLD_IGNORE_FILES` in `kit.py` is the single list of which paths get this
treatment; a test (`test_the_kit_ships_no_ignore_rule_that_hides_its_own_content`)
asserts no other `.gitignore` exists anywhere under the kit.

### `check_docs_references.py` exemption

`docs/local_agent_kit/` is excluded entirely from the repo's doc-reference
checker: its internal references resolve against a *scaffolded agent folder on
the user's machine* (`docs/WORKFLOW_PROMPT.md`, `scripts/update_status.py`) or <!-- nocheck -->
the user's chosen root (`~/Documents/MyAgents`, `Local/`, `Cloud/`,
`.cinna-kit/`), not against this repository's tree — checking them here would
report every one as broken.

## Account context package integration (`ContextPackageService`)

`context_package_service.py` adds a **4th packaged source**: the kit's own
*rendered* tree (via `LocalAgentKitService.get_rendered_tree()`, never the raw
snapshot — the packaged copy has to carry this instance's URLs, exactly like
the copy `/agent-start` serves) under `context/local-kit/`.

- **Cache key** — `snapshot_cache_key(platform_dir, examples_dir, guides_dir,
  local_kit_dir())` now includes the kit directory, so an edited kit rebuilds
  the context package too.
- **Content version** — the kit's rendered bytes are folded into
  `ContextPackageService._content_version` under the `local-kit/` label,
  alongside `platform/`, `examples/`, `guides/`, and the rendered index.
- **Degradation** — a missing or broken kit snapshot is caught (broad
  `except Exception`, not just the `503` `LocalAgentKitService` itself raises —
  a bad mount surfaces as `OSError` mid-read) and logged as a warning; the
  package is still built **without** `local-kit/`, exactly like a missing
  `examples/` or `guides/`. The instance's `local_agent_kit_enabled` switch is
  **not** consulted here — it governs the public anonymous surface only; an
  operator who stopped publishing to strangers has not withdrawn the
  conventions from their own authenticated users.
- **Index** — `_render_index()` gets a `local-kit/` row: "conventions for
  agents built locally with a coding assistant; read
  `local-kit/guides/11-go-cloud.md` when importing one."
- **Tarball mode** — same rule as the kit's own tarball: `0o755` for `*.py`
  members, else `0o644`, so `context/local-kit/tools/kit.py` and the copy
  served at `/agent-start` are literally the same bytes with the same mode.

See [Account CLI Workspace — Downloading the Platform Context Package](../cinna_cli_integration/account_cli_workspace.md#4-downloading-the-platform-context-package)
for the rest of the package's shape and the `/version` staleness contract,
which the kit addition rides unchanged.

## Frontend

### `useLocalAgentKitAvailable()` (`hooks/useLocalAgentKit.ts`)

```ts
useQuery<LocalAgentKitVersion>({
  queryKey: ["localAgentKitVersion"],
  queryFn: () => fetch(`${API_BASE_URL}/api/agent-start/version`)...,
  staleTime: Number.POSITIVE_INFINITY,
  retry: false,
})
```

Probes `GET /api/agent-start/version` (the alias, not `/agent-start` — works even on a
proxy missing the pretty-URL block) with a plain `fetch` — the route is
excluded from the OpenAPI schema, so there is no generated client method.
`API_BASE_URL` falls back to `""` (same-origin), matching `OpenAPI.BASE`. One
shared query key means every call site (Rotating Hints, Getting Started Modal,
Local Development card, login page) costs one request per browser session;
failures are silent by design (this only decides whether an optional pointer
shows). `LocalAgentKitCard`'s save mutation also invalidates
`["localAgentKitVersion"]`, so an admin toggling the switch sees the other
surfaces reflect it in the same session.

### `CopyPromptSnippet.tsx`

- `localAgentKitStartUrl()` — **`window.location.origin` + `/agent-start`**,
  deliberately not `VITE_API_URL`: `/agent-start` is the pasteable URL served at the
  SPA's own origin through the explicit nginx `/agent-start` block; `VITE_API_URL`
  points at the API host, which on a split deployment is not where the pretty
  URL lives.
  In local dev the Vite server proxies `^/agent-start(/|$)` to `VITE_API_URL`
  (`frontend/vite.config.ts`), so the origin URL works there too.
- `localAgentKitPrompt(startUrl?)` — `` `read ${startUrl} and help me start making my agents` ``
- `<CopyPromptSnippet startUrl? className? />` — the shared copyable block used
  by the Getting Started article, the admin card (via its own URL display), and
  the Local Development card hint.

### `LocalAgentKitCard.tsx` (Admin → Server Configuration → Interface)

Sits next to `DisclaimerCard`, sharing its `["serverConfig"]` query /
`ServerConfigService.updateServerConfig` mutation. On success invalidates both
`["serverConfig"]` and `["localAgentKitVersion"]` (the other surfaces' cached
probe answer is now stale for this session). Renders the switch plus the
instance's `/agent-start` URL with a copy button; default `enabled = config?.local_agent_kit_enabled ?? true`
matches the column default.

### `GettingStartedModal.tsx` — `local-first` article

New `ArticleId`, inserted after "How to Build An Agent". Content: 3-step visual,
`<CopyPromptSnippet />`, the resulting folder diagram, a "no account needed"
callout, cross-links to `build-agent` and `conversation-vs-building`. The whole
article list is filtered by `useLocalAgentKitAvailable()` — `visibleArticles`
drops `local-first` (and re-selects a fallback article) on a disabled instance,
since its entire content is a prompt pointing at a URL that would 404.

### `RotatingHints.tsx`

Appends one conditional hint (`LOCAL_AGENT_KIT_HINT`) to the shuffled pool only
when `useLocalAgentKitAvailable()` is true — folded into the `useMemo` shuffle
dependency array so toggling availability mid-session re-shuffles correctly.

### `LocalDevelopmentCard.tsx`

A collapsed disclosure (`ChevronDown`, `scratchOpen` state) below the existing
cloud-workspace Setup section, gated on `useLocalAgentKitAvailable()` (hidden
entirely, not just collapsed, on a disabled instance) — "Starting from scratch
on a new machine? Paste into your coding assistant", expanding to
`<CopyPromptSnippet />`.

### `routes/login/index.tsx`

A muted link under the login form, gated on `useLocalAgentKitAvailable()`:
`href="/agent-start?format=html"` (the pretty URL a person would type or share, not
the `/api/agent-start` alias the probe itself used), `target="_blank" rel="noopener"`.

## Nginx (`frontend/nginx.conf`)

```nginx
location ~ ^/agent-start(/|$) {
  set $upstream http://backend:8000;
  proxy_pass $upstream;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
}
```

A **regex** location (`~ ^/agent-start(/|$)`), not the prefix form `location /agent-start`:
an nginx prefix match is on the raw string, so `location /agent-start` would also
capture a future SPA route named `/startup` or `/starter-kit` and proxy it to a
backend path that 404s. The regex matches `/agent-start`, `/agent-start/`, and
`/agent-start/<anything>` only. See
[Nginx Setup](../../infrastructure/nginx_setup.md#start) for the production
reverse-proxy requirement and the `/api/agent-start` fallback rationale.

## `kit.py` (`docs/local_agent_kit/tools/kit.py`)

Stdlib-only (Python ≥ 3.10, declared as PEP 723 inline script metadata so `uv run` provisions the interpreter — macOS ships 3.9), runs on the **user's** machine — this is shipped
content, not backend code, and is unit-tested as a subprocess in
`backend/tests/unit/test_local_kit_tool.py` to genuinely exercise the
no-third-party-imports constraint.

| Command | Behaviour |
|---------|-----------|
| `new <slug> [--name N] [--root DIR]` | Copies `templates/agent/` to `<root>/Local/<slug>/`, restores dotted `.gitignore` files (`restore_scaffold_ignore_files`), substitutes `{{name}}`/`{{slug}}`, stamps `slug`/`name`/`kit_version` into the manifest. Refuses if the target folder already exists. |
| `validate <path> [--fix] [--json] [--cloud-ready]` | Manifest schema subset (`validate_manifest`), required/expected files, secret hygiene (`_validate_secrets` — tracked/untracked `.env`, stray env files outside `credentials/`, key-material filenames), `workspace_requirements.txt` mirrors `pyproject.toml` deps (`--fix` regenerates), `CLI_COMMANDS.yaml` names valid + mirrored in the Makefile, scripts catalogued in `scripts/README.md`. `--cloud-ready` promotes readiness *advice* (empty `example_prompts`, an unedited scaffold `description`) to hard errors — the gate `guides/11-go-cloud.md` step 7 requires. Exit 1 on any error (warnings never fail a run). |
| `list [--root DIR]` | Table of `Local/*` — slug, name, ladder rungs present (`_rungs_present`, inferred from the manifest and folder contents, never hand-tracked), cloud-imported flag — plus `Cloud/agents/*` if `Cloud/.cinna/account.json` exists. |
| `refresh [--check]` | Reads `kit.json.kit_base_url`, `GET {base}/version`; if different from the local `VERSION`, downloads `{base}/kit.tar.gz` and atomically swaps `.cinna-kit/` (old tree kept as a timestamped backup only until the swap succeeds). Any network failure is a **warning, never a blocked session** — `refresh` always exits 0 except on a genuinely malformed archive. Refuses a non-`http(s)` `kit_base_url` outright. |
| `export <path> --to DIR [--force]` | Produces the exact tree `cinna agent import` pushes: applies `kit.json.cloud_import.exclude` (gitignore-shaped matching, `is_excluded`) plus a hardcoded `ALWAYS_EXCLUDE` (`credentials/`, `.git/`, `.venv/`, every `.env*` shape, `credentials.json`) that applies **even if** `kit.json` says otherwise; regenerates `workspace_requirements.txt`; clears the manifest's `cloud` block. Runs the same `--cloud-ready` validation gate first and refuses on any error unless `--force`. |

### Safe tar extraction (`safe_extract`, used by `refresh`)

Rejects absolute member paths, `..` segments, any member resolving outside the
destination, and every non-regular-file/directory member type (symlink,
hardlink, device, fifo) — `tarfile`'s own default extraction behaviour is not
relied on. Strips setuid/setgid/sticky and group/other write bits from every
member's mode.

### Manifest validation subset

`validate_manifest` checks `schema_version` (must equal `1`; a newer value the
tool has never seen is a hard error telling the user to `kit.py refresh`
first), `name`/`description`/`slug` shape, `prompts{}` keys, `example_prompts[]`
non-empty strings, `credentials[]` (type ∈ the platform `CredentialType` names,
`env_prefix` shape, and a guard that flags any accidental `value`/`secret`/
`token`/`password`/… key — the manifest never holds a credential value),
`schedules[]` (`cron_string` shape, `schedule_type` ∈ `static_prompt` |
`script_trigger`, `prompt` required for the former / `command` for the latter),
`handovers[].target_slug` shape. This is a **pragmatic stdlib-only subset** of
`schema/cinna-agent.schema.json` — a backend unit test
(`test_template_manifest_matches_the_shipped_schema`) validates the shipped
scaffold manifest against the full JSON Schema (via `jsonschema`, skipped if
unavailable) **and** asserts the stdlib subset validator agrees, so the two
never silently diverge.

## Tests

### `backend/tests/api/cli/test_local_agent_kit.py` (the serving side)

Two autouse fixtures reset process-global state the snapshot-mtime cache key
does not cover: a fresh `RateLimiter` instance per test, and
`LocalAgentKitService._cache = None` (needed because a test that monkeypatches
a setting — e.g. `PROJECT_NAME` — does not move the snapshot's mtime, so
without the reset the next assertion would read the previous test's render).
Covers, on **both mounts**: markdown-vs-HTML negotiation (including `?format=`
override and an unknown-value fallback), `/version` == tarball `VERSION` ==
`X-Kit-Version` header == `kit.json.kit_version` (one identity, four carriers),
path traversal / symlink / unknown-path 404s (asserted against the property —
in-memory dict lookup — not the implementation), Host-header non-reflection,
unrendered-placeholder scan across the whole tarball, JSON-escaping under a
hostile `PROJECT_NAME` containing a quote, per-representation ETag scoping
(cross-file 304 must **not** happen), rate limiting (including the spoofed
`X-Forwarded-For` and proxied-last-hop cases described above), the instance
disable/enable round-trip through the real `PUT /admin/server-config` path, and
missing-snapshot → 503 with cache recovery afterward.

### `backend/tests/unit/test_local_kit_tool.py` (`kit.py` itself)

Locates the kit source via `$LOCAL_AGENT_KIT_DIR`, a repo checkout
(`docs/local_agent_kit/`), or the synced snapshot, in that order; skips the
whole module if none is found (so it self-adapts to running inside vs. outside
the Docker image). Every command is invoked as a real subprocess
(`sys.executable kit.py ...`), which genuinely exercises the stdlib-only
constraint. Covers: the dot-restoration convention (and a repo-wide assertion
that no other `.gitignore` exists under the kit), a fresh scaffold validates
clean, `--json` machine-readable reports, `--fix` regenerating
`workspace_requirements.txt`, every validation failure mode (empty workflow
prompt, slug/folder mismatch, stray `.env` outside `credentials/`), the
`--cloud-ready` gate promoting advice to errors, `export` excluding
`credentials/` / `AGENTS.md` / `CLAUDE.md` / `.claude/` / `app-data/` / `.venv/`
and never leaking a secret value into stdout, and `safe_extract` rejecting
traversal / symlink / special-file members while accepting a well-formed
archive.

## Config Knobs

| Setting | Default | Purpose |
|---------|---------|---------|
| `LOCAL_AGENT_KIT_RATE_LIMIT_PER_MIN` | `120` | Per-caller backstop against tarball hammering. |
| `CINNA_CLI_INSTALL_SPEC` | `"cinna-cli"` | Rendered into the go-cloud guide's `uv tool install {{CLI_INSTALL_SPEC}}`; a dev instance points this at a VCS/local path spec. |

Reused unchanged: `FRONTEND_HOST`, `backend_base_url`, `PROJECT_NAME`,
`MINIMUM_CLI_VERSION`.

---

*Last updated: 2026-09-02*
