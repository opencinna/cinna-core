# AI Model Freshness & Deprecation Nudges — Technical Reference

## File Locations

### Backend — Catalog & Health

- `backend/app/services/environments/model_catalog.py` — Static catalog SSOT: `ModelTier`, `MODE_TO_TIER`, `DEFAULT_CATALOG`, `RETIRED_MODELS`, `KNOWN_TIER_WORDS`, `resolve_model`, `infer_tier`, `is_retired`, `is_known_word`
- `backend/app/services/environments/model_health_service.py` — `evaluate_environment`, `_evaluate_mode`, `_model_in_discovered`, `_normalize_model_id`
- `backend/app/services/environments/environment_lifecycle.py` — calls `resolve_model` for opencode config generation and for injecting `MODEL_BUILDING`/`MODEL_CONVERSATION` env vars
- `backend/app/services/environments/environment_service.py` — `EnvironmentService.to_public_with_health()` (populates transient `model_health` on `AgentEnvironmentPublic`)
- `backend/app/services/environments/admin_environment_service.py` — calls `evaluate_environment` per row to populate `model_health_warning`

### Backend — Discovery & Cron

- `backend/app/services/credentials/model_discovery_service.py` — `probe_models` (DB-free shared dispatch used by both the cron and the Test Connection endpoint), `discover_models_for_credential`, `test_connection`, `refresh_all_credentials`, `dispatch_model_deprecation_notifications`
- `backend/app/services/credentials/model_discovery_scheduler.py` — APScheduler cron, Postgres advisory-lock single-leader guard, `start_scheduler`, `shutdown_scheduler`
- `backend/app/main.py` — registers `start_scheduler` / `shutdown_scheduler` on lifespan events alongside other schedulers
- `backend/app/core/config.py` — `MODEL_DISCOVERY_ENABLED`, `MODEL_DISCOVERY_INTERVAL_HOURS`

### Backend — Models

- `backend/app/models/environments/environment.py` — `ModelHealthMode`, `ModelHealthPublic`, `AgentEnvironmentPublic.model_health` (transient), `AdminAgentEnvironmentPublic.model_health_warning`
- `backend/app/models/credentials/ai_credential.py` — `AICredential.discovered_models`, `models_discovered_at`, `models_discovery_error`; `AICredentialPublic` exposes all three

### Backend — Notifications

- `backend/app/services/notifications/notification_catalog.py` — `NotificationType.MODEL_DEPRECATED`, catalog entry with `dedup_scope="environment_id"`
- `backend/app/email-templates/build/model_deprecated.html` — compiled HTML template read at runtime

### Backend — Migrations

- `backend/app/alembic/versions/581dd9e44be1_add_discovered_models_to_ai_credential.py` — adds `discovered_models` (JSON), `models_discovered_at` (timestamptz), `models_discovery_error` (text) to `ai_credential`; downgrade drops all three; no backfill

### Agent Environment (inside container)

- `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py` — reads `MODEL_BUILDING` / `MODEL_CONVERSATION` env vars and sets `options.model`
- `backend/app/env-templates/general-env/docker-compose.template.yml` — forwards `MODEL_BUILDING` / `MODEL_CONVERSATION` into the container
- `backend/app/env-templates/python-env-advanced/docker-compose.template.yml` — same forwarding
- `backend/app/env-templates/general-assistant-env/docker-compose.template.yml` — same forwarding

### Frontend

- `frontend/src/components/Environments/ModelHealthBadge.tsx` — amber badge + tooltip with per-mode details and cause CTA
- `frontend/src/components/Environments/EnvironmentCard.tsx` — renders `ModelHealthBadge` when `environment.model_health` is present
- `frontend/src/components/Agents/AgentEnvironmentsTab.tsx` — renders `EnvironmentCard` (badge included via composition)
- `frontend/src/components/Admin/Environments/AdminEnvTable.tsx` — `model_health_warning` column beside the stale column; `ModelHealthCell` renders the flag
- `frontend/src/components/Environments/EnvironmentConfigForm.tsx` — model override input uses `discovered_models` as a `<datalist>` for suggestions
- `frontend/src/components/UserSettings/AICredentials.tsx` — model override input uses `discovered_models` as a `<datalist>` for suggestions

---

## Model Catalog (`model_catalog.py`)

### `ModelTier(str, Enum)`

```python
class ModelTier(str, Enum):
    FAST = "fast"
    BALANCED = "balanced"
    POWERFUL = "powerful"  # reserved, not yet in DEFAULT_CATALOG
```

### `MODE_TO_TIER`

```python
{"conversation": ModelTier.FAST, "building": ModelTier.BALANCED}
```

### `DEFAULT_CATALOG`

Keyed by `(engine, provider)` → `{ModelTier: model_string}`.

| (engine, provider) | FAST | BALANCED |
|---|---|---|
| `claude-code`, `anthropic` | `"haiku"` | `"sonnet"` |
| `claude-code`, `minimax` | `"MiniMax-M2.1-lightning"` | `"MiniMax-M2.1"` |
| `opencode`, `anthropic` | `"anthropic/claude-haiku-4-5"` | `"anthropic/claude-sonnet-4-6"` |
| `opencode`, `openai` | `"openai/gpt-5.4-nano"` | `"openai/gpt-5.4-mini"` |
| `opencode`, `google` | `"google/gemini-2.5-flash"` | `"google/gemini-2.5-pro"` |
| `opencode`, `openai_compatible` | from credential `model` field | from credential `model` field |

The `claude-code/anthropic` row stores tier **words** (`haiku`/`sonnet`) rather than dated
snapshots — the Claude Code CLI auto-resolves these, so they never go stale.

Freshness fix: `opencode/anthropic` BALANCED was changed from `anthropic/claude-sonnet-4-5`
to `anthropic/claude-sonnet-4-6` at catalog creation time.

### `RETIRED_MODELS`

A `frozenset[str]` of known-retired concrete model IDs. Lenient matching: both provider-prefixed
(e.g. `anthropic/claude-3-opus-20240229`) and bare forms are recognized. Tier words are never
included.

### `KNOWN_TIER_WORDS`

```python
frozenset({"haiku", "sonnet", "opus"})
```

### Key Functions

**`resolve_model(engine, provider, mode, override, openai_compatible_model=None) -> str`**

Resolution order:
1. Truthy `override` → return verbatim.
2. `openai_compatible` provider → return `openai_compatible_model` or `"gpt-4"` fallback.
3. Look up `DEFAULT_CATALOG[(engine, provider)][MODE_TO_TIER[mode]]`.
4. Unknown `(engine, provider)` → fall back to the engine's `anthropic` row and log a warning.

**`is_retired(model: str) -> bool`**

Checks both the given string and its prefix-stripped form against `RETIRED_MODELS`. Tier words
return `False` unconditionally.

**`is_known_word(model: str) -> bool`**

Case-insensitive check against `KNOWN_TIER_WORDS` after stripping any provider prefix.

**`infer_tier(provider, model_id) -> ModelTier | None`**

Name-heuristic tier inference. Tokenizes on non-alphanumeric separators:
- Contains `haiku`/`nano`/`flash`/`lightning` → `FAST`
- Contains `sonnet`/`opus`/`mini`/`pro` → `BALANCED`
- Both or neither strong marker → `None`

Used by discovery for availability checks; returns `None` rather than guessing when ambiguous.

---

## Model Health Service (`model_health_service.py`)

### Status and Cause Constants

```python
STATUS_OK = "ok"
STATUS_RETIRED_OVERRIDE = "retired_override"
STATUS_UNKNOWN_MODEL = "unknown_model"
STATUS_UNVERIFIED = "unverified"

CAUSE_STALE_DEFAULT = "stale_default"
CAUSE_FROZEN_OVERRIDE = "frozen_override"
```

### `_normalize_model_id(model: str) -> str`

Reduces a model ID to a family stem for lenient cross-source matching:
1. Strips any `provider/` prefix.
2. Strips a trailing `-YYYYMMDD` snapshot suffix (regex `r"-\d{8}$"`).
3. Lowercases.

Example: `anthropic/claude-sonnet-4-5-20250929` → `claude-sonnet-4-5`.

This prevents false-positive `unknown_model` flags: discovered model IDs from native APIs
typically include a dated snapshot suffix, while the catalog stores undated family IDs.

### `_model_in_discovered(model, discovered) -> bool`

Normalizes both the effective model and every discovered ID before comparing (see above).
A match on the family stem is sufficient.

### `evaluate_environment(session, environment, agent=None) -> ModelHealthPublic`

Entry point. Iterates `("conversation", "building")`. Per mode:

1. Calls `_evaluate_mode`.
2. Collects into `ModelHealthPublic.modes`.
3. Sets `has_warning=True` when any mode is `retired_override` or `unknown_model`.

Never raises — failures degrade to a healthy roll-up (`has_warning=False`, `modes=[]`).

**DB cost:** one `Agent` PK lookup (skippable by passing a pre-loaded `agent`); per mode, at
most one `AICredential` PK lookup or one type-default query. No live provider calls.

### `_evaluate_mode(session, environment, user_id, mode) -> ModelHealthMode`

Classification priority (in order):

1. **Tier words** (`is_known_word`) → always `ok`.
2. **`openai_compatible`** provider → always `ok` (user-owned namespace).
3. **Per-credential `discovered_models`** (most authoritative):
   - Model found via normalized match → `ok`.
   - Override present, not found → `retired_override` / `frozen_override`.
   - No override, not found → `unknown_model` / `stale_default`.
4. **Catalog `RETIRED_MODELS`** (fallback when no discovery data):
   - Same override/no-override branching as above.
5. **Credential exists but `discovered_models is None` and not MiniMax** → `unverified`.
6. **Otherwise** → `ok`.

---

## `AgentEnvironmentPublic` — Transient `model_health` Field

```python
class ModelHealthMode(SQLModel):
    mode: str                       # "conversation" | "building"
    model: str                      # effective resolved model
    status: str                     # "ok" | "retired_override" | "unknown_model" | "unverified"
    cause: str | None = None        # "stale_default" | "frozen_override" | None
    suggested_model: str | None = None  # catalog tier default for this mode (when concrete)
    cta: str | None = None          # plain-language remediation copy

class ModelHealthPublic(SQLModel):
    has_warning: bool = False
    modes: list[ModelHealthMode] = []
```

`model_health: ModelHealthPublic | None = None` on `AgentEnvironmentPublic` is never
persisted (no DB column). It is populated by `EnvironmentService.to_public_with_health()`,
which is called from env list and detail route builders.

**Precedent:** mirrors `refresh_command_warning` on `AgentStatusPublic`.

---

## `AdminAgentEnvironmentPublic` — `model_health_warning` Field

```python
model_health_warning: bool = False
```

Computed per row inside `AdminEnvironmentService.list_environments()`, which calls
`evaluate_environment(session, env, agent=agent)` (passes the pre-loaded agent to avoid
an extra PK lookup). Distinct from `is_stale` (image-tag) — different remediation path.

---

## Per-Credential Model Discovery

### `AICredential` Schema Changes (migration `581dd9e44be1`)

| Column | Type | Purpose |
|--------|------|---------|
| `discovered_models` | `JSON`, nullable | List of model IDs the key can access. `None` = never discovered |
| `models_discovered_at` | `timestamptz`, nullable | Timestamp of last SUCCESSFUL discovery |
| `models_discovery_error` | `Text`, nullable | Coarse reason code for last failure (e.g. `"oauth_token_unsupported"`, `"invalid_key"`) |

All three are exposed on `AICredentialPublic` (non-secret). The `encrypted_data` field is
never included.

### `probe_models(cred_type, api_key, base_url=None) -> ProbeResult`

DB-free async function. **The single dispatch path used by both the discovery cron and the Test Connection endpoint** (`POST /ai-credentials/test-connection`). Dispatch by `AICredentialType`:

| Type | Mechanism |
|------|-----------|
| `anthropic` | `GET https://api.anthropic.com/v1/models` (httpx); skips OAuth tokens (`sk-ant-oat*`) — `reason="oauth_token_unsupported"` |
| `openai` | `GET https://api.openai.com/v1/models` (httpx) |
| `google` | `google.genai.Client.models.list()` (strips `models/` prefix from returned names) |
| `openai_compatible` | `GET {base_url}/models` (httpx, OpenAI response shape assumed); skips when no `base_url` — `reason="no_base_url"` |
| `minimax` | Always skipped — `reason="no_list_endpoint"` |

All blocking HTTP calls run via `anyio.to_thread.run_sync` (event-loop safety).

HTTP 401/403 responses → `ok=False`, `reason="invalid_key"`.

On success: deduplicates while preserving order, `ok=True`, `reason=None`.

### `discover_models_for_credential(session, credential) -> list[str]`

Decrypts the credential, delegates to `probe_models`, maps the result onto the credential row. On success: writes `discovered_models`, stamps `models_discovered_at`, clears `models_discovery_error`. On skip or invalid_key: records the coarse reason, leaves `discovered_models` unchanged. Caller commits.

The **Test Connection endpoint** (`test_connection`) follows the same `probe_models` path. When called with an existing `credential_id` (Edit case) it also persists the probe result onto the row — making it the manual force-refresh entry point for `discovered_models`. See [ai_credentials_tech.md](../../application/ai_credentials/ai_credentials_tech.md) for the full endpoint contract.

### `refresh_all_credentials(session) -> int`

Iterates all `AICredential` rows. Per-credential `try/except` — one bad key never aborts
the batch. On exception: rolls back, refetches the row, records the exception class name as
`models_discovery_error`. Returns the count of successful (error-cleared) credentials.

### Discovery Scheduler (`model_discovery_scheduler.py`)

- `BackgroundScheduler` (APScheduler) with `interval` trigger.
- **Multi-leader guard:** before each run, executes `SELECT pg_try_advisory_lock(:k)` with
  key `0x4D4F44454C4453` ("MODELDS"). Only the worker that wins runs the batch; others log
  a skip message. The lock is released in a `finally` block on the same connection.
- Jitter: ±600 seconds to avoid thundering herd across workers.
- `max_instances=1`, `coalesce=True`.
- After discovery batch: calls `dispatch_model_deprecation_notifications`.

**Config knobs:**

| Setting | Default | Description |
|---------|---------|-------------|
| `MODEL_DISCOVERY_ENABLED` | `True` | Set to `False` to disable the cron entirely |
| `MODEL_DISCOVERY_INTERVAL_HOURS` | `24` | Cron interval in hours |

### `dispatch_model_deprecation_notifications(session) -> int`

Called by the scheduler after `refresh_all_credentials`. For each `AgentEnvironment`:

1. Calls `evaluate_environment`.
2. If `has_warning=False` → removes env from `_warned_env_ids`, continues.
3. If already in `_warned_env_ids` (previously warned) → skips (transition-only fire).
4. Otherwise: adds to `_warned_env_ids`; builds a detail line from flagged modes; calls
   `SystemNotificationService.notify(... notification_type=NotificationType.MODEL_DEPRECATED ...)`.

`_warned_env_ids` is a process-local `set[str]`. Resets on restart (at most one extra email
shortly after a deploy — consistent with notification throttle semantics).

---

## Resolution Wiring in `environment_lifecycle.py`

The lifecycle manager calls `resolve_model` in two places:

1. **`_generate_env_file`** — resolves `model_building` and `model_conversation` via a helper
   `_resolve_mode_model(sdk_value, mode)` that splits the SDK string and passes the environment's
   stored override. Injects as `MODEL_BUILDING` and `MODEL_CONVERSATION` in the `.env` file.

2. **`_build_config` (OpenCode)** — resolves the model for each mode's `opencode.json` `model`
   field. Baked into the config at generation time; the opencode process reads it on startup.

3. **`_generate_minimax_settings_files`** — sources model IDs from the catalog for the JSON
   settings written to `app/core/.claude/`.

---

## Claude Code Adapter — `MODEL_BUILDING` / `MODEL_CONVERSATION` Consumption

`claude_code_sdk_adapter.py`, inside the container:

```python
model_env_var = f"MODEL_{mode.upper()}"  # "MODEL_BUILDING" or "MODEL_CONVERSATION"
model_value = (os.getenv(model_env_var) or "").strip()
if model_value:
    options.model = model_value
elif mode == "conversation":
    options.model = "haiku"  # fallback for pre-catalog environments
else:
    pass  # leave unset; SDK uses its own default
```

This fixes a latent bug: previously claude-code ignored `model_override_*` because no resolved
value was ever injected. Now the adapter uses whatever the backend computed from the catalog.

All three docker-compose templates forward these vars with `${MODEL_BUILDING:-}` /
`${MODEL_CONVERSATION:-}` syntax (empty-string default for backwards compatibility with
environments generated before this feature shipped).

---

## System Notification — `MODEL_DEPRECATED`

Added to `NotificationType` enum and `NOTIFICATION_CATALOG`:

| Field | Value |
|-------|-------|
| `label` | `"Deprecated AI models"` |
| `description` | `"Email me when one of my agent environments is configured to use an AI model that is deprecated or no longer available."` |
| `default_email_enabled` | `True` |
| `email_template` | `"model_deprecated.html"` |
| `subject` | `{PROJECT_NAME} — Update the AI model for {instance_name}` |
| `dedup_scope` | `"environment_id"` |

Context keys passed to the template:

| Key | Source |
|-----|--------|
| `project_name` | `settings.PROJECT_NAME` |
| `agent_name` | `Agent.name` |
| `instance_name` | `AgentEnvironment.instance_name` |
| `environment_id` | `str(env.id)` |
| `detail` | Per-mode flagged model + CTA copy, joined by `"; "` |
| `link` | `{FRONTEND_HOST}/agents/{agent.id}` |

No schema migration required — the catalog is code-only.

---

## Frontend Components

### `ModelHealthBadge.tsx`

Amber badge rendered on `EnvironmentCard` when `environment.model_health?.has_warning`.
Tooltip shows each flagged mode's `model`, `status`, and `cta`. Primary action is
cause-specific:
- `frozen_override` → opens the environment reconfigure dialog.
- `stale_default` → triggers a restart.

### `AdminEnvTable.tsx` — `model_health_warning` column

`columnHelper.accessor("model_health_warning", ...)` renders a `ModelHealthCell` with
the boolean flag. Positioned beside the `StaleBadge` column.

### Discovered-models `<datalist>` suggestions

Both `EnvironmentConfigForm.tsx` and `AICredentials.tsx` expose the credential's
`discovered_models` list as `<datalist>` options on the model override input field.
`const discoveredModels = selectedCredential?.discovered_models ?? []`.

---

## Security

- **Credential secrets** never leave the encrypted store. Discovery decrypts in-memory only
  inside the cron; `discovered_models` / timestamps / error strings are non-secret.
- **`model_health`** on `AgentEnvironmentPublic` is owner-scoped (same access guard as the
  environment response it rides on). Admin column is behind the `is_superuser` guard.
- **No secret in logs:** discovery logs provider + credential id + model count only.
  `models_discovery_error` stores a coarse reason code, not a raw API error body.
- **OAuth tokens skipped** for Anthropic discovery; no Models API calls made with `sk-ant-oat*`.

---

*Last updated: 2026-06-05 — noted probe_models shared dispatch and Test Connection as manual force-refresh path*
