# AI Model Freshness & Deprecation Nudges — Implementation Plan

## Overview

Keep agent environments from getting silently locked into stale or retired LLM
models, and **detect & nudge** (never silently auto-upgrade) when an environment
is configured to use a model that is deprecated, retired, or unavailable to its
credential.

Core capabilities:

- **Single source of truth** for default model selection per `(engine, provider, tier)` — replaces three scattered hardcoded maps.
- **Tier-intent resolution** — claude-code emits auto-tracking tier words (`haiku`/`sonnet`); OpenCode/MiniMax resolve to concrete IDs from the catalog. The per-mode `model_override_*` is honored by **all** engines (fixes a latent claude-code bug).
- **Per-credential available-model discovery** — a cron polls native provider APIs (Anthropic / OpenAI / Google) using each AI credential, because different keys can see different models. Results are cached on the credential.
- **Detect & nudge** — a computed, user-facing model-health signal on the environment, surfaced as a badge on the environment card with a cause-specific CTA, plus a `model_deprecated` system notification. Distinct from the existing image-tag `is_stale` flag (remediation is *reconfigure/restart*, not a Docker rebuild).

### High-level flow

```
                          ┌─────────────────────────────────────────────┐
                          │ model_catalog.py  (static SSOT + RETIRED set) │
                          └───────────────┬─────────────────────────────┘
                                          │ resolve_model(engine,provider,mode,override)
        ┌─────────────────────────────────┼──────────────────────────────────┐
        ▼                                  ▼                                   ▼
 claude-code adapter            environment_lifecycle.py               model_health_service
 (tier word / MODEL_* env)      (opencode.json / minimax settings)      (validate resolved + override)
        ▲                                  ▲                                   │ flags
        │                                  │                                   ▼
        └──────────── per-credential discovered model list ◄───────  AICredential.discovered_models
                          ▲                                          (cron: native provider /models)
                          │
                 model_discovery_scheduler (APScheduler, periodic)
```

---

## Architecture Overview

Four layers, each independently shippable, mapping to phases:

| Layer | Phase | Component | New/Changed |
|-------|-------|-----------|-------------|
| 1 | P1 | `model_catalog.py` — static catalog, tiers, `resolve_model`, `RETIRED_MODELS` | New |
| 2 | P2 | Route all default/override resolution through the catalog; fix claude-code override plumbing | Changed |
| 3 | P3 | Per-credential discovered-model cache + `model_discovery_scheduler` cron | New + migration |
| 4 | P4 | Computed model-health on `AgentEnvironmentPublic` + EnvironmentCard badge + `model_deprecated` notification + admin column | New + changed |

Integration points (existing systems this touches):

- **agent_environment_core / multi-sdk** — default model resolution and settings-file generation.
- **ai_credentials** — new per-credential discovered-models cache; native API listing.
- **agent_environments** (admin staleness) — reuse the surfacing pattern, not the `is_stale` flag.
- **system_notifications** — new `model_deprecated` catalog type.
- **realtime_events** — optional WS event when an env is newly flagged (reuse existing event bus).

---

## Layer / Phase 1 — Centralized Model Catalog

### New file: `backend/app/services/environments/model_catalog.py`

Single source of truth. No DB, no I/O — pure data + functions.

**Types & data:**

- `class ModelTier(str, Enum)` → `FAST`, `BALANCED`. (Two tiers today: conversation→FAST, building→BALANCED. Keep the enum open for a future `POWERFUL`.)
- `MODE_TO_TIER: dict[str, ModelTier]` → `{"conversation": FAST, "building": BALANCED}`.
- `DEFAULT_CATALOG: dict[tuple[str, str], dict[ModelTier, str]]` keyed by `(engine, provider)`:

  | (engine, provider) | FAST | BALANCED |
  |---|---|---|
  | `claude-code`,`anthropic` | `"haiku"` | `"sonnet"` (tier WORDS — CLI auto-resolves) |
  | `claude-code`,`minimax` | `"MiniMax-M2.1-lightning"` | `"MiniMax-M2.1"` |
  | `opencode`,`anthropic` | `"anthropic/claude-haiku-4-5"` | `"anthropic/claude-sonnet-4-6"` |
  | `opencode`,`openai` | `"openai/gpt-5.4-nano"` | `"openai/gpt-5.4-mini"` |
  | `opencode`,`google` | `"google/gemini-2.5-flash"` | `"google/gemini-2.5-pro"` |
  | `opencode`,`openai_compatible` | *(from credential `model`)* | *(from credential `model`)* |

  > Fixes a live staleness bug: current opencode/anthropic building default is `claude-sonnet-4-5` (one generation behind `claude-sonnet-4-6`).

- `RETIRED_MODELS: frozenset[str]` — curated seed list of known-retired IDs (e.g. `claude-3-5-haiku-20241022`, `claude-3-7-sonnet-20250219`, `claude-3-opus-20240229`, `claude-3-5-sonnet-20241022`, retired OpenAI/Gemini snapshots). Match leniently (provider-prefixed and bare forms). This is the **fallback** signal when no per-credential discovery data exists.
- `KNOWN_TIER_WORDS: frozenset[str]` → `{"haiku","sonnet","opus"}` (claude-code only; never flagged as retired since the CLI resolves them).

**Functions:**

- `resolve_model(engine: str, provider: str, mode: str, override: str | None, openai_compatible_model: str | None = None) -> str`
  - If `override` truthy → return it (honored verbatim; validation is Layer 4's job).
  - Else look up `DEFAULT_CATALOG[(engine, provider)][MODE_TO_TIER[mode]]`.
  - `openai_compatible` → use `openai_compatible_model` (or a sane literal fallback).
  - Unknown `(engine, provider)` → fall back to the `anthropic` row of the engine; log a warning.
- `infer_tier(provider: str, model_id: str) -> ModelTier | None` — best-effort name-based tier inference for discovery (e.g. contains `haiku`/`nano`/`flash`→FAST, `sonnet`/`opus`/`mini`/`pro`/`gpt-5`→BALANCED). Returns `None` when ambiguous. (See Open Questions.)
- `is_known_word(model: str) -> bool` and `is_retired(model: str) -> bool` helpers.

**Tests (P1):** unit tests (no Docker) for `resolve_model` across every `(engine, provider, mode)` with and without override; `is_retired` matching bare vs prefixed; `infer_tier` on representative IDs.

---

## Layer / Phase 2 — Route All Resolution Through the Catalog

### `backend/app/services/environments/environment_lifecycle.py`

- **Delete** the local `_default_models` map inside `_generate_opencode_config_files` (~L1852).
- `_build_config(mode, sdk_value)` (~L1932): replace the `model_override or provider_defaults...` logic with `resolve_model(engine="opencode", provider=provider, mode=mode, override=model_override, openai_compatible_model=openai_compatible_model)`.
- `_generate_minimax_settings_files` (~L1774): build the model fields from `resolve_model("claude-code", "minimax", mode, override)` instead of the hardcoded `MiniMax-M2.1*` literals. (Note: MiniMax settings currently apply the same model to both modes; preserve that unless per-mode is desired.)

### claude-code engine — honor overrides + tier words

The claude-code adapter runs **inside the container**, so the resolved model must reach it via env var (the plumbing half-exists: `routes.py:689` references `MODEL_OVERRIDE_BUILDING`, currently dead/`None`).

- **Backend (host side):** in the env-file / settings generation path, inject per-mode `MODEL_CONVERSATION` / `MODEL_BUILDING` (or reuse/repurpose the `MODEL_OVERRIDE_*` names) computed via `resolve_model("claude-code", provider, mode, override)`. For anthropic this yields a tier word (`haiku`/`sonnet`) when no override; for minimax it yields the concrete MiniMax id. Ensure the docker-compose templates forward these vars into the container.
- **Container (`claude_code_sdk_adapter.py` ~L313):** remove the hardcoded `options.model = "haiku"`. Read `MODEL_<MODE>` from env; if set, `options.model = <value>`; if unset, leave unset (SDK default) for building and fall back to `"haiku"` for conversation only as a last resort. This **fixes the latent bug** that claude-code ignores `model_override_*`.

> Re-resolution timing is unchanged: create-time and every start/restart/rebuild re-run generation, so default changes propagate on next reconfigure (per the existing credential-bag re-resolution flow). A frozen override still won't self-heal — that's Layer 4's job.

**Tests (P2):** assert generated `opencode.json` `model` matches catalog per provider/mode and honors override; assert MiniMax settings use catalog values; assert the container env file/compose carries `MODEL_*`; container-side adapter unit test that `MODEL_*` env drives `options.model` (or add to existing adapter tests). Regression: existing `agents_ai_credential_slot_mismatch_test.py` still passes.

---

## Layer / Phase 3 — Per-Credential Model Discovery (cron)

Different API keys can have access to different models, so the available-model list is cached **per `AICredential`**.

### Data model changes — `backend/app/models/credentials/ai_credential.py`

Add to `AICredential` (table):

- `discovered_models: list[str] | None` — JSON column (`sa_column=Column(JSON, nullable=True)`); concrete provider model IDs the key can see. `None` = never discovered.
- `models_discovered_at: datetime | None` — last successful discovery timestamp (TTL/staleness of the cache itself).
- `models_discovery_error: str | None` — last discovery failure reason (nullable text); for surfacing "couldn't verify".

Expose **safe** fields on `AICredentialPublic`: `discovered_models`, `models_discovered_at`, `models_discovery_error` (no secrets). These let the AddEnvironment / AICredentials UI show "models this key can access".

### Migration

- `add_discovered_models_to_ai_credential.py`: add the three nullable columns. Downgrade drops them. No backfill (cron populates).

### New service: `backend/app/services/credentials/model_discovery_service.py`

- `discover_models_for_credential(session, credential) -> list[str]` — decrypts the key (existing `ai_credentials_service` decryption), dispatches by `type`:
  - `anthropic` → `client.models.list()` (`GET /v1/models`). **Skip OAuth tokens** (`sk-ant-oat*`) — they cannot call the Models/Messages API the same way (see `anthropic_credential_types`); record a benign `models_discovery_error="oauth_token_unsupported"` and leave `discovered_models` as-is.
  - `openai` → `GET /v1/models`.
  - `google` → ListModels.
  - `openai_compatible` → `{base_url}/models` if reachable, else skip (record error).
  - `minimax` → no public list endpoint assumed → skip (catalog-only).
  - All blocking HTTP runs via `anyio.to_thread` (per the event-handler concurrency convention: sync DB I/O on the loop is fine; offload blocking network).
- `refresh_all_credentials(session)` — iterate credentials, call discovery, persist `discovered_models` / `models_discovered_at` / `models_discovery_error`. Per-credential try/except so one bad key doesn't abort the batch (failure-isolated, like the notification dispatcher).
- Idempotent; safe to re-run.

### New cron: `backend/app/services/credentials/model_discovery_scheduler.py`

- Mirror `environment_status_scheduler.py` / `environment_suspension_scheduler.py`: module-level `BackgroundScheduler`, `run_*` wrapper calling `asyncio.run(...)`, `start_scheduler()` with `scheduler.add_job(...)`, `shutdown_scheduler()`.
- Interval: configurable via `settings` (default **daily**, e.g. 24h — model lists change rarely; see Open Questions). Add jitter/`max_instances=1`.
- Register `start_scheduler()` / `shutdown` in `backend/app/main.py` alongside the other schedulers (the `start_*_scheduler` block ~L100-154 and shutdown ~L342).
- Add config knobs to `backend/app/core/config.py`: `MODEL_DISCOVERY_ENABLED: bool = True`, `MODEL_DISCOVERY_INTERVAL_HOURS: int = 24`.

**Tests (P3):** service tests with mocked provider clients (anthropic/openai/google happy path + error path + OAuth skip); persistence of the three columns; batch isolation (one failing credential doesn't block others). Cron registration smoke (job added).

---

## Layer / Phase 4 — Detect & Nudge (user-facing model health)

A deprecated/unavailable model is a **config** problem fixed by reconfigure/restart (or editing the override) — **not** a Docker image rebuild. So this is a *new* user-facing config-health signal, NOT folded into the admin-only image-tag `is_stale`.

### New service: `backend/app/services/environments/model_health_service.py`

- `evaluate_environment(session, environment) -> ModelHealth` — for each mode:
  1. Determine `(engine, provider)` from `agent_sdk_<mode>`.
  2. Resolve the effective model via `resolve_model(...)` (including the stored `model_override_<mode>`).
  3. Classify against, in priority order: the linked credential's `discovered_models` (most authoritative, per-key) → `RETIRED_MODELS` → `KNOWN_TIER_WORDS`/catalog known set.
  4. Skip claude-code tier words (`haiku`/`sonnet`/`opus`) — always healthy (CLI resolves).
  5. Emit a per-mode status: `ok` | `retired_override` | `unknown_model` | `unverified` (discovery failed / no data).
- Determine **cause** for the CTA:
  - **Stale default** (no override; resolved default not in discovered list) → CTA "Restart to use the current model".
  - **Frozen override on retired/unavailable model** → CTA "Edit or clear the model override, then restart".

### Model schema — `backend/app/models/environments/environment.py`

Add a **computed, transient** (never persisted) structure to `AgentEnvironmentPublic`, mirroring the `refresh_command_warning` precedent on `AgentStatusPublic`:

- `model_health: ModelHealthPublic | None = None` where `ModelHealthPublic` carries per-mode `{ mode, model, status, cause, suggested_model, cta }` plus a roll-up `has_warning: bool`.

Populate it where `AgentEnvironmentPublic` is built for list/detail responses (env list, agent environments tab). Keep it cheap: it reads catalog + the already-loaded credential's `discovered_models`; no live API calls in the request path.

### Admin column (cheap)

- Add `model_health_warning: bool` (or reuse `model_health`) to `AdminAgentEnvironmentPublic`, computed in `AdminEnvironmentService.list_environments()` (it already iterates every env). Surface as a column next to the existing `StaleBadge`.

### System notification — `model_deprecated`

- Add `NotificationType.MODEL_DEPRECATED = "model_deprecated"` and a `NOTIFICATION_CATALOG` entry (label/description/`default_email_enabled=True`/template/subject/`dedup_scope="environment_id"`). Build `email-templates/build/model_deprecated.html`. **No migration** (catalog-driven, per its docstring).
- Dispatch from the discovery cron (Phase 3) when a refresh newly flags an env, or from `model_health_service` when it transitions an env to a warning state. Reuse the existing failure-isolated dispatch + in-memory throttle.

### API / client

- No new endpoints required if `model_health` rides on existing env list/detail responses. (Optional: a `POST /environments/{id}/clear-model-override` convenience for the override CTA, or just reuse the existing env update path.)
- Regenerate the client: `source ./backend/.venv/bin/activate && make gen-client`.

### Frontend

- **`frontend/src/components/Environments/EnvironmentCard.tsx`** — render a `ModelHealthBadge` (amber/orange, like the admin `StaleBadge`) when `model_health.has_warning`. Tooltip shows per-mode model + reason; primary action button maps to the cause CTA (Restart / Reconfigure, or open the override editor → then restart).
- **`frontend/src/components/Agents/AgentEnvironmentsTab.tsx`** — surface the same badge in the tab list.
- **`frontend/src/components/Environments/AddEnvironment.tsx`** / **`AICredentials.tsx`** — optionally show the credential's `discovered_models` as the model-override `<datalist>` suggestions (replaces/augments the static `SUGGESTED_MODELS`), and warn inline if a typed override isn't in the discovered list.
- React Query: reuse existing env list/`aiCredentialsList` queries; invalidate on restart/override-edit mutations.
- **`frontend/src/components/Admin/Environments/AdminEnvTable.tsx`** — add the model-health column.

**Tests (P4):** `model_health_service` classification matrix (override retired; default stale vs current; tier words always ok; discovery-missing → `unverified`); `AgentEnvironmentPublic.model_health` populated correctly; admin column; notification fires once per env (dedup). Frontend: badge renders on warning, CTA wiring (manual/QA).

---

## Security Architecture

- **Credential secrets** stay encrypted at rest (existing Fernet via `ai_credentials_service`). Discovery decrypts only in-memory inside the cron; `discovered_models` / timestamps / error strings are **non-secret** and safe on `AICredentialPublic`.
- **Access control:** `model_health` on `AgentEnvironmentPublic` is owner-scoped (same guard as the env it rides on). Admin column behind the existing superuser guard. Per-credential discovery only ever uses the owning user's key.
- **No secret leakage in logs:** discovery logs provider + credential id + count, never the key or raw responses. `models_discovery_error` stores a coarse reason code, not raw API error bodies.
- **OAuth tokens:** explicitly skipped for Anthropic discovery; never attempt Messages/Models API calls with `sk-ant-oat*`.
- **Rate/abuse:** cron is daily, `max_instances=1`, per-credential isolation; native list endpoints are cheap GETs.

---

## Database Migrations

1. `add_discovered_models_to_ai_credential.py` (P3) — add nullable `discovered_models` (JSON), `models_discovered_at` (timestamptz), `models_discovery_error` (text) to `ai_credential`. Downgrade drops all three. No backfill.

No other schema changes (Layers 1, 2, 4 are catalog/computed/notification-catalog only).

---

## Error Handling & Edge Cases

- **Discovery failure for one credential** → record `models_discovery_error`, keep prior `discovered_models`, continue batch.
- **No discovery data yet** (fresh key, cron hasn't run) → Layer 4 falls back to `RETIRED_MODELS`/catalog; status `unverified` rather than a false "deprecated".
- **OAuth Anthropic token** → discovery skipped; health uses static fallback so envs aren't false-flagged.
- **`openai_compatible` with no `/models`** → skip discovery; never flag (user-provided endpoint).
- **Unknown `(engine, provider)`** in `resolve_model` → engine-anthropic fallback + warning log (no crash).
- **Override that IS valid but non-default** (user deliberately pinned a current model) → not flagged; only retired/unavailable flags.
- **Catalog vs discovery disagreement** → discovery (per-key) wins for availability; catalog wins for tier defaults.
- **Notification storms** → dedup on `environment_id`; only fire on transition into a warning state.

---

## UI/UX Considerations

- Amber/orange badge consistent with the admin `StaleBadge`; tooltip with per-mode model + plain-language reason.
- Cause-specific CTA copy:
  - Stale default → "This environment will pick up the current model on restart." + Restart button.
  - Retired override → "Conversation/Building is pinned to `<model>`, which is no longer available. Edit or clear the override, then restart." + Edit override + Restart.
  - Unverified → quiet/secondary styling ("couldn't verify available models"), no alarm.
- Model-override inputs suggest the credential's `discovered_models`; inline warning if a typed value isn't in the list.

---

## Integration Points

- **multi-sdk / agent_environment_core**: catalog becomes the documented SSOT for default models (update `multi_sdk_tech.md` default-model table to point at `model_catalog.py`).
- **ai_credentials**: new per-credential cache; update `ai_credentials_tech.md` / `anthropic_credential_types.md` (OAuth discovery skip).
- **admin_agent_environments**: new model-health column beside `is_stale`.
- **system_notifications**: new `model_deprecated` type.
- **API client**: regenerate after P3/P4 (`make gen-client`).

---

## Future Enhancements (Out of Scope)

- **Layer 3+ auto-upgrade** (silent or opt-in) — re-pointing defaults/overrides to the newest in-tier model. Deliberately excluded; current policy is detect-and-nudge.
- **Per-provider (not per-credential) shared cache** to dedupe identical keys.
- **Cost/latency-aware tier selection** from discovered capabilities (Models API exposes capabilities).
- **MiniMax discovery** if/when a list endpoint is available.
- **One-click "upgrade override to current"** action from the badge.

---

## Open Questions

1. **Tier inference** from a raw provider model list — name-heuristic (`haiku/nano/flash`→FAST, etc.) is brittle for new families. Acceptable for *availability* checks (Layer 4 mainly needs "does this ID exist for this key"); only matters if we later auto-suggest a tier-appropriate replacement.
2. **Mapping a discovered concrete ID back to a tier** — needed only for "suggested_model" in the CTA; could defer (CTA can just say "restart to use current default").
3. **Caching granularity** — per-credential (chosen) vs per-provider+key-hash dedupe. Per-credential is simplest and correct; revisit if many duplicate keys.
4. **Cron cadence** — daily proposed; model lists change rarely. Confirm interval + whether to run once on startup.
5. **`openai_compatible` discovery** — assume `/models` OpenAI-shaped; some endpoints differ. Default to skip-on-error.
6. **MODEL_* env var naming** — reuse the dead `MODEL_OVERRIDE_*` names vs introduce `MODEL_<MODE>`; pick one and update `routes.py` debug echo accordingly.

---

## Summary Checklist

### Phase 1 — Catalog (backend)
- [ ] Create `backend/app/services/environments/model_catalog.py` (`ModelTier`, `MODE_TO_TIER`, `DEFAULT_CATALOG`, `RETIRED_MODELS`, `KNOWN_TIER_WORDS`, `resolve_model`, `infer_tier`, `is_retired`).
- [ ] Unit tests for `resolve_model` / `is_retired` / `infer_tier` (no Docker).

### Phase 2 — Resolution wiring (backend + container)
- [ ] `environment_lifecycle.py`: delete `_default_models`; `_build_config` + MiniMax generator call `resolve_model`.
- [ ] Inject per-mode `MODEL_*` env var (host side) computed via catalog; forward through docker-compose templates.
- [ ] `claude_code_sdk_adapter.py`: remove hardcoded `"haiku"`; consume `MODEL_<MODE>` env; honor override.
- [ ] Update `routes.py:689` debug echo to the real value.
- [ ] Tests: opencode.json / minimax settings match catalog + honor override; regression `agents_ai_credential_slot_mismatch_test.py`.

### Phase 3 — Discovery (backend + migration)
- [ ] Add `discovered_models` / `models_discovered_at` / `models_discovery_error` to `AICredential` + `AICredentialPublic`.
- [ ] Migration `add_discovered_models_to_ai_credential.py`.
- [ ] `model_discovery_service.py` (per-provider native listing, OAuth skip, `anyio.to_thread`, failure-isolated batch).
- [ ] `model_discovery_scheduler.py` (APScheduler) + register in `main.py` + config knobs in `config.py`.
- [ ] Tests: mocked provider discovery happy/error/OAuth-skip; persistence; batch isolation.

### Phase 4 — Detect & nudge (backend + frontend)
- [ ] `model_health_service.py` (`evaluate_environment`, cause classification).
- [ ] `ModelHealthPublic` + transient `model_health` on `AgentEnvironmentPublic`; populate in env list/detail builders.
- [ ] Admin model-health column on `AdminAgentEnvironmentPublic` + `AdminEnvironmentService.list_environments()`.
- [ ] `NotificationType.MODEL_DEPRECATED` + catalog entry + `model_deprecated.html`; dispatch on new flag (dedup on `environment_id`).
- [ ] `make gen-client`.
- [ ] Frontend: `ModelHealthBadge` on `EnvironmentCard.tsx` + `AgentEnvironmentsTab.tsx` with cause CTA; discovered-models suggestions in `AddEnvironment.tsx` / `AICredentials.tsx`; admin column in `AdminEnvTable.tsx`.

### Testing & validation
- [ ] Default resolution correct per engine/provider/mode (incl. tier words for claude-code).
- [ ] Override honored by all engines (claude-code bug fixed).
- [ ] Retired override flags + correct CTA; stale default flags + restart CTA; tier words never flagged; no-discovery → `unverified`.
- [ ] Discovery cron populates per-credential lists; OAuth tokens skipped; one bad key doesn't break the batch.
- [ ] `model_deprecated` email fires once per env; respects user notification prefs.

### Docs to update
- [ ] `docs/agents/agent_environment_core/multi_sdk_tech.md` (default-model table → catalog SSOT; `MODEL_*` env vars).
- [ ] `docs/application/ai_credentials/ai_credentials_tech.md` + `anthropic_credential_types.md` (per-credential discovery; OAuth skip).
- [ ] `docs/application/admin_agent_environments/*` (model-health column).
- [ ] `docs/application/system_notifications/*` (`model_deprecated` type).
- [ ] New feature docs (business + tech) under `docs/agents/agent_environments/` (e.g. `model_freshness.md` / `_tech.md`) + Feature Registry row in `docs/README.md`.
