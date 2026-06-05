# AI Model Freshness & Deprecation Nudges

## Purpose

Keep agent environments from silently running on stale or retired LLM models.
When an environment's configured model is deprecated or unavailable to its credential,
the platform surfaces a **model-health badge** (amber, next to the environment card) with
a cause-specific call to action. The policy is **detect-and-nudge**, never auto-upgrade:
the user always decides when and how to update.

Model staleness is a **configuration** problem (fix: reconfigure or clear the override, then
restart), not a Docker image problem. It is deliberately distinct from the existing image-tag
`is_stale` flag (fix: rebuild the Docker image).

## Core Concepts

### Catalog SSOT

`backend/app/services/environments/model_catalog.py` is the single source of truth for default
model selection. It replaced three previously scattered hardcoded model maps. Two concepts
drive selection:

- **Tier** — `FAST` (conversation mode: cheap/low-latency) and `BALANCED` (building mode:
  more capable).
- **Engine/provider pair** — the catalog stores a default per `(engine, provider)` combination
  (e.g. `opencode/anthropic`, `claude-code/minimax`). For `claude-code/anthropic` the stored
  value is a tier **word** (`haiku` / `sonnet`) rather than a concrete dated snapshot — the
  Claude Code CLI resolves tier words to the current model automatically, so they never go
  stale and are never flagged as deprecated.

A notable freshness fix shipped with the catalog: the opencode/anthropic building-mode default
was updated from the stale `anthropic/claude-sonnet-4-5` to `anthropic/claude-sonnet-4-6`.

### Model Override

Users can pin a concrete model ID per mode (`model_override_conversation` /
`model_override_building` on `AgentEnvironment`). Overrides are honored verbatim in resolution;
detecting whether an override is still valid is the health service's job, not the resolver's.

### Per-Credential Model Discovery

Different API keys can access different models. The platform caches each AI credential's
available model list (`AICredential.discovered_models`) by polling the provider's native
`/models` endpoint via a daily background cron. Discovery results are the most authoritative
source for health classification; the curated `RETIRED_MODELS` set in the catalog is the
fallback when no discovery data exists yet.

Users can also **force an immediate refresh** for a saved credential by clicking **Test Connection** in the Edit AI Credential dialog — this probes the provider and persists the fresh model list right away, without waiting for the next cron run. See [AI Credentials](../../application/ai_credentials/ai_credentials.md) for details.

### Model Health Signal

`model_health_service.evaluate_environment` computes a per-mode health classification without
live API calls:

| Status | Meaning |
|--------|---------|
| `ok` | Model is available and healthy |
| `retired_override` | A `model_override_*` points at a retired/unavailable model |
| `unknown_model` | The resolved catalog default is not in the credential's discovered list |
| `unverified` | Discovery has not run yet or failed; no false alarm raised |

A roll-up `has_warning: bool` flags any mode that is `retired_override` or `unknown_model`.

Two **cause** codes drive the CTA copy:

- `frozen_override` — the user has a pinned override that is no longer available. CTA:
  "Edit or clear the model override, then restart."
- `stale_default` — the resolved catalog default is not in the discovered list. CTA:
  "Restart to use the current model."

The `unverified` status intentionally produces no warning badge — the platform does not raise
a false alarm when it simply has no data yet.

## User Flows

### 1. Discovering a deprecated model via a badge

1. User opens the Environments tab for their agent.
2. An amber **Model Health** badge appears on one of the environment cards.
3. User hovers or clicks the badge. A tooltip shows the per-mode model + reason:
   - "Conversation is pinned to `claude-3-5-sonnet-20241022`, which is no longer available."
   - Primary action button: "Edit override" → opens the environment reconfigure dialog, then restart.
4. User clears or updates the override, reconfigures the environment, and restarts. Badge disappears.

### 2. Stale catalog default surfaced after discovery

1. Discovery cron runs overnight. Credential `discovered_models` refreshes.
2. User opens environments tab next morning. Badge appears on an environment whose catalog
   default is not in the discovered list.
3. Badge tooltip: "Building mode is using `anthropic/claude-sonnet-4-5`, which is not available
   to this credential. Restart to use the current model."
4. User restarts. Lifecycle regenerates the config, picking the current catalog default.

### 3. Email notification on new model deprecation

1. Discovery cron completes a refresh batch.
2. For any environment that newly transitioned into a warning state, the platform sends a
   `model_deprecated` email to the agent owner.
3. Email subject: `{PROJECT_NAME} — Update the AI model for {instance_name}`.
4. Body lists the affected mode(s) and their remediation CTAs. Deep link to the agent page.
5. The notification fires only on **transition** into a warning state — not on every daily cron
   run for a persistently-unchecked environment.

### 4. Admin oversight

1. Admin opens Admin → Agent Environments.
2. A "Model Health" column appears beside the existing Stale column.
3. Rows with `model_health_warning = true` show an amber indicator.
4. Admin can investigate which environments need attention across the whole fleet.

## Business Rules

- **Detect-and-nudge only** — the platform never auto-upgrades a model, not even when discovery
  confirms a newer one exists. The user always triggers any change.
- **Override is always honored** — a pinned `model_override_*` is used verbatim. The health
  service validates it; the resolver does not.
- **Tier words are always healthy** — `haiku`, `sonnet`, `opus` (claude-code/anthropic) auto-track
  the current model via the CLI and are never classified as deprecated.
- **`openai_compatible` is always ok** — the user owns the model namespace for custom endpoints;
  the platform never flags them.
- **Discovery skip is safe** — Anthropic OAuth tokens (`sk-ant-oat*`) cannot call the models
  listing API. Discovery records `oauth_token_unsupported` and leaves `discovered_models`
  unchanged. Health classification falls back to the static catalog.
- **MiniMax has no discovery** — no public `/models` endpoint is assumed. Classification is
  catalog-only for MiniMax credentials.
- **Notification fires once per transition** — the in-memory `_warned_env_ids` set records
  environments already in a warning state; the platform emails the owner only when an environment
  **enters** a warning state, not on every cron iteration. The `dedup_scope="environment_id"`
  throttle is a second line of defense.
- **No schema migration for the notification type** — `MODEL_DEPRECATED` follows the
  [Notification Catalog](../../application/system_notifications/system_notifications.md)
  pattern: adding a new type requires only a catalog entry and a built email template.
- **Unverified is quiet** — when a credential has never been discovered (fresh key, cron has not
  yet run), status is `unverified` and no badge or email is raised.

## Integration Points

| System | Integration |
|--------|-------------|
| [multi-sdk / agent_environment_core](../agent_environment_core/multi_sdk.md) | The catalog is the SSOT for default model resolution. `resolve_model` is called from environment lifecycle generation and the health service. |
| [AI Credentials](../../application/ai_credentials/ai_credentials.md) | `AICredential.discovered_models` is the per-key available-model cache. The discovery cron populates it; the health service reads it. |
| [Admin Agent Environments](../../application/admin_agent_environments/admin_agent_environments.md) | `model_health_warning` column surfaces alongside `is_stale` in the fleet console. |
| [System Notifications](../../application/system_notifications/system_notifications.md) | `model_deprecated` catalog type; dispatched from the discovery cron on transition into a warning state. |
| [Agent Environments](agent_environments.md) | `model_health` is a transient field on `AgentEnvironmentPublic`; no new DB column on the environment. |

## Edge Cases

- **No discovery data yet** — fresh credential before the cron has run. Status: `unverified`. No badge, no email. First cron run resolves it.
- **Discovery failure** — `models_discovery_error` records the coarse reason. Prior `discovered_models` are retained. Health classification falls back to the retired set.
- **Override that is valid but non-default** — a user who deliberately pinned a current model is never flagged; only retired/unavailable models flag.
- **Both modes affected** — `has_warning` is true when any mode is flagged; the badge tooltip shows all affected modes.
- **Restart vs rebuild** — restarting (or reconfiguring) re-runs lifecycle generation, which picks up the updated catalog default. A Docker image rebuild is NOT required to change the model.

---

*Last updated: 2026-06-05 — noted manual force-refresh via Test Connection*
