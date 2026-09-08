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

- `docs/local_agent_kit/` — the kit's source of truth. **Contract members**
  (see **The contract** below): `kit.json`, `layout.json`, `CONTRACT_VERSION`,
  `CHANGELOG.md`, `schema/**` (`cinna-agent.schema.json`,
  `publications.schema.json`), `templates/**` (`templates/root/*`,
  `templates/agent/*` — the scaffold). **Kit-only**: `START.md`, `README.md`
  (index + capability ladder), `VERSION` (a `{{KIT_VERSION}}` placeholder),
  `guides/01`–`12-*.md`, `assistants/{claude-code,codex,other,cinna-desktop}.md`,
  `tools/kit.py`
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
| `GET` | `/version` | JSON: `kit_version`, `platform_url`, `kit_base_url`, `start_url`, `instance_name`, `cli.{install_spec,min_version}` | What `kit.py refresh` polls. Built by `_version_payload`. **No `schema_version`** — see below. |
| `GET` | `/kit.json` | JSON | Same bytes as `/kit/kit.json`. |
| `GET` | `/kit.tar.gz` | `application/tar+gzip`, `Content-Disposition: attachment; filename="cinna-kit.tar.gz"` | Whole rendered kit, rooted at `cinna-kit/`. |
| `GET` | `/contract/version` | JSON: the `/version` envelope **plus** `contract_version` | For a host that consumes only the contract. **503** on an incoherent snapshot. |
| `GET` | `/contract.tar.gz` | `application/tar+gzip`, `Content-Disposition: attachment; filename="cinna-contract.tar.gz"` | Contract subset only, rooted at `cinna-contract/`. **503** on an incoherent snapshot. |
| `GET` | `/kit/{path:path}` | file | Exact-key lookup in the in-memory rendered tree; unknown → 404. |

**`schema_version` is not in the `/version` response.** It used to be
synthesised there as a literal `1`, mirroring a field `kit.json` also carried;
both were removed in one change, because a payload that disagrees with the
shipped file is worse than either state alone — a number on the wire with no
backing artefact in the tree reads as a serving bug rather than a deliberate
removal. The literal was a compiled-in constant, so nothing could have branched
on it: a constant field is exactly the one whose removal breaks only code that
reads it without using it. `_version_payload` carries a note saying so; do not
re-add it "for parity with `kit.json`". **The manifest's own `schema_version`
is a different, still-live legacy marker** (`cinna-agent.json`, the schema's
legacy-exemption `allOf`, `kit.py`'s `_validate_identity`) and is untouched by
this — `kit.py`'s `MANIFEST_NAME` is `cinna-agent.json` and its `KIT_JSON_PATH`
is `kit.json`; no code path reads one for the other.

Both contract routes share `_serve_tarball` with `/kit.tar.gz` (one body for
the validator, header and `Content-Disposition` plumbing) and inherit the
surface's behaviour without exception: unauthenticated, the
`local_agent_kit_enabled` 404 guard, the rate limiter, per-representation
ETags, `X-Kit-Version`, `Cache-Control`. `frontend/nginx.conf` and the Vite dev
proxy both match `^/agent-start(/|$)`, so the two new paths ride the existing
block with no config change.

**Both contract representations are validated by `kit_version`, never by
`contract_version`** (the decision is recorded in a comment above the two route
handlers). `contract_version` is hand-maintained and does not move when a
template or the schema changes, so an ETag — or `X-Kit-Version` — keyed on it
would answer "unchanged" to a client that is in fact holding a stale contract,
which is the one thing a validator promises cannot happen. `kit_version` moves
on any content change. The body still carries `contract_version`; the validator
does not.

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

### Rate limiting (`anonymous_caller_key`)

The caller-key helper lives in `backend/app/services/common/rate_limiter.py` as
`anonymous_caller_key(request)` (with `is_private_peer`), shared with the other
anonymous surface that needs it — the public access-policy projection (see
[Access Policy — tech](../server_configuration/access_policy_tech.md)). It is
keyed by the **socket peer**, not `app.utils.client_ip` (that helper trusts the
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

### The contract (`CONTRACT_MEMBERS`, `CONTRACT_TARBALL_ROOT`)

```python
CONTRACT_VERSION_MEMBER = "CONTRACT_VERSION"
LAYOUT_MEMBER           = "layout.json"
CHANGELOG_MEMBER        = "CHANGELOG.md"
CONTRACT_TARBALL_ROOT     = "cinna-contract"
CONTRACT_TARBALL_FILENAME = "cinna-contract.tar.gz"

CONTRACT_MEMBERS = frozenset({INDEX_MEMBER, LAYOUT_MEMBER, CONTRACT_VERSION_MEMBER, CHANGELOG_MEMBER})
CONTRACT_MEMBER_PREFIXES = ("schema/", "templates/")
```

`_is_contract_member(rel_path)` is `rel_path in CONTRACT_MEMBERS or
rel_path.startswith(CONTRACT_MEMBER_PREFIXES)` — a **selector over the one
rendered tree**, not a second tree. Everything else in the kit (`START.md`,
`README.md`, `guides/`, `assistants/`, `tools/`) is prose for a human or a
coding assistant and is deliberately outside it.

`VERSION` is deliberately **absent** from the contract: it holds the *kit*
content version, and shipping it inside a contract tree would put two meanings
on one filename. The contract's own version is `CONTRACT_VERSION` (and
`kit.json`'s `contract_version`), hand-maintained rather than derived. So a
consumer's secondary read must be `CONTRACT_VERSION` — Cinna Desktop's
`readVersionAt` (`src/main/kit/contractStore.ts`) currently falls back to
`VERSION`, which a contract tarball never ships; that is a required
desktop-side change, and it should never fire in practice since `kit.json` is
the primary read and the coherence guard refuses to serve an archive without
it.

### Contract coherence (`_contract_defect_reason`, `_require_serviceable_contract`)

The packer is a **filter**, not an assertion: a member that is not in the
rendered tree is simply not packed, so an incomplete contract would otherwise
ship as a 200 with a truncated archive and a perfectly valid ETag. Two classes
of defect are checked once per snapshot, in `_contract_defect_reason` (pure —
it decides, it neither raises nor logs):

1. **The load-bearing member set** — `kit.json`, `layout.json`,
   `CONTRACT_VERSION`. The first two are the consumer's own identity pair
   (Cinna Desktop's `contractStore.isContractTree` is exactly "`kit.json` and
   `layout.json` at the root"), so an archive missing either is not a contract
   tree by the only definition that matters. The third is what the version
   fallback rests on. `schema/` and `templates/` are **not** in this set: a thin
   or empty schema/templates tree is a *content* problem and must not be dressed
   up as an identity failure.
2. **The three `contract_version` declarations must agree** —
   `CONTRACT_VERSION`, `kit.json`, `layout.json`. All three are named in the
   diagnostic, not just the disagreeing pair. An invariant test only catches
   drift if someone runs it before shipping; checking in the serving path turns
   a silent inconsistency into a loud one at the one moment anyone can act on
   it.

The verdict is carried on the build as **data** (`_KitBuild.contract_defect`),
not raised from `_build_or_cached`, and that is the whole boundary:

- `_require_serviceable_contract` raises **503** and is called by
  `get_versioned_contract_tarball()`, `get_contract_version_payload()` and
  `get_contract_version()` — so `/contract.tar.gz` and `/contract/version`
  **degrade together**. Two representations of one thing reporting different
  health is the asymmetry the guard exists to remove, and `/contract/version` is
  the one a consumer polls: publishing an unvalidated number that a
  major-version gate keys on is worse than publishing nothing, because a false
  *compatibility* fails silently where a refusal at least stops.
- The kit surface — `START.md`, `/version`, `kit.tar.gz`, `/kit/{path}` — never
  calls it, and keeps serving whatever state the contract is in. Do not narrow
  the guard to one representation and do not widen it to the kit surface; both
  directions have been tried and ruled on.

The specific reason is logged once per snapshot at `ERROR` in
`_build_or_cached` (the per-request 503 carries only the surface's generic
detail). `_read_contract_version` decodes leniently with `errors="replace"` and
treats a `U+FFFD` as corruption — `_render_bytes` passes undecodable bytes
through untouched, and a corrupt member must land in the "build defect ⇒ 503"
mode, not in a 500.

### Caching (`_build_or_cached`, `_KitBuild`)

Process-local memo: `(cache_key, _KitBuild)` behind a `threading.Lock`,
double-checked inside the lock. `_KitBuild` is a `NamedTuple` — `version`,
`rendered`, `tarball`, `contract_tarball`, `contract_defect` — rather than a
bare tuple, because positional unpacking with a row of underscores is how a
later edit silently reads the contract tarball as the kit one. **Both tarballs
are packed and the contract verdict decided inside the one critical section
from the one rendered tree**, so they can never disagree about what the kit at
`kit_version` contains; a second cache could otherwise hold a contract built
from a different render than the kit it claims to belong to while reporting one
`kit_version` for both.

`cache_key` = `snapshot_cache_key(local_kit_dir())` — the **shared** helper in
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

**One packer for both archives**, parameterised by `root` and an optional
`include: Callable[[str], bool]` predicate: `None` packs everything
(`cinna-kit/`), `_is_contract_member` packs the contract (`cinna-contract/`).
Two packers would be two chances to reintroduce a wall-clock timestamp and
serve two different bodies under one strong ETag.

Members are added in sorted path order; **fixed mtime (`0`) on every member and
the gzip header itself**, so two workers building the same rendered tree produce
byte-identical tarballs — required for one strong ETag to mean what it promises.
File mode `0o755` for `*.py`, else `0o644`.

### `LocalAgentKitService` public surface

| Method | Returns |
|--------|---------|
| `is_enabled(session)` | `bool` — the `ServerConfig` flag |
| `get_version()` / `get_version_payload()` | content version / the `/version` JSON body (via `_version_payload`) |
| `get_contract_version()` / `get_contract_version_payload()` | `CONTRACT_VERSION` / the `/contract/version` body — the `/version` envelope plus `contract_version`, both halves from **one** build so a redeploy landing between two probes cannot pair one build's `kit_version` with another's `contract_version`. 503 on an incoherent contract. |
| `get_versioned_contract_tarball()` | `(kit_version, contract tarball)`. 503 on an incoherent contract. |
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
the user's chosen root (`~/Documents/CinnaAgents`, `Local/`, `Cloud/`,
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
| `new <slug> [--name N] [--description S] [--root DIR] [--json]` | Copies `templates/agent/` into `<root>/<agents_dir>/<slug>/` (`workshop_agents_dir()`, not a literal `Local`), restores dotted `.gitignore` files (`restore_scaffold_ignore_files`), then **one** `substitute_tokens` pass fills the whole tree — `NAME`, `SLUG`, `DESCRIPTION`, `ID` (a fresh UUID v4), `CREATED_AT` (UTC, second precision, `Z`), `CONTRACT_VERSION`, `KIT_VERSION`. Nothing is patched onto the parsed dict afterwards. Asserts the shipped template carries no `publications`/`cloud` ledger key (an error naming the *kit* as the defect), then re-writes through `write_manifest` — the sole manifest writer — so the scaffold shares one serialisation and the ledger migration is structural. `--json` prints only `{path, slug, id, contract_version}`. Refuses if the target folder already exists. |
| `validate <path> [--fix] [--json] [--cloud-ready]` | Manifest schema subset (`validate_manifest` → `_validate_identity` + the rest), required/expected files, secret hygiene (`_validate_secrets`), `workspace_requirements.txt` mirrors `pyproject.toml` deps (`--fix` regenerates), `CLI_COMMANDS.yaml` names valid + mirrored in the Makefile, scripts catalogued in `scripts/README.md`, `publications.json` shape. Every path read is guarded: an unreadable **directory** is an error and an immediate return (a "missing required file" that is really "unreadable directory" is a filter standing where an assertion belongs), and an unreadable **file** is caught per-check so the other checks still run — one error per failing check, not per file. `--cloud-ready` promotes readiness *advice* (empty `example_prompts`, an unedited scaffold `description`) to hard errors. `--json` also reports `contract_version` (the folder's) and `tool_contract_version` (this kit's), so a conformance harness sees both sides of the gate rather than inferring it from a message. Exit 1 on any error (warnings never fail a run). |
| `list [--root DIR]` | Table of the agents directory — `SLUG`, `NAME`, `RUNGS` (`_rungs_present`, inferred from the manifest and folder contents, never hand-tracked), `CLOUD` (`is_published`, the one reader of that question, shared with the `go_cloud` rung so the two cells cannot contradict), `DESKTOP` (`_desktop_connected`). Then every cinna-cli account workspace under `Cloud/` — `Cloud/<host>/` per instance, **and** the flat legacy `Cloud/` itself, both reported (`_cloud_workspaces`). Unreadability is a second return value, not an empty list: a directory that could not be probed is skipped, its siblings still listed, and a line says the listing may be short. |
| `refresh [--check]` | Reads `kit.json.kit_base_url`, `GET {base}/version`; if different from the local `VERSION`, downloads `{base}/kit.tar.gz` and atomically swaps `.cinna-kit/` (old tree kept as a timestamped backup only until the swap succeeds). Any network failure is a **warning, never a blocked session** — `refresh` always exits 0 except on a genuinely malformed archive. Refuses a non-`http(s)` `kit_base_url` outright. `_parse_remote_version` takes the key names to read (`("kit_version", "version")` by default), so a contract-version poll reuses the one parser instead of growing a second that drifts from it; `refresh` itself still polls `/version` only. |
| `export <path> --to DIR [--force] [--hash]` | Produces the exact tree `cinna agent import` pushes. Runs the `--cloud-ready` validation gate first and refuses on any error unless `--force`. Excludes come from **`layout.json` `cloud_import_excludes`** (see below), joined once with `ALWAYS_EXCLUDE` by `_with_always_excluded` so the walk that validates and the walk that exports can never combine them differently; the secret rules come from `layout.json` too. One walk of the **source** produces both the tree that travels and the hash over it. Regenerates `workspace_requirements.txt` in the destination, and **asserts** `cinna-agent.json` was copied — contract data a later version can change must not be able to produce a cheerful 0-error export of a tree no host can identify. **Export never writes the manifest**: the ledger lives in the excluded `publications.json`, so there is nothing to clear, and the exported manifest is byte-identical to the source. `--hash` prints the tree's `content_hash` bare on the final line for a conformance harness. |
| `chat <path> "<prompt>"` | Sends one prompt to an agent running under Cinna Desktop. Reads the desktop-owned state file named by `layout.json` (`desktop_state_file()`), `POST {api_base_url}{chat_path or "/chat"}` with `Authorization: Bearer <agent_token>` and body `{"prompt": …}`, and prints each NDJSON line's text as it arrives. **Never falls back to role-play**: not connected, connection refused, 401, any non-2xx ⇒ one line on stderr and a non-zero exit, and nothing that could be mistaken for the agent's answer. Nothing on any path prints the token, the URL, or the text of an exception raised while reading the file (`_network_reason`, not `str(exc)`). `_chat_url` refuses a non-`http(s)` base URL — `urllib` will happily open `file://`, and `Request()` quotes an unclassifiable URL back in its `ValueError`. |

**Provisional, and marked as such in the source.** The desktop has not built the
chat endpoint yet, so two things in `chat` are a seam rather than settled
contract: `chat_path` defaulting to `/chat`, and a line's text being the first
present of `CHAT_TEXT_KEYS` (`text`, then `content`, then `delta`). Both
collapse to the one shape the endpoint emits when it ships.

### Reading the contract (`layout.json`)

`kit.py` reads the folder model rather than knowing it. `layout_config()` never
raises — an unreadable contract degrades to `{}`, exactly as the desktop's
`parseLayout` degrades to an empty layout — and each accessor then decides its
own fallback direction:

| Accessor | Reads | On unevaluable |
|----------|-------|----------------|
| `workshop_kit_dir/agents_dir/cloud_dir()` | `workshop.{kit_dir,agents_dir,cloud_dir}` | falls back to `.cinna-kit` / `Local` / `Cloud` — guessing costs a visible empty listing, refusing costs every verb |
| `agent_command_catalog()`, `agent_status_file()` | `agent.{command_catalog,status_file}` | falls back to the built-in path |
| `scaffold_ignore_files()` | `scaffold_ignore_files.agent` | falls back **whole**, never a subset — a partly applied rename ships an agent with no `.gitignore` |
| `contract_exclude_patterns()` | `cloud_import_excludes` | **`None`** — the caller that emits a `content_hash` must refuse |
| `cloud_import_excludes()` | the same, for *what travels* | falls back to `DEFAULT_EXCLUDES` |
| `secret_file_rules()` | `secret_files.rules` | fails toward **treating the file as secret** |
| `desktop_state_file()` | `desktop_owned` | tristate: declared ⇒ that path; block **absent** ⇒ `app-data/desktop.json`; block present and unreadable ⇒ `None`, and `chat` refuses |
| `contract_version()` | `kit.json` `contract_version`, then the `CONTRACT_VERSION` file | `None` |

**`contract_exclude_patterns()` and `secret_file_rules()` fail in opposite
directions on purpose, and each carries a cross-reference note saying not to
harmonise them.** An unevaluable *exclude list* withholds the **hash** (a
plausible wrong drift number is untraceable; a missing one is merely visible);
an unevaluable *secret rule* withholds the **file** (a leaked credential is
unrecoverable). Met cold the pair reads as an inconsistency and is not one: the
safe direction is a property of the consequence, not a house style. The same
reasoning, derived rather than copied, makes `desktop_state_file()` refuse.

Each `DEFAULT_*` constant in `kit.py` (`DEFAULT_EXCLUDES`,
`DEFAULT_SCAFFOLD_IGNORE_FILES`, `DEFAULT_SECRET_FILE_RULES`,
`DEFAULT_KIT_DIR`/`AGENTS_DIR`/`CLOUD_DIR`, `DESKTOP_STATE_FILE`) is an offline
**fallback** that must stay content-identical to the shipped `layout.json`
block — same entries, same order. A fallback that diverges is a silent
hash-parity break: the two hosts hash different file sets and the drift
indicator never clears.

### Contract 1.1.0 — the `skills` role

The contract's `agent` member list gained
`{path: "skills", kind: "directory", role: "skills", survives_update: true}`,
and the `docs` role text dropped "one doc per local skill". A skill is
`skills/<name>/SKILL.md` shaped like the open Agent Skills standard; see
[agent_skills](../../agents/agent_skills/agent_skills.md) and its
[tech reference](../../agents/agent_skills/agent_skills_tech.md).

- **Additive, so a minor bump.** Majors match, so a tool built against `1.0.0`
  still operates a `1.1.0` folder and no existing agent folder needs a change.
  All three declarations move together — `CONTRACT_VERSION`, `kit.json` and
  `layout.json` — or both contract representations 503.
- **`skills/` is not in `cloud_import_excludes`**: it is part of what the agent
  *is*, so it travels to the cloud like `docs/` and `scripts/`.
- **`_rungs_present`** now adopts the "Knowledge & local skills" rung on
  authored files under `knowledge/` **or** `skills/`, each folder's scaffolded
  `README.md` excluded. Checking only `knowledge/` would tell an author who went
  all-in on skills that they had not climbed a rung they had, and the ladder
  check would then send them back to the guide they followed.
- **The scaffold gains `templates/agent/skills/README.md`**, picked up by the
  contract tarball automatically (`templates/**` is a declared member subset).
- **`templates/agent/AGENTS.md` carries an explicit read-the-`SKILL.md` rule.**
  Nothing on the user's machine registers `skills/` with their coding assistant,
  so locally the progressive disclosure the folder exists for is an
  *instruction*, not machinery. Do not document local auto-discovery.
- **`docs/skill_<name>.md` is legacy, not removed** — still shipped, still <!-- nocheck -->
  imported, still read when the workflow prompt points at it. Guide 08 carries a
  five-step per-capability migration, deliberately not a sweep: a capability
  living in both places is two sources of truth.

### Exclude-pattern semantics (`matches_pattern`, `is_excluded`)

A port of the desktop's `matchesPattern` (`src/main/kit/layout.ts`), and it has
to stay one: both hosts hash the file set this function selects, and a
difference of a single file makes the two hashes disagree forever while every
individual step still looks like it worked. `normalize_rel_path` (POSIX,
root-relative, no `./`, no leading/trailing `/`) is applied to both the pattern
and the path first.

- trailing `/` — the directory itself and everything beneath it
- `*` — any run of characters inside one segment
- `?` — one character inside one segment
- `**` — zero or more whole segments
- no leading `**` ⇒ **anchored at the agent root**, and the pattern must consume
  the whole path: `README.md` drops the agent's own README and never
  `docs/README.md` or `scripts/README.md`

Two portability details are load-bearing rather than fussy. Segment regexes are
compiled unanchored and used with **`fullmatch`**, not `^…$` with `match`:
Python's `$` also matches just before a trailing newline and JavaScript's does
not, so `README.md` would drop a file literally named `README.md\n` here and
keep it there. And both pattern and path segment go through `_to_code_units`
before matching, because JavaScript indexes by UTF-16 code unit — its `[^/]`
(what `?` compiles to) consumes half a non-BMP character where Python's
consumes a whole code point.

`is_excluded(relative, patterns)` is `any(matches_pattern(p, relative) …)` and
is asked about **directories before descending**, so an excluded directory is
never opened. `ALWAYS_EXCLUDE` (`credentials/`, `.git/`, `.venv/`, `**/.env`,
`**/*.env`, `**/credentials.json`) applies even if `layout.json` says otherwise;
every one of its patterns is already covered by the shipped
`cloud_import_excludes`, so the append is provably a no-op against an untampered
contract — keep it that way, since a pattern that *added* something would make
the exported tree a different set from the hashed one.

`is_env_filename` is the **wide** reading (`.env`, `*.env`, `.env.*`, including
`.env.example`) and is used by `validate` only: an `.env.example` outside
`credentials/` is misplaced whether or not it holds a value. What may *travel*
is a different question, answered by the contract's `secret_files` rules
through `is_secret_filename` — which exempts `.example`, `.sample` and
`.template` suffixes.

### `content_hash` (`hash_export_files`, `content_hash`)

The desktop's `hashExportFiles`, byte-for-byte. One line per file,
`<relpath>` NUL `<sha256 hex of the bytes>` LF, in sorted order, fed into one
running SHA-256; result `sha256:<hex>`. No mtimes, sizes, modes or directory
entries. Symlinks are never followed and never listed; excluded directories are
never descended into.

- **Sorted by `path.encode("utf-16-be", "surrogatepass")`**, not by Python's
  default ordering. The desktop sorts with `Array.prototype.sort` (UTF-16
  code-unit order); plain `sorted()` agrees for ASCII and diverges for non-BMP
  characters, so an emoji in a filename would reorder two lines and change the
  digest — whose only symptom is a host reporting "unpublished changes"
  forever.
- **`UNREADABLE_MARKER` is `"\0unreadable"`** — with a leading NUL, so the
  emitted line carries two. That matches the desktop's `exportTree.ts`; do not
  tidy the leading NUL away.
- **`hash_export_files` returns `(digest, unreadable)`; `content_hash` refuses
  when that list is non-empty.** A digest folding in the unreadable marker is
  stable and comparable but no longer describes the bytes that would be
  uploaded — recorded on a publication it reads "up to date" forever. The
  refusal is structural, not advice in a docstring: the previous version
  returned the digest and told callers in prose to use the primitive instead,
  and the reader who most needs that warning is exactly the one who will not go
  looking for it.
- `cmd_export` refuses the same way, at two levels of one walk (an unscannable
  directory, and an unreadable file), before writing anything — and `--force`
  waives neither: it waives validation findings, not a tree this host could not
  read.
- Known parity limit: for a filename that is not valid UTF-8 the two hosts hash
  *different strings* (Python decodes with `surrogateescape`, Node with lossy
  `U+FFFD`). `surrogatepass` only guarantees this side stays deterministic.

### The compatibility gate (`parse_semver`, `check_contract_compatibility`, `_validate_identity`)

`SEMVER_RE` and `UUID_RE` are character-identical to the schema's and to the
desktop's `src/shared/kit/manifest.ts` / `contractVersion.ts`, so no two hosts
can disagree about what a well-formed value looks like.
`check_contract_compatibility(agent_version, tool_version)` returns
`(status, reason)` with `status ∈ {ok, app_too_old, migratable, unknown}` and a
reason a UI can show as written. **Only the major version decides** — the
contract's minor releases are additive by definition. `_validate_identity` maps
the verdict onto severities: `app_too_old` ⇒ error, `migratable` ⇒ warning,
`unknown` ⇒ info.

One deliberate divergence from the desktop: they report an unusable *tool*
version through `manifest.contract_version.invalid`, which blames the folder for
the host's problem. Here the folder parsed and the kit is what cannot answer, so
it is reported as information about the kit.

**The one tolerated identity absence:** a manifest with `schema_version` and
neither `contract_version` nor `id` is a pre-1.0.0 folder — warned, asked to be
re-stamped, and the rest of the identity checks skipped (mirroring the early
return in the desktop's `checkIdentity()`; without it a legacy folder would
collect two "required" errors for the very keys the branch exists to excuse).
This is why `contract_version` and `id` are **not** in the schema's top-level
`required`.

### Safe tar extraction (`safe_extract`, used by `refresh`)

Rejects absolute member paths, `..` segments, any member resolving outside the
destination, and every non-regular-file/directory member type (symlink,
hardlink, device, fifo) — `tarfile`'s own default extraction behaviour is not
relied on. Strips setuid/setgid/sticky and group/other write bits from every
member's mode.

### Manifest validation subset

`validate_manifest` runs `_validate_identity` (above) first, then checks
`name`/`description`/`slug` shape, `prompts{}` keys, `example_prompts[]`
non-empty strings, `runtime` (`_validate_runtime` — `credential` is a
*reference*, so a value matching `SECRET_LOOKALIKE_RE` — `sk-`, `sk_`, `ghp_`,
`gho_`, `xox[baprs]-`, `AIza`, `AKIA` — or longer than 200 characters is an
error with a rotate-it message), `credentials[]`, `schedules[]` (`cron_string`
shape, `schedule_type` ∈ `static_prompt` | `script_trigger`, `prompt` required
for the former / `command` for the latter), `handovers[].target_slug` shape,
the legacy ledger keys (`_validate_manifest_ledger_keys`), and — in its own
file — `publications.json` (`_validate_publications`).

Three severity choices are worth naming because they are contract decisions,
not style:

- **An unrecognised `credentials[].type` is a warning**, and the schema agrees:
  `type` is `"string"` with the twelve current values as `examples`, **not a
  closed `enum`**. A closed enum inside a contract that is pinned, bundled and
  carried offline retroactively invalidates every folder using the first type
  the platform adds, on a machine that cannot learn about it. `CREDENTIAL_TYPES`
  in `kit.py` is what the kit knows at contract 1.0.0; the authoritative list
  lives at import.
- **A credential slot carrying a `value`/`secret`/`token`/`password`/`api_key`/…
  key is an error**, and this is the one place `kit.py` is deliberately stricter
  than the desktop's validator, which has no equivalent check. Demoting a
  secret-leak guard to buy severity parity is the wrong trade: the cost of the
  false positive is an edit, the cost of the false negative is a published key.
- **A catalogued command with no Makefile target is a warning**, and command
  names have two thresholds: `COMMAND_NAME_RE` (what the desktop can turn into a
  `/run:` command — broken everywhere, so an error) and the stricter house-style
  `COMMAND_NAME_CONVENTION_RE` (runnable but unconventional, so a warning).
  Making the stricter one the error was a false-positive generator — `kit.py`
  refused folders the desktop runs happily.

This is a **pragmatic stdlib-only subset** of
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

*Last updated: 2026-09-03*
