# Local Agent Kit — auth-free local-first agent building with a coding assistant

**Feature name:** `local-agent-kit`
**Status:** Draft — implementation blueprint (2026-09-01)
**Owner decisions taken (2026-09-01):** canonical `/agent-start` + `/api/agent-start` alias · local layout mirrors the cloud workspace · in scope: kit helper script, cinna-cli `agent import`, admin opt-out toggle, human-facing surfaces · kit source of truth lives in `docs/local_agent_kit/` and is synced into the knowledge template.

---

## 1. Overview

A user with any local coding assistant (Claude Code, Codex, opencode, …) and **no Cinna account** pastes one prompt — `read https://<instance>/agent-start and help me start making my agents` — and the assistant receives a versioned, platform-maintained *kit*: how to set up `~/Documents/MyAgents/{Local,Cloud}`, how to build local agents whose folder layout and metadata are byte-compatible with a Cinna cloud agent workspace, a *capability ladder* that reveals credentials / schedules / status / CLI commands / knowledge / multi-agent only when the task needs them, and a go-cloud playbook that turns the `Cloud/` folder into a cinna-cli account workspace and imports a local agent with one verb.

Core capabilities:

- **Public, unauthenticated, read-only entrypoint** `GET /agent-start` (markdown for assistants, HTML landing for browsers) + `/api/agent-start` alias that works on every deployment; kit files, tarball, and a content version for refresh.
- **Kit content set** authored in `docs/local_agent_kit/`, synced into the `platform-knowledge-env` snapshot (same pipeline as feature docs), rendered with instance placeholders, hashed into a content version.
- **Cloud-mirroring local agent layout** + `cinna-agent.json` manifest carrying the same definitional metadata a bundle revision carries (description, example_prompts, router trigger, prompts, credential specs, schedules, status command).
- **Gradual discovery**: `README.md` capability ladder with explicit trigger rules and per-rung guides; the assistant re-runs the ladder check after every substantive change.
- **`kit.py`** stdlib-only helper: `new`, `validate`, `list`, `refresh`, `export` — deterministic scaffolding and cloud-readiness validation for any assistant.
- **Go-cloud**: `cinna login <host> --dir Cloud` (existing) + new `cinna agent import Local/<slug>` (create → metadata → sync → copy → push → credential drafts → schedules → status command → verify hint).
- **Freshness**: `context/VERSION`-style content hash, `GET /api/agent-start/version`, `kit.py refresh`; the kit also ships inside the account context package (`context/local-kit/`) so cloud-side orchestrators know the local conventions.
- **Instance control**: `ServerConfig.local_agent_kit_enabled` (default on) — disabled instances return 404 on the whole surface.

High-level flow:

```
 user ──"read https://host/agent-start …"──▶ coding assistant
                                          │ GET /agent-start  (markdown)                 ┌──────────────────────────────┐
                                          ├───────────────────────────────────────▶│ backend: LocalAgentKitService │
                                          │ curl /api/agent-start/kit.tar.gz | tar xz    │  snapshot knowledge/local-kit │
                                          ◀───────────────────────────────────────┤  rendered + hashed + cached   │
   ~/Documents/MyAgents/                  │                                        └──────────────────────────────┘
     AGENTS.md, CLAUDE.md  ◀── templates ─┤
     .cinna-kit/           ◀── tarball ───┤
     Local/<slug>/         ◀── kit.py new ┤   … build, ladder check, validate, test as agent …
     Cloud/                               │
       (cinna login host --dir Cloud)  ───┼──▶ device-flow login (needs an account: signup link in guide)
       cinna agent import Local/<slug> ───┼──▶ create · prompts/metadata · sync · copy · push · cred drafts · schedules
                                          └──▶ cinna chat --agent <slug> "<example prompt>"   (verify)
```

---

## 2. Architecture Overview

### Components

| Component | Location | Role |
|-----------|----------|------|
| Kit source (SSOT) | `docs/local_agent_kit/` | Hand-authored markdown, JSON schema, scaffold templates, `tools/kit.py`. Maintained with feature docs. |
| Sync step | `.cinna-core-kit/scripts/sync_platform_knowledge.py` (+ `make sync-platform-knowledge`) | Copies `docs/local_agent_kit/` → `backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit/` (rmtree of that subtree only; `knowledge/platform/` and `knowledge/guides/` untouched). |
| Asset locator | `backend/app/services/cli/platform_knowledge_assets.py` | New `local_kit_dir()` next to `platform_knowledge_dir()`, `guides_dir()`, `example_scripts_dir()`. |
| Service | `backend/app/services/cli/local_agent_kit_service.py` | Loads snapshot, renders placeholders, builds in-memory rendered tree + tarball, computes content version, caches (mtime+count key, like `ContextPackageService`). |
| Public router | `backend/app/api/routes/local_agent_kit.py` | `start_router`, included twice in `backend/app/main.py` (`/agent-start`, `/api/agent-start`), root-level like the `.well-known` routers, `include_in_schema=False`. |
| Rate limiting | `backend/app/services/common/rate_limiter.py` | Existing `RateLimiter.check(key, limit_per_min)`, keyed by client IP. |
| Instance toggle | `ServerConfig.local_agent_kit_enabled` | Migration + admin UI switch (Server Configuration → Interface). |
| Context package | `ContextPackageService` | Adds `context/local-kit/` (4th source) so account workspaces carry the local conventions. |
| Frontend | Getting Started article, login-page link, admin toggle card, LocalDevelopmentCard hint | Human-facing surfaces. |
| cinna-cli (separate repo `/Users/evgenyl/dev/ml-llm/cinna-cli`) | `src/cinna/account.py`, `src/cinna/main.py`, new `src/cinna/local_import.py` | `cinna agent import <path>` + `ACCOUNT_CLAUDE.md.template` mention of `context/local-kit/`. |

### Data flow

1. Author edits `docs/local_agent_kit/**` → `make sync-platform-knowledge` → snapshot committed under the knowledge template → shipped in the backend image (the only copy available at runtime, exactly like feature docs and guides).
2. Request `GET /agent-start` → enabled check (`ServerConfigService.get_or_create`) → rate limit → `LocalAgentKitService.get_rendered_file("START.md")` (cache hit after first build) → markdown or HTML (content negotiation) with `ETag`, `X-Kit-Version`, `Cache-Control`.
3. Assistant downloads `GET /api/agent-start/kit.tar.gz` → extracts to `~/Documents/MyAgents/.cinna-kit/` → writes root `AGENTS.md`/`CLAUDE.md` from `templates/root/`.
4. `kit.py refresh` → `GET /api/agent-start/version` → compare with `.cinna-kit/VERSION` → re-download when different.
5. Go-cloud → `cinna login <host> --dir Cloud` (existing device flow) → `cinna agent import Local/<slug>` (new) → existing account-CLI endpoints only; no new authenticated backend endpoint is required.

### Integration points with existing systems

- **Account CLI workspace** (`docs/application/cinna_cli_integration/account_cli_workspace.md`): `Cloud/` *is* an account workspace created by `cinna login … --dir Cloud`; import reuses `run_agent_create`, `run_agent_sync`, credential drafting, schedule CRUD, status set-command, and the bulk prompt write documented in the authoring-agent-prompts guide.
- **Context package** (`ContextPackageService`): adds `local-kit/`; content version now folds in the kit.
- **Agent bundles** (`docs/agents/agent_bundles/agent_bundles_tech.md`, manifest format): `cinna-agent.json` mirrors the manifest `metadata` / `required_credential_specs` / `schedules` / `prompts` blocks so a local agent has exactly the definitional data a revision carries.
- **Agent prompts** (`docs/agents/agent_prompts/agent_prompts.md`): local `docs/WORKFLOW_PROMPT.md`, `ENTRYPOINT_PROMPT.md`, `REFINER_PROMPT.md` are the *same files* the cloud reconciles into `Agent.workflow_prompt` etc.
- **CLI commands** (`docs/agents/cli_commands/cli_commands.md`): local `docs/CLI_COMMANDS.yaml` is the same file; kit rule: commands are cloud-first (`python scripts/x.py`, relative paths, cwd = workspace root), Makefile targets mirror them with `uv run`.
- **Agent status tracking**: local `app-data/storage/STATUS.md` + scaffolded `scripts/update_status.py` are the same convention.
- **Getting Started** (`docs/application/getting_started/getting_started.md`): new article.
- **Server configuration** (`docs/application/server_configuration/`): new toggle.
- **Nginx** (`docs/infrastructure/nginx_setup.md`, `frontend/nginx.conf`): new `location /agent-start` block.

---

## 3. Data Models

### 3.1 `server_config` (modify existing singleton)

| Field | Type | Constraint | Purpose |
|-------|------|-----------|---------|
| `local_agent_kit_enabled` | `bool` | NOT NULL, server_default `true` | Instance-level opt-out of the public `/agent-start` surface. |

No other tables. The feature is otherwise stateless server-side (static content). No user data is read or written by the public surface.

### 3.2 `ServerConfigUpdate` (schema)

Add `local_agent_kit_enabled: bool | None = None`. `ServerConfigService.update` must **not** bump `disclaimer_version` when only this flag changes (check the existing update path: it bumps on disclaimer content/mode changes only — keep it that way).

### 3.3 Kit manifest on disk (not a DB model) — `cinna-agent.json`

Lives at the **agent folder root** (bundle-owned; travels into the cloud workspace and into bundle revisions unchanged). Validated by `docs/local_agent_kit/schema/cinna-agent.schema.json` (JSON Schema draft 2020-12; `kit.py` validates a pragmatic subset with stdlib — required keys, types, enum values, cron shape — and prints the schema path for full validation).

```json
{
  "schema_version": 1,
  "kit_version": "3f9c1e2a7b4d5e60",
  "name": "Invoice Watcher",
  "slug": "invoice-watcher",
  "description": "Watches the billing inbox and flags invoices missing a PO number.",
  "example_prompts": ["check invoices from last week", "list invoices without PO"],
  "router_trigger_prompt": "Checks incoming invoices and flags missing purchase-order numbers.",
  "prompts": {
    "workflow": "docs/WORKFLOW_PROMPT.md",
    "entrypoint": "docs/ENTRYPOINT_PROMPT.md",
    "refiner": "docs/REFINER_PROMPT.md"
  },
  "status_refresh_command": "/run:status",
  "credentials": [
    {
      "name": "billing-inbox",
      "type": "email_imap",
      "description": "IMAP access to billing@…",
      "env_prefix": "BILLING_INBOX_",
      "fields": ["host", "port", "login", "password", "is_ssl"]
    }
  ],
  "schedules": [
    {
      "name": "Weekday morning check",
      "cron_string": "0 6 * * 1-5",
      "timezone": "Europe/Berlin",
      "schedule_type": "static_prompt",
      "prompt": "Check the invoices that arrived since yesterday.",
      "command": null,
      "enabled": true
    }
  ],
  "handovers": [],
  "features": { "webapp": false, "agent_api": false },
  "cloud": { "platform_url": null, "agent_id": null, "imported_at": null }
}
```

Rules:
- `slug`: `^[a-z0-9][a-z0-9-]{1,62}$`, equals the folder name; `name` ≤ 255 chars; `description` one sentence; `example_prompts` ≥ 1 for a cloud-ready agent (routing input, see authoring guide).
- `credentials[].type` values are the platform `CredentialType` names (`email_imap`, `email_smtp`, `odoo`, `api_token`, `google_service_account`, `gmail_oauth*`, `gdrive_oauth*`, `gcalendar_oauth*`, `ssh_key`); OAuth types are flagged "cloud-only, fill in the UI" by the credentials guide. `env_prefix` names the local `.env` variables (`<PREFIX><FIELD upper>`).
- `schedules[]` mirror `manifest.schedules` (`schedule_type` ∈ `static_prompt` | `script`; `command` required for `script`). `timezone` is local-only metadata used to render the cron description at import.
- `cloud` block is written only by `cinna agent import` (and cleared by `kit.py` when copying an agent).
- Unknown keys are preserved (forward compatibility); `schema_version` gates parsing.

### 3.4 `kit.json` (served index, rendered)

```json
{
  "kit_version": "…", "schema_version": 1, "platform_url": "https://…",
  "kit_base_url": "https://…/api/agent-start", "cli": {"install_spec": "cinna-cli", "min_version": "0.2.3"},
  "entry": "START.md",
  "ladder": [
    {"rung": "credentials", "doc": "guides/04-credentials.md", "trigger": "workflow calls any external system that needs a token, login or key"},
    …
  ],
  "cloud_import": {"exclude": ["credentials/", ".venv/", ".claude/", "AGENTS.md", "CLAUDE.md", "app-data/", "temp/", "__pycache__/", "*.pyc", ".git/", ".DS_Store"]}
}
```

---

## 4. Security Architecture

- **Unauthenticated by design, read-only, static.** The surface serves only rendered snapshot files. No DB reads except the `ServerConfig` enabled flag; no user, agent, credential, or session data is ever touched. No POST/PUT/DELETE.
- **Path safety.** `GET /agent-start/kit/{path:path}`: normalize, reject absolute paths, `..` segments, empty segments, NUL bytes, and any resolved path outside `local_kit_dir()`; refuse symlinks (`Path.is_symlink()` on every component). Serve only from the **in-memory rendered tree** (dict keyed by relative POSIX path) — the request never touches the filesystem, which makes traversal impossible by construction. Unknown path → 404.
- **No request reflection.** Placeholders come from settings only (`FRONTEND_HOST`, `backend_base_url`, `PROJECT_NAME`, `MINIMUM_CLI_VERSION`, new `CINNA_CLI_INSTALL_SPEC`). The `Host` header is never used (host-header injection would otherwise poison the `cinna login` target in the go-cloud guide).
- **HTML landing.** All interpolated text HTML-escaped (`html.escape`); no inline event handlers except a single copy-button script with no dynamic content; `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'` on that response only; `X-Content-Type-Options: nosniff` on all responses.
- **Rate limiting.** Shared `RateLimiter` keyed by client IP (same IP-derivation helper server_channels uses; first `X-Forwarded-For` hop only when behind the known proxy, else `request.client.host`), limit `settings.LOCAL_AGENT_KIT_RATE_LIMIT_PER_MIN` (default 120). 429 with `Retry-After`. Cached responses make the endpoint cheap; the limiter is a backstop against tarball hammering.
- **Caching headers.** `ETag: "<kit_version>"`, `If-None-Match` → 304, `Cache-Control: public, max-age=300`. Responses are identical for every caller, so intermediaries may cache.
- **CORS.** `Access-Control-Allow-Origin: *` on GET responses (public static content; browser-hosted assistants may fetch it). No credentials, no other methods.
- **Instance opt-out.** When `local_agent_kit_enabled` is false every path (both mounts) returns 404 (not 403 — do not advertise the feature's existence).
- **Secrets discipline in content.** The kit repeatedly instructs assistants never to print `.env`/credential values, mirrors BUILDING_AGENT.md rules, and `kit.py validate` fails if `credentials/.env` is git-tracked (checks `.gitignore` coverage) or if a `*.env` file exists outside `credentials/`.
- **Tarball size cap.** Build refuses (503, logged) if the rendered tree exceeds 5 MiB — the kit is text; a bloated snapshot is a build defect, not something to serve.
- **Failure mode.** Missing `knowledge/local-kit/` snapshot → 503 (fail loud, same discipline as `ContextPackageService._build_tarball`), never an empty 200.

---

## 5. Backend Implementation

### 5.1 Settings (`backend/app/core/config.py`)

| Setting | Default | Purpose |
|---------|---------|---------|
| `LOCAL_AGENT_KIT_RATE_LIMIT_PER_MIN` | `120` | Per-IP backstop. |
| `CINNA_CLI_INSTALL_SPEC` | `"cinna-cli"` | Rendered into the go-cloud guide (`uv tool install {{CLI_INSTALL_SPEC}}`); dev instances set `git+https://…` or `-e /path`. Also reuse in `CLIService.render_bootstrap_script` install hints (optional, one-line change). |

Reused: `FRONTEND_HOST`, `backend_base_url`, `PROJECT_NAME`, `MINIMUM_CLI_VERSION`.

### 5.2 Asset locator (`platform_knowledge_assets.py`)

- `LOCAL_KIT_SUBDIR = "local-kit"`; `def local_kit_dir() -> Path: return _knowledge_workspace_dir() / "knowledge" / LOCAL_KIT_SUBDIR`.
- Docstring note: synced by `sync_platform_knowledge.py` from `docs/local_agent_kit/` (unlike `guides/`, which is hand-authored in place).

### 5.3 Sync script (`.cinna-core-kit/scripts/sync_platform_knowledge.py`)

- New `KIT_SOURCE = PROJECT_ROOT / "docs" / "local_agent_kit"`, `KIT_TARGET = …/knowledge/local-kit`.
- New step `[3/3] Syncing local agent kit`: rmtree `KIT_TARGET` only, copy the whole tree (all file types, including dotfiles and `tools/kit.py`), skip `__pycache__`, print count. Fail loud if the source is missing.
- `check_docs_references.py`: kit docs reference kit-relative paths (`guides/…`, `templates/agent/docs/WORKFLOW_PROMPT.md`) — add `docs/local_agent_kit/` to the placeholder-tolerant set or annotate kit files with `<!-- nocheck -->` where they mention user-machine paths (`~/Documents/MyAgents/…`). Prefer a scanner rule: paths starting with `~/`, `Local/`, `Cloud/`, `.cinna-kit/` are skipped.

### 5.4 Service (`backend/app/services/cli/local_agent_kit_service.py`)

```
class LocalAgentKitService:
    _cache: tuple[cache_key, kit_version, rendered: dict[str, bytes], tarball: bytes] | None
    _lock = threading.Lock()

    placeholders() -> dict[str, str]
        PLATFORM_URL, KIT_BASE_URL (= backend_base_url + "/api/agent-start"), START_URL (= FRONTEND_HOST + "/agent-start"),
        INSTANCE_NAME, KIT_VERSION (filled after hashing), CLI_INSTALL_SPEC, MIN_CLI_VERSION,
        SIGNUP_URL (= FRONTEND_HOST + "/signup"), LOGIN_URL
    _build_or_cached() -> (kit_version, rendered, tarball)
        cache key = newest mtime + file count of local_kit_dir() (reuse ContextPackageService._snapshot_version — extract to a shared helper `snapshot_cache_key(*dirs)` in platform_knowledge_assets and make ContextPackageService call it too)
        1. read every regular file under local_kit_dir() (skip symlinks, __pycache__)
        2. render placeholders in text files (extensions: .md .json .yaml .yml .txt .py .toml .example .gitignore, and dotfiles) — `{{NAME}}` tokens from the fixed dict only; unknown tokens left verbatim
        3. kit_version = sha256 over sorted (path, rendered bytes) with KIT_VERSION rendered as the literal "{{KIT_VERSION}}" during hashing, then substituted afterwards (so the hash does not depend on itself)
        4. write VERSION member; render kit.json (index) with version + ladder from the source kit.json
        5. build gzip tarball rooted at `cinna-kit/`
    get_file(rel_path) -> (bytes, media_type) | None
    get_start_markdown() -> str            # rendered START.md
    get_start_html() -> str                # landing page embedding START.md
    get_tarball() -> bytes
    get_version() -> str
    is_enabled(session) -> bool            # ServerConfigService.get_or_create(session).local_agent_kit_enabled
    media_type_for(rel_path) -> str        # .md → text/markdown; .json → application/json; .py/.txt/.yaml/.example/dotfiles → text/plain; default application/octet-stream
```

Rendering is deliberately dumb string substitution — no Jinja (the kit contains fenced code with braces).

### 5.5 Routes (`backend/app/api/routes/local_agent_kit.py`)

`start_router = APIRouter(tags=["local-agent-kit"], include_in_schema=False)`; a router-level dependency `_public_kit_guard(request, session)` performs: enabled check (404), rate limit (429), and sets a `request.state.kit_version`.

| Method | Path (relative to mount) | Response | Notes |
|--------|--------------------------|----------|-------|
| GET | `` (mount root) | `text/markdown` START.md, or `text/html` landing | Negotiation: `?format=md|html` wins; else `text/html` explicitly present in `Accept` with a higher q than `text/markdown`/`text/plain` → HTML; else markdown. Both variants contain the complete START.md text, so a mis-negotiation never hides instructions. |
| GET | `/START.md` | `text/markdown` | Always raw. |
| GET | `/version` | `{"kit_version", "schema_version", "platform_url", "kit_base_url", "cli": {"install_spec", "min_version"}}` | JSON, cheap (cached). |
| GET | `/kit.json` | index | Same as `/kit/kit.json`. |
| GET | `/kit.tar.gz` | `application/tar+gzip`, `Content-Disposition: attachment; filename="cinna-kit.tar.gz"`, `X-Kit-Version` | Mirrors the context package download. |
| GET | `/kit/{path:path}` | file | From the rendered tree only; 404 otherwise. |

All responses: `ETag`, `Cache-Control: public, max-age=300`, `X-Kit-Version`, `Access-Control-Allow-Origin: *`, `X-Content-Type-Options: nosniff`; `If-None-Match` → 304.

Mounting in `backend/app/main.py` next to the `.well-known` routers:

```
app.include_router(start_router, prefix="/agent-start")       # canonical (needs proxy block)
app.include_router(start_router, prefix="/api/agent-start")   # alias, proxied by the universal /api/ block
```

Mount order: before any catch-all; `/api/agent-start` must not be shadowed by the `/api/v1` API router (different prefix — verify with a test that hits both).

### 5.6 Server config

- Model + `ServerConfigUpdate` field (§3).
- `ServerConfigService.update`: copy the flag when present; no disclaimer version bump.
- Existing admin routes `GET/PUT /admin/server-config` already return the full row → the new field is exposed automatically. `GET /server-config/disclaimer` unchanged.

### 5.7 Context package (`ContextPackageService`)

- Add `local_kit_dir()` as a 4th source: packaged under `context/local-kit/` (rendered through `LocalAgentKitService` so placeholders are resolved — reuse `_build_or_cached()` output rather than raw files).
- Fold into `_content_version` (label `local-kit`) and `_snapshot_version`; tolerate absence with a warning (like `examples/`), since the kit is helpful, not core, to a cloud orchestrator.
- `_render_index()` gets a bullet: `local-kit/` — conventions for agents built locally with a coding assistant; read `local-kit/guides/11-go-cloud.md` when importing one.

### 5.8 Nginx

- `frontend/nginx.conf`: add `location /agent-start { proxy_pass $upstream; … same headers … }` (prefix match covers `/agent-start`, `/agent-start/…`; no SPA route starts with `/agent-start`, and the router's `redirect_slashes` behaviour must be checked so `/agent-start/` and `/agent-start` both resolve).
- `docs/infrastructure/nginx_setup.md`: new section `/agent-start` (feature: Local Agent Kit; present in both the production proxy and the frontend container nginx). Mention the `/api/agent-start` alias as the fallback when a proxy cannot be changed.
- Vite dev: nothing (dev uses `http://localhost:8000/agent-start` directly; the Getting Started article computes the URL from `VITE_API_URL` when it is set, else `window.location.origin`).

### 5.9 Background tasks

None. Building is lazy, in-process, cached, and invalidated by the snapshot mtime (a deploy artifact).

---

## 6. Kit Content (the product)

Directory `docs/local_agent_kit/` — everything below is served verbatim (rendered) at `/api/agent-start/kit/<path>` and inside the tarball under `cinna-kit/`.

```
docs/local_agent_kit/
├── START.md                         # the entry document (see 6.1)
├── README.md                        # kit index + capability ladder (see 6.2)
├── kit.json                         # machine-readable index (ladder, exclude list, cli spec)
├── VERSION                          # placeholder file, replaced by the rendered hash
├── guides/
│   ├── 01-first-agent.md            # interview → scaffold → build loop → test-as-agent → validate
│   ├── 02-prompts-and-description.md# workflow/entrypoint/refiner/description/example_prompts/router trigger (distilled from knowledge/guides/authoring-agent-prompts.md; same six-field table)
│   ├── 03-scripts-and-data.md       # one-step scripts, uv, config/, app-data/storage vs cache vs files/, pagination sort order, cache freshness (from HOW_TO_WRITE_AN_AGENT.md)
│   ├── 04-credentials.md            # ladder rung: .env + credentials/README.md + cinna_credentials.py shim + manifest specs + platform type table
│   ├── 05-schedules.md              # ladder rung: declare in manifest; local dry-run via Makefile; ENTRYPOINT_PROMPT rules
│   ├── 06-status-reporting.md       # ladder rung: STATUS.md frontmatter, update_status.py, status_refresh_command, `/run:status`
│   ├── 07-cli-commands.md           # ladder rung: docs/CLI_COMMANDS.yaml (cloud-first), Makefile parity
│   ├── 08-knowledge-and-local-skills.md # ladder rung: knowledge/ topics, local skill docs, scripts/ subfolders
│   ├── 09-multi-agent.md            # ladder rung: several agents, handover declarations, shared conventions, root orchestrator role
│   ├── 10-testing-locally.md        # role-switch testing, example_prompts as acceptance tests, kit.py validate
│   ├── 11-go-cloud.md               # THE migration playbook (see 6.4)
│   └── 12-keeping-up-to-date.md     # kit refresh + what changes between kit versions (CHANGELOG pointer)
├── assistants/
│   ├── claude-code.md               # CLAUDE.md ↔ AGENTS.md, .claude/settings.local.json permissions, AskUserQuestion habits
│   ├── codex.md                     # AGENTS.md, sandbox/network caveats (download the tarball once; work offline)
│   └── other.md                     # generic: read AGENTS.md first; no tool assumptions
├── schema/
│   └── cinna-agent.schema.json
├── templates/
│   ├── root/
│   │   ├── AGENTS.md                # ~/Documents/MyAgents/AGENTS.md — orchestrator/builder role (see 6.3)
│   │   ├── CLAUDE.md                # "@AGENTS.md" + Claude-specific notes
│   │   ├── .gitignore               # **/credentials/.env, **/.venv/, **/app-data/, .cinna-kit/ (kit is re-downloadable)
│   │   └── README.md                # human-facing: what this folder is
│   └── agent/                       # scaffold copied by `kit.py new` (see 6.5)
├── tools/
│   └── kit.py                       # stdlib-only helper (see 6.6)
└── CHANGELOG.md                     # human-maintained; bump when conventions change
```

### 6.1 `START.md` — what the assistant reads first

Written *to the assistant*, second person, ~120 lines, no marketing. Sections:

1. **Who you are now**: "You are helping the user build AI agents that run locally today and can move to the {{INSTANCE_NAME}} cloud ({{PLATFORM_URL}}) later. No account is needed for anything in this document."
2. **One-time setup** (idempotent; skip steps already done):
   - choose the root (default `~/Documents/MyAgents`, confirm with the user),
   - `mkdir -p Local Cloud`,
   - download the kit: `curl -sL {{KIT_BASE_URL}}/kit.tar.gz | tar xz -C <root>` → `<root>/cinna-kit/` renamed/moved to `<root>/.cinna-kit/` (tarball root is `cinna-kit/` for clarity when extracted elsewhere; `kit.py` handles the move),
   - copy `templates/root/*` to the root (never overwrite an existing `AGENTS.md`/`CLAUDE.md`; append a pointer instead),
   - `git init` the root (recommended, optional),
   - no network? fall back to reading docs by URL: `{{KIT_BASE_URL}}/kit/<path>`.
3. **How to work from now on**: read `<root>/AGENTS.md` (the orchestrator role) and `.cinna-kit/README.md` (index + ladder). Start the first agent via `guides/01-first-agent.md`.
4. **The three roles** the assistant switches between: *Orchestrator* (root; manages many agents), *Builder* (inside `Local/<slug>`; changes the agent), *Agent* (inside `Local/<slug>`; acts as the agent following `docs/WORKFLOW_PROMPT.md`). Rule of thumb for switching.
5. **Non-negotiables**: never print secrets; cloud-mirroring layout; keep `cinna-agent.json` current; run the ladder check after every substantive change; keep docs in sync with scripts.
6. **When the user says "move it to the cloud"**: read `guides/11-go-cloud.md`.
7. **Freshness**: `uv run .cinna-kit/tools/kit.py refresh` (compares with `{{KIT_BASE_URL}}/version`).
8. Footer: `Kit version {{KIT_VERSION}} · {{START_URL}} · human page: {{START_URL}}?format=html`.

### 6.2 `README.md` — index and capability ladder (gradual discovery)

A table of every doc with a **Read when** column, then the ladder:

| Rung | Trigger (read the guide when…) | Adds to the agent |
|------|-------------------------------|-------------------|
| Prompts & description | always, before the first test | `docs/*_PROMPT.md`, `description`, `example_prompts`, `router_trigger_prompt` |
| Scripts & data | the agent does anything beyond answering from prompts | `scripts/`, `config/`, `app-data/storage`, cache rules |
| Credentials | any external system needs a token, login, key, or OAuth | `credentials/.env(.example)`, `credentials/README.md`, manifest `credentials[]`, `cinna_credentials.py` usage |
| Schedules | the user says daily/weekly/every/at …, or the agent should run unattended | manifest `schedules[]`, `ENTRYPOINT_PROMPT.md` becomes mandatory, Makefile `run-<name>` |
| Status reporting | the agent runs unattended (schedules) or long-running checks | `scripts/update_status.py`, `STATUS.md`, `status_refresh_command`, `/run:status` in `CLI_COMMANDS.yaml` |
| CLI commands | the same operation is run repeatedly by name | `docs/CLI_COMMANDS.yaml` + Makefile parity |
| Knowledge & local skills | ≥3 distinct capabilities or domain docs beyond a page | `knowledge/<topic>/`, local skill docs, `scripts/<skill>/` subfolders |
| Multi-agent | a second agent appears, or one agent should delegate | root orchestrator conventions, `handovers[]` |
| Go cloud | the user asks, or the agent needs 24/7, email/chat channels, sharing, or webapps | `guides/11-go-cloud.md` |

The **ladder check** procedure: after any substantive change, walk the table top-down; for each rung whose trigger matches and whose artefacts are missing, read the guide and add them; tell the user in one line what rung was added and why. Never add a rung whose trigger does not match (explicit anti-over-engineering rule).

### 6.3 Root `AGENTS.md` (orchestrator role)

- Explains the folder model (`Local/`, `Cloud/`, `.cinna-kit/`), lists agents via `uv run .cinna-kit/tools/kit.py list`.
- Commands table: `kit.py new <slug> [--name …]`, `validate Local/<slug>`, `list`, `refresh`, `export Local/<slug> --to <dir>` (produce the cloud-import tree without cinna-cli).
- Role-switch rules (identical to START.md §4, restated for sessions that start at the root).
- Freshness rule: if `.cinna-kit/.last_refresh_check` is older than 7 days, run `kit.py refresh --check` first (offline-tolerant: warn and continue).
- "Cloud" section: `Cloud/` is empty until the user decides to go cloud; then it becomes a cinna-cli account workspace with its own `CLAUDE.md` — inside `Cloud/`, that file wins.

`CLAUDE.md` = one line `@AGENTS.md` plus a "Claude Code specifics" pointer to `assistants/claude-code.md`.

### 6.4 `guides/11-go-cloud.md` — the migration playbook

Preconditions block (each verified with a command, each with a recovery):
1. **uv** installed (`uv --version`; install hint from docs.astral.sh).
2. **cinna-cli** installed and ≥ `{{MIN_CLI_VERSION}}`: `uv tool install {{CLI_INSTALL_SPEC}}` / `uv tool upgrade cinna-cli`; `cinna --version`.
3. **Mutagen** present or let `cinna` prompt-install it.
4. **An account** on {{PLATFORM_URL}} — sign up at {{SIGNUP_URL}} (email confirmation may gate agent creation; role must allow building — the guide says what `403` means and that an admin grants `agent-developer`).
5. `cd <root> && cinna login {{PLATFORM_URL}} --dir Cloud` → the user clicks **Authorize** in the browser. `Cloud/` is now an account workspace (`.cinna/account.json`, `CLAUDE.md`, `context/`).
6. `cinna account user-workspace list` — pick where the agent should land (optional).
7. `kit.py validate Local/<slug>` must pass (blocking: description, ≥1 example prompt, workflow prompt non-empty, manifest schema, no tracked secrets).
8. `cd Cloud && cinna agent import ../Local/<slug>` — prints what it did and the **credential setup URLs** the user must open to fill secrets; the secrets never leave the user's machine through the CLI.
9. Verify: `cinna chat --agent <slug> "<first example prompt>"`; iterate in `Cloud/agents/<slug>/workspace/` with `cinna dev` (from here on, that copy is live; `Local/<slug>` is an archive unless the user wants to keep experimenting there — the guide says which one wins and how to re-import with `--update`).
10. Optional: publish as a bundle (pointer to `context/platform/agents/agent_bundles/agent_bundles.md` inside `Cloud/context/`).
11. Manual fallback (when `agent import` is unavailable — older CLI): step-by-step with existing verbs (`agent create`, prompt bulk write per the authoring guide, `agent sync`, copy with the exclude list from `kit.json`, `sync push`, `account credentials create --agent`, `agent schedule create`, `agent status set-command`).

### 6.5 `templates/agent/` — the scaffold (mirrors the cloud workspace)

```
<slug>/
├── cinna-agent.json                 # manifest (§3.3), slug/name filled by kit.py
├── AGENTS.md                        # local runtime wrapper (excluded from cloud import)
├── CLAUDE.md                        # "@AGENTS.md" (excluded from cloud import)
├── README.md                        # human docs: purpose, install, usage, make targets
├── Makefile                         # help, run-<schedule>, status, validate, test targets; uses `uv run`
├── pyproject.toml / .python-version # uv project; deps also mirrored into workspace_requirements.txt
├── workspace_requirements.txt       # cloud dependency list (kept in sync by kit.py validate --fix)
├── .gitignore                       # credentials/.env, .venv/, app-data/, temp/, __pycache__/
├── .claude/settings.local.json      # allow Bash(uv run:*), Bash(make:*), Bash(uv run .cinna-kit/tools/kit.py:*)
├── docs/
│   ├── WORKFLOW_PROMPT.md           # conversation-mode prompt (cloud-identical)
│   ├── ENTRYPOINT_PROMPT.md
│   ├── REFINER_PROMPT.md
│   └── CLI_COMMANDS.yaml            # ships with `status` command wired to update_status.py
├── scripts/
│   ├── README.md                    # catalog (mandatory to keep in sync)
│   ├── cinna_credentials.py         # portability shim: credentials.json (cloud) → else .env (local); get_credential(name|type)
│   └── update_status.py             # writes app-data/storage/STATUS.md atomically (same helper the cloud ships)
├── knowledge/README.md
├── files/.gitkeep
├── config/README.md                 # user-editable parameters (bundle-owned in cloud)
├── credentials/
│   ├── .env.example
│   ├── README.md                    # redacted description of credentials (same role as the cloud's credentials/README.md)
│   └── .gitignore                   # .env
└── app-data/
    ├── storage/.gitkeep
    ├── cache/.gitignore  (*)
    └── uploads/.gitkeep
```

`AGENTS.md` (agent wrapper) content rules: "When the user talks to you here, you are **{{name}}**: read `docs/WORKFLOW_PROMPT.md` and follow it; run scripts with `uv run scripts/<x>.py`; credentials come from `credentials/.env` through `scripts/cinna_credentials.py`; write runtime output to `app-data/storage/`; never print secrets. When the user asks to change/extend this agent, switch to the **Builder** role: read `.cinna-kit/README.md` (two levels up) and run the ladder check." The wrapper embeds nothing from the workflow prompt, so the prompt file stays the single source.

Cloud portability rules baked into the templates: paths relative to the agent root; `CLI_COMMANDS.yaml` uses `python scripts/x.py` (cloud) while the Makefile uses `uv run scripts/x.py` (local); scripts import the shim rather than reading files directly; `config/` is bundle-owned; `app-data/` is runtime.

### 6.6 `tools/kit.py` (stdlib only; Python ≥ 3.10)

| Command | Behaviour |
|---------|-----------|
| `kit.py new <slug> [--name N] [--root DIR]` | Copies `templates/agent/` to `<root>/Local/<slug>/`, fills `cinna-agent.json` (slug, name, kit_version), rewrites `{{name}}`/`{{slug}}` tokens in templates, `uv init`-free (pyproject shipped), prints next steps. Refuses if the folder exists. |
| `kit.py validate <path> [--fix] [--json]` | Checks: manifest present/valid (schema subset), slug == folder, required files, workflow prompt non-empty & no `{{`, `example_prompts` ≥ 1 (warn if <2), `.env` not tracked and ignored, `workspace_requirements.txt` ⊇ pyproject deps (`--fix` regenerates it), `CLI_COMMANDS.yaml` names ⊆ Makefile targets (warn), every `scripts/*.py` mentioned in `scripts/README.md` (warn), `status` command present when schedules exist (warn), `handovers[]` targets exist under `Local/`. Exit 1 on errors, 0 on warnings. `--json` for assistants. |
| `kit.py list [--root DIR]` | Table of `Local/*` (slug, name, ladder rungs present, cloud-imported?) and `Cloud/agents/*` if `Cloud/.cinna/account.json` exists. |
| `kit.py refresh [--check]` | Reads `.cinna-kit/VERSION` and `kit.json.kit_base_url`; `GET …/version`; if different, downloads the tarball to a temp dir and swaps `.cinna-kit/` atomically (old tree removed only after success); writes `.last_refresh_check`. `--check` only reports. Network errors → warning, exit 0. |
| `kit.py export <path> --to DIR` | Copies the agent minus `kit.json.cloud_import.exclude`, regenerates `workspace_requirements.txt`, clears `cloud` block — the exact tree `cinna agent import` pushes; useful for manual import or inspection. |

`kit.py` uses `urllib`, `tarfile` (with the same safe-extract discipline as cinna-cli: reject absolute members and `..`), `json`, `shutil`, `re`. No YAML parser: `CLI_COMMANDS.yaml` is inspected with a line regex for `- name:` only.

A backend unit test executes `kit.py new` + `validate` on the snapshot to guarantee the shipped scaffold validates against the shipped tool (§11).

---

## 7. Frontend Implementation

### 7.1 Getting Started article — `frontend/src/components/Onboarding/GettingStartedModal.tsx`

- New article `id: "local-first"`, title **"Build agents locally with your coding assistant"**, positioned after "How to Build An Agent".
- Content: 3-step visual (paste prompt → build in `Local/` → say "move to the cloud"); the prompt block `read <startUrl> and help me start making my agents` with a copy button (`startUrl = (import.meta.env.VITE_API_URL || window.location.origin) + "/agent-start"` — prefer origin when the SPA and API share a host; document the choice in a comment); the folder diagram; a note that no account is needed until the cloud step; cross-links to "How to Build An Agent" and "Conversation vs Building".
- `RotatingHints` gains one hint pointing at the article.

### 7.2 Login page link — `frontend/src/routes/login.tsx` (or the shared auth layout)

- Small muted link under the form: "Building agents locally with Claude Code or Codex? Start here" → `href="/agent-start?format=html"`, `target="_blank" rel="noopener"`. Rendered only when the public flag is on: the login page is unauthenticated, so fetch `GET /api/agent-start/version` once (React Query, `staleTime: Infinity`, silent on 404) and show the link on success. Keeps the link honest on disabled instances.

### 7.3 Admin toggle — Server Configuration → Interface tab

- New card `LocalAgentKitCard` next to the Disclaimer card (`frontend/src/components/Admin/ServerConfiguration/…` — follow the existing card's file location and form pattern): switch **Public local-agent starter (`/agent-start`)**, helper text with the instance's `/agent-start` URL and copy button, save via existing `PUT /admin/server-config` mutation, invalidate `["serverConfig"]`.
- Regenerate client after the model change (`bash scripts/generate-client.sh`) — `ServerConfig`/`ServerConfigUpdate` types gain the field.

### 7.4 LocalDevelopmentCard hint — `frontend/src/components/UserSettings/LocalDevelopmentCard.tsx`

- One collapsible line: "Starting from scratch on a new machine? Paste into your coding assistant: …" with the same copy button (shared small `CopyPromptSnippet` component used by 7.1 and 7.3).

### 7.5 State management

- Query keys: `["serverConfig"]` (existing), `["localAgentKitVersion"]` (login page probe).
- No new routes, no localStorage.

### 7.6 User flows (human)

- **Curious visitor** opens `https://host/agent-start` in a browser → HTML landing: headline, the copy-able prompt, "what happens next" in three bullets, link to raw markdown, link to sign up. If disabled → SPA 404 page (proxy still routes to backend which returns 404 JSON — acceptable; the login link is hidden anyway).
- **Logged-in user** finds the article in Getting Started / hints and the hint in Settings → Channels → Local Development.
- **Admin** toggles the surface; the card shows the URL so they can test it.

---

## 8. Database Migrations

- File: `backend/app/alembic/versions/<rev>_add_local_agent_kit_enabled_to_server_config.py` (generate with `make migration`, then edit).
- `op.add_column("server_config", sa.Column("local_agent_kit_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))`.
- Downgrade: `op.drop_column("server_config", "local_agent_kit_enabled")`.
- No indexes, no FKs. Single-row table; the server default keeps the existing row valid.
- Verify single head after generation (`alembic heads`), remembering merge revisions use tuple `down_revision` (known false positive when grepping).

---

## 9. Knowledge / Repository Format

Covered by §3.3 (`cinna-agent.json`), §3.4 (`kit.json`), §6.5 (scaffold). Additional validation rules the sync + tests enforce on the **kit source**:

- Every file under `docs/local_agent_kit/` is UTF-8 text except none (no binaries allowed; test asserts).
- Every `{{TOKEN}}` used in the kit is in the placeholder dict (test scans the snapshot and fails on unknown tokens — catches typos like `{{PLATFORM_URl}}`).
- `kit.json.ladder[].doc` paths exist; `templates/agent/cinna-agent.json` validates against `schema/cinna-agent.schema.json` (jsonschema is available in the backend test env; if not, the `kit.py` subset validator is the assertion).
- `CHANGELOG.md` top entry must mention the current `schema_version` (soft check: warning in the sync script).

---

## 10. Error Handling & Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| Snapshot dir missing (image built without sync) | 503 + error log on every kit path; `/version` also 503. Never cached. |
| Instance disabled | 404 on both mounts, all paths; login link hidden; admin card shows "disabled" state. |
| Reverse proxy lacks `/agent-start` block | `/agent-start` returns the SPA shell; the Getting Started article and all kit-internal links use `{{KIT_BASE_URL}}` = `/api/agent-start`, so only the *pretty* URL is affected. START.md footer prints both URLs. The nginx doc explains. |
| `Accept` negotiation wrong | Both variants embed the full START.md; HTML landing wraps it in `<pre>` so an HTML→markdown converter still yields usable instructions. `?format=md` documented in the first line of the HTML. |
| Rate limited | 429 + `Retry-After`; START.md tells the assistant to download the tarball once instead of fetching files one by one. |
| Assistant without network (Codex sandbox) | START.md: ask the user to run the curl themselves (Claude Code: `! curl …`), then continue offline from `.cinna-kit/`. |
| Existing `AGENTS.md`/`CLAUDE.md` at the chosen root | Never overwritten; append a two-line pointer block delimited by `<!-- cinna-kit:begin/end -->` (idempotent). |
| Root is a non-empty folder the user picked | Allowed; the kit only adds `Local/`, `Cloud/`, `.cinna-kit/`, and the root files. |
| Kit refresh mid-work | Atomic swap; `templates/` changes do not touch existing agents; `CHANGELOG.md` lists convention changes; `kit.py validate` reports agents created with an older `kit_version` (info level). |
| `cinna login` fails (no account, email not confirmed, no developer role) | Guide maps each error to the action (sign up / confirm email / ask admin for `agent-developer`). |
| `cinna agent import` partial failure (e.g. credential draft 403) | Import is resumable: each step is idempotent (agent by slug, credentials by name, schedules by name); rerun with `--update`; the manifest `cloud` block is written only after the workspace push succeeded. |
| Name/slug collision on the platform | `agent create` succeeds with a duplicate display name but `agent sync` resolves by id — import records `agent_id` and always resolves by id afterwards; `--update` requires the id to match. |
| Secrets in the tree | `kit.py validate` blocks import if `credentials/.env` is tracked or any `.env` exists outside `credentials/`; `cinna agent import` refuses to copy `credentials/` regardless. |
| Placeholder leak | Test asserts no `{{` remains in rendered text files except inside fenced examples explicitly marked `{{{{ … }}}}`? — simpler: the kit never needs literal double braces; test fails on any `{{`. |

---

## 11. UI/UX Considerations

- Assistant-facing docs use imperative, checklist-style prose; each guide starts with **Read this when** and ends with **Done when** (verifiable state), so the assistant can self-check and report in one line.
- Every guide is ≤ 250 lines; the ladder prevents reading everything at once.
- The HTML landing is a single self-contained page (no SPA assets), theme-neutral, with the copy button and keyboard-accessible controls; `<title>{{INSTANCE_NAME}} — build agents locally</title>`.
- Getting Started article and admin card reuse existing card/typography components; the copy snippet component announces "Copied" via the existing toast.
- Status colours and ladder wording are consistent with the platform glossary (`docs/README.md`).

---

## 12. Integration Points

- **Backend**: `main.py` (two mounts), `config.py`, `platform_knowledge_assets.py`, `context_package_service.py`, `server_config` model/service, new service + route, rate limiter reuse.
- **Repo tooling**: `sync_platform_knowledge.py` (+ Makefile target already exists), `check_docs_references.py` rule.
- **Frontend**: regenerate the client after the `ServerConfig` change (`bash scripts/generate-client.sh`); typecheck the touched components with `npx tsc --noEmit | grep …`.
- **Infra**: `frontend/nginx.conf`, `docs/infrastructure/nginx_setup.md`, production proxy (documented).
- **cinna-cli** (separate repo): `cinna agent import`, `ACCOUNT_CLAUDE.md.template` context section mentions `context/local-kit/`; README + `docs/features/` entry; tests in `tests/test_account.py` style with a mocked client.
- **Docs**: new feature docs `docs/application/local_agent_kit/local_agent_kit.md` + `_tech.md`; `docs/README.md` registry row (Application domain; no counters in the Domain Map); `getting_started.md` article list; `server_configuration` docs; `cinna_cli_integration` docs (`agent import`, `context/local-kit/`); `account_cli_workspace.md` context package section.

---

## 13. cinna-cli: `cinna agent import` (separate repo)

`cinna agent import <path> [--name N] [--workspace <user-workspace>] [--update] [--dry-run] [--no-push] [--yes]` — runs from the account workspace root (or any folder inside it).

Steps (each printed as `[n/9] …`, each idempotent):
1. Load and validate `cinna-agent.json` (same subset rules as `kit.py`; refuse `schema_version` > supported). Refuse if `credentials/.env` would be copied (it is never copied).
2. Resolve target: if `cloud.agent_id` set and reachable → require `--update`; else `run_agent_create(name, description)` → agent id.
3. Metadata + prompts: bulk prompt write (workflow/entrypoint/refiner read from the files named in `prompts`, `router_trigger_prompt`, `description`, `example_prompts`) via the same account endpoint the authoring guide uses; `status_refresh_command` via `run_status_set_command` when set.
4. `run_agent_sync(slug)` → `agents/<slug>/`.
5. Copy tree into `agents/<slug>/workspace/` honouring `kit.json.cloud_import.exclude` (embedded default list in the CLI as fallback), generate `workspace_requirements.txt` from `pyproject.toml` `[project.dependencies]` when missing, never touch `credentials/` or `app-data/`.
6. `cinna sync push` (skip with `--no-push`); on conflicts print the resolve hint.
7. Credentials: for each spec → `run_credentials_create(name, type, description, agent=slug)`; collect `setup_url`s; existing credential with the same name in the active workspace → share only.
8. Schedules: `run_schedule_create` per spec (name-idempotent; `--update` updates in place).
9. Stamp `cloud` block into **both** the Local manifest and the synced workspace copy; print summary table + next steps (`cinna chat --agent <slug> "<example_prompts[0]>"`, credential URLs to open, `cd agents/<slug> && cinna dev`).

`--dry-run` prints the plan (files to copy, credentials/schedules to create) without network writes. Errors from the platform surface verbatim (400/403/404 semantics as in `agent create` / credential verbs).

---

## 14. Future Enhancements (Out of Scope)

- Platform-side **import endpoint** (upload a `kit.py export` tarball to create an agent without cinna-cli).
- Public MCP for live knowledge (explicitly deferred by the owner; the kit stays a static, versioned prompt set).
- `cinna agent export` (cloud → `Local/`) to round-trip an agent back for offline experiments.
- Reading `cinna-agent.json` on the platform at publish time (manifest ↔ file reconciliation).
- Local scheduler integration (cron/launchd generation) — the kit documents a manual snippet only.
- Per-assistant plugin packaging (a Claude Code plugin / Codex skill wrapping the kit).
- Telemetry on `/agent-start` usage (none by design in this pass).

---

## 15. Summary Checklist

### Phase 1 — Kit content + backend surface
- [ ] Create `docs/local_agent_kit/` with START.md, README.md (ladder), kit.json, guides 01–12, assistants/*, schema, templates/root, templates/agent (§6), CHANGELOG.md. Distil from `docs/HOW_TO_WRITE_AN_AGENT.md` (erp-sh repo) + `knowledge/guides/authoring-agent-prompts.md` + `BUILDING_AGENT.md`; keep guides ≤ 250 lines each with "Read this when / Done when".
- [ ] Extend `sync_platform_knowledge.py` (step 3, rmtree only `knowledge/local-kit/`), run `make sync-platform-knowledge`, commit the snapshot.
- [ ] `platform_knowledge_assets.local_kit_dir()` + shared `snapshot_cache_key()` helper (ContextPackageService refactored to use it).
- [ ] `LocalAgentKitService` (render, hash, tarball, cache, media types, HTML landing).
- [ ] `local_agent_kit.py` router + guard (enabled, rate limit, ETag/304, headers); mount at `/agent-start` and `/api/agent-start` in `main.py`.
- [ ] Settings `LOCAL_AGENT_KIT_RATE_LIMIT_PER_MIN`, `CINNA_CLI_INSTALL_SPEC`.
- [ ] `frontend/nginx.conf` `/agent-start` block; `docs/infrastructure/nginx_setup.md` section.
- [ ] Tests `backend/tests/api/cli/test_local_agent_kit.py` (both mounts; markdown vs HTML negotiation incl. `?format`; `/version` == tarball `VERSION` == `X-Kit-Version`; file fetch; traversal/symlink/unknown → 404; 304 on `If-None-Match`; 429 after limit; no `{{` left; placeholders equal settings; disabled flag → 404 everywhere; 503 when snapshot dir missing, monkeypatched).

### Phase 2 — kit.py + scaffold validation
- [ ] `tools/kit.py` (`new`, `validate [--fix] [--json]`, `list`, `refresh [--check]`, `export`), stdlib only, safe tar extract.
- [ ] `backend/tests/unit/test_local_kit_tool.py`: run `kit.py new` on the snapshot into tmp, `validate` passes; `export` excludes `credentials/`, `AGENTS.md`, `CLAUDE.md`, `.venv/`; unknown-token scan; ladder docs exist; scaffold manifest matches schema.

### Phase 3 — Instance toggle + human surfaces
- [ ] `ServerConfig.local_agent_kit_enabled` + `ServerConfigUpdate` + service update path; migration `add_local_agent_kit_enabled_to_server_config`; single alembic head.
- [ ] Regenerate client; admin `LocalAgentKitCard` in Server Configuration → Interface; Getting Started article `local-first` + hint; login-page link gated by `/api/agent-start/version`; LocalDevelopmentCard hint; shared `CopyPromptSnippet`.
- [ ] Tests: admin PUT toggles the flag and `/agent-start` flips 200↔404; non-superuser PUT 403 (existing); disclaimer version unchanged when only the flag changes.

### Phase 4 — Context package + docs
- [ ] `ContextPackageService` 4th source `context/local-kit/` (rendered), version folding, index bullet; test: package contains `local-kit/START.md` and its `VERSION` changes when the kit changes.
- [ ] Feature docs `docs/application/local_agent_kit/local_agent_kit.md` + `_tech.md`; `docs/README.md` row; updates to getting_started, server_configuration, cinna_cli_integration (account workspace context section, `agent import`), nginx doc; `check_docs_references.py` rule for `~/`, `Local/`, `Cloud/`, `.cinna-kit/` paths.

### Phase 5 — cinna-cli (`/Users/evgenyl/dev/ml-llm/cinna-cli`)
- [ ] `src/cinna/local_import.py` + `agent import` command in `main.py` (§13), reuse `run_agent_create`, `run_agent_sync`, credential/schedule/status helpers; embedded default exclude list; `--dry-run`, `--update`, `--no-push`.
- [ ] `ACCOUNT_CLAUDE.md.template`: `context/local-kit/` bullet + "importing a locally built agent" pointer to `context/local-kit/guides/11-go-cloud.md`.
- [ ] README + `docs/features/` entry; tests (`tests/test_local_import.py`) with the mocked account client: happy path, `--dry-run` makes no calls, resume with `--update`, credentials/ never copied, secrets never printed.
- [ ] Bump `MINIMUM_CLI_VERSION` in the backend only if the kit's go-cloud guide requires the new verb (it documents the manual fallback, so no bump is required).

### Validation (what to verify end-to-end)
- [ ] Fresh machine simulation: paste the prompt into Claude Code against a local instance → kit downloaded → `Local/<slug>` scaffolded → `validate` green → role-switch test answers an example prompt.
- [ ] `cinna login http://localhost:5173 --dir Cloud` → `cinna agent import ../Local/<slug>` → `cinna chat` returns an answer; credential draft shows "needs setup" in the UI with the printed URL.
- [ ] Disabled flag hides everything; re-enable restores; no user data on any public response (grep tests).
